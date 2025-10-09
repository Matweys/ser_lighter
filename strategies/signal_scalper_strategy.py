import asyncio
import time
from typing import Dict, Any, Optional
from decimal import Decimal, getcontext

from api.bybit_api import BybitAPI
from .base_strategy import BaseStrategy
from core.enums import StrategyType, EventType
from core.logger import log_info, log_error, log_warning, log_debug
from core.events import EventBus, NewCandleEvent, PriceUpdateEvent, OrderFilledEvent
from analysis.signal_analyzer import SignalAnalyzer, SignalAnalysisResult
from analysis.spike_detector import SpikeDetector

getcontext().prec = 28


class SignalScalperStrategy(BaseStrategy):
    """
    Стратегия скальпинга, основанная на внешних сигналах от SignalAnalyzer (EMA + RSI).
    Реализует сложную логику входа, удержания и выхода из позиции.
    """

    def __init__(self, user_id: int, symbol: str, signal_data: Dict[str, Any], api: BybitAPI, event_bus: EventBus,
                 bot: "Bot", config: Optional[Dict] = None):
        super().__init__(user_id, symbol, signal_data, api, event_bus, bot, config)

        # Компоненты
        self.signal_analyzer: Optional[SignalAnalyzer] = None
        self.spike_detector: Optional[SpikeDetector] = None  # Детектор всплесков для оптимизации входа

        # Состояние стратегии
        self.position_active = False
        self.active_direction: Optional[str] = None  # "LONG" или "SHORT"
        self.entry_price: Optional[Decimal] = None
        self.position_size: Optional[Decimal] = None
        self.last_closed_direction: Optional[str] = None
        self.hold_signal_counter = 0
        self.peak_profit_usd: Decimal = Decimal('0')
        self.is_waiting_for_trade = False  # Флаг для блокировки действий
        self.processed_orders: set = set()  # Отслеживание обработанных ордеров
        self.current_order_id: Optional[str] = None  # ID текущего ожидаемого ордера
        self.intended_order_amount: Optional[Decimal] = None  # Запрошенная сумма ордера
        self.close_reason: Optional[str] = None  # Причина закрытия позиции для передачи в _handle_order_filled

        # Стоп-лосс управление
        self.stop_loss_order_id: Optional[str] = None
        self.stop_loss_price: Optional[Decimal] = None

        # Система подтверждения сигналов и кулдауна
        self.last_signal: Optional[str] = None  # Последний полученный сигнал
        self.signal_confirmation_count = 0  # Счетчик одинаковых сигналов подряд
        self.required_confirmations = 1  # Требуемое количество подтверждений
        self.last_trade_close_time: Optional[float] = None  # Время закрытия последней сделки
        self.cooldown_seconds = 60  # Кулдаун в секундах (1 минута)
        self.last_trade_was_loss = False  # Была ли последняя сделка убыточной

        # СИСТЕМА КОНТРОЛЯ РЕВЕРСОВ
        self.last_reversal_time: Optional[float] = None  # Время последнего реверса
        self.reversal_cooldown_seconds = 60  # Кулдаун после реверса в секундах (1 минута)
        self.reversal_required_confirmations = 1  # Требуемые подтверждения после реверса
        self.after_reversal_mode = False  # Флаг: находимся ли мы в режиме после реверса


        # ОСНОВНАЯ СИСТЕМА УСРЕДНЕНИЯ (ОДИНОЧНОЕ УТРОЕНИЕ)
        self.averaging_enabled = False  # Включена ли система усреднения
        self.averaging_executed = False  # Флаг: было ли выполнено усреднение
        self.averaging_count = 0  # Счетчик выполненных усреднений
        self.max_averaging_count = 1  # Максимальное количество усреднений (из конфигурации)
        self.averaging_trigger_loss_percent = Decimal('25.0')  # Триггер: убыток от маржи
        self.averaging_multiplier = Decimal('2.0')  # Удвоение суммы
        self.averaging_stop_loss_percent = Decimal('30.0')  # Программный SL: от маржи
        self.total_position_size = Decimal('0')  # Общий размер позиции после усреднения
        self.average_entry_price = Decimal('0')  # Средняя цена входа после усреднения
        self.initial_margin_usd = Decimal('0')  # Начальная маржа для расчета % убытка
        self.total_fees_paid = Decimal('0')  # Накопленные комиссии
        self.intermediate_averaging_executed = False  # Флаг: было ли промежуточное усреднение (legacy)

        # ИЗОЛЯЦИЯ НАСТРОЕК ДЛЯ АКТИВНОЙ СДЕЛКИ
        self.active_trade_config = None  # Конфигурация, зафиксированная при входе в сделку
        self.config_frozen = False  # Флаг: заморожены ли настройки для активной сделки

        # ============================================================
        # ДЕТЕКТОР ЗАСТРЯВШЕЙ ЦЕНЫ (STAGNATION DETECTOR)
        # Легко удалить: удалите этот блок и связанные методы
        # ============================================================
        self.stagnation_detector_enabled = False  # Включен ли детектор
        self.stagnation_check_interval = 30  # Время наблюдения в секундах (1 минута)
        self.stagnation_ranges = []  # Список диапазонов {"min": -15.0, "max": -20.0}
        self.stagnation_averaging_multiplier = Decimal('2.0')  # Множитель усреднения (x2)
        self.stagnation_averaging_leverage = 1  # Плечо для усреднения (x1)


        # Мониторинг состояния детектора
        self.stagnation_monitor_active = False  # Активен ли мониторинг
        self.stagnation_monitor_start_time: Optional[float] = None  # Время начала мониторинга
        self.stagnation_current_range_index: Optional[int] = None  # Индекс текущего отслеживаемого диапазона
        self.stagnation_averaging_executed = False  # Флаг: было ли выполнено усреднение
        # ============================================================


    def _get_strategy_type(self) -> StrategyType:
        return StrategyType.SIGNAL_SCALPER

    def _get_frozen_config_value(self, key: str, default: Any = None) -> Any:
        """
        Получает значение из ЗАМОРОЖЕННОЙ конфигурации для активной сделки.
        Если сделка не активна, возвращает текущее значение конфигурации.
        """
        if self.config_frozen and self.active_trade_config:
            return self.active_trade_config.get(key, default)
        else:
            return self.get_config_value(key, default)

    async def _load_strategy_config(self):
        """Переопределяем для инициализации SignalAnalyzer и SpikeDetector."""
        await super()._load_strategy_config()
        if self.config:
            self.signal_analyzer = SignalAnalyzer(self.user_id, self.api, self.config)

            # Инициализируем детектор всплесков для оптимального входа
            self.spike_detector = SpikeDetector(
                user_id=self.user_id,
                symbol=self.symbol,
                lookback=50,
                threshold=0.001  # 0.1% порог для всплеска
            )
            log_info(self.user_id, f"📡 SpikeDetector инициализирован для {self.symbol}", "SignalScalper")

            # ============================================================
            # ЗАГРУЗКА ПАРАМЕТРОВ ДЕТЕКТОРА ЗАСТРЯВШЕЙ ЦЕНЫ
            # ============================================================
            self.stagnation_detector_enabled = self.config.get("enable_stagnation_detector", True)
            self.stagnation_check_interval = int(self.config.get("stagnation_check_interval_seconds", 30))
            # НОВАЯ СИСТЕМА: диапазоны в процентах от маржи
            self.stagnation_ranges = self.config.get("stagnation_ranges_percent", [])
            self.stagnation_averaging_multiplier = self._convert_to_decimal(self.config.get("stagnation_averaging_multiplier", "1.0"))
            self.stagnation_averaging_leverage = int(self.config.get("stagnation_averaging_leverage", 1))

            # ============================================================

            # Загружаем параметры ОСНОВНОГО усреднения (одиночное утроение)
            self.averaging_enabled = self.config.get("enable_averaging", True)
            self.max_averaging_count = int(self.config.get("max_averaging_count", 1))
            self.averaging_trigger_loss_percent = self._convert_to_decimal(self.config.get("averaging_trigger_loss_percent", "25.0"))
            self.averaging_multiplier = self._convert_to_decimal(self.config.get("averaging_multiplier", "2.0"))
            self.averaging_stop_loss_percent = self._convert_to_decimal(self.config.get("averaging_stop_loss_percent", "30.0"))

    async def start(self) -> bool:
        """Запуск стратегии и подписка на события свечей."""
        is_started = await super().start()
        if is_started:
            # Основной триггер стратегии - новая 5-минутная свеча
            await self.event_bus.subscribe(EventType.NEW_CANDLE, self._handle_new_candle, user_id=self.user_id)
        return is_started

    async def stop(self, reason: str = "Manual stop"):
        """Остановка стратегии и отписка от событий."""
        await self.event_bus.unsubscribe(self._handle_new_candle)
        await super().stop(reason)

    async def _handle_new_candle(self, event: NewCandleEvent):
        """Главный обработчик логики на каждой новой свече."""
        if event.symbol != self.symbol:
            return

        # SPIKE DETECTOR: Обрабатываем 1-минутные свечи для детектора всплесков
        if event.interval == '1m' and self.spike_detector:
            # Добавляем закрытую 1-минутную свечу в детектор
            close_price = event.candle_data.get("close")
            timestamp = event.candle_data.get("timestamp")
            if close_price:
                self.spike_detector.add_candle(close_price, timestamp=timestamp)
            return  # Не продолжаем обработку для 1-минутных свечей

        # ОСНОВНАЯ ЛОГИКА: Обрабатываем только 5-минутные свечи для торговли
        if self.is_waiting_for_trade:
            return

        config_timeframe = self.get_config_value('analysis_timeframe', '5m')
        if event.interval != config_timeframe:
            return

        log_debug(self.user_id, f"SignalScalper ({self.symbol}) получил новую {event.interval} свечу.", "SignalScalper")
        analysis_result = await self.signal_analyzer.get_analysis(self.symbol)

        if not analysis_result:
            return

        signal = analysis_result.direction
        price = analysis_result.price
        log_info(self.user_id,
                 f"Анализ для {self.symbol}: Сигнал={signal}, Цена={price:.4f}, Индикаторы={analysis_result.indicators}",
                 "SignalScalper")

        # --- Конечный автомат логики ---
        if self.position_active:
            # Правило 4: Реверс позиции при смене сигнала (ВРЕМЕННО БЕЗ ПРОВЕРКИ PnL)
            if (signal == "LONG" and self.active_direction == "SHORT") or \
                    (signal == "SHORT" and self.active_direction == "LONG"):
                current_pnl = await self._calculate_current_pnl(price)

                # ВРЕМЕННО ОТКЛЮЧЕНО: Проверка PnL для реверса (чтобы вернуть - раскомментируй блок ниже)
                if current_pnl >= 0:
                    log_warning(self.user_id,
                            f"СМЕНА СИГНАЛА! Реверс позиции по {self.symbol} с {self.active_direction} на {signal} при PnL={current_pnl:.2f}$.",
                            "SignalScalper")
                # ЕСЛИ ВОЗВРАЩАЕШЬ РЕВЕРС УДАЛИ ЭТУ СТРОКУ ИЛИ ЗАКОММЕНТИРУЙ
                #await self._reverse_position(new_direction=signal)
                else:
                    log_info(self.user_id,
                            f"Сигнал на реверс с {self.active_direction} на {signal}, но позиция в убытке {current_pnl:.2f} USDT. Ожидаем улучшения.",
                            "SignalScalper")

            # Правило 5: Закрытие при двух "HOLD" подряд (только при положительном PnL)
            elif signal == "HOLD":
                self.hold_signal_counter += 1
                if self.hold_signal_counter >= 2:
                    # Получаем текущий PnL для проверки
                    current_pnl = await self._calculate_current_pnl(price)

                    if current_pnl >= 0:
                        log_warning(self.user_id,
                                    f"Два сигнала 'HOLD' подряд. Закрытие позиции {self.symbol} с прибылью {current_pnl:.2f} USDT.",
                                    "SignalScalper")
                        await self._close_position("double_hold_signal")
                    else:
                        log_info(self.user_id,
                                f"Два сигнала 'HOLD' подряд, но позиция в убытке {current_pnl:.2f} USDT. Ожидаем улучшения.",
                                "SignalScalper")
                        # Сбрасываем счетчик, чтобы дать позиции еще один шанс
                        self.hold_signal_counter = 0

            # Сбрасываем счетчик HOLD, если сигнал изменился
            else:
                self.hold_signal_counter = 0
        else:
            # Правило 1: Вход в новую сделку
            if signal in ["LONG", "SHORT"]:
                # Проверка обычного кулдауна после закрытия сделки
                if self._is_cooldown_active():
                    return

                # НОВАЯ ПРОВЕРКА: Проверка кулдауна после реверса
                if self._is_reversal_cooldown_active():
                    return

                # Проверка подтверждения сигнала (включает логику после реверса)
                if not self._is_signal_confirmed(signal):
                    return

                # НОВАЯ ПРОВЕРКА: Spike Detector для оптимального входа
                if self.spike_detector:
                    should_enter, spike_reason = self.spike_detector.should_enter_on_pullback(signal)

                    # Получаем статистику для логирования
                    recent_spikes = self.spike_detector.get_recent_spikes(seconds=300)
                    total_spikes = len(self.spike_detector.spike_history)
                    candles_count = len(self.spike_detector.price_history)

                    if not should_enter:
                        log_info(self.user_id,
                                f"⏸️ Spike Detector ({candles_count} свечей, {len(recent_spikes)}/{total_spikes} всплесков за 5мин): {spike_reason}",
                                "SignalScalper")
                        return

                    log_info(self.user_id,
                            f"✅ Spike Detector ({candles_count} свечей, {len(recent_spikes)}/{total_spikes} всплесков за 5мин): {spike_reason}",
                            "SignalScalper")

                # Правило 1.1: Пропуск сигнала для "успокоения" рынка
                if signal == self.last_closed_direction:
                    log_info(self.user_id,
                             f"Пропуск сигнала {signal} для {self.symbol} (совпадает с последней закрытой сделкой).",
                             "SignalScalper")
                    self.last_closed_direction = None  # Сбрасываем, чтобы следующий сигнал вошел
                else:
                    await self._enter_position(direction=signal, signal_price=price)
            else:
                # При сигнале HOLD сбрасываем счетчик подтверждений
                self.signal_confirmation_count = 0
                self.last_signal = None

    async def _handle_price_update(self, event: PriceUpdateEvent):
        """Внутренний метод обработки обновления цены (вызывается из BaseStrategy)"""
        await self.handle_price_update(event)

    async def handle_price_update(self, event: PriceUpdateEvent):
        """Обработка тиков цены для усреднения и динамического тейк-профита."""
        # КРИТИЧЕСКИ ВАЖНО: Проверяем что это цена НАШЕГО символа!
        if event.symbol != self.symbol:
            return

        if not self.position_active or not self.entry_price or self.is_waiting_for_trade:
            return

        current_price = event.price

        # Защита от неправильных цен
        if current_price <= 0:
            return

        # Проверка на адекватность изменения цены (не больше 50% от цены входа)
        price_change_percent = abs((current_price - self.entry_price) / self.entry_price * 100)
        if price_change_percent > 50:
            return

        # Используем среднюю цену входа если есть усреднения
        entry_price_to_use = self.average_entry_price if self.average_entry_price > 0 else self.entry_price
        position_size_to_use = self.total_position_size if self.total_position_size > 0 else self.position_size

        # Рассчитываем PnL относительно средней цены входа
        if self.active_direction == "LONG":
            pnl = (current_price - entry_price_to_use) * position_size_to_use
        else:  # SHORT
            pnl = (entry_price_to_use - current_price) * position_size_to_use


        # ============================================================
        # ДЕТЕКТОР ЗАСТРЯВШЕЙ ЦЕНЫ (STAGNATION DETECTOR)
        # ============================================================
        # Проверяем детектор стагнации (работает параллельно с другими триггерами)
        if not self.intermediate_averaging_executed and not self.averaging_executed and not self.stagnation_averaging_executed:
            # Проверяем условия детектора
            if await self._check_stagnation_detector(pnl):
                # Триггер сработал! Выполняем усреднение
                await self._execute_stagnation_averaging(current_price)
                # Детектор теперь отключится автоматически через флаг stagnation_averaging_executed

        # ЛОГИКА ВЫХОДА ПОСЛЕ УСРЕДНЕНИЯ ПО ДЕТЕКТОРУ СТАГНАЦИИ
        if self.stagnation_averaging_executed:
            # После усреднения по стагнации: выходим только в плюс по трейлингу
            if pnl > 0:
                # Закрытие в плюс (трейлинг)
                log_warning(self.user_id,
                           f"💰 ЗАКРЫТИЕ В ПЛЮС ПОСЛЕ УСРЕДНЕНИЯ ПО СТАГНАЦИИ! PnL=${pnl:.2f}",
                           "SignalScalper")
                await self._close_position("profit_after_stagnation_averaging")
                return
        # ============================================================

        # ОСНОВНОЕ УСРЕДНЕНИЕ (ОДИНОЧНОЕ УТРОЕНИЕ)
        if self.averaging_enabled and not self.averaging_executed:
            # Рассчитываем % убытка от начальной маржи
            if self.initial_margin_usd > 0:
                loss_percent_from_margin = (abs(pnl) / self.initial_margin_usd) * Decimal('100') if pnl < 0 else Decimal('0')

                log_debug(self.user_id,
                         f"📊 Мониторинг усреднения: PnL=${pnl:.2f}, маржа=${self.initial_margin_usd:.2f}, "
                         f"убыток={loss_percent_from_margin:.2f}%, триггер={self.averaging_trigger_loss_percent}%",
                         "SignalScalper")

                # Проверяем триггер усреднения: убыток >= 3% от маржи
                if loss_percent_from_margin >= self.averaging_trigger_loss_percent:
                    log_warning(self.user_id,
                               f"🎯 ТРИГГЕР УСРЕДНЕНИЯ! Убыток {loss_percent_from_margin:.2f}% >= {self.averaging_trigger_loss_percent}% от маржи",
                               "SignalScalper")
                    await self._execute_averaging(current_price)

        # Обновляем пиковую прибыль
        if pnl > self.peak_profit_usd:
            self.peak_profit_usd = pnl

        # НОВАЯ СИСТЕМА: Поэтапный трейлинг с динамическими порогами и 20% откатом
        current_trailing_level = self._get_trailing_level(pnl)

        if current_trailing_level > 0:  # Если достигли хотя бы начального уровня
            # Фиксированный 20% откат от пика на всех уровнях
            trailing_distance = self.peak_profit_usd * Decimal('0.20')

            # Проверяем условие закрытия: откат от пика >= 20%
            if pnl < (self.peak_profit_usd - trailing_distance):
                level_name = self._get_level_name(current_trailing_level)
                log_info(self.user_id,
                         f"💎 ЗАКРЫТИЕ НА {level_name}! Пик: ${self.peak_profit_usd:.2f}, PnL: ${pnl:.2f}, откат: ${trailing_distance:.2f} (20%)",
                         "SignalScalper")
                await self._close_position("level_trailing_profit")
            else:
                # Логируем текущий статус трейлинга
                level_name = self._get_level_name(current_trailing_level)
                log_debug(self.user_id,
                         f"Трейлинг {level_name}: пик=${self.peak_profit_usd:.2f}, PnL=${pnl:.2f}, откат допустим=${trailing_distance:.2f}",
                         "SignalScalper")

    async def _enter_position(self, direction: str, signal_price: Decimal):
        """Логика входа в позицию."""

        # ЗАЩИТА: Если позиция уже активна в стратегии - не открываем новую
        if self.position_active:
            log_warning(self.user_id,
                      f"⚠️ Позиция уже активна ({self.active_direction}). Новый сигнал {direction} ИГНОРИРУЕТСЯ.",
                      "SignalScalper")
            return

        self.is_waiting_for_trade = True

        # Сохраняем цену сигнала для передачи в уведомление
        self.signal_price = signal_price

        # Получаем актуальные данные ТОЛЬКО ОДИН РАЗ перед созданием ордера
        await self._force_config_reload()

        # КРИТИЧНО: Обновляем параметры усреднения из свежезагруженного конфига
        self.max_averaging_count = int(self.config.get("max_averaging_count", 1))
        self.averaging_trigger_loss_percent = self._convert_to_decimal(self.config.get("averaging_trigger_loss_percent", "15.0"))
        self.averaging_multiplier = self._convert_to_decimal(self.config.get("averaging_multiplier", "2.0"))
        self.averaging_stop_loss_percent = self._convert_to_decimal(self.config.get("averaging_stop_loss_percent", "30.0"))

        log_info(self.user_id,
                f"🔧 Параметры усреднения обновлены: триггер={self.averaging_trigger_loss_percent}%, "
                f"SL={self.averaging_stop_loss_percent}%, множитель={self.averaging_multiplier}, лимит={self.max_averaging_count}",
                "SignalScalper")

        # ЗАМОРАЖИВАЕМ КОНФИГУРАЦИЮ ДЛЯ ЭТОЙ СДЕЛКИ
        self.active_trade_config = self.config.copy()  # Полная копия конфигурации
        self.config_frozen = True
        log_info(self.user_id, f"Конфигурация заморожена для сделки по {self.symbol}: order_amount={self.active_trade_config.get('order_amount')}, leverage={self.active_trade_config.get('leverage')}", "SignalScalper")

        await self._set_leverage()
        order_amount = self._convert_to_decimal(self.get_config_value("order_amount", 50.0))
        leverage = self._convert_to_decimal(self.get_config_value("leverage", 1.0))
        self.intended_order_amount = order_amount  # Сохраняем запрошенную сумму
        qty = await self.api.calculate_quantity_from_usdt(self.symbol, order_amount, leverage, price=signal_price)

        if qty <= 0:
            await self.stop("Calculated order quantity is zero")
            self.is_waiting_for_trade = False
            return

        side = "Buy" if direction == "LONG" else "Sell"
        order_id = await self._place_order(side=side, order_type="Market", qty=qty)

        if order_id:
            self.current_order_id = order_id  # Сохраняем ID ожидаемого ордера
            await self._await_order_fill(order_id, side=side, qty=qty)
        else:
            self.is_waiting_for_trade = False


    async def _close_position(self, reason: str):
        """Логика закрытия текущей позиции."""
        if not self.position_active:
            return

        log_info(self.user_id, f"Закрытие позиции {self.symbol}. Причина: {reason}", "SignalScalper")
        self.is_waiting_for_trade = True
        self.close_reason = reason  # Сохраняем причину для использования в _handle_order_filled
        side = "Sell" if self.active_direction == "LONG" else "Buy"

        # Используем общий размер позиции с учетом усреднений
        position_size_to_close = self.total_position_size if self.total_position_size > 0 else self.position_size

        order_id = await self._place_order(side=side, order_type="Market", qty=position_size_to_close, reduce_only=True)

        if order_id:
            self.current_order_id = order_id  # Сохраняем ID ожидаемого ордера
            await self._await_order_fill(order_id, side=side, qty=position_size_to_close)
        else:
            self.is_waiting_for_trade = False

    async def _reverse_position(self, new_direction: str):
        """Закрывает текущую позицию и УСТАНАВЛИВАЕТ ЗАДЕРЖКУ перед открытием новой."""
        # Сначала закрываем текущую
        await self._close_position(reason=f"reversing_to_{new_direction}")

        # НОВАЯ ЛОГИКА: Устанавливаем флаг реверса вместо немедленного открытия новой позиции
        self.last_reversal_time = time.time()
        self.after_reversal_mode = True

        # Сбрасываем счетчики сигналов для требования новых подтверждений
        self.signal_confirmation_count = 0
        self.last_signal = None

        log_warning(self.user_id,
                   f"🔄 РЕВЕРС ВЫПОЛНЕН! Установлена задержка {self.reversal_cooldown_seconds} сек. "
                   f"Следующему сигналу {new_direction} потребуется {self.reversal_required_confirmations} подтверждения.",
                   "SignalScalper")

    async def _handle_order_filled(self, event: OrderFilledEvent):
        """Обработка исполненных ордеров."""
        # УЛУЧШЕННАЯ ЗАЩИТА ОТ ДВОЙНОЙ ОБРАБОТКИ
        if event.order_id in self.processed_orders:
            log_debug(self.user_id, f"[ДУПЛИКАТ] Ордер {event.order_id} уже обработан, игнорируем EventBus дубликат.", "SignalScalper")
            return

        # КРИТИЧЕСКИ ВАЖНО: Проверяем что ордер принадлежит БОТУ (есть в БД)
        from database.db_trades import db_manager
        try:
            order_in_db = await db_manager.get_order_by_id(event.order_id)

            if not order_in_db:
                log_warning(self.user_id,
                           f"⚠️ [НЕ НАШ ОРДЕР] Ордер {event.order_id} НЕ найден в БД бота! "
                           f"Это РУЧНОЙ ордер пользователя или внешний. ИГНОРИРУЮ.",
                           "SignalScalper")
                return

            # Проверяем что ордер принадлежит ЭТОЙ стратегии (symbol и user_id)
            if order_in_db['symbol'] != self.symbol or order_in_db['user_id'] != self.user_id:
                log_debug(self.user_id,
                         f"[НЕ НАШ] Ордер {event.order_id} принадлежит другой стратегии или пользователю. ИГНОРИРУЮ.",
                         "SignalScalper")
                return

            log_info(self.user_id, f"✅ [НАША СДЕЛКА] Ордер {event.order_id} подтверждён в БД, обрабатываем.", "SignalScalper")

        except Exception as db_check_error:
            log_error(self.user_id,
                     f"❌ Ошибка проверки ордера {event.order_id} в БД: {db_check_error}. "
                     f"НЕ МОГУ ПОДТВЕРДИТЬ ПРИНАДЛЕЖНОСТЬ - ИГНОРИРУЮ из безопасности!",
                     "SignalScalper")
            return

        # НЕМЕДЛЕННО добавляем ордер в обработанные чтобы блокировать повторную обработку
        self.processed_orders.add(event.order_id)
        self.current_order_id = None  # Сбрасываем ожидаемый ордер

        log_info(self.user_id, f"[ОБРАБОТКА] Обрабатываем ордер {event.order_id} ({event.side} {event.qty} {self.symbol})", "SignalScalper")

        # УМНАЯ МНОГОУРОВНЕВАЯ ЛОГИКА ОПРЕДЕЛЕНИЯ ТИПА ОРДЕРА

        # ПЕРВИЧНАЯ проверка по reduce_only флагу (наиболее надежно)
        is_closing_order = hasattr(event, 'reduce_only') and event.reduce_only

        # ВТОРИЧНАЯ проверка по направлению ордера (fallback для случаев без reduce_only)
        if not is_closing_order and self.position_active:
            # Для закрытия позиции используется противоположное направление
            expected_closing_side = "Sell" if self.active_direction == "LONG" else "Buy"

            # Если ордер в направлении закрытия и НЕТ флага reduce_only - скорее всего это закрытие
            if event.side == expected_closing_side:
                is_closing_order = True
                log_info(self.user_id, f"[FALLBACK] Ордер {event.order_id} определен как ЗАКРЫТИЕ по направлению: {event.side} (ожидалось {expected_closing_side})", "SignalScalper")

        # ДЕТАЛЬНОЕ логирование для диагностики
        log_info(self.user_id,
                f"[ДИАГНОСТИКА] Ордер {event.order_id}: "
                f"side={event.side}, qty={event.qty}, price={event.price}, "
                f"reduce_only={getattr(event, 'reduce_only', 'НЕТ')}, "
                f"position_active={self.position_active}, active_direction={self.active_direction}, "
                f"is_closing={is_closing_order}",
                "SignalScalper")

        # Определение усреднения: позиция активна + НЕ закрытие + правильное направление
        is_averaging_order = False
        if self.position_active and not is_closing_order:
            expected_averaging_side = "Buy" if self.active_direction == "LONG" else "Sell"
            is_averaging_order = (event.side == expected_averaging_side)
            log_info(self.user_id, f"[УСРЕДНЕНИЕ] Проверка: expected_side={expected_averaging_side}, actual_side={event.side}, is_averaging={is_averaging_order}", "SignalScalper")

        if not is_closing_order and not self.position_active:
            # ПРОВЕРКА БИРЖИ ТОЛЬКО ПРИ ВОССТАНОВЛЕНИИ ПОСЛЕ КРАХА
            # В нормальном режиме работы эта проверка НЕ НУЖНА - она вызывает лишние API запросы
            if self.is_bot_restart_recovery:
                log_info(self.user_id, f"[RECOVERY MODE] Проверяю биржу для восстановления состояния...", "SignalScalper")
                try:
                    exchange_positions = await self.api.get_positions()
                    for position in exchange_positions:
                        if (position.get('symbol') == self.symbol and
                            float(position.get('size', 0)) > 0):
                            # НА БИРЖЕ УЖЕ ЕСТЬ ПОЗИЦИЯ! Восстанавливаем состояние
                            position_side = position.get('side', '').lower()  # "buy" или "sell" от Bybit
                            # Нормализуем для сравнения: Buy->buy/long, Sell->sell/short
                            expected_side = "buy" if event.side == "Buy" else "sell"

                            if position_side == expected_side:
                                # Восстановление состояния после краша
                                log_warning(self.user_id,
                                          f"⚠️ ВОССТАНОВЛЕНИЕ: Обнаружена позиция на бирже! Ордер {event.order_id} будет обработан как часть существующей позиции {position_side.upper()}",
                                          "SignalScalper")

                                if not self.position_active:
                                    log_warning(self.user_id,
                                              f"⚠️ Стратегия не знала о позиции! Восстанавливаю состояние...",
                                              "SignalScalper")
                                    self.position_active = True
                                    self.active_direction = "LONG" if position_side == "buy" else "SHORT"
                                    self.entry_price = event.price
                                    self.position_size = event.qty
                                    self.peak_profit_usd = Decimal('0')
                                    self.hold_signal_counter = 0

                                    # Подписываемся на события цены
                                    await self.event_bus.subscribe(EventType.PRICE_UPDATE, self.handle_price_update, user_id=self.user_id)

                                    # ПРАВИЛЬНЫЙ расчет начальной маржи из фактических данных позиции
                                    # Формула: margin = (entry_price * position_size) / leverage
                                    leverage = self._convert_to_decimal(self._get_frozen_config_value("leverage", 1.0))
                                    position_value = event.price * event.qty
                                    calculated_margin = position_value / leverage

                                    # Отправляем уведомление об открытии
                                    signal_price = getattr(self, 'signal_price', None)
                                    await self._send_trade_open_notification(event.side, event.price, event.qty, calculated_margin, signal_price)

                                    # Инициализируем переменные усреднения
                                    self.averaging_executed = False
                                    self.total_position_size = Decimal('0')
                                    self.average_entry_price = Decimal('0')
                                    self.initial_margin_usd = calculated_margin  # Используем рассчитанную маржу
                                    self.total_fees_paid = event.fee

                                    log_info(self.user_id, f"💰 Начальная маржа (recovery): ${self.initial_margin_usd:.2f} (position_value=${position_value:.2f}, leverage={leverage})", "SignalScalper")

                                    # Устанавливаем стоп-лосс
                                    await self._place_stop_loss_order(self.active_direction, self.entry_price, self.position_size)

                                    log_info(self.user_id, "✅ Состояние стратегии восстановлено из позиции на бирже", "SignalScalper")

                                    # Сбрасываем режим восстановления
                                    self.is_bot_restart_recovery = False

                                    # КРИТИЧНО: Завершаем обработку после восстановления состояния
                                    self.is_waiting_for_trade = False
                                    return
                                else:
                                    # Позиция уже активна - это усреднение
                                    is_averaging_order = True
                                break
                            else:
                                log_error(self.user_id,
                                        f"🚨 КРИТИЧЕСКАЯ ОШИБКА: Попытка открыть {expected_side.upper()}, но на бирже уже {position_side.upper()}! Это конфликт направлений.",
                                        "SignalScalper")
                                self.is_waiting_for_trade = False
                                return
                except Exception as check_error:
                    log_error(self.user_id, f"Ошибка проверки позиций при обработке ордера: {check_error}", "SignalScalper")

            # Ордер на открытие позиции
            log_info(self.user_id, f"[ОТКРЫТИЕ] Обрабатываем ордер открытия: {event.order_id}", "SignalScalper")
            self.position_active = True
            self.active_direction = "LONG" if event.side == "Buy" else "SHORT"
            self.entry_price = event.price
            self.position_size = event.qty
            self.peak_profit_usd = Decimal('0')
            self.hold_signal_counter = 0

            # КРИТИЧЕСКИ ВАЖНО: Подписываемся на события цены для усреднения и трейлинга
            await self.event_bus.subscribe(EventType.PRICE_UPDATE, self.handle_price_update, user_id=self.user_id)

            # Передаем сохраненную цену сигнала в уведомление
            signal_price = getattr(self, 'signal_price', None)
            await self._send_trade_open_notification(event.side, event.price, event.qty, self.intended_order_amount, signal_price)

            # Инициализируем переменные НОВОЙ системы усреднения (одиночное удвоение)
            self.averaging_executed = False  # Флаг: было ли выполнено усреднение
            self.total_position_size = Decimal('0')  # Сброс размера усредненной позиции
            self.average_entry_price = Decimal('0')  # Сброс средней цены
            self.total_fees_paid = event.fee  # Начальная комиссия

            # ПРАВИЛЬНЫЙ расчет начальной маржи: order_amount УЖЕ является маржой пользователя
            self.initial_margin_usd = self.intended_order_amount

            log_info(self.user_id, f"💰 Начальная маржа для усреднения: ${self.initial_margin_usd:.2f}", "SignalScalper")

            # КРИТИЧНО: Загружаем параметры усреднения из ЗАМОРОЖЕННОЙ конфигурации
            if self.active_trade_config:
                self.averaging_trigger_loss_percent = self._convert_to_decimal(self.active_trade_config.get("averaging_trigger_loss_percent", "25.0"))
                self.averaging_stop_loss_percent = self._convert_to_decimal(self.active_trade_config.get("averaging_stop_loss_percent", "30.0"))
                self.averaging_multiplier = self._convert_to_decimal(self.active_trade_config.get("averaging_multiplier", "2.0"))
                log_info(self.user_id,
                        f"🔧 Параметры усреднения: триггер={self.averaging_trigger_loss_percent}%, "
                        f"SL={self.averaging_stop_loss_percent}%, множитель={self.averaging_multiplier}x",
                        "SignalScalper")

            # ВСЕГДА устанавливаем стоп-лосс для защиты (даже при усреднении)
            await self._place_stop_loss_order(self.active_direction, self.entry_price, self.position_size)

        elif is_averaging_order and self.position_active:
            # Ордер на усреднение позиции
            log_info(self.user_id, f"[УСРЕДНЕНИЕ] Обрабатываем ордер усреднения: {event.order_id}", "SignalScalper")

            # СОХРАНЯЕМ данные ДО усреднения для уведомления
            old_entry_price = self.entry_price
            old_size = self.position_size

            # Рассчитываем добавленную маржу
            leverage = self._convert_to_decimal(self._get_frozen_config_value("leverage", 1.0))
            averaging_amount = (event.price * event.qty) / leverage

            # Рассчитываем текущий PnL ДО усреднения (для информирования о причине усреднения)
            if self.active_direction == "LONG":
                current_pnl = (event.price - self.entry_price) * self.position_size
            else:  # SHORT
                current_pnl = (self.entry_price - event.price) * self.position_size

            loss_percent = ((abs(current_pnl) / self.initial_margin_usd) * Decimal('100')) if (
                        self.initial_margin_usd > 0 > current_pnl) else Decimal('0')

            # НЕ ОБНОВЛЯЕМ position_active, так как позиция остается активной
            # Обновляем размер позиции и среднюю цену напрямую в этом методе
            if self.total_position_size == 0:
                # Первое усреднение - инициализируем
                self.total_position_size = self.position_size + event.qty
                self.average_entry_price = ((self.entry_price * self.position_size) + (event.price * event.qty)) / self.total_position_size
            else:
                # Последующие усреднения
                old_total_value = self.average_entry_price * self.total_position_size
                new_value = event.price * event.qty
                self.total_position_size += event.qty
                self.average_entry_price = (old_total_value + new_value) / self.total_position_size

            # НАКОПЛЕНИЕ КОМИССИЙ (НОВАЯ СИСТЕМА)
            self.total_fees_paid += event.fee

            log_info(self.user_id,
                    f"[УСРЕДНЕНИЕ] Усреднение выполнено. Новая средняя цена: {self.average_entry_price:.4f}, размер: {self.total_position_size}, комиссия: ${event.fee:.4f}",
                    "SignalScalper")

            # ОБНОВЛЯЕМ БД: сохраняем новую среднюю цену входа и общий размер позиции
            if hasattr(self, 'active_trade_db_id') and self.active_trade_db_id:
                from database.db_trades import db_manager
                await db_manager.update_trade_on_averaging(
                    trade_id=self.active_trade_db_id,
                    new_entry_price=self.average_entry_price,
                    new_quantity=self.total_position_size
                )
                log_info(self.user_id, f"[БД] Сделка {self.active_trade_db_id} обновлена в БД после усреднения", "SignalScalper")

            # ДИНАМИЧЕСКАЯ КОРРЕКТИРОВКА СТОП-ЛОССА после усреднения - ОТКЛЮЧЕНО для новой системы
            await self._update_stop_loss_after_averaging()

            # Отправляем МАКСИМАЛЬНО ИНФОРМАТИВНОЕ уведомление об усреднении
            await self._send_averaging_notification(
                price=event.price,
                quantity=event.qty,
                new_avg_price=self.average_entry_price,
                new_total_size=self.total_position_size,
                side=event.side,
                old_entry_price=old_entry_price,
                old_size=old_size,
                current_pnl=current_pnl,
                loss_percent=loss_percent,
                trigger_percent=self.averaging_trigger_loss_percent,
                averaging_amount=averaging_amount
            )

        elif is_closing_order and self.position_active:
            # Ордер на закрытие позиции
            log_info(self.user_id, f"[ЗАКРЫТИЕ] Обрабатываем ордер закрытия: {event.order_id}", "SignalScalper")

            # ПРАВИЛЬНЫЙ РАСЧЕТ PnL: Берём данные из БД если они есть, иначе из локальных переменных
            from database.db_trades import db_manager

            # Пытаемся получить актуальные данные из БД
            trade_from_db = None
            if hasattr(self, 'active_trade_db_id') and self.active_trade_db_id:
                try:
                    trade_from_db = await db_manager.get_active_trade(self.user_id, self.symbol)
                    if trade_from_db:
                        log_info(self.user_id, f"[БД] Получены данные из БД: entry_price={trade_from_db['entry_price']}, quantity={trade_from_db['quantity']}", "SignalScalper")
                except Exception as db_error:
                    log_warning(self.user_id, f"[БД] Не удалось получить данные из БД: {db_error}, используем локальные", "SignalScalper")

            # Используем данные из БД если они есть, иначе локальные
            if trade_from_db:
                entry_price_for_pnl = Decimal(str(trade_from_db['entry_price']))
                position_size_for_pnl = Decimal(str(trade_from_db['quantity']))
                log_info(self.user_id, f"[БД] Используем данные из БД для расчёта PnL", "SignalScalper")
            else:
                entry_price_for_pnl = self.average_entry_price if self.average_entry_price > 0 else self.entry_price
                position_size_for_pnl = self.total_position_size if self.total_position_size > 0 else self.position_size
                log_info(self.user_id, f"[ЛОКАЛЬНО] Используем локальные данные для расчёта PnL", "SignalScalper")

            pnl_gross = (event.price - entry_price_for_pnl) * position_size_for_pnl if self.active_direction == "LONG" else (
                entry_price_for_pnl - event.price) * position_size_for_pnl

            # НАКОПЛЕНИЕ КОМИССИИ ЗАКРЫТИЯ
            self.total_fees_paid += event.fee

            # ПРАВИЛЬНЫЙ РАСЧЁТ: Вычитаем ВСЕ накопленные комиссии (открытие + усреднение + закрытие)
            pnl_net = pnl_gross - self.total_fees_paid

            log_info(self.user_id,
                    f"[PNL_CALC] entry_price={entry_price_for_pnl:.4f}, position_size={position_size_for_pnl}, "
                    f"exit_price={event.price:.4f}, close_fee={event.fee:.4f}, total_fees={self.total_fees_paid:.4f}, "
                    f"direction={self.active_direction}, pnl_gross={pnl_gross:.4f}, pnl_net={pnl_net:.4f}",
                    "SignalScalper")

            # КРИТИЧЕСКИ ВАЖНО: Обновляем ордер CLOSE в БД с profit
            try:
                await db_manager.update_order_on_fill(
                    order_id=event.order_id,
                    filled_quantity=event.qty,
                    average_price=event.price,
                    commission=event.fee,
                    profit=pnl_net  # Для CLOSE ордера передаём рассчитанный profit
                )
                log_debug(self.user_id, f"✅ Ордер CLOSE {event.order_id} обновлён в БД с profit={pnl_net:.2f}$", "SignalScalper")
            except Exception as db_error:
                log_error(self.user_id, f"❌ Ошибка обновления CLOSE ордера {event.order_id} в БД: {db_error}", "SignalScalper")

            self.last_closed_direction = self.active_direction

            # Фиксируем время закрытия и результат сделки
            self.last_trade_close_time = time.time()
            self.last_trade_was_loss = pnl_net < 0

            if self.last_trade_was_loss:
                log_warning(self.user_id, f"Убыточная сделка! Следующему сигналу потребуется 3 подтверждения.", "SignalScalper")

            # Сбрасываем счетчики подтверждения после закрытия сделки
            self.signal_confirmation_count = 0
            self.last_signal = None

            # Отменяем стоп-лосс перед сбросом состояния (БЫСТРО)
            if self.stop_loss_order_id:
                await self._cancel_stop_loss_order()

            # Сброс состояния (ВКЛЮЧАЯ ПЕРЕМЕННЫЕ УСРЕДНЕНИЯ)
            self.position_active = False
            self.active_direction = None
            self.entry_price = None
            self.position_size = None

            # СБРОС ПЕРЕМЕННЫХ ПРОМЕЖУТОЧНОГО УСРЕДНЕНИЯ
            self.intermediate_averaging_executed = False

            # СБРОС ПЕРЕМЕННЫХ ОСНОВНОГО УСРЕДНЕНИЯ (ОДИНОЧНОЕ УТРОЕНИЕ)
            self.averaging_executed = False
            self.averaging_count = 0  # Сброс счетчика усреднений
            self.initial_margin_usd = Decimal('0')
            self.total_fees_paid = Decimal('0')
            self.total_position_size = Decimal('0')
            self.average_entry_price = Decimal('0')

            # СБРОС ФЛАГОВ ИНТЕЛЛЕКТУАЛЬНОГО SL
            self.sl_extended = False
            self.sl_extension_notified = False


            # КРИТИЧЕСКИ ВАЖНО: СБРОС РЕЖИМА РЕВЕРСА
            # Сбрасываем ТОЛЬКО если это НЕ реверс (при реверсе флаг уже установлен)
            # Проверяем, был ли это обычный reason закрытия или реверс
            if self.close_reason and not self.close_reason.startswith("reversing_to_"):
                self.after_reversal_mode = False
                self.last_reversal_time = None
                log_info(self.user_id, f"🔄 Режим реверса сброшен при закрытии сделки (причина: {self.close_reason})", "SignalScalper")

            # РАЗМОРОЗКА КОНФИГУРАЦИИ ПОСЛЕ ЗАКРЫТИЯ СДЕЛКИ
            self.active_trade_config = None
            self.config_frozen = False
            log_info(self.user_id, f"Конфигурация разморожена после закрытия сделки по {self.symbol}", "SignalScalper")

            await self.event_bus.unsubscribe(self._handle_price_update)
            # МГНОВЕННО отправляем уведомление
            await self._send_trade_close_notification(pnl_net, event.fee, exit_price=event.price)
            log_info(self.user_id, f"[УСПЕХ] Позиция {self.symbol} закрыта быстро! PnL: {pnl_net:.2f}$", "SignalScalper")

            # ПРОВЕРКА ОТЛОЖЕННОЙ ОСТАНОВКИ
            # Проверяем, должна ли стратегия быть остановлена после закрытия позиции
            await self.check_deferred_stop()
        else:
            log_warning(self.user_id, f"[НЕОЖИДАННО] Неожиданное состояние при обработке ордера {event.order_id}. position_active={self.position_active}, is_closing={is_closing_order}", "SignalScalper")

        self.is_waiting_for_trade = False

    def _get_stop_loss_info(self, side: str, price: Decimal, quantity: Decimal) -> tuple[Decimal, Decimal]:
        """
        ПЕРЕОПРЕДЕЛЕНИЕ для Signal Scalper: рассчитывает SL на основе процента от маржи.

        Returns:
            tuple[Decimal, Decimal]: (цена_SL, ожидаемый_убыток_USDT)
        """
        try:
            # Используем маржу пользователя (initial_margin_usd) и процент SL
            if self.initial_margin_usd > 0:
                max_loss_usd = self.initial_margin_usd * (self.averaging_stop_loss_percent / Decimal('100'))
            else:
                # Если маржа еще не установлена, рассчитываем её (для уведомления ДО установки initial_margin_usd)
                order_amount = self._convert_to_decimal(self.get_config_value("order_amount", 50.0))
                max_loss_usd = order_amount * (self.averaging_stop_loss_percent / Decimal('100'))

            # Определяем направление позиции
            is_long = side.lower() == 'buy'

            # Рассчитываем цену стоп-лосса
            sl_price = BaseStrategy._calculate_precise_stop_loss(price, quantity, max_loss_usd, is_long)

            # ТОЧНЫЙ расчёт реального убытка при срабатывании SL
            if is_long:
                actual_loss = (price - sl_price) * quantity
            else:
                actual_loss = (sl_price - price) * quantity

            # Добавляем комиссию при закрытии (используем реальное значение из конфига)
            from core.settings_config import EXCHANGE_FEES
            from core.enums import ExchangeType
            taker_fee_rate = EXCHANGE_FEES[ExchangeType.BYBIT]['taker'] / Decimal('100')  # Конвертируем из % в десятичное
            estimated_close_fee = sl_price * quantity * taker_fee_rate
            total_expected_loss = actual_loss + estimated_close_fee

            return sl_price, total_expected_loss

        except Exception as e:
            log_error(self.user_id, f"Ошибка расчёта SL для уведомления: {e}", "SignalScalper")
            return price, Decimal('0')  # Fallback

    def _calculate_stop_loss_price(self, entry_price: Decimal, direction: str, position_size: Decimal) -> Decimal:
        """
        Рассчитывает цену стоп-лосса на основе процента от маржи.

        НОВАЯ СИСТЕМА: SL ставится так, чтобы убыток = averaging_stop_loss_percent от начальной маржи

        Формула:
        max_loss_usd = initial_margin_usd * (averaging_stop_loss_percent / 100)
        LONG: SL = entry_price - (max_loss_usd / position_size)
        SHORT: SL = entry_price + (max_loss_usd / position_size)
        """
        if position_size <= 0:
            log_error(self.user_id, "Невозможно рассчитать стоп-лосс: размер позиции равен нулю", "SignalScalper")
            return entry_price  # Возвращаем цену входа как fallback

        # Рассчитываем максимальный убыток в USDT на основе процента от маржи
        max_loss_usd = self.initial_margin_usd * (self.averaging_stop_loss_percent / Decimal('100'))

        # Используем точный метод расчета
        is_long = (direction == "LONG")
        stop_loss_price = BaseStrategy._calculate_precise_stop_loss(entry_price, position_size, max_loss_usd, is_long)

        log_info(self.user_id,
                f"Точный расчет стоп-лосса для {direction}: вход=${entry_price:.4f}, SL=${stop_loss_price:.4f}, "
                f"макс. убыток=${max_loss_usd:.2f} ({self.averaging_stop_loss_percent}% от маржи ${self.initial_margin_usd:.2f})",
                "SignalScalper")

        return stop_loss_price

    async def _place_stop_loss_order(self, direction: str, entry_price: Decimal, position_size: Decimal):
        """Выставляет стоп-лосс ордер после открытия позиции."""
        try:
            # Рассчитываем цену стоп-лосса
            stop_loss_price = self._calculate_stop_loss_price(entry_price, direction, position_size)

            # Размещаем стоп-лосс через установку торговых стопов
            # Используем API для установки стоп-лосса на позицию вместо ордера
            success = await self.api.set_trading_stop(
                symbol=self.symbol,
                stop_loss=stop_loss_price
            )

            if success:
                # Сохраняем информацию о стоп-лоссе
                self.stop_loss_price = stop_loss_price
                stop_loss_order_id = f"trading_stop_{self.symbol}_{int(time.time())}"
                self.stop_loss_order_id = stop_loss_order_id
                log_info(self.user_id,
                        f"Стоп-лосс установлен: ID={stop_loss_order_id}, цена=${stop_loss_price:.4f}",
                        "SignalScalper")
            else:
                log_error(self.user_id, "Не удалось выставить стоп-лосс через торговые стопы", "SignalScalper")

        except Exception as e:
            log_error(self.user_id, f"Ошибка при установке стоп-лосса: {e}", "SignalScalper")

    async def _cancel_stop_loss_order(self):
        """Отменяет активный стоп-лосс ордер."""
        if self.stop_loss_order_id:
            try:
                # Отменяем торговый стоп через установку значения "0" (правильный способ для Bybit API)
                success = await self.api.set_trading_stop(
                    symbol=self.symbol,
                    stop_loss="0"  # Убираем стоп-лосс (правильный формат для Bybit)
                )
                if success:
                    log_info(self.user_id, f"Стоп-лосс {self.stop_loss_order_id} отменен", "SignalScalper")
                else:
                    log_warning(self.user_id, f"Не удалось отменить стоп-лосс {self.stop_loss_order_id}", "SignalScalper")
            except Exception as e:
                log_error(self.user_id, f"Ошибка отмены стоп-лосса {self.stop_loss_order_id}: {e}", "SignalScalper")
            finally:
                self.stop_loss_order_id = None
                self.stop_loss_price = None

    async def _update_stop_loss_after_averaging(self):
        """
        Обновляет стоп-лосс после усреднения позиции.
        Смещает SL на основе новой средней цены входа и общего размера позиции.
        """
        if not self.average_entry_price or not self.total_position_size:
            log_debug(self.user_id, "Пропуск обновления SL: нет данных об усреднении", "SignalScalper")
            return

        try:
            # Отменяем старый SL
            if self.stop_loss_order_id:
                await self._cancel_stop_loss_order()

            # ПРАВИЛЬНЫЙ расчет: используем тот же метод что и при открытии позиции
            # Рассчитываем максимальный убыток в USDT на основе процента от маржи
            max_loss_usd = self.initial_margin_usd * (self.averaging_stop_loss_percent / Decimal('100'))

            # Используем точный метод расчета SL
            is_long = (self.active_direction == "LONG")
            new_sl_price = BaseStrategy._calculate_precise_stop_loss(
                self.average_entry_price,
                self.total_position_size,
                max_loss_usd,
                is_long
            )

            # Устанавливаем новый SL через Bybit API
            success = await self.api.set_trading_stop(
                symbol=self.symbol,
                stop_loss=str(new_sl_price),
                position_idx=1 if self.active_direction == "LONG" else 2
            )

            if success:
                self.stop_loss_price = new_sl_price
                log_info(self.user_id,
                        f"✅ SL смещен после усреднения: новая средняя цена=${self.average_entry_price:.4f}, "
                        f"новый SL=${new_sl_price:.4f}, размер позиции={self.total_position_size}",
                        "SignalScalper")
            else:
                log_warning(self.user_id, "Не удалось установить новый SL после усреднения", "SignalScalper")

        except Exception as e:
            log_error(self.user_id, f"Ошибка обновления SL после усреднения: {e}", "SignalScalper")

    def _is_signal_confirmed(self, signal: str) -> bool:
        """
        Проверяет, подтвержден ли сигнал достаточным количеством повторений.
        После убыточной сделки или реверса требует больше подтверждений.
        """
        if signal == self.last_signal:
            self.signal_confirmation_count += 1
        else:
            # Новый сигнал - сбрасываем счетчик
            self.last_signal = signal
            self.signal_confirmation_count = 1

        # Определяем требуемое количество подтверждений
        required = self.required_confirmations

        # После убыточной сделки требуем больше подтверждений
        if self.last_trade_was_loss:
            required = max(required, 2)  # После убытка требуем минимум 2 подтверждения

        # НОВАЯ ЛОГИКА: После реверса требуем специальное количество подтверждений
        if self.after_reversal_mode:
            required = max(required, self.reversal_required_confirmations)  # Выбираем максимум

        confirmed = self.signal_confirmation_count >= required

        # ДОПОЛНИТЕЛЬНАЯ ЛОГИКА: После подтверждения сигнала в режиме реверса, выходим из этого режима
        if confirmed and self.after_reversal_mode:
            log_info(self.user_id,
                    f"🔄 Режим после реверса завершен. Сигнал {signal} получил необходимые подтверждения.",
                    "SignalScalper")
            self.after_reversal_mode = False
            self.last_reversal_time = None

        if confirmed:
            log_info(self.user_id,
                    f"Сигнал {signal} подтвержден! ({self.signal_confirmation_count}/{required})",
                    "SignalScalper")
        else:
            reason = ""
            if self.last_trade_was_loss:
                reason = " (после убытка)"
            elif self.after_reversal_mode:
                reason = " (после реверса)"

            log_info(self.user_id,
                    f"Сигнал {signal} ожидает подтверждения ({self.signal_confirmation_count}/{required}){reason}",
                    "SignalScalper")

        return confirmed

    def _is_cooldown_active(self) -> bool:
        """Проверяет, активен ли кулдаун после закрытия последней сделки."""
        if self.last_trade_close_time is None:
            return False

        current_time = time.time()
        time_since_close = current_time - self.last_trade_close_time
        cooldown_active = time_since_close < self.cooldown_seconds

        if cooldown_active:
            remaining_time = self.cooldown_seconds - time_since_close
            log_info(self.user_id,
                    f"Кулдаун активен. Осталось {remaining_time:.0f} сек до следующего входа",
                    "SignalScalper")

        return cooldown_active

    def _is_reversal_cooldown_active(self) -> bool:
        """Проверяет, активен ли кулдаун после реверса позиции."""
        if not self.after_reversal_mode or self.last_reversal_time is None:
            return False

        current_time = time.time()
        time_since_reversal = current_time - self.last_reversal_time
        cooldown_active = time_since_reversal < self.reversal_cooldown_seconds

        if cooldown_active:
            remaining_time = self.reversal_cooldown_seconds - time_since_reversal
            log_info(self.user_id,
                    f"🔄 Кулдаун после реверса активен. Осталось {remaining_time:.0f} сек до следующего входа",
                    "SignalScalper")

        return cooldown_active


    async def _execute_averaging(self, current_price: Decimal):
        """
        Выполняет ОСНОВНОЕ Удвоение позиции при достижении триггера убытка.
        После выполнения устанавливается флаг averaging_executed = True.
        """
        # ПРОВЕРКА: отключено или достигнут лимит усреднений
        if not self.averaging_enabled or self.averaging_count >= self.max_averaging_count:
            return

        try:
            self.is_waiting_for_trade = True

            # Используем ЗАМОРОЖЕННЫЕ параметры текущей сделки
            order_amount = self._convert_to_decimal(self._get_frozen_config_value("order_amount", 50.0))

            # ДЛЯ УСРЕДНЕНИЯ: ВСЕГДА используем плечо 1x (БЕЗ плеча)
            leverage = Decimal('1.0')

            # Удвоение суммы (множитель 2.0)
            averaging_amount = order_amount * self.averaging_multiplier

            log_warning(self.user_id,
                       f"💰 УСРЕДНЕНИЕ (x{self.averaging_multiplier}): {order_amount:.2f}$ × {self.averaging_multiplier} = {averaging_amount:.2f}$ USDT (БЕЗ ПЛЕЧА)",
                       "SignalScalper")

            qty = await self.api.calculate_quantity_from_usdt(self.symbol, averaging_amount, leverage, price=current_price)

            if qty <= 0:
                log_error(self.user_id, "Не удалось рассчитать количество для усреднения", "SignalScalper")
                self.is_waiting_for_trade = False
                return

            # Размещаем усредняющий ордер
            side = "Buy" if self.active_direction == "LONG" else "Sell"
            order_id = await self._place_order(side=side, order_type="Market", qty=qty)

            if order_id:
                self.current_order_id = order_id
                # Увеличиваем счетчик усреднений
                self.averaging_count += 1
                # Устанавливаем флаг (для обратной совместимости)
                self.averaging_executed = True
                log_info(self.user_id, f"✅ Усреднение #{self.averaging_count} выполнено. Лимит: {self.averaging_count}/{self.max_averaging_count}", "SignalScalper")

                # Ждем исполнения ордера
                # Вся логика обновления статистики будет в _handle_order_filled()
                await self._await_order_fill(order_id, side=side, qty=qty)

            self.is_waiting_for_trade = False

        except Exception as e:
            log_error(self.user_id, f"Ошибка при усреднении: {e}", "SignalScalper")
            self.is_waiting_for_trade = False

    # ============================================================
    # ДЕТЕКТОР ЗАСТРЯВШЕЙ ЦЕНЫ (STAGNATION DETECTOR)
    # Легко удалить: удалите эти методы
    # ============================================================

    async def _check_stagnation_detector(self, current_pnl: Decimal) -> bool:
        """
        Проверяет условия детектора застрявшей цены.
        НОВАЯ СИСТЕМА: диапазоны в процентах от маржи (order_amount × leverage)

        Args:
            current_pnl: Текущий PnL в USDT

        Returns:
            bool: True если сработал триггер усреднения
        """
        # Пропускаем если детектор отключен или уже выполнено усреднение
        if not self.stagnation_detector_enabled or self.stagnation_averaging_executed:
            return False

        # Пропускаем если нет диапазонов для отслеживания
        if not self.stagnation_ranges:
            return False

        # Проверяем только если в убытке
        if current_pnl >= 0:
            # Если цена вышла в плюс - сбрасываем мониторинг
            if self.stagnation_monitor_active:
                self._reset_stagnation_monitor()
            return False

        # Рассчитываем маржу (номинал позиции с учетом плеча)
        order_amount = self._convert_to_decimal(self._get_frozen_config_value("order_amount", 100.0))
        leverage = self._convert_to_decimal(self._get_frozen_config_value("leverage", 1.0))
        margin = order_amount * leverage

        # Рассчитываем убыток в процентах от маржи
        loss_percent = (abs(current_pnl) / margin) * Decimal('100') if margin > 0 else Decimal('0')

        # Проверяем, находится ли убыток в одном из диапазонов (в процентах)
        current_range_index = None
        for idx, range_dict in enumerate(self.stagnation_ranges):
            range_min_percent = Decimal(str(range_dict.get('min', 0)))
            range_max_percent = Decimal(str(range_dict.get('max', 0)))

            # Проверяем вхождение в диапазон процентов
            if range_min_percent <= loss_percent <= range_max_percent:
                current_range_index = idx
                break

        # Если PnL НЕ в диапазоне
        if current_range_index is None:
            # Сбрасываем мониторинг если был активен
            if self.stagnation_monitor_active:
                log_debug(self.user_id,
                         f"🔄 Детектор стагнации: PnL=${current_pnl:.2f} вышел из диапазона. Сброс мониторинга.",
                         "SignalScalper")
                self._reset_stagnation_monitor()
            return False

        # PnL В ДИАПАЗОНЕ
        current_time = time.time()

        # Если мониторинг НЕ активен - запускаем
        if not self.stagnation_monitor_active:
            self.stagnation_monitor_active = True
            self.stagnation_monitor_start_time = current_time
            self.stagnation_current_range_index = current_range_index

            range_dict = self.stagnation_ranges[current_range_index]
            # Рассчитываем USDT эквиваленты для логов
            loss_usdt_min = (margin * range_dict['min']) / Decimal('100')
            loss_usdt_max = (margin * range_dict['max']) / Decimal('100')
            log_info(self.user_id,
                    f"🎯 Детектор стагнации АКТИВИРОВАН! PnL=${current_pnl:.2f} ({loss_percent:.1f}%) "
                    f"в диапазоне [{range_dict['min']:.1f}%-{range_dict['max']:.1f}% (${loss_usdt_min:.1f}-${loss_usdt_max:.1f})]. "
                    f"Мониторинг {self.stagnation_check_interval} сек...",
                    "SignalScalper")
            return False

        # Мониторинг АКТИВЕН - проверяем условия
        # Проверка 1: PnL все еще в ТОМ ЖЕ диапазоне?
        if current_range_index != self.stagnation_current_range_index:
            log_warning(self.user_id,
                       f"⚠️ Детектор стагнации: PnL перешел в другой диапазон! Сброс мониторинга.",
                       "SignalScalper")
            self._reset_stagnation_monitor()
            return False

        # Проверка 2: Прошло ли достаточно времени?
        elapsed_time = current_time - self.stagnation_monitor_start_time

        if elapsed_time >= self.stagnation_check_interval:
            # ТРИГГЕР СРАБОТАЛ!
            range_dict = self.stagnation_ranges[current_range_index]
            # Рассчитываем USDT эквиваленты для логов
            loss_usdt_min = (margin * range_dict['min']) / Decimal('100')
            loss_usdt_max = (margin * range_dict['max']) / Decimal('100')
            log_warning(self.user_id,
                       f"🚨 ТРИГГЕР ДЕТЕКТОРА СТАГНАЦИИ! PnL=${current_pnl:.2f} ({loss_percent:.1f}%) застрял в диапазоне "
                       f"[{range_dict['min']:.1f}%-{range_dict['max']:.1f}% (${loss_usdt_min:.1f}-${loss_usdt_max:.1f})] на {elapsed_time:.0f} сек! "
                       f"Выполняю усреднение...",
                       "SignalScalper")
            return True
        else:
            # Еще не прошло достаточно времени
            remaining_time = self.stagnation_check_interval - elapsed_time
            log_debug(self.user_id,
                     f"⏱️ Детектор стагнации: PnL=${current_pnl:.2f} в диапазоне. Осталось {remaining_time:.0f} сек...",
                     "SignalScalper")
            return False

    def _reset_stagnation_monitor(self):
        """Сбрасывает состояние мониторинга детектора стагнации."""
        self.stagnation_monitor_active = False
        self.stagnation_monitor_start_time = None
        self.stagnation_current_range_index = None

    async def _execute_stagnation_averaging(self, current_price: Decimal):
        """
        Выполняет усреднение при срабатывании детектора застрявшей цены.
        Удваивает позицию с плечом x1.
        """
        # ПРОВЕРКА: отключено или уже выполнено
        if not self.stagnation_detector_enabled or self.stagnation_averaging_executed:
            return

        try:
            self.is_waiting_for_trade = True

            # Используем ЗАМОРОЖЕННЫЕ параметры текущей сделки
            order_amount = self._convert_to_decimal(self._get_frozen_config_value("order_amount", 50.0))

            # Используем настройки детектора стагнации
            leverage = Decimal(str(self.stagnation_averaging_leverage))  # x1
            multiplier = self.stagnation_averaging_multiplier  # x2

            # Расчет суммы усреднения
            stagnation_amount = order_amount * multiplier

            log_warning(self.user_id,
                       f"💎 УСРЕДНЕНИЕ ПО ДЕТЕКТОРУ СТАГНАЦИИ (x{multiplier}): "
                       f"{order_amount:.2f}$ × {multiplier} = {stagnation_amount:.2f}$ USDT (плечо x{leverage})",
                       "SignalScalper")

            qty = await self.api.calculate_quantity_from_usdt(
                self.symbol, stagnation_amount, leverage, price=current_price
            )

            if qty <= 0:
                log_error(self.user_id,
                         "Не удалось рассчитать количество для усреднения по детектору стагнации",
                         "SignalScalper")
                self.is_waiting_for_trade = False
                return

            # Размещаем усредняющий ордер
            side = "Buy" if self.active_direction == "LONG" else "Sell"
            order_id = await self._place_order(side=side, order_type="Market", qty=qty)

            if order_id:
                self.current_order_id = order_id
                # Устанавливаем флаг выполнения
                self.stagnation_averaging_executed = True
                # Сбрасываем мониторинг
                self._reset_stagnation_monitor()

                log_info(self.user_id,
                        f"✅ Усреднение по детектору стагнации выполнено",
                        "SignalScalper")

                # Ждем исполнения ордера
                await self._await_order_fill(order_id, side=side, qty=qty)

            self.is_waiting_for_trade = False

        except Exception as e:
            log_error(self.user_id, f"Ошибка при усреднении по детектору стагнации: {e}", "SignalScalper")
            self.is_waiting_for_trade = False

    # ============================================================
    # КОНЕЦ ДЕТЕКТОРА ЗАСТРЯВШЕЙ ЦЕНЫ
    # ============================================================

    def _calculate_dynamic_levels(self) -> Dict[int, Decimal]:
        """
        🚀 УМНАЯ АДАПТИВНАЯ СИСТЕМА ТРЕЙЛИНГА 🚀

        Автоматически подстраивается под любой размер депозита и плечо!

        Формула: profit_level = (order_amount × leverage) × percentage

        Примеры работы для разных пользователей:

        📊 Малый депозит: $50, 1x плечо
        - Номинал = $50
        - Уровень 1: $50 × 0.20% = $0.10 (мгновенная активация)
        - Уровень 6: $50 × 2.50% = $1.25 (максимум)

        📊 Средний депозит: $500, 2x плечо
        - Номинал = $1000
        - Уровень 1: $1000 × 0.20% = $2.00
        - Уровень 6: $1000 × 2.50% = $25.00

        📊 Большой депозит: $2000, 3x плечо
        - Номинал = $6000
        - Уровень 1: $6000 × 0.20% = $12.00
        - Уровень 6: $6000 × 2.50% = $150.00

        📊 Огромный депозит: $10000, 5x плечо
        - Номинал = $50000
        - Уровень 1: $50000 × 0.20% = $100.00
        - Уровень 6: $50000 × 2.50% = $1250.00

        Преимущества:
        ✅ Автоматическая адаптация под размер депозита
        ✅ Быстрый выход (0.20% от номинала)
        ✅ 6 уровней для плавных переходов


        Returns:
            Dict[int, Decimal]: Словарь с уровнями {уровень: прибыль_в_USDT}
        """
        # Получаем параметры пользователя
        order_amount = max(self._convert_to_decimal(self.get_config_value("order_amount", 50.0)), Decimal('10.0'))
        leverage = self._convert_to_decimal(self.get_config_value("leverage", 1.0))

        # Номинальная стоимость позиции (реальный риск с учетом плеча)
        notional_value = order_amount * leverage

        # 🎯 ОПТИМИЗИРОВАННЫЕ ПРОЦЕНТЫ для скальпинга
        # Уровни растут примерно в 1.8-2x для плавного перехода
        level_percentages = {
            1: Decimal('0.0020'),   # 0.20% - МГНОВЕННЫЙ (самый быстрый выход)
            2: Decimal('0.0045'),   # 0.45% - РАННИЙ
            3: Decimal('0.0085'),   # 0.85% - СРЕДНИЙ
            4: Decimal('0.0130'),   # 1.30% - ХОРОШИЙ
            5: Decimal('0.0185'),   # 1.85% - ОТЛИЧНЫЙ
            6: Decimal('0.0250')    # 2.50% - МАКСИМАЛЬНЫЙ
        }

        # Рассчитываем пороги в USDT для всех уровней
        levels = {
            0: Decimal('0')  # Уровень 0 - трейлинг неактивен (без минимального порога)
        }

        for level, percentage in level_percentages.items():
            levels[level] = notional_value * percentage

        return levels


    def _get_trailing_level(self, current_pnl: Decimal) -> int:
        """
        Определяет текущий уровень трейлинга на основе динамически рассчитанной прибыли.

        Args:
            current_pnl: Текущая прибыль в USDT

        Returns:
            int: Уровень трейлинга (0-6)
        """
        levels = self._calculate_dynamic_levels()

        if current_pnl < levels[1]:
            return 0  # Не достигли минимального порога
        elif current_pnl < levels[2]:
            return 1  # МГНОВЕННЫЙ уровень (0.20%)
        elif current_pnl < levels[3]:
            return 2  # РАННИЙ уровень (0.45%)
        elif current_pnl < levels[4]:
            return 3  # СРЕДНИЙ уровень (0.85%)
        elif current_pnl < levels[5]:
            return 4  # ХОРОШИЙ уровень (1.30%)
        elif current_pnl < levels[6]:
            return 5  # ОТЛИЧНЫЙ уровень (1.85%)
        else:
            return 6  # МАКСИМАЛЬНЫЙ уровень (2.50%)

    def _get_level_name(self, level: int) -> str:
        """Возвращает человекочитаемое название уровня с динамическими значениями."""
        if level == 0:
            return "ОЖИДАНИЕ"

        levels = self._calculate_dynamic_levels()

        level_names = {
            1: f"МГНОВЕННЫЙ УРОВЕНЬ (${levels[1]:.2f}+, 0.20%)",
            2: f"РАННИЙ УРОВЕНЬ (${levels[2]:.2f}+, 0.45%)",
            3: f"СРЕДНИЙ УРОВЕНЬ (${levels[3]:.2f}+, 0.85%)",
            4: f"ХОРОШИЙ УРОВЕНЬ (${levels[4]:.2f}+, 1.30%)",
            5: f"ОТЛИЧНЫЙ УРОВЕНЬ (${levels[5]:.2f}+, 1.85%)",
            6: f"МАКСИМАЛЬНЫЙ УРОВЕНЬ (${levels[6]:.2f}+, 2.50%)"
        }
        return level_names.get(level, "НЕИЗВЕСТНЫЙ УРОВЕНЬ")

    async def _calculate_current_pnl(self, current_price: Decimal) -> Decimal:
        """
        Рассчитывает текущий PnL позиции для принятия решений.

        Args:
            current_price: Текущая цена актива

        Returns:
            Decimal: Текущий PnL в USDT
        """
        if not self.position_active or not self.entry_price:
            return Decimal('0')

        # Используем среднюю цену входа если есть усреднения
        entry_price_to_use = self.average_entry_price if self.average_entry_price > 0 else self.entry_price
        position_size_to_use = self.total_position_size if self.total_position_size > 0 else self.position_size

        # Рассчитываем PnL в зависимости от направления позиции
        if self.active_direction == "LONG":
            pnl = (current_price - entry_price_to_use) * position_size_to_use
        else:  # SHORT
            pnl = (entry_price_to_use - current_price) * position_size_to_use

        return pnl

    async def _execute_strategy_logic(self):
        """Пустышка, так как логика теперь управляется событиями свечей."""
        pass

    # ===============================================================================
    # СПЕЦИФИЧНОЕ ВОССТАНОВЛЕНИЕ СОСТОЯНИЯ ДЛЯ SIGNAL SCALPER
    # ===============================================================================

    async def _strategy_specific_recovery(self, additional_data: Dict[str, Any]):
        """
        Специфичное восстановление состояния для SignalScalper.
        Восстанавливает состояние усреднения, мониторинга позиций и сигналов.
        """
        try:
            log_info(self.user_id, f"🔧 Специфичное восстановление SignalScalper для {self.symbol}...", "SignalScalper")

            # УСТАНАВЛИВАЕМ РЕЖИМ ВОССТАНОВЛЕНИЯ
            # Этот флаг указывает, что мы восстанавливаемся после краша
            # и позволяет проверять биржу при обработке ордеров
            # is_bot_restart_recovery уже установлен из базового класса BaseStrategy

            # Проверяем, была ли активна позиция на момент сохранения
            if hasattr(self, 'position_active') and self.position_active:
                log_info(self.user_id, f"🎯 Восстанавливаю активную позицию SignalScalper", "SignalScalper")

                # Восстанавливаем подписки на события цен (критически важно для мониторинга)
                if not hasattr(self, '_price_subscription_restored'):
                    await self.event_bus.subscribe(EventType.PRICE_UPDATE, self.handle_price_update, user_id=self.user_id)
                    log_info(self.user_id, f"✅ Восстановлена подписка на обновления цен для {self.symbol}", "SignalScalper")
                    self._price_subscription_restored = True

                # Если есть состояние усреднения - восстанавливаем его детально
                if hasattr(self, 'averaging_executed') and self.averaging_executed:
                    log_info(self.user_id,
                            f"📊 Восстанавливаю состояние усреднения: executed={self.averaging_executed}, "
                            f"общий размер: {getattr(self, 'total_position_size', 0)}, средняя цена: {getattr(self, 'average_entry_price', 0)}",
                            "SignalScalper")

                # Если есть активный стоп-лосс - восстанавливаем его отслеживание
                if hasattr(self, 'stop_loss_order_id') and self.stop_loss_order_id:
                    log_info(self.user_id, f"🛡️ Восстанавливаю отслеживание стоп-лосса: {self.stop_loss_order_id}", "SignalScalper")

                # Восстанавливаем мониторинг позиции для предотвращения десинхронизации
                if not self._position_monitor_task or self._position_monitor_task.done():
                    if hasattr(self, 'position_size') and getattr(self, 'position_size', 0) > 0:
                        self._position_monitor_task = asyncio.create_task(self._monitor_active_position())
                        log_info(self.user_id, f"🔍 Запущен монитор позиции для {self.symbol}", "SignalScalper")

                # Проверяем состояние замороженной конфигурации
                if hasattr(self, 'config_frozen') and self.config_frozen:
                    log_info(self.user_id, f"❄️ Восстановлена заморозка конфигурации активной сделки", "SignalScalper")

                # Восстанавливаем последние сигналы и счетчики для корректной работы логики
                if hasattr(self, 'last_signal'):
                    log_debug(self.user_id, f"📡 Восстановлен последний сигнал: {getattr(self, 'last_signal', 'None')}", "SignalScalper")

                # Инициализируем анализатор сигналов если его нет
                if not self.signal_analyzer:
                    from analysis.signal_analyzer import SignalAnalyzer
                    self.signal_analyzer = SignalAnalyzer(self.user_id, self.api, self.config)
                    log_info(self.user_id, f"📈 Переинициализирован анализатор сигналов", "SignalScalper")

                log_info(self.user_id, f"✅ Активная позиция SignalScalper для {self.symbol} полностью восстановлена", "SignalScalper")

            else:
                log_info(self.user_id, f"ℹ️ Позиция неактивна, восстанавливаю только базовые компоненты", "SignalScalper")

                # Даже для неактивной позиции нужен анализатор сигналов
                if not self.signal_analyzer:
                    from analysis.signal_analyzer import SignalAnalyzer
                    self.signal_analyzer = SignalAnalyzer(self.user_id, self.api, self.config)
                    log_info(self.user_id, f"📈 Инициализирован анализатор сигналов для неактивной позиции", "SignalScalper")

            # КРИТИЧЕСКИ ВАЖНО: Принудительная синхронизация с биржей
            await self._force_sync_with_exchange()

            # Проверяем синхронизацию с базой данных
            await self._sync_database_state()

        except Exception as e:
            log_error(self.user_id, f"❌ Ошибка специфичного восстановления SignalScalper: {e}", "SignalScalper")

    async def _sync_database_state(self):
        """
        Синхронизирует состояние стратегии с базой данных.
        Проверяет соответствие активных сделок в памяти и БД.
        """
        try:
            # Если есть активная связь с БД, проверяем что запись существует
            if hasattr(self, 'active_trade_db_id') and self.active_trade_db_id:
                from database.db_trades import db_manager

                # Проверяем, что сделка действительно существует в БД
                trade_exists = await db_manager.trade_exists(self.active_trade_db_id)

                if trade_exists:
                    log_info(self.user_id, f"✅ Связь с БД подтверждена: trade_id={self.active_trade_db_id}", "SignalScalper")
                else:
                    log_warning(self.user_id, f"⚠️ Сделка {self.active_trade_db_id} не найдена в БД, сбрасываю связь", "SignalScalper")
                    delattr(self, 'active_trade_db_id')

        except Exception as e:
            log_error(self.user_id, f"Ошибка синхронизации с БД: {e}", "SignalScalper")

    async def _force_sync_with_exchange(self):
        """
        КРИТИЧЕСКИ ВАЖНАЯ принудительная синхронизация состояния стратегии с биржей.
        Восстанавливает состояние активных позиций и предотвращает дублирование ордеров.
        """
        try:
            log_info(self.user_id, f"🔄 Принудительная синхронизация с биржей для {self.symbol}...", "SignalScalper")

            # Получаем активные позиции с биржи
            exchange_positions = await self.api.get_positions()
            active_position = None

            for position in exchange_positions:
                if (position.get('symbol') == self.symbol and
                    float(position.get('size', 0)) > 0):
                    active_position = position
                    break

            if active_position:
                # На бирже есть активная позиция по нашему символу
                position_size = Decimal(str(active_position.get('size', 0)))
                position_side = active_position.get('side', '').lower()
                entry_price = Decimal(str(active_position.get('avgPrice', 0)))

                log_warning(self.user_id,
                          f"🚨 НАЙДЕНА АКТИВНАЯ ПОЗИЦИЯ на бирже: {self.symbol} {position_side.upper()} "
                          f"размер={position_size}, вход=${entry_price:.4f}", "SignalScalper")

                # ПРИНУДИТЕЛЬНО восстанавливаем состояние стратегии
                if not self.position_active:
                    log_warning(self.user_id,
                              f"⚠️ Стратегия считала позицию НЕАКТИВНОЙ, но на бирже есть позиция! "
                              f"ВОССТАНАВЛИВАЮ состояние...", "SignalScalper")

                    # Восстанавливаем базовое состояние позиции
                    self.position_active = True
                    self.active_direction = "LONG" if position_side == "long" else "SHORT"
                    self.entry_price = entry_price
                    self.position_size = position_size
                    self.peak_profit_usd = Decimal('0')
                    self.hold_signal_counter = 0

                    # Восстанавливаем подписку на события цены
                    await self.event_bus.subscribe(EventType.PRICE_UPDATE, self.handle_price_update, user_id=self.user_id)
                    log_info(self.user_id, f"✅ Восстановлена подписка на обновления цен", "SignalScalper")

                    # Проверяем, было ли усреднение (размер больше базового)
                    expected_base_size = await self._estimate_base_position_size()
                    if expected_base_size and position_size > expected_base_size * Decimal('1.1'):
                        # Похоже на усреднение, пытаемся восстановить состояние
                        log_info(self.user_id,
                               f"📊 Обнаружено возможное усреднение: биржа={position_size}, ожидаемый_базовый≈{expected_base_size:.0f}",
                               "SignalScalper")

                        # Устанавливаем усредненные значения
                        self.total_position_size = position_size
                        self.average_entry_price = entry_price
                        self.averaging_executed = True  # Флаг что было усреднение

                        log_info(self.user_id,
                               f"📊 Восстановлено состояние усреднения: executed={self.averaging_executed}, "
                               f"total_size={self.total_position_size}, avg_price={self.average_entry_price:.4f}",
                               "SignalScalper")
                    else:
                        # Обычная позиция без усреднения
                        self.total_position_size = Decimal('0')
                        self.average_entry_price = Decimal('0')

                    # Попытаемся восстановить стоп-лосс
                    await self._restore_stop_loss_from_exchange()

                    log_info(self.user_id,
                           f"✅ Состояние стратегии ВОССТАНОВЛЕНО: {self.active_direction} позиция "
                           f"размер={position_size}, вход=${entry_price:.4f}", "SignalScalper")

                    # Отправляем уведомление пользователю
                    averaging_status = "Да" if self.averaging_executed else "Нет"
                    recovery_message = (
                        f"🔄 <b>ВОССТАНОВЛЕНИЕ ПОЗИЦИИ</b>\n\n"
                        f"📊 <b>Символ:</b> {self.symbol}\n"
                        f"📈 <b>Направление:</b> {self.active_direction}\n"
                        f"📏 <b>Размер:</b> {position_size}\n"
                        f"💰 <b>Цена входа:</b> {entry_price:.4f} USDT\n"
                        f"🔄 <b>Усреднение выполнено:</b> {averaging_status}\n\n"
                        f"Стратегия продолжит мониторинг восстановленной позиции."
                    )

                    if self.bot:
                        await self.bot.send_message(self.user_id, recovery_message, parse_mode="HTML")

                else:
                    # Позиция была активна, проверяем соответствие размеров
                    strategy_total_size = self.total_position_size if self.total_position_size > 0 else self.position_size

                    if abs(strategy_total_size - position_size) > Decimal('1'):  # Допуск в 1 единицу
                        log_warning(self.user_id,
                                  f"⚠️ НЕСООТВЕТСТВИЕ РАЗМЕРОВ: стратегия={strategy_total_size}, биржа={position_size}. "
                                  f"Синхронизирую...", "SignalScalper")

                        # Принудительно синхронизируем размеры
                        if self.total_position_size > 0:
                            self.total_position_size = position_size
                        else:
                            self.position_size = position_size

            else:
                # На бирже НЕТ активных позиций по нашему символу
                if self.position_active:
                    log_warning(self.user_id,
                              f"⚠️ Стратегия считала позицию АКТИВНОЙ, но на бирже позиции НЕТ! "
                              f"Сбрасываю состояние...", "SignalScalper")

                    # Принудительно сбрасываем состояние
                    await self._force_reset_position_state()
                else:
                    log_info(self.user_id, f"✅ Синхронизация подтверждена: нет активных позиций", "SignalScalper")

        except Exception as e:
            log_error(self.user_id, f"❌ Критическая ошибка синхронизации с биржей: {e}", "SignalScalper")

    async def _estimate_base_position_size(self) -> Optional[Decimal]:
        """
        Оценивает размер базовой позиции на основе текущих настроек.
        Используется для определения усреднения.
        """
        try:
            order_amount = self._convert_to_decimal(self.get_config_value("order_amount", 50.0))
            leverage = self._convert_to_decimal(self.get_config_value("leverage", 1.0))

            # Используем текущую цену для оценки
            current_price = await self._get_current_market_price()
            if current_price:
                estimated_qty = await self.api.calculate_quantity_from_usdt(
                    self.symbol, order_amount, leverage, price=current_price
                )
                return estimated_qty
        except Exception as e:
            log_error(self.user_id, f"Ошибка оценки базового размера позиции: {e}", "SignalScalper")

        return None

    async def _get_current_market_price(self) -> Optional[Decimal]:
        """Получает текущую рыночную цену символа."""
        try:
            ticker = await self.api.get_ticker(self.symbol)
            if ticker and 'lastPrice' in ticker:
                return Decimal(str(ticker['lastPrice']))
        except Exception as e:
            log_error(self.user_id, f"Ошибка получения рыночной цены: {e}", "SignalScalper")
        return None

    async def _restore_stop_loss_from_exchange(self):
        """Восстанавливает информацию о стоп-лоссе с биржи."""
        try:
            # Получаем информацию о торговых стопах
            position_info = await self.api.get_position_info(self.symbol)
            if position_info:
                stop_loss_str = position_info.get('stopLoss', '0')
                if stop_loss_str and stop_loss_str != '0':
                    self.stop_loss_price = Decimal(str(stop_loss_str))
                    self.stop_loss_order_id = f"restored_sl_{self.symbol}_{int(time.time())}"
                    log_info(self.user_id, f"🛡️ Восстановлен стоп-лосс: ${self.stop_loss_price:.4f}", "SignalScalper")
        except Exception as e:
            log_error(self.user_id, f"Ошибка восстановления стоп-лосса: {e}", "SignalScalper")

    async def _force_reset_position_state(self):
        """Принудительно сбрасывает состояние позиции."""
        log_info(self.user_id, "🔄 Принудительный сброс состояния позиции...", "SignalScalper")

        # Сбрасываем все переменные состояния
        self.position_active = False
        self.active_direction = None
        self.entry_price = None
        self.position_size = None
        self.peak_profit_usd = Decimal('0')
        self.hold_signal_counter = 0

        # Сбрасываем переменные НОВОЙ системы усреднения (одиночное удвоение)
        self.averaging_executed = False
        self.averaging_count = 0  # Сброс счетчика усреднений
        self.initial_margin_usd = Decimal('0')
        self.total_fees_paid = Decimal('0')
        self.total_position_size = Decimal('0')
        self.average_entry_price = Decimal('0')

        # Сбрасываем стоп-лосс
        self.stop_loss_order_id = None
        self.stop_loss_price = None

        # Отписываемся от событий цены
        await self.event_bus.unsubscribe(self._handle_price_update)

        log_info(self.user_id, "✅ Состояние позиции сброшено", "SignalScalper")


