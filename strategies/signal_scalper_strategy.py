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

        # НОВАЯ СИСТЕМА УСРЕДНЕНИЯ ПОЗИЦИИ
        self.averaging_enabled = False  # Включена ли система усреднения
        self.averaging_count = 0  # Текущее количество усреднений
        self.max_averaging_count = 3  # Максимальное количество усреднений
        self.averaging_trigger_percent = Decimal('1.0')  # Процент убытка для усреднения (1%)
        self.averaging_multiplier = Decimal('1.0')  # Размер усредняющего ордера
        self.last_averaging_percent = Decimal('0')  # Последний процент, при котором усреднялись
        self.total_position_size = Decimal('0')  # Общий размер позиции после усреднений
        self.average_entry_price = Decimal('0')  # Средняя цена входа после усреднений


    def _get_strategy_type(self) -> StrategyType:
        return StrategyType.SIGNAL_SCALPER

    async def _load_strategy_config(self):
        """Переопределяем для инициализации SignalAnalyzer."""
        await super()._load_strategy_config()
        if self.config:
            self.signal_analyzer = SignalAnalyzer(self.user_id, self.api, self.config)
            self.min_profit_usd = self._convert_to_decimal(self.config.get("min_profit_usd", "1.0"))
            self.trailing_pullback_usd = self._convert_to_decimal(self.config.get("trailing_pullback_usd", "1.0"))
            self.max_loss_usd = self._convert_to_decimal(self.config.get("max_loss_usd", "15.0"))

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
            self.averaging_rsi_oversold = float(self.config.get("averaging_rsi_oversold", 25))
            self.averaging_rsi_overbought = float(self.config.get("averaging_rsi_overbought", 75))

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

        # Принудительная перезагрузка конфигурации перед анализом сигнала
        await self._force_config_reload()

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
            # Правило 4: Реверс позиции при смене сигнала
            if (signal == "LONG" and self.active_direction == "SHORT") or \
                    (signal == "SHORT" and self.active_direction == "LONG"):
                log_warning(self.user_id,
                            f"СМЕНА СИГНАЛА! Реверс позиции по {self.symbol} с {self.active_direction} на {signal}.",
                            "SignalScalper")
                await self._reverse_position(new_direction=signal)

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
                # Проверка кулдауна
                if self._is_cooldown_active():
                    return

                # Проверка подтверждения сигнала
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
            # Детальное логирование для диагностики
            log_debug(self.user_id, f"Усреднение: enabled={self.averaging_enabled}, pnl={pnl:.2f}, loss%={loss_percent:.2f}, count={self.averaging_count}/{self.max_averaging_count}", "SignalScalper")

            if (pnl < 0 and  # Позиция в убытке
                self.averaging_count < self.max_averaging_count):

                # Проверяем, достиг ли убыток нового порога для усреднения
                next_trigger_percent = self.averaging_trigger_percent * (self.averaging_count + 1)

                log_debug(self.user_id, f"Проверка триггера: loss={loss_percent:.2f}% >= trigger={next_trigger_percent:.1f}%, last={self.last_averaging_percent:.2f}%", "SignalScalper")

                if loss_percent >= next_trigger_percent and loss_percent > self.last_averaging_percent:
                    # Проверяем технические фильтры перед усреднением
                    if await self._check_averaging_filters():
                        log_info(self.user_id,
                                f"🎯 ТРИГГЕР УСРЕДНЕНИЯ: убыток {loss_percent:.2f}% >= {next_trigger_percent:.1f}%",
                                "SignalScalper")
                        await self._execute_averaging(current_price)
                    else:
                        log_debug(self.user_id, f"Усреднение пропущено: не прошел технические фильтры", "SignalScalper")
                else:
                    log_debug(self.user_id, f"Триггер НЕ сработал: условие {loss_percent:.2f} >= {next_trigger_percent:.1f} and {loss_percent:.2f} > {self.last_averaging_percent:.2f}", "SignalScalper")
            else:
                if pnl >= 0:
                    log_debug(self.user_id, f"Усреднение пропущено: позиция в плюсе ({pnl:.2f})", "SignalScalper")
                if self.averaging_count >= self.max_averaging_count:
                    log_debug(self.user_id, f"Усреднение пропущено: достигнут лимит ({self.averaging_count}/{self.max_averaging_count})", "SignalScalper")

        # Обновляем пиковую прибыль
        if pnl > self.peak_profit_usd:
            self.peak_profit_usd = pnl

        # НОВАЯ СИСТЕМА: Поэтапный трейлинг с фиксированными порогами и 20% откатом
        current_trailing_level = SignalScalperStrategy._get_trailing_level(pnl)

        if current_trailing_level > 0:  # Если достигли хотя бы начального уровня
            # Фиксированный 20% откат от пика на всех уровнях
            trailing_distance = self.peak_profit_usd * Decimal('0.20')

            # Проверяем условие закрытия: откат от пика >= 20%
            if pnl < (self.peak_profit_usd - trailing_distance):
                level_name = SignalScalperStrategy._get_level_name(current_trailing_level)
                log_info(self.user_id,
                         f"💎 ЗАКРЫТИЕ НА {level_name}! Пик: ${self.peak_profit_usd:.2f}, PnL: ${pnl:.2f}, откат: ${trailing_distance:.2f} (20%)",
                         "SignalScalper")
                await self._close_position("level_trailing_profit")
            else:
                # Логируем текущий статус трейлинга
                level_name = SignalScalperStrategy._get_level_name(current_trailing_level)
                log_debug(self.user_id,
                         f"Трейлинг {level_name}: пик=${self.peak_profit_usd:.2f}, PnL=${pnl:.2f}, откат допустим=${trailing_distance:.2f}",
                         "SignalScalper")

    async def _enter_position(self, direction: str, signal_price: Decimal):
        """Логика входа в позицию."""
        self.is_waiting_for_trade = True
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
        """Закрывает текущую позицию и немедленно открывает противоположную."""
        # Сначала закрываем текущую
        await self._close_position(reason=f"reversing_to_{new_direction}")

        # Небольшая пауза, чтобы биржа обработала закрытие
        await asyncio.sleep(2)

        # Открываем новую
        # Для получения актуальной цены нужен новый анализ, но для скорости используем последнюю цену из PriceUpdate
        last_price = await self.api.get_current_price(self.symbol)
        if last_price:
            await self._enter_position(direction=new_direction, signal_price=last_price)

    async def _handle_order_filled(self, event: OrderFilledEvent):
        """Обработка исполненных ордеров."""
        # Защита от двойной обработки одного и того же ордера
        if event.order_id in self.processed_orders:
            log_debug(self.user_id, f"Ордер {event.order_id} уже был обработан, пропускаем.", "SignalScalper")
            return

        # Проверяем, что это ожидаемый ордер
        if self.current_order_id and event.order_id != self.current_order_id:
            log_warning(self.user_id, f"Получен неожиданный ордер {event.order_id}, ожидался {self.current_order_id}", "SignalScalper")
            return

        # Добавляем ордер в обработанные
        self.processed_orders.add(event.order_id)
        self.current_order_id = None  # Сбрасываем ожидаемый ордер

        # Определяем тип ордера по reduce_only флагу
        is_closing_order = hasattr(event, 'reduce_only') and event.reduce_only

        # Или определяем по текущему состоянию позиции и направлению ордера
        if not is_closing_order:
            # Если позиция активна и ордер в противоположном направлении - это закрытие
            if self.position_active:
                opposite_side = "Sell" if self.active_direction == "LONG" else "Buy"
                is_closing_order = (event.side == opposite_side)

        if not is_closing_order and not self.position_active:
            # Ордер на открытие позиции
            log_info(self.user_id, f"Обрабатываем ордер открытия: {event.order_id}", "SignalScalper")
            self.position_active = True
            self.active_direction = "LONG" if event.side == "Buy" else "SHORT"
            self.entry_price = event.price
            self.position_size = event.qty
            self.peak_profit_usd = Decimal('0')
            self.hold_signal_counter = 0
            await self.event_bus.subscribe(EventType.PRICE_UPDATE, self._handle_price_update, user_id=self.user_id)
            await self._send_trade_open_notification(event.side, event.price, event.qty, self.intended_order_amount)

            # Инициализируем счетчики усреднения
            self.averaging_count = 0
            self.last_averaging_percent = Decimal('0')
            self.total_position_size = Decimal('0')
            self.average_entry_price = Decimal('0')

            # ВСЕГДА устанавливаем стоп-лосс для защиты (даже при усреднении)
            await self._place_stop_loss_order(self.active_direction, self.entry_price, self.position_size)

        elif is_closing_order and self.position_active:
            # Ордер на закрытие позиции
            log_info(self.user_id, f"Обрабатываем ордер закрытия: {event.order_id}", "SignalScalper")

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

            # Отменяем стоп-лосс перед сбросом состояния
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

            await self.event_bus.unsubscribe(self._handle_price_update)
            await self._send_trade_close_notification(pnl_net, event.fee, exit_price=event.price)
        else:
            log_warning(self.user_id, f"Неожиданное состояние при обработке ордера {event.order_id}. position_active={self.position_active}, is_closing={is_closing_order}", "SignalScalper")

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
        После убыточной сделки требует больше подтверждений.
        """
        if signal == self.last_signal:
            self.signal_confirmation_count += 1
        else:
            # Новый сигнал - сбрасываем счетчик
            self.last_signal = signal
            self.signal_confirmation_count = 1

        # После убыточной сделки требуем больше подтверждений
        required = self.required_confirmations
        if self.last_trade_was_loss:
            required = 3  # После убытка требуем 3 подтверждения

        confirmed = self.signal_confirmation_count >= required

        if confirmed:
            log_info(self.user_id,
                    f"Сигнал {signal} подтвержден! ({self.signal_confirmation_count}/{required})",
                    "SignalScalper")
        else:
            log_info(self.user_id,
                    f"Сигнал {signal} ожидает подтверждения ({self.signal_confirmation_count}/{required})",
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

    async def _execute_averaging(self, current_price: Decimal):
        """Выполняет усреднение позиции."""
        if not self.averaging_enabled or self.averaging_count >= self.max_averaging_count:
            return

        try:
            self.is_waiting_for_trade = True

            # Рассчитываем размер усредняющего ордера (ПРОГРЕССИВНОЕ УСРЕДНЕНИЕ)
            order_amount = self._convert_to_decimal(self.get_config_value("order_amount", 50.0))

            if self.averaging_mode == "progressive":
                # Прогрессивное усреднение: каждый ордер больше предыдущего
                progressive_factor = self.progressive_multiplier ** self.averaging_count  # 1.5^1, 1.5^2, 1.5^3
                averaging_amount = order_amount * progressive_factor
                log_info(self.user_id, f"Прогрессивное усреднение #{self.averaging_count + 1}: {order_amount} × {progressive_factor:.2f} = {averaging_amount:.2f} USDT", "SignalScalper")
            else:
                # Фиксированное усреднение
                averaging_amount = order_amount * self.averaging_multiplier

            leverage = self._convert_to_decimal(self.get_config_value("leverage", 1.0))

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
                success = await self._await_order_fill(order_id, side=side, qty=qty)

                if success:
                    # Обновляем статистику усреднения
                    self.averaging_count += 1

                    # Обновляем размер позиции и среднюю цену
                    if self.total_position_size == 0:
                        # Первое усреднение - инициализируем
                        self.total_position_size = self.position_size + qty
                        self.average_entry_price = ((self.entry_price * self.position_size) + (current_price * qty)) / self.total_position_size
                    else:
                        # Последующие усреднения
                        old_total_value = self.average_entry_price * self.total_position_size
                        new_value = current_price * qty
                        self.total_position_size += qty
                        self.average_entry_price = (old_total_value + new_value) / self.total_position_size

                    # Сохраняем процент, при котором усреднялись
                    entry_price_for_calc = self.average_entry_price if self.average_entry_price > 0 else self.entry_price
                    if self.active_direction == "LONG":
                        self.last_averaging_percent = ((entry_price_for_calc - current_price) / entry_price_for_calc * 100)
                    else:  # SHORT
                        self.last_averaging_percent = ((current_price - entry_price_for_calc) / entry_price_for_calc * 100)

                    log_info(self.user_id,
                            f"Усреднение #{self.averaging_count} выполнено. Новая средняя цена: {self.average_entry_price:.4f}, размер: {self.total_position_size}",
                            "SignalScalper")

                    # ДИНАМИЧЕСКАЯ КОРРЕКТИРОВКА СТОП-ЛОССА после усреднения
                    await self._update_stop_loss_after_averaging()

                    # Отправляем уведомление об усреднении
                    await self._send_averaging_notification(
                        current_price, qty, self.average_entry_price, self.total_position_size,
                        side="Buy" if self.active_direction == "LONG" else "Sell"
                    )

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
            return True

        try:
            # Получаем анализ для проверки RSI
            analysis_result = await self.signal_analyzer.get_analysis(self.symbol)
            if not analysis_result or not analysis_result.indicators:
                log_debug(self.user_id, "Не удалось получить индикаторы для фильтров усреднения", "SignalScalper")
                return True  # Если нет данных - разрешаем усреднение

            current_rsi = analysis_result.indicators.get('rsi', 50.0)

            # Применяем RSI фильтры в зависимости от направления позиции
            if self.active_direction == "LONG":
                # Для LONG: усредняемся только в зоне перепроданности (RSI < 25)
                rsi_ok = current_rsi <= self.averaging_rsi_oversold
                log_debug(self.user_id, f"RSI фильтр LONG: RSI={current_rsi:.1f} <= {self.averaging_rsi_oversold} = {rsi_ok}", "SignalScalper")
                return rsi_ok
            else:  # SHORT
                # Для SHORT: усредняемся только в зоне перекупленности (RSI > 75)
                rsi_ok = current_rsi >= self.averaging_rsi_overbought
                log_debug(self.user_id, f"RSI фильтр SHORT: RSI={current_rsi:.1f} >= {self.averaging_rsi_overbought} = {rsi_ok}", "SignalScalper")
                return rsi_ok

        except Exception as e:
            log_error(self.user_id, f"Ошибка в фильтрах усреднения: {e}", "SignalScalper")
            return True  # В случае ошибки разрешаем усреднение

    async def _update_stop_loss_after_averaging(self):
        """
        Обновляет стоп-лосс после усреднения позиции.
        Пересчитывает стоп-лосс на основе новой средней цены входа.
        """
        try:
            if not self.position_active or self.average_entry_price <= 0:
                log_warning(self.user_id, "Не удалось обновить стоп-лосс: позиция неактивна или нет средней цены", "SignalScalper")
                return

            # Рассчитываем новую цену стоп-лосса на основе средней цены входа
            new_stop_loss_price = self._calculate_stop_loss_price(
                self.average_entry_price,
                self.active_direction,
                self.total_position_size
            )

            # Обновляем стоп-лосс через API
            success = await self.api.set_trading_stop(
                symbol=self.symbol,
                stop_loss=new_stop_loss_price
            )

            if success:
                old_stop_loss = self.stop_loss_price
                self.stop_loss_price = new_stop_loss_price
                log_info(self.user_id,
                        f"✅ Стоп-лосс обновлен после усреднения: {old_stop_loss:.4f} → {new_stop_loss_price:.4f}",
                        "SignalScalper")
            else:
                log_error(self.user_id, "Не удалось обновить стоп-лосс после усреднения", "SignalScalper")

        except Exception as e:
            log_error(self.user_id, f"Ошибка при обновлении стоп-лосса после усреднения: {e}", "SignalScalper")

    @staticmethod
    def _get_trailing_level(current_pnl: Decimal) -> int:
        """
        Определяет текущий уровень трейлинга на основе прибыли.

        Возвращает:
        0 - Начальный (до 1.5$)
        1 - 1-й уровень (1.5$ - 3.2$)
        2 - 2-й уровень (3.2$ - 6.2$)
        3 - 3-й уровень (6.2$ - 8.5$)
        4 - Максимальный уровень (8.5$+)
        """
        if current_pnl < Decimal('1.5'):
            return 0  # Не достигли минимального порога
        elif current_pnl < Decimal('3.2'):
            return 1  # Начальный уровень
        elif current_pnl < Decimal('6.2'):
            return 2  # 1-й уровень
        elif current_pnl < Decimal('8.5'):
            return 3  # 2-й уровень
        else:
            return 4  # 3-й уровень (максимальный)

    @staticmethod
    def _get_level_name(level: int) -> str:
        """Возвращает человекочитаемое название уровня."""
        level_names = {
            0: "ОЖИДАНИЕ",
            1: "НАЧАЛЬНЫЙ УРОВЕНЬ (1.5$+)",
            2: "1-Й УРОВЕНЬ (3.2$+)",
            3: "2-Й УРОВЕНЬ (6.2$+)",
            4: "3-Й УРОВЕНЬ (8.5$+)"
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