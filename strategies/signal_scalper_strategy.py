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

        # Настраиваемые параметры
        self.min_profit_usd: Decimal = Decimal('1.0')
        self.trailing_pullback_usd: Decimal = Decimal('1.0')
        self.max_loss_usd: Decimal = Decimal('15.0')

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

        # НОВАЯ СИСТЕМА УСРЕДНЕНИЯ ПОЗИЦИИ
        self.averaging_enabled = False  # Включена ли система усреднения
        self.averaging_count = 0  # Текущее количество усреднений
        self.max_averaging_count = 3  # Максимальное количество усреднений
        self.averaging_trigger_percent = Decimal('1.0')  # Процент убытка для усреднения (1%)
        self.averaging_multiplier = Decimal('1.0')  # Размер усредняющего ордера
        self.last_averaging_percent = Decimal('0')  # Последний процент, при котором усреднялись
        self.total_position_size = Decimal('0')  # Общий размер позиции после усреднений
        self.average_entry_price = Decimal('0')  # Средняя цена входа после усреднений
        self.last_averaging_time = 0  # Время последнего усреднения для защиты от частых срабатываний

        # ИНТЕЛЛЕКТУАЛЬНАЯ СИСТЕМА УПРАВЛЕНИЯ SL ПРИ УСРЕДНЕНИИ
        self.original_max_loss_usd = None  # Будет инициализирован из конфигурации
        self.sl_extended = False  # Флаг: был ли SL расширен для усреднения
        self.sl_extension_notified = False  # Флаг: было ли отправлено уведомление о расширении SL

        # ИЗОЛЯЦИЯ НАСТРОЕК ДЛЯ АКТИВНОЙ СДЕЛКИ
        self.active_trade_config = None  # Конфигурация, зафиксированная при входе в сделку
        self.config_frozen = False  # Флаг: заморожены ли настройки для активной сделки


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
        """Переопределяем для инициализации SignalAnalyzer."""
        await super()._load_strategy_config()
        if self.config:
            self.signal_analyzer = SignalAnalyzer(self.user_id, self.api, self.config)
            self.min_profit_usd = self._convert_to_decimal(self.config.get("min_profit_usd", "1.0"))
            self.trailing_pullback_usd = self._convert_to_decimal(self.config.get("trailing_pullback_usd", "1.0"))
            self.max_loss_usd = self._convert_to_decimal(self.config.get("max_loss_usd", "15.0"))

            # Сохраняем изначальный лимит убытка для интеллектуального SL
            self.original_max_loss_usd = self.max_loss_usd

            # Загружаем параметры усреднения
            self.averaging_enabled = self.config.get("enable_averaging", True)
            self.averaging_trigger_percent = self._convert_to_decimal(self.config.get("averaging_trigger_percent", "1.0"))
            self.max_averaging_count = int(self.config.get("max_averaging_count", 3))
            self.averaging_multiplier = self._convert_to_decimal(self.config.get("averaging_multiplier", "1.0"))

            # НОВЫЕ параметры прогрессивного усреднения
            self.averaging_mode = self.config.get("averaging_mode", "fixed")  # "fixed" или "progressive"
            self.progressive_multiplier = self._convert_to_decimal(self.config.get("progressive_multiplier", "1.5"))

            # Параметры технических фильтров
            self.averaging_rsi_filter = self.config.get("averaging_rsi_filter", True)
            self.averaging_rsi_oversold = float(self.config.get("averaging_rsi_oversold", 60))
            self.averaging_rsi_overbought = float(self.config.get("averaging_rsi_overbought", 35))

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
        if event.symbol != self.symbol or self.is_waiting_for_trade:
            return

        log_debug(self.user_id, f"SignalScalper ({self.symbol}) получил новую свечу.", "SignalScalper")
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
            # Правило 4: Реверс позиции при смене сигнала (ТОЛЬКО при прибыли >= 0)
            if (signal == "LONG" and self.active_direction == "SHORT") or \
                    (signal == "SHORT" and self.active_direction == "LONG"):
                current_pnl = await self._calculate_current_pnl(price)
                if current_pnl >= 0:
                    log_warning(self.user_id,
                                f"СМЕНА СИГНАЛА! Реверс позиции по {self.symbol} с {self.active_direction} на {signal} при PnL={current_pnl:.2f}$.",
                                "SignalScalper")
                    await self._reverse_position(new_direction=signal)
                else:
                    log_info(self.user_id,
                            f"Смена сигнала на {signal}, но позиция в убытке {current_pnl:.2f}$. Ждем восстановления или SL.",
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
            loss_percent = ((entry_price_to_use - current_price) / entry_price_to_use * 100) if current_price < entry_price_to_use else 0
        else:  # SHORT
            pnl = (entry_price_to_use - current_price) * position_size_to_use
            loss_percent = ((current_price - entry_price_to_use) / entry_price_to_use * 100) if current_price > entry_price_to_use else 0

        # НОВАЯ ЛОГИКА УСРЕДНЕНИЯ
        if self.averaging_enabled:
            # Детальное логирование для диагностики с ценами
            entry_price_display = self.average_entry_price if self.average_entry_price > 0 else self.entry_price
            log_info(self.user_id, f"📊 Усреднение: pnl=${pnl:.2f}, loss%={loss_percent:.2f}, вход=${entry_price_display:.4f}, текущая=${current_price:.4f}, count={self.averaging_count}/{self.max_averaging_count}", "SignalScalper")

            if (pnl < 0 and  # Позиция в убытке
                self.averaging_count < self.max_averaging_count):

                # Проверяем, достиг ли убыток нового порога для усреднения
                next_trigger_percent = self.averaging_trigger_percent * (self.averaging_count + 1)

                log_info(self.user_id, f"🔍 Проверка триггера: loss={loss_percent:.2f}% >= trigger={next_trigger_percent:.1f}%, last={self.last_averaging_percent:.2f}%", "SignalScalper")

                if loss_percent >= next_trigger_percent and loss_percent > self.last_averaging_percent:
                    # ЗАЩИТА ОТ ЧАСТЫХ УСРЕДНЕНИЙ: проверяем временной интервал
                    current_time = time.time()
                    time_since_last_averaging = current_time - self.last_averaging_time
                    min_averaging_interval = 30  # Минимум 30 секунд между усреднениями

                    if self.last_averaging_time > 0 and time_since_last_averaging < min_averaging_interval:
                        remaining_time = min_averaging_interval - time_since_last_averaging
                        log_info(self.user_id, f"⏳ Усреднение пропущено: нужно подождать ещё {remaining_time:.0f} сек", "SignalScalper")
                        return

                    # Проверяем технические фильтры перед усреднением
                    filter_result = await self._check_averaging_filters()
                    if filter_result:
                        log_info(self.user_id,
                                f"🎯 ТРИГГЕР УСРЕДНЕНИЯ: убыток {loss_percent:.2f}% >= {next_trigger_percent:.1f}%",
                                "SignalScalper")
                        await self._execute_averaging(current_price)
                    else:
                        log_info(self.user_id, f"❌ Усреднение пропущено: не прошел технические фильтры", "SignalScalper")
                else:
                    log_info(self.user_id, f"⏸️ Триггер НЕ сработал: условие {loss_percent:.2f} >= {next_trigger_percent:.1f} and {loss_percent:.2f} > {self.last_averaging_percent:.2f}", "SignalScalper")
            else:
                if pnl >= 0:
                    log_info(self.user_id, f"✅ Усреднение пропущено: позиция в плюсе (${pnl:.2f})", "SignalScalper")
                if self.averaging_count >= self.max_averaging_count:
                    log_info(self.user_id, f"🚫 Усреднение пропущено: достигнут лимит ({self.averaging_count}/{self.max_averaging_count})", "SignalScalper")

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
        self.is_waiting_for_trade = True

        # Получаем актуальные данные ТОЛЬКО ОДИН РАЗ перед созданием ордера
        await self._force_config_reload()

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

        # Проверяем, что это ожидаемый ордер ИЛИ связанный с нашей позицией
        is_our_order = (
            # Ожидаемый ордер
            (self.current_order_id and event.order_id == self.current_order_id) or
            # Или ордер по нашему символу (для SL и других системных ордеров)
            (not self.current_order_id and hasattr(event, 'symbol') and event.symbol == self.symbol)
        )

        if not is_our_order:
            log_debug(self.user_id, f"[НЕ НАШ] Ордер {event.order_id} не относится к этой стратегии.", "SignalScalper")
            return

        # НЕМЕДЛЕННО добавляем ордер в обработанные чтобы блокировать повторную обработку
        self.processed_orders.add(event.order_id)
        self.current_order_id = None  # Сбрасываем ожидаемый ордер

        # КРИТИЧЕСКИ ВАЖНО: Обновляем статус ордера в БД как FILLED
        try:
            from database.db_trades import db_manager
            await db_manager.update_order_status(
                order_id=event.order_id,
                status="FILLED",
                filled_quantity=event.qty,
                average_price=event.price,
                filled_price=event.price
            )
            log_debug(self.user_id, f"Статус ордера {event.order_id} обновлён в БД: FILLED", "SignalScalper")
        except Exception as db_error:
            log_error(self.user_id, f"Ошибка обновления статуса ордера {event.order_id} в БД: {db_error}", "SignalScalper")

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
            # Ордер на открытие позиции
            log_info(self.user_id, f"[ОТКРЫТИЕ] Обрабатываем ордер открытия: {event.order_id}", "SignalScalper")
            self.position_active = True
            self.active_direction = "LONG" if event.side == "Buy" else "SHORT"
            self.entry_price = event.price
            self.position_size = event.qty
            self.peak_profit_usd = Decimal('0')
            self.hold_signal_counter = 0
            await self.event_bus.subscribe(EventType.PRICE_UPDATE, self.handle_price_update, user_id=self.user_id)
            await self._send_trade_open_notification(event.side, event.price, event.qty, self.intended_order_amount)

            # Инициализируем счетчики усреднения
            self.averaging_count = 0
            self.last_averaging_percent = Decimal('0')
            self.total_position_size = Decimal('0')
            self.average_entry_price = Decimal('0')

            # ВСЕГДА устанавливаем стоп-лосс для защиты (даже при усреднении)
            await self._place_stop_loss_order(self.active_direction, self.entry_price, self.position_size)

        elif is_averaging_order and self.position_active:
            # Ордер на усреднение позиции
            log_info(self.user_id, f"[УСРЕДНЕНИЕ] Обрабатываем ордер усреднения: {event.order_id}", "SignalScalper")

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

            # Сохраняем процент, при котором усреднялись
            if self.active_direction == "LONG":
                self.last_averaging_percent = ((self.average_entry_price - event.price) / self.average_entry_price * 100)
            else:  # SHORT
                self.last_averaging_percent = ((event.price - self.average_entry_price) / self.average_entry_price * 100)

            # Увеличиваем счетчик усреднений
            self.averaging_count += 1

            # ОБНОВЛЯЕМ ВРЕМЯ ПОСЛЕДНЕГО УСРЕДНЕНИЯ
            self.last_averaging_time = time.time()

            log_info(self.user_id,
                    f"[УСРЕДНЕНИЕ] Усреднение #{self.averaging_count} выполнено. Новая средняя цена: {self.average_entry_price:.4f}, размер: {self.total_position_size}",
                    "SignalScalper")

            # ДИНАМИЧЕСКАЯ КОРРЕКТИРОВКА СТОП-ЛОССА после усреднения
            await self._update_stop_loss_after_averaging()

            # Отправляем уведомление об усреднении
            await self._send_averaging_notification(
                event.price, event.qty, self.average_entry_price, self.total_position_size,
                side=event.side
            )

        elif is_closing_order and self.position_active:
            # Ордер на закрытие позиции
            log_info(self.user_id, f"[ЗАКРЫТИЕ] Обрабатываем ордер закрытия: {event.order_id}", "SignalScalper")

            # ИСПРАВЛЕННЫЙ РАСЧЕТ PnL с учетом усреднения
            entry_price_for_pnl = self.average_entry_price if self.average_entry_price > 0 else self.entry_price
            position_size_for_pnl = self.total_position_size if self.total_position_size > 0 else self.position_size

            pnl_gross = (event.price - entry_price_for_pnl) * position_size_for_pnl if self.active_direction == "LONG" else (
                entry_price_for_pnl - event.price) * position_size_for_pnl
            pnl_net = pnl_gross - event.fee

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

            # СБРОС ПЕРЕМЕННЫХ УСРЕДНЕНИЯ
            self.averaging_count = 0
            self.last_averaging_percent = Decimal('0')
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

    def _calculate_stop_loss_price(self, entry_price: Decimal, direction: str, position_size: Decimal) -> Decimal:
        """
        Рассчитывает цену стоп-лосса с учетом комиссий и буфера для точности.

        Формула:
        LONG: SL = entry_price - (adjusted_max_loss_usd / position_size)
        SHORT: SL = entry_price + (adjusted_max_loss_usd / position_size)
        """
        if position_size <= 0:
            log_error(self.user_id, "Невозможно рассчитать стоп-лосс: размер позиции равен нулю", "SignalScalper")
            return entry_price  # Возвращаем цену входа как fallback

        # Используем новый точный метод расчета
        is_long = (direction == "LONG")
        stop_loss_price = BaseStrategy._calculate_precise_stop_loss(entry_price, position_size, self.max_loss_usd, is_long)

        log_info(self.user_id,
                f"Точный расчет стоп-лосса для {direction}: вход=${entry_price:.4f}, SL=${stop_loss_price:.4f}, макс. убыток=${self.max_loss_usd:.2f}",
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
            required = max(required, 3)  # После убытка требуем минимум 3 подтверждения

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
        """Выполняет усреднение позиции."""
        if not self.averaging_enabled or self.averaging_count >= self.max_averaging_count:
            return

        try:
            self.is_waiting_for_trade = True

            # НЕ обновляем конфигурацию при усреднении - используем ЗАМОРОЖЕННЫЕ параметры текущей сделки

            # Рассчитываем размер усредняющего ордера (ПРОГРЕССИВНОЕ УСРЕДНЕНИЕ)
            # ВАЖНО: используем замороженную конфигурацию!
            order_amount = self._convert_to_decimal(self._get_frozen_config_value("order_amount", 50.0))

            if self.averaging_mode == "progressive":
                # Прогрессивное усреднение: каждый ордер больше предыдущего
                progressive_factor = self.progressive_multiplier ** self.averaging_count  # 1.5^1, 1.5^2, 1.5^3
                averaging_amount = order_amount * progressive_factor
                log_info(self.user_id, f"Прогрессивное усреднение #{self.averaging_count + 1}: {order_amount} × {progressive_factor:.2f} = {averaging_amount:.2f} USDT", "SignalScalper")
            else:
                # Фиксированное усреднение
                averaging_amount = order_amount * self.averaging_multiplier

            leverage = self._convert_to_decimal(self._get_frozen_config_value("leverage", 1.0))

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
                # Просто ждем исполнения ордера
                # Вся логика обновления статистики будет в _handle_order_filled()
                await self._await_order_fill(order_id, side=side, qty=qty)

            self.is_waiting_for_trade = False

        except Exception as e:
            log_error(self.user_id, f"Ошибка при усреднении: {e}", "SignalScalper")
            self.is_waiting_for_trade = False

    def _calculate_dynamic_min_profit(self) -> Decimal:
        """
        Рассчитывает персонализированный минимальный профит на основе:
        - Размера ордера пользователя (0.5% базовый)
        - Количества усреднений (+0.3% за каждое)
        """
        try:
            # Получаем свежие данные из конфигурации
            order_amount = self._convert_to_decimal(self.get_config_value("order_amount", 50.0))

            # Настройки для расчета (реалистичные проценты)
            base_profit_percent = Decimal('0.005')      # 0.5% от суммы ордера
            averaging_bonus_percent = Decimal('0.003')   # 0.3% за каждое усреднение

            # Базовый минимальный профит
            base_profit = order_amount * base_profit_percent

            # Бонус за усреднения
            averaging_bonus = (order_amount * averaging_bonus_percent) * self.averaging_count

            # Итоговый минимальный профит
            dynamic_min_profit = base_profit + averaging_bonus

            log_info(self.user_id,
                     f"💰 Динамический профит: {dynamic_min_profit:.2f}$ (базовый {base_profit:.2f}$ + усреднения {averaging_bonus:.2f}$)",
                     "SignalScalper")

            return dynamic_min_profit

        except Exception as e:
            log_error(self.user_id, f"Ошибка расчета динамического профита: {e}", "SignalScalper")
            # Возвращаем фиксированное значение как fallback
            return self.min_profit_usd

    async def _check_averaging_filters(self) -> bool:
        """Проверяет технические фильтры для усреднения."""
        if not self.averaging_rsi_filter:
            # Если фильтры отключены - разрешаем усреднение
            log_info(self.user_id, f"✅ RSI фильтры отключены - усреднение разрешено", "SignalScalper")
            return True

        try:
            # Получаем анализ для проверки RSI
            analysis_result = await self.signal_analyzer.get_analysis(self.symbol)
            if not analysis_result or not analysis_result.indicators:
                log_info(self.user_id, "✅ Не удалось получить RSI - усреднение разрешено", "SignalScalper")
                return True  # Если нет данных - разрешаем усреднение

            current_rsi = analysis_result.indicators.get('rsi', 50.0)

            # Используем пороги из конфигурации пользователя
            if self.active_direction == "LONG":
                # Для LONG: усредняемся если RSI <= oversold (цена упала, можно покупать)
                rsi_ok = current_rsi <= self.averaging_rsi_oversold
                log_info(self.user_id, f"🔍 RSI фильтр LONG: RSI={current_rsi:.1f} <= {self.averaging_rsi_oversold} = {rsi_ok}", "SignalScalper")
                return rsi_ok
            else:  # SHORT
                # Для SHORT: усредняемся если RSI >= overbought (цена выросла, можно продавать)
                rsi_ok = current_rsi >= self.averaging_rsi_overbought
                log_info(self.user_id, f"🔍 RSI фильтр SHORT: RSI={current_rsi:.1f} >= {self.averaging_rsi_overbought} = {rsi_ok}", "SignalScalper")
                return rsi_ok

        except Exception as e:
            log_error(self.user_id, f"Ошибка в фильтрах усреднения: {e}", "SignalScalper")
            return True  # В случае ошибки разрешаем усреднение


    def _calculate_dynamic_levels(self) -> Dict[int, Decimal]:
        """
        Рассчитывает динамические уровни трейлинга на основе параметров пользователя.

        Базовая логика:
        - Номинальная стоимость = order_amount × leverage
        - Уровни рассчитываются как проценты от номинальной стоимости

        Returns:
            Dict[int, Decimal]: Словарь с уровнями {уровень: минимальная_прибыль_USDT}
        """
        # Получаем параметры пользователя
        order_amount = max(self._convert_to_decimal(self.get_config_value("order_amount", 50.0)), Decimal('50.0'))
        leverage = self._convert_to_decimal(self.get_config_value("leverage", 1.0))

        # Номинальная стоимость позиции
        notional_value = order_amount * leverage

        # Проценты для каждого уровня от номинальной стоимости
        level_percentages = {
            1: Decimal('0.0025'),   # 0.25% - НАЧАЛЬНЫЙ УРОВЕНЬ
            2: Decimal('0.0053'),   # 0.53% - 1-Й УРОВЕНЬ
            3: Decimal('0.0103'),   # 1.03% - 2-Й УРОВЕНЬ
            4: Decimal('0.0142')    # 1.42% - 3-Й УРОВЕНЬ (максимальный)
        }

        # Рассчитываем пороги в USDT
        levels = {
            0: Decimal('0')  # Уровень 0 - трейлинг неактивен
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
            int: Уровень трейлинга (0-4)
        """
        levels = self._calculate_dynamic_levels()

        if current_pnl < levels[1]:
            return 0  # Не достигли минимального порога
        elif current_pnl < levels[2]:
            return 1  # Начальный уровень
        elif current_pnl < levels[3]:
            return 2  # 1-й уровень
        elif current_pnl < levels[4]:
            return 3  # 2-й уровень
        else:
            return 4  # 3-й уровень (максимальный)

    def _get_level_name(self, level: int) -> str:
        """Возвращает человекочитаемое название уровня с динамическими значениями."""
        if level == 0:
            return "ОЖИДАНИЕ"

        levels = self._calculate_dynamic_levels()

        level_names = {
            1: f"НАЧАЛЬНЫЙ УРОВЕНЬ (${levels[1]:.2f}+)",
            2: f"1-Й УРОВЕНЬ (${levels[2]:.2f}+)",
            3: f"2-Й УРОВЕНЬ (${levels[3]:.2f}+)",
            4: f"3-Й УРОВЕНЬ (${levels[4]:.2f}+)"
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

    def _calculate_required_space_for_averaging(self, current_price: Decimal) -> Decimal:
        """
        Рассчитывает необходимое пространство до SL для выполнения всех оставшихся усреднений.

        Returns:
            Decimal: Минимальная цена, до которой должен дойти актив для всех усреднений
        """
        if not self.averaging_enabled or self.averaging_count >= self.max_averaging_count:
            return current_price

        remaining_averagings = self.max_averaging_count - self.averaging_count
        if remaining_averagings <= 0:
            return current_price

        # Рассчитываем максимальный процент падения для оставшихся усреднений
        max_loss_percent = Decimal('0')
        for i in range(1, remaining_averagings + 1):
            next_averaging_level = self.averaging_count + i
            trigger_percent = self.averaging_trigger_percent * next_averaging_level
            max_loss_percent = max(max_loss_percent, trigger_percent)

        # Добавляем минимальный буфер 5% для безопасности
        max_loss_percent_with_buffer = max_loss_percent * Decimal('1.05')

        # Рассчитываем минимальную цену для всех усреднений
        entry_price_for_calc = self.average_entry_price if self.average_entry_price > 0 else self.entry_price

        if self.active_direction == "LONG":
            # Для LONG: цена может упасть на max_loss_percent_with_buffer%
            min_price_for_averaging = entry_price_for_calc * (Decimal('100') - max_loss_percent_with_buffer) / Decimal('100')
        else:  # SHORT
            # Для SHORT: цена может вырасти на max_loss_percent_with_buffer%
            min_price_for_averaging = entry_price_for_calc * (Decimal('100') + max_loss_percent_with_buffer) / Decimal('100')

        log_debug(self.user_id,
                f"Расчет пространства для усреднения: осталось {remaining_averagings} усреднений, "
                f"макс. падение {max_loss_percent_with_buffer:.1f}%, мин. цена {min_price_for_averaging:.4f}",
                "SignalScalper")

        return min_price_for_averaging

    def _calculate_smart_stop_loss(self, entry_price: Decimal, direction: str, position_size: Decimal) -> tuple[Decimal, bool, str]:
        """
        Интеллектуальный расчет стоп-лосса с учетом будущих усреднений.

        Returns:
            tuple: (новая_цена_SL, был_ли_расширен_SL, причина_изменения)
        """
        # Стандартный расчет SL
        standard_sl = BaseStrategy._calculate_precise_stop_loss(entry_price, position_size, self.original_max_loss_usd, direction == "LONG")

        # Если усреднение отключено или достигнут лимит усреднений, используем стандартный SL
        if not self.averaging_enabled or self.averaging_count >= self.max_averaging_count:
            return standard_sl, False, "standard_calculation"

        # ИСПРАВЛЕННАЯ ЛОГИКА: SL корректируется только при втором усреднении и далее
        # При первом усреднении (averaging_count = 0) НЕ корректируем SL
        if self.averaging_count == 0:
            log_info(self.user_id, f"🧠 УМНЫЙ SL: Первое усреднение - SL не корректируется", "SignalScalper")
            return standard_sl, False, "first_averaging_no_correction"

        # Проверяем, есть ли еще усреднения впереди
        next_averaging_level = self.averaging_count + 1
        if next_averaging_level > self.max_averaging_count:
            log_info(self.user_id, f"🧠 УМНЫЙ SL: Все усреднения завершены, используем стандартный SL", "SignalScalper")
            return standard_sl, False, "all_averagings_completed"

        # Рассчитываем цену для СЛЕДУЮЩЕГО усреднения (не всех сразу)
        next_trigger_percent = self.averaging_trigger_percent * next_averaging_level
        entry_price_for_calc = self.average_entry_price if self.average_entry_price > 0 else entry_price

        if direction == "LONG":
            # Цена для следующего усреднения = текущая_средняя_цена * (1 - процент_падения/100)
            next_averaging_price = entry_price_for_calc * (Decimal('100') - next_trigger_percent) / Decimal('100')
        else:  # SHORT
            # Цена для следующего усреднения = текущая_средняя_цена * (1 + процент_роста/100)
            next_averaging_price = entry_price_for_calc * (Decimal('100') + next_trigger_percent) / Decimal('100')

        # Проверяем, блокирует ли стандартный SL СЛЕДУЮЩЕЕ усреднение
        sl_threatens_next_averaging = False
        threat_reason = ""

        # Добавляем небольшой буфер (0.5%) для безопасности
        safety_buffer = next_averaging_price * Decimal('0.005')

        if direction == "LONG":
            # Для LONG: SL должен быть ниже цены следующего усреднения с буфером
            safe_averaging_price = next_averaging_price - safety_buffer
            if standard_sl > safe_averaging_price:
                sl_threatens_next_averaging = True
                threat_reason = f"SL {standard_sl:.4f} блокирует усреднение #{next_averaging_level} на уровне {next_averaging_price:.4f}"
        else:  # SHORT
            # Для SHORT: SL должен быть выше цены следующего усреднения с буфером
            safe_averaging_price = next_averaging_price + safety_buffer
            if standard_sl < safe_averaging_price:
                sl_threatens_next_averaging = True
                threat_reason = f"SL {standard_sl:.4f} блокирует усреднение #{next_averaging_level} на уровне {next_averaging_price:.4f}"

        if not sl_threatens_next_averaging:
            return standard_sl, False, f"sufficient_space_for_next_averaging_#{next_averaging_level}"

        # МИНИМАЛЬНОЕ расширение SL для обеспечения места под СЛЕДУЮЩЕЕ усреднение
        extended_buffer = next_averaging_price * Decimal('0.008')  # 0.8% буфер для надежности

        if direction == "LONG":
            # Для LONG: опускаем SL ниже цены следующего усреднения
            extended_sl = next_averaging_price - extended_buffer
        else:  # SHORT
            # Для SHORT: поднимаем SL выше цены следующего усреднения
            extended_sl = next_averaging_price + extended_buffer

        # Рассчитываем новый максимальный убыток
        position_size_for_calc = self.total_position_size if self.total_position_size > 0 else position_size

        if direction == "LONG":
            new_max_loss = (entry_price - extended_sl) * position_size_for_calc
        else:  # SHORT
            new_max_loss = (extended_sl - entry_price) * position_size_for_calc

        extension_reason = f"SL расширен для усреднения: {threat_reason}, новый макс. убыток ${new_max_loss:.2f}"

        log_info(self.user_id,
                f"🧠 УМНЫЙ SL: {extension_reason}",
                "SignalScalper")

        return extended_sl, True, extension_reason

    async def _update_stop_loss_after_averaging(self):
        """
        УСОВЕРШЕНСТВОВАННАЯ версия: Обновляет стоп-лосс после усреднения позиции
        с интеллектуальным управлением для будущих усреднений.
        """
        try:
            if not self.position_active or self.average_entry_price <= 0:
                log_warning(self.user_id, "Не удалось обновить стоп-лосс: позиция неактивна или нет средней цены", "SignalScalper")
                return

            # УЛУЧШЕННАЯ ЛОГИКА: Используем умный расчёт SL с учетом будущих усреднений
            new_stop_loss_price, was_extended, extension_reason = self._calculate_smart_stop_loss(
                self.average_entry_price,
                self.active_direction,
                self.total_position_size
            )

            if was_extended:
                log_info(self.user_id, f"🧠 УМНОЕ обновление SL: {new_stop_loss_price:.4f} (расширен для усреднений)", "SignalScalper")
            else:
                log_info(self.user_id, f"🔧 Стандартное обновление SL: {new_stop_loss_price:.4f} (достаточно места)", "SignalScalper")

            # Обновляем SL через API
            success = await self.api.set_trading_stop(
                symbol=self.symbol,
                stop_loss=new_stop_loss_price
            )

            if success:
                old_stop_loss = self.stop_loss_price
                self.stop_loss_price = new_stop_loss_price

                # Отправляем уведомление о расширении SL (если было расширение)
                if was_extended and not self.sl_extension_notified:
                    await self._send_sl_extension_notification(old_stop_loss, new_stop_loss_price)
                    self.sl_extended = True
                    self.sl_extension_notified = True

                log_info(self.user_id,
                        f"✅ Стоп-лосс обновлен после усреднения: {old_stop_loss:.4f} → {new_stop_loss_price:.4f} "
                        f"({'РАСШИРЕН' if was_extended else 'стандартный'})",
                        "SignalScalper")
            else:
                log_error(self.user_id, "Не удалось обновить стоп-лосс после усреднения", "SignalScalper")

        except Exception as e:
            log_error(self.user_id, f"Ошибка при обновлении стоп-лосса после усреднения: {e}", "SignalScalper")

    async def _send_sl_extension_notification(self, old_sl: Decimal, new_sl: Decimal):
        """Отправляет уведомление пользователю о расширении SL."""
        try:
            # Рассчитываем новый максимальный убыток
            position_size_for_calc = self.total_position_size if self.total_position_size > 0 else self.position_size
            entry_price_for_calc = self.average_entry_price if self.average_entry_price > 0 else self.entry_price

            if self.active_direction == "LONG":
                new_max_loss = (entry_price_for_calc - new_sl) * position_size_for_calc
            else:  # SHORT
                new_max_loss = (new_sl - entry_price_for_calc) * position_size_for_calc

            extension_percent = abs(new_max_loss - self.original_max_loss_usd) / self.original_max_loss_usd * 100

            notification_text = (
                f"⚠️ <b>АВТОКОРРЕКЦИЯ STOP LOSS</b> ⚠️\n\n"
                f"▫️ Стратегия: Signal Scalper\n"
                f"▫️ Символ: {self.symbol}\n"
                f"▫️ Усреднение: #{self.averaging_count}/{self.max_averaging_count}\n\n"
                f"🔄 <b>Изменения SL:</b>\n"
                f"▫️ Старый SL: {old_sl:.4f} USDT\n"
                f"▫️ Новый SL: {new_sl:.4f} USDT\n"
                f"▫️ Макс. убыток: <b>{new_max_loss:.2f} USDT</b> "
                f"(+{extension_percent:.1f}% от лимита)\n\n"
                f"📋 <b>Причина:</b>\n"
                f"Система автоматически расширила SL, чтобы обеспечить место для оставшихся усреднений. "
                f"Это защищает от преждевременного срабатывания SL и позволяет завершить стратегию усреднения.\n\n"
                f"✅ SL будет восстановлен к изначальному лимиту после завершения всех усреднений."
            )

            if self.bot:
                await self.bot.send_message(self.user_id, notification_text, parse_mode="HTML")

        except Exception as e:
            log_error(self.user_id, f"Ошибка отправки уведомления о расширении SL: {e}", "SignalScalper")

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

            # Проверяем, была ли активна позиция на момент сохранения
            if hasattr(self, 'position_active') and self.position_active:
                log_info(self.user_id, f"🎯 Восстанавливаю активную позицию SignalScalper", "SignalScalper")

                # Восстанавливаем подписки на события цен (критически важно для мониторинга)
                if not hasattr(self, '_price_subscription_restored'):
                    await self.event_bus.subscribe(EventType.PRICE_UPDATE, self.handle_price_update, user_id=self.user_id)
                    log_info(self.user_id, f"✅ Восстановлена подписка на обновления цен для {self.symbol}", "SignalScalper")
                    self._price_subscription_restored = True

                # Если есть состояние усреднения - восстанавливаем его детально
                if hasattr(self, 'averaging_count') and self.averaging_count > 0:
                    log_info(self.user_id,
                            f"📊 Восстанавливаю состояние усреднения: #{self.averaging_count}/{getattr(self, 'max_averaging_count', 3)}, "
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