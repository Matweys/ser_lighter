# core/user_session.py
"""
Пользовательская торговая сессия
Управляет всеми компонентами торговли для одного пользователя
Объединенная версия с полной функциональностью
"""
import asyncio
from typing import Dict, Optional, Any, List, Set
from decimal import Decimal, getcontext
from datetime import datetime
from core.logger import log_info, log_error, log_warning
from core.events import (
    event_bus, EventType, BaseEvent, StrategyStartEvent, StrategyStopEvent,
    SignalEvent, OrderEvent, RiskLimitExceededEvent
)
from cache.redis_manager import redis_manager
from analysis.meta_strategist import MetaStrategist
from analysis.risk_manager import RiskManager
from api.bybit_api import BybitAPI
# Импорт стратегий
from strategies.bidirectional_grid_strategy import BidirectionalGridStrategy
from strategies.impulse_trailing_strategy import ImpulseTrailingStrategy
from strategies.grid_scalping_strategy import GridScalpingStrategy
from strategies.base_strategy import BaseStrategy


# Установка точности Decimal
getcontext().prec = 28

# Маппинг типов стратегий на классы
STRATEGY_CLASSES = {
    "bidirectional_grid": BidirectionalGridStrategy,
    "impulse_trailing": ImpulseTrailingStrategy,
    "grid_scalping": GridScalpingStrategy
}


class UserSession:
    """
    Пользовательская торговая сессия
    
    Управляет всеми компонентами торговли для одного пользователя:
    - MetaStrategist (анализ и принятие решений)
    - RiskManager (управление рисками)
    - DataFeedHandler (получение данных)
    - Активные стратегии
    
    Принципы работы:
    - Полная изоляция между пользователями
    - Динамическая загрузка настроек из Redis
    - Событийно-ориентированная архитектура
    - Автоматическое управление жизненным циклом стратегий
    """
    
    def __init__(self, user_id: int):
        self.user_id = user_id
        self.running = False

        # API клиент сессии
        self.api: Optional[BybitAPI] = None
        # Основные компоненты
        self.meta_strategist: Optional[MetaStrategist] = None
        self.risk_manager: Optional[RiskManager] = None
        self.data_feed_handler: Optional[DataFeedHandler] = None
        
        # Управление стратегиями
        self.active_strategies: Dict[str, BaseStrategy] = {}
        self.strategy_tasks: Dict[str, asyncio.Task] = {}
        
        # Статистика сессии
        self.session_stats = {
            "start_time": datetime.now(),
            "strategies_launched": 0,
            "strategies_stopped": 0,
            "total_signals": 0,
            "risk_violations": 0,
            "successful_trades": 0,
            "failed_trades": 0,
            "total_pnl": Decimal("0")
        }
        
        # Блокировка для thread-safety
        self.lock = asyncio.Lock()
        
        # Задачи компонентов
        self._component_tasks: List[asyncio.Task] = []
        
    async def start(self) -> bool:
        """
        Запуск пользовательской сессии
        
        Returns:
            bool: True если сессия запущена успешно
        """
        if self.running:
            return True
            
        log_info(self.user_id, "Запуск пользовательской сессии...", module_name=__name__)
        
        try:
            # Загрузка конфигураций пользователя
            global_config = await redis_manager.get_json(f"user:{self.user_id}:global_config")
            if not global_config:
                log_error(self.user_id, "Конфигурация пользователя не найдена", module_name=__name__)
                return False
                
            # Инициализация компонентов
            await self._initialize_components()
            
            # Подписка на события
            await self._subscribe_to_events()
            
            # Запуск компонентов
            await self._start_components()
            
            # Запуск стратегий (если включены)
            await self._start_enabled_strategies()
            
            # Сохранение состояния сессии в Redis
            await self._save_session_state()
            
            self.running = True
            
            log_info(self.user_id, "Пользовательская сессия запущена", module_name=__name__)
            return True
            
        except Exception as e:
            log_error(self.user_id, f"Ошибка запуска сессии: {e}", module_name=__name__)
            await self.stop("Startup error")
            return False
            
    async def stop(self, reason: str = "Manual stop"):
        """
        Остановка пользовательской сессии
        
        Args:
            reason: Причина остановки
        """
        if not self.running:
            return
            
        log_info(self.user_id, f"Остановка пользовательской сессии: {reason}", module_name=__name__)
        
        try:
            async with self.lock:
                self.running = False
                
                # Остановка всех стратегий
                await self._stop_all_strategies("Session stopping")
                
                # Остановка компонентов
                await self._stop_components()
                
                # Отписка от событий
                await self._unsubscribe_from_events()
                
                # Сохранение финальной статистики
                await self._save_final_stats()
                
                # Удаление состояния сессии из Redis
                await redis_manager.delete(f"user:{self.user_id}:session")
                
            log_info(self.user_id, "Пользовательская сессия остановлена", module_name=__name__)
            
        except Exception as e:
            log_error(self.user_id, f"Ошибка остановки сессии: {e}", module_name=__name__)
            
    async def get_status(self) -> Dict[str, Any]:
        """
        Получение статуса сессии
        
        Returns:
            Dict: Статус сессии
        """
        try:
            # Статус стратегий
            strategies_status = {}
            for strategy_id, strategy in self.active_strategies.items():
                strategies_status[strategy_id] = await strategy.get_status()
                
            # Статус компонентов
            components_status = {
                "meta_strategist": self.meta_strategist.running if self.meta_strategist else False,
                "risk_manager": self.risk_manager.running if self.risk_manager else False,
                "data_feed_handler": self.data_feed_handler.running if self.data_feed_handler else False
            }
            
            return {
                "user_id": self.user_id,
                "running": self.running,
                "session_stats": {
                    "start_time": self.session_stats["start_time"].isoformat(),
                    "runtime": str(datetime.now() - self.session_stats["start_time"]),
                    "strategies_launched": self.session_stats["strategies_launched"],
                    "strategies_stopped": self.session_stats["strategies_stopped"],
                    "total_signals": self.session_stats["total_signals"],
                    "risk_violations": self.session_stats["risk_violations"],
                    "successful_trades": self.session_stats["successful_trades"],
                    "failed_trades": self.session_stats["failed_trades"],
                    "total_pnl": str(self.session_stats["total_pnl"])
                },
                "components_status": components_status,
                "active_strategies": strategies_status
            }
            
        except Exception as e:
            log_error(self.user_id, f"Ошибка получения статуса сессии: {e}", module_name=__name__)
            return {"user_id": self.user_id, "running": self.running, "error": str(e)}
            
    async def start_strategy(self, strategy_type: str, symbol: str, config: Optional[Dict] = None) -> bool:
        """
        Запуск стратегии
        
        Args:
            strategy_type: Тип стратегии
            symbol: Торговый символ
            config: Дополнительная конфигурация
            
        Returns:
            bool: True если стратегия запущена успешно
        """
        try:
            strategy_id = f"{strategy_type}_{symbol}"
            
            if strategy_id in self.active_strategies:
                log_warning(self.user_id, f"Стратегия {strategy_id} уже запущена", module_name=__name__)
                return True
                
            # Проверка лимитов
            if not await self.risk_manager.can_start_new_strategy():
                log_warning(self.user_id, "Превышен лимит активных стратегий", module_name=__name__)
                return False
                
            # Создание стратегии
            if strategy_type not in STRATEGY_CLASSES:
                log_error(self.user_id, f"Неизвестный тип стратегии: {strategy_type}", module_name=__name__)
                return False

            strategy_class = STRATEGY_CLASSES[strategy_type]
            # Передаем self.api как четвертый аргумент
            strategy = strategy_class(self.user_id, symbol, config, self.api)
            
            # Запуск стратегии
            if await strategy.start():
                self.active_strategies[strategy_id] = strategy
                
                # Создание задачи для стратегии
                task = asyncio.create_task(strategy.run())
                self.strategy_tasks[strategy_id] = task
                
                # Обновление статистики
                self.session_stats["strategies_launched"] += 1
                
                # Публикация события
                event = StrategyStartEvent(
                    user_id=self.user_id,
                    strategy_type=strategy_type,
                    symbol=symbol,
                    strategy_id=strategy.strategy_id  # <-- Добавлен обязательный параметр
                )
                await event_bus.publish(event)
                log_info(self.user_id, f"Стратегия {strategy_id} запущена", module_name=__name__)
                return True
            else:
                log_error(self.user_id, f"Не удалось запустить стратегию {strategy_id}", module_name=__name__)
                return False
                
        except Exception as e:
            log_error(self.user_id, f"Ошибка запуска стратегии {strategy_type}: {e}", module_name=__name__)
            return False
            
    async def stop_strategy(self, strategy_id: str, reason: str = "Manual stop") -> bool:
        """
        Остановка стратегии
        
        Args:
            strategy_id: ID стратегии
            reason: Причина остановки
            
        Returns:
            bool: True если стратегия остановлена успешно
        """
        try:
            if strategy_id not in self.active_strategies:
                log_warning(self.user_id, f"Стратегия {strategy_id} не найдена", module_name=__name__)
                return True
                
            strategy = self.active_strategies[strategy_id]
            
            # Остановка стратегии
            await strategy.stop(reason)
            
            # Остановка задачи
            if strategy_id in self.strategy_tasks:
                task = self.strategy_tasks[strategy_id]
                if not task.done():
                    task.cancel()
                    try:
                        await task
                    except asyncio.CancelledError:
                        pass
                del self.strategy_tasks[strategy_id]
                
            # Удаление из активных стратегий
            del self.active_strategies[strategy_id]
            
            # Обновление статистики
            self.session_stats["strategies_stopped"] += 1
            
            # Публикация события
            event = StrategyStopEvent(
                user_id=self.user_id,
                strategy_id=strategy_id,
                reason=reason,
                symbol=strategy.symbol,
                strategy_type=strategy.strategy_type.value
            )
            await event_bus.publish(event)
            log_info(self.user_id, f"Стратегия {strategy_id} остановлена: {reason}", module_name=__name__)
            return True
        except Exception as e:
            log_error(self.user_id, f"Ошибка остановки стратегии {strategy_id}: {e}", module_name=__name__)
            return False

    async def _initialize_components(self):
        """Инициализация компонентов сессии"""
        try:
            # Получаем ключи и создаем API клиент
            api_keys = await redis_manager.get_user_api_keys(self.user_id)
            if not api_keys:
                raise ValueError("API ключи для пользователя не найдены в Redis")

            self.api = BybitAPI(
                user_id=self.user_id,
                api_key=api_keys.get("api_key"),
                api_secret=api_keys.get("api_secret")
            )

            # Инициализация компонентов с передачей API клиента
            self.meta_strategist = MetaStrategist(self.api)  # <-- self.user_id уже есть в self.api
            self.risk_manager = RiskManager(self.user_id, self.api)

            log_info(self.user_id, "Компоненты сессии инициализированы", module_name=__name__)

        except Exception as e:  # <-- Добавлен недостающий блок
            log_error(self.user_id, f"Ошибка инициализации компонентов: {e}", module_name=__name__)
            raise
            
    async def _start_components(self):
        """Запуск компонентов сессии"""
        try:
            # Запуск RiskManager
            if not await self.risk_manager.start():
                raise Exception("Не удалось запустить RiskManager")
                
            # Запуск DataFeedHandler
            if not await self.data_feed_handler.start():
                raise Exception("Не удалось запустить DataFeedHandler")
                
            # Запуск MetaStrategist
            if not await self.meta_strategist.start():
                raise Exception("Не удалось запустить MetaStrategist")
                
            # Создание задач для компонентов
            self._component_tasks = [
                asyncio.create_task(self.meta_strategist.run()),
                asyncio.create_task(self.risk_manager.run()),
                asyncio.create_task(self.data_feed_handler.run())
            ]
            
            log_info(self.user_id, "Компоненты сессии запущены", module_name=__name__)
            
        except Exception as e:
            log_error(self.user_id, f"Ошибка запуска компонентов: {e}", module_name=__name__)
            raise
            
    async def _stop_components(self):
        """Остановка компонентов сессии"""
        try:
            # Остановка задач компонентов
            for task in self._component_tasks:
                if not task.done():
                    task.cancel()
                    try:
                        await task
                    except asyncio.CancelledError:
                        pass
                        
            self._component_tasks.clear()
            
            # Остановка компонентов
            if self.meta_strategist:
                await self.meta_strategist.stop()
                
            if self.risk_manager:
                await self.risk_manager.stop()
                
            if self.data_feed_handler:
                await self.data_feed_handler.stop()
                
            log_info(self.user_id, "Компоненты сессии остановлены", module_name=__name__)
            
        except Exception as e:
            log_error(self.user_id, f"Ошибка остановки компонентов: {e}", module_name=__name__)
            
    async def _start_enabled_strategies(self):
        """Запуск включенных стратегий"""
        try:
            global_config = await redis_manager.get_json(f"user:{self.user_id}:global_config")
            if not global_config:
                return
                
            enabled_strategies = global_config.get("enabled_strategies", [])
            watchlist_symbols = global_config.get("watchlist_symbols", [])
            
            for strategy_type in enabled_strategies:
                for symbol in watchlist_symbols:
                    await self.start_strategy(strategy_type, symbol)
                    
        except Exception as e:
            log_error(self.user_id, f"Ошибка запуска включенных стратегий: {e}", module_name=__name__)
            
    async def _stop_all_strategies(self, reason: str):
        """Остановка всех стратегий"""
        try:
            strategy_ids = list(self.active_strategies.keys())
            
            for strategy_id in strategy_ids:
                await self.stop_strategy(strategy_id, reason)
                
            log_info(self.user_id, f"Остановлено {len(strategy_ids)} стратегий", module_name=__name__)
            
        except Exception as e:
            log_error(self.user_id, f"Ошибка остановки всех стратегий: {e}", module_name=__name__)
            
    async def _subscribe_to_events(self):
        """Подписка на события"""
        try:
            # Подписка на сигналы
            event_bus.subscribe(EventType.SIGNAL_GENERATED, self._handle_signal_event)
            
            # Подписка на торговые события
            event_bus.subscribe(EventType.ORDER_FILLED, self._handle_order_event)
            event_bus.subscribe(EventType.ORDER_CANCELLED, self._handle_order_event)
            
            # Подписка на события риска
            event_bus.subscribe(EventType.RISK_LIMIT_EXCEEDED, self._handle_risk_event)
            
        except Exception as e:
            log_error(self.user_id, f"Ошибка подписки на события: {e}", module_name=__name__)
            
    async def _unsubscribe_from_events(self):
        """Отписка от событий"""
        try:
            event_bus.unsubscribe(EventType.SIGNAL_GENERATED, self._handle_signal_event)
            event_bus.unsubscribe(EventType.ORDER_FILLED, self._handle_order_event)
            event_bus.unsubscribe(EventType.ORDER_CANCELLED, self._handle_order_event)
            event_bus.unsubscribe(EventType.RISK_LIMIT_EXCEEDED, self._handle_risk_event)
            
        except Exception as e:
            log_error(self.user_id, f"Ошибка отписки от событий: {e}", module_name=__name__)
            
    async def _save_session_state(self):
        """Сохранение состояния сессии в Redis"""
        try:
            session_state = {
                "user_id": self.user_id,
                "running": self.running,
                "start_time": self.session_stats["start_time"].isoformat(),
                "active_strategies": list(self.active_strategies.keys())
            }
            
            await redis_manager.set_json(f"user:{self.user_id}:session", session_state)
            
        except Exception as e:
            log_error(self.user_id, f"Ошибка сохранения состояния сессии: {e}", module_name=__name__)
            
    async def _save_final_stats(self):
        """Сохранение финальной статистики"""
        try:
            final_stats = {
                **self.session_stats,
                "end_time": datetime.now().isoformat(),
                "total_runtime": str(datetime.now() - self.session_stats["start_time"])
            }
            
            await redis_manager.set_json(f"user:{self.user_id}:session_stats", final_stats)
            
        except Exception as e:
            log_error(self.user_id, f"Ошибка сохранения финальной статистики: {e}", module_name=__name__)
            
    # Обработчики событий
    async def _handle_signal_event(self, event: SignalEvent):
        """Обработчик событий сигналов"""
        if event.user_id != self.user_id:
            return
            
        self.session_stats["total_signals"] += 1
        log_info(self.user_id, f"Получен сигнал: {event.signal_type}", module_name=__name__)
        
    async def _handle_order_event(self, event: OrderEvent):
        """Обработчик торговых событий"""
        if event.user_id != self.user_id:
            return
            
        if event.event_type == EventType.ORDER_FILLED:
            # Определение успешности сделки по PnL
            pnl = event.order_data.get("pnl", Decimal("0"))
            if pnl > 0:
                self.session_stats["successful_trades"] += 1
            elif pnl < 0:
                self.session_stats["failed_trades"] += 1
                
            self.session_stats["total_pnl"] += pnl
            
        log_info(self.user_id, f"Торговое событие: {event.event_type}", module_name=__name__)
        
    async def _handle_risk_event(self, event: RiskLimitExceededEvent):
        """Обработчик событий риска"""
        if event.user_id != self.user_id:
            return
            
        self.session_stats["risk_violations"] += 1
        
        log_error(
            self.user_id,
            f"Превышен лимит риска: {event.limit_type}",
            module_name=__name__
        )
        
        # Экстренная остановка при критических нарушениях
        if event.limit_type in ["daily_drawdown", "max_loss"]:
            await self.stop(f"Risk limit exceeded: {event.limit_type}")

