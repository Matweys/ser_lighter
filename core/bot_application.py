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

from core.logger import log_info, log_error
from core.events import (
    event_bus, EventType, BaseEvent, UserSessionStartEvent, UserSessionStopEvent,
    UserSettingsChangedEvent, RiskLimitExceededEvent
)
from cache.redis_manager import redis_manager
from core.user_session import UserSession
from core.websocket_manager import GlobalWebSocketManager
from core.default_configs import DefaultConfigs


class BotApplication:
    """
    Главный класс приложения, управляющий жизненным циклом пользовательских торговых сессий
    
    Принципы работы:
    - Каждый пользователь получает изолированную UserSession
    - Все настройки загружаются динамически из Redis
    - Событийно-ориентированная архитектура через EventBus
    - Автоматическое управление ресурсами и очистка
    - Глобальный WebSocket менеджер для оптимизации соединений
    - Автоматическое восстановление после перезапуска
    """
    
    def __init__(self):
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
            await event_bus.start()

            # Подписка на системные события
            event_bus.subscribe(EventType.USER_SESSION_STARTED, self._handle_session_start)
            event_bus.subscribe(EventType.USER_SESSION_STOPPED, self._handle_session_stop)
            event_bus.subscribe(EventType.USER_SETTINGS_CHANGED, self._handle_settings_changed)
            event_bus.subscribe(EventType.RISK_LIMIT_EXCEEDED, self._handle_risk_limit)
            
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
                
                # Отписка от событий
                event_bus.unsubscribe(EventType.USER_SESSION_START, self._handle_session_start)
                event_bus.unsubscribe(EventType.USER_SESSION_STOP, self._handle_session_stop)
                event_bus.unsubscribe(EventType.USER_SETTINGS_CHANGED, self._handle_settings_changed)
                event_bus.unsubscribe(EventType.RISK_LIMIT_EXCEEDED, self._handle_risk_limit)
                
                # Остановка EventBus
                await event_bus.stop()
                
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
                session = UserSession(user_id)
                
                # Запуск сессии
                if await session.start():
                    self.active_sessions[user_id] = session
                    
                    # Обновление статистики
                    self.app_stats["total_users"] += 1
                    self.app_stats["active_sessions"] = len(self.active_sessions)
                    
                    # Публикация события
                    event = UserSessionStartEvent(user_id=user_id, timestamp=datetime.now())
                    await event_bus.publish(event)
                    
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
                event = UserSessionStopEvent(user_id=user_id, reason=reason, timestamp=datetime.now())
                await event_bus.publish(event)
                
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
            # Инициализация глобального WebSocket менеджера
            self.global_websocket_manager = GlobalWebSocketManager()
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
            # Получение списка активных сессий из Redis
            session_keys = await redis_manager.keys("user:*:session")
            
            restored_count = 0
            for key in session_keys:
                try:
                    # Извлечение user_id из ключа
                    user_id = int(key.split(":")[1])
                    
                    # Проверка конфигурации пользователя
                    global_config = await redis_manager.get_json(f"user:{user_id}:global_config")
                    if not global_config:
                        continue
                        
                    # Создание и запуск сессии
                    session = UserSession(user_id)
                    if await session.start():
                        self.active_sessions[user_id] = session
                        restored_count += 1
                        
                        log_info(user_id, "Сессия восстановлена", module_name=__name__)
                        
                except Exception as e:
                    log_error(0, f"Ошибка восстановления сессии {key}: {e}", module_name=__name__)
                    continue
                    
            # Обновление статистики
            self.app_stats["active_sessions"] = len(self.active_sessions)
            if restored_count > 0:
                self.app_stats["system_restarts"] += 1
                
            log_info(
                0,
                f"Восстановлено {restored_count} пользовательских сессий",
                module_name=__name__
            )
            
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
            # Проверка существования конфигураций
            global_config = await redis_manager.get_json(f"user:{user_id}:global_config")
            
            if not global_config:
                # Создание конфигураций по умолчанию
                default_configs = DefaultConfigs.get_all_default_configs()
                
                # Сохранение глобальной конфигурации
                await redis_manager.set_json(
                    f"user:{user_id}:global_config",
                    default_configs["global_config"]
                )
                
                # Сохранение конфигураций стратегий
                for strategy_type, strategy_config in default_configs["strategy_configs"].items():
                    await redis_manager.set_json(
                        f"user:{user_id}:strategy_config:{strategy_type}",
                        strategy_config
                    )
                    
                # Сохранение конфигураций компонентов
                for component_type, component_config in default_configs["component_configs"].items():
                    await redis_manager.set_json(
                        f"user:{user_id}:component_config:{component_type}",
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
            
            # Сохранение статистики в Redis
            await redis_manager.set_json("app:stats", self.app_stats)
            
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
            
            await redis_manager.set_json("app:final_stats", final_stats)
            
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

