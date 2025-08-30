import sys
import os
import asyncio
import logging
from contextlib import asynccontextmanager
from aiogram.types import BotCommand

# Добавляем корневую папку проекта в PYTHONPATH
project_root = os.path.dirname(os.path.abspath(__file__))
if project_root not in sys.path:
    sys.path.insert(0, project_root)


from telegram.bot import bot, dp, bot_manager
from core.logger import log_info, log_error
from core.settings_config import system_config
from database.db_trades import db_manager
from cache.redis_manager import redis_manager
from core.bot_application import BotApplication
from telegram.handlers import basic_handlers, callback_handlers

# Регистрация роутеров
dp.include_router(basic_handlers.router)
dp.include_router(callback_handlers.router)

# Настройка точности для Decimal
getcontext().prec = 28

# ВОЗВРАЩАЕМ ВАШУ ФУНКЦИЮ: set_commands
async def set_commands():
    """Устанавливает команды, видимые в меню Telegram."""
    commands = [
        BotCommand(command="/start", description="🏠 Главное меню"),
        BotCommand(command="/autotrade_start", description="🚀 Начать авто-торговлю"),
        BotCommand(command="/autotrade_stop", description="🛑 Остановить авто-торговлю"),
        BotCommand(command="/autotrade_status", description="📊 Статус авто-торговли"),
        BotCommand(command="/manual", description="🛠️ Ручные стратегии"),
        BotCommand(command="/positions", description="📈 Открытые позиции"),
        BotCommand(command="/stop_all", description="🚫 Остановить ВСЕ и закрыть позиции"),
        BotCommand(command="/balance", description="💰 Баланс"),
        BotCommand(command="/parameters", description="⚙️ Настроить параметры"),
        BotCommand(command="/settings", description="🔧 Настройки стратегий"),
        BotCommand(command="/help", description="ℹ️ Помощь"),
    ]

    await bot_manager.bot.set_my_commands(commands)

async def setup_admin_user():
    """Проверяет, существует ли админ в БД, и добавляет его, если нет."""
    from core.database.db_trades import db_manager

    admin_exists = await db_manager.get_user(config.admin_id)

    if not admin_exists:
        log_info(0, f"Администратор с ID {config.admin_id} не найден в БД. Добавление...", module_name=__name__)
        try:
            # Используем новую структуру UserProfile для добавления
            from core.database.db_trades import UserProfile
            admin_profile = UserProfile(
                user_id=config.admin_id,
                username="admin",
                is_active=True
            )
            await db_manager.create_user(admin_profile)
            log_info(0, f"Администратор с ID {config.admin_id} успешно добавлен в БД.", module_name=__name__)
        except Exception as err:
            log_error(0, f"Не удалось добавить администратора в БД: {err}", module_name=__name__)


async def initialize_default_configs():
    """Инициализирует конфигурации по умолчанию для новых пользователей в Redis"""
    try:
        # Глобальные настройки по умолчанию
        default_global_config = {
            "enabled_strategies": [],
            "watchlist_symbols": ["BTCUSDT", "ETHUSDT"],
            "max_simultaneous_trades": 2,
            "global_daily_drawdown_percent": 5.0,
            "risk_per_trade_percent": 1.0,
            "analysis_config": {
                "fast_ema": 20,
                "slow_ema": 50,
                "adx_period": 14,
                "atr_period": 14,
                "timeframes": ["15m", "1h", "4h"]
            }
        }

        # Настройки стратегий по умолчанию
        default_strategies = {
            "grid_scalping": {
                "enabled": False,
                "leverage": 3,
                "initial_order_amount": 10.0,
                "averaging_order_amount": 10.0,
                "profit_percent": 1.0,
                "drop_percent": 1.0,
                "max_averaging": 5,
                "stop_loss_percent": 1.0
            },
            "bidirectional_grid": {
                "enabled": False,
                "leverage": 3,
                "order_amount": 10.0,
                "grid_levels": 6,
                "grid_step_percent": 1.0,
                "profit_percent": 1.0
            },
            "impulse_trailing": {
                "enabled": False,
                "leverage": 5,
                "order_amount": 50.0,
                "initial_stop_loss_percent": 1.0,
                "trailing_percent": 2.0,
                "min_profit_percent": 1.0
            }
        }

        # Сохраняем шаблоны в Redis
        await redis_manager.set_json("default:global_config", default_global_config)
        for strategy_name, strategy_config in default_strategies.items():
            await redis_manager.set_json(f"default:strategy:{strategy_name}", strategy_config)

        log_info(0, "Конфигурации по умолчанию инициализированы", module_name=__name__)

    except Exception as err:
        log_error(0, f"Ошибка инициализации конфигураций по умолчанию: {err}", module_name=__name__)


@asynccontextmanager
async def lifespan_context():
    """Контекстный менеджер для управления жизненным циклом приложения"""
    bot_app = None
    try:
        log_info(0, "=== ЗАПУСК FUTURES TRADING BOT v2.0 ===", module_name=__name__)

        # Инициализация всех компонентов
        await db_manager.initialize()
        await redis_manager.init_redis()
        await bot_manager.initialize() # Инициализируем Telegram бота

        # Установка команд бота
        await set_commands()

        # Создание и запуск BotApplication
        bot_app = BotApplication()
        await bot_app.start()

        log_info(0, "=== БОТ УСПЕШНО ЗАПУЩЕН ===", module_name=__name__)
        yield bot_app
    finally:
        # Очистка при завершении
        log_info(0, "=== ЗАВЕРШЕНИЕ РАБОТЫ БОТА ===", module_name=__name__)
        if bot_app:
            await bot_app.stop()
        await redis_manager.close()
        await db_manager.close()
        await bot_manager.stop()
        log_info(0, "=== БОТ ЗАВЕРШЕН ===", module_name=__name__)

async def main():
    """Главная функция запуска бота"""
    try:
        async with lifespan_context() as bot_app:
            await dp.start_polling(
                bot_manager.bot,
                allowed_updates=["message", "callback_query"],
                drop_pending_updates=True,
                bot_application=bot_app
            )
    except (KeyboardInterrupt, SystemExit):
        log_info(0, "Получен сигнал завершения", module_name=__name__)
    except Exception as err:
        log_error(0, f"Критическая ошибка в main(): {err}", module_name=__name__)

if __name__ == "__main__":
    try:
        # Настройка логирования для aiogram
        logging.getLogger("aiogram").setLevel(logging.WARNING)
        logging.getLogger("aiohttp").setLevel(logging.WARNING)

        # Запуск бота
        asyncio.run(main())

    except KeyboardInterrupt:
        print("\n🛑 Бот остановлен пользователем")
    except Exception as e:
        print(f"\n❌ Критическая ошибка: {e}")
        raise
