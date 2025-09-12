# core/websocket_manager.py
"""
Многопользовательский WebSocket менеджер для торговой системы
Реализует DataFeedHandler для каждого пользователя с событийной архитектурой
"""
import asyncio
import json
import websockets
from decimal import Decimal, getcontext
from typing import Dict, Optional, Set, List, Any
from datetime import datetime
import hmac
import hashlib
import time

from core.logger import log_info, log_error, log_warning
from core.events import (
    EventType, NewCandleEvent, PriceUpdateEvent, OrderUpdateEvent,
    OrderFilledEvent, PositionUpdateEvent, PositionClosedEvent, EventBus
)
from cache.redis_manager import redis_manager, ConfigType
from database.db_trades import db_manager
from core.settings_config import system_config

# Настройка точности для Decimal
getcontext().prec = 28


class GlobalWebSocketManager:
    """
    Глобальный менеджер WebSocket соединений
    Управляет одним публичным соединением для всех пользователей
    """

    def __init__(self, event_bus: EventBus, demo: bool = False):
        self.public_connection: Optional[websockets.WebSocketClientProtocol] = None
        self.event_bus = event_bus
        self.running = False

        # Публичный URL всегда один - боевой, т.к. он для всех пользователей
        self.public_url = "wss://stream.bybit.com/v5/public/linear"

        # Приватный URL зависит от режима demo, он будет использоваться в DataFeedHandler
        private_domain = "stream-demo.bybit.com" if demo else "stream.bybit.com"
        self.private_url_template = f"wss://{private_domain}/v5/private"

        log_info(0, f"WebSocket Manager использует Public URL: {self.public_url}", module_name=__name__)
        log_info(0, f"WebSocket Manager использует Private URL Template: {self.private_url_template}",
                 module_name=__name__)

        # Отслеживание подписок
        self.symbol_subscribers: Dict[str, Set[int]] = {}  # symbol -> set of user_ids
        self.subscribed_symbols: Set[str] = set()

        # Задача только для публичного соединения
        self._public_task: Optional[asyncio.Task] = None

    async def start(self):
        """Запуск глобального WebSocket менеджера"""
        if self.running:
            return

        log_info(0, "Запуск GlobalWebSocketManager...", module_name=__name__)
        self.running = True
        self._public_task = asyncio.create_task(self._public_websocket_loop())
        log_info(0, "GlobalWebSocketManager запущен", module_name=__name__)

    async def stop(self):
        """Остановка глобального WebSocket менеджера"""
        if not self.running:
            return

        log_info(0, "Остановка GlobalWebSocketManager...", module_name=__name__)

        self.running = False

        if self._public_task:
            self._public_task.cancel()
            try:
                await self._public_task
            except asyncio.CancelledError:
                pass

        if self.public_connection:
            await self.public_connection.close()

        log_info(0, "GlobalWebSocketManager остановлен", module_name=__name__)

    async def subscribe_symbol(self, user_id: int, symbol: str):
        """Подписка пользователя на символ"""
        if symbol not in self.symbol_subscribers:
            self.symbol_subscribers[symbol] = set()

        self.symbol_subscribers[symbol].add(user_id)

        # Если это первая подписка на символ, подписываемся в WebSocket
        if symbol not in self.subscribed_symbols:
            await self._subscribe_to_symbol(symbol)
            self.subscribed_symbols.add(symbol)

        log_info(user_id, f"Подписка на {symbol}", module_name=__name__)

    async def unsubscribe_symbol(self, user_id: int, symbol: str):
        """Отписка пользователя от символа"""
        if symbol in self.symbol_subscribers:
            self.symbol_subscribers[symbol].discard(user_id)

            # Если больше нет подписчиков, отписываемся от WebSocket
            if not self.symbol_subscribers[symbol]:
                await self._unsubscribe_from_symbol(symbol)
                self.subscribed_symbols.discard(symbol)
                del self.symbol_subscribers[symbol]

        log_info(user_id, f"Отписка от {symbol}", module_name=__name__)

    async def unsubscribe_user(self, user_id: int):
        """Отписка пользователя от всех символов"""
        symbols_to_remove = []

        for symbol, subscribers in self.symbol_subscribers.items():
            if user_id in subscribers:
                subscribers.discard(user_id)
                if not subscribers:
                    symbols_to_remove.append(symbol)

        for symbol in symbols_to_remove:
            await self._unsubscribe_from_symbol(symbol)
            self.subscribed_symbols.discard(symbol)
            del self.symbol_subscribers[symbol]

        log_info(user_id, "Отписка от всех символов", module_name=__name__)

    async def _public_websocket_loop(self):
        """Основной цикл публичного WebSocket"""
        while self.running:
            try:
                log_info(0, f"Подключение к публичному WebSocket: {self.public_url}", module_name=__name__)

                async with websockets.connect(self.public_url) as websocket:

                    self.public_connection = websocket
                    log_info(0, "Подключен к публичному WebSocket", module_name=__name__)

                    # Переподписка на все символы
                    for symbol in list(self.subscribed_symbols):
                        await self._subscribe_to_symbol(symbol)

                    # Обработка сообщений
                    async for message in websocket:
                        if not self.running:
                            break

                        try:
                            await self._handle_public_message(message)
                        except Exception as e:
                            log_error(0, f"Ошибка обработки публичного сообщения: {e}", module_name=__name__)

            except Exception as e:
                log_error(0, f"Ошибка публичного WebSocket: {e}", module_name=__name__)

                if self.running:
                    await asyncio.sleep(5)  # Пауза перед переподключением

    async def _subscribe_to_symbol(self, symbol: str):
        """Подписка на символ в WebSocket"""
        if not self.public_connection:
            return

        try:
            # Подписка на тикер (цена)
            ticker_msg = {
                "op": "subscribe",
                "args": [f"tickers.{symbol}"]
            }
            await self.public_connection.send(json.dumps(ticker_msg))

            # Подписка на свечи 1m
            candle_msg = {
                "op": "subscribe",
                "args": [f"kline.1.{symbol}"]
            }
            await self.public_connection.send(json.dumps(candle_msg))

            log_info(0, f"Подписка на {symbol} отправлена", module_name=__name__)

        except Exception as e:
            log_error(0, f"Ошибка подписки на {symbol}: {e}", module_name=__name__)

    async def _unsubscribe_from_symbol(self, symbol: str):
        """Отписка от символа в WebSocket"""
        if not self.public_connection:
            return

        try:
            # Отписка от тикера
            ticker_msg = {
                "op": "unsubscribe",
                "args": [f"tickers.{symbol}"]
            }
            await self.public_connection.send(json.dumps(ticker_msg))

            # Отписка от свечей
            candle_msg = {
                "op": "unsubscribe",
                "args": [f"kline.1.{symbol}"]
            }
            await self.public_connection.send(json.dumps(candle_msg))

            log_info(0, f"Отписка от {symbol} отправлена", module_name=__name__)

        except Exception as e:
            log_error(0, f"Ошибка отписки от {symbol}: {e}", module_name=__name__)

    async def _handle_public_message(self, message: str):
        """Обработка публичных сообщений"""
        try:
            data = json.loads(message)

            if "topic" not in data:
                return

            topic = data["topic"]

            # Обработка тикеров (цены)
            if topic.startswith("tickers."):
                symbol = topic.split(".")[1]
                await self._handle_ticker_update(symbol, data["data"])

            # Обработка свечей
            elif topic.startswith("kline."):
                parts = topic.split(".")
                interval = parts[1]
                symbol = parts[2]
                await self._handle_candle_update(symbol, interval, data["data"])

        except Exception as e:
            log_error(0, f"Ошибка парсинга публичного сообщения: {e}", module_name=__name__)

    async def _handle_ticker_update(self, symbol: str, ticker_data: Dict[str, Any]):
        """Обработка обновления тикера"""
        try:
            # Данные тикера приходят как объект, а не список
            if not ticker_data:
                return

            price = Decimal(str(ticker_data.get("lastPrice", "0")))

            if price <= 0:
                return

            # Отправка события всем подписчикам символа
            if symbol in self.symbol_subscribers:
                for user_id in self.symbol_subscribers[symbol]:
                    price_event = PriceUpdateEvent(
                        user_id=user_id,
                        symbol=symbol,
                        price=price
                    )
                    await self.event_bus.publish(price_event)

        except Exception as e:
            log_error(0, f"Ошибка обработки тикера {symbol}: {e}", module_name=__name__)

    async def _handle_candle_update(self, symbol: str, interval: str, candle_data: List[Dict]):
        """Обработка обновления свечи"""
        try:
            if not candle_data:
                return

            candle = candle_data[0]

            # Проверяем, что свеча закрыта
            if not candle.get("confirm", False):
                return

            # Конвертация данных свечи в Decimal
            candle_decimal = {
                "timestamp": int(candle["start"]),
                "open": Decimal(str(candle["open"])),
                "high": Decimal(str(candle["high"])),
                "low": Decimal(str(candle["low"])),
                "close": Decimal(str(candle["close"])),
                "volume": Decimal(str(candle["volume"]))
            }

            # Отправка события всем подписчикам символа
            if symbol in self.symbol_subscribers:
                for user_id in self.symbol_subscribers[symbol]:
                    candle_event = NewCandleEvent(
                        user_id=user_id,
                        symbol=symbol,
                        interval=interval,
                        candle_data=candle_decimal
                    )
                    await self.event_bus.publish(candle_event)

        except Exception as e:
            log_error(0, f"Ошибка обработки свечи {symbol}: {e}", module_name=__name__)


class DataFeedHandler:
    """
    Персональный обработчик данных для пользователя
    Управляет подписками на рыночные данные и приватные события
    """

    def __init__(self, user_id: int, event_bus: EventBus, global_ws_manager: "GlobalWebSocketManager"):
        self.user_id = user_id
        self.event_bus = event_bus
        self.global_ws_manager = global_ws_manager
        self.running = False

        # Приватное WebSocket соединение
        self.private_connection: Optional[websockets.WebSocketClientProtocol] = None
        self._private_task: Optional[asyncio.Task] = None

        # API ключи пользователя
        self.api_key: Optional[str] = None
        self.api_secret: Optional[str] = None

    async def start(self):
        """Запуск DataFeedHandler"""
        if self.running:
            return

        log_info(self.user_id, "Запуск DataFeedHandler...", module_name=__name__)

        try:
            await self._load_api_credentials()
            await self._subscribe_to_watchlist()

            if self.api_key and self.api_secret:
                self._private_task = asyncio.create_task(self._private_websocket_loop())

            self.running = True
            log_info(self.user_id, "DataFeedHandler запущен", module_name=__name__)

        except Exception as e:
            log_error(self.user_id, f"Ошибка запуска DataFeedHandler: {e}", module_name=__name__)
            raise

    async def _handle_position_activity(self, event: PositionUpdateEvent):
        """
        Обрабатывает активность по позиции для управления подписками на рыночные данные.
        """
        if event.user_id != self.user_id:
            return

        symbol = event.symbol
        position_size = event.size

        try:
            if position_size > 0:
                # Позиция активна (открыта или увеличена), подписываемся на данные
                log_info(self.user_id,
                         f"Позиция по {symbol} активна (размер: {position_size}), подписываюсь на обновления цены.",
                         module_name=__name__)
                await self.global_ws_manager.subscribe_symbol(self.user_id, symbol)
            else:
                # Позиция закрыта (размер 0), отписываемся, если символ не в watchlist
                global_config = await redis_manager.get_config(self.user_id, ConfigType.GLOBAL)
                watchlist = global_config.get("watchlist_symbols", []) if global_config else []

                if symbol not in watchlist:
                    log_info(self.user_id, f"Позиция по {symbol} (вне watchlist) закрыта, отписываюсь от обновлений.",
                             module_name=__name__)
                    await self.global_ws_manager.unsubscribe_symbol(self.user_id, symbol)
                else:
                    log_info(self.user_id, f"Позиция по {symbol} (из watchlist) закрыта, подписка остается активной.",
                             module_name=__name__)

        except Exception as e:
            log_error(self.user_id, f"Ошибка в _handle_position_activity для {symbol}: {e}", module_name=__name__)

    async def stop(self):
        """Остановка DataFeedHandler"""
        if not self.running:
            return

        log_info(self.user_id, "Остановка DataFeedHandler...", module_name=__name__)

        self.running = False

        # Отписка от всех символов
        await global_ws_manager.unsubscribe_user(self.user_id)

        # Остановка приватного WebSocket
        if self._private_task:
            self._private_task.cancel()
            try:
                await self._private_task
            except asyncio.CancelledError:
                pass

        if self.private_connection:
            await self.private_connection.close()

        log_info(self.user_id, "DataFeedHandler остановлен", module_name=__name__)

    async def _load_api_credentials(self):
        """Загрузка API ключей пользователя"""
        try:
            # Используем db_manager и его метод get_api_keys
            keys = await db_manager.get_api_keys(self.user_id, "bybit")
            if keys:
                # Метод возвращает кортеж (api_key, secret_key, passphrase)
                self.api_key, self.api_secret, _ = keys
                log_info(self.user_id, "API ключи загружены", module_name=__name__)
            else:
                log_info(self.user_id, "API ключи не найдены", module_name=__name__)
        except Exception as e:
            log_error(self.user_id, f"Ошибка загрузки API ключей: {e}", module_name=__name__)

    async def _subscribe_to_watchlist(self):
        """Подписка на символы из watchlist пользователя"""
        try:
            global_config = await redis_manager.get_config(self.user_id, ConfigType.GLOBAL)
            if not global_config:
                return

            watchlist = global_config.get("watchlist_symbols", [])
            for symbol in watchlist:
                await self.global_ws_manager.subscribe_symbol(self.user_id, symbol)
            log_info(self.user_id, f"Подписка на watchlist: {watchlist}", module_name=__name__)
        except Exception as e:
            log_error(self.user_id, f"Ошибка подписки на watchlist: {e}", module_name=__name__)

    async def _private_websocket_loop(self):
        """Основной цикл приватного WebSocket"""
        while self.running:
            try:
                private_url = self.global_ws_manager.private_url_template
                log_info(self.user_id, f"Подключение к приватному WebSocket: {private_url}", module_name=__name__)

                async with websockets.connect(private_url) as websocket:
                    self.private_connection = websocket

                    # Аутентификация
                    await self._authenticate_private_websocket()

                    # Подписка на приватные каналы
                    await self._subscribe_private_channels()

                    log_info(self.user_id, "Подключен к приватному WebSocket", module_name=__name__)

                    # Обработка сообщений
                    async for message in websocket:
                        if not self.running:
                            break

                        try:
                            await self._handle_private_message(message)
                        except Exception as e:
                            log_error(self.user_id, f"Ошибка обработки приватного сообщения: {e}", module_name=__name__)

            except Exception as e:
                log_error(self.user_id, f"Ошибка приватного WebSocket: {e}", module_name=__name__)

                if self.running:
                    await asyncio.sleep(5)  # Пауза перед переподключением

    async def _authenticate_private_websocket(self):
        """Аутентификация в приватном WebSocket"""
        if not self.api_key or not self.api_secret:
            return

        try:
            expires = int(time.time() * 1000) + 10000
            signature = hmac.new(
                self.api_secret.encode('utf-8'),
                f'GET/realtime{expires}'.encode('utf-8'),
                hashlib.sha256
            ).hexdigest()

            auth_msg = {
                "op": "auth",
                "args": [self.api_key, expires, signature]
            }

            await self.private_connection.send(json.dumps(auth_msg))
            log_info(self.user_id, "Аутентификация отправлена", module_name=__name__)

        except Exception as e:
            log_error(self.user_id, f"Ошибка аутентификации: {e}", module_name=__name__)

    async def _subscribe_private_channels(self):
        """Подписка на приватные каналы"""
        try:
            # Подписка на ордера
            order_msg = {
                "op": "subscribe",
                "args": ["order"]
            }
            await self.private_connection.send(json.dumps(order_msg))

            # Подписка на позиции
            position_msg = {
                "op": "subscribe",
                "args": ["position"]
            }
            await self.private_connection.send(json.dumps(position_msg))

            log_info(self.user_id, "Подписка на приватные каналы отправлена", module_name=__name__)

        except Exception as e:
            log_error(self.user_id, f"Ошибка подписки на приватные каналы: {e}", module_name=__name__)

    async def _handle_private_message(self, message: str):
        """Обработка приватных сообщений"""
        try:
            log_info(self.user_id, f"[TRACE] Получено приватное сообщение: {message[:200]}...", module_name=__name__)
            data = json.loads(message)

            if "topic" not in data:
                log_warning(self.user_id, f"[TRACE] Сообщение без топика: {data}", module_name=__name__)
                return

            topic = data["topic"]
            log_info(self.user_id, f"[TRACE] Обрабатываем топик: {topic}", module_name=__name__)

            # Обработка ордеров
            if topic == "order":
                log_info(self.user_id, f"[TRACE] Обрабатываем обновление ордера: {data['data']}", module_name=__name__)
                await self._handle_order_update(data["data"])

            # Обработка позиций
            elif topic == "position":
                await self._handle_position_update(data["data"])

        except Exception as e:
            log_error(self.user_id, f"Ошибка парсинга приватного сообщения: {e}", module_name=__name__)

    async def _handle_order_update(self, order_data: List[Dict]):
        """Обработка обновления ордера"""
        try:
            log_info(self.user_id, f"[TRACE] Обрабатываем {len(order_data)} ордеров", module_name=__name__)
            for order in order_data:
                order_status = order.get("orderStatus", "Unknown")
                order_id = order.get("orderId", "Unknown")
                symbol = order.get("symbol", "Unknown")
                log_info(self.user_id, f"[TRACE] Ордер {order_id} ({symbol}): статус = {order_status}",
                         module_name=__name__)

                # Общее событие обновления ордера
                order_event = OrderUpdateEvent(
                    user_id=self.user_id,
                    order_data=order
                )
                await self.event_bus.publish(order_event)
        except Exception as e:
            log_error(self.user_id, f"Ошибка обработки ордера: {e}", module_name=__name__)

    async def _handle_position_update(self, position_data: List[Dict]):
        """Обработка обновления позиции"""
        try:
            for position in position_data:
                position_event = PositionUpdateEvent(
                    user_id=self.user_id,
                    symbol=position.get("symbol", ""),
                    side=position.get("side", ""),
                    size=Decimal(str(position.get("size", "0"))),
                    entry_price=Decimal(str(position.get("avgPrice", "0"))),
                    mark_price=Decimal(str(position.get("markPrice", "0"))),
                    unrealized_pnl=Decimal(str(position.get("unrealisedPnl", "0")))
                )
                await self.event_bus.publish(position_event)

        except Exception as e:
            log_error(self.user_id, f"Ошибка обработки позиции: {e}", module_name=__name__)