import sys
import os
import asyncio
import logging
from contextlib import asynccontextmanager
from aiogram.types import BotCommand
from decimal import Decimal, getcontext
from aiogram import Dispatcher

# --- 1. Настройка путей (обязательно в самом верху) ---
project_root = os.path.dirname(os.path.abspath(__file__))
if project_root not in sys.path:
    sys.path.insert(0, project_root)

# --- 2. Корректные и единственные импорты ---
from core.logger import log_info, log_error, log_warning
from core.settings_config import system_config # config теперь импортируется как system_config
from database.db_trades import db_manager
from cache.redis_manager import redis_manager
from core.bot_application import BotApplication
from telegram.bot import bot_manager
from telegram.handlers import basic, callback
from core.default_configs import DefaultConfigs
from core.enums import ConfigType
from aiogram.exceptions import TelegramRetryAfter
from core.events import EventBus
# --- 3. Настройка точности ---
getcontext().prec = 28

# --- 4. Ваши функции  ---
async def set_commands():
    """Устанавливает команды, видимые в меню Telegram."""
    commands = [
        BotCommand(command="/start", description="🏠 Главное меню"),
        BotCommand(command="/autotrade_start", description="▶️ Начать торговлю"),
        BotCommand(command="/autotrade_stop", description="⏹️ Остановить торговлю"),
        BotCommand(command="/stop_all", description="🚫 Экстренная остановка"),
        BotCommand(command="/settings", description="⚙️ Настройки"),
        BotCommand(command="/stats", description="📊 Статистика"),
        BotCommand(command="/balance", description="💰 Баланс"),
        BotCommand(command="/positions", description="📈 Позиции"),
        BotCommand(command="/orders", description="📋 Ордера"),
        BotCommand(command="/autotrade_status", description="📊 Статус торговли"),
        BotCommand(command="/help", description="ℹ️ Помощь")

    ]
    await bot_manager.bot.set_my_commands(commands)

async def setup_admin_user():
    """
    Проверяет, существуют ли админы из конфига в БД, добавляет их, если нет,
    и сохраняет их API ключи из .env в базу данных.
    """
    admin_ids = system_config.telegram.admin_ids
    if not admin_ids:
        log_warning(0, "В конфигурации не указаны ID администраторов (ADMIN_IDS).", module_name=__name__)
        return

    # Получаем конфигурацию биржи Bybit из системного конфига
    bybit_config = system_config.get_exchange_config("bybit")
    if not (bybit_config and bybit_config.api_key and bybit_config.secret_key):
        log_warning(0, "API ключи для Bybit не найдены в .env. Ключи администратора не будут сохранены.", module_name=__name__)
        return

    for admin_id in admin_ids:
        try:
            # 1. Создаем или обновляем профиль администратора
            admin_exists = await db_manager.get_user(admin_id)
            if not admin_exists:
                log_info(0, f"Администратор с ID {admin_id} не найден в БД. Добавление...", module_name=__name__)
                from database.db_trades import UserProfile
                admin_profile = UserProfile(
                    user_id=admin_id,
                    username=f"admin_{admin_id}",
                    is_active=True,
                    is_premium=True
                )
                await db_manager.create_user(admin_profile)
                log_info(0, f"Администратор с ID {admin_id} успешно добавлен в БД.", module_name=__name__)

            # 2. Сохраняем API ключи для администратора
            log_info(0, f"Сохранение API ключей для администратора {admin_id}...", module_name=__name__)
            success = await db_manager.save_api_keys(
                user_id=admin_id,
                exchange="bybit",
                api_key=bybit_config.api_key,
                secret_key=bybit_config.secret_key
            )
            if success:
                log_info(0, f"API ключи для администратора {admin_id} успешно сохранены/обновлены.", module_name=__name__)
            else:
                log_error(0, f"Не удалось сохранить API ключи для администратора {admin_id}.", module_name=__name__)

        except Exception as err:
            log_error(0, f"Ошибка при настройке администратора {admin_id}: {err}", module_name=__name__)


async def initialize_default_configs():
    """Сохраняет шаблоны конфигураций по умолчанию в Redis."""
    try:
        template_user_id = 0  # Используем user_id=0 для хранения шаблонов
        all_defaults = DefaultConfigs.get_all_default_configs()

        # Сохраняем глобальный конфиг
        await redis_manager.save_config(template_user_id, ConfigType.GLOBAL, all_defaults["global_config"])

        # Сохраняем конфиги стратегий, используя новые, конкретные типы
        for s_type, s_config in all_defaults["strategy_configs"].items():
            config_enum = getattr(ConfigType, f"STRATEGY_{s_type.upper()}")
            await redis_manager.save_config(template_user_id, config_enum, s_config)

        # Сохраняем конфиги компонентов
        for c_type, c_config in all_defaults["component_configs"].items():
            config_enum = getattr(ConfigType, f"COMPONENT_{c_type.upper()}")
            await redis_manager.save_config(template_user_id, config_enum, c_config)

        log_info(0, "Шаблоны конфигураций по умолчанию сохранены в Redis.", module_name=__name__)
    except Exception as err:
        log_error(0, f"Ошибка инициализации конфигураций по умолчанию: {err}", module_name=__name__)


# --- 5. Контекстный менеджер жизненного цикла ---
@asynccontextmanager
async def lifespan_context():
    """Контекстный менеджер для управления жизненным циклом приложения"""
    bot_app = None
    # Создаем EventBus здесь, в самом начале
    event_bus = EventBus()
    try:
        log_info(0, "=== ЗАПУСК FUTURES TRADING BOT v2.0 ===", module_name="main")

        # Инициализация всех компонентов
        await db_manager.initialize()
        await redis_manager.init_redis()
        # Передаем event_bus в bot_manager при инициализации
        await bot_manager.initialize(event_bus=event_bus)

        # Регистрация роутеров
        bot_manager.dp.include_router(basic.router)
        bot_manager.dp.include_router(callback.router)
        log_info(0, "Обработчики Telegram (роутеры) зарегистрированы.", module_name="main")

        # Передаем event_bus в обработчики, как и раньше
        basic.set_event_bus(event_bus)
        callback.set_event_bus(event_bus)

        # Вызов ваших функций
        await setup_admin_user()
        await initialize_default_configs()
        try:
            await set_commands()
        except TelegramRetryAfter as err:
            log_warning(0,
                        f"Не удалось установить команды из-за флуд-лимита Telegram. Повторная попытка будет при следующем запуске. Ошибка: {err}",
                        module_name=__name__)
        except Exception as err:
            log_error(0, f"Непредвиденная ошибка при установке команд: {err}", module_name=__name__)

        # Создание и запуск основного приложения с передачей event_bus
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
        async with lifespan_context():
            await bot_manager.dp.start_polling(
                bot_manager.bot,
                allowed_updates=["message", "callback_query"],
                drop_pending_updates=True,
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
