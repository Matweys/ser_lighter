#!/usr/bin/env python3
"""
Скрипт для очистки базы данных SQLite
Удаляет все сделки и статистику
"""
import asyncio
import sys
import os
from pathlib import Path

# Добавляем корневую директорию в путь
project_root = Path(__file__).parent
if str(project_root) not in sys.path:
    sys.path.insert(0, str(project_root))

from database.sqlite_db import sqlite_db
from core.logger import log_info, log_error

async def clear_database():
    """Очистка базы данных"""
    try:
        # Устанавливаем правильный путь к базе данных (абсолютный путь)
        import os
        bot_working_dir = "/root/ser_lighter"
        db_path = os.path.join(bot_working_dir, "lighter_trading.db")
        sqlite_db.db_path = db_path
        
        log_info(0, f"Инициализация SQLite базы данных: {db_path}", module_name="clear_db")
        await sqlite_db.initialize()
        
        # Подсчитываем количество записей перед удалением
        async with sqlite_db.conn.execute('SELECT COUNT(*) FROM trades') as cursor:
            trades_count = (await cursor.fetchone())[0]
        
        async with sqlite_db.conn.execute('SELECT COUNT(*) FROM orders') as cursor:
            orders_count = (await cursor.fetchone())[0]
        
        async with sqlite_db.conn.execute('SELECT COUNT(*) FROM user_strategy_stats') as cursor:
            stats_count = (await cursor.fetchone())[0]
        
        print(f"📊 Найдено в базе данных:")
        print(f"   - Сделок: {trades_count}")
        print(f"   - Ордеров: {orders_count}")
        print(f"   - Статистики: {stats_count}")
        
        if trades_count == 0 and orders_count == 0 and stats_count == 0:
            print("✅ База данных уже пуста")
            await sqlite_db.close()
            return
        
        # Удаляем все данные
        print("\n🗑️  Удаление данных...")
        
        await sqlite_db.conn.execute('DELETE FROM trades')
        await sqlite_db.conn.execute('DELETE FROM orders')
        await sqlite_db.conn.execute('DELETE FROM user_strategy_stats')
        
        await sqlite_db.conn.commit()
        
        print("✅ База данных очищена!")
        print(f"   - Удалено сделок: {trades_count}")
        print(f"   - Удалено ордеров: {orders_count}")
        print(f"   - Удалено статистики: {stats_count}")
        
        await sqlite_db.close()
        log_info(0, "База данных успешно очищена", module_name="clear_db")
        
    except Exception as e:
        log_error(0, f"Ошибка очистки базы данных: {e}", module_name="clear_db")
        print(f"❌ Ошибка: {e}")
        if sqlite_db.conn:
            await sqlite_db.close()
        sys.exit(1)

if __name__ == "__main__":
    print("=" * 50)
    print("ОЧИСТКА БАЗЫ ДАННЫХ SQLite")
    print("=" * 50)
    print()
    print("⚠️  ВНИМАНИЕ: Все сделки, ордера и статистика будут удалены!")
    print()
    
    response = input("Продолжить? (yes/no): ").strip().lower()
    
    if response not in ['yes', 'y', 'да', 'д']:
        print("❌ Операция отменена")
        sys.exit(0)
    
    asyncio.run(clear_database())

