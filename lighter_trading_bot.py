"""
Скрипт запуска торгового бота для Lighter биржи
Имитация торговли на основе сигналов EMA+RSI
"""
import asyncio
import sys
import os
from decimal import Decimal
from datetime import datetime

# Добавляем корневую директорию в путь
project_root = os.path.dirname(os.path.abspath(__file__))
if project_root not in sys.path:
    sys.path.insert(0, project_root)

from core.logger import log_info, log_error, log_warning
from core.settings_config import system_config
from database.sqlite_db import sqlite_db
from api.lighter_simulator import LighterSimulator
from strategies.lighter_signal_scalper_strategy import LighterSignalScalperStrategy
from core.events import EventBus
from telegram.bot import bot_manager
from core.default_configs import DefaultConfigs
from core.enums import ConfigType
from cache.redis_manager import redis_manager


async def main():
    """Главная функция запуска бота для Lighter"""
    log_info(0, "=== ЗАПУСК LIGHTER TRADING BOT ===", module_name="lighter_bot")
    
    try:
        # Инициализация компонентов
        await sqlite_db.initialize()
        
        # Redis опционален (для конфигураций)
        try:
            await redis_manager.init_redis()
            use_redis = True
        except Exception as e:
            log_warning(0, f"Redis недоступен, используем конфигурацию по умолчанию: {e}", module_name="lighter_bot")
            use_redis = False
        
        event_bus = EventBus()
        await event_bus.start()
        
        await bot_manager.initialize(event_bus=event_bus)
        
        # Настройка конфигураций
        user_id = 0  # Используем user_id=0 для шаблонов
        
        if use_redis:
            try:
                await DefaultConfigs.create_default_user_config(user_id)
                strategy_config = await redis_manager.get_config(user_id, ConfigType.STRATEGY_SIGNAL_SCALPER)
            except Exception as e:
                log_warning(0, f"Ошибка загрузки конфигурации из Redis: {e}", module_name="lighter_bot")
                use_redis = False
        
        if not use_redis:
            # Используем конфигурацию по умолчанию
            strategy_config = DefaultConfigs.get_signal_scalper_config()
            log_info(0, "Используется конфигурация по умолчанию (Redis недоступен)", module_name="lighter_bot")
        
        # Параметры (симуляция - ключи не нужны!)
        symbol = os.getenv("LIGHTER_SYMBOL", "SOL")
        
        log_info(0, f"🎮 РЕЖИМ СИМУЛЯЦИИ: symbol={symbol} (API ключи не требуются!)", module_name="lighter_bot")
        
        # Создание симулятора (без реальных API ключей!)
        api = LighterSimulator(
            user_id=user_id,
            symbol=symbol
        )
        
        await api.connect()
        
        # Создание стратегии
        strategy = LighterSignalScalperStrategy(
            user_id=user_id,
            symbol=symbol,
            signal_data={},
            api=api,
            event_bus=event_bus,
            bot=bot_manager.bot,
            config=strategy_config,
            account_priority=1
        )
        
        # Запуск стратегии
        await strategy.start()
        
        log_info(0, "=== БОТ УСПЕШНО ЗАПУЩЕН ===", module_name="lighter_bot")
        
        # Отправка уведомления о старте в Telegram
        try:
            from datetime import datetime
            moscow_time = datetime.now().strftime("%Y-%m-%d %H:%M:%S MSK")
            
            start_message = (
                f"🚀 <b>Lighter Trading Bot запущен!</b>\n\n"
                f"📊 <b>Символ:</b> <code>{symbol}</code>\n"
                f"🎮 <b>Режим:</b> Симуляция\n"
                f"⏰ <b>Время запуска:</b> {moscow_time}\n\n"
                f"✅ Бот начал мониторинг сигналов EMA+RSI"
            )
            
            # Отправляем в канал или админам
            await bot_manager.send_admin_notification(start_message, parse_mode="HTML")
            log_info(0, "✅ Уведомление о старте отправлено в Telegram", module_name="lighter_bot")
        except Exception as e:
            log_warning(0, f"Не удалось отправить уведомление о старте: {e}", module_name="lighter_bot")
        
        # Запуск Telegram бота
        await bot_manager.start_polling()
        
    except KeyboardInterrupt:
        log_info(0, "Получен сигнал завершения", module_name="lighter_bot")
    except Exception as e:
        log_error(0, f"Критическая ошибка: {e}", module_name="lighter_bot")
    finally:
        # Очистка ресурсов
        try:
            if 'strategy' in locals():
                await strategy.stop()
            if 'api' in locals():
                await api.close()
            try:
                if redis_manager.is_connected:
                    await redis_manager.close()
            except Exception:
                pass
            if sqlite_db.conn:
                await sqlite_db.close()
            if bot_manager.is_running:
                await bot_manager.stop()
        except Exception as e:
            log_error(0, f"Ошибка очистки ресурсов: {e}", module_name="lighter_bot")
        
        log_info(0, "=== БОТ ОСТАНОВЛЕН ===", module_name="lighter_bot")


if __name__ == "__main__":
    asyncio.run(main())

