# Вставьте этот код в начало файла core/events.py, прямо после импортов
from typing import Dict, Any, Optional, List, Callable, Awaitable
from dataclasses import dataclass

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
            log_error(0, f"Попытка публикации в остановленную EventBus: {type(event).__name__}", "EventBus")
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

                for handler in handlers_to_run:
                    try:
                        await handler(event)
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