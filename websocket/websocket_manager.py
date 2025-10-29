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
from core.functions import to_decimal
from core.logger import log_info, log_error, log_warning, log_debug
from core.events import (
    EventType, NewCandleEvent, PriceUpdateEvent, OrderUpdateEvent,
    OrderFilledEvent, PositionUpdateEvent, PositionClosedEvent, EventBus
)
from cache.redis_manager import redis_manager, ConfigType
from database.db_trades import db_manager
from core.settings_config import system_config
from api.bybit_api import BybitAPI

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
                # Фильтруем обычные сетевые ошибки WebSocket
                error_str = str(e)
                if "no close frame" in error_str or "connection closed" in error_str:
                    log_info(0, f"Публичный WebSocket переподключение: {e}", module_name=__name__)
                else:
                    log_error(0, f"Ошибка публичного WebSocket: {e}", module_name=__name__)

                if self.running:
                    await asyncio.sleep(5)  # Пауза перед переподключением

    async def _subscribe_to_symbol(self, symbol: str):
        """Подписка на символ в WebSocket"""
        if not self.public_connection:
            return

        try:
            # МГНОВЕННАЯ ПОДПИСКА: publicTrade - реальные сделки в режиме реального времени
            # Обновления приходят при КАЖДОЙ сделке на бирже (самый быстрый поток!)
            trade_msg = {
                "op": "subscribe",
                "args": [f"publicTrade.{symbol}"]
            }
            await self.public_connection.send(json.dumps(trade_msg))

            # Подписка на свечи 5m (для стратегий на 5-минутном таймфрейме)
            candle_5m_msg = {
                "op": "subscribe",
                "args": [f"kline.5.{symbol}"]  # 5-минутные свечи
            }
            await self.public_connection.send(json.dumps(candle_5m_msg))

            # Подписка на свечи 1m (для spike detector)
            candle_1m_msg = {
                "op": "subscribe",
                "args": [f"kline.1.{symbol}"]  # 1-минутные свечи
            }
            await self.public_connection.send(json.dumps(candle_1m_msg))

            log_info(0, f"⚡ МГНОВЕННАЯ подписка на {symbol} (publicTrade + kline.5 + kline.1) отправлена", module_name=__name__)

        except Exception as e:
            log_error(0, f"Ошибка подписки на {symbol}: {e}", module_name=__name__)

    async def _unsubscribe_from_symbol(self, symbol: str):
        """Отписка от символа в WebSocket"""
        if not self.public_connection:
            return

        try:
            # Отписка от мгновенных обновлений
            trade_msg = {
                "op": "unsubscribe",
                "args": [f"publicTrade.{symbol}"]
            }
            await self.public_connection.send(json.dumps(trade_msg))

            # Отписка от свечей 5m
            candle_5m_msg = {
                "op": "unsubscribe",
                "args": [f"kline.5.{symbol}"]  # 5-минутные свечи
            }
            await self.public_connection.send(json.dumps(candle_5m_msg))

            # Отписка от свечей 1m
            candle_1m_msg = {
                "op": "unsubscribe",
                "args": [f"kline.1.{symbol}"]  # 1-минутные свечи
            }
            await self.public_connection.send(json.dumps(candle_1m_msg))

            log_info(0, f"Отписка от {symbol} (kline.5 + kline.1) отправлена", module_name=__name__)

        except Exception as e:
            log_error(0, f"Ошибка отписки от {symbol}: {e}", module_name=__name__)

    async def _handle_public_message(self, message: str):
        """Обработка публичных сообщений"""
        try:
            data = json.loads(message)

            if "topic" not in data:
                return

            topic = data["topic"]

            # Обработка публичных сделок (МГНОВЕННЫЕ обновления цен!)
            if topic.startswith("publicTrade."):
                symbol = topic.split(".")[1]
                await self._handle_public_trade(symbol, data["data"])

            # Обработка тикеров (медленные обновления - deprecated)
            elif topic.startswith("tickers."):
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

    async def _handle_public_trade(self, symbol: str, trade_data: List[Dict[str, Any]]):
        """
        Обработка публичных сделок (МГНОВЕННЫЕ обновления!)
        Это самый быстрый способ получать обновления цен в реальном времени.
        """
        try:
            if not trade_data:
                return

            # Берем последнюю сделку из массива (самая свежая цена)
            latest_trade = trade_data[-1]
            price = Decimal(str(latest_trade.get("p", "0")))

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
            log_error(0, f"Ошибка обработки публичной сделки {symbol}: {e}", module_name=__name__)

    async def _handle_ticker_update(self, symbol: str, ticker_data: Dict[str, Any]):
        """Обработка обновления тикера (медленный fallback)"""
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
                    # Bybit присылает интервал как "5", нужно конвертировать в "5m"
                    interval_formatted = f"{interval}m"
                    candle_event = NewCandleEvent(
                        user_id=user_id,
                        symbol=symbol,
                        interval=interval_formatted,
                        candle_data=candle_decimal
                    )
                    await self.event_bus.publish(candle_event)

        except Exception as e:
            log_error(0, f"Ошибка обработки свечи {symbol}: {e}", module_name=__name__)


class DataFeedHandler:
    """
    Персональный обработчик данных для пользователя
    Управляет подписками на рыночные данные и приватные события

    MULTI-ACCOUNT SUPPORT: Может создаваться несколько экземпляров для одного пользователя
    (по одному на каждый account_priority: 1=PRIMARY, 2=SECONDARY, 3=TERTIARY)
    """

    def __init__(self, user_id: int, event_bus: EventBus, global_ws_manager: "GlobalWebSocketManager",
                 account_priority: int = 1):
        self.user_id = user_id
        self.event_bus = event_bus
        self.global_ws_manager = global_ws_manager
        self.account_priority = account_priority  # 1=PRIMARY, 2=SECONDARY, 3=TERTIARY
        self.running = False

        # Приватное WebSocket соединение
        self.private_connection: Optional[websockets.WebSocketClientProtocol] = None
        self._private_task: Optional[asyncio.Task] = None

        # API ключи пользователя (для конкретного account_priority)
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

            # --- НАЧАЛО ИСПРАВЛЕНИЯ ---
            # Подписываем обработчик на события обновления позиций
            await self.event_bus.subscribe(EventType.POSITION_UPDATE, self._handle_position_activity, user_id=self.user_id)
            # --- КОНЕЦ ИСПРАВЛЕНИЯ ---

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
                log_debug(self.user_id,
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
        """
        Загрузка API ключей пользователя для конкретного аккаунта (account_priority).

        MULTI-ACCOUNT SUPPORT: Каждый DataFeedHandler загружает свой API ключ (1, 2 или 3)
        """
        try:
            keys = await db_manager.get_api_keys(self.user_id, "bybit", account_priority=self.account_priority)
            if keys:
                # Метод возвращает кортеж (api_key, secret_key, passphrase)
                self.api_key, self.api_secret, _ = keys
                log_info(self.user_id,
                        f"API ключи загружены для account_priority={self.account_priority} (Bot_{self.account_priority})",
                        module_name=__name__)
            else:
                log_info(self.user_id,
                        f"API ключи не найдены для account_priority={self.account_priority}",
                        module_name=__name__)
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

                    # КРИТИЧНО: Синхронизация состояния после переподключения
                    # Проверяем пропущенные события исполнения ордеров
                    await self._sync_orders_after_reconnect()

                    # Обработка сообщений
                    async for message in websocket:
                        if not self.running:
                            break

                        try:
                            await self._handle_private_message(message)
                        except Exception as e:
                            log_error(self.user_id, f"Ошибка обработки приватного сообщения: {e}", module_name=__name__)

            except Exception as e:
                # Фильтруем обычные сетевые ошибки WebSocket
                error_str = str(e)
                if "no close frame" in error_str or "connection closed" in error_str:
                    log_info(self.user_id, f"WebSocket переподключение: {e}", module_name=__name__)
                else:
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
            data = json.loads(message)

            # Игнорируем системные сообщения (auth, subscribe) без логирования
            if "topic" not in data:
                return

            topic = data["topic"]

            # Обработка ордеров
            if topic == "order":
                await self._handle_order_update(data["data"])

            # Обработка позиций
            elif topic == "position":
                await self._handle_position_update(data["data"])

        except Exception as e:
            log_error(self.user_id, f"Ошибка парсинга приватного сообщения: {e}", module_name=__name__)

    async def _sync_orders_after_reconnect(self):
        """
        КРИТИЧНАЯ СИНХРОНИЗАЦИЯ: Проверяет пропущенные события исполнения ордеров после WebSocket переподключения.

        Проблема: WebSocket может потерять соединение в момент исполнения ордера,
        и событие OrderFilledEvent будет потеряно. Стратегия не узнает о открытой позиции.

        Решение: После каждого переподключения WebSocket проверяем все активные ордера
        в БД и синхронизируем их статус с биржей.
        """
        try:
            # Получаем все активные ордера из БД для этого аккаунта
            # Активные = статус NEW/FILLED и order_role = OPEN (не закрывающие)
            active_orders = await db_manager.get_active_orders_for_sync(
                user_id=self.user_id,
                account_priority=self.account_priority
            )

            if not active_orders:
                # Нет ордеров для синхронизации - это нормально, не спамим логи
                return

            # КРИТИЧНО: Найдены необработанные ордера - логируем!
            log_info(self.user_id, f"🔄 СИНХРОНИЗАЦИЯ: Найдено {len(active_orders)} необработанных ордеров после WebSocket переподключения (bot_priority={self.account_priority})", module_name=__name__)

            # ДЕТАЛЬНОЕ ЛОГИРОВАНИЕ для диагностики
            for order in active_orders:
                log_info(self.user_id,
                        f"  → Ордер {order.get('order_id')}: {order.get('symbol')} {order.get('side')} {order.get('quantity')}, статус БД={order.get('status')}, purpose={order.get('order_purpose')}",
                        module_name=__name__)

            # Проверяем каждый ордер
            synced_count = 0
            for order in active_orders:
                order_id = order.get("order_id")
                symbol = order.get("symbol")
                db_status = order.get("status")

                try:
                    # Запрашиваем актуальный статус с биржи через API
                    keys = await db_manager.get_api_keys(self.user_id, "bybit", account_priority=self.account_priority)
                    if not keys:
                        log_warning(self.user_id, f"⚠️ Не найдены API ключи для синхронизации ордера {order_id}", module_name=__name__)
                        continue

                    api_key, api_secret, _ = keys

                    # Создаем временный API клиент
                    # ИСПРАВЛЕНО: demo режим определяется через system_config
                    demo_mode = system_config.DEMO_MODE
                    api = BybitAPI(api_key=api_key, secret_key=api_secret, demo=demo_mode, user_id=self.user_id)

                    # Запрашиваем статус ордера с биржи
                    order_info = await api.get_order_status(order_id=order_id)

                    if not order_info:
                        log_warning(self.user_id, f"⚠️ Ордер {order_id} не найден на бирже (возможно уже отменён)", module_name=__name__)
                        continue

                    exchange_status = order_info.get("orderStatus", "")

                    # КРИТИЧНО: Если ордер исполнен на бирже - ВСЕГДА генерируем событие!
                    # Стратегия сама проверит через processed_orders - был ли ордер уже обработан
                    if exchange_status == "Filled":
                        # Обновляем статус в БД (если еще не FILLED)
                        if db_status != "FILLED":
                            log_warning(self.user_id,
                                       f"🔔 ПРОПУЩЕННОЕ СОБЫТИЕ (БД=NEW): Ордер {order_id} исполнен на бирже, обновляю БД и генерирую OrderFilledEvent...",
                                       module_name=__name__)

                            await db_manager.update_order_on_fill(
                                order_id=order_id,
                                filled_quantity=to_decimal(order_info.get("cumExecQty", "0")),
                                average_price=to_decimal(order_info.get("avgPrice", "0")),
                                commission=to_decimal(order_info.get("cumExecFee", "0"))
                            )
                        else:
                            # Ордер FILLED в БД, но событие могло быть потеряно!
                            log_warning(self.user_id,
                                       f"🔔 ВОССТАНОВЛЕНИЕ: Ордер {order_id} FILLED в БД, генерирую OrderFilledEvent для восстановления подписки...",
                                       module_name=__name__)

                        # КРИТИЧНО: ВСЕГДА генерируем событие для FILLED ордеров после переподключения!
                        # Стратегия сама проверит в processed_orders - был ли ордер уже обработан
                        filled_event = OrderFilledEvent(
                            user_id=self.user_id,
                            order_id=order_id,
                            symbol=symbol,
                            side=order_info.get("side"),
                            qty=to_decimal(order_info.get("cumExecQty", "0")),
                            price=to_decimal(order_info.get("avgPrice", "0")),
                            fee=to_decimal(order_info.get("cumExecFee", "0"))
                        )
                        await self.event_bus.publish(filled_event)

                        synced_count += 1
                        log_info(self.user_id, f"✅ Ордер {order_id} ({symbol}) - OrderFilledEvent отправлено в EventBus для восстановления", module_name=__name__)
                    else:
                        log_debug(self.user_id, f"○ Ордер {order_id} еще не исполнен (статус: {exchange_status})", module_name=__name__)

                except Exception as order_error:
                    log_error(self.user_id, f"❌ Ошибка синхронизации ордера {order_id}: {order_error}", module_name=__name__)
                    continue

            if synced_count > 0:
                log_info(self.user_id, f"🎯 Синхронизация завершена: восстановлено {synced_count} пропущенных событий", module_name=__name__)
            else:
                log_info(self.user_id, "✅ Синхронизация завершена: все ордера актуальны", module_name=__name__)

        except Exception as e:
            log_error(self.user_id, f"❌ Ошибка синхронизации ордеров после переподключения: {e}", module_name=__name__)

    async def _handle_order_update(self, data: List[Dict]):
        """
        МОНИТОРИНГ РУЧНЫХ ДЕЙСТВИЙ ПОЛЬЗОВАТЕЛЯ НА БИРЖЕ.

        АРХИТЕКТУРА СИСТЕМЫ:
        =====================
        ✅ Filled статусы → ТОЛЬКО через API (в _place_order() после 300ms)
        ⚠️ Cancelled/Rejected → ТОЛЬКО через WebSocket (ручная отмена пользователем)

        РОЛЬ ЭТОГО МЕТОДА:
        - Отслеживает когда пользователь ВРУЧНУЮ отменил/отклонил ордер бота на бирже
        - Обновляет статус в БД
        - Отправляет уведомление через Strategy handler

        КРИТИЧНО: НЕ обрабатывает Filled! Это создаёт дублирование с API polling!
        """
        try:
            for order_data in data:
                order_id = order_data.get("orderId")
                status = order_data.get("orderStatus")

                # ШАГ 1: Проверяем что это ордер БОТА (есть в БД)
                db_order = await db_manager.get_order_by_exchange_id(order_id, self.user_id)

                if not db_order:
                    # Это НЕ ордер бота - игнорируем (пользователь создал вручную)
                    log_debug(self.user_id,
                             f"⏭️ Пропускаю WebSocket событие для ордера {order_id} - не найден в БД (ручной ордер пользователя)",
                             "DataFeedHandler")
                    continue

                # ШАГ 2: КРИТИЧНО! Обрабатываем ТОЛЬКО ручную отмену/отклонение
                # Filled статусы обрабатываются через API в _place_order()!
                if status in ["Cancelled", "Rejected"]:
                    log_info(self.user_id,
                             f"⚠️ [WebSocket] Ордер {order_id} {status} ВРУЧНУЮ пользователем на бирже!",
                             "DataFeedHandler")

                    # Обновляем статус в БД
                    try:
                        status_map = {"Cancelled": "CANCELLED", "Rejected": "REJECTED"}
                        await db_manager.update_order_status(
                            order_id=order_id,
                            status=status_map[status],
                            filled_quantity=to_decimal(order_data.get("cumExecQty", "0")),
                            average_price=to_decimal(order_data.get("avgPrice", "0")) if order_data.get("avgPrice") else None
                        )
                        log_info(self.user_id,
                                f"✅ [WebSocket] Статус ордера {order_id} обновлён в БД: {status_map[status]}",
                                "DataFeedHandler")
                    except Exception as db_error:
                        log_error(self.user_id,
                                 f"❌ Ошибка обновления статуса ордера {order_id} в БД: {db_error}",
                                 "DataFeedHandler")

                    # Публикуем событие для Strategy handler (он отправит уведомление)
                    update_event = OrderUpdateEvent(
                        user_id=self.user_id,
                        order_data=order_data
                    )
                    await self.event_bus.publish(update_event)

                elif status == "Filled":
                    # Filled обрабатывается ТОЛЬКО через API в _place_order()!
                    # Игнорируем WebSocket Filled события для избежания дублирования
                    log_debug(self.user_id,
                             f"⏭️ [WebSocket] Игнорирую Filled для ордера {order_id} - обрабатывается через API",
                             "DataFeedHandler")
                    continue

        except Exception as e:
            log_error(self.user_id, f"Ошибка обработки обновления ордера: {e}", module_name=__name__)


    async def _handle_position_update(self, position_data: List[Dict]):
        """
        Обработка обновления позиции через WebSocket.
        КРИТИЧНО: Мгновенно обнаруживает ручное закрытие позиции пользователем (size=0).

        MULTI-ACCOUNT SUPPORT: Каждый DataFeedHandler подключён к своему аккаунту,
        поэтому проверяет только СВОЙ account_priority.
        """
        try:
            for position in position_data:
                symbol = position.get("symbol", "")
                size = Decimal(str(position.get("size", "0")))

                # КРИТИЧНО: Проверяем ручное закрытие позиции (size=0)
                if size == Decimal('0'):
                    # Позиция закрыта! Проверяем: это МЫ или ПОЛЬЗОВАТЕЛЬ?

                    # Шаг 1: Проверяем наличие НАШИХ CLOSE ордеров
                    has_our_close = await db_manager.has_pending_close_order(
                        self.user_id,
                        symbol,
                        bot_priority=self.account_priority
                    )

                    if has_our_close:
                        # Это ОЖИДАЕМОЕ ЗАКРЫТИЕ - мы сами создали CLOSE ордер
                        log_debug(self.user_id,
                                 f"✅ [ОЖИДАЕМОЕ ЗАКРЫТИЕ] Позиция {symbol} закрыта нашим CLOSE ордером (Bot_{self.account_priority})",
                                 module_name=__name__)
                        # НЕ публикуем событие ручного закрытия - это наш ордер!
                    else:
                        # Шаг 2: Нет наших CLOSE ордеров, проверяем есть ли незакрытая позиция
                        has_unclosed = await db_manager.has_unclosed_position(
                            self.user_id,
                            symbol,
                            bot_priority=self.account_priority
                        )

                        if has_unclosed:
                            # РУЧНОЕ ЗАКРЫТИЕ - есть OPEN без CLOSE, пользователь закрыл на бирже!
                            log_warning(self.user_id,
                                       f"⚠️ ОБНАРУЖЕНО РУЧНОЕ ЗАКРЫТИЕ через WebSocket (Bot_{self.account_priority}): "
                                       f"Позиция {symbol} закрыта (size=0), есть незакрытый OPEN ордер в БД!",
                                       module_name=__name__)

                            # Публикуем событие ручного закрытия
                            closed_event = PositionClosedEvent(
                                user_id=self.user_id,
                                symbol=symbol,
                                bot_priority=self.account_priority,
                                closed_manually=True
                            )
                            await self.event_bus.publish(closed_event)
                        else:
                            # Нет ни CLOSE ни незакрытого OPEN - позиция уже обработана
                            log_debug(self.user_id,
                                     f"ℹ️ Позиция {symbol} закрыта (size=0), позиция уже полностью обработана в БД (Bot_{self.account_priority})",
                                     module_name=__name__)

                # Публикуем обычное событие обновления позиции (для управления подписками)
                position_event = PositionUpdateEvent(
                    user_id=self.user_id,
                    symbol=symbol,
                    side=position.get("side", ""),
                    size=size,
                    entry_price=Decimal(str(position.get("avgPrice", "0"))),
                    mark_price=Decimal(str(position.get("markPrice", "0"))),
                    unrealized_pnl=Decimal(str(position.get("unrealisedPnl", "0")))
                )
                await self.event_bus.publish(position_event)

        except Exception as e:
            log_error(self.user_id, f"Ошибка обработки позиции: {e}", module_name=__name__)