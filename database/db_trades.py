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
    """
    Профиль пользователя (ТОЛЬКО профильные данные).

    Статистика (total_trades, win_rate, total_profit) вычисляется из таблицы trades
    и НЕ хранится в users для предотвращения дублирования данных.
    """
    user_id: int
    username: Optional[str] = None
    first_name: Optional[str] = None
    last_name: Optional[str] = None
    is_active: bool = True
    is_premium: bool = False
    registration_date: Optional[datetime] = None
    last_activity: Optional[datetime] = None
    # Статистика добавляется динамически через get_user()
    winning_trades: int = 0  # Вычисляется из trades
    total_trades: int = 0     # Вычисляется из trades
    total_profit: Decimal = Decimal('0')  # Вычисляется из trades
    max_drawdown: Decimal = Decimal('0')  # Вычисляется из trades
    win_rate: Decimal = Decimal('0')      # Вычисляется из trades

@dataclass
class UserApiKeys:
    """API ключи пользователя"""
    user_id: int
    exchange: str
    api_key: str
    secret_key: str
    account_priority: int = 1  # 1=PRIMARY, 2=SECONDARY, 3=TERTIARY
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
                # Устанавливаем timezone для сессии PostgreSQL
                await conn.execute("SET TIME ZONE 'Europe/Moscow';")
                log_info(0, "PostgreSQL timezone установлен на Europe/Moscow (МСК)", 'database')
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

                # Миграция 2: Добавление полей для отслеживания усреднений в таблицу trades
                fields_to_add = [
                    ("averaging_count", "INTEGER DEFAULT 0"),
                    ("is_averaging_trade", "BOOLEAN DEFAULT FALSE"),
                    ("total_position_size", "DECIMAL(20,8) DEFAULT 0"),
                    ("average_entry_price", "DECIMAL(20,8) DEFAULT 0")
                ]

                for field_name, field_definition in fields_to_add:
                    check_field_query = """
                    SELECT 1 FROM information_schema.columns
                    WHERE table_name='trades' AND column_name=$1
                    """
                    field_exists = await conn.fetchval(check_field_query, field_name)
                    if not field_exists:
                        log_warning(0, f"Колонка '{field_name}' отсутствует в таблице 'trades'. Добавляю...", 'database')
                        await conn.execute(f"ALTER TABLE trades ADD COLUMN {field_name} {field_definition};")
                        log_info(0, f"Колонка '{field_name}' успешно добавлена.", 'database')

                # Миграция 3: Добавление индекса для быстрого поиска по order_id
                index_exists_query = """
                SELECT 1 FROM pg_indexes
                WHERE tablename='trades' AND indexname='idx_trades_order_id'
                """
                index_exists = await conn.fetchval(index_exists_query)
                if not index_exists:
                    log_warning(0, "Индекс 'idx_trades_order_id' отсутствует. Добавляю...", 'database')
                    await conn.execute("CREATE INDEX idx_trades_order_id ON trades(order_id);")
                    log_info(0, "Индекс 'idx_trades_order_id' успешно добавлен.", 'database')

                # Миграция 4: Добавление новых полей в таблицу orders
                orders_fields_to_add = [
                    ("order_purpose", "VARCHAR(20)"),
                    ("leverage", "INTEGER DEFAULT 1"),
                    ("profit", "DECIMAL(20,8) DEFAULT 0"),
                    ("commission", "DECIMAL(20,8) DEFAULT 0"),
                    ("filled_at", "TIMESTAMPTZ"),
                    ("trade_id", "INTEGER"),
                    ("is_active", "BOOLEAN DEFAULT TRUE")
                ]

                for field_name, field_definition in orders_fields_to_add:
                    check_field_query = """
                    SELECT 1 FROM information_schema.columns
                    WHERE table_name='orders' AND column_name=$1
                    """
                    field_exists = await conn.fetchval(check_field_query, field_name)
                    if not field_exists:
                        log_warning(0, f"Колонка '{field_name}' отсутствует в таблице 'orders'. Добавляю...", 'database')
                        await conn.execute(f"ALTER TABLE orders ADD COLUMN {field_name} {field_definition};")
                        log_info(0, f"Колонка '{field_name}' успешно добавлена в orders.", 'database')

                # Добавляем индексы для новых полей
                orders_indexes = [
                    ("idx_orders_is_active", "CREATE INDEX IF NOT EXISTS idx_orders_is_active ON orders(is_active)"),
                    ("idx_orders_order_purpose", "CREATE INDEX IF NOT EXISTS idx_orders_order_purpose ON orders(order_purpose)"),
                    ("idx_orders_trade_id", "CREATE INDEX IF NOT EXISTS idx_orders_trade_id ON orders(trade_id)")
                ]

                for index_name, index_query in orders_indexes:
                    index_exists = await conn.fetchval(f"""
                        SELECT 1 FROM pg_indexes WHERE tablename='orders' AND indexname='{index_name}'
                    """)
                    if not index_exists:
                        log_warning(0, f"Индекс '{index_name}' отсутствует. Добавляю...", 'database')
                        await conn.execute(index_query)
                        log_info(0, f"Индекс '{index_name}' успешно добавлен.", 'database')

                # Миграция 5: Добавление поля bot_priority для Multi-Account режима
                bot_priority_exists = await conn.fetchval("""
                    SELECT 1 FROM information_schema.columns
                    WHERE table_name='orders' AND column_name='bot_priority'
                """)
                if not bot_priority_exists:
                    log_warning(0, "Колонка 'bot_priority' отсутствует в таблице 'orders'. Добавляю...", 'database')
                    await conn.execute("ALTER TABLE orders ADD COLUMN bot_priority INTEGER DEFAULT 1;")
                    log_info(0, "Колонка 'bot_priority' успешно добавлена в orders (для Multi-Account режима).", 'database')

                    # Добавляем комментарий для документации
                    await conn.execute("""
                        COMMENT ON COLUMN orders.bot_priority IS 'Приоритет бота в Multi-Account режиме: 1=PRIMARY, 2=SECONDARY, 3=TERTIARY'
                    """)

                    # Добавляем индекс для быстрого поиска по bot_priority
                    await conn.execute("CREATE INDEX IF NOT EXISTS idx_orders_bot_priority ON orders(bot_priority);")
                    log_info(0, "Индекс 'idx_orders_bot_priority' успешно добавлен.", 'database')

                # Миграция 6: Добавление составного UNIQUE индекса для безопасности многопользовательского режима
                unique_constraint_exists = await conn.fetchval("""
                    SELECT 1 FROM pg_constraint
                    WHERE conname = 'unique_user_order_id'
                """)
                if not unique_constraint_exists:
                    log_warning(0, "UNIQUE constraint (user_id, order_id) отсутствует. Добавляю...", 'database')

                    # КРИТИЧНО: Сначала проверяем, нет ли дубликатов в существующих данных
                    duplicates = await conn.fetchval("""
                        SELECT COUNT(*) FROM (
                            SELECT user_id, order_id
                            FROM orders
                            WHERE order_id IS NOT NULL AND order_id != ''
                            GROUP BY user_id, order_id
                            HAVING COUNT(*) > 1
                        ) AS dups
                    """)

                    if duplicates > 0:
                        log_warning(0, f"⚠️ Обнаружено {duplicates} дубликатов (user_id, order_id). Очистка перед добавлением UNIQUE constraint...", 'database')
                        # В production лучше вручную разобраться с дубликатами
                        # Но для безопасности оставляем только последние записи
                        await conn.execute("""
                            DELETE FROM orders
                            WHERE id NOT IN (
                                SELECT MAX(id)
                                FROM orders
                                WHERE order_id IS NOT NULL AND order_id != ''
                                GROUP BY user_id, order_id
                            )
                        """)
                        log_info(0, "Дубликаты удалены.", 'database')

                    # Добавляем UNIQUE constraint
                    await conn.execute("""
                        ALTER TABLE orders
                        ADD CONSTRAINT unique_user_order_id UNIQUE (user_id, order_id)
                    """)

                    log_info(0, "✅ UNIQUE constraint 'unique_user_order_id' успешно добавлен для многопользовательской безопасности.", 'database')

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
            
            # Таблица API ключей (MULTI-ACCOUNT SUPPORT)
            await self._execute_query("""
                CREATE TABLE IF NOT EXISTS user_api_keys (
                    id SERIAL PRIMARY KEY,
                    user_id BIGINT NOT NULL REFERENCES users(user_id) ON DELETE CASCADE,
                    exchange VARCHAR(50) NOT NULL,
                    api_key_encrypted TEXT NOT NULL,
                    secret_key_encrypted TEXT NOT NULL,
                    account_priority INTEGER NOT NULL DEFAULT 1,
                    passphrase_encrypted TEXT,
                    is_active BOOLEAN DEFAULT TRUE,
                    created_at TIMESTAMPTZ DEFAULT NOW(),
                    updated_at TIMESTAMPTZ DEFAULT NOW(),
                    UNIQUE(user_id, exchange, account_priority)
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
            
            # Таблица ордеров (ПОЛНАЯ СТРУКТУРА)
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

                    -- НОВЫЕ ПОЛЯ ДЛЯ ПОЛНОЙ ИНФОРМАЦИИ
                    order_purpose VARCHAR(20),              -- 'OPEN', 'CLOSE', 'AVERAGING', 'STOPLOSS'
                    leverage INTEGER DEFAULT 1,             -- Плечо
                    profit DECIMAL(20,8) DEFAULT 0,         -- PnL при закрытии (для CLOSE ордеров)
                    commission DECIMAL(20,8) DEFAULT 0,     -- Комиссия
                    filled_at TIMESTAMPTZ,                  -- Время исполнения (МСК)
                    trade_id INTEGER,                       -- Связь со сделкой
                    is_active BOOLEAN DEFAULT TRUE,         -- Активен ли ордер (для подсчета слотов)

                    metadata JSONB DEFAULT '{}'::jsonb,
                    created_at TIMESTAMPTZ DEFAULT NOW(),
                    updated_at TIMESTAMPTZ DEFAULT NOW()
                )
            """)

            # Таблица статистики по стратегиям
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
                "CREATE INDEX IF NOT EXISTS idx_orders_user_id ON orders(user_id)",
                "CREATE INDEX IF NOT EXISTS idx_orders_symbol ON orders(symbol)",
                "CREATE INDEX IF NOT EXISTS idx_orders_status ON orders(status)",
                "CREATE INDEX IF NOT EXISTS idx_orders_order_id ON orders(order_id)",
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
            moscow_tz = timezone(timedelta(hours=3))
            current_time = datetime.now(moscow_tz)

            query = """
                INSERT INTO users (user_id, username, first_name, last_name, is_active, is_premium, last_activity)
                VALUES ($1, $2, $3, $4, $5, $6, $7)
                ON CONFLICT (user_id) DO UPDATE SET
                    username = EXCLUDED.username,
                    first_name = EXCLUDED.first_name,
                    last_name = EXCLUDED.last_name,
                    last_activity = $7
            """

            await self._execute_query(query, (
                user_profile.user_id,
                user_profile.username,
                user_profile.first_name,
                user_profile.last_name,
                user_profile.is_active,
                user_profile.is_premium,
                current_time
            ))

            log_info(user_profile.user_id, f"Пользователь {user_profile.user_id} создан/обновлен", module_name='database')
            return True

        except Exception as e:
            log_error(user_profile.user_id, f"Ошибка создания пользователя: {e}", module_name='database')
            return False
    
    async def get_user(self, user_id: int) -> Optional[UserProfile]:
        """
        Получение пользователя с автоматическим вычислением статистики из trades.

        ЕДИНСТВЕННЫЙ источник истины для статистики - таблица trades.
        """
        try:
            # Получаем профильные данные
            query = "SELECT * FROM users WHERE user_id = $1"
            result = await self._execute_query(query, (user_id,), fetch_one=True)

            if not result:
                return None

            # Вычисляем статистику из таблицы trades (единственный источник истины)
            stats_query = """
                SELECT
                    COUNT(*) as total_trades,
                    COUNT(CASE WHEN profit > 0 THEN 1 END) as winning_trades,
                    COALESCE(SUM(profit), 0) as total_profit,
                    COALESCE(MIN(profit), 0) as max_drawdown
                FROM trades
                WHERE user_id = $1
                    AND exit_time IS NOT NULL
                    AND profit IS NOT NULL
            """
            stats = await self._execute_query(stats_query, (user_id,), fetch_one=True)

            # Вычисляем win_rate
            total_trades = stats['total_trades'] if stats else 0
            winning_trades = stats['winning_trades'] if stats else 0
            total_profit = to_decimal(stats['total_profit']) if stats else Decimal('0')
            max_drawdown = to_decimal(stats['max_drawdown']) if stats else Decimal('0')
            win_rate = (Decimal(winning_trades) / Decimal(total_trades) * 100) if total_trades > 0 else Decimal('0')

            return UserProfile(
                user_id=result['user_id'],
                username=result['username'],
                first_name=result['first_name'],
                last_name=result['last_name'],
                is_active=result['is_active'],
                is_premium=result['is_premium'],
                registration_date=result['registration_date'],
                last_activity=result['last_activity'],
                # Статистика вычислена из trades
                winning_trades=winning_trades,
                total_trades=total_trades,
                total_profit=total_profit,
                max_drawdown=max_drawdown,
                win_rate=win_rate
            )

        except Exception as e:
            log_error(user_id, f"Ошибка получения пользователя: {e}", module_name='database')
            return None
    
    async def save_api_keys(self, user_id: int, exchange: str, api_key: str, secret_key: str,
                           account_priority: int = 1, passphrase: str = None) -> bool:
        """
        Сохранение API ключей (MULTI-ACCOUNT SUPPORT)

        Args:
            user_id: ID пользователя
            exchange: Биржа (bybit)
            api_key: API ключ
            secret_key: Секретный ключ
            account_priority: Приоритет аккаунта (1=PRIMARY, 2=SECONDARY, 3=TERTIARY)
            passphrase: Passphrase (опционально)

        Returns:
            bool: True если успешно сохранено
        """
        try:
            moscow_tz = timezone(timedelta(hours=3))
            current_time = datetime.now(moscow_tz)

            encrypted_api_key = self.encrypt_data(api_key)
            encrypted_secret_key = self.encrypt_data(secret_key)
            encrypted_passphrase = self.encrypt_data(passphrase) if passphrase else None

            query = """
                INSERT INTO user_api_keys (user_id, exchange, api_key_encrypted, secret_key_encrypted, account_priority, passphrase_encrypted, updated_at)
                VALUES ($1, $2, $3, $4, $5, $6, $7)
                ON CONFLICT (user_id, exchange, account_priority) DO UPDATE SET
                    api_key_encrypted = EXCLUDED.api_key_encrypted,
                    secret_key_encrypted = EXCLUDED.secret_key_encrypted,
                    passphrase_encrypted = EXCLUDED.passphrase_encrypted,
                    is_active = TRUE,
                    updated_at = $7
            """

            await self._execute_query(query, (
                user_id, exchange, encrypted_api_key, encrypted_secret_key, account_priority, encrypted_passphrase, current_time
            ))

            log_info(user_id, f"API ключи для биржи {exchange} (приоритет {account_priority}) сохранены", module_name='database')
            return True

        except Exception as e:
            log_error(user_id, f"Ошибка сохранения API ключей: {e}", module_name='database')
            return False
    
    async def get_api_keys(self, user_id: int, exchange: str, account_priority: int = 1) -> Optional[Tuple[str, str, str]]:
        """
        Получение API ключей для конкретного аккаунта

        Args:
            user_id: ID пользователя
            exchange: Биржа (bybit)
            account_priority: Приоритет аккаунта (1=PRIMARY, 2=SECONDARY, 3=TERTIARY)

        Returns:
            Optional[Tuple]: (api_key, secret_key, passphrase) или None
        """
        try:
            query = """
                SELECT api_key_encrypted, secret_key_encrypted, passphrase_encrypted
                FROM user_api_keys
                WHERE user_id = $1 AND exchange = $2 AND account_priority = $3 AND is_active = TRUE
            """

            result = await self._execute_query(query, (user_id, exchange, account_priority), fetch_one=True)

            if result:
                api_key = self.decrypt_data(result['api_key_encrypted'])
                secret_key = self.decrypt_data(result['secret_key_encrypted'])
                passphrase = self.decrypt_data(result['passphrase_encrypted']) if result['passphrase_encrypted'] else None

                return api_key, secret_key, passphrase

            return None

        except Exception as e:
            log_error(user_id, f"Ошибка получения API ключей для приоритета {account_priority}: {e}", module_name='database')
            return None

    async def get_all_user_api_keys(self, user_id: int, exchange: str) -> List[Dict[str, Any]]:
        """
        Получение ВСЕХ API ключей пользователя (для multi-account системы)

        Args:
            user_id: ID пользователя
            exchange: Биржа (bybit)

        Returns:
            List[Dict]: Список словарей с полями priority, api_key, secret_key, passphrase
        """
        try:
            query = """
                SELECT account_priority, api_key_encrypted, secret_key_encrypted, passphrase_encrypted
                FROM user_api_keys
                WHERE user_id = $1 AND exchange = $2 AND is_active = TRUE
                ORDER BY account_priority ASC
            """

            results = await self._execute_query(query, (user_id, exchange), fetch_all=True)

            api_keys_list = []
            for result in results:
                api_key = self.decrypt_data(result['api_key_encrypted'])
                secret_key = self.decrypt_data(result['secret_key_encrypted'])
                passphrase = self.decrypt_data(result['passphrase_encrypted']) if result['passphrase_encrypted'] else None

                api_keys_list.append({
                    'priority': result['account_priority'],
                    'api_key': api_key,
                    'secret_key': secret_key,
                    'passphrase': passphrase
                })

            log_info(user_id, f"Загружено {len(api_keys_list)} API ключей для {exchange}", module_name='database')
            return api_keys_list

        except Exception as e:
            log_error(user_id, f"Ошибка получения всех API ключей: {e}", module_name='database')
            return []

    async def save_trade(self, trade: TradeRecord) -> Optional[int]:
        """Сохранение сделки с московским временем"""
        try:
            # Преобразуем время в московское (UTC+3)
            moscow_tz = timezone(timedelta(hours=3))

            # Если время не указано, используем текущее московское время
            entry_time_msk = trade.entry_time
            if entry_time_msk and entry_time_msk.tzinfo is None:
                # Если время naive, считаем его UTC и конвертируем в московское
                entry_time_msk = entry_time_msk.replace(tzinfo=timezone.utc).astimezone(moscow_tz)
            elif entry_time_msk is None:
                entry_time_msk = datetime.now(moscow_tz)

            exit_time_msk = trade.exit_time
            if exit_time_msk and exit_time_msk.tzinfo is None:
                exit_time_msk = exit_time_msk.replace(tzinfo=timezone.utc).astimezone(moscow_tz)

            query = """
                INSERT INTO trades (
                    user_id, symbol, side, entry_price, exit_price, quantity, leverage,
                    profit, commission, status, strategy_type, order_id, position_idx,
                    entry_time, exit_time, metadata, created_at, updated_at
                ) VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10, $11, $12, $13, $14, $15, $16, $17, $17)
                RETURNING id
            """

            current_moscow_time = datetime.now(moscow_tz)

            result = await self._execute_query(query, (
                trade.user_id, trade.symbol, trade.side, trade.entry_price, trade.exit_price,
                trade.quantity, trade.leverage, trade.profit, trade.commission, trade.status,
                trade.strategy_type, trade.order_id, trade.position_idx, entry_time_msk,
                exit_time_msk, json.dumps(trade.metadata or {}, cls=DecimalEncoder), current_moscow_time
            ), fetch_one=True)

            trade_id = result['id'] if result else None
            log_info(trade.user_id, f"Сделка сохранена с ID: {trade_id}, время: {current_moscow_time.strftime('%Y-%m-%d %H:%M:%S')} МСК", module_name='database')
            return trade_id

        except Exception as e:
            log_error(trade.user_id, f"Ошибка сохранения сделки: {e}", module_name='database')
            return None

    async def update_trade_on_close(self, trade_id: int, exit_price: Decimal, pnl: Decimal, commission: Decimal,
                                    exit_time: datetime) -> bool:
        """Обновление записи о сделке при ее закрытии с московским временем."""
        try:
            # Преобразуем время в московское (UTC+3)
            moscow_tz = timezone(timedelta(hours=3))

            # Конвертируем время выхода в московское
            exit_time_msk = exit_time
            if exit_time_msk and exit_time_msk.tzinfo is None:
                exit_time_msk = exit_time_msk.replace(tzinfo=timezone.utc).astimezone(moscow_tz)
            elif exit_time_msk is None:
                exit_time_msk = datetime.now(moscow_tz)

            current_moscow_time = datetime.now(moscow_tz)

            query = """
                UPDATE trades
                SET
                    exit_price = $1,
                    profit = $2,
                    commission = $3,
                    exit_time = $4,
                    status = 'CLOSED',
                    updated_at = $6
                WHERE id = $5
                RETURNING user_id
            """
            result = await self._execute_query(query, (exit_price, pnl, commission, exit_time_msk, trade_id, current_moscow_time), fetch_one=True)

            if result:
                user_id = result['user_id']
                log_info(0, f"Сделка с ID {trade_id} закрыта в БД. Выход: {exit_time_msk.strftime('%Y-%m-%d %H:%M:%S')} МСК, PnL: {pnl:.2f}$", module_name='database')

                # Статистика вычисляется динамически в get_user() из таблицы trades
                return True

            return False

        except Exception as e:
            log_error(0, f"Ошибка обновления сделки {trade_id} в БД: {e}", module_name='database')
            return False

    async def update_trade_on_averaging(self, trade_id: int, new_entry_price: Decimal, new_quantity: Decimal) -> bool:
        """Обновление записи о сделке при усреднении (изменение entry_price и quantity)."""
        try:
            moscow_tz = timezone(timedelta(hours=3))
            current_moscow_time = datetime.now(moscow_tz)

            query = """
                UPDATE trades
                SET
                    entry_price = $1,
                    quantity = $2,
                    updated_at = $4
                WHERE id = $3
            """
            await self._execute_query(query, (new_entry_price, new_quantity, trade_id, current_moscow_time))
            log_info(0, f"Сделка с ID {trade_id} обновлена при усреднении. Новая цена входа: {new_entry_price:.4f}, количество: {new_quantity}", module_name='database')
            return True
        except Exception as e:
            log_error(0, f"Ошибка обновления сделки {trade_id} при усреднении: {e}", module_name='database')
            return False

    async def get_active_trade(self, user_id: int, symbol: str) -> Optional[Dict]:
        """Получение активной сделки пользователя по символу."""
        try:
            query = """
                SELECT id, entry_price, quantity, side, leverage
                FROM trades
                WHERE user_id = $1 AND symbol = $2 AND status = 'ACTIVE'
                ORDER BY entry_time DESC
                LIMIT 1
            """
            result = await self._execute_query(query, (user_id, symbol), fetch_one=True)
            return dict(result) if result else None
        except Exception as e:
            log_error(user_id, f"Ошибка получения активной сделки для {symbol}: {e}", module_name='database')
            return None

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
            moscow_tz = timezone(timedelta(hours=3))
            current_time = datetime.now(moscow_tz)
            win_increment = 1 if pnl > 0 else 0

            query = """
                INSERT INTO user_strategy_stats (user_id, strategy_type, total_trades, winning_trades, total_pnl, updated_at)
                VALUES ($1, $2, 1, $3, $4, $5)
                ON CONFLICT (user_id, strategy_type) DO UPDATE SET
                    total_trades = user_strategy_stats.total_trades + 1,
                    winning_trades = user_strategy_stats.winning_trades + $3,
                    total_pnl = user_strategy_stats.total_pnl + $4,
                    updated_at = $5
                RETURNING total_trades, winning_trades;
            """

            result = await self._execute_query(query, (user_id, strategy_type, win_increment, pnl, current_time), fetch_one=True)

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

    async def analyze_database_usage(self) -> Dict[str, Any]:
        """
        Анализирует использование таблиц базы данных.
        Возвращает информацию о количестве записей в каждой таблице.
        """
        try:
            tables_info = {}

            # Список основных таблиц для анализа
            tables_to_check = [
                'users', 'user_api_keys', 'trades', 'positions',
                'orders', 'user_strategies', 'user_strategy_stats', 'notifications'
            ]

            async with self.get_connection() as conn:
                for table_name in tables_to_check:
                    try:
                        # Получаем количество записей
                        count_query = f"SELECT COUNT(*) as count FROM {table_name}"
                        result = await conn.fetchrow(count_query)
                        record_count = result['count'] if result else 0

                        # Получаем размер таблицы
                        size_query = f"SELECT pg_size_pretty(pg_total_relation_size('{table_name}')) as size"
                        size_result = await conn.fetchrow(size_query)
                        table_size = size_result['size'] if size_result else 'Unknown'

                        # Получаем дату последнего изменения (если есть поля created_at/updated_at)
                        last_activity = None
                        try:
                            if table_name in ['trades', 'positions', 'orders', 'user_strategies', 'notifications']:
                                activity_query = f"SELECT MAX(created_at) as last_activity FROM {table_name}"
                                activity_result = await conn.fetchrow(activity_query)
                                last_activity = activity_result['last_activity'] if activity_result else None
                        except:
                            pass  # Ignore if column doesn't exist

                        tables_info[table_name] = {
                            'records': record_count,
                            'size': table_size,
                            'last_activity': last_activity,
                            'status': 'active' if record_count > 0 else 'empty'
                        }

                    except Exception as e:
                        tables_info[table_name] = {
                            'records': 0,
                            'size': 'Error',
                            'last_activity': None,
                            'status': f'error: {str(e)}'
                        }

            # Добавляем общую статистику
            total_records = sum(info['records'] for info in tables_info.values() if isinstance(info['records'], int))
            empty_tables = [name for name, info in tables_info.items() if info['status'] == 'empty']

            summary = {
                'total_tables': len(tables_to_check),
                'total_records': total_records,
                'empty_tables': empty_tables,
                'empty_count': len(empty_tables),
                'analysis_time': datetime.now(timezone(timedelta(hours=3))).strftime('%Y-%m-%d %H:%M:%S MSK')
            }

            return {
                'summary': summary,
                'tables': tables_info
            }

        except Exception as e:
            log_error(0, f"Ошибка анализа использования БД: {e}", module_name='database')
            return {
                'summary': {'error': str(e)},
                'tables': {}
            }

    async def get_user_stats_by_period(self, user_id: int, start_date: Optional[datetime] = None,
                                     end_date: Optional[datetime] = None) -> Dict[str, Any]:
        """Получение статистики пользователя за указанный период"""
        try:
            # Базовые условия для WHERE
            where_conditions = ["user_id = $1"]
            params = [user_id]
            param_index = 2

            # Добавляем фильтры по датам, если указаны
            if start_date:
                where_conditions.append(f"exit_time >= ${param_index}")
                params.append(start_date)
                param_index += 1

            if end_date:
                where_conditions.append(f"exit_time <= ${param_index}")
                params.append(end_date)
                param_index += 1

            # Добавляем фильтр только на завершённые сделки
            where_conditions.append("exit_time IS NOT NULL")
            where_conditions.append("profit IS NOT NULL")

            where_clause = " AND ".join(where_conditions)

            # Запрос общей статистики за период
            query = f"""
                SELECT
                    COUNT(*) as total_trades,
                    COUNT(CASE WHEN profit > 0 THEN 1 END) as winning_trades,
                    COALESCE(SUM(profit), 0) as total_profit,
                    COALESCE(SUM(commission), 0) as total_commission,
                    COALESCE(AVG(profit), 0) as avg_profit,
                    COALESCE(MIN(profit), 0) as min_profit,
                    COALESCE(MAX(profit), 0) as max_profit
                FROM trades
                WHERE {where_clause}
            """

            stats = await self._execute_query(query, params, fetch_one=True)

            if not stats or stats['total_trades'] == 0:
                return {
                    'total_trades': 0,
                    'winning_trades': 0,
                    'total_profit': Decimal('0'),
                    'total_commission': Decimal('0'),
                    'win_rate': Decimal('0'),
                    'avg_profit': Decimal('0'),
                    'min_profit': Decimal('0'),
                    'max_profit': Decimal('0'),
                    'net_profit': Decimal('0'),
                    'profit_percentage': Decimal('0')
                }

            # Вычисляем производные показатели
            total_trades = stats['total_trades']
            winning_trades = stats['winning_trades']
            total_profit = Decimal(str(stats['total_profit'])) if stats['total_profit'] else Decimal('0')
            total_commission = Decimal(str(stats['total_commission'])) if stats['total_commission'] else Decimal('0')
            # ВАЖНО: total_profit УЖЕ содержит чистую прибыль (комиссии вычтены при закрытии сделки)
            # Поэтому НЕ вычитаем комиссии повторно!
            net_profit = total_profit

            win_rate = (Decimal(winning_trades) / Decimal(total_trades) * 100) if total_trades > 0 else Decimal('0')

            # В demo режиме используем сумму маржи всех сделок как приблизительный депозит
            # В будущем здесь будет использоваться реальный депозит пользователя
            # TODO: В продакшене заменить на user_profile.initial_deposit

            # Запрос для оценки размера депозита на основе использованной маржи
            margin_query = f"""
                SELECT COALESCE(SUM(ABS(quantity * entry_price / leverage)), 1000) as estimated_deposit
                FROM trades
                WHERE {where_clause}
            """
            margin_result = await self._execute_query(margin_query, params, fetch_one=True)
            estimated_deposit = Decimal(str(margin_result['estimated_deposit'])) if margin_result else Decimal('1000')

            # Рассчитываем процент дохода к депозиту
            profit_percentage = (net_profit / estimated_deposit * 100) if estimated_deposit > 0 else Decimal('0')

            return {
                'total_trades': total_trades,
                'winning_trades': winning_trades,
                'total_profit': total_profit,
                'total_commission': total_commission,
                'net_profit': net_profit,
                'win_rate': win_rate,
                'avg_profit': Decimal(str(stats['avg_profit'])) if stats['avg_profit'] else Decimal('0'),
                'min_profit': Decimal(str(stats['min_profit'])) if stats['min_profit'] else Decimal('0'),
                'max_profit': Decimal(str(stats['max_profit'])) if stats['max_profit'] else Decimal('0'),
                'profit_percentage': profit_percentage,
                'estimated_deposit': estimated_deposit
            }

        except Exception as e:
            log_error(user_id, f"Ошибка получения статистики пользователя за период: {e}", module_name='database')
            return {}

    async def get_strategy_stats_by_period(self, user_id: int, start_date: Optional[datetime] = None,
                                         end_date: Optional[datetime] = None) -> List[Dict]:
        """Получение статистики по стратегиям за указанный период"""
        try:
            # Базовые условия для WHERE
            where_conditions = ["user_id = $1"]
            params = [user_id]
            param_index = 2

            # Добавляем фильтры по датам, если указаны
            if start_date:
                where_conditions.append(f"exit_time >= ${param_index}")
                params.append(start_date)
                param_index += 1

            if end_date:
                where_conditions.append(f"exit_time <= ${param_index}")
                params.append(end_date)
                param_index += 1

            # Добавляем фильтр только на завершённые сделки
            where_conditions.append("exit_time IS NOT NULL")
            where_conditions.append("profit IS NOT NULL")
            where_conditions.append("strategy_type IS NOT NULL")

            where_clause = " AND ".join(where_conditions)

            # Запрос статистики по стратегиям за период
            query = f"""
                SELECT
                    strategy_type,
                    COUNT(*) as total_trades,
                    COUNT(CASE WHEN profit > 0 THEN 1 END) as winning_trades,
                    COALESCE(SUM(profit), 0) as total_pnl,
                    COALESCE(SUM(commission), 0) as total_commission,
                    COALESCE(AVG(profit), 0) as avg_profit,
                    COALESCE(MIN(profit), 0) as min_profit,
                    COALESCE(MAX(profit), 0) as max_profit
                FROM trades
                WHERE {where_clause}
                GROUP BY strategy_type
                ORDER BY total_pnl DESC
            """

            results = await self._execute_query(query, params, fetch_all=True)

            if not results:
                return []

            # Получаем общий депозит для расчёта процента по стратегиям
            user_stats = await self.get_user_stats_by_period(user_id, start_date, end_date)
            total_deposit = user_stats.get('estimated_deposit', Decimal('1000'))

            strategy_stats = []
            for row in results:
                total_trades = row['total_trades']
                winning_trades = row['winning_trades']
                total_pnl = Decimal(str(row['total_pnl'])) if row['total_pnl'] else Decimal('0')
                total_commission = Decimal(str(row['total_commission'])) if row['total_commission'] else Decimal('0')
                # ВАЖНО: total_pnl УЖЕ содержит чистую прибыль (комиссии вычтены при закрытии сделки)
                net_pnl = total_pnl

                win_rate = (Decimal(winning_trades) / Decimal(total_trades) * 100) if total_trades > 0 else Decimal('0')

                # Процент дохода стратегии к общему депозиту
                profit_percentage = (net_pnl / total_deposit * 100) if total_deposit > 0 else Decimal('0')

                strategy_stats.append({
                    'strategy_type': row['strategy_type'],
                    'total_trades': total_trades,
                    'winning_trades': winning_trades,
                    'total_pnl': total_pnl,
                    'total_commission': total_commission,
                    'net_pnl': net_pnl,
                    'win_rate': win_rate,
                    'avg_profit': Decimal(str(row['avg_profit'])) if row['avg_profit'] else Decimal('0'),
                    'min_profit': Decimal(str(row['min_profit'])) if row['min_profit'] else Decimal('0'),
                    'max_profit': Decimal(str(row['max_profit'])) if row['max_profit'] else Decimal('0'),
                    'profit_percentage': profit_percentage
                })

            return strategy_stats

        except Exception as e:
            log_error(user_id, f"Ошибка получения статистики по стратегиям за период: {e}", module_name='database')
            return []

    async def get_available_months(self, user_id: int) -> List[Dict[str, str]]:
        """Получение списка месяцев, в которые пользователь торговал"""
        try:
            query = """
                SELECT DISTINCT
                    DATE_TRUNC('month', exit_time) as month_start,
                    TO_CHAR(exit_time, 'YYYY-MM') as month_key,
                    TO_CHAR(exit_time, 'FMMonth YYYY') as month_display
                FROM trades
                WHERE user_id = $1
                    AND exit_time IS NOT NULL
                    AND profit IS NOT NULL
                ORDER BY month_start DESC
            """

            results = await self._execute_query(query, (user_id,), fetch_all=True)

            months = []
            for row in results:
                months.append({
                    'key': row['month_key'],      # '2024-09'
                    'display': row['month_display'],  # 'September 2024'
                    'start_date': row['month_start']
                })

            return months

        except Exception as e:
            log_error(user_id, f"Ошибка получения доступных месяцев: {e}", module_name='database')
            return []

    # ===============================================================================
    # МЕТОДЫ ДЛЯ РАБОТЫ С ОРДЕРАМИ (для системы восстановления)
    # ===============================================================================

    async def save_order(self, user_id: int, symbol: str, side: str, order_type: str,
                        quantity: Decimal, price: Decimal, order_id: str,
                        strategy_type: str = None, client_order_id: str = None,
                        metadata: Dict[str, Any] = None) -> Optional[int]:
        """
        Сохраняет ордер в базу данных

        Args:
            user_id: ID пользователя
            symbol: Символ
            side: Сторона (BUY/SELL)
            order_type: Тип ордера (LIMIT/MARKET/STOP)
            quantity: Количество
            price: Цена
            order_id: ID ордера на бирже
            strategy_type: Тип стратегии
            client_order_id: Клиентский ID ордера
            metadata: Дополнительные данные

        Returns:
            Optional[int]: ID записи в БД или None при ошибке
        """
        try:
            query = """
            INSERT INTO orders (user_id, symbol, side, order_type, quantity, price,
                              order_id, client_order_id, strategy_type, metadata)
            VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10)
            RETURNING id
            """

            metadata_json = json.dumps(metadata or {}, cls=DecimalEncoder)

            result = await self._execute_query(
                query,
                (user_id, symbol, side, order_type, quantity, price,
                 order_id, client_order_id, strategy_type, metadata_json),
                fetch_one=True
            )

            if result:
                log_debug(user_id, f"Ордер {order_id} сохранён в БД с ID {result['id']}", module_name='database')
                return result['id']
            return None

        except Exception as e:
            log_error(user_id, f"Ошибка сохранения ордера {order_id}: {e}", module_name='database')
            return None

    async def get_active_orders_by_user(self, user_id: int, symbol: str = None,
                                      strategy_type: str = None) -> List[Dict[str, Any]]:
        """
        Получает активные ордера пользователя из БД

        Args:
            user_id: ID пользователя
            symbol: Символ (опционально)
            strategy_type: Тип стратегии (опционально)

        Returns:
            List[Dict]: Список активных ордеров
        """
        try:
            conditions = ["user_id = $1", "status IN ('PENDING', 'PARTIALLY_FILLED', 'NEW')"]
            params = [user_id]
            param_count = 1

            if symbol:
                param_count += 1
                conditions.append(f"symbol = ${param_count}")
                params.append(symbol)

            if strategy_type:
                param_count += 1
                conditions.append(f"strategy_type = ${param_count}")
                params.append(strategy_type)

            query = f"""
            SELECT id, user_id, symbol, side, order_type, quantity, price,
                   filled_quantity, average_price, status, order_id,
                   client_order_id, strategy_type, metadata, created_at, updated_at
            FROM orders
            WHERE {' AND '.join(conditions)}
            ORDER BY created_at DESC
            """

            rows = await self._execute_query(query, tuple(params), fetch_all=True)

            orders = []
            for row in rows:
                order_dict = dict(row)
                if order_dict.get('metadata'):
                    order_dict['metadata'] = json.loads(order_dict['metadata'])
                orders.append(order_dict)

            log_debug(user_id, f"Найдено {len(orders)} активных ордеров", module_name='database')
            return orders

        except Exception as e:
            log_error(user_id, f"Ошибка получения активных ордеров: {e}", module_name='database')
            return []

    async def get_active_orders_by_bot_priority(self, user_id: int, symbol: str,
                                                bot_priority: int,
                                                strategy_type: str = None) -> List[Dict[str, Any]]:
        """
        Получает активные ордера конкретного бота в Multi-Account режиме.

        КРИТИЧНО для восстановления: каждый бот (1/2/3) должен восстанавливать только СВОИ ордера!

        Args:
            user_id: ID пользователя
            symbol: Символ
            bot_priority: Приоритет бота (1=PRIMARY, 2=SECONDARY, 3=TERTIARY)
            strategy_type: Тип стратегии (опционально)

        Returns:
            List[Dict]: Список активных ордеров конкретного бота
        """
        try:
            conditions = [
                "user_id = $1",
                "symbol = $2",
                "bot_priority = $3",
                "status IN ('PENDING', 'PARTIALLY_FILLED', 'NEW')"
            ]
            params = [user_id, symbol, bot_priority]
            param_count = 3

            if strategy_type:
                param_count += 1
                conditions.append(f"strategy_type = ${param_count}")
                params.append(strategy_type)

            query = f"""
            SELECT id, user_id, symbol, side, order_type, quantity, price,
                   filled_quantity, average_price, status, order_id,
                   client_order_id, strategy_type, bot_priority, metadata, created_at, updated_at
            FROM orders
            WHERE {' AND '.join(conditions)}
            ORDER BY created_at DESC
            """

            rows = await self._execute_query(query, tuple(params), fetch_all=True)

            orders = []
            for row in rows:
                order_dict = dict(row)
                if order_dict.get('metadata'):
                    order_dict['metadata'] = json.loads(order_dict['metadata'])
                orders.append(order_dict)

            log_debug(user_id, f"Найдено {len(orders)} активных ордеров для Bot{bot_priority} ({symbol})", module_name='database')
            return orders

        except Exception as e:
            log_error(user_id, f"Ошибка получения ордеров для Bot{bot_priority}: {e}", module_name='database')
            return []

    async def update_order_status(self, order_id: str, status: str,
                                filled_quantity: Decimal = None,
                                average_price: Decimal = None,
                                metadata: Dict[str, Any] = None) -> bool:
        """
        Обновляет статус ордера в БД (УСТАРЕВШИЙ МЕТОД - используйте update_order_on_fill)

        Args:
            order_id: ID ордера на бирже
            status: Новый статус
            filled_quantity: Исполненное количество
            average_price: Средняя цена исполнения
            metadata: Дополнительные данные

        Returns:
            bool: True если обновление успешно
        """
        try:
            moscow_tz = timezone(timedelta(hours=3))
            current_time = datetime.now(moscow_tz)

            set_clauses = ["status = $2"]
            params = [order_id, status]
            param_count = 2

            if filled_quantity is not None:
                param_count += 1
                set_clauses.append(f"filled_quantity = ${param_count}")
                params.append(filled_quantity)

            if average_price is not None:
                param_count += 1
                set_clauses.append(f"average_price = ${param_count}")
                params.append(average_price)

            if metadata is not None:
                param_count += 1
                set_clauses.append(f"metadata = ${param_count}")
                params.append(json.dumps(metadata, cls=DecimalEncoder))

            # Добавляем updated_at в конец
            param_count += 1
            set_clauses.append(f"updated_at = ${param_count}")
            params.append(current_time)

            query = f"""
            UPDATE orders
            SET {', '.join(set_clauses)}
            WHERE order_id = $1
            RETURNING user_id
            """

            result = await self._execute_query(query, tuple(params), fetch_one=True)

            if result:
                log_debug(result['user_id'], f"Статус ордера {order_id} обновлён на {status}", module_name='database')
                return True
            return False

        except Exception as e:
            log_error(0, f"Ошибка обновления статуса ордера {order_id}: {e}", module_name='database')
            return False

    async def trade_exists(self, trade_id: int) -> bool:
        """
        Проверяет существование сделки в БД

        Args:
            trade_id: ID сделки

        Returns:
            bool: True если сделка существует
        """
        try:
            query = "SELECT 1 FROM trades WHERE id = $1"
            result = await self._execute_query(query, (trade_id,), fetch_one=True)
            return result is not None

        except Exception as e:
            log_error(0, f"Ошибка проверки существования сделки {trade_id}: {e}", module_name='database')
            return False

    async def get_order_by_exchange_id(self, order_id: str) -> Optional[Dict[str, Any]]:
        """
        Получает ордер по ID с биржи (с fallback для только что созданных ордеров)

        Args:
            order_id: ID ордера на бирже

        Returns:
            Optional[Dict]: Данные ордера или None

        КРИТИЧНО: Поддерживает поиск ордеров которые только что были созданы с order_id="PENDING"
        и еще не обновлены настоящим ID с биржи (race condition fix)
        """
        try:
            # Основной поиск по order_id
            query = """
            SELECT id, user_id, symbol, side, order_type, quantity, price,
                   filled_quantity, average_price, status, order_id,
                   client_order_id, strategy_type, bot_priority, metadata, created_at, updated_at
            FROM orders
            WHERE order_id = $1
            """

            result = await self._execute_query(query, (order_id,), fetch_one=True)

            if result:
                order_dict = dict(result)
                if order_dict.get('metadata'):
                    order_dict['metadata'] = json.loads(order_dict['metadata'])
                return order_dict

            # FALLBACK: Если не нашли по order_id, ищем недавно созданные PENDING ордера
            # (для случая когда WebSocket событие приходит быстрее чем обновляется order_id)
            query_pending = """
            SELECT id, user_id, symbol, side, order_type, quantity, price,
                   filled_quantity, average_price, status, order_id,
                   client_order_id, strategy_type, bot_priority, metadata, created_at, updated_at
            FROM orders
            WHERE order_id = 'PENDING'
              AND created_at > NOW() - INTERVAL '10 seconds'
            ORDER BY created_at DESC
            LIMIT 1
            """

            result_pending = await self._execute_query(query_pending, fetch_one=True)

            if result_pending:
                order_dict = dict(result_pending)
                if order_dict.get('metadata'):
                    order_dict['metadata'] = json.loads(order_dict['metadata'])

                # КРИТИЧНО: Сразу обновляем order_id в БД
                update_query = "UPDATE orders SET order_id = $1, updated_at = NOW() WHERE id = $2"
                await self._execute_query(update_query, (order_id, order_dict['id']))

                log_info(order_dict.get('user_id', 0),
                        f"✅ [FALLBACK] Ордер найден как PENDING и обновлен: {order_id}",
                        module_name='database')

                return order_dict

            return None

        except Exception as e:
            log_error(0, f"Ошибка получения ордера {order_id}: {e}", module_name='database')
            return None

    # Алиас для обратной совместимости
    async def get_order_by_id(self, order_id: str) -> Optional[Dict[str, Any]]:
        """Алиас для get_order_by_exchange_id"""
        return await self.get_order_by_exchange_id(order_id)

    async def get_open_order_for_position(self, user_id: int, symbol: str, bot_priority: int = 1) -> Optional[Dict[str, Any]]:
        """
        Получает последний FILLED ордер открытия позиции (order_purpose='OPEN') для расчёта entry_time.

        ИСПОЛЬЗУЕТСЯ для получения filled_at из БД вместо self.entry_time в памяти.
        Это обеспечивает НАДЁЖНОСТЬ - даже если бот перезапустится, время входа берётся из БД!

        Args:
            user_id: ID пользователя
            symbol: Символ торговли
            bot_priority: Приоритет бота (1=PRIMARY, 2=SECONDARY, 3=TERTIARY)

        Returns:
            Optional[Dict]: Данные ордера с filled_at и average_price, или None если не найден
        """
        try:
            query = """
            SELECT id, user_id, symbol, side, order_type, quantity, price,
                   filled_quantity, average_price, status, order_id,
                   client_order_id, strategy_type, bot_priority, order_purpose,
                   filled_at, created_at, updated_at, metadata
            FROM orders
            WHERE user_id = $1
              AND symbol = $2
              AND bot_priority = $3
              AND order_purpose = 'OPEN'
              AND status = 'FILLED'
            ORDER BY filled_at DESC
            LIMIT 1
            """

            result = await self._execute_query(query, (user_id, symbol, bot_priority), fetch_one=True)

            if result:
                order_dict = dict(result)
                if order_dict.get('metadata'):
                    order_dict['metadata'] = json.loads(order_dict['metadata'])
                return order_dict
            return None

        except Exception as e:
            log_error(user_id, f"Ошибка получения OPEN ордера для {symbol} Bot_{bot_priority}: {e}", module_name='database')
            return None

    async def has_pending_close_order(self, user_id: int, symbol: str, bot_priority: int = 1) -> bool:
        """
        ПРАВИЛЬНЫЙ МЕТОД для WebSocket: Проверяет наличие ЛЮБОГО CLOSE ордера (активного или недавно исполненного).

        Используется чтобы отличить:
        - Наше закрытие: бот создал CLOSE ордер → WebSocket видит size=0 → всё ок
        - Ручное закрытие: пользователь закрыл на бирже → WebSocket видит size=0 → тревога!

        Args:
            user_id: ID пользователя
            symbol: Символ торговли
            bot_priority: Приоритет бота (1=PRIMARY, 2=SECONDARY, 3=TERTIARY)

        Returns:
            bool: True если есть наш CLOSE ордер (PENDING или недавно FILLED < 30 сек)
        """
        try:
            query = """
            SELECT 1
            FROM orders
            WHERE user_id = $1
              AND symbol = $2
              AND bot_priority = $3
              AND order_purpose = 'CLOSE'
              AND (
                  status IN ('PENDING', 'NEW', 'PARTIALLY_FILLED')
                  OR (status = 'FILLED' AND filled_at >= NOW() - INTERVAL '30 seconds')
              )
            LIMIT 1
            """

            result = await self._execute_query(query, (user_id, symbol, bot_priority), fetch_one=True)
            return result is not None

        except Exception as e:
            log_error(user_id, f"Ошибка проверки CLOSE ордера для {symbol} Bot_{bot_priority}: {e}", module_name='database')
            return False

    async def has_unclosed_position(self, user_id: int, symbol: str, bot_priority: int = 1) -> bool:
        """
        Проверяет наличие незакрытой позиции: есть OPEN FILLED, но нет CLOSE FILLED.

        Используется для детекции ручного закрытия пользователем.

        Args:
            user_id: ID пользователя
            symbol: Символ торговли
            bot_priority: Приоритет бота (1=PRIMARY, 2=SECONDARY, 3=TERTIARY)

        Returns:
            bool: True если позиция открыта но не закрыта
        """
        try:
            query = """
            SELECT 1
            FROM orders
            WHERE user_id = $1
              AND symbol = $2
              AND bot_priority = $3
              AND order_purpose = 'OPEN'
              AND status = 'FILLED'
              AND NOT EXISTS (
                  SELECT 1 FROM orders AS close_orders
                  WHERE close_orders.user_id = orders.user_id
                    AND close_orders.symbol = orders.symbol
                    AND close_orders.bot_priority = orders.bot_priority
                    AND close_orders.order_purpose = 'CLOSE'
                    AND close_orders.status = 'FILLED'
                    AND close_orders.created_at > orders.filled_at
              )
            LIMIT 1
            """

            result = await self._execute_query(query, (user_id, symbol, bot_priority), fetch_one=True)
            return result is not None

        except Exception as e:
            log_error(user_id, f"Ошибка проверки незакрытой позиции для {symbol} Bot_{bot_priority}: {e}", module_name='database')
            return False

    async def get_all_open_positions(self, user_id: int) -> List[Dict[str, Any]]:
        """
        Получает ВСЕ открытые позиции пользователя из БД (для команды /trade_details).

        КРИТИЧНО: Открытая позиция = есть OPEN ордер, но НЕТ соответствующего CLOSE ордера.
        Работает НЕЗАВИСИМО от self.position_active в памяти - только БД как источник истины!

        Показывает позиции для ВСЕХ стратегий (SignalScalper, FlashDropCatcher) и ВСЕХ ботов (Bot_1, Bot_2, Bot_3).

        Args:
            user_id: ID пользователя

        Returns:
            List[Dict]: Список открытых позиций с полной информацией

        Структура каждой позиции:
        {
            "symbol": "BTCUSDT",
            "strategy_type": "signal_scalper",
            "bot_priority": 1,
            "open_order": {...},  # Ордер открытия позиции
            "averaging_orders": [...],  # Список ордеров усреднения (если есть)
        }
        """
        try:
            # Шаг 1: Находим все уникальные комбинации (symbol, strategy_type, bot_priority),
            # для которых есть OPEN ордер, но НЕТ CLOSE ордера
            query_positions = """
            WITH open_positions AS (
                SELECT DISTINCT
                    symbol,
                    strategy_type,
                    bot_priority
                FROM orders
                WHERE user_id = $1
                  AND order_purpose = 'OPEN'
                  AND status = 'FILLED'
            ),
            closed_positions AS (
                SELECT DISTINCT
                    symbol,
                    strategy_type,
                    bot_priority
                FROM orders
                WHERE user_id = $1
                  AND order_purpose = 'CLOSE'
                  AND status = 'FILLED'
            )
            SELECT
                op.symbol,
                op.strategy_type,
                op.bot_priority
            FROM open_positions op
            LEFT JOIN closed_positions cp
                ON op.symbol = cp.symbol
                AND op.strategy_type = cp.strategy_type
                AND op.bot_priority = cp.bot_priority
            WHERE cp.symbol IS NULL  -- Нет соответствующего CLOSE ордера
            ORDER BY op.symbol, op.bot_priority
            """

            position_keys = await self._execute_query(query_positions, (user_id,), fetch_all=True)

            if not position_keys:
                return []

            # Шаг 2: Для каждой открытой позиции получаем детальную информацию
            open_positions = []

            for pos_key in position_keys:
                symbol = pos_key['symbol']
                strategy_type = pos_key['strategy_type']
                bot_priority = pos_key['bot_priority']

                # Получаем OPEN ордер (последний FILLED)
                query_open_order = """
                SELECT id, user_id, symbol, side, order_type, quantity, price,
                       filled_quantity, average_price, status, order_id,
                       client_order_id, strategy_type, bot_priority, order_purpose,
                       filled_at, created_at, updated_at, metadata
                FROM orders
                WHERE user_id = $1
                  AND symbol = $2
                  AND strategy_type = $3
                  AND bot_priority = $4
                  AND order_purpose = 'OPEN'
                  AND status = 'FILLED'
                ORDER BY filled_at DESC
                LIMIT 1
                """

                open_order = await self._execute_query(
                    query_open_order,
                    (user_id, symbol, strategy_type, bot_priority),
                    fetch_one=True
                )

                if not open_order:
                    continue

                open_order_dict = dict(open_order)
                if open_order_dict.get('metadata'):
                    open_order_dict['metadata'] = json.loads(open_order_dict['metadata'])

                # Получаем все AVERAGING ордера (если есть)
                query_averaging = """
                SELECT id, user_id, symbol, side, order_type, quantity, price,
                       filled_quantity, average_price, status, order_id,
                       client_order_id, strategy_type, bot_priority, order_purpose,
                       filled_at, created_at, updated_at, metadata
                FROM orders
                WHERE user_id = $1
                  AND symbol = $2
                  AND strategy_type = $3
                  AND bot_priority = $4
                  AND order_purpose = 'AVERAGING'
                  AND status = 'FILLED'
                ORDER BY filled_at ASC
                """

                averaging_orders = await self._execute_query(
                    query_averaging,
                    (user_id, symbol, strategy_type, bot_priority),
                    fetch_all=True
                )

                averaging_orders_list = []
                if averaging_orders:
                    for avg_order in averaging_orders:
                        avg_dict = dict(avg_order)
                        if avg_dict.get('metadata'):
                            avg_dict['metadata'] = json.loads(avg_dict['metadata'])
                        averaging_orders_list.append(avg_dict)

                # Формируем данные позиции
                position_data = {
                    "symbol": symbol,
                    "strategy_type": strategy_type,
                    "bot_priority": bot_priority,
                    "open_order": open_order_dict,
                    "averaging_orders": averaging_orders_list,
                }

                open_positions.append(position_data)

            log_debug(user_id, f"Найдено {len(open_positions)} открытых позиций в БД", module_name='database')
            return open_positions

        except Exception as e:
            log_error(user_id, f"Ошибка получения всех открытых позиций: {e}", module_name='database')
            return []

    # ===============================================================================
    # УЛУЧШЕННЫЕ МЕТОДЫ ДЛЯ РАБОТЫ С ОРДЕРАМИ (С ПОЛНОЙ ИНФОРМАЦИЕЙ)
    # ===============================================================================

    async def save_order_full(self, user_id: int, symbol: str, side: str, order_type: str,
                             quantity: Decimal, price: Decimal, order_id: str,
                             strategy_type: str, order_purpose: str, leverage: int,
                             trade_id: int = None, client_order_id: str = None,
                             bot_priority: int = 1, metadata: Dict[str, Any] = None) -> Optional[int]:
        """
        Сохраняет ордер в БД с ПОЛНОЙ информацией

        Args:
            user_id: ID пользователя
            symbol: Символ
            side: Сторона (Buy/Sell)
            order_type: Тип ордера (Market/Limit)
            quantity: Количество
            price: Цена
            order_id: ID ордера на бирже
            strategy_type: Тип стратегии ('signal_scalper')
            order_purpose: Назначение ('OPEN', 'CLOSE', 'AVERAGING', 'STOPLOSS')
            leverage: Плечо
            trade_id: ID связанной сделки
            client_order_id: Клиентский ID
            bot_priority: Приоритет бота (1=PRIMARY, 2=SECONDARY, 3=TERTIARY) для Multi-Account режима
            metadata: Дополнительные данные

        Returns:
            Optional[int]: ID записи в БД или None
        """
        try:
            moscow_tz = timezone(timedelta(hours=3))
            current_time = datetime.now(moscow_tz)

            query = """
            INSERT INTO orders (
                user_id, symbol, side, order_type, quantity, price,
                order_id, client_order_id, strategy_type, order_purpose,
                leverage, trade_id, bot_priority, is_active, status,
                metadata, created_at, updated_at
            )
            VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10, $11, $12, $13, $14, $15, $16, $17, $17)
            RETURNING id
            """

            metadata_json = json.dumps(metadata or {}, cls=DecimalEncoder)

            result = await self._execute_query(
                query,
                (user_id, symbol, side, order_type, quantity, price,
                 order_id, client_order_id, strategy_type, order_purpose,
                 leverage, trade_id, bot_priority, True, 'PENDING',
                 metadata_json, current_time),
                fetch_one=True
            )

            if result:
                log_info(user_id,
                        f"📝 Ордер сохранён в БД: {order_id} | Bot{bot_priority} | {order_purpose} {side} {quantity} {symbol} @ {price} | ID в БД: {result['id']}",
                        module_name='database')
                return result['id']
            return None

        except Exception as e:
            log_error(user_id, f"Ошибка сохранения ордера {order_id}: {e}", module_name='database')
            return None

    async def update_order_on_fill(self, order_id: str, filled_quantity: Decimal,
                                  average_price: Decimal, commission: Decimal,
                                  profit: Decimal = None) -> bool:
        """
        Обновляет ордер при исполнении

        Args:
            order_id: ID ордера на бирже
            filled_quantity: Исполненное количество
            average_price: Средняя цена исполнения
            commission: Комиссия
            profit: PnL (для CLOSE ордеров)

        Returns:
            bool: True если обновление успешно
        """
        try:
            moscow_tz = timezone(timedelta(hours=3))
            filled_at = datetime.now(moscow_tz)

            query = """
            UPDATE orders
            SET
                filled_quantity = $2,
                average_price = $3,
                commission = $4,
                profit = $5,
                filled_at = $6,
                status = 'FILLED',
                is_active = FALSE,
                updated_at = $6
            WHERE order_id = $1
            RETURNING user_id, symbol, side, order_purpose
            """

            result = await self._execute_query(
                query,
                (order_id, filled_quantity, average_price, commission,
                 profit or Decimal('0'), filled_at),
                fetch_one=True
            )

            if result:
                profit_str = f"{profit:.2f}$" if profit is not None else "N/A"
                log_info(result['user_id'],
                        f"✅ Ордер исполнен: {order_id} | {result['order_purpose']} {result['side']} {result['symbol']} | "
                        f"Комиссия: {commission:.4f}$ | PnL: {profit_str} | {filled_at.strftime('%H:%M:%S')} МСК",
                        module_name='database')
                return True
            return False

        except Exception as e:
            log_error(0, f"Ошибка обновления ордера {order_id} при исполнении: {e}", module_name='database')
            return False

    async def get_active_orders_count(self, user_id: int, strategy_type: str = None) -> int:
        """
        Получает количество АКТИВНЫХ ордеров пользователя

        Args:
            user_id: ID пользователя
            strategy_type: Тип стратегии (опционально)

        Returns:
            int: Количество активных ордеров
        """
        try:
            conditions = ["user_id = $1", "is_active = TRUE"]
            params = [user_id]

            if strategy_type:
                conditions.append("strategy_type = $2")
                params.append(strategy_type)

            query = f"""
            SELECT COUNT(*) as count
            FROM orders
            WHERE {' AND '.join(conditions)}
            """

            result = await self._execute_query(query, tuple(params), fetch_one=True)
            return result['count'] if result else 0

        except Exception as e:
            log_error(user_id, f"Ошибка подсчета активных ордеров: {e}", module_name='database')
            return 0

    async def close_order(self, order_id: str, close_price: float = None,
                         close_size: float = None, realized_pnl: float = None,
                         close_reason: str = "manual_close") -> bool:
        """
        Закрывает ордер в БД (для ручного закрытия пользователем).

        Обновляет статус OPEN ордера и сохраняет информацию о закрытии.

        Args:
            order_id: ID ордера на бирже (OPEN ордер)
            close_price: Цена закрытия
            close_size: Размер закрытия
            realized_pnl: Реализованный PnL
            close_reason: Причина закрытия

        Returns:
            bool: True если успешно обновлено
        """
        try:
            moscow_tz = timezone(timedelta(hours=3))
            current_time = datetime.now(moscow_tz)

            # Обновляем метаданные OPEN ордера
            query = """
            UPDATE orders
            SET
                is_active = FALSE,
                status = 'CLOSED',
                updated_at = $2,
                metadata = jsonb_set(
                    COALESCE(metadata, '{}'::jsonb),
                    '{close_info}',
                    jsonb_build_object(
                        'close_price', $3,
                        'close_size', $4,
                        'realized_pnl', $5,
                        'close_reason', $6,
                        'closed_at', $7
                    )
                )
            WHERE order_id = $1
            RETURNING user_id, symbol
            """

            result = await self._execute_query(
                query,
                (order_id, current_time, close_price, close_size, realized_pnl,
                 close_reason, current_time.isoformat()),
                fetch_one=True
            )

            if result:
                log_info(result['user_id'],
                        f"✅ Ордер {order_id} закрыт в БД: PnL={realized_pnl:.2f}$ (причина: {close_reason})",
                        module_name='database')
                return True
            else:
                log_warning(0, f"⚠️ Ордер {order_id} не найден в БД для закрытия", module_name='database')
                return False

        except Exception as e:
            log_error(0, f"Ошибка закрытия ордера {order_id}: {e}", module_name='database')
            return False

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

