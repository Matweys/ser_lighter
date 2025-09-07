import asyncio
from dataclasses import dataclass, field
from decimal import Decimal
from typing import Dict, Any, Optional, List, Callable, Awaitable
from datetime import datetime
from enum import Enum

# Enum EventType остается без изменений
class EventType(Enum):
    NEW_CANDLE = "new_candle"
    PRICE_UPDATE = "price_update"
    ORDER_UPDATE = "order_update"
    ORDER_FILLED = "order_filled"
    POSITION_UPDATE = "position_update"
    POSITION_CLOSED = "position_closed"
    SIGNAL = "signal"
    STRATEGY_START = "strategy_start"
    STRATEGY_STOP = "strategy_stop"
    STRATEGY_RESTART_REQUESTED = "strategy_restart_requested"
    USER_SESSION_START_REQUESTED = "user_session_start_requested"
    USER_SESSION_STOP_REQUESTED = "user_session_stop_requested"
    USER_SESSION_STARTED = "user_session_started"
    USER_SESSION_STOPPED = "user_session_stopped"
    USER_SETTINGS_CHANGED = "user_settings_changed"
    RISK_LIMIT_EXCEEDED = "risk_limit_exceeded"
    DRAWDOWN_WARNING = "drawdown_warning"
    SYSTEM_STATUS = "system_status"
    GLOBAL_CANDLE = "global_candle"

@dataclass
class GlobalCandleEvent:
    """
    Глобальное событие о новой свече от сканера рынка.
    Не привязано к конкретному пользователю.
    """
    candle_data: Dict[str, Any]
    timestamp: datetime = field(default_factory=datetime.now, init=False)
    event_type: EventType = field(default=EventType.GLOBAL_CANDLE, init=False)


@dataclass
class BaseEvent:
    """Базовый класс для всех событий"""
    user_id: int
    # timestamp теперь не участвует в __init__, а создается после
    timestamp: datetime = field(init=False)

    def __post_init__(self):
        """Устанавливает timestamp после создания объекта"""
        self.timestamp = datetime.now()


@dataclass
class NewCandleEvent(BaseEvent):
    """Событие о закрытии новой свечи"""
    symbol: str
    interval: str
    candle_data: Dict[str, Decimal]
    event_type: EventType = field(default=EventType.NEW_CANDLE, init=False)


@dataclass
class PriceUpdateEvent(BaseEvent):
    """Событие об обновлении цены тикера"""
    symbol: str
    price: Decimal
    event_type: EventType = field(default=EventType.PRICE_UPDATE, init=False)


@dataclass
class OrderUpdateEvent(BaseEvent):
    """Событие об обновлении статуса ордера"""
    order_data: Dict[str, Any]
    event_type: EventType = field(default=EventType.ORDER_UPDATE, init=False)


@dataclass
class OrderFilledEvent(BaseEvent):
    """Событие об исполнении ордера"""
    order_id: str
    symbol: str
    side: str
    qty: Decimal
    price: Decimal
    fee: Decimal
    strategy_type: Optional[str] = None
    event_type: EventType = field(default=EventType.ORDER_FILLED, init=False)


@dataclass
class PositionUpdateEvent(BaseEvent):
    """Событие об обновлении позиции"""
    symbol: str
    side: str
    size: Decimal
    entry_price: Decimal
    mark_price: Decimal
    unrealized_pnl: Decimal
    event_type: EventType = field(default=EventType.POSITION_UPDATE, init=False)


@dataclass
class PositionClosedEvent(BaseEvent):
    """Событие о закрытии позиции"""
    symbol: str
    side: str
    size: Decimal
    entry_price: Decimal
    exit_price: Decimal
    realized_pnl: Decimal
    strategy_type: Optional[str] = None
    event_type: EventType = field(default=EventType.POSITION_CLOSED, init=False)


@dataclass
class SignalEvent(BaseEvent):
    """Событие-сигнал от MetaStrategist для запуска стратегии"""
    symbol: str
    strategy_type: str
    signal_strength: int
    analysis_data: Dict[str, Any]
    event_type: EventType = field(default=EventType.SIGNAL, init=False)

@dataclass
class StrategyRestartRequestEvent(BaseEvent):
    """
    Событие-запрос на немедленный перезапуск стратегии.
    Обычно публикуется самой стратегией после успешного завершения цикла.
    """
    strategy_type: str
    symbol: str
    reason: str = "cycle_completed"
    event_type: EventType = field(default=EventType.STRATEGY_RESTART_REQUESTED, init=False)

@dataclass
class StrategyStartEvent(BaseEvent):
    """Событие о запуске стратегии"""
    symbol: str
    strategy_type: str
    strategy_id: str
    event_type: EventType = field(default=EventType.STRATEGY_START, init=False)


@dataclass
class StrategyStopEvent(BaseEvent):
    """Событие об остановке стратегии"""
    symbol: str
    strategy_type: str
    strategy_id: str
    reason: str
    event_type: EventType = field(default=EventType.STRATEGY_STOP, init=False)

@dataclass
class UserSessionStartRequestedEvent(BaseEvent):
    """Событие-запрос на запуск пользовательской сессии от пользователя"""
    event_type: EventType = field(default=EventType.USER_SESSION_START_REQUESTED, init=False)


@dataclass
class UserSessionStopRequestedEvent(BaseEvent):
    """Событие-запрос на остановку пользовательской сессии от пользователя"""
    reason: str = "user_request"
    event_type: EventType = field(default=EventType.USER_SESSION_STOP_REQUESTED, init=False)

@dataclass
class UserSessionStartedEvent(BaseEvent):
    """Событие о запуске пользовательской сессии"""
    timestamp: datetime
    event_type: EventType = field(default=EventType.USER_SESSION_STARTED, init=False)


@dataclass
class UserSessionStoppedEvent(BaseEvent):
    """Событие об остановке пользовательской сессии"""
    reason: str
    timestamp: datetime
    event_type: EventType = field(default=EventType.USER_SESSION_STOPPED, init=False)


@dataclass
class UserSettingsChangedEvent(BaseEvent):
    """Событие об изменении настроек пользователя"""
    changed_settings: List[str] = field(default_factory=list)
    config_type: str = "global"
    strategy_type: Optional[str] = None
    event_type: EventType = field(default=EventType.USER_SETTINGS_CHANGED, init=False)


@dataclass
class RiskLimitExceededEvent(BaseEvent):
    """Событие о превышении лимитов риска"""
    limit_type: str
    current_value: Decimal
    limit_value: Decimal
    action_required: str
    event_type: EventType = field(default=EventType.RISK_LIMIT_EXCEEDED, init=False)


@dataclass
class DrawdownWarningEvent(BaseEvent):
    """Событие-предупреждение о приближении к лимиту просадки"""
    current_drawdown_percent: Decimal
    warning_threshold_percent: Decimal
    max_drawdown_percent: Decimal
    event_type: EventType = field(default=EventType.DRAWDOWN_WARNING, init=False)


@dataclass
class SystemStatusEvent(BaseEvent):
    """Событие о статусе системы"""
    status: str
    message: str
    metadata: Dict[str, Any] = field(default_factory=dict)
    # Для системных событий user_id может быть неактуален, но он обязателен в BaseEvent
    # Он будет устанавливаться при создании события (например, user_id=0)
    event_type: EventType = field(default=EventType.SYSTEM_STATUS, init=False)


# Класс EventBus и глобальный экземпляр event_bus остаются без изменений
class EventBus:
    def __init__(self, max_queue_size: int = 10000):
        self._queue: asyncio.Queue = asyncio.Queue(maxsize=max_queue_size)
        self._subscribers: Dict[EventType, List[Callable[[BaseEvent], Awaitable[None]]]] = {}
        self._user_subscribers: Dict[int, List[Callable[[BaseEvent], Awaitable[None]]]] = {}
        self._running = False
        self._processor_task: Optional[asyncio.Task] = None

    async def start(self):
        if self._running: return
        self._running = True
        self._processor_task = asyncio.create_task(self._process_events())

    async def stop(self):
        self._running = False
        if self._processor_task:
            self._processor_task.cancel()
            try: await self._processor_task
            except asyncio.CancelledError: pass

    async def publish(self, event: BaseEvent):
        if not self._running: raise RuntimeError("EventBus не запущен")
        try:
            await self._queue.put(event)
        except asyncio.QueueFull:
            from core.logger import get_logger
            logger = get_logger(__name__)
            logger.error(f"EventBus переполнен, событие {type(event).__name__} отброшено", extra={"user_id": event.user_id})

    def subscribe(self, event_type: EventType, handler: Callable[[BaseEvent], Awaitable[None]]):
        if event_type not in self._subscribers: self._subscribers[event_type] = []
        self._subscribers[event_type].append(handler)

    def subscribe_user(self, user_id: int, handler: Callable[[BaseEvent], Awaitable[None]]):
        if user_id not in self._user_subscribers: self._user_subscribers[user_id] = []
        self._user_subscribers[user_id].append(handler)

    def unsubscribe_user(self, user_id: int):
        if user_id in self._user_subscribers: del self._user_subscribers[user_id]

    async def _process_events(self):
        from core.logger import get_logger
        logger = get_logger(__name__)
        while self._running:
            try:
                event = await asyncio.wait_for(self._queue.get(), timeout=1.0)
                event_type_attr = getattr(event, 'event_type', None)
                if event_type_attr in self._subscribers:
                    for handler in self._subscribers[event_type_attr]:
                        try: await handler(event)
                        except Exception as e: logger.error(f"Ошибка в обработчике события {event_type_attr}: {e}", extra={"user_id": event.user_id})
                user_id = getattr(event, 'user_id', None)
                if user_id is not None and user_id in self._user_subscribers:
                    for handler in self._user_subscribers[user_id]:
                        try:
                            await handler(event)
                        except Exception as e:
                            logger.error(f"Ошибка в пользовательском обработчике события: {e}",
                                         extra={"user_id": user_id})
                self._queue.task_done()
            except asyncio.TimeoutError: continue
            except Exception as e:
                logger.error(f"Критическая ошибка в EventBus: {e}")
                await asyncio.sleep(1)

