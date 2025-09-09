# strategies/impulse_trailing_strategy.py

from decimal import Decimal, getcontext
from typing import Dict, Any, Optional

from api.bybit_api import BybitAPI
from .base_strategy import BaseStrategy
from core.enums import StrategyType
from core.logger import log_info, log_error
from core.events import EventBus, PriceUpdateEvent, OrderFilledEvent
from core.settings_config import EXCHANGE_FEES
from core.enums import ExchangeType


getcontext().prec = 28


class ImpulseTrailingStrategy(BaseStrategy):
    """Асимметричная стратегия с гибридным управлением позицией (SL/TP + Trailing)."""

    def __init__(self, user_id: int, symbol: str, signal_data: Dict[str, Any], api: BybitAPI, event_bus: EventBus,
                 bot: "Bot", config: Optional[Dict] = None):
        super().__init__(user_id, symbol, signal_data, api, event_bus, bot, config)
        self.position_side: Optional[str] = None
        self.entry_price: Optional[Decimal] = None
        self.position_size: Optional[Decimal] = None
        self.stop_loss_price: Optional[Decimal] = None
        self.take_profit_price: Optional[Decimal] = None
        self.peak_price: Optional[Decimal] = None  # Пиковая цена для трейлинга

    def _get_strategy_type(self) -> StrategyType:
        return StrategyType.IMPULSE_TRAILING

    async def _execute_strategy_logic(self):
        """Анализ сигнала и принятие решения о входе, основанное на полном алгоритме."""
        try:
            # Если позиция уже открыта, логика входа не выполняется
            if self.position_side:
                return

            analysis = self.signal_data.get('analysis_data', {})
            if not analysis or not analysis.get('atr'):
                await self.stop("Insufficient analysis data in signal")
                return

            current_price = analysis['current_price']
            atr = analysis['atr']

            # --- Логика для ЛОНГА: пробой после консолидации в восходящем тренде ---
            if analysis.get('ema_trend') == "UP" and analysis.get('is_consolidating_now'):
                # Проверяем фрикцию - избегаем входа при высокой фрикции (вязкий рынок)
                friction_level = analysis.get('friction_level', 'NEUTRAL')
                if friction_level == "HIGH":
                    log_info(self.user_id,
                             f"LONG signal skipped for {self.symbol}: High market friction ({analysis.get('friction_value', 'N/A')})",
                             "impulse_trailing")
                    return

                breakout_level = analysis['consolidation_high'] * (
                        1 + Decimal(str(self.config['long_breakout_buffer'])))
                if current_price > breakout_level:
                    log_info(self.user_id,
                             f"LONG Signal for {self.symbol}: Breakout after consolidation. Friction: {friction_level}",
                             "impulse_trailing")
                    self.position_side = "Buy"
                    self.stop_loss_price = current_price - (atr * Decimal(str(self.config['long_sl_atr'])))
                    self.take_profit_price = current_price + (atr * Decimal(str(self.config['long_tp_atr'])))
                    await self._enter_position()
                    return

            # --- Логика для ШОРТА: паническая свеча с высоким объемом ---
            if analysis.get('is_panic_bar'):
                # Для шорта предпочитаем низкую фрикцию (скользкий рынок)
                friction_level = analysis.get('friction_level', 'NEUTRAL')
                if friction_level == "HIGH":
                    log_info(self.user_id,
                             f"SHORT signal skipped for {self.symbol}: High market friction not optimal for panic trades ({analysis.get('friction_value', 'N/A')})",
                             "impulse_trailing")
                    return

                log_info(self.user_id,
                         f"SHORT Signal for {self.symbol}: Panic bar detected. Friction: {friction_level}",
                         "impulse_trailing")
                self.position_side = "Sell"
                self.stop_loss_price = current_price + (atr * Decimal(str(self.config['short_sl_atr'])))
                self.take_profit_price = current_price - (atr * Decimal(str(self.config['short_tp_atr'])))
                await self._enter_position()
                return

            # Если ни одно из условий не выполнено, стратегия не делает ничего и ждет следующего сигнала
            # Не нужно вызывать self.stop(), так как стратегия должна продолжать анализировать рынок

        except Exception as e:
            log_error(self.user_id, f"Error in strategy logic: {e}", "impulse_trailing")
            await self.stop("Strategy logic error")



    async def _enter_position(self):
        """Вход в позицию и установка SL/TP через API."""
        await self._set_leverage()
        order_size_usdt = Decimal(str(self.config.get("order_amount", 50.0)))
        qty = await self.api.calculate_quantity_from_usdt(self.symbol, order_size_usdt)

        if qty > 0:
            order_id = await self._place_order(
                side=self.position_side,
                order_type="Market",
                qty=qty,
                stop_loss=self.stop_loss_price,
                take_profit=self.take_profit_price
            )
            if not order_id:
                await self.stop("Failed to place entry order")
        else:
            await self.stop("Calculated order quantity is zero")

    async def _handle_order_filled(self, event: OrderFilledEvent):
        """Обработка исполненных ордеров с расчетом чистого PnL."""
        log_info(self.user_id,
                 f"[TRACE] ImpulseTrailing._handle_order_filled получил событие: side={event.side}, price={event.price}, qty={event.qty}",
                 "impulse_trailing")

        # --- Ордер ВХОДА ---
        log_info(self.user_id,
                 f"[TRACE] Проверка условия для входа: self.position_side={self.position_side}, event.side={event.side}",
                 "impulse_trailing")
        if self.position_side and event.side == self.position_side:
            log_info(self.user_id, "[TRACE] Условие для входа выполнено. Захожу в блок 'Ордер ВХОДА'.",
                     "impulse_trailing")
            self.entry_price = event.price
            self.position_size = event.qty
            self.peak_price = event.price
            self.active_positions[self.symbol] = True

            # Добавляем получение и передачу intended_amount
            intended_amount = self._convert_to_decimal(self.get_config_value("order_amount", 50.0))
            log_info(self.user_id, "[TRACE] Вызов _send_trade_open_notification...", "impulse_trailing")
            await self._send_trade_open_notification(event.side, event.price, event.qty, intended_amount)
            return

        # --- Ордер ВЫХОДА (противоположный направлению позиции) ---
        if self.position_side and event.side != self.position_side:
            pnl_gross = Decimal('0')
            commission = Decimal('0')

            if self.entry_price and self.position_size:
                pnl_gross = (event.price - self.entry_price) * self.position_size if self.position_side == "Buy" else (
                                                                                                                                  self.entry_price - event.price) * self.position_size

                fee_rate = EXCHANGE_FEES.get(ExchangeType.BYBIT, {}).get('taker', Decimal('0.055')) / 100
                trade_volume = event.price * self.position_size
                commission = trade_volume * fee_rate

            pnl_net = pnl_gross - commission

            await self._send_trade_close_notification(pnl_net, commission)
            await self.stop("Position closed")

    async def _handle_price_update(self, event: PriceUpdateEvent):
        """Логика трейлинг-стопа при обновлении цены."""
        if not self.position_side or not self.active_positions.get(self.symbol):
            return

        current_price = event.price

        if self.peak_price is None or \
           (self.position_side == "Buy" and current_price > self.peak_price) or \
           (self.position_side == "Sell" and current_price < self.peak_price):
            self.peak_price = current_price
        else:
            return

        analysis_data = self.signal_data.get('analysis_data', {})
        atr = analysis_data.get('atr')
        if not atr:
            atr = (self.entry_price * Decimal('0.01'))
            log_warning(self.user_id, f"ATR not found in signal for {self.symbol}, using fallback.", "impulse_trailing")

        trailing_atr_mult = Decimal(str(self.config.get('trailing_sl_atr', 1.5)))

        if self.position_side == "Buy":
            new_stop_price = self.peak_price - (atr * trailing_atr_mult)
            if new_stop_price > self.stop_loss_price:
                self.stop_loss_price = new_stop_price
                await self.api.set_trading_stop(symbol=self.symbol, stop_loss=self.stop_loss_price)
                log_info(self.user_id, f"Trailing SL for LONG on {self.symbol} moved to {self.stop_loss_price:.4f}", "impulse_trailing")
        elif self.position_side == "Sell":
            new_stop_price = self.peak_price + (atr * trailing_atr_mult)
            if new_stop_price < self.stop_loss_price:
                self.stop_loss_price = new_stop_price
                await self.api.set_trading_stop(symbol=self.symbol, stop_loss=self.stop_loss_price)
                log_info(self.user_id, f"Trailing SL for SHORT on {self.symbol} moved to {self.stop_loss_price:.4f}", "impulse_trailing")
