"""
SQLite база данных для симуляции торговли
Упрощенная версия без PostgreSQL зависимостей
"""
import aiosqlite
import asyncio
from datetime import datetime, timezone
from typing import Optional, List, Dict, Any
from decimal import Decimal
import json
from pathlib import Path

from core.logger import log_info, log_error, log_warning, log_debug
from core.enums import TradingStatus
from core.functions import DecimalEncoder


class SQLiteDB:
    """Менеджер SQLite базы данных для симуляции"""
    
    def __init__(self, db_path: str = "lighter_trading.db"):
        self.db_path = db_path
        self.conn: Optional[aiosqlite.Connection] = None
        self._is_initialized = False
        self._lock = asyncio.Lock()
    
    async def initialize(self) -> None:
        """Инициализация базы данных"""
        async with self._lock:
            if self._is_initialized:
                return
            
            try:
                log_info(0, f"Инициализация SQLite базы данных: {self.db_path}", 'sqlite_db')
                
                self.conn = await aiosqlite.connect(self.db_path)
                self.conn.row_factory = aiosqlite.Row
                
                await self._create_tables()
                
                self._is_initialized = True
                log_info(0, "✅ SQLite база данных инициализирована", 'sqlite_db')
                
            except Exception as e:
                log_error(0, f"Ошибка инициализации SQLite: {e}", 'sqlite_db')
                raise
    
    async def _create_tables(self):
        """Создание таблиц"""
        try:
            async with self.conn.execute("""
                CREATE TABLE IF NOT EXISTS trades (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    user_id INTEGER NOT NULL,
                    symbol TEXT NOT NULL,
                    side TEXT NOT NULL,
                    entry_price DECIMAL NOT NULL,
                    exit_price DECIMAL DEFAULT 0,
                    quantity DECIMAL NOT NULL,
                    leverage INTEGER DEFAULT 1,
                    profit DECIMAL DEFAULT 0,
                    commission DECIMAL DEFAULT 0,
                    status TEXT DEFAULT 'ACTIVE',
                    strategy_type TEXT NOT NULL,
                    bot_priority INTEGER DEFAULT 1,
                    entry_time TIMESTAMP,
                    exit_time TIMESTAMP,
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                )
            """):
                pass
            
            async with self.conn.execute("""
                CREATE TABLE IF NOT EXISTS orders (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    user_id INTEGER NOT NULL,
                    symbol TEXT NOT NULL,
                    side TEXT NOT NULL,
                    order_type TEXT NOT NULL,
                    quantity DECIMAL NOT NULL,
                    price DECIMAL NOT NULL,
                    filled_quantity DECIMAL DEFAULT 0,
                    average_price DECIMAL DEFAULT 0,
                    status TEXT DEFAULT 'NEW',
                    order_id TEXT UNIQUE NOT NULL,
                    strategy_type TEXT NOT NULL,
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                )
            """):
                pass
            
            # Индексы
            await self.conn.execute("CREATE INDEX IF NOT EXISTS idx_trades_user_symbol ON trades(user_id, symbol)")
            await self.conn.execute("CREATE INDEX IF NOT EXISTS idx_trades_status ON trades(status)")
            await self.conn.execute("CREATE INDEX IF NOT EXISTS idx_orders_user ON orders(user_id)")
            
            await self.conn.commit()
            log_info(0, "✅ Таблицы SQLite созданы", 'sqlite_db')
            
        except Exception as e:
            log_error(0, f"Ошибка создания таблиц: {e}", 'sqlite_db')
            raise
    
    async def save_trade(self, trade_data: Dict[str, Any]) -> Optional[int]:
        """Сохранение сделки"""
        try:
            cursor = await self.conn.execute("""
                INSERT INTO trades (
                    user_id, symbol, side, entry_price, exit_price, quantity, leverage,
                    profit, commission, status, strategy_type, bot_priority, entry_time, exit_time
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """, (
                trade_data['user_id'],
                trade_data['symbol'],
                trade_data['side'],
                float(trade_data['entry_price']),
                float(trade_data.get('exit_price', 0)),
                float(trade_data['quantity']),
                trade_data.get('leverage', 1),
                float(trade_data.get('profit', 0)),
                float(trade_data.get('commission', 0)),
                trade_data.get('status', 'ACTIVE'),
                trade_data['strategy_type'],
                trade_data.get('bot_priority', 1),
                trade_data.get('entry_time', datetime.now(timezone.utc)).isoformat(),
                trade_data.get('exit_time').isoformat() if trade_data.get('exit_time') else None
            ))
            
            await self.conn.commit()
            trade_id = cursor.lastrowid
            
            log_info(trade_data['user_id'], f"✅ Сделка сохранена в SQLite: trade_id={trade_id}", 'sqlite_db')
            return trade_id
            
        except Exception as e:
            log_error(trade_data.get('user_id', 0), f"Ошибка сохранения сделки: {e}", 'sqlite_db')
            return None
    
    async def update_trade_on_close(self, trade_id: int, exit_price: Decimal, pnl: Decimal, 
                                   commission: Decimal, exit_time: datetime, bot_priority: int = 1) -> bool:
        """Обновление сделки при закрытии"""
        try:
            await self.conn.execute("""
                UPDATE trades 
                SET exit_price = ?, profit = ?, commission = ?, status = 'CLOSED', exit_time = ?
                WHERE id = ? AND bot_priority = ?
            """, (
                float(exit_price),
                float(pnl),
                float(commission),
                exit_time.isoformat(),
                trade_id,
                bot_priority
            ))
            
            await self.conn.commit()
            log_info(0, f"✅ Сделка {trade_id} обновлена в SQLite", 'sqlite_db')
            return True
            
        except Exception as e:
            log_error(0, f"Ошибка обновления сделки: {e}", 'sqlite_db')
            return False
    
    async def get_trades(self, user_id: int, limit: int = 100) -> List[Dict[str, Any]]:
        """Получение списка сделок"""
        try:
            async with self.conn.execute("""
                SELECT * FROM trades 
                WHERE user_id = ? 
                ORDER BY created_at DESC 
                LIMIT ?
            """, (user_id, limit)) as cursor:
                rows = await cursor.fetchall()
                
                result = []
                for row in rows:
                    result.append({
                        'id': row['id'],
                        'user_id': row['user_id'],
                        'symbol': row['symbol'],
                        'side': row['side'],
                        'entry_price': Decimal(str(row['entry_price'])),
                        'exit_price': Decimal(str(row['exit_price'])),
                        'quantity': Decimal(str(row['quantity'])),
                        'leverage': row['leverage'],
                        'profit': Decimal(str(row['profit'])),
                        'commission': Decimal(str(row['commission'])),
                        'status': row['status'],
                        'strategy_type': row['strategy_type'],
                        'entry_time': row['entry_time'],
                        'exit_time': row['exit_time']
                    })
                
                return result
                
        except Exception as e:
            log_error(user_id, f"Ошибка получения сделок: {e}", 'sqlite_db')
            return []
    
    async def close(self):
        """Закрытие соединения"""
        try:
            if self.conn:
                await self.conn.close()
                log_info(0, "SQLite соединение закрыто", 'sqlite_db')
        except Exception as e:
            log_error(0, f"Ошибка закрытия SQLite: {e}", 'sqlite_db')


# Глобальный экземпляр
sqlite_db = SQLiteDB()

