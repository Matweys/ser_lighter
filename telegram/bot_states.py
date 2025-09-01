import asyncio
from typing import Dict, Set, Optional, List, Any, Union
from datetime import datetime
from datetime import timezone
from enum import Enum
from dataclasses import dataclass
import json

from core.logger import log_info, log_error, log_warning
from core.events import EventBus, EventType, UserSessionStartedEvent, UserSessionStoppedEvent, SystemStatusEvent
from core.enums import SessionStatus, StrategyStatus, SystemConstants
from cache.redis_manager import redis_manager
from core.user_session import UserSession
from core.default_configs import DefaultConfigs




@dataclass
class SessionInfo:
    """Информация о пользовательской сессии"""
    user_id: int
    status: SessionStatus
    started_at: datetime
    last_activity: datetime
    active_strategies: List[str]
    total_trades: int = 0
    total_profit: float = 0.0
    error_count: int = 0
    last_error: Optional[str] = None


class BotStatesManager:
    """
    Профессиональный менеджер состояний бота для управления пользовательскими сессиями
    """

    def __init__(self, event_bus: EventBus):
        self.event_bus = event_bus
        self.user_sessions: Dict[int, UserSession] = {}
        self.session_info: Dict[int, SessionInfo] = {}
        self.is_running = False
        self.monitoring_task: Optional[asyncio.Task] = None
        self.cleanup_task: Optional[asyncio.Task] = None

        # Статистика системы
        self.system_stats: Dict[str, Union[int, float, datetime, None]] = {
            'total_sessions': 0,
            'active_sessions': 0,
            'total_strategies': 0,
            'total_trades': 0,
            'total_profit': 0.0,
            'uptime_start': None,
            'last_cleanup': None
        }

        # Подписка на события
        self._subscribe_to_events()

    def _subscribe_to_events(self):
        """Подписка на системные события"""
        self.event_bus.subscribe(
            EventType.USER_SESSION_START_REQUESTED,
            self._handle_session_start_request
        )

        self.event_bus.subscribe(
            EventType.USER_SESSION_STOP_REQUESTED,
            self._handle_session_stop_request
        )

        self.event_bus.subscribe(
            EventType.USER_SETTINGS_CHANGED,
            self._handle_settings_changed
        )

    async def start(self):
        """Запуск менеджера состояний"""
        if self.is_running:
            log_warning(0, "Менеджер состояний уже запущен", module_name='bot_states')
            return

        try:
            log_info(0, "Запуск менеджера состояний бота", module_name='bot_states')
            self.is_running = True
            self.system_stats['uptime_start'] = datetime.now(timezone.utc)

            # Восстанавливаем активные сессии
            await self._restore_active_sessions()

            # Запускаем задачи мониторинга
            self.monitoring_task = asyncio.create_task(self._monitoring_loop())
            self.cleanup_task = asyncio.create_task(self._cleanup_loop())

            # Публикуем событие запуска системы
            await self.event_bus.publish(SystemStatusEvent(
                user_id=0,
                status="STARTED",
                message="BotStatesManager запущен",
                metadata={
                    'active_sessions': len(self.user_sessions),
                    'timestamp': datetime.now(timezone.utc).isoformat()
                }
            ))

            log_info(0, f"Менеджер состояний запущен. Активных сессий: {len(self.user_sessions)}", module_name='bot_states')

        except Exception as e:
            log_error(0, f"Ошибка запуска менеджера состояний: {e}", module_name='bot_states')
            self.is_running = False
            raise

    async def stop(self):
        """Остановка менеджера состояний"""
        if not self.is_running:
            return

        try:
            log_info(0, "Остановка менеджера состояний бота", module_name='bot_states')
            self.is_running = False

            # Останавливаем все активные сессии
            stop_tasks = []
            for user_id, session in self.user_sessions.items():
                stop_tasks.append(self._stop_user_session(user_id))

            if stop_tasks:
                await asyncio.gather(*stop_tasks, return_exceptions=True)

            # Отменяем задачи мониторинга
            if self.monitoring_task and not self.monitoring_task.done():
                self.monitoring_task.cancel()
                try:
                    await self.monitoring_task
                except asyncio.CancelledError:
                    pass

            if self.cleanup_task and not self.cleanup_task.done():
                self.cleanup_task.cancel()
                try:
                    await self.cleanup_task
                except asyncio.CancelledError:
                    pass

            # Очищаем данные
            self.user_sessions.clear()
            self.session_info.clear()

            # Публикуем событие остановки системы
            await self.event_bus.publish(SystemStatusEvent(
                user_id=0,
                status="STARTED",
                message="BotStatesManager запущен",
                metadata={
                    'shutdown_time': datetime.now(timezone.utc).isoformat()
                }
            ))

            log_info(0, "Менеджер состояний остановлен", module_name='bot_states')

        except Exception as e:
            log_error(0, f"Ошибка остановки менеджера состояний: {e}", module_name='bot_states')

    async def start_user_session(self, user_id: int, force_restart: bool = False) -> bool:
        """
        Запуск пользовательской сессии

        Args:
            user_id: ID пользователя
            force_restart: Принудительный перезапуск если сессия уже активна

        Returns:
            bool: True если сессия успешно запущена
        """
        try:
            # Проверяем существующую сессию
            if user_id in self.user_sessions:
                if not force_restart:
                    log_info(user_id, "Сессия пользователя уже активна", module_name='bot_states')
                    return True
                else:
                    log_info(user_id, "Принудительный перезапуск сессии", module_name='bot_states')
                    await self._stop_user_session(user_id)

            # Проверяем конфигурацию пользователя
            user_config = await redis_manager.get_user_config(user_id)
            if not user_config:
                log_info(user_id, "Создание конфигурации по умолчанию для пользователя", module_name='bot_states')
                await DefaultConfigs.create_default_user_config(user_id)
                user_config = await redis_manager.get_user_config(user_id)

            if not user_config:
                log_error(user_id, "Не удалось получить конфигурацию для пользователя", module_name='bot_states')
                return False

            # Создаем и запускаем пользовательскую сессию
            user_session = UserSession(user_id)

            if await user_session.start():
                self.user_sessions[user_id] = user_session

                # Создаем информацию о сессии
                self.session_info[user_id] = SessionInfo(
                    user_id=user_id,
                    status=SessionStatus.ACTIVE,
                    started_at=datetime.now(timezone.utc),
                    last_activity=datetime.now(timezone.utc),
                    active_strategies=[]
                )

                # Обновляем статистику
                self.system_stats['total_sessions'] += 1
                self.system_stats['active_sessions'] = len(self.user_sessions)

                # Публикуем событие запуска сессии
                await self.event_bus.publish(UserSessionStartedEvent(
                    user_id=user_id,
                    timestamp=datetime.now(timezone.utc)
                ))

                log_info(user_id, "Пользовательская сессия успешно запущена", module_name='bot_states')
                return True
            else:
                log_error(user_id, "Ошибка запуска пользовательской сессии", module_name='bot_states')
                return False

        except Exception as e:
            log_error(user_id, f"Ошибка запуска сессии: {e}", module_name='bot_states')
            return False

    async def stop_user_session(self, user_id: int) -> bool:
        """
        Остановка пользовательской сессии

        Args:
            user_id: ID пользователя

        Returns:
            bool: True если сессия успешно остановлена
        """
        return await self._stop_user_session(user_id)

    async def _stop_user_session(self, user_id: int) -> bool:
        """Внутренний метод остановки сессии"""
        try:
            if user_id not in self.user_sessions:
                log_info(user_id, "Активная сессия не найдена", module_name='bot_states')
                return True

            session = self.user_sessions[user_id]

            # Останавливаем сессию
            if await session.stop():
                # Удаляем из активных сессий
                del self.user_sessions[user_id]

                # Обновляем информацию о сессии
                if user_id in self.session_info:
                    self.session_info[user_id].status = SessionStatus.STOPPED
                    del self.session_info[user_id]

                # Обновляем статистику
                self.system_stats['active_sessions'] = len(self.user_sessions)

                # Публикуем событие остановки сессии
                await self.event_bus.publish(UserSessionStoppedEvent(
                    user_id=user_id,
                    reason="USER_REQUEST",
                    timestamp=datetime.now(timezone.utc)
                ))

                log_info(user_id, "Пользовательская сессия остановлена", module_name='bot_states')
                return True
            else:
                log_error(user_id, "Ошибка остановки сессии", module_name='bot_states')
                return False

        except Exception as e:
            log_error(user_id, f"Ошибка остановки сессии: {e}", module_name='bot_states')
            return False

    async def get_session_status(self, user_id: int) -> Optional[Dict[str, Any]]:
        """
        Получение статуса пользовательской сессии

        Args:
            user_id: ID пользователя

        Returns:
            Optional[Dict]: Информация о сессии или None
        """
        try:
            if user_id not in self.user_sessions:
                return None

            session = self.user_sessions[user_id]
            session_info = self.session_info.get(user_id)

            if not session_info:
                return None

            # Получаем статистику сессии
            session_stats = await session.get_statistics()

            return {
                'user_id': user_id,
                'status': session_info.status.value,
                'started_at': session_info.started_at.isoformat(),
                'last_activity': session_info.last_activity.isoformat(),
                'active_strategies': session_info.active_strategies,
                'total_trades': session_info.total_trades,
                'total_profit': session_info.total_profit,
                'error_count': session_info.error_count,
                'last_error': session_info.last_error,
                'session_stats': session_stats,
                'is_healthy': await session.is_healthy()
            }

        except Exception as e:
            log_error(user_id, f"Ошибка получения статуса сессии: {e}", module_name='bot_states')
            return None

    async def get_system_status(self) -> Dict[str, Any]:
        """
        Получение общего статуса системы

        Returns:
            Dict: Информация о состоянии системы
        """
        try:
            uptime = None
            if self.system_stats['uptime_start']:
                uptime = (datetime.now(timezone.utc) - self.system_stats['uptime_start']).total_seconds()

            return {
                'is_running': self.is_running,
                'uptime_seconds': uptime,
                'active_sessions': len(self.user_sessions),
                'total_sessions': self.system_stats['total_sessions'],
                'total_strategies': self.system_stats['total_strategies'],
                'total_trades': self.system_stats['total_trades'],
                'total_profit': self.system_stats['total_profit'],
                'last_cleanup': self.system_stats['last_cleanup'].isoformat() if self.system_stats[
                    'last_cleanup'] else None,
                'memory_usage': await self._get_memory_usage(),
                'active_users': list(self.user_sessions.keys())
            }

        except Exception as e:
            log_error(0, f"Ошибка получения статуса системы: {e}", module_name='bot_states')
            return {'error': str(e)}

    async def _restore_active_sessions(self):
        """Восстановление активных сессий после перезапуска"""
        try:
            log_info(0, "Восстановление активных пользовательских сессий", module_name='bot_states')

            # Получаем список активных пользователей из Redis
            active_users = await redis_manager.get_active_users()

            if not active_users:
                log_info(0, "Нет активных пользователей для восстановления", module_name='bot_states')
                return

            log_info(0, f"Восстановление {len(active_users)} активных сессий", module_name='bot_states')

            # Запускаем сессии параллельно
            restore_tasks = []
            for user_id in active_users:
                restore_tasks.append(self.start_user_session(user_id))

            results = await asyncio.gather(*restore_tasks, return_exceptions=True)

            # Подсчитываем результаты
            successful = sum(1 for result in results if result is True)
            failed = len(results) - successful

            log_info(0, f"Восстановление завершено: {successful} успешно, {failed} с ошибками", module_name='bot_states')

        except Exception as e:
            log_error(0, f"Ошибка восстановления активных сессий: {e}", module_name='bot_states')

    async def _monitoring_loop(self):
        """Цикл мониторинга состояния сессий"""
        log_info(0, "Запуск цикла мониторинга сессий", module_name='bot_states')

        while self.is_running:
            try:
                # Проверяем состояние всех сессий
                for user_id, session in list(self.user_sessions.items()):
                    try:
                        # Проверяем активность сессии
                        if not await session.is_healthy():
                            log_warning(user_id, "Сессия неактивна, перезапускаем", module_name='bot_states')
                            await self.start_user_session(user_id, force_restart=True)
                        else:
                            # Обновляем время последней активности
                            if user_id in self.session_info:
                                self.session_info[user_id].last_activity = datetime.now(timezone.utc)

                    except Exception as e:
                        log_error(user_id, f"Ошибка мониторинга сессии: {e}", module_name='bot_states')

                        # Увеличиваем счетчик ошибок
                        if user_id in self.session_info:
                            self.session_info[user_id].error_count += 1
                            self.session_info[user_id].last_error = str(e)

                            # Если слишком много ошибок, останавливаем сессию
                            if self.session_info[user_id].error_count > SystemConstants.MAX_SESSION_ERRORS:
                                log_error(user_id, "Слишком много ошибок в сессии, останавливаем", module_name='bot_states')
                                await self._stop_user_session(user_id)

                # Пауза между проверками
                await asyncio.sleep(SystemConstants.MONITORING_INTERVAL)

            except asyncio.CancelledError:
                log_info(0, "Цикл мониторинга отменен", module_name='bot_states')
                break
            except Exception as e:
                log_error(0, f"Критическая ошибка в цикле мониторинга: {e}", module_name='bot_states')
                await asyncio.sleep(60)  # Увеличенная пауза при ошибке

    async def _cleanup_loop(self):
        """Цикл очистки неактивных данных"""
        log_info(0, "Запуск цикла очистки", module_name='bot_states')

        while self.is_running:
            try:
                # Очистка каждые 30 минут
                await asyncio.sleep(1800)

                if not self.is_running:
                    break

                log_info(0, "Выполнение очистки неактивных данных", module_name='bot_states')

                # Очищаем старые данные в Redis
                await redis_manager.cleanup_expired_data()

                # Обновляем время последней очистки
                self.system_stats['last_cleanup'] = datetime.now(timezone.utc)

                log_info(0, "Очистка завершена", module_name='bot_states')

            except asyncio.CancelledError:
                log_info(0, "Цикл очистки отменен", module_name='bot_states')
                break
            except Exception as e:
                log_error(0, f"Ошибка в цикле очистки: {e}", module_name='bot_states')

    async def _handle_session_start_request(self, event):
        """Обработчик запроса на запуск сессии"""
        user_id = event.user_id
        await self.start_user_session(user_id)

    async def _handle_session_stop_request(self, event):
        """Обработчик запроса на остановку сессии"""
        user_id = event.user_id
        await self.stop_user_session(user_id)

    async def _handle_settings_changed(self, event):
        """Обработчик изменения настроек пользователя"""
        user_id = event.user_id

        # Если сессия активна, перезапускаем её для применения новых настроек
        if user_id in self.user_sessions:
            log_info(user_id, "Перезапуск сессии из-за изменения настроек", module_name='bot_states')
            await self.start_user_session(user_id, force_restart=True)

    @staticmethod
    async def _get_memory_usage() -> Dict[str, Any]:
        """Получение информации об использовании памяти"""
        try:
            import psutil
            process = psutil.Process()
            memory_info = process.memory_info()

            return {
                'rss_mb': round(memory_info.rss / 1024 / 1024, 2),
                'vms_mb': round(memory_info.vms / 1024 / 1024, 2),
                'percent': round(process.memory_percent(), 2)
            }
        except ImportError:
            return {'error': 'psutil not available'}
        except Exception as e:
            return {'error': str(e)}

    def get_active_sessions_count(self) -> int:
        """Получение количества активных сессий"""
        return len(self.user_sessions)

    def get_active_users(self) -> Set[int]:
        """Получение множества ID активных пользователей"""
        return set(self.user_sessions.keys())

    async def force_sync_sessions(self):
        """Принудительная синхронизация сессий с Redis"""
        try:
            log_info(0, "Принудительная синхронизация сессий", module_name='bot_states')

            # Получаем актуальный список активных пользователей
            redis_active_users = await redis_manager.get_active_users()
            local_active_users = set(self.user_sessions.keys())

            # Запускаем новые сессии
            new_users = redis_active_users - local_active_users
            for user_id in new_users:
                await self.start_user_session(user_id)

            # Останавливаем лишние сессии
            removed_users = local_active_users - redis_active_users
            for user_id in removed_users:
                await self.stop_user_session(user_id)

            log_info(0, f"Синхронизация завершена: +{len(new_users)} -{len(removed_users)}", module_name='bot_states')

        except Exception as e:
            log_error(0, f"Ошибка принудительной синхронизации: {e}", module_name='bot_states')


# Функция для создания менеджера состояний
def create_bot_states_manager(event_bus: EventBus) -> BotStatesManager:
    """Создание экземпляра менеджера состояний"""
    return BotStatesManager(event_bus)