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
from core.enums import StrategyType, PositionSide
from core.events import EventType, EventBus, PriceUpdateEvent, OrderFilledEvent
from api.bybit_api import BybitAPI
from .base_strategy import BaseStrategy
from aiogram.utils.markdown import hbold, hcode
from core.concurrency_manager import strategy_locked

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
        self._ws_url = "wss://stream.bybit.com/v5/public/linear"

        # === ПАРАМЕТРЫ ТОРГОВЛИ ===
        self.MAX_CONCURRENT_POSITIONS = 2  # Максимум 2 позиции одновременно (из конфига)

        # Параметры позиции
        self.position_active = False
        self.entry_price: Decimal = Decimal('0')
        self.entry_time: Optional[datetime] = None  # Время открытия позиции
        self.position_size: Decimal = Decimal('0')
        self.active_direction = "LONG"

        # Информация о сигнале (для уведомлений)
        self.signal_drop_percent: Decimal = Decimal('0')
        self.signal_volume_ratio: Decimal = Decimal('0')
        self.signal_volatility_pct: Decimal = Decimal('0')

        # Trailing stop параметры (из signal_scalper)
        self.highest_pnl = Decimal('0')
        self.current_trailing_level = 0
        self.last_trailing_notification_level = -1

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

    async def _load_config(self):
        """Загрузка конфигурации из Redis и установка параметров"""
        await super()._load_strategy_config()

        # Загружаем параметры из конфигурации (ВАЖНО: дефолты должны совпадать с default_configs.py!)
        self.TIMEFRAME_INTERVAL = str(self.get_config_value("timeframe_interval", "15"))
        self.HISTORY_BARS = int(self.get_config_value("candle_history_size", 12))  # ИСПРАВЛЕНО: 7 → 12
        self.BASE_DROP_PCT = self._convert_to_decimal(self.get_config_value("base_drop_percent", 4.0)) / Decimal('100')  # ИСПРАВЛЕНО: 5.0 → 4.0
        self.MIN_DROP_PCT = self._convert_to_decimal(self.get_config_value("min_drop_percent", 2.5)) / Decimal('100')  # ИСПРАВЛЕНО: 3.0 → 2.5
        self.MAX_DROP_PCT = self._convert_to_decimal(self.get_config_value("max_drop_percent", 10.0)) / Decimal('100')  # ИСПРАВЛЕНО: 15.0 → 10.0
        self.VOLUME_SPIKE_MIN = self._convert_to_decimal(self.get_config_value("volume_spike_min", 2.5))  # ИСПРАВЛЕНО: 3.0 → 2.5
        self.MIN_DAILY_VOLUME_USD = self._convert_to_decimal(self.get_config_value("min_daily_volume_usd", 1000000.0))
        self.MAX_CONCURRENT_POSITIONS = int(self.get_config_value("max_concurrent_positions", 2))
        self.HARD_STOP_LOSS_USDT = self._convert_to_decimal(self.get_config_value("hard_stop_loss_usdt", -500.0))  # ИСПРАВЛЕНО: -15.0 → -500.0
        self.WEBSOCKET_CHUNK_SIZE = int(self.get_config_value("websocket_chunk_size", 150))
        self.ENABLE_HEARTBEAT = bool(self.get_config_value("enable_heartbeat_notifications", True))  # Heartbeat уведомления

        log_info(self.user_id,
                f"📋 Параметры FlashDropCatcher: интервал={self.TIMEFRAME_INTERVAL}m, история={self.HISTORY_BARS}, "
                f"базовый порог={float(self.BASE_DROP_PCT)*100:.1f}%, объем={self.VOLUME_SPIKE_MIN}x",
                "FlashDropCatcher")

    async def start(self):
        """Запуск стратегии"""
        if self.is_running:
            log_warning(self.user_id, "FlashDropCatcher уже запущена", "FlashDropCatcher")
            return True

        self.is_running = True

        # Загружаем конфигурацию
        await self._load_config()

        # КРИТИЧНО: Подписываемся на обновления цены для мониторинга активной позиции
        await self.event_bus.subscribe(EventType.PRICE_UPDATE, self.handle_price_update, user_id=self.user_id)

        # КРИТИЧНО: Подписываемся на ручное закрытие позиции
        from core.events import PositionClosedEvent
        await self.event_bus.subscribe(EventType.POSITION_CLOSED, self._handle_manual_close, user_id=self.user_id)

        # Запускаем WebSocket сканер
        self._scanner_task = asyncio.create_task(self._run_websocket_scanner())

        # Запускаем heartbeat мониторинг
        self._heartbeat_task = asyncio.create_task(self._run_heartbeat_monitor())

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

    async def _handle_drop_signal(self, symbol: str, price: Decimal, drop_pct: Decimal,
                                  volume_ratio: Decimal, volatility_pct: Decimal):
        """
        Обрабатывает сигнал резкого падения с проверкой лимитов.
        """
        try:
            # Проверка 1: Подсчитываем количество открытых позиций на бирже
            all_positions = await self.api.get_positions()
            open_positions_count = 0

            # get_positions() возвращает List[Dict] напрямую
            if all_positions and isinstance(all_positions, list):
                for pos in all_positions:
                    position_size = float(pos.get("size", 0))
                    if position_size > 0:
                        open_positions_count += 1
                        # Проверяем, есть ли уже позиция на этот символ
                        if pos["symbol"] == symbol:
                            self.rejected_due_to_position_exists += 1
                            log_warning(self.user_id,
                                       f"⚠️ Пропускаем сигнал {symbol} - уже есть открытая позиция!",
                                       "FlashDropCatcher")
                            return

            # Проверка 2: Достигнут ли лимит одновременных позиций
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

            # Обновляем символ для этой сделки
            self.symbol = symbol

            # Открываем LONG позицию (передаём информацию о сигнале)
            await self._open_long_position(price, drop_percent, volume_ratio, volatility_pct)

        except Exception as e:
            log_error(self.user_id, f"Ошибка обработки сигнала падения: {e}", "FlashDropCatcher")

    async def _open_long_position(self, entry_price: Decimal, drop_percent: Decimal, volume_ratio: Decimal, volatility_pct: Decimal):
        """Открывает LONG позицию по текущей цене"""
        try:
            # Сохраняем информацию о сигнале для уведомления
            self.signal_drop_percent = drop_percent
            self.signal_volume_ratio = volume_ratio
            self.signal_volatility_pct = volatility_pct

            # Получаем параметры из конфигурации
            order_amount = self._convert_to_decimal(self.get_config_value("order_amount", 200.0))
            leverage = int(self.get_config_value("leverage", 2))

            # Устанавливаем плечо
            await self.api.set_leverage(symbol=self.symbol, leverage=leverage)

            # Получаем информацию о символе для правильного размера позиции
            instrument_info = await self.api.get_instruments_info(symbol=self.symbol)
            if not instrument_info:
                log_error(self.user_id, "Не удалось получить информацию о символе", "FlashDropCatcher")
                return

            # get_instruments_info возвращает словарь {symbol: info} для одного символа
            qty_step = instrument_info.get("qtyStep", Decimal("0.001"))

            # Рассчитываем размер позиции
            position_size = (order_amount * leverage) / entry_price
            position_size = (position_size // qty_step) * qty_step

            # Открываем LONG позицию
            log_info(self.user_id,
                    f"📈 Открываем LONG: {self.symbol}, размер: {position_size}, плечо: {leverage}x",
                    "FlashDropCatcher")

            # ИСПРАВЛЕНИЕ: Используем _place_order из базового класса для сохранения в БД!
            order_result = await self._place_order(
                side="Buy",
                order_type="Market",
                qty=Decimal(str(position_size))
            )

            # place_order() возвращает order_id (строку), а не словарь
            if order_result:
                self.trades_opened += 1  # Увеличиваем счётчик успешно открытых сделок
                self.position_active = True
                self.entry_time = datetime.now()  # Сохраняем время открытия позиции
                self.position_size = position_size
                self.active_direction = "LONG"

                # КРИТИЧНО: Получаем РЕАЛЬНУЮ цену исполнения с биржи (не используем цену сигнала!)
                try:
                    # Даём бирже время обработать ордер
                    await asyncio.sleep(0.5)

                    # Получаем реальную позицию с биржи
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

                # Сбрасываем trailing stop параметры
                self.highest_pnl = Decimal('0')
                self.current_trailing_level = 0
                self.last_trailing_notification_level = -1

                log_info(self.user_id, f"✅ LONG позиция открыта по РЕАЛЬНОЙ цене {self.entry_price:.8f}", "FlashDropCatcher")

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
                log_error(self.user_id, "Не удалось открыть позицию", "FlashDropCatcher")

        except Exception as e:
            log_error(self.user_id, f"Ошибка открытия LONG позиции: {e}", "FlashDropCatcher")

    async def _handle_price_update(self, event: PriceUpdateEvent):
        """Внутренний метод обработки обновления цены (вызывается из BaseStrategy)"""
        await self.handle_price_update(event)

    @strategy_locked
    async def handle_price_update(self, event: PriceUpdateEvent):
        """
        Обработчик обновлений цены для активной позиции.
        THREAD-SAFE: Защищено декоратором @strategy_locked.
        """
        if not self.position_active or event.symbol != self.symbol:
            return

        try:
            current_price = self._convert_to_decimal(event.price)

            # Рассчитываем текущий PnL
            current_pnl = await self._calculate_current_pnl(current_price)

            # Проверка 1: Hard stop loss при -500$
            if current_pnl <= self.HARD_STOP_LOSS_USDT:
                log_warning(self.user_id,
                           f"🛑 HARD STOP LOSS! PnL={current_pnl:.2f}$ достиг {self.HARD_STOP_LOSS_USDT}$",
                           "FlashDropCatcher")
                await self._close_position("hard_stop_loss")
                return

            # Проверка 2: Trailing stop в прибыли
            if current_pnl > Decimal('0'):
                await self._check_trailing_stop(current_pnl)

        except Exception as e:
            log_error(self.user_id, f"Ошибка обработки обновления цены: {e}", "FlashDropCatcher")

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

    async def _check_trailing_stop(self, current_pnl: Decimal):
        """Проверяет условия trailing stop"""
        # Обновляем максимальный PnL
        if current_pnl > self.highest_pnl:
            self.highest_pnl = current_pnl

            # Определяем новый уровень
            new_level = self._get_trailing_level(current_pnl)

            if new_level > self.current_trailing_level:
                self.current_trailing_level = new_level

                # Уведомление о новом уровне
                if new_level != self.last_trailing_notification_level:
                    log_info(self.user_id,
                            f"📈 Новый уровень трейлинга: {self._get_level_name(new_level)}, PnL=${current_pnl:.2f}",
                            "FlashDropCatcher")
                    self.last_trailing_notification_level = new_level

        # Проверяем откат для закрытия (20% от максимума)
        if self.current_trailing_level > 0:
            pullback_threshold = self.highest_pnl * Decimal('0.8')

            if current_pnl <= pullback_threshold:
                log_warning(self.user_id,
                           f"💰 TRAILING STOP! Откат 20% от максимума. Max={self.highest_pnl:.2f}$, Current={current_pnl:.2f}$",
                           "FlashDropCatcher")
                await self._close_position("trailing_stop_profit")

    async def _calculate_current_pnl(self, current_price: Decimal) -> Decimal:
        """Рассчитывает текущий PnL позиции"""
        if not self.position_active or not self.entry_price:
            return Decimal('0')

        # LONG позиция: PnL = (текущая_цена - цена_входа) × размер
        pnl = (current_price - self.entry_price) * self.position_size

        return pnl

    async def _close_position(self, reason: str):
        """Закрывает текущую позицию"""
        if not self.position_active:
            return

        try:
            log_info(self.user_id, f"🔄 Закрытие позиции: {self.symbol}, причина: {reason}", "FlashDropCatcher")

            # ИСПРАВЛЕНИЕ: Используем _place_order из базового класса для сохранения в БД!
            close_result = await self._place_order(
                side="Sell",  # Закрываем LONG через Sell
                order_type="Market",
                qty=Decimal(str(self.position_size)),
                reduce_only=True
            )

            # place_order() возвращает order_id (строку), а не словарь
            if close_result:
                # ТОЧНЫЙ РАСЧЕТ PnL: Берём РЕАЛЬНЫЕ данные от биржи (closedPnL)
                final_pnl = Decimal('0')
                exit_price = Decimal('0')
                commission = Decimal('0')

                try:
                    log_info(self.user_id, f"[BYBIT API] Запрашиваю реальный closedPnL от биржи для {self.symbol}...", "FlashDropCatcher")
                    closed_pnl_data = await self.api.get_closed_pnl(self.symbol, limit=1)

                    if closed_pnl_data:
                        # Используем ТОЧНЫЕ данные от биржи
                        final_pnl = closed_pnl_data['closedPnl']  # Чистый PnL (с вычетом комиссии)
                        entry_price_from_exchange = closed_pnl_data.get('avgEntryPrice', Decimal('0'))
                        closed_size = closed_pnl_data.get('closedSize', Decimal('0'))

                        # КРИТИЧНО: НЕ используем avgExitPrice из API - он может быть неточным!
                        # Вычисляем РЕАЛЬНУЮ цену выхода из чистого PnL
                        #
                        # Формула для LONG (FlashDropCatcher всегда LONG):
                        #   net_pnl = (exit_price - entry_price) * size - commission
                        #   exit_price = entry_price + (net_pnl + commission) / size

                        if closed_size > 0 and entry_price_from_exchange > 0:
                            # Используем РЕАЛЬНУЮ комиссию из конфигурации
                            from core.settings_config import EXCHANGE_FEES
                            from core.enums import ExchangeType
                            taker_fee_rate = EXCHANGE_FEES[ExchangeType.BYBIT]['taker'] / Decimal('100')  # 0.1% -> 0.001

                            # Общая комиссия = вход (taker) + выход (taker) = 0.1% + 0.1% = 0.2%
                            position_value = entry_price_from_exchange * closed_size
                            estimated_commission = position_value * taker_fee_rate * Decimal('2')  # Вход + Выход

                            # Вычисляем цену выхода с учетом комиссии (для LONG)
                            # exit_price = entry_price + (net_pnl + commission) / size
                            exit_price = entry_price_from_exchange + (final_pnl + estimated_commission) / closed_size
                            commission = estimated_commission

                            log_info(self.user_id,
                                    f"💰 Расчет закрытия: Вход=${entry_price_from_exchange:.4f}, PnL=${final_pnl:.4f}, "
                                    f"Размер={closed_size}, Комиссия≈${commission:.4f} → Выход=${exit_price:.4f}",
                                    "FlashDropCatcher")
                        else:
                            log_warning(self.user_id,
                                       f"⚠️ Не удалось вычислить цену выхода: size={closed_size}, entry={entry_price_from_exchange}",
                                       "FlashDropCatcher")
                            exit_price = Decimal('0')
                    else:
                        log_warning(self.user_id, f"⚠️ [BYBIT PNL] Не удалось получить closedPnL от биржи, используем unrealisedPnl", "FlashDropCatcher")
                        # ФОЛБЭК: Используем unrealisedPnl из позиции
                        positions = await self.api.get_positions(symbol=self.symbol)
                        if positions and isinstance(positions, list):
                            for pos in positions:
                                if pos["symbol"] == self.symbol:
                                    final_pnl = self._convert_to_decimal(pos.get("unrealisedPnl", 0))
                                    break

                except Exception as api_error:
                    log_error(self.user_id, f"❌ [BYBIT PNL] Ошибка запроса closedPnL: {api_error}, используем unrealisedPnl", "FlashDropCatcher")
                    # ФОЛБЭК: Используем unrealisedPnl из позиции
                    positions = await self.api.get_positions(symbol=self.symbol)
                    if positions and isinstance(positions, list):
                        for pos in positions:
                            if pos["symbol"] == self.symbol:
                                final_pnl = self._convert_to_decimal(pos.get("unrealisedPnl", 0))
                                break

                # СОХРАНЯЕМ значения перед сбросом для передачи в уведомление
                # ПОЛУЧАЕМ ИЗ БД для надёжности (работает даже после перезапуска бота)
                from database.db_trades import db_manager
                open_order = await db_manager.get_open_order_for_position(self.user_id, self.symbol, self.account_priority)
                if open_order:
                    saved_entry_time = open_order.get('filled_at')  # Время из БД
                    saved_entry_price = open_order.get('average_price')  # Цена из БД
                    log_debug(self.user_id, f"[ИЗ БД] Время входа: {saved_entry_time}, Цена входа: {saved_entry_price}", "FlashDropCatcher")
                else:
                    # Fallback на переменные в памяти (если БД недоступна)
                    saved_entry_time = self.entry_time
                    saved_entry_price = self.entry_price
                    log_warning(self.user_id, f"[FALLBACK] Не найден OPEN ордер в БД, используем данные из памяти", "FlashDropCatcher")

                # Сбрасываем параметры
                self.position_active = False
                self.entry_price = Decimal('0')
                self.entry_time = None  # Сбрасываем время входа
                self.position_size = Decimal('0')
                self.highest_pnl = Decimal('0')
                self.current_trailing_level = 0
                self.last_trailing_notification_level = -1

                # ИСПРАВЛЕНИЕ: Сбрасываем symbol в "ALL" чтобы стратегия могла входить в новые сделки!
                self.symbol = "ALL"
                self.active_direction = None

                log_info(self.user_id, f"✅ Позиция закрыта. PnL: ${final_pnl:.2f}", "FlashDropCatcher")

                # ИСПОЛЬЗУЕМ БАЗОВЫЙ МЕТОД для отправки уведомления (с временем и ценами)
                await self._send_trade_close_notification(
                    pnl=final_pnl,
                    commission=commission,
                    exit_price=exit_price if exit_price > Decimal('0') else None,
                    entry_price=saved_entry_price,
                    entry_time=saved_entry_time
                )

            else:
                log_error(self.user_id, "Не удалось закрыть позицию", "FlashDropCatcher")

        except Exception as e:
            log_error(self.user_id, f"Ошибка закрытия позиции: {e}", "FlashDropCatcher")

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
        """Возвращает детальную информацию о текущей позиции для команды /trade_details"""
        try:
            if not self.position_active:
                return {
                    "has_position": False,
                    "symbol": self.symbol,
                    "strategy_type": StrategyType.FLASH_DROP_CATCHER.value,
                    "account_priority": self.account_priority
                }

            current_price = await self._get_current_market_price()
            if not current_price or current_price == Decimal('0'):
                current_price = self.entry_price

            current_pnl = await self._calculate_current_pnl(current_price)
            price_change_percent = Decimal('0')
            if self.entry_price > 0:
                price_change_percent = ((current_price - self.entry_price) / self.entry_price) * Decimal('100')

            breakeven_price = None
            try:
                positions = await self.api.get_positions(symbol=self.symbol)
                # get_positions() возвращает List[Dict]
                if positions and isinstance(positions, list) and len(positions) > 0:
                    breakeven_price_from_exchange = positions[0].get("breakEvenPrice", None)
                    if breakeven_price_from_exchange:
                        breakeven_price = self._convert_to_decimal(breakeven_price_from_exchange)
            except Exception as e:
                log_warning(self.user_id, f"Не удалось получить breakEvenPrice: {e}", "FlashDropCatcher")

            return {
                "has_position": True,
                "symbol": self.symbol,
                "strategy_type": StrategyType.FLASH_DROP_CATCHER.value,
                "account_priority": self.account_priority,
                "position": {
                    "direction": self.active_direction,
                    "entry_price": float(self.entry_price),
                    "current_price": float(current_price),
                    "position_size": float(self.position_size),
                    "total_position_size": float(self.position_size)
                },
                "averaging": {
                    "count": 0,
                    "executed": 0,
                    "average_entry_price": None,
                    "effective_entry_price": float(self.entry_price),
                    "breakeven_price": float(breakeven_price) if breakeven_price else None,
                    "use_breakeven_exit": False
                },
                "margin": {
                    "initial_margin": float(self.get_config_value("order_amount", 200.0)),
                    "current_total_margin": float(self.get_config_value("order_amount", 200.0)),
                    "total_fees_paid": 0.0
                },
                "pnl": {
                    "unrealized_pnl": float(current_pnl),
                    "price_change_percent": float(price_change_percent),
                    "peak_profit": float(self.highest_pnl)
                },
                "trailing_stop": {
                    "current_level": self.current_trailing_level,
                    "level_name": self._get_level_name(self.current_trailing_level),
                    "highest_pnl": float(self.highest_pnl)
                },
                "stop_loss": {
                    "has_stop_loss": True,
                    "stop_loss_type": "hard_stop",
                    "stop_loss_price": None,
                    "stop_loss_usdt": float(self.HARD_STOP_LOSS_USDT)
                }
            }

        except Exception as e:
            log_error(self.user_id, f"Ошибка получения детального статуса: {e}", "FlashDropCatcher")
            return {
                "has_position": False,
                "symbol": self.symbol,
                "strategy_type": StrategyType.FLASH_DROP_CATCHER.value,
                "account_priority": self.account_priority,
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

                # Проверяем, включены ли heartbeat уведомления в Telegram (используем загруженное значение)
                enable_heartbeat = self.ENABLE_HEARTBEAT

                # Формируем сообщение о статусе
                elapsed_time = datetime.now() - self.last_heartbeat_time
                elapsed_minutes = int(elapsed_time.total_seconds() / 60)

                # Статус позиции
                position_status = "🟢 НЕТ АКТИВНЫХ ПОЗИЦИЙ" if not self.position_active else f"🔵 АКТИВНА ПОЗИЦИЯ: {self.symbol}"

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

    async def _handle_manual_close(self, event):
        """
        МГНОВЕННЫЙ обработчик ручного закрытия позиции через WebSocket.
        Вызывается когда пользователь вручную закрыл позицию на бирже.
        """
        from core.events import PositionClosedEvent

        if not isinstance(event, PositionClosedEvent):
            return

        # Проверяем что это наш символ
        if event.symbol != self.symbol or not self.position_active:
            return

        log_warning(self.user_id,
                   f"⚠️ ОБРАБОТКА РУЧНОГО ЗАКРЫТИЯ через WebSocket: {event.symbol}",
                   "FlashDropCatcher")

        try:
            # Получаем ТОЧНЫЙ PnL от биржи
            final_pnl = Decimal('0')
            exit_price = Decimal('0')
            commission = Decimal('0')

            try:
                closed_pnl_data = await self.api.get_closed_pnl(self.symbol, limit=1)
                if closed_pnl_data:
                    final_pnl = closed_pnl_data['closedPnl']  # Чистый PnL (с вычетом комиссии)
                    entry_price_from_exchange = closed_pnl_data.get('avgEntryPrice', Decimal('0'))
                    closed_size = closed_pnl_data.get('closedSize', Decimal('0'))

                    # КРИТИЧНО: НЕ используем avgExitPrice из API - он может быть неточным!
                    # Вычисляем РЕАЛЬНУЮ цену выхода из чистого PnL (формула для LONG)

                    if closed_size > 0 and entry_price_from_exchange > 0:
                        # Используем РЕАЛЬНУЮ комиссию из конфигурации
                        from core.settings_config import EXCHANGE_FEES
                        from core.enums import ExchangeType
                        taker_fee_rate = EXCHANGE_FEES[ExchangeType.BYBIT]['taker'] / Decimal('100')  # 0.1% -> 0.001

                        # Общая комиссия = вход (taker) + выход (taker) = 0.1% + 0.1% = 0.2%
                        position_value = entry_price_from_exchange * closed_size
                        estimated_commission = position_value * taker_fee_rate * Decimal('2')  # Вход + Выход

                        # Вычисляем цену выхода с учетом комиссии (для LONG)
                        # exit_price = entry_price + (net_pnl + commission) / size
                        exit_price = entry_price_from_exchange + (final_pnl + estimated_commission) / closed_size
                        commission = estimated_commission

                        log_info(self.user_id,
                                f"💰 Расчет закрытия: Вход=${entry_price_from_exchange:.4f}, PnL=${final_pnl:.4f}, "
                                f"Размер={closed_size}, Комиссия≈${commission:.4f} → Выход=${exit_price:.4f}",
                                "FlashDropCatcher")
                    else:
                        log_warning(self.user_id,
                                   f"⚠️ Не удалось вычислить цену выхода: size={closed_size}, entry={entry_price_from_exchange}",
                                   "FlashDropCatcher")
            except Exception as api_error:
                log_error(self.user_id, f"❌ Ошибка получения closedPnL: {api_error}", "FlashDropCatcher")

            # Получаем данные из БД
            from database.db_trades import db_manager
            open_order = await db_manager.get_open_order_for_position(self.user_id, self.symbol, self.account_priority)
            if open_order:
                saved_entry_time = open_order.get('filled_at')
                saved_entry_price = open_order.get('average_price')

                # Обновляем БД - закрываем ордер
                try:
                    await db_manager.close_order(
                        order_id=open_order['order_id'],
                        close_price=float(exit_price) if exit_price > Decimal('0') else None,
                        close_size=float(self.position_size) if self.position_size > 0 else None,
                        realized_pnl=float(final_pnl),
                        close_reason="manual_close_by_user"
                    )
                    log_info(self.user_id, f"✅ Ордер {open_order['order_id']} закрыт в БД (ручное закрытие)", "FlashDropCatcher")
                except Exception as db_error:
                    log_error(self.user_id, f"❌ Ошибка обновления БД: {db_error}", "FlashDropCatcher")
            else:
                saved_entry_time = self.entry_time
                saved_entry_price = self.entry_price

            # Сбрасываем состояние
            self.position_active = False
            self.entry_price = Decimal('0')
            self.entry_time = None
            self.position_size = Decimal('0')
            self.highest_pnl = Decimal('0')
            self.current_trailing_level = 0
            self.last_trailing_notification_level = -1

            log_info(self.user_id, f"✅ Позиция закрыта вручную. PnL: ${final_pnl:.2f}", "FlashDropCatcher")

            # Отправляем уведомление
            await self._send_trade_close_notification(
                pnl=final_pnl,
                commission=commission,
                exit_price=exit_price if exit_price > Decimal('0') else None,
                entry_price=saved_entry_price,
                entry_time=saved_entry_time
            )

        except Exception as e:
            log_error(self.user_id, f"❌ Ошибка обработки ручного закрытия: {e}", "FlashDropCatcher")

    async def _execute_strategy_logic(self):
        """Базовый метод выполнения логики (не используется в этой стратегии)"""
        pass

    async def _handle_order_filled(self, event):
        """Обработка исполнения ордера (не используется в этой стратегии)"""
        pass