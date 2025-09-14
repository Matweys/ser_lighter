"""
Профессиональная инициализация Telegram бота с расширенными возможностями
"""
import asyncio
from typing import Optional, Dict, Any
from aiogram import Bot, Dispatcher
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode
from aiogram.types import BotCommand, BotCommandScope, BotCommandScopeDefault
from aiogram.webhook.aiohttp_server import SimpleRequestHandler, setup_application
from aiohttp import web
import aioredis
from contextlib import asynccontextmanager

from core.settings_config import system_config
from core.logger import log_info, log_error, log_warning
from core.enums import SystemConstants
from core.events import EventBus
from aiogram.fsm.storage.redis import RedisStorage
from aiogram.fsm.storage.base import BaseEventIsolation

class TelegramBotManager:
    """Профессиональный менеджер Telegram бота"""
    
    def __init__(self):
        self.config = system_config.telegram
        self.bot: Optional[Bot] = None
        self.dp: Optional[Dispatcher] = None
        self.storage: Optional[RedisStorage] = None
        self.event_bus: Optional[EventBus] = None
        self._webhook_app: Optional[web.Application] = None
        self._is_running = False

    # Эта функция остается для совместимости, но мы будем использовать новую
    async def initialize(self) -> None:
        await self.initialize_with_app()

    async def initialize_with_app(self, bot_application: Optional[Any] = None) -> None:
        """Инициализация бота и всех компонентов с передачей приложения."""
        try:
            log_info(0, "Инициализация Telegram бота...", module_name='bot')

            await self._setup_storage()
            await self._setup_bot()

            # Передаем bot_application напрямую в Dispatcher
            self.dp = Dispatcher(storage=self.storage, bot_application=bot_application)

            self.event_bus = event_bus  # Продолжаем использовать глобальный

            await self._setup_middleware()
            await self._setup_bot_commands()

            log_info(0, "Telegram бот успешно инициализирован", module_name='bot')

        except Exception as e:
            log_error(0, f"Ошибка инициализации Telegram бота: {e}", module_name='bot')
            raise
    
    async def _setup_storage(self) -> None:
        """Настройка Redis storage"""
        try:
            redis_config = system_config.redis
            
            # Создаем Redis storage с расширенными настройками
            self.storage = RedisStorage.from_url(redis_config.url,connection_kwargs={
                    'socket_timeout': redis_config.socket_timeout,
                    'socket_connect_timeout': redis_config.socket_connect_timeout,
                    'retry_on_timeout': redis_config.retry_on_timeout,
                    'health_check_interval': redis_config.health_check_interval,
                    'max_connections': redis_config.max_connections,
                }
            )
            
            log_info(0, "Redis storage настроен", module_name='bot')
            
        except Exception as e:
            log_error(0, f"Ошибка настройки Redis storage: {e}", module_name='bot')
            raise
    
    async def _setup_bot(self) -> None:
        """Настройка бота"""
        try:
            # Создаем бота с профессиональными настройками
            self.bot = Bot(
                token=self.config.token,
                default=DefaultBotProperties(
                    parse_mode=ParseMode.HTML,
                    protect_content=False,
                    allow_sending_without_reply=True,
                    link_preview_is_disabled=True,
                )
            )
            
            # Получаем информацию о боте
            bot_info = await self.bot.get_me()
            log_info(0, f"Бот инициализирован: @{bot_info.username} ({bot_info.full_name})", module_name='bot')
            
        except Exception as e:
            log_error(0, f"Ошибка создания бота: {e}", module_name='bot')
            raise

    async def _setup_dispatcher(self) -> None:
        """Настройка диспетчера"""
        try:
            self.dp = Dispatcher(
                storage=self.storage,
            )

            # Настраиваем middleware
            await self._setup_middleware()

            log_info(0, "Диспетчер настроен", module_name='bot')

        except Exception as e:
            log_error(0, f"Ошибка настройки диспетчера: {e}", module_name='bot')
            raise

    async def _setup_middleware(self) -> None:
        """Настройка middleware"""
        try:
            # Импортируем наш глобальный event_bus
            from core.events import event_bus

            # Middleware для передачи событий в нашу систему
            @self.dp.update.outer_middleware()
            async def event_bus_middleware(handler, event, data):
                # Просто "пробрасываем" событие в нашу шину, не ожидая ответа
                # Это позволяет основной логике бота работать независимо
                asyncio.create_task(event_bus.publish(event))
                return await handler(event, data)

            log_info(0, "EventBus middleware зарегистрирован.", module_name='bot')

            # Пример базового middleware для логирования
            @self.dp.message.middleware()
            async def logging_middleware(handler, event, data):
                user_id = event.from_user.id if event.from_user else None
                log_info(user_id, f"Получено сообщение: {event.text[:50]}...", module_name='bot')
                return await handler(event, data)

            @self.dp.callback_query.middleware()
            async def callback_logging_middleware(handler, event, data):
                user_id = event.from_user.id if event.from_user else None
                log_info(user_id, f"Получен callback: {event.data}", module_name='bot')
                return await handler(event, data)

            log_info(0, "Middleware настроены", module_name='bot')

        except Exception as e:
            log_error(0, f"Ошибка настройки middleware: {e}", module_name='bot')
            raise
    
    async def _setup_bot_commands(self) -> None:
        """Настройка команд бота"""
        try:
            commands = [
                BotCommand(command="start", description="🚀 Запуск бота"),
                BotCommand(command="help", description="❓ Помощь"),
                BotCommand(command="profile", description="👤 Мой профиль"),
                BotCommand(command="balance", description="💰 Баланс"),
                BotCommand(command="strategies", description="📊 Мои стратегии"),
                BotCommand(command="settings", description="⚙️ Настройки"),
                BotCommand(command="statistics", description="📈 Статистика"),
                BotCommand(command="positions", description="📋 Позиции"),
                BotCommand(command="orders", description="📝 Ордера"),
                BotCommand(command="stop", description="⛔ Остановить все"),
            ]
            
            await self.bot.set_my_commands(
                commands=commands,
                scope=BotCommandScopeDefault()
            )
            
            log_info(0, f"Настроено {len(commands)} команд бота", module_name='bot')
            
        except Exception as e:
            log_error(0, f"Ошибка настройки команд: {e}", module_name='bot')
            raise
    
    async def start_polling(self) -> None:
        """Запуск бота в режиме polling"""
        try:
            if not self.dp or not self.bot:
                raise ValueError("Бот не инициализирован")
            
            log_info(0, "Запуск бота в режиме polling...", module_name='bot')
            self._is_running = True
            
            # Пропускаем накопившиеся обновления
            await self.bot.delete_webhook(drop_pending_updates=True)
            
            # Запускаем polling
            await self.dp.start_polling(
                self.bot,
                allowed_updates=self.config.allowed_updates,
                handle_signals=True,
            )
            
        except Exception as e:
            log_error(0, f"Ошибка запуска polling: {e}", module_name='bot')
            raise
        finally:
            self._is_running = False
    
    async def start_webhook(self, webhook_url: str, webhook_path: str = "/webhook") -> web.Application:
        """Запуск бота в режиме webhook"""
        try:
            if not self.dp or not self.bot:
                raise ValueError("Бот не инициализирован")
            
            log_info(0, f"Настройка webhook: {webhook_url}", module_name='bot')
            
            # Устанавливаем webhook
            await self.bot.set_webhook(
                url=f"{webhook_url}{webhook_path}",
                allowed_updates=self.config.allowed_updates,
                max_connections=self.config.max_connections,
                drop_pending_updates=True,
            )
            
            # Создаем веб-приложение
            app = web.Application()
            
            # Настраиваем webhook handler
            webhook_requests_handler = SimpleRequestHandler(
                dispatcher=self.dp,
                bot=self.bot,
            )
            webhook_requests_handler.register(app, path=webhook_path)
            
            # Добавляем health check endpoint
            async def health_check(_request):
                return web.json_response({
                    "status": "healthy",
                    "bot_id": self.bot.id,
                    "webhook_url": webhook_url,
                })
            
            app.router.add_get("/health", health_check)
            
            self._webhook_app = app
            self._is_running = True
            
            log_info(0, "Webhook настроен успешно", module_name='bot')
            return app
            
        except Exception as e:
            log_error(0, f"Ошибка настройки webhook: {e}", module_name='bot')
            raise
    
    async def stop(self) -> None:
        """Остановка бота"""
        try:
            log_info(0, "Остановка Telegram бота...", module_name='bot')
            
            self._is_running = False
            
            if self.bot:
                # Удаляем webhook если был установлен
                try:
                    await self.bot.delete_webhook()
                except Exception as e:
                    log_warning(0, f"Не удалось удалить вебхук при остановке (это может быть нормально): {e}", module_name='bot')
                
                # Закрываем сессию бота
                await self.bot.session.close()
            
            if self.storage:
                # Закрываем Redis соединения
                await self.storage.close()
            
            log_info(0, "Telegram бот остановлен", module_name='bot')
            
        except Exception as e:
            log_error(0, f"Ошибка остановки бота: {e}", module_name='bot')
    
    @property
    def is_running(self) -> bool:
        """Проверка состояния бота"""
        return self._is_running
    
    async def send_admin_notification(self, message: str, parse_mode: str = "HTML") -> None:
        """Отправка уведомления администраторам"""
        try:
            if not self.bot or not self.config.admin_ids:
                return
            
            for admin_id in self.config.admin_ids:
                try:
                    await self.bot.send_message(
                        chat_id=admin_id,
                        text=message,
                        parse_mode=parse_mode,
                    )
                except Exception as e:
                    log_error(0, f"Ошибка отправки уведомления админу {admin_id}: {e}", module_name='bot')
                    
        except Exception as e:
            log_error(0, f"Ошибка отправки админ уведомлений: {e}", module_name='bot')
    
    async def get_bot_stats(self) -> Dict[str, Any]:
        """Получение статистики бота"""
        try:
            if not self.bot:
                return {}
            
            bot_info = await self.bot.get_me()
            
            return {
                "bot_id": bot_info.id,
                "username": bot_info.username,
                "first_name": bot_info.first_name,
                "is_running": self._is_running,
                "admin_count": len(self.config.admin_ids),
                "webhook_configured": self._webhook_app is not None,
            }
            
        except Exception as e:
            log_error(0, f"Ошибка получения статистики бота: {e}", module_name='bot')
            return {}

# Глобальный экземпляр менеджера бота
bot_manager = TelegramBotManager()

# Для обратной совместимости
bot = None
dp = None
storage = None

async def initialize_bot() -> TelegramBotManager:
    """Инициализация глобального экземпляра бота"""
    global bot, dp, storage
    
    await bot_manager.initialize()
    
    # Устанавливаем глобальные переменные для обратной совместимости
    bot = bot_manager.bot
    dp = bot_manager.dp
    storage = bot_manager.storage
    
    return bot_manager

async def start_bot_polling():
    """Запуск бота в режиме polling"""
    if not bot_manager.bot:
        await initialize_bot()
    
    await bot_manager.start_polling()

async def start_bot_webhook(webhook_url: str, webhook_path: str = "/webhook") -> web.Application:
    """Запуск бота в режиме webhook"""
    if not bot_manager.bot:
        await initialize_bot()
    
    return await bot_manager.start_webhook(webhook_url, webhook_path)

async def stop_bot():
    """Остановка бота"""
    await bot_manager.stop()

# Context manager для управления жизненным циклом бота
@asynccontextmanager
async def bot_lifespan():
    """Context manager для управления жизненным циклом бота"""
    try:
        await initialize_bot()
        log_info(0, "Бот инициализирован в context manager", module_name='bot')
        yield bot_manager
    finally:
        await stop_bot()
        log_info(0, "Бот остановлен в context manager", module_name='bot')

