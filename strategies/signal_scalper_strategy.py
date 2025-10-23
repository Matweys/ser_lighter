import asyncio
import time
from typing import Dict, Any, Optional
from decimal import Decimal, getcontext
from datetime import datetime

from api.bybit_api import BybitAPI
from .base_strategy import BaseStrategy
from core.enums import StrategyType, EventType
from core.logger import log_info, log_error, log_warning, log_debug
from core.events import EventBus, NewCandleEvent, PriceUpdateEvent, OrderFilledEvent
from analysis.signal_analyzer import SignalAnalyzer, SignalAnalysisResult
from analysis.spike_detector import SpikeDetector
from core.concurrency_manager import strategy_locked

getcontext().prec = 28


class SignalScalperStrategy(BaseStrategy):
    """
    Стратегия скальпинга, основанная на внешних сигналах от SignalAnalyzer (EMA + RSI).
    Реализует сложную логику входа, удержания и выхода из позиции.
    """

    def __init__(self, user_id: int, symbol: str, signal_data: Dict[str, Any], api: BybitAPI, event_bus: EventBus,
                 bot: "Bot", config: Optional[Dict] = None, account_priority: int = 1):
        super().__init__(user_id, symbol, signal_data, api, event_bus, bot, config, account_priority)

        # Компоненты
        self.signal_analyzer: Optional[SignalAnalyzer] = None
        self.spike_detector: Optional[SpikeDetector] = None  # Детектор всплесков для оптимизации входа

        # Состояние стратегии
        self.position_active = False
        self.active_direction: Optional[str] = None  # "LONG" или "SHORT"
        self.entry_price: Optional[Decimal] = None
        self.entry_time: Optional[datetime] = None  # Время открытия позиции
        self.position_size: Optional[Decimal] = None
        self.last_closed_direction: Optional[str] = None
        self.hold_signal_counter = 0
        self.peak_profit_usd: Decimal = Decimal('0')
        self.is_waiting_for_trade = False  # Флаг для блокировки действий
        self.processed_orders: set = set()  # Отслеживание обработанных ордеров
        self.current_order_id: Optional[str] = None  # ID текущего ожидаемого ордера
        self.intended_order_amount: Optional[Decimal] = None  # Запрошенная сумма ордера
        self.close_reason: Optional[str] = None  # Причина закрытия позиции для передачи в _handle_order_filled
        self._last_known_price: Optional[Decimal] = None  # Последняя известная цена для расчета PnL в координаторе

        # Стоп-лосс управление
        self.stop_loss_order_id: Optional[str] = None
        self.stop_loss_price: Optional[Decimal] = None

        # Система подтверждения сигналов и кулдауна
        self.last_signal: Optional[str] = None  # Последний полученный сигнал
        self.signal_confirmation_count = 0  # Счетчик одинаковых сигналов подряд
        self.required_confirmations = 2  # Требуемое количество подтверждений
        self.last_trade_close_time: Optional[float] = None  # Время закрытия последней сделки
        self.cooldown_seconds = 60  # Кулдаун в секундах (1 минута)
        self.last_trade_was_loss = False  # Была ли последняя сделка убыточной

        # СИСТЕМА КОНТРОЛЯ РЕВЕРСОВ
        self.last_reversal_time: Optional[float] = None  # Время последнего реверса
        self.reversal_cooldown_seconds = 60  # Кулдаун после реверса в секундах (1 минута)
        self.reversal_required_confirmations = 2  # Требуемые подтверждения после реверса
        self.after_reversal_mode = False  # Флаг: находимся ли мы в режиме после реверса


        # ОСНОВНАЯ СИСТЕМА УСРЕДНЕНИЯ
        self.averaging_enabled = False  # Включена ли система усреднения
        self.averaging_executed = False  # Флаг: было ли выполнено усреднение
        self.averaging_count = 0  # Счетчик выполненных усреднений
        self.max_averaging_count = 1  # Максимальное количество усреднений (из конфигурации)
        self.averaging_trigger_loss_percent = Decimal('15.0')  # Триггер: убыток в % от цены
        self.averaging_multiplier = Decimal('1.0')  # Удвоение суммы
        self.averaging_stop_loss_percent = Decimal('55.0')  # Программный SL: от маржи
        self.total_position_size = Decimal('0')  # Общий размер позиции после усреднения
        self.average_entry_price = Decimal('0')  # Средняя цена входа после усреднения
        self.initial_margin_usd = Decimal('0')  # Начальная маржа для расчета % убытка
        self.current_total_margin = Decimal('0')  # ТЕКУЩАЯ общая маржа (initial + все усреднения)
        self.total_fees_paid = Decimal('0')  # Накопленные комиссии
        self.intermediate_averaging_executed = False  # Флаг: было ли промежуточное усреднение (legacy)
        self.use_breakeven_exit = False  # НОВЫЙ ФЛАГ: Выход в безубыток после усреднений (вместо трейлинга)

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
        self.stagnation_averaging_multiplier = Decimal('1.0')  # Множитель усреднения
        self.stagnation_averaging_leverage = 1  # Плечо для усреднения (x1)


        # Мониторинг состояния детектора
        self.stagnation_monitor_active = False  # Активен ли мониторинг
        self.stagnation_monitor_start_time: Optional[float] = None  # Время начала мониторинга
        self.stagnation_current_range_index: Optional[int] = None  # Индекс текущего отслеживаемого диапазона
        self.stagnation_averaging_executed = False  # Флаг: было ли выполнено усреднение
        # ============================================================

        # Recovery Handler для восстановления после перезагрузки сервера
        from strategies.recovery import SignalScalperRecoveryHandler
        self.recovery_handler = SignalScalperRecoveryHandler(self)


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
                threshold=0.0008  # 0.05% порог для всплеска (снижен для большего количества сигналов)
            )
            # Настраиваем временные окна для анализа (по умолчанию: 3, 5, 10 минут)
            # Можно изменить на 3, 7, 15 для более долгосрочного анализа
            log_info(self.user_id,
                    f"📡 SpikeDetector инициализирован для {self.symbol} (анализ: 3m/5m/10m)",
                    "SignalScalper")

            # ============================================================
            # ЗАГРУЗКА ПАРАМЕТРОВ ДЕТЕКТОРА ЗАСТРЯВШЕЙ ЦЕНЫ
            # ============================================================
            self.stagnation_detector_enabled = self.config.get("enable_stagnation_detector", True)
            self.stagnation_check_interval = int(self.config.get("stagnation_check_interval_seconds", 30))
            # НОВАЯ СИСТЕМА: диапазоны задаются через min/max проценты
            stag_min = self._convert_to_decimal(self.config.get("stagnation_trigger_min_percent", "15.0"))
            stag_max = self._convert_to_decimal(self.config.get("stagnation_trigger_max_percent", "20.0"))
            self.stagnation_ranges = [{"min": stag_min, "max": stag_max}]
            self.stagnation_averaging_multiplier = self._convert_to_decimal(self.config.get("stagnation_averaging_multiplier", "1.0"))
            self.stagnation_averaging_leverage = int(self.config.get("stagnation_averaging_leverage", 1))

            # ============================================================

            # Загружаем параметры ОСНОВНОГО усреднения
            self.averaging_enabled = self.config.get("enable_averaging", True)
            self.max_averaging_count = int(self.config.get("max_averaging_count", 1))
            self.averaging_trigger_loss_percent = self._convert_to_decimal(self.config.get("averaging_trigger_loss_percent", "15.0"))
            self.averaging_multiplier = self._convert_to_decimal(self.config.get("averaging_multiplier", "1.0"))
            self.averaging_stop_loss_percent = self._convert_to_decimal(self.config.get("averaging_stop_loss_percent", "50.0"))

    async def start(self) -> bool:
        """Запуск стратегии и подписка на события свечей."""
        is_started = await super().start()
        if is_started:
            # Основной триггер стратегии - новая 5-минутная свеча
            await self.event_bus.subscribe(EventType.NEW_CANDLE, self._handle_new_candle, user_id=self.user_id)
        return is_started

    async def stop(self, reason: str = "Manual stop"):
        """Остановка стратегии и отписка от событий."""
        # КРИТИЧНО: Сначала останавливаем стратегию (is_running=False), затем отписываемся
        await super().stop(reason)
        await self.event_bus.unsubscribe(self._handle_new_candle)

    @strategy_locked
    async def _handle_new_candle(self, event: NewCandleEvent):
        """
        Главный обработчик логики на каждой новой свече.

        THREAD-SAFE: Защищено декоратором @strategy_locked для предотвращения race conditions.
        """
        # КРИТИЧНО: Проверяем флаг работы в самом начале
        if not self.is_running:
            return

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
                    should_enter, final_signal, spike_reason = self.spike_detector.should_enter_on_pullback(signal)

                    # Получаем статистику для логирования
                    recent_spikes = self.spike_detector.get_recent_spikes(seconds=600)
                    total_spikes = len(self.spike_detector.spike_history)
                    candles_count = len(self.spike_detector.price_history)

                    if not should_enter:
                        log_info(self.user_id,
                                f"⏸️ Spike Detector ({candles_count} свечей, {len(recent_spikes)}/{total_spikes} всплесков за 10мин): {spike_reason}",
                                "SignalScalper")
                        return

                    # ВАЖНО: Используем развернутый сигнал (может отличаться от исходного!)
                    if final_signal != signal:
                        log_info(self.user_id,
                                f"🔄 РАЗВОРОТ! Spike Detector изменил сигнал: {signal} → {final_signal}",
                                "SignalScalper")
                        signal = final_signal  # Перезаписываем сигнал!

                    log_info(self.user_id,
                            f"✅ Spike Detector ({candles_count} свечей, {len(recent_spikes)}/{total_spikes} всплесков за 10мин): {spike_reason}",
                            "SignalScalper")

                # Входим в позицию
                await self._enter_position(direction=signal, signal_price=price)
            else:
                # При сигнале HOLD сбрасываем счетчик подтверждений
                self.signal_confirmation_count = 0
                self.last_signal = None

    async def _handle_price_update(self, event: PriceUpdateEvent):
        """Внутренний метод обработки обновления цены (вызывается из BaseStrategy)"""
        await self.handle_price_update(event)

    @strategy_locked
    async def handle_price_update(self, event: PriceUpdateEvent):
        """
        Обработка тиков цены для усреднения и динамического тейк-профита.

        THREAD-SAFE: Защищено декоратором @strategy_locked для предотвращения race conditions.
        """
        # КРИТИЧЕСКИ ВАЖНО: Проверяем что это цена НАШЕГО символа!
        if event.symbol != self.symbol:
            return

        if not self.position_active or not self.entry_price or self.is_waiting_for_trade:
            return

        current_price = event.price

        # Защита от неправильных цен
        if current_price <= 0:
            return

        # СОХРАНЯЕМ ПОСЛЕДНЮЮ ЦЕНУ для координатора multi-account
        self._last_known_price = current_price

        # Проверка на адекватность изменения цены (не больше 50% от цены входа)
        price_change_percent = abs((current_price - self.entry_price) / self.entry_price * Decimal('100'))
        if price_change_percent > Decimal('50'):
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
            # Рассчитываем % убытка от ИЗМЕНЕНИЯ ЦЕНЫ
            if entry_price_to_use > 0:
                # Рассчитываем процентное изменение цены
                price_change_percent = abs((current_price - entry_price_to_use) / entry_price_to_use) * Decimal('100')

                # Для проверки триггера берем только убытки (когда цена движется против позиции)
                if pnl < 0:
                    loss_percent_from_price = price_change_percent
                else:
                    loss_percent_from_price = Decimal('0')

                log_debug(self.user_id,
                         f"📊 Мониторинг усреднения: PnL=${pnl:.2f}, цена_входа=${entry_price_to_use:.4f}, "
                         f"текущая_цена=${current_price:.4f}, изменение_цены={loss_percent_from_price:.2f}%, триггер={self.averaging_trigger_loss_percent}%",
                         "SignalScalper")

                # Проверяем триггер усреднения: изменение цены >= порога (например, 25%)
                if loss_percent_from_price >= self.averaging_trigger_loss_percent:
                    log_warning(self.user_id,
                               f"🎯 ТРИГГЕР УСРЕДНЕНИЯ! Изменение цены {loss_percent_from_price:.2f}% >= {self.averaging_trigger_loss_percent}%",
                               "SignalScalper")
                    await self._execute_averaging(current_price)

        # ============================================================
        # НОВАЯ ЛОГИКА: Выход в безубыток после усреднений
        # Используем ТОЧНУЮ цену безубыточности от биржи Bybit (breakEvenPrice)
        # Она уже включает: среднюю цену входа + все комиссии + funding rate
        # ============================================================
        if self.use_breakeven_exit:
            # Получаем ТОЧНУЮ цену безубыточности от биржи
            try:
                positions = await self.api.get_positions(symbol=self.symbol)
                if positions and len(positions) > 0:
                    breakeven_price_from_exchange = positions[0].get("breakEvenPrice", Decimal('0'))

                    if breakeven_price_from_exchange > 0:
                        # Проверяем достижение безубытка по точной цене от биржи
                        reached_breakeven = False
                        if self.active_direction == "LONG":
                            # Для LONG: текущая цена >= breakEvenPrice
                            reached_breakeven = current_price >= breakeven_price_from_exchange
                        else:  # SHORT
                            # Для SHORT: текущая цена <= breakEvenPrice
                            reached_breakeven = current_price <= breakeven_price_from_exchange

                        if reached_breakeven:
                            # Рассчитываем примерный PnL для информации
                            estimated_pnl = (current_price - breakeven_price_from_exchange) * position_size_to_use if self.active_direction == "LONG" else (breakeven_price_from_exchange - current_price) * position_size_to_use

                            log_warning(self.user_id,
                                       f"💰 ВЫХОД В БЕЗУБЫТОК ПОСЛЕ УСРЕДНЕНИЯ! "
                                       f"Цена БЕ (от биржи): ${breakeven_price_from_exchange:.4f}, "
                                       f"текущая_цена: ${current_price:.4f}, PnL≈${estimated_pnl:.2f}",
                                       "SignalScalper")
                            await self._close_position("breakeven_after_averaging")
                            return
                        else:
                            # Рассчитываем расстояние до безубытка
                            distance_to_breakeven = abs(current_price - breakeven_price_from_exchange)
                            distance_pct = (distance_to_breakeven / breakeven_price_from_exchange) * Decimal('100')

                            log_debug(self.user_id,
                                     f"⏳ Ожидание безубытка: цена_БЕ=${breakeven_price_from_exchange:.4f}, "
                                     f"текущая=${current_price:.4f}, расстояние={distance_pct:.3f}% (${distance_to_breakeven:.2f})",
                                     "SignalScalper")
                    else:
                        # Fallback: если биржа не вернула breakEvenPrice
                        log_debug(self.user_id,
                                 "⚠️ Биржа не вернула breakEvenPrice, используем PnL >= 0",
                                 "SignalScalper")
                        if pnl >= 0:
                            await self._close_position("breakeven_after_averaging")
                            return
                else:
                    # Fallback: позиция не найдена на бирже
                    if pnl >= 0:
                        await self._close_position("breakeven_after_averaging")
                        return

            except Exception as e:
                # Fallback: в случае ошибки API
                log_error(self.user_id,
                         f"Ошибка получения breakEvenPrice: {e}, fallback на PnL >= 0",
                         "SignalScalper")
                if pnl >= 0:
                    await self._close_position("breakeven_after_averaging")
                    return
        else:
            # СТАНДАРТНАЯ ЛОГИКА: Трейлинг-стоп (когда НЕТ усреднений)
            # Обновляем пиковую прибыль
            if pnl > self.peak_profit_usd:
                self.peak_profit_usd = pnl

            # Поэтапный трейлинг с динамическими порогами и 20% откатом
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
        # ============================================================

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
        self.averaging_multiplier = self._convert_to_decimal(self.config.get("averaging_multiplier", "1.0"))
        self.averaging_stop_loss_percent = self._convert_to_decimal(self.config.get("averaging_stop_loss_percent", "55.0"))

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
        """
        Обработка исполненных ордеров.

        ВАЖНО: НЕ использует @strategy_locked, т.к. ВСЕГДА вызывается из контекста,
        где блокировка УЖЕ захвачена (_await_order_fill вызывается из _enter_position,
        которая вызывается из _handle_new_candle с @strategy_locked).

        Добавление @strategy_locked вызывает DEADLOCK (asyncio.Lock не реентрабельная)!
        """
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

            # Проверяем что ордер принадлежит ЭТОЙ стратегии (symbol, user_id и bot_priority)
            if order_in_db['symbol'] != self.symbol or order_in_db['user_id'] != self.user_id:
                log_debug(self.user_id,
                         f"[НЕ НАШ] Ордер {event.order_id} принадлежит другой стратегии или пользователю. ИГНОРИРУЮ.",
                         "SignalScalper")
                return

            # КРИТИЧНО для Multi-Account: Проверяем что ордер принадлежит ЭТОМУ боту
            order_bot_priority = order_in_db.get('bot_priority', 1)  # По умолчанию 1 для обратной совместимости
            if order_bot_priority != self.account_priority:
                log_debug(self.user_id,
                         f"[НЕ НАШ БОТ] Ордер {event.order_id} принадлежит Bot_{order_bot_priority}, а это Bot_{self.account_priority}. ИГНОРИРУЮ.",
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
                                    self.entry_time = datetime.now()  # Сохраняем время открытия позиции
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
            self.entry_time = datetime.now()  # Сохраняем время открытия позиции
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
            # ТЕКУЩАЯ маржа отслеживает реальные вложения (initial + усреднения)
            self.current_total_margin = self.intended_order_amount

            log_info(self.user_id, f"💰 Начальная маржа для усреднения: ${self.initial_margin_usd:.2f}", "SignalScalper")

            # КРИТИЧНО: Загружаем параметры усреднения из ЗАМОРОЖЕННОЙ конфигурации
            if self.active_trade_config:
                self.averaging_trigger_loss_percent = self._convert_to_decimal(self.active_trade_config.get("averaging_trigger_loss_percent", "15.0"))
                self.averaging_stop_loss_percent = self._convert_to_decimal(self.active_trade_config.get("averaging_stop_loss_percent", "55.0"))
                self.averaging_multiplier = self._convert_to_decimal(self.active_trade_config.get("averaging_multiplier", "1.0"))

                # УЛУЧШЕНО: Показываем параметры ОБОИХ усреднений для полной ясности
                enable_stag = self.active_trade_config.get("enable_stagnation_detector", True)
                enable_avg = self.active_trade_config.get("enable_averaging", True)

                log_info(self.user_id,
                        f"🔧 Параметры усреднений:\n"
                        f"   📍 Усреднение #1 (Детектор застревания): {'✅ ВКЛ' if enable_stag else '❌ ВЫКЛ'}\n"
                        f"      ├─ Триггер: {self.stagnation_ranges[0]['min']}-{self.stagnation_ranges[0]['max']}% от маржи\n"
                        f"      ├─ Время наблюдения: {self.stagnation_check_interval} сек\n"
                        f"      └─ Множитель: {self.stagnation_averaging_multiplier}x\n"
                        f"   📊 Усреднение #2 (Основное): {'✅ ВКЛ' if enable_avg else '❌ ВЫКЛ'}\n"
                        f"      ├─ Триггер: {self.averaging_trigger_loss_percent}% от маржи\n"
                        f"      ├─ Множитель: {self.averaging_multiplier}x\n"
                        f"      └─ SL после усреднений: {self.averaging_stop_loss_percent}%",
                        "SignalScalper")

            # КРИТИЧЕСКИ ВАЖНО: Обновляем ордер OPEN в БД
            try:
                await db_manager.update_order_on_fill(
                    order_id=event.order_id,
                    filled_quantity=event.qty,
                    average_price=event.price,
                    commission=event.fee
                )
                log_debug(self.user_id, f"✅ Ордер OPEN {event.order_id} обновлён в БД как FILLED", "SignalScalper")
            except Exception as db_error:
                log_error(self.user_id, f"❌ Ошибка обновления OPEN ордера {event.order_id} в БД: {db_error}", "SignalScalper")

            # ВСЕГДА устанавливаем стоп-лосс для защиты (даже при усреднении)
            await self._place_stop_loss_order(self.active_direction, self.entry_price, self.position_size)

        elif is_averaging_order and self.position_active:
            # Ордер на усреднение позиции
            log_info(self.user_id, f"[УСРЕДНЕНИЕ] Обрабатываем ордер усреднения: {event.order_id}", "SignalScalper")

            # СОХРАНЯЕМ данные ДО усреднения для уведомления
            old_entry_price = self.entry_price
            old_size = self.position_size

            # ПРАВИЛЬНЫЙ расчет добавленной маржи:
            # Это просто order_amount * multiplier (без учета плеча, т.к. leverage=1 для усреднения)
            # Берем из замороженной конфигурации
            order_amount = self._convert_to_decimal(self._get_frozen_config_value("order_amount", 100.0))
            # Определяем множитель (averaging_multiplier или stagnation_multiplier)
            if self.averaging_executed or self.averaging_count > 0:
                # Это основное усреднение
                multiplier = self.averaging_multiplier
            elif self.stagnation_averaging_executed:
                # Это усреднение по стагнации
                multiplier = self.stagnation_averaging_multiplier
            else:
                # Первое усреднение - определяем по контексту
                multiplier = self.averaging_multiplier if self.averaging_enabled else self.stagnation_averaging_multiplier

            averaging_amount = order_amount * multiplier

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

            # ОБНОВЛЕНИЕ ТЕКУЩЕЙ МАРЖИ: добавляем сумму усреднения
            self.current_total_margin += averaging_amount
            log_info(self.user_id, f"💰 Текущая маржа обновлена: ${self.current_total_margin:.2f} (добавлено ${averaging_amount:.2f})", "SignalScalper")

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

            # КРИТИЧЕСКИ ВАЖНО: Обновляем ордер AVERAGING в БД
            try:
                await db_manager.update_order_on_fill(
                    order_id=event.order_id,
                    filled_quantity=event.qty,
                    average_price=event.price,
                    commission=event.fee
                )
                log_debug(self.user_id, f"✅ Ордер AVERAGING {event.order_id} обновлён в БД как FILLED", "SignalScalper")
            except Exception as db_error:
                log_error(self.user_id, f"❌ Ошибка обновления AVERAGING ордера {event.order_id} в БД: {db_error}", "SignalScalper")

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

            # ТОЧНЫЙ РАСЧЕТ PnL: Берём РЕАЛЬНЫЕ данные от биржи (closedPnL)
            # Биржа сама считает с учетом всех усреднений, комиссий, проскальзываний
            pnl_net = None
            entry_price_for_pnl = None
            exit_price_for_pnl = None
            position_size_for_pnl = None

            try:
                log_info(self.user_id, f"[BYBIT API] Запрашиваю реальный closedPnL от биржи для {self.symbol}...", "SignalScalper")
                closed_pnl_data = await self.api.get_closed_pnl(self.symbol, limit=1)

                if closed_pnl_data:
                    # Используем ТОЧНЫЕ данные от биржи
                    pnl_net = closed_pnl_data['closedPnl']  # Уже с учетом ВСЕХ комиссий!
                    entry_price_for_pnl = closed_pnl_data['avgEntryPrice']
                    exit_price_for_pnl = closed_pnl_data['avgExitPrice']
                    position_size_for_pnl = closed_pnl_data['closedSize']

                    log_info(self.user_id,
                            f"✅ [BYBIT PNL] Получен ТОЧНЫЙ PnL от биржи: "
                            f"closedPnl={pnl_net:.4f} USDT, "
                            f"avgEntryPrice={entry_price_for_pnl:.4f}, "
                            f"avgExitPrice={exit_price_for_pnl:.4f}, "
                            f"closedSize={position_size_for_pnl}",
                            "SignalScalper")
                else:
                    log_warning(self.user_id, f"⚠️ [BYBIT PNL] Не удалось получить closedPnL от биржи, используем расчет вручную", "SignalScalper")

            except Exception as api_error:
                log_error(self.user_id, f"❌ [BYBIT PNL] Ошибка запроса closedPnL: {api_error}, используем расчет вручную", "SignalScalper")

            # ФОЛБЭК: Если не удалось получить от биржи, считаем сами (старая логика)
            if pnl_net is None:
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

                exit_price_for_pnl = event.price

                pnl_gross = (exit_price_for_pnl - entry_price_for_pnl) * position_size_for_pnl if self.active_direction == "LONG" else (
                    entry_price_for_pnl - exit_price_for_pnl) * position_size_for_pnl

                # НАКОПЛЕНИЕ КОМИССИИ ЗАКРЫТИЯ
                self.total_fees_paid += event.fee

                # ПРАВИЛЬНЫЙ РАСЧЁТ: Вычитаем ВСЕ накопленные комиссии (открытие + усреднение + закрытие)
                pnl_net = pnl_gross - self.total_fees_paid

                log_info(self.user_id,
                        f"[PNL_CALC FALLBACK] entry_price={entry_price_for_pnl:.4f}, position_size={position_size_for_pnl}, "
                        f"exit_price={exit_price_for_pnl:.4f}, close_fee={event.fee:.4f}, total_fees={self.total_fees_paid:.4f}, "
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
                log_warning(self.user_id, f"Убыточная сделка! Следующему сигналу потребуется 2 продолжай подтверждения.", "SignalScalper")

            # Сбрасываем счетчики подтверждения после закрытия сделки
            self.signal_confirmation_count = 0
            self.last_signal = None

            # Отменяем стоп-лосс перед сбросом состояния (БЫСТРО)
            if self.stop_loss_order_id:
                await self._cancel_stop_loss_order()

            # СОХРАНЯЕМ значения перед сбросом для передачи в уведомление
            # ПОЛУЧАЕМ ИЗ БД для надёжности (работает даже после перезапуска бота)
            from database.db_trades import db_manager
            open_order = await db_manager.get_open_order_for_position(self.user_id, self.symbol, self.account_priority)
            if open_order:
                saved_entry_time = open_order.get('filled_at')  # Время из БД
                saved_entry_price = open_order.get('average_price')  # Цена из БД
                log_debug(self.user_id, f"[ИЗ БД] Время входа: {saved_entry_time}, Цена входа: {saved_entry_price}", "SignalScalper")
            else:
                # Fallback на переменные в памяти (если БД недоступна)
                saved_entry_time = self.entry_time
                saved_entry_price = self.entry_price
                log_warning(self.user_id, f"[FALLBACK] Не найден OPEN ордер в БД, используем данные из памяти", "SignalScalper")

            # Сброс состояния (ВКЛЮЧАЯ ПЕРЕМЕННЫЕ УСРЕДНЕНИЯ)
            self.position_active = False
            self.active_direction = None
            self.entry_price = None
            self.entry_time = None  # Сбрасываем время входа
            self.position_size = None

            # СБРОС ПЕРЕМЕННЫХ ПРОМЕЖУТОЧНОГО УСРЕДНЕНИЯ
            self.intermediate_averaging_executed = False

            # СБРОС ПЕРЕМЕННЫХ ОСНОВНОГО УСРЕДНЕНИЯ (ОДИНОЧНОЕ УТРОЕНИЕ)
            self.averaging_executed = False
            self.averaging_count = 0  # Сброс счетчика усреднений
            self.initial_margin_usd = Decimal('0')
            self.current_total_margin = Decimal('0')  # Сброс текущей маржи
            self.total_fees_paid = Decimal('0')
            self.total_position_size = Decimal('0')
            self.average_entry_price = Decimal('0')

            # СБРОС ФЛАГА ДЕТЕКТОРА ЗАСТРЕВАНИЯ
            self.stagnation_averaging_executed = False

            # СБРОС ФЛАГА ВЫХОДА В БЕЗУБЫТОК
            self.use_breakeven_exit = False

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
            # МГНОВЕННО отправляем уведомление (используем сохраненные значения)
            await self._send_trade_close_notification(pnl_net, event.fee, exit_price=event.price, entry_price=saved_entry_price, entry_time=saved_entry_time)
            log_info(self.user_id, f"[УСПЕХ] Позиция {self.symbol} закрыта быстро! PnL: {pnl_net:.2f}$", "SignalScalper")

            # ПРОВЕРКА ОТЛОЖЕННОЙ ОСТАНОВКИ
            # Проверяем, должна ли стратегия быть остановлена после закрытия позиции
            await self.check_deferred_stop()
        else:
            log_warning(self.user_id, f"[НЕОЖИДАННО] Неожиданное состояние при обработке ордера {event.order_id}. position_active={self.position_active}, is_closing={is_closing_order}", "SignalScalper")

        # КРИТИЧНО: Удаляем исполненный ордер из активных ордеров
        if event.order_id in self.active_orders:
            del self.active_orders[event.order_id]
            log_debug(self.user_id, f"Исполненный ордер {event.order_id} удалён из active_orders", "SignalScalper")

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
            # ПРОВЕРЯЕМ: включен ли Stop Loss в конфигурации
            enable_sl = self.get_config_value("enable_stop_loss", True)
            if not enable_sl:
                log_info(self.user_id, "⏭️ Stop Loss отключен в настройках - пропускаю установку SL", "SignalScalper")
                return

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

        НОВАЯ ЛОГИКА: SL смещается ВНИЗ, давая позиции больше пространства для восстановления.
        Максимальный убыток рассчитывается от ОБЩЕЙ маржи (initial + все усреднения),
        что приводит к ПОНИЖЕНИЮ цены SL после каждого усреднения.

        Пример (LONG):
        - Первый вход: маржа $500, SL при убытке $250 (50%) → SL = $171.35
        - После усреднения: маржа $1000, SL при убытке $500 (50%) → SL опускается до ~$152
        """
        if not self.average_entry_price or not self.total_position_size:
            log_debug(self.user_id, "Пропуск обновления SL: нет данных об усреднении", "SignalScalper")
            return

        try:
            # Отменяем старый SL
            if self.stop_loss_order_id:
                await self._cancel_stop_loss_order()

            # НОВАЯ ЛОГИКА: Рассчитываем максимальный убыток от ОБЩЕЙ маржи (с усреднениями)
            # Это приведет к ПОНИЖЕНИЮ SL, давая позиции больше пространства
            max_loss_usd = self.current_total_margin * (self.averaging_stop_loss_percent / Decimal('100'))

            # Используем точный метод расчета SL от средней цены входа
            is_long = (self.active_direction == "LONG")
            new_sl_price = BaseStrategy._calculate_precise_stop_loss(
                self.average_entry_price,
                self.total_position_size,
                max_loss_usd,
                is_long
            )

            # Устанавливаем новый SL через Bybit API
            # ВАЖНО: position_idx=0 для One-Way Mode (не используем Hedge Mode)
            success = await self.api.set_trading_stop(
                symbol=self.symbol,
                stop_loss=str(new_sl_price),
                position_idx=0  # One-Way Mode
            )

            if success:
                self.stop_loss_price = new_sl_price
                log_info(self.user_id,
                        f"✅ SL смещен ВНИЗ после усреднения: средняя_цена=${self.average_entry_price:.4f}, "
                        f"новый_SL=${new_sl_price:.4f}, макс_убыток=${max_loss_usd:.2f} "
                        f"({self.averaging_stop_loss_percent}% от общей_маржи ${self.current_total_margin:.2f})",
                        "SignalScalper")
            else:
                log_warning(self.user_id, "Не удалось установить новый SL после усреднения", "SignalScalper")

        except Exception as e:
            log_error(self.user_id, f"Ошибка обновления SL после усреднения: {e}", "SignalScalper")

    def _is_signal_confirmed(self, signal: str) -> bool:
        """
        Проверяет, подтвержден ли сигнал достаточным количеством повторений.
        После убыточной сделки или реверса требует больше подтверждений.
        ВАЖНО: Пропускает первый сигнал, если он совпадает с направлением только что закрытой позиции.
        """
        if signal == self.last_signal:
            self.signal_confirmation_count += 1
        else:
            # Новый сигнал - сбрасываем счетчик
            self.last_signal = signal
            self.signal_confirmation_count = 1

            # ЛОГИКА ПРОПУСКА ПЕРВОГО ПОВТОРНОГО СИГНАЛА
            # Если новый сигнал совпадает с направлением только что закрытой позиции,
            # начинаем счётчик с 0 вместо 1 (требуем дополнительное подтверждение)
            if signal == self.last_closed_direction:
                log_info(self.user_id,
                        f"⏭️ Первый сигнал {signal} после закрытия {self.last_closed_direction} позиции - требуется дополнительное подтверждение",
                        "SignalScalper")
                self.signal_confirmation_count = 0

        # Определяем требуемое количество подтверждений
        required = self.required_confirmations

        # После убыточной сделки требуем больше подтверждений
        if self.last_trade_was_loss:
            required = max(required, 2)  # После убытка требуем минимум 2 подтверждения /временно сменил на 1

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
                # АКТИВИРУЕМ ВЫХОД В БЕЗУБЫТОК после усреднения
                self.use_breakeven_exit = True
                log_info(self.user_id, f"✅ Усреднение #{self.averaging_count} выполнено. Лимит: {self.averaging_count}/{self.max_averaging_count}", "SignalScalper")
                log_info(self.user_id, f"🎯 Режим выхода в безубыток АКТИВИРОВАН после усреднения", "SignalScalper")

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

        # Рассчитываем процент изменения цены вместо процента от маржи
        entry_price_to_use = self.average_entry_price if self.average_entry_price > 0 else self.entry_price

        # Получаем текущую цену из вебсокета (берем из последнего PriceUpdateEvent)
        # Для расчета используем entry_price_to_use и текущее значение PnL
        if entry_price_to_use > 0:
            # Обратный расчет текущей цены из PnL
            position_size_to_use = self.total_position_size if self.total_position_size > 0 else self.position_size

            if position_size_to_use > 0:
                # LONG: pnl = (current_price - entry_price) * position_size
                # => current_price = (pnl / position_size) + entry_price
                # SHORT: pnl = (entry_price - current_price) * position_size
                # => current_price = entry_price - (pnl / position_size)
                if self.active_direction == "LONG":
                    estimated_current_price = (current_pnl / position_size_to_use) + entry_price_to_use
                else:  # SHORT
                    estimated_current_price = entry_price_to_use - (current_pnl / position_size_to_use)

                # Рассчитываем процент изменения цены
                loss_percent = abs((estimated_current_price - entry_price_to_use) / entry_price_to_use) * Decimal('100')
            else:
                loss_percent = Decimal('0')
        else:
            loss_percent = Decimal('0')

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
            # Для логов показываем диапазоны как проценты изменения цены
            log_info(self.user_id,
                    f"🎯 Детектор стагнации АКТИВИРОВАН! PnL=${current_pnl:.2f} (изменение цены: {loss_percent:.1f}%) "
                    f"в диапазоне [{range_dict['min']:.1f}%-{range_dict['max']:.1f}% изменения цены]. "
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
            # Для логов показываем диапазоны как проценты изменения цены
            log_warning(self.user_id,
                       f"🚨 ТРИГГЕР ДЕТЕКТОРА СТАГНАЦИИ! PnL=${current_pnl:.2f} (изменение цены: {loss_percent:.1f}%) застрял в диапазоне "
                       f"[{range_dict['min']:.1f}%-{range_dict['max']:.1f}% изменения цены] на {elapsed_time:.0f} сек! "
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
            multiplier = self.stagnation_averaging_multiplier

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
                # АКТИВИРУЕМ ВЫХОД В БЕЗУБЫТОК после усреднения
                self.use_breakeven_exit = True
                # Сбрасываем мониторинг
                self._reset_stagnation_monitor()

                log_info(self.user_id,
                        f"✅ Усреднение по детектору стагнации выполнено",
                        "SignalScalper")
                log_info(self.user_id, f"🎯 Режим выхода в безубыток АКТИВИРОВАН после усреднения по стагнации", "SignalScalper")

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
            2: Decimal('0.0035'),   # 0.45% - РАННИЙ
            3: Decimal('0.0070'),   # 0.85% - СРЕДНИЙ
            4: Decimal('0.0115'),   # 1.30% - ХОРОШИЙ
            5: Decimal('0.0155'),   # 1.85% - ОТЛИЧНЫЙ
            6: Decimal('0.0225')    # 2.50% - МАКСИМАЛЬНЫЙ
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
            2: f"РАННИЙ УРОВЕНЬ (${levels[2]:.2f}+, 0.35%)",
            3: f"СРЕДНИЙ УРОВЕНЬ (${levels[3]:.2f}+, 0.70%)",
            4: f"ХОРОШИЙ УРОВЕНЬ (${levels[4]:.2f}+, 1.15%)",
            5: f"ОТЛИЧНЫЙ УРОВЕНЬ (${levels[5]:.2f}+, 1.55%)",
            6: f"МАКСИМАЛЬНЫЙ УРОВЕНЬ (${levels[6]:.2f}+, 2.25%)"
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

    async def get_detailed_status(self) -> Dict[str, Any]:
        """
        Получение детального статуса стратегии для отображения пользователю.

        Returns:
            Dict с детальной информацией о текущей позиции, усреднениях, PnL и т.д.
        """
        try:
            if not self.position_active:
                return {
                    "has_position": False,
                    "symbol": self.symbol,
                    "strategy_type": self.strategy_type.value
                }

            # Получаем текущую цену
            current_price = self._last_known_price if self._last_known_price else self.entry_price

            # Рассчитываем текущий PnL
            current_pnl = await self._calculate_current_pnl(current_price)

            # Определяем цену входа (средняя если было усреднение)
            effective_entry_price = self.average_entry_price if self.average_entry_price > 0 else self.entry_price

            # Рассчитываем процент изменения от цены входа
            if effective_entry_price and current_price:
                if self.active_direction == "LONG":
                    price_change_percent = ((current_price - effective_entry_price) / effective_entry_price) * Decimal('100')
                else:  # SHORT
                    price_change_percent = ((effective_entry_price - current_price) / effective_entry_price) * Decimal('100')
            else:
                price_change_percent = Decimal('0')

            # ПОЛУЧАЕМ ЦЕНУ БЕЗУБЫТКА С БИРЖИ (после усреднения)
            breakeven_price = None
            if self.averaging_count > 0 and self.average_entry_price > 0:
                try:
                    # Получаем ТОЧНУЮ цену безубыточности от биржи Bybit (breakEvenPrice)
                    # Она уже включает: среднюю цену входа + все комиссии + funding rate
                    positions = await self.api.get_positions(symbol=self.symbol)
                    if positions and len(positions) > 0:
                        breakeven_price_from_exchange = positions[0].get("breakEvenPrice", None)
                        if breakeven_price_from_exchange:
                            breakeven_price = self._convert_to_decimal(breakeven_price_from_exchange)
                except Exception as e:
                    log_warning(self.user_id, f"Не удалось получить breakEvenPrice с биржи: {e}", "SignalScalper")

            # Формируем детальный статус
            detailed_status = {
                "has_position": True,
                "symbol": self.symbol,
                "strategy_type": self.strategy_type.value,
                "account_priority": self.account_priority,

                # Основная информация о позиции
                "position": {
                    "direction": self.active_direction,
                    "entry_price": float(self.entry_price) if self.entry_price else None,
                    "current_price": float(current_price) if current_price else None,
                    "position_size": float(self.position_size) if self.position_size else 0,
                    "total_position_size": float(self.total_position_size) if self.total_position_size > 0 else float(self.position_size) if self.position_size else 0,
                },

                # Информация об усреднениях
                "averaging": {
                    "count": self.averaging_count,
                    "executed": self.averaging_executed or self.stagnation_averaging_executed,
                    "average_entry_price": float(self.average_entry_price) if self.average_entry_price > 0 else None,
                    "effective_entry_price": float(effective_entry_price) if effective_entry_price else None,
                    "breakeven_price": float(breakeven_price) if breakeven_price else None,
                    "use_breakeven_exit": self.use_breakeven_exit,
                },

                # Информация о марже
                "margin": {
                    "initial_margin": float(self.initial_margin_usd) if self.initial_margin_usd > 0 else 0,
                    "current_total_margin": float(self.current_total_margin) if self.current_total_margin > 0 else float(self.initial_margin_usd) if self.initial_margin_usd > 0 else 0,
                    "total_fees_paid": float(self.total_fees_paid) if self.total_fees_paid > 0 else 0,
                },

                # PnL информация
                "pnl": {
                    "unrealized_pnl": float(current_pnl) if current_pnl else 0,
                    "price_change_percent": float(price_change_percent),
                    "peak_profit": float(self.peak_profit_usd) if self.peak_profit_usd > 0 else 0,
                },

                # Информация о стоп-лоссе
                "stop_loss": {
                    "has_stop_loss": self.stop_loss_order_id is not None,
                    "stop_loss_price": float(self.stop_loss_price) if self.stop_loss_price else None,
                },
            }

            return detailed_status

        except Exception as e:
            log_error(self.user_id, f"Ошибка получения детального статуса: {e}", "SignalScalper")
            return {
                "has_position": False,
                "symbol": self.symbol,
                "strategy_type": self.strategy_type.value,
                "error": str(e)
            }

    async def _execute_strategy_logic(self):
        """Пустышка, так как логика теперь управляется событиями свечей."""
        pass
