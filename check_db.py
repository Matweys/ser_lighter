#!/usr/bin/env python3
"""Скрипт для проверки базы данных SQLite"""
import asyncio
import sys
import os

# Добавляем корневую директорию в путь
project_root = os.path.dirname(os.path.abspath(__file__))
if project_root not in sys.path:
    sys.path.insert(0, project_root)

from database.sqlite_db import sqlite_db


async def main():
    """Проверка базы данных"""
    try:
        await sqlite_db.initialize()
        
        # Получаем последние 5 сделок
        async with sqlite_db.conn.execute(
            "SELECT * FROM trades ORDER BY created_at DESC LIMIT 5"
        ) as cursor:
            rows = await cursor.fetchall()
            
            if not rows:
                print("📊 База данных пуста - сделок пока нет")
            else:
                print(f"📊 Найдено сделок: {len(rows)}\n")
                print("Последние сделки:")
                print("-" * 80)
                
                # Получаем названия колонок
                columns = [description[0] for description in cursor.description]
                
                for row in rows:
                    trade = dict(zip(columns, row))
                    print(f"ID: {trade['id']}")
                    print(f"Символ: {trade['symbol']}")
                    print(f"Направление: {trade['side']}")
                    print(f"Цена входа: {trade['entry_price']}")
                    print(f"Цена выхода: {trade['exit_price']}")
                    print(f"Размер: {trade['quantity']}")
                    print(f"Прибыль: {trade['profit']}")
                    print(f"Статус: {trade['status']}")
                    print(f"Время входа: {trade['entry_time']}")
                    print(f"Время выхода: {trade['exit_time']}")
                    print("-" * 80)
        
        # Статистика
        async with sqlite_db.conn.execute(
            "SELECT COUNT(*) as total, SUM(profit) as total_profit FROM trades WHERE status = 'CLOSED'"
        ) as cursor:
            stats = await cursor.fetchone()
            if stats and stats[0]:
                print(f"\n📈 Статистика:")
                print(f"Всего закрытых сделок: {stats[0]}")
                print(f"Общая прибыль: {stats[1] or 0:.2f} USDT")
        
        await sqlite_db.close()
        
    except Exception as e:
        print(f"❌ Ошибка: {e}")
        import traceback
        traceback.print_exc()


if __name__ == "__main__":
    asyncio.run(main())

