import asyncio
from dataclasses import dataclass, field
from decimal import Decimal
from typing import Dict, Any, Optional, List, Callable, Awaitable
from datetime import datetime
from core.logger import log_debug, log_error, log_info
from core.enums import EventType

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
    """
    Событие о закрытии позиции

    MULTI-ACCOUNT SUPPORT: Содержит bot_priority для идентификации какой бот должен обработать событие
    """
    symbol: str
    side: Optional[str] = None
    size: Optional[Decimal] = None
    entry_price: Optional[Decimal] = None
    exit_price: Optional[Decimal] = None
    realized_pnl: Optional[Decimal] = None
    strategy_type: Optional[str] = None
    bot_priority: Optional[int] = None  # 1=PRIMARY, 2=SECONDARY, 3=TERTIARY (None = для всех)
    closed_manually: bool = False  # Флаг ручного закрытия через WebSocket
    event_type: EventType = field(default=EventType.POSITION_CLOSED, init=False)


@dataclass
class SignalEvent(BaseEvent):
    """Событие-сигнал от MetaStrategist для запуска стратегии"""
    symbol: str
    strategy_type: str
    signal_strength: int
    analysis_data: Optional[Dict[str, Any]] = None
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
    delay_seconds: int = 0
    event_type: EventType = field(default=EventType.STRATEGY_RESTART_REQUESTED, init=False)

@dataclass
class StrategyStartEvent(BaseEvent):
    """Событие о запуске стратегии"""
    symbol: str
    strategy_type: str
    strategy_id: str
    event_type: EventType = field(default=EventType.STRATEGY_STARTED, init=False)


@dataclass
class StrategyStopEvent(BaseEvent):
    """Событие об остановке стратегии"""
    symbol: str
    strategy_type: str
    strategy_id: str
    reason: str
    event_type: EventType = field(default=EventType.STRATEGY_STOPPED, init=False)

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



# Типизация для обработчиков
Handler = Callable[[Any], Awaitable[None]]


@dataclass
class Subscription:
    """Хранит информацию о подписке"""
    handler: Handler
    event_type: EventType
    user_id: Optional[int] = None


# Замените ваш старый класс EventBus на этот
class EventBus:
    """
    Улучшенная, унифицированная шина событий.
    - Единый список подписчиков.
    - Поддержка как глобальных, так и пользовательских подписок через один метод.
    - Гарантированная и логичная доставка событий.
    """

    def __init__(self, max_queue_size: int = 10000):
        self._queue: asyncio.Queue = asyncio.Queue(maxsize=max_queue_size)
        self._subscriptions: List[Subscription] = []
        self._running = False
        self._processor_task: Optional[asyncio.Task] = None
        self._lock = asyncio.Lock()

    async def start(self):
        if self._running: return
        self._running = True
        self._processor_task = asyncio.create_task(self._process_events())

    async def stop(self):
        if not self._running: return
        self._running = False
        if self._processor_task:
            if not self._queue.empty():
                await self._queue.join()
            self._processor_task.cancel()
            try:
                await self._processor_task
            except asyncio.CancelledError:
                pass

    async def publish(self, event: Any):
        if not self._running:
            # Более мягкое логирование - не каждое событие в остановленную шину является ошибкой
            log_debug(0, f"Попытка публикации в остановленную EventBus: {type(event).__name__}", "EventBus")
            return
        try:
            await self._queue.put(event)
        except asyncio.QueueFull:
            user_id_attr = getattr(event, 'user_id', 0)
            log_error(user_id_attr, f"EventBus переполнен, событие {type(event).__name__} отброшено", "EventBus")

    async def subscribe(self, event_type: EventType, handler: Handler, user_id: Optional[int] = None):
        """Единый метод подписки. Укажите user_id для пользовательской подписки."""
        async with self._lock:
            # Предотвращаем дублирование подписок
            for sub in self._subscriptions:
                if sub.handler == handler and sub.event_type == event_type and sub.user_id == user_id:
                    return
            sub = Subscription(handler=handler, event_type=event_type, user_id=user_id)
            self._subscriptions.append(sub)
            log_info(user_id or 0,
                     f"Новая подписка: {handler.__name__} на {event_type.value} (User: {user_id or 'Global'})",
                     "EventBus")

    async def unsubscribe(self, handler: Handler):
        """Отписка по обработчику. Удаляет ВСЕ подписки, связанные с этим обработчиком."""
        async with self._lock:
            initial_count = len(self._subscriptions)
            self._subscriptions = [sub for sub in self._subscriptions if sub.handler != handler]
            removed_count = initial_count - len(self._subscriptions)
            if removed_count > 0:
                log_info(0, f"Удалено {removed_count} подписок для обработчика {handler.__name__}", "EventBus")

    async def _process_events(self):
        while self._running:
            try:
                event = await self._queue.get()

                event_type = getattr(event, 'event_type', None)
                if not event_type:
                    self._queue.task_done()
                    continue

                event_user_id = getattr(event, 'user_id', None)

                # Блокировка не нужна, т.к. мы копируем список. Это безопасно.
                current_subs = self._subscriptions[:]

                handlers_to_run = []
                for sub in current_subs:
                    if sub.event_type == event_type:
                        # Если подписка глобальная (user_id is None), она получает все события этого типа.
                        # Если подписка пользовательская, она получает событие только если user_id совпадают.
                        if sub.user_id is None or sub.user_id == event_user_id:
                            handlers_to_run.append(sub.handler)

                # ДИАГНОСТИКА: Показываем доставку событий
                if event_type in [EventType.PRICE_UPDATE, EventType.NEW_CANDLE]:
                    symbol = getattr(event, 'symbol', 'N/A')
                    log_info(event_user_id or 0,
                            f"📮 EventBus: {event_type.value} для {symbol} → найдено {len(handlers_to_run)} обработчиков (user_id={event_user_id})",
                            "EventBus")

                for handler in handlers_to_run:
                    try:
                        await handler(event)
                        # ДИАГНОСТИКА: Логируем успешный вызов handler
                        if event_type in [EventType.PRICE_UPDATE, EventType.NEW_CANDLE]:
                            log_info(event_user_id or 0,
                                    f"✅ EventBus: handler {handler.__name__} вызван успешно",
                                    "EventBus")
                    except Exception as e:
                        log_error(getattr(event, 'user_id', 0), f"Ошибка в обработчике {handler.__name__}: {e}",
                                  "EventBus")

                self._queue.task_done()
            except asyncio.CancelledError:
                break
            except Exception as e:
                log_error(0, f"Критическая ошибка в EventBus: {e}", "EventBus")
                await asyncio.sleep(1)


# Глобальный экземпляр остается без изменений
event_bus = EventBus()