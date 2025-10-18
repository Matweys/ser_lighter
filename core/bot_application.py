# core/bot_application.py
"""
Главное приложение многопользовательской торговой системы
Управляет пользовательскими сессиями и глобальными компонентами
Объединенная версия с лучшими компонентами из обеих версий
"""
import asyncio
from typing import Dict, Optional, Set, Any, List
from decimal import Decimal
from datetime import datetime
from aiogram import Bot
from core.logger import log_info, log_error, log_warning
from core.events import (
    EventType, BaseEvent, UserSessionStartedEvent, UserSessionStoppedEvent,
    UserSettingsChangedEvent, RiskLimitExceededEvent, EventBus, event_bus,
    UserSessionStartRequestedEvent, UserSessionStopRequestedEvent
)

from cache.redis_manager import redis_manager
from core.user_session import UserSession
from websocket.websocket_manager import GlobalWebSocketManager
from core.default_configs import DefaultConfigs
from core.enums import ConfigType
from core.settings_config import system_config
from database.db_trades import db_manager



class BotApplication:
    """
    Главный класс приложения, управляющий жизненным циклом пользовательских торговых сессий
    """
    
    def __init__(self, bot: Bot):
        self.bot = bot
        self.event_bus = event_bus
        self.active_sessions: Dict[int, UserSession] = {}
        self.session_tasks: Dict[int, asyncio.Task] = {}
        self._running = False
        self._monitor_task: Optional[asyncio.Task] = None
        
        # Глобальные компоненты
        self.global_websocket_manager: Optional[GlobalWebSocketManager] = None

        # Статистика приложения
        self.app_stats = {
            "start_time": datetime.now(),
            "total_users": 0,
            "active_sessions": 0,
            "total_strategies_launched": 0,
            "system_restarts": 0
        }
        
        # Блокировка для thread-safety
        self.lock = asyncio.Lock()
        
    async def start(self):
        """Запуск BotApplication"""
        if self._running:
            return
            
        log_info(0, "Запуск BotApplication...", module_name=__name__)
        
        try:
            # Запуск EventBus
            await self.event_bus.start()

            # Подписка на системные события
            await self.event_bus.subscribe(EventType.USER_SESSION_STARTED, self._handle_session_start)
            await self.event_bus.subscribe(EventType.USER_SESSION_STOPPED, self._handle_session_stop)
            await self.event_bus.subscribe(EventType.USER_SETTINGS_CHANGED, self._handle_settings_changed)
            await self.event_bus.subscribe(EventType.RISK_LIMIT_EXCEEDED, self._handle_risk_limit)

            await self.event_bus.subscribe(EventType.USER_SESSION_START_REQUESTED, self._handle_session_start_request)
            await self.event_bus.subscribe(EventType.USER_SESSION_STOP_REQUESTED, self._handle_session_stop_request)

            # Инициализация глобальных компонентов
            await self._initialize_global_components()
            
            # Восстановление пользовательских сессий
            await self._restore_user_sessions()
            
            # Запуск мониторинга
            self._monitor_task = asyncio.create_task(self._monitoring_loop())
            
            self._running = True
            
            log_info(
                0,
                f"BotApplication запущен. Активных сессий: {len(self.active_sessions)}",
                module_name=__name__
            )
            
        except Exception as err:
            log_error(0, f"Ошибка запуска BotApplication: {err}", module_name=__name__)
            await self.stop()
            raise
            
    async def stop(self):
        """Остановка BotApplication"""
        if not self._running:
            return
            
        log_info(0, "Остановка BotApplication...", module_name=__name__)
        
        try:
            async with self.lock:
                self._running = False
                
                # Остановка мониторинга
                if self._monitor_task and not self._monitor_task.done():
                    self._monitor_task.cancel()
                    try:
                        await self._monitor_task
                    except asyncio.CancelledError:
                        pass
                
                # Остановка всех пользовательских сессий
                await self._stop_all_user_sessions("Application shutdown")
                
                # Остановка глобальных компонентов
                await self._stop_global_components()

                # Остановка EventBus
                await self.event_bus.stop()
                
                # Сохранение финальной статистики
                await self._save_final_stats()
                
            log_info(0, "BotApplication остановлен", module_name=__name__)
            
        except Exception as err:
            log_error(0, f"Ошибка остановки BotApplication: {err}", module_name=__name__)
            
    async def create_user_session(self, user_id: int) -> bool:
        """
        Создание пользовательской сессии
        
        Args:
            user_id: ID пользователя
            
        Returns:
            bool: True если сессия создана успешно
        """
        try:
            async with self.lock:
                if user_id in self.active_sessions:
                    log_info(user_id, "Сессия уже существует", module_name=__name__)
                    return True

                # Инициализация конфигураций по умолчанию
                await self._initialize_user_configs(user_id)
                
                # Создание сессии
                session = UserSession(user_id, self.event_bus, self.global_websocket_manager, self.bot)
                
                # Запуск сессии
                if await session.start():
                    self.active_sessions[user_id] = session
                    
                    # Обновление статистики
                    self.app_stats["total_users"] += 1
                    self.app_stats["active_sessions"] = len(self.active_sessions)
                    
                    # Публикация события
                    event = UserSessionStartedEvent(user_id=user_id, timestamp=datetime.now())
                    await self.event_bus.publish(event)
                    
                    log_info(user_id, "Пользовательская сессия создана", module_name=__name__)
                    return True
                else:
                    log_error(user_id, "Не удалось запустить сессию", module_name=__name__)
                    return False
                    
        except Exception as e:
            log_error(user_id, f"Ошибка создания сессии: {e}", module_name=__name__)
            return False
            
    async def stop_user_session(self, user_id: int, reason: str = "Manual stop") -> bool:
        """
        Остановка пользовательской сессии
        
        Args:
            user_id: ID пользователя
            reason: Причина остановки
            
        Returns:
            bool: True если сессия остановлена успешно
        """
        try:
            async with self.lock:
                if user_id not in self.active_sessions:
                    log_info(user_id, "Сессия не найдена", module_name=__name__)
                    return True

                session = self.active_sessions[user_id]

                # Обновляем статус в Redis ПЕРЕД остановкой
                session_data = await redis_manager.get_user_session(user_id) or {}
                session_data['autotrade_enabled'] = False
                await redis_manager.create_user_session(user_id, session_data)

                await session.stop(reason)

                # Удаление из активных сессий
                del self.active_sessions[user_id]

                # Остановка задачи сессии
                if user_id in self.session_tasks:
                    task = self.session_tasks[user_id]
                    if not task.done():
                        task.cancel()
                    del self.session_tasks[user_id]
                
                # Обновление статистики
                self.app_stats["active_sessions"] = len(self.active_sessions)
                
                # Публикация события
                event = UserSessionStoppedEvent(user_id=user_id, reason=reason, timestamp=datetime.now())
                await self.event_bus.publish(event)
                
                log_info(user_id, f"Пользовательская сессия остановлена: {reason}", module_name=__name__)
                return True
                
        except Exception as e:
            log_error(user_id, f"Ошибка остановки сессии: {e}", module_name=__name__)
            return False
            
    async def get_user_session_status(self, user_id: int) -> Optional[Dict[str, Any]]:
        """
        Получение статуса пользовательской сессии
        
        Args:
            user_id: ID пользователя
            
        Returns:
            Dict: Статус сессии или None если сессия не найдена
        """
        try:
            if user_id in self.active_sessions:
                return await self.active_sessions[user_id].get_status()
            else:
                return None
                
        except Exception as e:
            log_error(user_id, f"Ошибка получения статуса сессии: {e}", module_name=__name__)
            return None
            
    async def get_app_status(self) -> Dict[str, Any]:
        """Получение статуса приложения"""
        try:
            # Статистика пользовательских сессий
            sessions_stats = {}
            for user_id, session in self.active_sessions.items():
                sessions_stats[str(user_id)] = await session.get_status()
                
            return {
                "running": self._running,
                "app_stats": {
                    "start_time": self.app_stats["start_time"].isoformat(),
                    "runtime": str(datetime.now() - self.app_stats["start_time"]),
                    "total_users": self.app_stats["total_users"],
                    "active_sessions": self.app_stats["active_sessions"],
                    "total_strategies_launched": self.app_stats["total_strategies_launched"],
                    "system_restarts": self.app_stats["system_restarts"]
                },
                "global_components": {
                    "websocket_manager_running": (
                        self.global_websocket_manager.running 
                        if self.global_websocket_manager else False
                    )
                },
                "user_sessions": sessions_stats
            }
            
        except Exception as e:
            log_error(0, f"Ошибка получения статуса приложения: {e}", module_name=__name__)
            return {"running": self._running, "error": str(e)}

    async def _initialize_global_components(self):
        """Инициализация глобальных компонентов"""
        try:
            exchange_config = system_config.get_exchange_config("bybit")
            use_demo = exchange_config.demo if exchange_config else False

            # Инициализация глобального WebSocket менеджера
            self.global_websocket_manager = GlobalWebSocketManager(self.event_bus, demo=use_demo)
            await self.global_websocket_manager.start()

            log_info(0, "Глобальные компоненты инициализированы", module_name=__name__)

        except Exception as e:
            log_error(0, f"Ошибка инициализации глобальных компонентов: {e}", module_name=__name__)
            raise

    async def _stop_global_components(self):
        """Остановка глобальных компонентов"""
        try:
            if self.global_websocket_manager:
                await self.global_websocket_manager.stop()

            log_info(0, "Глобальные компоненты остановлены", module_name=__name__)

        except Exception as e:
            log_error(0, f"Ошибка остановки глобальных компонентов: {e}", module_name=__name__)

    async def _restore_user_sessions(self):
        """Восстановление пользовательских сессий после перезапуска"""
        try:
            active_users = await redis_manager.get_active_users()

            if not active_users:
                log_info(0, "Нет активных сессий для восстановления.", module_name=__name__)
                return

            restored_count = 0
            for user_id in active_users:
                try:
                    # Проверяем, включил ли пользователь автоторговлю
                    session_data = await redis_manager.get_user_session(user_id)
                    if not session_data or not session_data.get('autotrade_enabled'):
                        log_info(user_id, "Автотрейдинг для пользователя не включен, сессия не будет восстановлена.",
                                 module_name=__name__)
                        continue

                    # Проверка конфигурации пользователя
                    global_config = await redis_manager.get_config(user_id, ConfigType.GLOBAL)
                    if not global_config:
                        log_warning(0,
                                    f"Нет конфигурации для активного пользователя {user_id}, сессия не будет восстановлена.",
                                    module_name=__name__)
                        continue

                    # Создание и запуск сессии
                    session = UserSession(user_id, self.event_bus, self.global_websocket_manager, self.bot)

                    # КРИТИЧНО: Устанавливаем флаг восстановления ПЕРЕД стартом сессии
                    session.is_bot_restart = True

                    if await session.start():
                        self.active_sessions[user_id] = session
                        restored_count += 1
                        log_info(user_id, "Сессия восстановлена, так как автотрейдинг был активен.",
                                 module_name=__name__)

                        # НОВЫЙ ФУНКЦИОНАЛ: Восстановление активных стратегий после перезагрузки
                        await self._restore_strategies_for_user(user_id, session)

                        # Сбрасываем флаг после восстановления
                        session.is_bot_restart = False

                except Exception as e:
                    log_error(0, f"Ошибка восстановления сессии для пользователя {user_id}: {e}", module_name=__name__)
                    continue
                    
            # Обновление статистики
            self.app_stats["active_sessions"] = len(self.active_sessions)
            if restored_count > 0:
                self.app_stats["system_restarts"] += 1
            log_info(0,f"Восстановлено {restored_count} пользовательских сессий", module_name=__name__)
        except Exception as e:
            log_error(0, f"Ошибка восстановления сессий: {e}", module_name=__name__)
            
    async def _stop_all_user_sessions(self, reason: str):
        """Остановка всех пользовательских сессий"""
        try:
            user_ids = list(self.active_sessions.keys())
            
            for user_id in user_ids:
                await self.stop_user_session(user_id, reason)
                
            log_info(0, f"Остановлено {len(user_ids)} пользовательских сессий", module_name=__name__)
            
        except Exception as e:
            log_error(0, f"Ошибка остановки всех сессий: {e}", module_name=__name__)

    @staticmethod
    async def _initialize_user_configs(user_id: int):
        """Инициализация конфигураций пользователя по умолчанию"""
        try:
            global_config = await redis_manager.get_config(user_id, ConfigType.GLOBAL)

            if not global_config:
                default_configs = DefaultConfigs.get_all_default_configs()

                await redis_manager.save_config(
                    user_id,
                    ConfigType.GLOBAL,
                    default_configs["global_config"]
                )

                for strategy_type, strategy_config in default_configs["strategy_configs"].items():
                    config_enum = getattr(ConfigType, f"STRATEGY_{strategy_type.upper()}")
                    await redis_manager.save_config(
                        user_id,
                        config_enum,
                        strategy_config
                    )

                for component_type, component_config in default_configs["component_configs"].items():
                    config_enum = getattr(ConfigType, f"COMPONENT_{component_type.upper()}")
                    await redis_manager.save_config(
                        user_id,
                        config_enum,
                        component_config
                    )

                log_info(user_id, "Конфигурации по умолчанию созданы", module_name=__name__)

        except Exception as e:
            log_error(user_id, f"Ошибка инициализации конфигураций: {e}", module_name=__name__)
            raise
            
    async def _monitoring_loop(self):
        """Цикл мониторинга состояния приложения"""
        try:
            while self._running:
                await asyncio.sleep(60)  # Проверка каждую минуту
                
                if not self._running:
                    break
                    
                # Проверка состояния сессий
                await self._check_sessions_health()
                
                # Обновление статистики
                await self._update_app_stats()
                
        except asyncio.CancelledError:
            log_info(0, "Мониторинг остановлен", module_name=__name__)
        except Exception as e:
            log_error(0, f"Ошибка в цикле мониторинга: {e}", module_name=__name__)
            
    async def _check_sessions_health(self):
        """Проверка состояния пользовательских сессий"""
        try:
            unhealthy_sessions = []
            
            for user_id, session in self.active_sessions.items():
                if not session.running:
                    unhealthy_sessions.append(user_id)
                    
            # Удаление неактивных сессий
            for user_id in unhealthy_sessions:
                await self.stop_user_session(user_id, "Session health check failed")
                
        except Exception as e:
            log_error(0, f"Ошибка проверки состояния сессий: {e}", module_name=__name__)

    async def _update_app_stats(self):
        """Обновление статистики приложения"""
        try:
            self.app_stats["active_sessions"] = len(self.active_sessions)
            await redis_manager.cache_data("app:stats", self.app_stats, ttl=600)
        except Exception as e:
            log_error(0, f"Ошибка обновления статистики: {e}", module_name=__name__)

    async def _save_final_stats(self):
        """Сохранение финальной статистики"""
        try:
            final_stats = {
                **self.app_stats,
                "shutdown_time": datetime.now().isoformat(),
                "total_runtime": str(datetime.now() - self.app_stats["start_time"])
            }

            # ИСПРАВЛЕНИЕ: Используем cache_data с TTL=None, чтобы сохранить
            # финальную статистику навсегда.
            await redis_manager.cache_data("app:final_stats", final_stats, ttl=None)
        except Exception as e:
            log_error(0, f"Ошибка сохранения финальной статистики: {e}", module_name=__name__)
            
    # Обработчики событий
    @staticmethod
    async def _handle_session_start(event: UserSessionStartedEvent):  # Также исправлен тип события
        """Обработчик события запуска сессии"""
        log_info(event.user_id, "Получено событие запуска сессии", module_name=__name__)

    @staticmethod
    async def _handle_session_stop(event: UserSessionStoppedEvent):  # Также исправлен тип события
        """Обработчик события остановки сессии"""
        log_info(event.user_id, f"Получено событие остановки сессии: {event.reason}", module_name=__name__)
        
    async def _handle_settings_changed(self, event: UserSettingsChangedEvent):
        """Обработчик события изменения настроек"""
        log_info(event.user_id, f"Настройки изменены: {event.changed_settings}", module_name=__name__)
        
        # Перезагрузка сессии при критических изменениях
        if any(setting in ["global_config", "risk_config"] for setting in event.changed_settings):
            if event.user_id in self.active_sessions:
                await self.stop_user_session(event.user_id, "Settings changed")
                await self.create_user_session(event.user_id)
                
    async def _handle_risk_limit(self, event: RiskLimitExceededEvent):
        """Обработчик события превышения лимита риска"""
        log_error(event.user_id, f"Превышен лимит риска: {event.limit_type}", module_name=__name__)
        
        # Экстренная остановка сессии
        if event.user_id in self.active_sessions:
            await self.stop_user_session(event.user_id, f"Risk limit exceeded: {event.limit_type}")

    async def _handle_session_start_request(self, event: UserSessionStartRequestedEvent):
        """Обработчик запроса на запуск авто-торговли."""
        user_id = event.user_id
        log_info(user_id, "🎯 [SESSION_START_HANDLER] Получен запрос на запуск авто-торговли...", module_name=__name__)
        log_info(user_id, f"🎯 [SESSION_START_HANDLER] Событие: {event}", module_name=__name__)

        session = self.active_sessions.get(user_id)
        if not session or not session.running:
            log_warning(user_id, "Активная сессия не найдена, создается новая для старта торговли.", module_name=__name__)
            if not await self.create_user_session(user_id):
                log_error(user_id, "Не удалось создать и запустить сессию для старта.", module_name=__name__)
                return
            # После успешного создания сессия будет в self.active_sessions
            session = self.active_sessions.get(user_id)

        if session:
            # Обновляем статус в Redis
            session_data = await redis_manager.get_user_session(user_id) or {}
            session_data['autotrade_enabled'] = True
            await redis_manager.create_user_session(user_id, session_data)
            log_info(user_id, "Статус авто-торговли установлен в 'active' в Redis.", module_name=__name__)

            # КРИТИЧЕСКИ ВАЖНО: Запускаем стратегии для всех символов из watchlist
            global_config = await redis_manager.get_config(user_id, ConfigType.GLOBAL)
            if global_config:
                watchlist_symbols = global_config.get("watchlist_symbols", [])
                if watchlist_symbols:
                    log_info(user_id, f"🚀 Запуск стратегий для {len(watchlist_symbols)} символов из watchlist: {watchlist_symbols}", module_name=__name__)

                    # Запускаем стратегии для каждого символа
                    from core.enums import StrategyType
                    for symbol in watchlist_symbols:
                        try:
                            success = await session.start_strategy(
                                strategy_type=StrategyType.SIGNAL_SCALPER.value,
                                symbol=symbol,
                                analysis_data={'trigger': 'autotrade_start'}
                            )
                            if success:
                                log_info(user_id, f"✅ Стратегия для {symbol} успешно запущена", module_name=__name__)
                            else:
                                log_warning(user_id, f"⚠️ Не удалось запустить стратегию для {symbol}", module_name=__name__)
                        except Exception as e:
                            log_error(user_id, f"❌ Ошибка запуска стратегии для {symbol}: {e}", module_name=__name__)
                else:
                    log_warning(user_id, "⚠️ Список watchlist_symbols пуст, стратегии не запущены", module_name=__name__)
            else:
                log_error(user_id, "❌ Не удалось получить global_config для запуска стратегий", module_name=__name__)
        else:
            log_error(user_id, "Не удалось получить сессию после попытки создания.", module_name=__name__)


    async def _handle_session_stop_request(self, event: UserSessionStopRequestedEvent):
        """Обработчик запроса на остановку авто-торговли."""
        user_id = event.user_id
        log_info(user_id, f"Получен запрос на остановку авто-торговли (причина: {event.reason})...", module_name=__name__)

        session = self.active_sessions.get(user_id)
        if session:
            await session.stop_all_strategies(event.reason)

        # Обновляем статус напрямую в Redis
        session_data = await redis_manager.get_user_session(user_id) or {}
        session_data['autotrade_enabled'] = False
        await redis_manager.create_user_session(user_id, session_data)
        log_info(user_id, "Статус авто-торговли установлен в 'inactive' в Redis.", module_name=__name__)

    # --- ПУБЛИЧНЫЕ МЕТОДЫ ДЛЯ ВЫЗОВА ИЗВНЕ ---

    async def request_session_start(self, user_id: int):
        """Публичный метод для безопасного запроса на запуск сессии."""
        await self._handle_session_start_request(
            UserSessionStartRequestedEvent(user_id=user_id)
        )

    async def request_session_stop(self, user_id: int, reason: str = "user_request"):
        """Публичный метод для безопасного запроса на остановку сессии."""
        await self._handle_session_stop_request(
            UserSessionStopRequestedEvent(user_id=user_id, reason=reason)
        )

    @property
    def is_running(self) -> bool:
        """
        Публичное свойство для безопасной проверки, запущено ли приложение.
        Возвращает True, если приложение активно.
        """
        return self._running

    # ===============================================================================
    # СИСТЕМА ВОССТАНОВЛЕНИЯ СТРАТЕГИЙ ПОСЛЕ ПЕРЕЗАГРУЗКИ СЕРВЕРА
    # ===============================================================================

    async def _restore_strategies_for_user(self, user_id: int, session: 'UserSession'):
        """
        ПОЛНОСТЬЮ ПЕРЕПИСАННАЯ ВЕРСИЯ.
        Восстанавливает активные стратегии пользователя после перезагрузки сервера.

        РАБОТАЕТ ТОЛЬКО С ОРДЕРАМИ БОТА ИЗ БД!
        Игнорирует ручные ордера пользователя.
        """
        try:
            # Уведомляем пользователя о начале восстановления
            await self._notify_user_about_server_restart(user_id)

            # Импорты
            from core.enums import StrategyType
            from strategies.base_strategy import BaseStrategy

            strategy_types = [StrategyType.SIGNAL_SCALPER]
            restored_strategies = []

            # Получаем конфигурацию пользователя
            global_config = await redis_manager.get_config(user_id, ConfigType.GLOBAL)
            if not global_config:
                log_error(user_id, "Нет конфигурации пользователя", "BotApplication")
                return

            watchlist_symbols = global_config.get("watchlist_symbols", [])
            auto_trading_enabled = global_config.get("auto_trading_enabled", False)

            if not watchlist_symbols:
                log_info(user_id, "Список символов пуст, восстановление стратегий пропущено", "BotApplication")
                await self.bot.send_message(
                    chat_id=user_id,
                    text="ℹ️ <b>Проверка завершена</b>\n\nСписок символов watchlist пуст.",
                    parse_mode="HTML"
                )
                return

            # ===================================================================
            # КЛЮЧЕВАЯ ЧАСТЬ: Получаем активные ордера БОТА из БД
            # ===================================================================
            active_orders_from_db = []
            has_active_orders = False

            try:
                active_orders_from_db = await db_manager.get_active_orders_by_user(
                    user_id=user_id,
                    symbol=None,  # Все символы
                    strategy_type=None  # Все стратегии
                )

                if active_orders_from_db:
                    has_active_orders = True
                    log_info(user_id, f"🎯 Найдено {len(active_orders_from_db)} активных ордеров бота в БД", "BotApplication")

                    # Группируем ордера по символам
                    orders_by_symbol = {}
                    for order in active_orders_from_db:
                        symbol = order['symbol']
                        if symbol not in orders_by_symbol:
                            orders_by_symbol[symbol] = []
                        orders_by_symbol[symbol].append(order)

                    log_info(user_id, f"📊 Активные ордера по символам: {list(orders_by_symbol.keys())}", "BotApplication")
                else:
                    log_info(user_id, "✅ Активных ордеров бота не найдено", "BotApplication")

            except Exception as e:
                log_error(user_id, f"Ошибка проверки активных ордеров в БД: {e}", "BotApplication")

            # ===================================================================
            # Определяем: нужно ли восстанавливать стратегии
            # ===================================================================
            real_auto_trading_status = auto_trading_enabled or has_active_orders

            log_info(user_id,
                    f"Статус: autotrade={'✅' if auto_trading_enabled else '❌'}, "
                    f"active_orders={'✅' if has_active_orders else '❌'}, "
                    f"итог={'✅ ВОССТАНАВЛИВАЕМ' if real_auto_trading_status else '❌ НЕ ВОССТАНАВЛИВАЕМ'}",
                    "BotApplication")

            if not real_auto_trading_status:
                # Автоторговля отключена И нет активных ордеров
                recovery_message = (
                    f"ℹ️ <b>Проверка завершена</b>\n\n"
                    f"Автоторговля отключена и активных ордеров бота не найдено.\n"
                    f"Включите автоторговлю в настройках для запуска стратегий."
                )
                await self.bot.send_message(
                    chat_id=user_id,
                    text=recovery_message,
                    parse_mode="HTML"
                )
                return

            # ===================================================================
            # Уведомляем об активных ордерах
            # ===================================================================
            if has_active_orders:
                orders_text = "\n".join([
                    f"📊 {order['symbol']}: {order.get('side', 'N/A')} - ID:{order['order_id']}"
                    for order in active_orders_from_db[:5]
                ])
                if len(active_orders_from_db) > 5:
                    orders_text += f"\n...и ещё {len(active_orders_from_db) - 5} ордеров"

                order_alert = (
                    f"🚨 <b>ОБНАРУЖЕНЫ АКТИВНЫЕ ОРДЕРА БОТА</b>\n\n"
                    f"{orders_text}\n\n"
                    f"🔄 Запускаю восстановление стратегий для управления ордерами..."
                )
                await self.bot.send_message(
                    chat_id=user_id,
                    text=order_alert,
                    parse_mode="HTML"
                )

            # Сначала пытаемся восстановить стратегии из сохранённых состояний
            for strategy_type in strategy_types:
                for symbol in watchlist_symbols:
                    try:
                        saved_state = await BaseStrategy.restore_strategy_state(user_id, symbol, strategy_type)

                        if saved_state:
                            log_info(user_id, f"🔄 Найдено сохранённое состояние {strategy_type.value} для {symbol}", "BotApplication")

                            # Создаём и восстанавливаем стратегию через UserSession
                            success = await session.recover_strategy_from_state(strategy_type, symbol, saved_state)

                            if success:
                                restored_strategies.append(f"{strategy_type.value}({symbol})")
                                log_info(user_id, f"✅ Стратегия {strategy_type.value} для {symbol} восстановлена из состояния", "BotApplication")
                            else:
                                log_error(user_id, f"❌ Не удалось восстановить стратегию {strategy_type.value} для {symbol}", "BotApplication")

                    except Exception as strategy_error:
                        log_error(user_id, f"Ошибка восстановления стратегии {strategy_type.value} для {symbol}: {strategy_error}", "BotApplication")
                        continue

            # ===================================================================
            # ШАГ 2: Если не восстановили из Redis, запускаем стратегии заново
            # ===================================================================
            if not restored_strategies:
                log_info(user_id, "Сохранённых стратегий в Redis не найдено", "BotApplication")

                if has_active_orders:
                    # Есть активные ордера - восстанавливаем для их символов
                    log_info(user_id, f"Восстанавливаю стратегии для символов с активными ордерами", "BotApplication")

                    symbols_with_orders = set(order['symbol'] for order in active_orders_from_db)
                    log_info(user_id, f"Символы с ордерами: {symbols_with_orders}", "BotApplication")

                    for symbol in symbols_with_orders:
                        try:
                            # Запускаем стратегию для символа с ордерами
                            success = await session.start_strategy(
                                strategy_type=StrategyType.SIGNAL_SCALPER.value,
                                symbol=symbol,
                                analysis_data={'trigger': 'order_recovery', 'recovery_mode': True}
                            )

                            if success:
                                restored_strategies.append(f"SignalScalper({symbol})")
                                log_info(user_id, f"✅ Восстановлена стратегия для {symbol} с активными ордерами", "BotApplication")
                            else:
                                log_error(user_id, f"❌ Не удалось восстановить стратегию для {symbol}", "BotApplication")

                        except Exception as e:
                            log_error(user_id, f"Ошибка восстановления стратегии для {symbol}: {e}", "BotApplication")

                    # Запускаем стратегии для остальных символов из watchlist (без ордеров)
                    symbols_without_orders = [s for s in watchlist_symbols if s not in symbols_with_orders]
                    if symbols_without_orders:
                        log_info(user_id, f"Запускаю стратегии для {len(symbols_without_orders)} символов без ордеров", "BotApplication")
                        for symbol in symbols_without_orders:
                            try:
                                await session.start_strategy(
                                    strategy_type=StrategyType.SIGNAL_SCALPER.value,
                                    symbol=symbol,
                                    analysis_data={'trigger': 'autotrade_restart'}
                                )
                            except Exception as e:
                                log_error(user_id, f"Ошибка запуска стратегии для {symbol}: {e}", "BotApplication")

                    recovery_message = (
                        f"✅ <b>ВОССТАНОВЛЕНИЕ ЗАВЕРШЕНО</b>\n\n"
                        f"🔄 Восстановлено стратегий с активными ордерами: <b>{len(restored_strategies)}</b>\n"
                        f"{'📋 ' + ', '.join(restored_strategies) if restored_strategies else ''}\n\n"
                        f"🚀 Запущено новых стратегий: <b>{len(symbols_without_orders)}</b>\n\n"
                        f"✅ <b>Все ордера под контролем бота!</b>\n"
                        f"📊 Отслеживание активно"
                    )

                else:
                    # Нет активных ордеров, но автоторговля была включена
                    log_info(user_id, "Автоторговля была включена - запускаю обычные стратегии", "BotApplication")

                    for symbol in watchlist_symbols:
                        try:
                            success = await session.start_strategy(
                                strategy_type=StrategyType.SIGNAL_SCALPER.value,
                                symbol=symbol,
                                analysis_data={'trigger': 'autotrade_restart'}
                            )
                            if success:
                                restored_strategies.append(f"SignalScalper({symbol})")
                        except Exception as e:
                            log_error(user_id, f"Ошибка запуска стратегии для {symbol}: {e}", "BotApplication")

                    recovery_message = (
                        f"✅ <b>Восстановление завершено</b>\n\n"
                        f"Запущены стратегии для {len(restored_strategies)} символов\n"
                        f"🔄 Автоторговля возобновлена"
                    )
            else:
                # Отправляем итоговое уведомление о восстановлении из сохранённых состояний
                recovery_message = (
                    f"✅ <b>Восстановление завершено</b>\n\n"
                    f"Восстановлено стратегий: <b>{len(restored_strategies)}</b>\n"
                    f"{'📋 ' + chr(10).join(restored_strategies) if restored_strategies else ''}\n\n"
                    f"🔄 Отслеживание ордеров возобновлено."
                )

            await self.bot.send_message(
                chat_id=user_id,
                text=recovery_message,
                parse_mode="HTML"
            )

        except Exception as e:
            log_error(user_id, f"Критическая ошибка восстановления стратегий: {e}", "BotApplication")
            # Отправляем уведомление об ошибке пользователю
            try:
                await self.bot.send_message(
                    chat_id=user_id,
                    text="❌ <b>Ошибка восстановления стратегий</b>\n\nПроизошла ошибка при восстановлении стратегий после перезагрузки. Запустите стратегии вручную.",
                    parse_mode="HTML"
                )
            except:
                pass

    async def _notify_user_about_server_restart(self, user_id: int):
        """Уведомляет пользователя о перезагрузке сервера и начале восстановления"""
        try:
            restart_time = datetime.now().strftime("%H:%M:%S")
            message = (
                f"🔄 <b>Сервер был перезагружен</b>\n\n"
                f"⏰ Время: <b>{restart_time} МСК</b>\n"
                f"🔍 Проверяю активные стратегии...\n"
                f"📡 Восстанавливаю отслеживание ордеров...\n\n"
                f"Пожалуйста, подождите..."
            )

            await self.bot.send_message(
                chat_id=user_id,
                text=message,
                parse_mode="HTML"
            )

        except Exception as e:
            log_error(user_id, f"Ошибка отправки уведомления о перезагрузке: {e}", "BotApplication")
