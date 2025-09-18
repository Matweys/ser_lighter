import asyncio
from typing import Dict, Any, Optional
from decimal import Decimal, getcontext

from api.bybit_api import BybitAPI
from .base_strategy import BaseStrategy
from core.enums import StrategyType
from core.logger import log_info, log_error, log_warning
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

        # Настраиваемые параметры
        self.min_profit_usd: Decimal = Decimal('1.0')
        self.trailing_pullback_usd: Decimal = Decimal('1.0')

    def _get_strategy_type(self) -> StrategyType:
        return StrategyType.SIGNAL_SCALPER

    async def _load_strategy_config(self):
        """Переопределяем для инициализации SignalAnalyzer."""
        await super()._load_strategy_config()
        if self.config:
            self.signal_analyzer = SignalAnalyzer(self.user_id, self.api, self.config)
            self.min_profit_usd = self._convert_to_decimal(self.config.get("min_profit_usd", "1.0"))
            self.trailing_pullback_usd = self._convert_to_decimal(self.config.get("trailing_pullback_usd", "1.0"))

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
            # Правило 4: Реверс позиции при смене сигнала
            if (signal == "LONG" and self.active_direction == "SHORT") or \
                    (signal == "SHORT" and self.active_direction == "LONG"):
                log_warning(self.user_id,
                            f"СМЕНА СИГНАЛА! Реверс позиции по {self.symbol} с {self.active_direction} на {signal}.",
                            "SignalScalper")
                await self._reverse_position(new_direction=signal)

            # Правило 5: Закрытие при двух "HOLD" подряд
            elif signal == "HOLD":
                self.hold_signal_counter += 1
                if self.hold_signal_counter >= 2:
                    log_warning(self.user_id,
                                f"Два сигнала 'HOLD' подряд. Закрытие позиции {self.symbol} по безубытку.",
                                "SignalScalper")
                    await self._close_position("double_hold_signal")

            # Сбрасываем счетчик HOLD, если сигнал изменился
            else:
                self.hold_signal_counter = 0
        else:
            # Правило 1: Вход в новую сделку
            if signal in ["LONG", "SHORT"]:
                # Правило 1.1: Пропуск сигнала для "успокоения" рынка
                if signal == self.last_closed_direction:
                    log_info(self.user_id,
                             f"Пропуск сигнала {signal} для {self.symbol} (совпадает с последней закрытой сделкой).",
                             "SignalScalper")
                    self.last_closed_direction = None  # Сбрасываем, чтобы следующий сигнал вошел
                else:
                    await self._enter_position(direction=signal, signal_price=price)

    async def _handle_price_update(self, event: PriceUpdateEvent):
        """Обработка тиков цены для динамического тейк-профита."""
        if not self.position_active or not self.entry_price or self.is_waiting_for_trade:
            return

        current_price = event.price
        pnl = (current_price - self.entry_price) * self.position_size if self.active_direction == "LONG" else (
                                                                                                                          self.entry_price - current_price) * self.position_size

        # Обновляем пиковую прибыль
        if pnl > self.peak_profit_usd:
            self.peak_profit_usd = pnl

        # Правило 2: Закрытие по минимальной прибыли
        if pnl >= self.min_profit_usd:
            # Продвинутая логика трейлинга
            if pnl < (self.peak_profit_usd - self.trailing_pullback_usd):
                log_info(self.user_id,
                         f"Трейлинг-профит! Закрытие {self.symbol}. Пик: ${self.peak_profit_usd:.2f}, Текущий PnL: ${pnl:.2f}",
                         "SignalScalper")
                await self._close_position("trailing_profit_stop")
            else:
                # Просто ждем дальше, если откат небольшой
                pass

    async def _enter_position(self, direction: str, signal_price: Decimal):
        """Логика входа в позицию."""
        self.is_waiting_for_trade = True
        await self._set_leverage()
        order_amount = self._convert_to_decimal(self.get_config_value("order_amount", 50.0))
        leverage = self._convert_to_decimal(self.get_config_value("leverage", 1.0))
        qty = await self.api.calculate_quantity_from_usdt(self.symbol, order_amount, leverage, price=signal_price)

        if qty <= 0:
            await self.stop("Calculated order quantity is zero")
            self.is_waiting_for_trade = False
            return

        side = "Buy" if direction == "LONG" else "Sell"
        order_id = await self._place_order(side=side, order_type="Market", qty=qty)

        if order_id:
            await self._await_order_fill(order_id, side=side, qty=qty)
        else:
            self.is_waiting_for_trade = False

    async def _close_position(self, reason: str):
        """Логика закрытия текущей позиции."""
        if not self.position_active:
            return

        self.is_waiting_for_trade = True
        side = "Sell" if self.active_direction == "LONG" else "Buy"
        order_id = await self._place_order(side=side, order_type="Market", qty=self.position_size, reduce_only=True)

        if order_id:
            await self._await_order_fill(order_id, side=side, qty=self.position_size)
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
        # Ордер на открытие/реверс
        if not self.position_active:
            self.position_active = True
            self.active_direction = "LONG" if event.side == "Buy" else "SHORT"
            self.entry_price = event.price
            self.position_size = event.qty
            self.peak_profit_usd = Decimal('0')
            self.hold_signal_counter = 0
            await self.event_bus.subscribe(EventType.PRICE_UPDATE, self._handle_price_update, user_id=self.user_id)
            await self._send_trade_open_notification(event.side, event.price, event.qty)

        # Ордер на закрытие
        else:
            pnl_gross = (event.price - self.entry_price) * self.position_size if self.active_direction == "LONG" else (
                                                                                                                                  self.entry_price - event.price) * self.position_size
            pnl_net = pnl_gross - event.fee

            self.last_closed_direction = self.active_direction
            # Сброс состояния
            self.position_active = False
            self.active_direction = None
            self.entry_price = None
            self.position_size = None

            await self.event_bus.unsubscribe(self._handle_price_update)
            await self._send_trade_close_notification(pnl_net, event.fee, exit_price=event.price)

        self.is_waiting_for_trade = False

    async def _execute_strategy_logic(self):
        """Пустышка, так как логика теперь управляется событиями свечей."""
        pass