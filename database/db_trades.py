"""
Профессиональная система управления базой данных для многопользовательского торгового бота
"""
import asyncio
import asyncpg
from datetime import datetime, timezone, timedelta
from typing import Optional, List, Dict, Any, Union, Tuple
from decimal import Decimal
from contextlib import asynccontextmanager
import json
import ssl
from dataclasses import dataclass, asdict
from enum import Enum

from core.logger import log_info, log_error, log_warning, log_debug
from core.settings_config import system_config
from core.enums import OrderStatus, PositionSide, StrategyType, TradingStatus
from core.functions import to_decimal, DecimalEncoder
from cryptography.fernet import Fernet
import base64
import os


class DatabaseError(Exception):
    """Базовое исключение для ошибок базы данных"""
    pass

class DBConnectionError(DatabaseError):
    """Ошибка соединения с базой данных"""
    pass

class QueryError(DatabaseError):
    """Ошибка выполнения запроса"""
    pass

@dataclass
class UserProfile:
    """Профиль пользователя"""
    user_id: int
    username: Optional[str] = None
    first_name: Optional[str] = None
    last_name: Optional[str] = None
    is_active: bool = True
    is_premium: bool = False
    registration_date: Optional[datetime] = None
    last_activity: Optional[datetime] = None
    winning_trades: int = 0
    total_trades: int = 0
    total_profit: Decimal = Decimal('0')
    max_drawdown: Decimal = Decimal('0')
    win_rate: Decimal = Decimal('0')

@dataclass
class UserApiKeys:
    """API ключи пользователя"""
    user_id: int
    exchange: str
    api_key: str
    secret_key: str
    passphrase: Optional[str] = None
    is_active: bool = True
    created_at: Optional[datetime] = None
    updated_at: Optional[datetime] = None

@dataclass
class TradeRecord:
    """Запись о сделке"""
    id: Optional[int] = None
    user_id: int = 0
    symbol: str = ""
    side: str = ""
    entry_price: Decimal = Decimal('0')
    exit_price: Decimal = Decimal('0')
    quantity: Decimal = Decimal('0')
    leverage: int = 1
    profit: Decimal = Decimal('0')
    commission: Decimal = Decimal('0')
    status: str = TradingStatus.TRADING.value
    strategy_type: str = ""
    order_id: Optional[str] = None
    position_idx: int = 0
    entry_time: Optional[datetime] = None
    exit_time: Optional[datetime] = None
    metadata: Optional[Dict[str, Any]] = None

@dataclass
class PositionRecord:
    """Запись о позиции"""
    id: Optional[int] = None
    user_id: int = 0
    symbol: str = ""
    side: str = ""
    size: Decimal = Decimal('0')
    entry_price: Decimal = Decimal('0')
    current_price: Decimal = Decimal('0')
    unrealized_pnl: Decimal = Decimal('0')
    realized_pnl: Decimal = Decimal('0')
    leverage: int = 1
    margin: Decimal = Decimal('0')
    status: str = "ACTIVE"
    strategy_type: str = ""
    created_at: Optional[datetime] = None
    updated_at: Optional[datetime] = None
    metadata: Optional[Dict[str, Any]] = None

@dataclass
class OrderRecord:
    """Запись об ордере"""
    id: Optional[int] = None
    user_id: int = 0
    symbol: str = ""
    side: str = ""
    order_type: str = ""
    quantity: Decimal = Decimal('0')
    price: Decimal = Decimal('0')
    filled_quantity: Decimal = Decimal('0')
    average_price: Decimal = Decimal('0')
    status: str = OrderStatus.NEW.value
    order_id: str = ""
    client_order_id: Optional[str] = None
    strategy_type: str = ""
    created_at: Optional[datetime] = None
    updated_at: Optional[datetime] = None
    metadata: Optional[Dict[str, Any]] = None


# константы для переподключения к БД
DB_RETRY_COUNT = 5
DB_RETRY_DELAY = 10  # секунд


class _DatabaseManager:
    def __init__(self):
        self.pool: Optional[asyncpg.Pool] = None
        self._is_initialized = False
        self._lock = asyncio.Lock()
        self._encryption_key: Optional[str] = None

    async def initialize(self) -> None:
        """Инициализация единственного пула соединений и других асинхронных компонентов."""
        async with self._lock:
            if self._is_initialized:
                return

            log_info(0, "Инициализация DatabaseManager...", 'database')

            # --- 1. СНАЧАЛА НАСТРАИВАЕМ ШИФРОВАНИЕ ---
            # Эта операция теперь выполняется асинхронно и не блокирует импорт
            try:
                key = system_config.encryption_key
                if not key: raise ValueError("ENCRYPTION_KEY не найден")
                Fernet(key.encode()) # Проверка валидности
                self._encryption_key = key
                log_info(0, "Ключ шифрования успешно настроен.", 'database')
            except Exception as e:
                log_error(0, f"Критическая ошибка инициализации ключа шифрования: {e}", 'database')
                raise

            # Используем цикл повторных попыток, как вы и просили
            for attempt in range(DB_RETRY_COUNT):
                try:
                    # --- КОНФИГУРАЦИЯ СТРОГО ИЗ ВАШЕГО РАБОЧЕГО ПРИМЕРА ---
                    self.pool = await asyncpg.create_pool(
                        dsn=system_config.database.url,
                        ssl='require',
                        min_size=2,
                        max_size=10,
                        command_timeout=60,
                        timeout=30,
                        server_settings={'jit': 'off'}
                    )

                    log_info(0, f"Пул соединений с PostgreSQL успешно создан (попытка {attempt + 1}).", 'database')

                    # Настройка и создание таблиц ПОСЛЕ успешного подключения
                    self._setup_encryption()
                    await self._create_tables()
                    await self._run_migrations()
                    await self._create_indexes()

                    self._is_initialized = True
                    return  # Успех, выходим из функции

                except Exception as e:
                    log_error(0, f"Ошибка подключения к БД (попытка {attempt + 1}/{DB_RETRY_COUNT}): {e}", 'database')
                    if attempt == DB_RETRY_COUNT - 1:
                        raise ConnectionError(f"Не удалось подключиться к БД после {DB_RETRY_COUNT} попыток.")
                    await asyncio.sleep(DB_RETRY_DELAY)

    async def _run_migrations(self):
        """Проверяет и применяет необходимые изменения к схеме БД."""
        log_info(0, "Запуск миграций базы данных...", 'database')
        try:
            async with self.get_connection() as conn:
                # Миграция 1: Добавление колонки winning_trades в таблицу users
                check_col_query = """
                SELECT 1 FROM information_schema.columns 
                WHERE table_name='users' AND column_name='winning_trades'
                """
                column_exists = await conn.fetchval(check_col_query)
                if not column_exists:
                    log_warning(0, "Колонка 'winning_trades' отсутствует в таблице 'users'. Добавляю...", 'database')
                    await conn.execute("ALTER TABLE users ADD COLUMN winning_trades INTEGER DEFAULT 0;")
                    log_info(0, "Колонка 'winning_trades' успешно добавлена.", 'database')

                # Сюда можно добавлять другие проверки и миграции в будущем

            log_info(0, "Миграции базы данных завершены.", 'database')
        except Exception as e:
            log_error(0, f"Ошибка во время выполнения миграций: {e}", 'database')
            raise



    def _setup_encryption(self) -> None:
        """Настройка шифрования"""
        try:
            # --- Обращаемся к ключу напрямую из system_config ---
            encryption_key = system_config.encryption_key
            if not encryption_key:
                raise ValueError("ENCRYPTION_KEY не настроен")
            
            # Проверяем формат ключа
            try:
                key_bytes = base64.urlsafe_b64decode(encryption_key.encode())
                if len(key_bytes) != 32:
                    raise ValueError("Ключ должен быть 32 байта")
            except Exception:
                # Если ключ не в base64, генерируем новый
                key_bytes = Fernet.generate_key()
                encryption_key = base64.urlsafe_b64encode(key_bytes).decode()
                log_warning(0, "Сгенерирован новый ключ шифрования", module_name='database')
            
            self._encryption_key = encryption_key
            log_info(0, "Система шифрования настроена", module_name='database')
            
        except Exception as e:
            log_error(0, f"Ошибка настройки шифрования: {e}", module_name='database')
            raise
    
    def encrypt_data(self, data: str) -> str:
        """Шифрование данных"""
        try:
            if not self._encryption_key:
                raise ValueError("Ключ шифрования не настроен")
            
            f = Fernet(self._encryption_key.encode())
            encrypted_data = f.encrypt(data.encode())
            return base64.urlsafe_b64encode(encrypted_data).decode()
            
        except Exception as e:
            log_error(0, f"Ошибка шифрования: {e}", module_name='database')
            raise
    
    def decrypt_data(self, encrypted_data: str) -> str:
        """Расшифровка данных"""
        try:
            if not self._encryption_key:
                raise ValueError("Ключ шифрования не настроен")
            
            f = Fernet(self._encryption_key.encode())
            decoded_data = base64.urlsafe_b64decode(encrypted_data.encode())
            decrypted_data = f.decrypt(decoded_data)
            return decrypted_data.decode()
            
        except Exception as e:
            log_error(0, f"Ошибка расшифровки: {e}", module_name='database')
            raise
    
    async def _create_tables(self) -> None:
        """Создание всех таблиц"""
        try:
            # Таблица пользователей
            await self._execute_query("""
                CREATE TABLE IF NOT EXISTS users (
                    user_id BIGINT PRIMARY KEY,
                    username VARCHAR(255),
                    first_name VARCHAR(255),
                    last_name VARCHAR(255),
                    is_active BOOLEAN DEFAULT TRUE,
                    is_premium BOOLEAN DEFAULT FALSE,
                    registration_date TIMESTAMPTZ DEFAULT NOW(),
                    last_activity TIMESTAMPTZ DEFAULT NOW(),
                    winning_trades INTEGER DEFAULT 0,
                    total_trades INTEGER DEFAULT 0,
                    total_profit DECIMAL(20,8) DEFAULT 0,
                    max_drawdown DECIMAL(10,4) DEFAULT 0,
                    win_rate DECIMAL(5,2) DEFAULT 0,
                    metadata JSONB DEFAULT '{}'::jsonb,
                    created_at TIMESTAMPTZ DEFAULT NOW(),
                    updated_at TIMESTAMPTZ DEFAULT NOW()
                )
            """)
            
            # Таблица API ключей
            await self._execute_query("""
                CREATE TABLE IF NOT EXISTS user_api_keys (
                    id SERIAL PRIMARY KEY,
                    user_id BIGINT NOT NULL REFERENCES users(user_id) ON DELETE CASCADE,
                    exchange VARCHAR(50) NOT NULL,
                    api_key_encrypted TEXT NOT NULL,
                    secret_key_encrypted TEXT NOT NULL,
                    passphrase_encrypted TEXT,
                    is_active BOOLEAN DEFAULT TRUE,
                    created_at TIMESTAMPTZ DEFAULT NOW(),
                    updated_at TIMESTAMPTZ DEFAULT NOW(),
                    UNIQUE(user_id, exchange)
                )
            """)
            
            # Таблица сделок
            await self._execute_query("""
                CREATE TABLE IF NOT EXISTS trades (
                    id SERIAL PRIMARY KEY,
                    user_id BIGINT NOT NULL REFERENCES users(user_id) ON DELETE CASCADE,
                    symbol VARCHAR(50) NOT NULL,
                    side VARCHAR(10) NOT NULL,
                    entry_price DECIMAL(20,8) NOT NULL,
                    exit_price DECIMAL(20,8) DEFAULT 0,
                    quantity DECIMAL(20,8) NOT NULL,
                    leverage INTEGER DEFAULT 1,
                    profit DECIMAL(20,8) DEFAULT 0,
                    commission DECIMAL(20,8) DEFAULT 0,
                    status VARCHAR(20) DEFAULT 'ACTIVE',
                    strategy_type VARCHAR(50),
                    order_id VARCHAR(100),
                    position_idx INTEGER DEFAULT 0,
                    entry_time TIMESTAMPTZ DEFAULT NOW(),
                    exit_time TIMESTAMPTZ,
                    metadata JSONB DEFAULT '{}'::jsonb,
                    created_at TIMESTAMPTZ DEFAULT NOW(),
                    updated_at TIMESTAMPTZ DEFAULT NOW()
                )
            """)
            
            # Таблица позиций
            await self._execute_query("""
                CREATE TABLE IF NOT EXISTS positions (
                    id SERIAL PRIMARY KEY,
                    user_id BIGINT NOT NULL REFERENCES users(user_id) ON DELETE CASCADE,
                    symbol VARCHAR(50) NOT NULL,
                    side VARCHAR(10) NOT NULL,
                    size DECIMAL(20,8) NOT NULL,
                    entry_price DECIMAL(20,8) NOT NULL,
                    current_price DECIMAL(20,8) DEFAULT 0,
                    unrealized_pnl DECIMAL(20,8) DEFAULT 0,
                    realized_pnl DECIMAL(20,8) DEFAULT 0,
                    leverage INTEGER DEFAULT 1,
                    margin DECIMAL(20,8) DEFAULT 0,
                    status VARCHAR(20) DEFAULT 'ACTIVE',
                    strategy_type VARCHAR(50),
                    metadata JSONB DEFAULT '{}'::jsonb,
                    created_at TIMESTAMPTZ DEFAULT NOW(),
                    updated_at TIMESTAMPTZ DEFAULT NOW(),
                    UNIQUE(user_id, symbol, side)
                )
            """)
            
            # Таблица ордеров
            await self._execute_query("""
                CREATE TABLE IF NOT EXISTS orders (
                    id SERIAL PRIMARY KEY,
                    user_id BIGINT NOT NULL REFERENCES users(user_id) ON DELETE CASCADE,
                    symbol VARCHAR(50) NOT NULL,
                    side VARCHAR(10) NOT NULL,
                    order_type VARCHAR(20) NOT NULL,
                    quantity DECIMAL(20,8) NOT NULL,
                    price DECIMAL(20,8) DEFAULT 0,
                    filled_quantity DECIMAL(20,8) DEFAULT 0,
                    average_price DECIMAL(20,8) DEFAULT 0,
                    status VARCHAR(20) DEFAULT 'PENDING',
                    order_id VARCHAR(100) NOT NULL,
                    client_order_id VARCHAR(100),
                    strategy_type VARCHAR(50),
                    metadata JSONB DEFAULT '{}'::jsonb,
                    created_at TIMESTAMPTZ DEFAULT NOW(),
                    updated_at TIMESTAMPTZ DEFAULT NOW()
                )
            """)
            
            # Таблица стратегий
            await self._execute_query("""
                CREATE TABLE IF NOT EXISTS user_strategies (
                    id SERIAL PRIMARY KEY,
                    user_id BIGINT NOT NULL REFERENCES users(user_id) ON DELETE CASCADE,
                    strategy_type VARCHAR(50) NOT NULL,
                    symbol VARCHAR(50) NOT NULL,
                    is_active BOOLEAN DEFAULT FALSE,
                    config JSONB NOT NULL DEFAULT '{}'::jsonb,
                    statistics JSONB DEFAULT '{}'::jsonb,
                    created_at TIMESTAMPTZ DEFAULT NOW(),
                    updated_at TIMESTAMPTZ DEFAULT NOW(),
                    UNIQUE(user_id, strategy_type, symbol)
                )
            """)
            await self._execute_query("""
                            CREATE TABLE IF NOT EXISTS user_strategy_stats (
                                id SERIAL PRIMARY KEY,
                                user_id BIGINT NOT NULL REFERENCES users(user_id) ON DELETE CASCADE,
                                strategy_type VARCHAR(50) NOT NULL,
                                total_trades INTEGER DEFAULT 0,
                                winning_trades INTEGER DEFAULT 0,
                                total_pnl DECIMAL(20, 8) DEFAULT 0,
                                updated_at TIMESTAMPTZ DEFAULT NOW(),
                                UNIQUE(user_id, strategy_type)
                            )
                        """)
            # Таблица уведомлений
            await self._execute_query("""
                CREATE TABLE IF NOT EXISTS notifications (
                    id SERIAL PRIMARY KEY,
                    user_id BIGINT NOT NULL REFERENCES users(user_id) ON DELETE CASCADE,
                    type VARCHAR(50) NOT NULL,
                    title VARCHAR(255) NOT NULL,
                    message TEXT NOT NULL,
                    is_read BOOLEAN DEFAULT FALSE,
                    metadata JSONB DEFAULT '{}'::jsonb,
                    created_at TIMESTAMPTZ DEFAULT NOW()
                )
            """)
            
            log_info(0, "Все таблицы созданы успешно", module_name='database')
            
        except Exception as e:
            log_error(0, f"Ошибка создания таблиц: {e}", module_name='database')
            raise
    
    async def _create_indexes(self) -> None:
        """Создание индексов для оптимизации"""
        try:
            indexes = [
                "CREATE INDEX IF NOT EXISTS idx_trades_user_id ON trades(user_id)",
                "CREATE INDEX IF NOT EXISTS idx_trades_symbol ON trades(symbol)",
                "CREATE INDEX IF NOT EXISTS idx_trades_status ON trades(status)",
                "CREATE INDEX IF NOT EXISTS idx_trades_entry_time ON trades(entry_time)",
                "CREATE INDEX IF NOT EXISTS idx_positions_user_id ON positions(user_id)",
                "CREATE INDEX IF NOT EXISTS idx_positions_symbol ON positions(symbol)",
                "CREATE INDEX IF NOT EXISTS idx_positions_status ON positions(status)",
                "CREATE INDEX IF NOT EXISTS idx_orders_user_id ON orders(user_id)",
                "CREATE INDEX IF NOT EXISTS idx_orders_symbol ON orders(symbol)",
                "CREATE INDEX IF NOT EXISTS idx_orders_status ON orders(status)",
                "CREATE INDEX IF NOT EXISTS idx_orders_order_id ON orders(order_id)",
                "CREATE INDEX IF NOT EXISTS idx_user_strategies_user_id ON user_strategies(user_id)",
                "CREATE INDEX IF NOT EXISTS idx_user_strategies_active ON user_strategies(is_active)",
                "CREATE INDEX IF NOT EXISTS idx_notifications_user_id ON notifications(user_id)",
                "CREATE INDEX IF NOT EXISTS idx_notifications_unread ON notifications(user_id, is_read)",
            ]
            
            for index_query in indexes:
                await self._execute_query(index_query)
            
            log_info(0, "Индексы созданы успешно", module_name='database')
            
        except Exception as e:
            log_error(0, f"Ошибка создания индексов: {e}", module_name='database')
            raise
    
    @asynccontextmanager
    async def get_connection(self):
        """Context manager для получения соединения"""
        if not self.pool:
            raise ConnectionError("Пул соединений не инициализирован")
        
        async with self.pool.acquire() as connection:
            try:
                yield connection
            except Exception as e:
                log_error(0, f"Ошибка в соединении с БД: {e}", module_name='database')
                raise
    
    async def _execute_query(self, query: str, params: tuple = (), fetch_one: bool = False, fetch_all: bool = False):
        """Выполнение SQL запроса"""
        try:
            async with self.get_connection() as conn:
                if fetch_one:
                    result = await conn.fetchrow(query, *params)
                    return dict(result) if result else None
                elif fetch_all:
                    result = await conn.fetch(query, *params)
                    return [dict(row) for row in result]
                else:
                    await conn.execute(query, *params)
                    return None
                    
        except Exception as e:
            log_error(0, f"Ошибка выполнения запроса: {e}", module_name='database')
            log_error(0, f"Запрос: {query[:200]}...", module_name='database')
            raise QueryError(f"Ошибка выполнения запроса: {e}")
    
    # Методы для работы с пользователями
    async def create_user(self, user_profile: UserProfile) -> bool:
        """Создание пользователя"""
        try:
            query = """
                INSERT INTO users (user_id, username, first_name, last_name, is_active, is_premium)
                VALUES ($1, $2, $3, $4, $5, $6)
                ON CONFLICT (user_id) DO UPDATE SET
                    username = EXCLUDED.username,
                    first_name = EXCLUDED.first_name,
                    last_name = EXCLUDED.last_name,
                    last_activity = NOW()
            """
            
            await self._execute_query(query, (
                user_profile.user_id,
                user_profile.username,
                user_profile.first_name,
                user_profile.last_name,
                user_profile.is_active,
                user_profile.is_premium
            ))
            
            log_info(user_profile.user_id, f"Пользователь {user_profile.user_id} создан/обновлен", module_name='database')
            return True
            
        except Exception as e:
            log_error(user_profile.user_id, f"Ошибка создания пользователя: {e}", module_name='database')
            return False
    
    async def get_user(self, user_id: int) -> Optional[UserProfile]:
        """Получение пользователя"""
        try:
            query = "SELECT * FROM users WHERE user_id = $1"
            result = await self._execute_query(query, (user_id,), fetch_one=True)
            
            if result:
                return UserProfile(
                    user_id=result['user_id'],
                    username=result['username'],
                    first_name=result['first_name'],
                    last_name=result['last_name'],
                    is_active=result['is_active'],
                    is_premium=result['is_premium'],
                    registration_date=result['registration_date'],
                    last_activity=result['last_activity'],
                    winning_trades=result['winning_trades'],
                    total_trades=result['total_trades'],
                    total_profit=to_decimal(result['total_profit']),
                    max_drawdown=to_decimal(result['max_drawdown']),
                    win_rate=to_decimal(result['win_rate'])
                )
            
            return None
            
        except Exception as e:
            log_error(user_id, f"Ошибка получения пользователя: {e}", module_name='database')
            return None
    
    async def save_api_keys(self, user_id: int, exchange: str, api_key: str, secret_key: str, passphrase: str = None) -> bool:
        """Сохранение API ключей"""
        try:
            encrypted_api_key = self.encrypt_data(api_key)
            encrypted_secret_key = self.encrypt_data(secret_key)
            encrypted_passphrase = self.encrypt_data(passphrase) if passphrase else None
            
            query = """
                INSERT INTO user_api_keys (user_id, exchange, api_key_encrypted, secret_key_encrypted, passphrase_encrypted)
                VALUES ($1, $2, $3, $4, $5)
                ON CONFLICT (user_id, exchange) DO UPDATE SET
                    api_key_encrypted = EXCLUDED.api_key_encrypted,
                    secret_key_encrypted = EXCLUDED.secret_key_encrypted,
                    passphrase_encrypted = EXCLUDED.passphrase_encrypted,
                    updated_at = NOW()
            """
            
            await self._execute_query(query, (
                user_id, exchange, encrypted_api_key, encrypted_secret_key, encrypted_passphrase
            ))
            
            log_info(user_id, f"API ключи для биржи {exchange} сохранены", module_name='database')
            return True
            
        except Exception as e:
            log_error(user_id, f"Ошибка сохранения API ключей: {e}", module_name='database')
            return False
    
    async def get_api_keys(self, user_id: int, exchange: str) -> Optional[Tuple[str, str, str]]:
        """Получение API ключей"""
        try:
            query = """
                SELECT api_key_encrypted, secret_key_encrypted, passphrase_encrypted 
                FROM user_api_keys 
                WHERE user_id = $1 AND exchange = $2 AND is_active = TRUE
            """
            
            result = await self._execute_query(query, (user_id, exchange), fetch_one=True)
            
            if result:
                api_key = self.decrypt_data(result['api_key_encrypted'])
                secret_key = self.decrypt_data(result['secret_key_encrypted'])
                passphrase = self.decrypt_data(result['passphrase_encrypted']) if result['passphrase_encrypted'] else None

                return api_key, secret_key, passphrase
            
            return None
            
        except Exception as e:
            log_error(user_id, f"Ошибка получения API ключей: {e}", module_name='database')
            return None

    async def save_trade(self, trade: TradeRecord) -> Optional[int]:
        """Сохранение сделки"""
        try:
            query = """
                INSERT INTO trades (
                    user_id, symbol, side, entry_price, exit_price, quantity, leverage,
                    profit, commission, status, strategy_type, order_id, position_idx,
                    entry_time, exit_time, metadata
                ) VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10, $11, $12, $13, $14, $15, $16)
                RETURNING id
            """

            result = await self._execute_query(query, (
                trade.user_id, trade.symbol, trade.side, trade.entry_price, trade.exit_price,
                trade.quantity, trade.leverage, trade.profit, trade.commission, trade.status,
                trade.strategy_type, trade.order_id, trade.position_idx, trade.entry_time,
                trade.exit_time, json.dumps(trade.metadata or {}, cls=DecimalEncoder)
            ), fetch_one=True)

            trade_id = result['id'] if result else None
            log_info(trade.user_id, f"Сделка сохранена с ID: {trade_id}", module_name='database')
            return trade_id

        except Exception as e:
            log_error(trade.user_id, f"Ошибка сохранения сделки: {e}", module_name='database')
            return None

    async def update_trade_on_close(self, trade_id: int, exit_price: Decimal, pnl: Decimal, commission: Decimal,
                                    exit_time: datetime) -> bool:
        """Обновление записи о сделке при ее закрытии."""
        try:
            query = """
                UPDATE trades
                SET 
                    exit_price = $1,
                    profit = $2,
                    commission = $3,
                    exit_time = $4,
                    status = 'CLOSED',
                    updated_at = NOW()
                WHERE id = $5
            """
            await self._execute_query(query, (exit_price, pnl, commission, exit_time, trade_id))
            log_info(0, f"Сделка с ID {trade_id} успешно обновлена и закрыта в БД.", module_name='database')
            return True
        except Exception as e:
            log_error(0, f"Ошибка обновления сделки {trade_id} в БД: {e}", module_name='database')
            return False
    
    async def get_user_trades(self, user_id: int, limit: int = 100, offset: int = 0) -> List[TradeRecord]:
        """Получение сделок пользователя"""
        try:
            query = """
                SELECT * FROM trades 
                WHERE user_id = $1 
                ORDER BY created_at DESC 
                LIMIT $2 OFFSET $3
            """
            
            results = await self._execute_query(query, (user_id, limit, offset), fetch_all=True)
            
            trades = []
            for result in results:
                trade = TradeRecord(
                    id=result['id'],
                    user_id=result['user_id'],
                    symbol=result['symbol'],
                    side=result['side'],
                    entry_price=to_decimal(result['entry_price']),
                    exit_price=to_decimal(result['exit_price']),
                    quantity=to_decimal(result['quantity']),
                    leverage=result['leverage'],
                    profit=to_decimal(result['profit']),
                    commission=to_decimal(result['commission']),
                    status=result['status'],
                    strategy_type=result['strategy_type'],
                    order_id=result['order_id'],
                    position_idx=result['position_idx'],
                    entry_time=result['entry_time'],
                    exit_time=result['exit_time'],
                    metadata=result['metadata']
                )
                trades.append(trade)
            
            return trades
            
        except Exception as e:
            log_error(user_id, f"Ошибка получения сделок: {e}", module_name='database')
            return []
    
    async def close(self) -> None:
        """Закрытие соединений"""
        try:
            if self.pool:
                await self.pool.close()
                log_info(0, "Пул соединений закрыт", module_name='database')
        except Exception as e:
            log_error(0, f"Ошибка закрытия пула соединений: {e}", module_name='database')

    async def update_strategy_stats(self, user_id: int, strategy_type: str, pnl: Decimal) -> Decimal:
        """
        Обновляет статистику для конкретной стратегии пользователя и возвращает обновленный Win Rate.
        """
        try:
            win_increment = 1 if pnl > 0 else 0

            query = """
                INSERT INTO user_strategy_stats (user_id, strategy_type, total_trades, winning_trades, total_pnl)
                VALUES ($1, $2, 1, $3, $4)
                ON CONFLICT (user_id, strategy_type) DO UPDATE SET
                    total_trades = user_strategy_stats.total_trades + 1,
                    winning_trades = user_strategy_stats.winning_trades + $3,
                    total_pnl = user_strategy_stats.total_pnl + $4,
                    updated_at = NOW()
                RETURNING total_trades, winning_trades;
            """

            result = await self._execute_query(query, (user_id, strategy_type, win_increment, pnl), fetch_one=True)

            if result and result['total_trades'] > 0:
                win_rate = (Decimal(result['winning_trades']) / Decimal(result['total_trades'])) * 100
                return win_rate

            return Decimal('0')

        except Exception as e:
            log_error(user_id, f"Ошибка обновления статистики стратегии {strategy_type}: {e}", module_name='database')
            return Decimal('0')

    async def get_strategy_stats(self, user_id: int) -> List[Dict]:
        """Получение статистики по всем стратегиям пользователя."""
        try:
            query = "SELECT * FROM user_strategy_stats WHERE user_id = $1 ORDER BY total_pnl DESC"
            return await self._execute_query(query, (user_id,), fetch_all=True)
        except Exception as e:
            log_error(user_id, f"Ошибка получения статистики по стратегиям: {e}", module_name='database')
            return []

    async def update_user_totals(self, user_id: int, pnl: Decimal):
        """
        Обновляет общую статистику пользователя (total_profit, total_trades, win_rate).
        """
        async with self.get_connection() as conn:
            # Используем транзакцию для атомарности
            async with conn.transaction():
                try:
                    # Получаем текущие значения с блокировкой строки
                    user = await conn.fetchrow("SELECT * FROM users WHERE user_id = $1 FOR UPDATE", user_id)

                    if user:
                        current_total_profit = user['total_profit'] or Decimal('0')
                        new_total_profit = current_total_profit + pnl

                        current_winning_trades = user['winning_trades'] or 0
                        new_winning_trades = current_winning_trades + 1 if pnl > 0 else current_winning_trades

                        new_total_trades = (user['total_trades'] or 0) + 1

                        new_win_rate = (Decimal(new_winning_trades) / Decimal(
                            new_total_trades)) * 100 if new_total_trades > 0 else Decimal('0')

                        await conn.execute("""
                            UPDATE users 
                            SET total_profit = $1, total_trades = $2, winning_trades = $3, win_rate = $4, updated_at = NOW()
                            WHERE user_id = $5
                        """, new_total_profit, new_total_trades, new_winning_trades, new_win_rate, user_id)

                except Exception as e:
                    log_error(user_id, f"Ошибка обновления общей статистики пользователя: {e}", "db_manager")
                    # Транзакция будет автоматически отменена
                    raise

# Глобальный экземпляр менеджера базы данных
db_manager = _DatabaseManager()

# Функции для обратной совместимости
async def init_db_pool():
    """Инициализация пула соединений (обратная совместимость)"""
    await db_manager.initialize()


async def get_user_bybit_keys(user_id: int) -> Tuple[Optional[str], Optional[str]]:
    """Получение Bybit ключей (обратная совместимость)"""
    keys = await db_manager.get_api_keys(user_id, "bybit")
    if keys:
        return keys[0], keys[1]  # api_key, secret_key
    return None, None

def encrypt_data(data: str) -> str:
    """Шифрование данных (обратная совместимость)"""
    return db_manager.encrypt_data(data)

def decrypt_data(encrypted_data: str) -> str:
    """Расшифровка данных (обратная совместимость)"""
    return db_manager.decrypt_data(encrypted_data)

