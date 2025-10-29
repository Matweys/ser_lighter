# strategies/flash_drop_catcher_strategy.py
"""
🚀 Flash Drop Catcher Strategy - Стратегия ловли резких падений

РЕФАКТОРЕННАЯ ВЕРСИЯ с параметрами из оригинального сканера:
- Динамические пороги на основе волатильности
- Фильтр ликвидности (минимальный дневной объем)
- Фильтр всплеска объема (volume spike)
- Настраиваемый интервал свечей (1m, 15m, и т.д.)
"""
import asyncio
import json
import websockets
from decimal import Decimal, getcontext
from typing import Dict, Any, Optional, List
from datetime import datetime
from collections import defaultdict, deque
from aiogram import Bot
from core.logger import log_info, log_error, log_warning, log_debug
from core.enums import StrategyType, PositionSide, ConfigType
from core.events import EventType, EventBus, PriceUpdateEvent, OrderFilledEvent
from api.bybit_api import BybitAPI
from .base_strategy import BaseStrategy
from aiogram.utils.markdown import hbold, hcode
from core.concurrency_manager import strategy_locked
from cache.redis_manager import redis_manager

# Настройка точности для Decimal
getcontext().prec = 28


class FlashDropCatcherStrategy(BaseStrategy):
    """
    Стратегия для обнаружения резких падений и входа в LONG позиции.

    Логика работы (ОРИГИНАЛЬНАЯ ИЗ СКАНЕРА):
    1. Сканирует ВСЕ фьючерсные символы через WebSocket
    2. Фильтрует по ликвидности (минимальный дневной объем)
    3. Обнаруживает резкое падение (сравнивая с N-свечным средним)
    4. Проверяет всплеск объема (volume spike >= 3x среднего)
    5. Использует ДИНАМИЧЕСКИЙ порог падения на основе волатильности символа
    6. Входит в LONG на отскок
    7. Выходит по trailing stop или при достижении hard stop loss (-15$)
    """

    def __init__(self, user_id: int, symbol: str, signal_data: Dict[str, Any],
                 api: BybitAPI, event_bus: EventBus, bot: Bot, config: Optional[Dict] = None, account_priority: int = 1):
        """Инициализация стратегии Flash Drop Catcher"""
        super().__init__(user_id, symbol, signal_data, api, event_bus, bot, config, account_priority)

        # === ПАРАМЕТРЫ ИЗ КОНФИГУРАЦИИ (загружаются из Redis) ===
        # Эти значения будут установлены в _load_config()
        self.TIMEFRAME_INTERVAL: str = "15"  # По умолчанию 15-минутные свечи
        self.HISTORY_BARS: int = 7  # Количество свечей для истории
        self.BASE_DROP_PCT: Decimal = Decimal('0.05')  # 5% базовый порог
        self.MIN_DROP_PCT: Decimal = Decimal('0.03')  # 3% минимальный порог
        self.MAX_DROP_PCT: Decimal = Decimal('0.15')  # 15% максимальный порог
        self.VOLUME_SPIKE_MIN: Decimal = Decimal('3.0')  # 3x среднего объема
        self.MIN_DAILY_VOLUME_USD: Decimal = Decimal('1000000')  # $1M минимальный дневной объем
        self.WEBSOCKET_CHUNK_SIZE: int = 150  # Размер чанка для подписки

        # === ХРАНИЛИЩЕ ДАННЫХ ПО СИМВОЛАМ ===
        # Структура: {symbol: {'closes': deque, 'volumes': deque, 'highs': deque, 'lows': deque,
        #                       'volatility': float, 'avg_volume': float, 'dynamic_threshold': float}}
        self.symbol_data: Dict[str, Dict[str, Any]] = {}

        # WebSocket задача
        self._scanner_task: Optional[asyncio.Task] = None
        self._heartbeat_task: Optional[asyncio.Task] = None  # Задача для heartbeat мониторинга
        self._config_reload_task: Optional[asyncio.Task] = None  # Задача для периодической перезагрузки конфигурации
        self._ws_url = "wss://stream.bybit.com/v5/public/linear"

        # === ПАРАМЕТРЫ ТОРГОВЛИ ===
        self.MAX_CONCURRENT_POSITIONS = 2  # Максимум позиций одновременно (из конфига, динамический)

        # КРИТИЧНО: Словарь для отслеживания МНОЖЕСТВЕННЫХ активных позиций
        # Каждая позиция отслеживается независимо с полным набором данных
        # Структура: {
        #   symbol: {
        #     'entry_price': Decimal,           # Цена входа
        #     'entry_time': datetime,           # Время входа
        #     'position_size': Decimal,         # Размер позиции
        #     'order_id': str,                  # ID ордера открытия для отслеживания в БД
        #     'highest_pnl': Decimal,           # Максимальный PnL для trailing stop
        #     'current_trailing_level': int,    # Текущий уровень trailing stop
        #     'last_trailing_notification_level': int,  # Последний уведомленный уровень
        #     'signal_drop_percent': Decimal,   # Процент падения сигнала
        #     'signal_volume_ratio': Decimal,   # Коэффициент всплеска объёма
        #     'signal_volatility_pct': Decimal  # Волатильность на момент сигнала
        #   }
        # }
        self.active_flash_positions: Dict[str, Dict[str, Any]] = {}

        # КРИТИЧНО: Защита от двойной обработки ордеров и отслеживание состояния
        self.processed_orders: set = set()  # Множество обработанных order_id
        self.pending_orders: Dict[str, str] = {}  # {order_id: symbol} - ожидающие исполнения OPEN ордера
        self._last_known_price: Optional[Decimal] = None  # КРИТИЧНО: Последняя известная цена для fallback расчёта PnL

        # ВАЖНО: self.symbol ВСЕГДА равен "ALL" для этой стратегии!
        # Стратегия сканирует ВСЕ символы и работает с множеством позиций параллельно
        # self.symbol меняется ВРЕМЕННО только при вызове _place_order() для корректной работы базового класса

        # Hard stop loss при -15$ (из конфига)
        self.HARD_STOP_LOSS_USDT = Decimal('-15.0')

        # Список отфильтрованных ликвидных символов
        self._liquid_symbols: List[str] = []

        # === HEARTBEAT МОНИТОРИНГ ===
        self.last_heartbeat_time = datetime.now()
        self.processed_candles_count = 0  # Счётчик обработанных свечей
        self.detected_drops_count = 0  # Счётчик обнаруженных падений (прошли все фильтры)
        self.rejected_due_to_position_exists = 0  # Отклонено: уже есть позиция на символ
        self.rejected_due_to_max_positions = 0  # Отклонено: достигнут лимит позиций
        self.trades_opened = 0  # Успешно открыто сделок

        log_info(self.user_id,
                f"🚀 FlashDropCatcher инициализирована для {self.symbol}",
                "FlashDropCatcher")

    def _get_strategy_type(self) -> StrategyType:
        """Возвращает тип стратегии"""
        return StrategyType.FLASH_DROP_CATCHER

    async def _load_strategy_config(self):
        """
        Загрузка конфигурации из Redis и установка параметров.

        ✅ КРИТИЧНО: Метод назван _load_strategy_config() (не _load_config()!)
        чтобы ПЕРЕОПРЕДЕЛИТЬ базовый метод и обновляться при _force_config_reload().
        """
        await super()._load_strategy_config()

        # Загружаем параметры из конфигурации (ВАЖНО: дефолты должны совпадать с default_configs.py!)
        self.TIMEFRAME_INTERVAL = str(self.get_config_value("timeframe_interval", "15"))
        self.HISTORY_BARS = int(float(self.get_config_value("candle_history_size", 12)))  # ИСПРАВЛЕНО: 7 → 12
        self.BASE_DROP_PCT = self._convert_to_decimal(self.get_config_value("base_drop_percent", 4.0)) / Decimal('100')  # ИСПРАВЛЕНО: 5.0 → 4.0
        self.MIN_DROP_PCT = self._convert_to_decimal(self.get_config_value("min_drop_percent", 2.5)) / Decimal('100')  # ИСПРАВЛЕНО: 3.0 → 2.5
        self.MAX_DROP_PCT = self._convert_to_decimal(self.get_config_value("max_drop_percent", 10.0)) / Decimal('100')  # ИСПРАВЛЕНО: 15.0 → 10.0
        self.VOLUME_SPIKE_MIN = self._convert_to_decimal(self.get_config_value("volume_spike_min", 2.5))  # ИСПРАВЛЕНО: 3.0 → 2.5
        self.MIN_DAILY_VOLUME_USD = self._convert_to_decimal(self.get_config_value("min_daily_volume_usd", 1000000.0))
        self.MAX_CONCURRENT_POSITIONS = int(float(self.get_config_value("max_concurrent_positions", 2)))
        self.HARD_STOP_LOSS_USDT = self._convert_to_decimal(self.get_config_value("hard_stop_loss_usdt", -500.0))  # ИСПРАВЛЕНО: -15.0 → -500.0
        self.WEBSOCKET_CHUNK_SIZE = int(float(self.get_config_value("websocket_chunk_size", 150)))
        self.ENABLE_HEARTBEAT = bool(self.get_config_value("enable_heartbeat_notifications", True))  # Heartbeat уведомления

        log_info(self.user_id,
                f"📋 Параметры FlashDropCatcher: интервал={self.TIMEFRAME_INTERVAL}m, история={self.HISTORY_BARS}, "
                f"базовый порог={float(self.BASE_DROP_PCT)*100:.1f}%, макс={float(self.MAX_DROP_PCT)*100:.1f}%, объем={self.VOLUME_SPIKE_MIN}x",
                "FlashDropCatcher")

    async def start(self):
        """Запуск стратегии"""
        if self.is_running:
            log_warning(self.user_id, "FlashDropCatcher уже запущена", "FlashDropCatcher")
            return True

        self.is_running = True

        # Загружаем конфигурацию
        await self._load_strategy_config()

        # КРИТИЧНО: Подписываемся на обновления цены для мониторинга активной позиции
        await self.event_bus.subscribe(EventType.PRICE_UPDATE, self.handle_price_update, user_id=self.user_id)

        # КРИТИЧНО: Подписываемся на ручное закрытие позиции
        from core.events import PositionClosedEvent
        await self.event_bus.subscribe(EventType.POSITION_CLOSED, self._handle_manual_close, user_id=self.user_id)

        # Запускаем WebSocket сканер
        self._scanner_task = asyncio.create_task(self._run_websocket_scanner())

        # Запускаем heartbeat мониторинг
        self._heartbeat_task = asyncio.create_task(self._run_heartbeat_monitor())

        # ✅ КРИТИЧНО: Запускаем периодическую перезагрузку конфигурации (каждые 5 минут)
        # Это гарантирует что настройки обновляются даже если нет сигналов!
        self._config_reload_task = asyncio.create_task(self._run_config_reload_monitor())

        log_info(self.user_id,
                f"✅ FlashDropCatcher запущена! Сканирование всех символов на падения...",
                "FlashDropCatcher")

        return True

    async def stop(self, reason: str = "Manual stop") -> bool:
        """Остановка стратегии"""
        if not self.is_running:
            return True

        self.is_running = False

        # Останавливаем WebSocket сканер
        if self._scanner_task and not self._scanner_task.done():
            self._scanner_task.cancel()
            try:
                await self._scanner_task
            except asyncio.CancelledError:
                pass

        # Останавливаем heartbeat мониторинг
        if self._heartbeat_task and not self._heartbeat_task.done():
            self._heartbeat_task.cancel()
            try:
                await self._heartbeat_task
            except asyncio.CancelledError:
                pass

        # Останавливаем периодическую перезагрузку конфигурации
        if self._config_reload_task and not self._config_reload_task.done():
            self._config_reload_task.cancel()
            try:
                await self._config_reload_task
            except asyncio.CancelledError:
                pass

        # КРИТИЧНО: НЕ отписываемся от PRICE_UPDATE вручную!
        # BaseStrategy автоматически управляет подпиской через handle_event

        log_info(self.user_id, f"⏹️ FlashDropCatcher остановлена: {reason}", "FlashDropCatcher")
        return True

    # ============================================================================
    # === ВСПОМОГАТЕЛЬНЫЕ ФУНКЦИИ (ИЗ ОРИГИНАЛЬНОГО СКАНЕРА) ===
    # ============================================================================

    @staticmethod
    def _calculate_volatility(prices: List[Decimal]) -> Decimal:
        """
        Вычисляет волатильность (стандартное отклонение процентных изменений).
        ОРИГИНАЛЬНАЯ ЛОГИКА ИЗ СКАНЕРА - НЕ ИЗМЕНЯТЬ ЧИСЛОВЫЕ ЗНАЧЕНИЯ!
        """
        if len(prices) < 2:
            return Decimal('0')

        # Рассчитываем процентные изменения
        returns = []
        for i in range(1, len(prices)):
            if prices[i-1] != Decimal('0'):
                ret = (prices[i] - prices[i-1]) / prices[i-1]
                returns.append(ret)

        if not returns:
            return Decimal('0')

        # Среднее значение доходности
        mean_return = sum(returns) / len(returns)

        # Дисперсия
        variance = sum((r - mean_return) ** 2 for r in returns) / len(returns)

        # Стандартное отклонение (волатильность)
        volatility = variance ** Decimal('0.5')

        return volatility

    async def _get_liquidity_filter(self) -> List[str]:
        """
        Фильтрует символы по ликвидности (дневной объем).
        Исключает Pre-Market символы для демо режима.
        ОРИГИНАЛЬНАЯ ЛОГИКА ИЗ СКАНЕРА.
        """
        log_info(self.user_id, "🔍 Применение фильтра ликвидности...", "FlashDropCatcher")

        try:
            # Получаем тикеры ВСЕХ символов одним запросом через публичный метод API
            tickers = await self.api.get_all_tickers()

            if not tickers:
                log_error(self.user_id, "Не удалось получить тикеры для фильтра ликвидности", "FlashDropCatcher")
                return []

            # Получаем информацию о символах для проверки Pre-Market статуса
            instruments_info = await self.api.get_instruments_info()

            liquid_symbols = []

            for ticker in tickers:
                symbol = ticker.get("symbol", "")

                # Проверяем что это USDT futures
                if not symbol.endswith("USDT"):
                    continue

                # Фильтр Pre-Market символов (нельзя торговать в демо режиме)
                if instruments_info and symbol in instruments_info:
                    symbol_info = instruments_info[symbol]
                    # Проверяем статус - Pre-Market символы имеют статус отличный от "Trading"
                    # или содержат contractType = "PreMarket"
                    contract_type = symbol_info.get("contractType", "")
                    if contract_type == "PreMarket":
                        log_debug(self.user_id, f"Пропуск Pre-Market символа: {symbol}", "FlashDropCatcher")
                        continue

                # Дневной объем в USD (turnover24h)
                daily_volume = self._convert_to_decimal(ticker.get("turnover24h", 0))

                if daily_volume >= self.MIN_DAILY_VOLUME_USD:
                    liquid_symbols.append(symbol)

            log_info(self.user_id,
                    f"✅ Отфильтровано {len(liquid_symbols)} ликвидных символов (мин. объем: ${float(self.MIN_DAILY_VOLUME_USD):,.0f}, исключены Pre-Market)",
                    "FlashDropCatcher")

            return sorted(liquid_symbols)

        except Exception as e:
            log_error(self.user_id, f"Ошибка фильтрации ликвидности: {e}", "FlashDropCatcher")
            return []

    async def _prime_history(self, symbol: str):
        """
        Загружает начальную историю и вычисляет динамические параметры для символа.
        ОРИГИНАЛЬНАЯ ЛОГИКА ИЗ СКАНЕРА - НЕ ИЗМЕНЯТЬ ЧИСЛОВЫЕ ЗНАЧЕНИЯ!
        """
        try:
            # Загружаем OHLCV свечи (используем правильное имя метода get_klines)
            ohlcv_response = await self.api.get_klines(
                symbol=symbol,
                interval=f"{self.TIMEFRAME_INTERVAL}m",  # Добавляем "m" для минут
                limit=self.HISTORY_BARS
            )

            if not ohlcv_response:
                # Инициализируем пустые данные
                self.symbol_data[symbol] = {
                    'closes': deque(maxlen=self.HISTORY_BARS),
                    'volumes': deque(maxlen=self.HISTORY_BARS),
                    'highs': deque(maxlen=self.HISTORY_BARS),
                    'lows': deque(maxlen=self.HISTORY_BARS),
                    'volatility': Decimal('0'),
                    'avg_volume': Decimal('0'),
                    'dynamic_threshold': self.BASE_DROP_PCT
                }
                return

            klines = ohlcv_response

            if len(klines) >= self.HISTORY_BARS:
                # get_klines() возвращает список словарей, уже отсортированный от старых к новым
                # Данные уже в формате Decimal
                closes = [k["close"] for k in klines]
                volumes = [k["volume"] for k in klines]
                highs = [k["high"] for k in klines]
                lows = [k["low"] for k in klines]

                # Вычисляем волатильность для динамического порога
                volatility = self._calculate_volatility(closes)

                # Динамический порог: чем выше волатильность, тем выше требуемое падение
                # Для низковолатильных монет (BTC, ETH) - меньше порог
                # Для высоковолатильных (мемкоины) - больше порог
                # ОПТИМИЗИРОВАННАЯ ФОРМУЛА: BASE_DROP_PCT + (volatility * 4) - ВАРИАНТ 1 (Сбалансированный)
                dynamic_threshold = self.BASE_DROP_PCT + (volatility * Decimal('4'))
                # Ограничиваем min-max (НЕ блокирует сильные падения, только ограничивает расчет порога!)
                dynamic_threshold = max(self.MIN_DROP_PCT, min(dynamic_threshold, self.MAX_DROP_PCT))

                avg_volume = sum(volumes) / len(volumes) if volumes else Decimal('0')

                self.symbol_data[symbol] = {
                    'closes': deque(closes, maxlen=self.HISTORY_BARS),
                    'volumes': deque(volumes, maxlen=self.HISTORY_BARS),
                    'highs': deque(highs, maxlen=self.HISTORY_BARS),
                    'lows': deque(lows, maxlen=self.HISTORY_BARS),
                    'volatility': volatility,
                    'avg_volume': avg_volume,
                    'dynamic_threshold': dynamic_threshold
                }
            else:
                # Недостаточно данных
                self.symbol_data[symbol] = {
                    'closes': deque(maxlen=self.HISTORY_BARS),
                    'volumes': deque(maxlen=self.HISTORY_BARS),
                    'highs': deque(maxlen=self.HISTORY_BARS),
                    'lows': deque(maxlen=self.HISTORY_BARS),
                    'volatility': Decimal('0'),
                    'avg_volume': Decimal('0'),
                    'dynamic_threshold': self.BASE_DROP_PCT
                }

        except Exception as e:
            log_error(self.user_id, f"Ошибка загрузки истории для {symbol}: {e}", "FlashDropCatcher")
            # Инициализируем пустые данные при ошибке
            self.symbol_data[symbol] = {
                'closes': deque(maxlen=self.HISTORY_BARS),
                'volumes': deque(maxlen=self.HISTORY_BARS),
                'highs': deque(maxlen=self.HISTORY_BARS),
                'lows': deque(maxlen=self.HISTORY_BARS),
                'volatility': Decimal('0'),
                'avg_volume': Decimal('0'),
                'dynamic_threshold': self.BASE_DROP_PCT
            }

    # ============================================================================
    # === WEBSOCKET СКАНЕР (РЕФАКТОРЕННАЯ ВЕРСИЯ) ===
    # ============================================================================

    async def _run_websocket_scanner(self):
        """
        РЕФАКТОРЕННАЯ ЛОГИКА СКАНЕРА с фильтрами из оригинала.
        Подключается к WebSocket и мониторит ликвидные символы на резкие падения
        """
        while self.is_running:
            try:
                log_info(self.user_id, "🔌 Подключение к WebSocket сканеру...", "FlashDropCatcher")

                # Получаем список всех фьючерсных символов
                all_symbols = await self._get_all_futures_symbols()

                if not all_symbols:
                    log_error(self.user_id, "Не удалось получить список символов", "FlashDropCatcher")
                    await asyncio.sleep(10)
                    continue

                # Применяем фильтр ликвидности (оставляем только топ-монеты)
                self._liquid_symbols = await self._get_liquidity_filter()

                if not self._liquid_symbols:
                    log_warning(self.user_id,
                               f"После фильтрации не осталось символов. Используем все {len(all_symbols)} символов.",
                               "FlashDropCatcher")
                    self._liquid_symbols = all_symbols

                log_info(self.user_id,
                        f"📊 Будет отслеживаться {len(self._liquid_symbols)} ликвидных символов из {len(all_symbols)} доступных",
                        "FlashDropCatcher")

                # Загружаем историю и вычисляем динамические параметры для каждого символа
                log_info(self.user_id,
                        f"📥 Загрузка начальной истории для {len(self._liquid_symbols)} символов...",
                        "FlashDropCatcher")

                # Загружаем историю параллельно (чанками для безопасности)
                chunk_size = 50
                for i in range(0, len(self._liquid_symbols), chunk_size):
                    chunk = self._liquid_symbols[i:i + chunk_size]
                    tasks = [self._prime_history(sym) for sym in chunk]
                    await asyncio.gather(*tasks, return_exceptions=True)

                initialized_count = sum(1 for s in self._liquid_symbols
                                       if s in self.symbol_data and len(self.symbol_data[s]['closes']) >= self.HISTORY_BARS)

                log_info(self.user_id,
                        f"✅ История загружена. Инициализировано: {initialized_count}/{len(self._liquid_symbols)} символов",
                        "FlashDropCatcher")

                # Подключаемся к WebSocket
                async with websockets.connect(self._ws_url) as ws:
                    # Подписываемся на kline свечи для отфильтрованных символов (чанками)
                    for i in range(0, len(self._liquid_symbols), self.WEBSOCKET_CHUNK_SIZE):
                        chunk = self._liquid_symbols[i:i + self.WEBSOCKET_CHUNK_SIZE]
                        topics = [f"kline.{self.TIMEFRAME_INTERVAL}.{s}" for s in chunk]

                        subscribe_message = {
                            "op": "subscribe",
                            "args": topics
                        }

                        await ws.send(json.dumps(subscribe_message))
                        await asyncio.sleep(0.2)  # Небольшая задержка между чанками

                    log_info(self.user_id,
                            f"✅ Подписка на {len(self._liquid_symbols)} символов для мониторинга падений (интервал: {self.TIMEFRAME_INTERVAL}m)",
                            "FlashDropCatcher")

                    # Обрабатываем входящие сообщения
                    async for message in ws:
                        if not self.is_running:
                            break

                        await self._process_websocket_message(message)

            except asyncio.CancelledError:
                log_info(self.user_id, "WebSocket сканер отменен", "FlashDropCatcher")
                break
            except Exception as e:
                log_error(self.user_id, f"Ошибка WebSocket сканера: {e}", "FlashDropCatcher")
                if self.is_running:
                    log_info(self.user_id, "Переподключение через 5 секунд...", "FlashDropCatcher")
                    await asyncio.sleep(5)

    async def _get_all_futures_symbols(self) -> List[str]:
        """
        Получает список всех доступных фьючерсных символов
        """
        try:
            # get_instruments_info() возвращает словарь {symbol: info}
            response = await self.api.get_instruments_info()

            if response and isinstance(response, dict):
                # Фильтруем по статусу Trading и USDT
                symbols = [symbol for symbol, info in response.items()
                          if info.get("status") == "Trading" and symbol.endswith("USDT")]

                log_info(self.user_id,
                        f"📊 Получено {len(symbols)} торгуемых USDT фьючерсов",
                        "FlashDropCatcher")
                return symbols

            return []

        except Exception as e:
            log_error(self.user_id, f"Ошибка получения списка символов: {e}", "FlashDropCatcher")
            return []

    async def _process_websocket_message(self, message: str):
        """
        РЕФАКТОРЕННАЯ ЛОГИКА: Обрабатывает сообщения WebSocket с отслеживанием ОБЪЕМА
        """
        try:
            data = json.loads(message)

            # Ping-pong для поддержания соединения
            if data.get("op") == "ping":
                # Не нужно отвечать, Bybit не требует pong

                return

            # Проверяем, что это обновление свечи
            topic = data.get("topic", "")
            if not topic.startswith(f"kline.{self.TIMEFRAME_INTERVAL}."):
                return

            kline_data = data.get("data", [])
            if not kline_data:
                return

            for candle in kline_data:
                symbol = topic.split(".")[-1]

                # Проверяем, что свеча закрыта (confirm=True)
                if not candle.get("confirm", False):
                    continue

                close_price = self._convert_to_decimal(candle["close"])
                volume = self._convert_to_decimal(candle["volume"])
                high = self._convert_to_decimal(candle["high"])
                low = self._convert_to_decimal(candle["low"])

                # Добавляем данные в историю символа
                if symbol in self.symbol_data:
                    data_obj = self.symbol_data[symbol]
                    data_obj['closes'].append(close_price)
                    data_obj['volumes'].append(volume)
                    data_obj['highs'].append(high)
                    data_obj['lows'].append(low)

                    # Увеличиваем счётчик обработанных свечей
                    self.processed_candles_count += 1

                    # Пересчитываем волатильность и динамический порог
                    closes_list = list(data_obj['closes'])
                    if len(closes_list) >= self.HISTORY_BARS:
                        data_obj['volatility'] = self._calculate_volatility(closes_list)

                        # ОПТИМИЗИРОВАННАЯ ФОРМУЛА: BASE_DROP_PCT + (volatility * 4) - ВАРИАНТ 1 (Сбалансированный)
                        data_obj['dynamic_threshold'] = self.BASE_DROP_PCT + (data_obj['volatility'] * Decimal('4'))
                        data_obj['dynamic_threshold'] = max(self.MIN_DROP_PCT, min(data_obj['dynamic_threshold'], self.MAX_DROP_PCT))

                    # Проверяем падение с НОВЫМИ ФИЛЬТРАМИ
                    await self._check_for_drop(symbol)

        except json.JSONDecodeError:
            pass  # Игнорируем некорректные JSON
        except Exception as e:
            log_error(self.user_id, f"Ошибка обработки WebSocket сообщения: {e}", "FlashDropCatcher")

    async def _check_for_drop(self, symbol: str):
        """
        РЕФАКТОРЕННАЯ ЛОГИКА: Проверяет падение с ДИНАМИЧЕСКИМИ ФИЛЬТРАМИ.
        ОРИГИНАЛЬНАЯ ЛОГИКА ИЗ СКАНЕРА - НЕ ИЗМЕНЯТЬ ЧИСЛОВЫЕ ЗНАЧЕНИЯ!

        Фильтры (все должны пройти):
        1. Падение >= динамического порога (на основе волатильности)
        2. Всплеск объема >= VOLUME_SPIKE_MIN (3x среднего)
        """
        try:
            data = self.symbol_data.get(symbol)
            if not data or not isinstance(data, dict):
                return

            closes = list(data['closes'])
            volumes = list(data['volumes'])

            if len(closes) < self.HISTORY_BARS or len(volumes) < self.HISTORY_BARS:
                return

            # Вычисляем среднюю цену за предыдущие N-1 свечей (без текущей)
            prev_closes = closes[:-1]
            last_close = closes[-1]

            prev_volumes = volumes[:-1]
            current_volume = volumes[-1]

            if len(prev_closes) < 2:
                return

            avg_prev_price = sum(prev_closes) / len(prev_closes)

            if avg_prev_price == Decimal('0'):
                return

            # 1. Проверка падения относительно средней цены
            rel_drop = (avg_prev_price - last_close) / avg_prev_price

            # 2. Используем динамический порог для каждого символа
            dynamic_threshold = data.get('dynamic_threshold', self.BASE_DROP_PCT)

            if rel_drop < dynamic_threshold:
                return  # Падение недостаточное

            # 3. Проверка объема (должен быть >= VOLUME_SPIKE_MIN * среднего)
            avg_prev_volume = sum(prev_volumes) / len(prev_volumes) if prev_volumes else Decimal('1')
            volume_ratio = current_volume / avg_prev_volume if avg_prev_volume > Decimal('0') else Decimal('0')

            if volume_ratio < self.VOLUME_SPIKE_MIN:
                return  # Объем слишком низкий - игнорируем сигнал

            # 4. Все фильтры пройдены - генерируем качественный сигнал!
            drop_pct = rel_drop * Decimal('100')
            volatility_pct = data.get('volatility', Decimal('0')) * Decimal('100')

            # Увеличиваем счётчик обнаруженных падений
            self.detected_drops_count += 1

            log_warning(self.user_id,
                       f"🎯 КАЧЕСТВЕННЫЙ СИГНАЛ: {symbol} | "
                       f"Падение: {float(drop_pct):.2f}% (порог: {float(dynamic_threshold)*100:.2f}%) | "
                       f"Объем: {float(volume_ratio):.2f}x среднего | "
                       f"Волатильность: {float(volatility_pct):.3f}%",
                       "FlashDropCatcher")

            # Обрабатываем сигнал
            await self._handle_drop_signal(symbol, last_close, rel_drop, volume_ratio, volatility_pct)

        except Exception as e:
            log_error(self.user_id, f"Ошибка проверки падения для {symbol}: {e}", "FlashDropCatcher")

    # ============================================================================
    # === ТОРГОВАЯ ЛОГИКА (ИНТЕГРАЦИЯ) ===
    # ============================================================================

    async def _is_symbol_in_signal_scalper_watchlist(self, symbol: str) -> bool:
        """
        Проверяет, находится ли символ в вайтлисте signal_scalper стратегии.

        ✅ КРИТИЧНО: Flash Drop Catcher НЕ ДОЛЖНА открывать позиции на символах,
        которые уже торгуются signal_scalper стратегией (находятся в вайтлисте).

        Args:
            symbol: Символ для проверки

        Returns:
            True если символ В вайтлисте signal_scalper, False если НЕ в вайтлисте
        """
        try:
            # Получаем глобальную конфигурацию пользователя из Redis
            global_config = await redis_manager.get_config(self.user_id, ConfigType.GLOBAL)

            if not global_config:
                log_debug(self.user_id, "Глобальная конфигурация не найдена, вайтлист пуст", "FlashDropCatcher")
                return False

            # Получаем вайтлист символов (watchlist_symbols)
            watchlist = global_config.get("watchlist_symbols", [])

            if not watchlist or not isinstance(watchlist, list):
                log_debug(self.user_id, "Вайтлист пуст или имеет неверный формат", "FlashDropCatcher")
                return False

            # Проверяем наличие символа в вайтлисте
            is_in_watchlist = symbol in watchlist

            if is_in_watchlist:
                log_debug(self.user_id,
                         f"✅ Символ {symbol} найден в signal_scalper вайтлисте: {watchlist}",
                         "FlashDropCatcher")

            return is_in_watchlist

        except Exception as e:
            log_error(self.user_id, f"Ошибка проверки вайтлиста для {symbol}: {e}", "FlashDropCatcher")
            # При ошибке возвращаем False (разрешаем торговать), чтобы не блокировать стратегию
            return False

    async def _handle_drop_signal(self, symbol: str, price: Decimal, drop_pct: Decimal,
                                  volume_ratio: Decimal, volatility_pct: Decimal):
        """
        Обрабатывает сигнал резкого падения с проверкой лимитов.
        """
        try:
            # ✅ КРИТИЧНО: Перезагружаем конфигурацию ПЕРЕД входом в сделку
            # Это гарантирует, что используются АКТУАЛЬНЫЕ настройки из Redis
            await self._force_config_reload()

            # Проверка 0: НЕ ТОРГОВАТЬ символами из signal_scalper вайтлиста
            if await self._is_symbol_in_signal_scalper_watchlist(symbol):
                log_warning(self.user_id,
                           f"⚠️ Пропускаем сигнал {symbol} - символ в вайтлисте signal_scalper!",
                           "FlashDropCatcher")
                return

            # Проверка 1: Проверяем, есть ли уже позиция на этот символ (в НАШЕМ словаре)
            if symbol in self.active_flash_positions:
                self.rejected_due_to_position_exists += 1
                log_warning(self.user_id,
                           f"⚠️ Пропускаем сигнал {symbol} - уже есть открытая позиция!",
                           "FlashDropCatcher")
                return

            # Проверка 2: Достигнут ли лимит одновременных позиций (считаем СВОИ позиции из словаря)
            open_positions_count = len(self.active_flash_positions)
            if open_positions_count >= self.MAX_CONCURRENT_POSITIONS:
                self.rejected_due_to_max_positions += 1
                log_warning(self.user_id,
                           f"⚠️ Пропускаем сигнал {symbol} - достигнут лимит позиций ({open_positions_count}/{self.MAX_CONCURRENT_POSITIONS})",
                           "FlashDropCatcher")
                return

            # Проверка 3: Проверяем, что это наш символ (если стратегия работает для конкретного символа)
            if self.symbol != "ALL" and symbol != self.symbol:
                log_debug(self.user_id,
                         f"⏩ Пропускаем сигнал {symbol} - стратегия настроена только для {self.symbol}",
                         "FlashDropCatcher")
                return

            # Генерируем детальное сообщение сигнала
            drop_percent = drop_pct * Decimal('100')

            # Отправляем уведомление пользователю
            if self.bot:
                await self.bot.send_message(
                    self.user_id,
                    f"{hbold('🎯 КАЧЕСТВЕННЫЙ СИГНАЛ')}\n\n"
                    f"Символ: {hcode(symbol)}\n"
                    f"Цена: {hcode(f'{price:.8f}')}\n"
                    f"📉 Падение: {hcode(f'{float(drop_percent):.2f}%')}\n"
                    f"📊 Объем: {hcode(f'{float(volume_ratio):.2f}x среднего')}\n"
                    f"📈 Волатильность: {hcode(f'{float(volatility_pct):.3f}%')}\n\n"
                    f"Открываем LONG позицию..."
                )

            # ❌ НЕ меняем self.symbol здесь! Изменение ТОЛЬКО после успешного открытия ордера в _open_long_position

            # Открываем LONG позицию (передаём информацию о сигнале + symbol)
            await self._open_long_position(symbol, price, drop_percent, volume_ratio, volatility_pct)

        except Exception as e:
            log_error(self.user_id, f"Ошибка обработки сигнала падения: {e}", "FlashDropCatcher")

    async def _open_long_position(self, symbol: str, entry_price: Decimal, drop_percent: Decimal, volume_ratio: Decimal, volatility_pct: Decimal):
        """Открывает LONG позицию по текущей цене"""
        # ✅ КРИТИЧНО: Сохраняем старое значение self.symbol ДО try блока для восстановления при ошибке
        old_symbol = self.symbol

        try:
            # Сохраняем информацию о сигнале для уведомления
            self.signal_drop_percent = drop_percent
            self.signal_volume_ratio = volume_ratio
            self.signal_volatility_pct = volatility_pct

            # Получаем параметры из конфигурации
            order_amount = self._convert_to_decimal(self.get_config_value("order_amount", 200.0))
            leverage = self._convert_to_decimal(self.get_config_value("leverage", 2))

            # Устанавливаем плечо (используем параметр symbol, НЕ self.symbol!)
            await self.api.set_leverage(symbol=symbol, leverage=int(leverage))

            # ИСПРАВЛЕНИЕ: Используем calculate_quantity_from_usdt() как в signal_scalper
            # Этот метод правильно округляет количество по qtyStep
            position_size = await self.api.calculate_quantity_from_usdt(symbol, order_amount, leverage, price=entry_price)

            if position_size <= Decimal('0'):
                log_error(self.user_id, f"Не удалось рассчитать количество для {symbol}", "FlashDropCatcher")
                return

            log_info(self.user_id,
                    f"✅ Рассчитано количество: {position_size} для {symbol} (order_amount={order_amount}, leverage={leverage}, price={entry_price})",
                    "FlashDropCatcher")

            # Открываем LONG позицию (логируем symbol, НЕ self.symbol!)
            log_info(self.user_id,
                    f"📈 Открываем LONG: {symbol}, размер: {position_size}, плечо: {leverage}x",
                    "FlashDropCatcher")

            # ✅ КРИТИЧНО: Меняем self.symbol ПЕРЕД вызовом _place_order()!
            # Метод _place_order() использует self.symbol внутри себя для:
            # 1. client_order_id генерации (строка 817 в base_strategy.py)
            # 2. Сохранения в БД (строка 837)
            # 3. Вызова API (строка 867)
            self.symbol = symbol
            log_info(self.user_id, f"✅ self.symbol временно изменён с '{old_symbol}' на '{self.symbol}' перед вызовом _place_order()", "FlashDropCatcher")

            # ИСПРАВЛЕНИЕ: Используем _place_order из базового класса для сохранения в БД!
            order_result = await self._place_order(
                side="Buy",
                order_type="Market",
                qty=Decimal(str(position_size))
            )

            # place_order() возвращает order_id (строку), а не словарь
            if order_result:
                # ✅ Ордер успешно размещен, symbol остается измененным
                self.trades_opened += 1  # Увеличиваем счётчик успешно открытых сделок
                self.position_active = True
                self.entry_time = datetime.now()  # Сохраняем время открытия позиции
                self.position_size = position_size
                self.active_direction = "LONG"

                log_info(self.user_id, f"✅ Ордер успешно размещен, self.symbol остается {self.symbol}", "FlashDropCatcher")

                # КРИТИЧНО: Получаем РЕАЛЬНУЮ цену исполнения с биржи (не используем цену сигнала!)
                try:
                    # Даём бирже время обработать ордер
                    await asyncio.sleep(0.5)

                    # Получаем реальную позицию с биржи (теперь можем использовать self.symbol)
                    positions = await self.api.get_positions(symbol=self.symbol)
                    if positions and isinstance(positions, list) and len(positions) > 0:
                        # avgPrice - это РЕАЛЬНАЯ средняя цена исполнения ордера
                        real_entry_price = self._convert_to_decimal(positions[0].get("avgPrice", entry_price))

                        if real_entry_price > Decimal('0'):
                            self.entry_price = real_entry_price

                            # Рассчитываем проскальзывание для логирования
                            slippage = ((real_entry_price - entry_price) / entry_price) * Decimal('100')

                            log_info(self.user_id,
                                    f"✅ РЕАЛЬНАЯ цена исполнения: {real_entry_price:.8f} "
                                    f"(сигнал: {entry_price:.8f}, проскальзывание: {float(slippage):.2f}%)",
                                    "FlashDropCatcher")
                        else:
                            log_warning(self.user_id,
                                       f"⚠️ Не удалось получить avgPrice с биржи, используем цену сигнала {entry_price}",
                                       "FlashDropCatcher")
                            self.entry_price = entry_price
                    else:
                        log_warning(self.user_id,
                                   f"⚠️ Позиция не найдена на бирже, используем цену сигнала {entry_price}",
                                   "FlashDropCatcher")
                        self.entry_price = entry_price

                except Exception as price_fetch_error:
                    log_error(self.user_id,
                             f"❌ Ошибка получения реальной цены с биржи: {price_fetch_error}, используем цену сигнала {entry_price}",
                             "FlashDropCatcher")
                    self.entry_price = entry_price

                # КРИТИЧНО: Создаём trade в БД СРАЗУ после исполнения OPEN ордера (НЕ в уведомлении!)
                from database.db_trades import db_manager, TradeRecord
                from datetime import timezone as tz
                try:
                    new_trade = TradeRecord(
                        user_id=self.user_id,
                        symbol=symbol,  # Используем symbol (не self.symbol!)
                        side="Buy",  # FlashDropCatcher всегда открывает LONG
                        entry_price=self.entry_price,
                        quantity=position_size,
                        leverage=int(float(leverage)),
                        status="ACTIVE",
                        strategy_type=self.strategy_type.value,
                        entry_time=datetime.now(tz.utc),
                        profit=Decimal('0'),
                        commission=Decimal('0')  # Комиссия будет обновлена при закрытии
                    )
                    trade_id = await db_manager.save_trade(new_trade)
                    if trade_id:
                        log_info(self.user_id, f"✅ Trade создан в БД: trade_id={trade_id} для {symbol}", "FlashDropCatcher")

                        # Связываем OPEN ордер со сделкой
                        await db_manager.update_order_trade_id(order_result, trade_id)

                        # КРИТИЧНО: Сохраняем trade_id для обновления при закрытии
                        created_trade_id = trade_id
                    else:
                        log_error(self.user_id, f"❌ Не удалось создать trade в БД для {symbol}!", "FlashDropCatcher")
                        created_trade_id = None
                except Exception as trade_error:
                    log_error(self.user_id, f"❌ Ошибка создания trade в БД для {symbol}: {trade_error}", "FlashDropCatcher")
                    created_trade_id = None

                # Инициализируем trailing stop параметры
                highest_pnl = Decimal('0')
                current_trailing_level = 0
                last_trailing_notification_level = -1

                # КРИТИЧНО: Добавляем позицию в словарь активных позиций со ВСЕМИ данными
                self.active_flash_positions[symbol] = {
                    'entry_price': self.entry_price,
                    'entry_time': self.entry_time,
                    'position_size': position_size,
                    'order_id': order_result,  # ID ордера для отслеживания в БД
                    'trade_id': created_trade_id,  # ID записи в таблице trades для обновления при закрытии
                    'highest_pnl': highest_pnl,
                    'current_trailing_level': current_trailing_level,
                    'last_trailing_notification_level': last_trailing_notification_level,
                    'signal_drop_percent': drop_percent,
                    'signal_volume_ratio': volume_ratio,
                    'signal_volatility_pct': volatility_pct
                }

                # СТАРЫЕ поля (для совместимости с текущим кодом, будут удалены в следующих шагах)
                self.highest_pnl = highest_pnl
                self.current_trailing_level = current_trailing_level
                self.last_trailing_notification_level = last_trailing_notification_level

                log_info(self.user_id,
                        f"✅ LONG позиция открыта по РЕАЛЬНОЙ цене {self.entry_price:.8f}\n"
                        f"   Позиция добавлена в active_flash_positions[{symbol}]:\n"
                        f"   - order_id: {order_result}\n"
                        f"   - entry_price: {self.entry_price}\n"
                        f"   - position_size: {position_size}\n"
                        f"   - signal: drop={float(drop_percent):.2f}%, volume={float(volume_ratio):.2f}x",
                        "FlashDropCatcher")

                # Уведомление
                if self.bot:
                    try:
                        # Время входа
                        entry_time_str = self.entry_time.strftime("%H:%M:%S") if self.entry_time else "N/A"

                        # Формируем текст уведомления
                        notification_text = (
                            f"📈 {hbold('ОТКРЫТА НОВАЯ СДЕЛКА')} 📈\n\n"
                            f"▫️ {hbold('Стратегия:')} {hcode('Flash Drop Catcher')}\n"
                            f"▫️ {hbold('Символ:')} {hcode(self.symbol)}\n"
                            f"▫️ {hbold('Направление:')} {hcode('LONG 🟢')}\n"
                            f"▫️ {hbold('Время входа:')} {hcode(entry_time_str)}\n"
                            f"▫️ {hbold('Цена входа:')} {hcode(f'{entry_price:.8f}')}\n"
                            f"▫️ {hbold('Объем:')} {hcode(f'{position_size:.6f}')}\n"
                            f"▫️ {hbold('Плечо:')} {hcode(f'{leverage}x')}\n"
                            f"▫️ {hbold('Стоимость позиции:')} {hcode(f'{order_amount:.2f} USDT')}\n\n"
                            f"🎯 {hbold('ДЕТАЛИ СИГНАЛА:')}\n"
                            f"▫️ Падение: {hcode(f'{float(self.signal_drop_percent):.2f}%')}\n"
                            f"▫️ Всплеск объёма: {hcode(f'{float(self.signal_volume_ratio):.2f}x среднего')}\n"
                            f"▫️ Волатильность: {hcode(f'{float(self.signal_volatility_pct):.3f}%')}\n\n"
                            f"🛑 {hbold('STOP LOSS:')}\n"
                            f"▫️ Hard SL: {hcode(f'{float(self.HARD_STOP_LOSS_USDT):.2f} USDT')}\n"
                            f"▫️ Trailing Stop: Активен (откат 20% от максимума)"
                        )

                        await self.bot.send_message(self.user_id, notification_text)
                        log_info(self.user_id, "Уведомление об открытии позиции отправлено", "FlashDropCatcher")
                    except Exception as notification_error:
                        log_error(self.user_id, f"❌ Ошибка отправки уведомления об открытии: {notification_error}", "FlashDropCatcher")
            else:
                # ❌ Ордер НЕ размещен - восстанавливаем старый символ
                self.symbol = old_symbol
                log_error(self.user_id, f"❌ Не удалось открыть позицию для {symbol}, self.symbol восстановлен обратно в '{old_symbol}'", "FlashDropCatcher")

        except Exception as e:
            # ❌ Исключение при размещении ордера - восстанавливаем старый символ
            if hasattr(self, 'symbol') and self.symbol != old_symbol:
                self.symbol = old_symbol
                log_error(self.user_id, f"❌ Исключение при открытии позиции: {e}, self.symbol восстановлен обратно в '{old_symbol}'", "FlashDropCatcher")
            else:
                log_error(self.user_id, f"Ошибка открытия LONG позиции: {e}", "FlashDropCatcher")

    async def _handle_price_update(self, event: PriceUpdateEvent):
        """Внутренний метод обработки обновления цены (вызывается из BaseStrategy)"""
        await self.handle_price_update(event)

    @strategy_locked
    async def handle_price_update(self, event: PriceUpdateEvent):
        """
        Обработчик обновлений цены для ВСЕХ активных позиций.

        ✅ КРИТИЧНО: Теперь обрабатывает МНОЖЕСТВЕННЫЕ позиции параллельно!
        Каждая позиция отслеживается независимо со своими trailing stop параметрами.

        THREAD-SAFE: Защищено декоратором @strategy_locked.
        """
        # ВАЖНО: Проверяем, есть ли хотя бы одна активная позиция
        if not self.active_flash_positions:
            return

        try:
            # Проверяем, есть ли позиция по этому символу в нашем словаре
            if event.symbol not in self.active_flash_positions:
                return  # Это не наш символ, игнорируем

            # Получаем данные позиции из словаря
            position_data = self.active_flash_positions[event.symbol]

            current_price = self._convert_to_decimal(event.price)

            # КРИТИЧНО: Сохраняем последнюю известную цену для расчета PnL при закрытии
            self._last_known_price = current_price

            # Рассчитываем текущий PnL для этой конкретной позиции
            entry_price = position_data['entry_price']
            position_size = position_data['position_size']
            current_pnl = (current_price - entry_price) * position_size

            # ДИАГНОСТИКА: Логируем расчет PnL для отладки
            log_debug(self.user_id,
                     f"💰 PnL расчёт для {event.symbol}: price={current_price:.4f}, entry={entry_price:.4f}, "
                     f"size={position_size}, pnl={current_pnl:.2f}$, "
                     f"highest={position_data['highest_pnl']:.2f}$, level={position_data['current_trailing_level']}",
                     "FlashDropCatcher")

            # Проверка 1: Hard stop loss при -500$
            if current_pnl <= self.HARD_STOP_LOSS_USDT:
                log_warning(self.user_id,
                           f"🛑 HARD STOP LOSS! {event.symbol}: PnL={current_pnl:.2f}$ достиг {self.HARD_STOP_LOSS_USDT}$",
                           "FlashDropCatcher")
                await self._close_position(event.symbol, "hard_stop_loss")
                return

            # Проверка 2: Trailing stop в прибыли
            if current_pnl > Decimal('0'):
                await self._check_trailing_stop(event.symbol, position_data, current_pnl)

        except Exception as e:
            log_error(self.user_id, f"Ошибка обработки обновления цены для {event.symbol}: {e}", "FlashDropCatcher")

    # ============================================================================
    # === TRAILING STOP ЛОГИКА (СКОПИРОВАНО ИЗ SIGNAL_SCALPER) ===
    # ============================================================================

    def _calculate_dynamic_levels(self) -> Dict[int, Decimal]:
        """
        Динамическая система трейлинга (копия из signal_scalper_strategy.py)
        Автоматически подстраивается под размер депозита и плечо
        """
        order_amount = max(self._convert_to_decimal(self.get_config_value("order_amount", 200.0)), Decimal('10.0'))
        leverage = self._convert_to_decimal(self.get_config_value("leverage", 2.0))

        # Номинальная стоимость позиции
        notional_value = order_amount * leverage

        # Проценты для уровней трейлинга
        level_percentages = {
            1: Decimal('0.0035'),   # 0.35%
            2: Decimal('0.0065'),   # 0.65%
            3: Decimal('0.0095'),   # 0.95%
            4: Decimal('0.0145'),   # 1.45%
            5: Decimal('0.0195'),   # 1.95%
            6: Decimal('0.0350')    # 3.50%
        }

        # Рассчитываем пороги в USDT
        levels = {0: Decimal('0')}

        for level, percentage in level_percentages.items():
            levels[level] = notional_value * percentage

        return levels

    def _get_trailing_level(self, current_pnl: Decimal) -> int:
        """Определяет текущий уровень трейлинга"""
        levels = self._calculate_dynamic_levels()

        if current_pnl < levels[1]:
            return 0
        elif current_pnl < levels[2]:
            return 1
        elif current_pnl < levels[3]:
            return 2
        elif current_pnl < levels[4]:
            return 3
        elif current_pnl < levels[5]:
            return 4
        elif current_pnl < levels[6]:
            return 5
        else:
            return 6

    def _get_level_name(self, level: int) -> str:
        """Возвращает название уровня"""
        if level == 0:
            return "ОЖИДАНИЕ"

        levels = self._calculate_dynamic_levels()

        level_names = {
            1: f"МГНОВЕННЫЙ УРОВЕНЬ (${levels[1]:.2f}+, 0.35%)",
            2: f"РАННИЙ УРОВЕНЬ (${levels[2]:.2f}+, 0.65%)",
            3: f"СРЕДНИЙ УРОВЕНЬ (${levels[3]:.2f}+, 0.95%)",
            4: f"ХОРОШИЙ УРОВЕНЬ (${levels[4]:.2f}+, 1.45%)",
            5: f"ОТЛИЧНЫЙ УРОВЕНЬ (${levels[5]:.2f}+, 1.95%)",
            6: f"МАКСИМАЛЬНЫЙ УРОВЕНЬ (${levels[6]:.2f}+, 3.50%)"
        }
        return level_names.get(level, "НЕИЗВЕСТНЫЙ УРОВЕНЬ")

    async def _check_trailing_stop(self, symbol: str, position_data: Dict[str, Any], current_pnl: Decimal):
        """
        Проверяет условия trailing stop для конкретной позиции.

        ✅ КРИТИЧНО: Работает с МНОЖЕСТВЕННЫМИ позициями.
        Каждая позиция отслеживается независимо со своими trailing stop параметрами.

        Args:
            symbol: Символ позиции
            position_data: Данные позиции из словаря active_flash_positions[symbol]
            current_pnl: Текущий PnL позиции
        """
        # Обновляем максимальный PnL в словаре позиции
        if current_pnl > position_data['highest_pnl']:
            position_data['highest_pnl'] = current_pnl

            # Определяем новый уровень
            new_level = self._get_trailing_level(current_pnl)

            if new_level > position_data['current_trailing_level']:
                position_data['current_trailing_level'] = new_level

                # Уведомление о новом уровне
                if new_level != position_data['last_trailing_notification_level']:
                    log_info(self.user_id,
                            f"📈 {symbol} - Новый уровень трейлинга: {self._get_level_name(new_level)}, PnL=${current_pnl:.2f}",
                            "FlashDropCatcher")
                    position_data['last_trailing_notification_level'] = new_level

        # Проверяем откат для закрытия (20% от максимума)
        if position_data['current_trailing_level'] > 0:
            pullback_threshold = position_data['highest_pnl'] * Decimal('0.8')

            if current_pnl <= pullback_threshold:
                log_warning(self.user_id,
                           f"💰 TRAILING STOP! {symbol}: Откат 20% от максимума. Max={position_data['highest_pnl']:.2f}$, Current={current_pnl:.2f}$",
                           "FlashDropCatcher")
                await self._close_position(symbol, "trailing_stop_profit")

    async def _calculate_current_pnl(self, current_price: Decimal) -> Decimal:
        """Рассчитывает текущий PnL позиции"""
        if not self.position_active or not self.entry_price:
            return Decimal('0')

        # LONG позиция: PnL = (текущая_цена - цена_входа) × размер
        pnl = (current_price - self.entry_price) * self.position_size

        return pnl

    async def _close_position(self, symbol: str, reason: str):
        """
        Закрывает позицию по указанному символу.

        ✅ КРИТИЧНО: Работает с МНОЖЕСТВЕННЫМИ позициями через словарь active_flash_positions.
        Каждая позиция закрывается независимо с использованием своих данных.

        Args:
            symbol: Символ позиции для закрытия
            reason: Причина закрытия (для логирования)
        """
        # Проверяем, существует ли позиция по этому символу
        if symbol not in self.active_flash_positions:
            log_warning(self.user_id, f"⚠️ Позиция {symbol} не найдена в active_flash_positions, пропускаем закрытие", "FlashDropCatcher")
            return

        try:
            # Получаем данные позиции из словаря
            position_data = self.active_flash_positions[symbol]
            position_size = position_data['position_size']

            log_info(self.user_id, f"🔄 Закрытие позиции: {symbol}, причина: {reason}, размер: {position_size}", "FlashDropCatcher")

            # КРИТИЧНО: Временно устанавливаем self.symbol для _place_order
            # (базовый метод использует self.symbol для сохранения в БД)
            original_symbol = self.symbol
            self.symbol = symbol

            try:
                # ИСПРАВЛЕНИЕ: Используем _place_order из базового класса для сохранения в БД!
                close_result = await self._place_order(
                    side="Sell",  # Закрываем LONG через Sell
                    order_type="Market",
                    qty=position_size,
                    reduce_only=True
                )
            finally:
                # Восстанавливаем self.symbol обратно в "ALL"
                self.symbol = original_symbol

            # place_order() возвращает order_id (строку), а не словарь
            if close_result:
                # ✅ ПРАВИЛЬНЫЙ РАСЧЕТ PnL: используем ТОЧНЫЕ данные по order_id из БД и WebSocket
                # Источники истины:
                # 1. OPEN ордер из БД (по order_id) → entry_price, entry_qty, entry_commission
                # 2. CLOSE ордер из БД (по order_id - это close_result) → exit_price, exit_qty, exit_commission
                # В flash_drop_catcher НЕТ усреднений, поэтому просто: total_fees = open_commission + close_commission

                from database.db_trades import db_manager

                final_pnl = Decimal('0')
                exit_price = Decimal('0')
                commission = Decimal('0')
                open_order = None  # КРИТИЧНО: Инициализация перед try блоком для использования после except

                try:
                    # КРИТИЧНО: Используем order_id из словаря position_data для гарантированного поиска
                    open_order_id = position_data.get('order_id')

                    if not open_order_id:
                        log_error(self.user_id, f"❌ order_id отсутствует в position_data для {symbol}!", "FlashDropCatcher")
                        open_order = None
                    else:
                        # Ищем OPEN ордер по order_id + user_id (изоляция!)
                        open_order = await db_manager.get_order_by_id(open_order_id, self.user_id)

                    if open_order:
                        # ✅ ИСТОЧНИК ИСТИНЫ #1: OPEN ордер из БД
                        entry_price_for_pnl = Decimal(str(open_order.get('average_price', '0')))
                        position_size_for_pnl = position_size
                        open_commission = Decimal(str(open_order.get('commission', '0')))

                        log_info(self.user_id,
                                f"[БД→ORDER_ID] Используем OPEN ордер {open_order['order_id']}: "
                                f"entry_price={entry_price_for_pnl:.4f}, size={position_size_for_pnl}, fee={open_commission:.4f}",
                                "FlashDropCatcher")

                        # ✅ ИСТОЧНИК ИСТИНЫ #2: CLOSE ордер из БД (по order_id + user_id!)
                        # close_result - это order_id закрывающего ордера
                        # Ждём немного, чтобы ордер точно попал в БД
                        await asyncio.sleep(0.5)
                        close_order = await db_manager.get_order_by_id(close_result, self.user_id)

                        if close_order:
                            exit_price = Decimal(str(close_order.get('average_price', '0')))
                            close_commission = Decimal(str(close_order.get('commission', '0')))

                            # КРИТИЧНО: Проверяем, что ордер действительно исполнен (average_price > 0)
                            # Если ордер найден в БД, но еще не исполнен (статус NEW), average_price = 0
                            if exit_price == Decimal('0'):
                                # Используем fallback на последнюю известную цену
                                exit_price = self._last_known_price if hasattr(self, '_last_known_price') and self._last_known_price else entry_price_for_pnl
                                log_warning(self.user_id,
                                           f"⚠️ [FALLBACK] CLOSE ордер {close_result} найден в БД, но еще не исполнен (average_price=0), используем _last_known_price={exit_price:.4f}",
                                           "FlashDropCatcher")
                            else:
                                log_info(self.user_id,
                                        f"[БД→ORDER_ID] Используем CLOSE ордер {close_result}: "
                                        f"exit_price={exit_price:.4f}, fee={close_commission:.4f}",
                                        "FlashDropCatcher")
                        else:
                            # Fallback: используем последнюю известную цену
                            exit_price = self._last_known_price if hasattr(self, '_last_known_price') and self._last_known_price else entry_price_for_pnl
                            close_commission = Decimal('0')
                            log_warning(self.user_id,
                                       f"⚠️ [FALLBACK] CLOSE ордер {close_result} не найден в БД, используем _last_known_price={exit_price:.4f}",
                                       "FlashDropCatcher")

                        # Расчёт PnL (LONG позиция)
                        pnl_gross = (exit_price - entry_price_for_pnl) * position_size_for_pnl

                        # ФИНАЛЬНЫЙ PnL: Вычитаем комиссии (открытие + закрытие)
                        commission = open_commission + close_commission
                        final_pnl = pnl_gross - commission

                        log_info(self.user_id,
                                f"💰 [PNL РАСЧЁТ] {symbol}: entry={entry_price_for_pnl:.4f}, exit={exit_price:.4f}, "
                                f"size={position_size_for_pnl}, direction=LONG | "
                                f"PnL_gross={pnl_gross:.4f}, fees={commission:.4f} (open={open_commission:.4f}+close={close_commission:.4f}), PnL_net={final_pnl:.4f}",
                                "FlashDropCatcher")
                    else:
                        log_warning(self.user_id, f"⚠️ [FALLBACK] OPEN ордер для {symbol} не найден в БД, используем данные из памяти", "FlashDropCatcher")
                        # ФОЛБЭК: Используем unrealisedPnl из позиции
                        positions = await self.api.get_positions(symbol=symbol)
                        if positions and isinstance(positions, list):
                            for pos in positions:
                                if pos["symbol"] == symbol:
                                    final_pnl = self._convert_to_decimal(pos.get("unrealisedPnl", 0))
                                    break

                except Exception as api_error:
                    log_error(self.user_id, f"❌ [BYBIT PNL] Ошибка запроса closedPnL для {symbol}: {api_error}, используем unrealisedPnl", "FlashDropCatcher")
                    # ФОЛБЭК: Используем unrealisedPnl из позиции
                    positions = await self.api.get_positions(symbol=symbol)
                    if positions and isinstance(positions, list):
                        for pos in positions:
                            if pos["symbol"] == symbol:
                                final_pnl = self._convert_to_decimal(pos.get("unrealisedPnl", 0))
                                break

                # СОХРАНЯЕМ значения для передачи в уведомление
                # ИСПОЛЬЗУЕМ УЖЕ ПОЛУЧЕННЫЙ open_order выше (строка 1159)
                if open_order:
                    saved_entry_time = open_order.get('filled_at')  # Время из БД
                    saved_entry_price = open_order.get('average_price')  # Цена из БД
                    log_debug(self.user_id, f"[ИЗ БД] {symbol} - Время входа: {saved_entry_time}, Цена входа: {saved_entry_price}", "FlashDropCatcher")
                else:
                    # Fallback на данные из словаря (если БД недоступна)
                    saved_entry_time = position_data.get('entry_time')
                    saved_entry_price = position_data.get('entry_price')
                    log_warning(self.user_id, f"[FALLBACK] Не найден OPEN ордер в БД для {symbol}, используем данные из active_flash_positions", "FlashDropCatcher")

                # КРИТИЧНО: Удаляем позицию из словаря активных позиций
                del self.active_flash_positions[symbol]
                log_info(self.user_id, f"✅ Позиция {symbol} удалена из active_flash_positions (осталось позиций: {len(self.active_flash_positions)})", "FlashDropCatcher")

                log_info(self.user_id, f"✅ Позиция {symbol} закрыта. PnL: ${final_pnl:.2f}", "FlashDropCatcher")

                # КРИТИЧНО: Обновляем trade в таблице trades СРАЗУ после закрытия (НЕ в уведомлении!)
                trade_id_to_update = position_data.get('trade_id')
                if trade_id_to_update:
                    try:
                        from datetime import timezone as tz
                        await db_manager.update_trade_on_close(
                            trade_id=trade_id_to_update,
                            exit_price=exit_price,
                            pnl=final_pnl,
                            commission=commission,
                            exit_time=datetime.now(tz.utc)
                        )
                        log_info(self.user_id, f"✅ Trade {trade_id_to_update} обновлён в БД: PnL={final_pnl:.2f}$", "FlashDropCatcher")
                    except Exception as trade_update_error:
                        log_error(self.user_id, f"❌ Ошибка обновления trade {trade_id_to_update} в БД: {trade_update_error}", "FlashDropCatcher")
                else:
                    log_warning(self.user_id, f"⚠️ trade_id не найден в position_data для {symbol} - trade не обновлён в БД!", "FlashDropCatcher")

                # ИСПОЛЬЗУЕМ БАЗОВЫЙ МЕТОД для отправки уведомления (с временем и ценами)
                await self._send_trade_close_notification(
                    pnl=final_pnl,
                    commission=commission,
                    exit_price=exit_price if exit_price > Decimal('0') else None,
                    entry_price=saved_entry_price,
                    entry_time=saved_entry_time
                )

            else:
                log_error(self.user_id, f"Не удалось закрыть позицию {symbol}", "FlashDropCatcher")

        except Exception as e:
            log_error(self.user_id, f"Ошибка закрытия позиции {symbol}: {e}", "FlashDropCatcher")

    # ============================================================================
    # === ВСПОМОГАТЕЛЬНЫЕ МЕТОДЫ ===
    # ============================================================================

    def _convert_to_decimal(self, value: Any) -> Decimal:
        """Безопасное преобразование в Decimal"""
        if isinstance(value, Decimal):
            return value
        elif isinstance(value, (int, float)):
            return Decimal(str(value))
        elif isinstance(value, str):
            try:
                return Decimal(value)
            except (ValueError, TypeError, ArithmeticError):
                return Decimal('0')
        return Decimal('0')

    async def get_detailed_status(self) -> Dict[str, Any]:
        """
        Возвращает детальную информацию обо ВСЕХ активных позициях для команды /trade_details.

        ✅ КРИТИЧНО: Работает с МНОЖЕСТВЕННЫМИ позициями.
        Возвращает список всех активных позиций из словаря active_flash_positions.
        """
        try:
            # Проверяем, есть ли активные позиции в словаре
            if not self.active_flash_positions:
                return {
                    "has_position": False,
                    "symbol": "ALL",
                    "strategy_type": StrategyType.FLASH_DROP_CATCHER.value,
                    "account_priority": self.account_priority,
                    "total_positions": 0
                }

            # Собираем информацию обо всех активных позициях
            positions_list = []

            for symbol, position_data in self.active_flash_positions.items():
                try:
                    # Получаем текущую цену для этого символа
                    try:
                        ticker = await self.api.get_ticker(symbol=symbol)
                        current_price = ticker["lastPrice"] if ticker and "lastPrice" in ticker else position_data['entry_price']
                    except Exception as e:
                        log_warning(self.user_id, f"Не удалось получить текущую цену для {symbol}: {e}", "FlashDropCatcher")
                        current_price = position_data['entry_price']

                    # Рассчитываем PnL для этой позиции
                    entry_price = position_data['entry_price']
                    position_size = position_data['position_size']
                    current_pnl = (current_price - entry_price) * position_size

                    price_change_percent = Decimal('0')
                    if entry_price > 0:
                        price_change_percent = ((current_price - entry_price) / entry_price) * Decimal('100')

                    # Получаем breakeven price с биржи
                    breakeven_price = None
                    try:
                        exchange_positions = await self.api.get_positions(symbol=symbol)
                        if exchange_positions and isinstance(exchange_positions, list) and len(exchange_positions) > 0:
                            breakeven_price_from_exchange = exchange_positions[0].get("breakEvenPrice", None)
                            if breakeven_price_from_exchange:
                                breakeven_price = self._convert_to_decimal(breakeven_price_from_exchange)
                    except Exception as e:
                        log_debug(self.user_id, f"Не удалось получить breakEvenPrice для {symbol}: {e}", "FlashDropCatcher")

                    # Формируем информацию о позиции
                    position_info = {
                        "symbol": symbol,
                        "direction": "LONG",
                        "entry_price": float(entry_price),
                        "entry_time": position_data.get('entry_time').isoformat() if position_data.get('entry_time') else None,
                        "current_price": float(current_price),
                        "position_size": float(position_size),
                        "order_id": position_data.get('order_id'),
                        "pnl": {
                            "unrealized_pnl": float(current_pnl),
                            "price_change_percent": float(price_change_percent),
                            "peak_profit": float(position_data['highest_pnl'])
                        },
                        "trailing_stop": {
                            "current_level": position_data['current_trailing_level'],
                            "level_name": self._get_level_name(position_data['current_trailing_level']),
                            "highest_pnl": float(position_data['highest_pnl'])
                        },
                        "signal": {
                            "drop_percent": float(position_data.get('signal_drop_percent', 0)),
                            "volume_ratio": float(position_data.get('signal_volume_ratio', 0)),
                            "volatility_pct": float(position_data.get('signal_volatility_pct', 0))
                        },
                        "margin": {
                            "initial_margin": float(self.get_config_value("order_amount", 200.0)),
                            "breakeven_price": float(breakeven_price) if breakeven_price else None
                        },
                        "stop_loss": {
                            "has_stop_loss": True,
                            "stop_loss_type": "hard_stop",
                            "stop_loss_usdt": float(self.HARD_STOP_LOSS_USDT)
                        }
                    }

                    positions_list.append(position_info)

                except Exception as pos_error:
                    log_error(self.user_id, f"Ошибка получения статуса позиции {symbol}: {pos_error}", "FlashDropCatcher")
                    # Продолжаем обработку остальных позиций

            # Возвращаем информацию обо всех позициях
            return {
                "has_position": True,
                "symbol": "ALL",  # Для совместимости
                "strategy_type": StrategyType.FLASH_DROP_CATCHER.value,
                "account_priority": self.account_priority,
                "total_positions": len(self.active_flash_positions),
                "positions": positions_list  # Список всех активных позиций
            }

        except Exception as e:
            log_error(self.user_id, f"Ошибка получения детального статуса: {e}", "FlashDropCatcher")
            return {
                "has_position": False,
                "symbol": "ALL",
                "strategy_type": StrategyType.FLASH_DROP_CATCHER.value,
                "account_priority": self.account_priority,
                "total_positions": 0,
                "error": str(e)
            }

    async def _get_current_market_price(self) -> Optional[Decimal]:
        """Получает текущую рыночную цену символа с биржи"""
        try:
            ticker = await self.api.get_ticker(symbol=self.symbol)
            if ticker and "lastPrice" in ticker:
                return ticker["lastPrice"]  # Уже в формате Decimal
            return None
        except Exception as e:
            log_error(self.user_id, f"Ошибка получения текущей цены: {e}", "FlashDropCatcher")
            return None

    async def _run_heartbeat_monitor(self):
        """
        💓 HEARTBEAT МОНИТОРИНГ - Периодические уведомления в Telegram каждые 30 минут
        Показывает что стратегия активна и работает правильно
        """
        heartbeat_interval = 3600  # 60 минут в секундах

        while self.is_running:
            try:
                # КРИТИЧНО: Sleep в НАЧАЛЕ цикла, чтобы первое сообщение пришло через 30 минут после запуска
                await asyncio.sleep(heartbeat_interval)

                if not self.is_running:
                    break

                # КРИТИЧНО: Перезагружаем конфигурацию перед проверкой настройки heartbeat
                await self._force_config_reload()

                # КРИТИЧНО: Читаем СВЕЖЕЕ значение из конфига (после перезагрузки), а не закешированное
                enable_heartbeat = bool(self.get_config_value("enable_heartbeat_notifications", True))

                # Формируем сообщение о статусе
                elapsed_time = datetime.now() - self.last_heartbeat_time
                elapsed_minutes = int(elapsed_time.total_seconds() / 60)

                # Статус позиции (для множественных позиций)
                if not self.active_flash_positions:
                    position_status = "🟢 НЕТ АКТИВНЫХ ПОЗИЦИЙ"
                elif len(self.active_flash_positions) == 1:
                    symbol = list(self.active_flash_positions.keys())[0]
                    position_status = f"🔵 АКТИВНА 1 ПОЗИЦИЯ: {symbol}"
                else:
                    symbols_str = ", ".join(list(self.active_flash_positions.keys()))
                    position_status = f"🔵 АКТИВНО {len(self.active_flash_positions)} ПОЗИЦИЙ: {symbols_str}"

                # Статистика за период (защита от деления на 0)
                candles_per_minute = self.processed_candles_count / max(elapsed_minutes, 1) if elapsed_minutes > 0 else 0
                
                # Формируем детализацию по падениям
                drops_detail = ""
                if self.detected_drops_count > 0:
                    # Качественные падения (прошли все фильтры)
                    drops_detail += f"\n📉 {hbold('Качественных падений обнаружено:')} {self.detected_drops_count}"

                    # Из них сколько отработали (вошли в сделку)
                    drops_detail += f"\n   ├─ ✅ Отработано (вход в сделку): {self.trades_opened}"

                    # Сколько пропустили и почему
                    rejected_total = self.rejected_due_to_position_exists + self.rejected_due_to_max_positions
                    if rejected_total > 0:
                        drops_detail += f"\n   └─ ⏭️  Пропущено: {rejected_total}"
                        if self.rejected_due_to_position_exists > 0:
                            drops_detail += f"\n       ▪️ Уже открыта позиция: {self.rejected_due_to_position_exists}"
                        if self.rejected_due_to_max_positions > 0:
                            drops_detail += f"\n       ▪️ Достигнут лимит ({self.MAX_CONCURRENT_POSITIONS} поз.): {self.rejected_due_to_max_positions}"
                else:
                    drops_detail += f"\n📉 {hbold('Качественных падений обнаружено:')} 0"
                    drops_detail += f"\n   └─ ⏳ Ожидаем качественные сигналы..."

                # Формируем текст сообщения
                message_text = (
                    f"{'='*40}\n"
                    f"💓 {hbold('HEARTBEAT - FLASH DROP CATCHER АКТИВНА')}\n"
                    f"{'='*40}\n\n"
                    f"📊 {hbold('Отслеживается символов:')} {len(self._liquid_symbols)}"
                    f"{drops_detail}\n"
                    f"📌 {hbold('Статус:')} {position_status}\n\n"
                    f"⚙️  {hbold('Текущие настройки:')}\n"
                    f"  ▫️ Интервал анализа: {hcode(f'{self.TIMEFRAME_INTERVAL}m')}\n"
                    f"  ▫️ Базовый порог: {hcode(f'{float(self.BASE_DROP_PCT)*100:.1f}%')} (для BTC/ETH: {hcode(f'{float(self.MIN_DROP_PCT)*100:.1f}%')}, макс: {hcode(f'{float(self.MAX_DROP_PCT)*100:.1f}%')})\n"
                    f"  ▫️ Мин. всплеск объёма: {hcode(f'{self.VOLUME_SPIKE_MIN}x')}\n"
                    f"{'='*40}"
                )

                # Отправляем в Telegram если включено
                if enable_heartbeat and self.bot:
                    try:
                        await self.bot.send_message(
                            self.user_id,
                            message_text
                        )
                        log_info(self.user_id, "Heartbeat уведомление отправлено в Telegram", "FlashDropCatcher")
                    except Exception as telegram_error:
                        log_error(self.user_id, f"Ошибка отправки Telegram heartbeat: {telegram_error}", "FlashDropCatcher")

                # Дублируем в логи для отладки (всегда, независимо от настройки)
                log_info(
                    self.user_id,
                    f"💓 HEARTBEAT: {elapsed_minutes}м работы | {len(self._liquid_symbols)} символов | "
                    f"{self.processed_candles_count} свечей | {self.detected_drops_count} падений | {position_status}",
                    "FlashDropCatcher"
                )

                # Сбрасываем счётчики для следующего периода
                self.last_heartbeat_time = datetime.now()
                self.processed_candles_count = 0
                self.detected_drops_count = 0
                self.rejected_due_to_position_exists = 0
                self.rejected_due_to_max_positions = 0
                self.trades_opened = 0

            except asyncio.CancelledError:
                log_info(self.user_id, "Heartbeat мониторинг остановлен", "FlashDropCatcher")
                break
            except Exception as e:
                log_error(self.user_id, f"Ошибка heartbeat мониторинга: {e}", "FlashDropCatcher")
                # Продолжаем работу даже при ошибке
                await asyncio.sleep(60)

    async def _run_config_reload_monitor(self):
        """
        ⚙️ ПЕРИОДИЧЕСКАЯ ПЕРЕЗАГРУЗКА КОНФИГУРАЦИИ - каждые 5 минут

        ✅ КРИТИЧНО: Это гарантирует что настройки обновляются даже если нет сигналов!
        Настройки вроде MAX_DROP_PCT, BASE_DROP_PCT, MIN_DROP_PCT влияют на ПОИСК сигналов,
        поэтому должны обновляться регулярно, а не только перед входом в сделку.
        """
        config_reload_interval = 300  # 5 минут в секундах

        while self.is_running:
            try:
                # КРИТИЧНО: Sleep в НАЧАЛЕ цикла, чтобы первая перезагрузка произошла через 5 минут после запуска
                await asyncio.sleep(config_reload_interval)

                if not self.is_running:
                    break

                # Перезагружаем конфигурацию
                log_info(self.user_id,
                        "⚙️ Периодическая перезагрузка ВСЕХ настроек конфигурации (каждые 5 мин)",
                        "FlashDropCatcher")

                await self._force_config_reload()

                log_info(self.user_id,
                        f"✅ ВСЕ настройки обновлены из Redis:\n"
                        f"  • Интервал: {self.TIMEFRAME_INTERVAL}m, История свечей: {self.HISTORY_BARS}\n"
                        f"  • Пороги падения: BASE={float(self.BASE_DROP_PCT)*100:.1f}%, MIN={float(self.MIN_DROP_PCT)*100:.1f}%, MAX={float(self.MAX_DROP_PCT)*100:.1f}%\n"
                        f"  • Объем: {self.VOLUME_SPIKE_MIN}x, Мин.дневной объем: ${float(self.MIN_DAILY_VOLUME_USD):,.0f}\n"
                        f"  • Макс.позиций: {self.MAX_CONCURRENT_POSITIONS}, Hard SL: {float(self.HARD_STOP_LOSS_USDT):.0f} USDT\n"
                        f"  • Order amount: {float(self.get_config_value('order_amount', 200.0)):.0f} USDT, Плечо: {self.get_config_value('leverage', 2)}x",
                        "FlashDropCatcher")

            except asyncio.CancelledError:
                log_info(self.user_id, "Мониторинг перезагрузки конфигурации остановлен", "FlashDropCatcher")
                break
            except Exception as e:
                log_error(self.user_id, f"Ошибка перезагрузки конфигурации: {e}", "FlashDropCatcher")
                # Продолжаем работу даже при ошибке
                await asyncio.sleep(60)

    async def _handle_manual_close(self, event):
        """
        МГНОВЕННЫЙ обработчик ручного закрытия позиции через WebSocket.
        Вызывается когда пользователь вручную закрыл позицию на бирже.

        ✅ КРИТИЧНО: Работает с МНОЖЕСТВЕННЫМИ позициями.
        Проверяет, есть ли закрытый символ в словаре active_flash_positions.
        """
        from core.events import PositionClosedEvent

        if not isinstance(event, PositionClosedEvent):
            return

        # Проверяем, есть ли этот символ в наших активных позициях
        if event.symbol not in self.active_flash_positions:
            log_debug(self.user_id, f"Символ {event.symbol} не найден в active_flash_positions, пропускаем", "FlashDropCatcher")
            return

        log_warning(self.user_id,
                   f"⚠️ ОБРАБОТКА РУЧНОГО ЗАКРЫТИЯ через WebSocket: {event.symbol}",
                   "FlashDropCatcher")

        try:
            # Получаем данные позиции из словаря
            position_data = self.active_flash_positions[event.symbol]

            # ✅ ПРАВИЛЬНЫЙ РАСЧЕТ PnL: используем ТОЧНЫЕ данные по order_id из БД
            # Источники истины:
            # 1. OPEN ордер из БД (по order_id) → entry_price, entry_qty, entry_commission
            # 2. Последняя известная цена из WebSocket/память → exit_price (приблизительно)
            # В flash_drop_catcher НЕТ усреднений, поэтому просто: total_fees = open_commission

            from database.db_trades import db_manager

            final_pnl = Decimal('0')
            exit_price = Decimal('0')
            commission = Decimal('0')

            # Получаем данные OPEN ордера из БД
            open_order = await db_manager.get_open_order_for_position(self.user_id, event.symbol, self.account_priority)
            if open_order:
                saved_entry_time = open_order.get('filled_at')
                saved_entry_price = open_order.get('average_price')

                # ✅ ИСТОЧНИК ИСТИНЫ #1: OPEN ордер из БД
                entry_price_for_pnl = Decimal(str(saved_entry_price))
                position_size_for_pnl = position_data['position_size']
                open_commission = Decimal(str(open_order.get('commission', '0')))

                log_info(self.user_id,
                        f"[БД→ORDER_ID] {event.symbol}: Используем OPEN ордер {open_order['order_id']}: "
                        f"entry_price={entry_price_for_pnl:.4f}, size={position_size_for_pnl}, fee={open_commission:.4f}",
                        "FlashDropCatcher")

                # ✅ ИСТОЧНИК ИСТИНЫ #2: Последняя известная цена из WebSocket
                exit_price = self._last_known_price if hasattr(self, '_last_known_price') and self._last_known_price else entry_price_for_pnl

                # Расчёт PnL (LONG позиция)
                pnl_gross = (exit_price - entry_price_for_pnl) * position_size_for_pnl

                # ФИНАЛЬНЫЙ PnL: Вычитаем комиссию открытия (комиссия закрытия неизвестна при ручном закрытии)
                commission = open_commission
                final_pnl = pnl_gross - commission

                log_info(self.user_id,
                        f"💰 [PNL РАСЧЁТ РУЧНОЕ] {event.symbol}: entry={entry_price_for_pnl:.4f}, exit≈{exit_price:.4f}, "
                        f"size={position_size_for_pnl}, direction=LONG | "
                        f"PnL_gross={pnl_gross:.4f}, fees≈{commission:.4f}, PnL_net≈{final_pnl:.4f}",
                        "FlashDropCatcher")

                # Обновляем БД - закрываем ордер (с user_id для изоляции!)
                try:
                    await db_manager.close_order(
                        order_id=open_order['order_id'],
                        user_id=self.user_id,
                        close_price=float(exit_price) if exit_price > Decimal('0') else None,
                        close_size=float(position_size_for_pnl) if position_size_for_pnl > 0 else None,
                        realized_pnl=float(final_pnl),
                        close_reason="manual_close_by_user"
                    )
                    log_info(self.user_id, f"✅ Ордер {open_order['order_id']} для {event.symbol} закрыт в БД (ручное закрытие)", "FlashDropCatcher")
                except Exception as db_error:
                    log_error(self.user_id, f"❌ Ошибка обновления БД для {event.symbol}: {db_error}", "FlashDropCatcher")
            else:
                # Fallback на данные из словаря
                saved_entry_time = position_data.get('entry_time')
                saved_entry_price = position_data.get('entry_price')
                log_warning(self.user_id, f"[FALLBACK] OPEN ордер для {event.symbol} не найден в БД, используем данные из словаря", "FlashDropCatcher")

            # КРИТИЧНО: Удаляем позицию из словаря активных позиций
            del self.active_flash_positions[event.symbol]
            log_info(self.user_id, f"✅ Позиция {event.symbol} удалена из active_flash_positions (ручное закрытие, осталось позиций: {len(self.active_flash_positions)})", "FlashDropCatcher")

            log_info(self.user_id, f"✅ Позиция {event.symbol} закрыта вручную. PnL: ${final_pnl:.2f}", "FlashDropCatcher")

            # Отправляем уведомление
            await self._send_trade_close_notification(
                pnl=final_pnl,
                commission=commission,
                exit_price=exit_price if exit_price > Decimal('0') else None,
                entry_price=saved_entry_price,
                entry_time=saved_entry_time
            )

        except Exception as e:
            log_error(self.user_id, f"❌ Ошибка обработки ручного закрытия для {event.symbol}: {e}", "FlashDropCatcher")

    async def _execute_strategy_logic(self):
        """Базовый метод выполнения логики (не используется в этой стратегии)"""
        pass

    async def _handle_order_filled(self, event: OrderFilledEvent):
        """
        КРИТИЧНАЯ ОБРАБОТКА исполненных ордеров для ВОССТАНОВЛЕНИЯ после WebSocket потери.

        Эта обработка КРИТИЧНА для случаев:
        1. WebSocket потерял соединение → событие OrderFilledEvent пришло ПОСЛЕ переподключения
        2. Бот перезапустился → восстановление состояния из БД через синхронизацию

        БЕЗ этого метода: если WebSocket потеряется, стратегия НЕ УЗНАЕТ о открытой позиции!
        """
        # КРИТИЧНО: АТОМАРНАЯ ЗАЩИТА ОТ RACE CONDITION!
        # Добавляем ордер в set НЕМЕДЛЕННО, ПЕРЕД любыми async операциями
        # Это предотвращает двойную обработку при одновременном приходе WebSocket + Recovery событий
        if event.order_id in self.processed_orders:
            log_debug(self.user_id, f"[ДУПЛИКАТ] Ордер {event.order_id} уже обработан, игнорируем", "FlashDropCatcher")
            return

        # АТОМАРНО добавляем в set (set.add() thread-safe благодаря GIL)
        self.processed_orders.add(event.order_id)
        log_debug(self.user_id, f"🔒 Ордер {event.order_id} заблокирован от повторной обработки", "FlashDropCatcher")

        # КРИТИЧНО: Проверяем что ордер принадлежит БОТУ (есть в БД)
        from database.db_trades import db_manager
        try:
            order_in_db = await db_manager.get_order_by_id(event.order_id, self.user_id)

            if not order_in_db:
                log_warning(self.user_id,
                           f"⚠️ [НЕ НАШ ОРДЕР] Ордер {event.order_id} НЕ найден в БД бота! ИГНОРИРУЮ.",
                           "FlashDropCatcher")
                return

            # Проверяем bot_priority
            order_bot_priority = order_in_db.get('bot_priority', 1)
            if order_bot_priority != self.account_priority:
                log_debug(self.user_id,
                         f"[НЕ НАШ БОТ] Ордер {event.order_id} принадлежит Bot_{order_bot_priority}, а это Bot_{self.account_priority}",
                         "FlashDropCatcher")
                return

            # КРИТИЧНО: Проверяем strategy_type - каждая стратегия работает СТРОГО со своими ордерами!
            order_strategy_type = order_in_db.get('strategy_type')
            if order_strategy_type != self.strategy_type.value:
                log_debug(self.user_id,
                         f"[НЕ НАШ ТИП] Ордер {event.order_id} принадлежит стратегии {order_strategy_type}, а это {self.strategy_type.value}",
                         "FlashDropCatcher")
                return

            log_info(self.user_id, f"✅ [НАША СДЕЛКА] Ордер {event.order_id} подтверждён в БД", "FlashDropCatcher")

        except Exception as db_check_error:
            log_error(self.user_id,
                     f"❌ Ошибка проверки ордера {event.order_id} в БД: {db_check_error}. ИГНОРИРУЮ!",
                     "FlashDropCatcher")
            return

        # Определяем тип ордера (OPEN или CLOSE) по order_purpose из БД
        order_purpose = order_in_db.get('order_purpose', 'OPEN')
        symbol = order_in_db.get('symbol')

        log_info(self.user_id,
                f"[ОБРАБОТКА] Ордер {event.order_id} ({event.side} {event.qty} {symbol}) - purpose={order_purpose}",
                "FlashDropCatcher")

        if order_purpose == 'OPEN':
            # Это ОТКРЫТИЕ позиции
            # Проверяем, есть ли уже позиция по этому символу (защита от дубликатов)
            if symbol in self.active_flash_positions:
                log_warning(self.user_id,
                           f"⚠️ Позиция {symbol} УЖЕ существует в active_flash_positions, пропускаем дубликат OPEN события",
                           "FlashDropCatcher")
                return

            # Восстанавливаем состояние позиции из БД
            entry_price = Decimal(str(order_in_db.get('average_price', '0')))
            position_size = Decimal(str(event.qty))
            entry_time = order_in_db.get('filled_at')

            # КРИТИЧНО: Восстанавливаем trade_id из ордера (если есть)
            restored_trade_id = order_in_db.get('trade_id')

            # Добавляем позицию в словарь активных
            self.active_flash_positions[symbol] = {
                'entry_price': entry_price,
                'entry_time': entry_time,
                'position_size': position_size,
                'order_id': event.order_id,
                'trade_id': restored_trade_id,  # ID записи в таблице trades для обновления при закрытии
                'highest_pnl': Decimal('0'),
                'current_trailing_level': 0,
                'last_trailing_notification_level': -1,
                'signal_drop_percent': Decimal('0'),  # Неизвестно при восстановлении
                'signal_volume_ratio': Decimal('0'),
                'signal_volatility_pct': Decimal('0')
            }

            log_info(self.user_id,
                    f"✅ [ВОССТАНОВЛЕНО] Позиция {symbol} добавлена в active_flash_positions "
                    f"(entry={entry_price:.8f}, size={position_size}, time={entry_time})",
                    "FlashDropCatcher")

        elif order_purpose == 'CLOSE':
            # Это ЗАКРЫТИЕ позиции
            # Позиция уже должна быть удалена из active_flash_positions в методе _close_position
            # Но на всякий случай проверяем и удаляем если еще есть
            if symbol in self.active_flash_positions:
                log_warning(self.user_id,
                           f"⚠️ Позиция {symbol} ещё в active_flash_positions при CLOSE событии, удаляем",
                           "FlashDropCatcher")
                del self.active_flash_positions[symbol]

            log_info(self.user_id,
                    f"✅ [CLOSE] Позиция {symbol} закрыта (осталось позиций: {len(self.active_flash_positions)})",
                    "FlashDropCatcher")