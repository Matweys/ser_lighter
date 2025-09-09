# strategies/grid_scalping_strategy.py

import asyncio
from typing import Dict, Any, Optional, List
from decimal import Decimal, getcontext
from datetime import datetime, timedelta

from api.bybit_api import BybitAPI
from .base_strategy import BaseStrategy
from core.enums import StrategyType, OrderType
from core.logger import log_info, log_error, log_warning
from core.events import OrderFilledEvent, EventBus, StrategyRestartRequestEvent, PriceUpdateEvent
from core.settings_config import EXCHANGE_FEES
from core.enums import ExchangeType


getcontext().prec = 28


class GridScalpingStrategy(BaseStrategy):
    """
    Стратегия грид-скальпинга (LONG-only) с усреднением и перезапуском.
    Реализует логику, предложенную пользователем: вход, усреднение при падении,
    фиксация прибыли при росте и автоматический перезапуск цикла.
    """

    def __init__(self, user_id: int, symbol: str, signal_data: Dict[str, Any], api: BybitAPI, event_bus: EventBus,
                 bot: "Bot", config: Optional[Dict] = None):
        super().__init__(user_id, symbol, signal_data, api, event_bus, bot, config)

        # Параметры из конфигурации
        self.scalp_levels: int = 5  # Макс. кол-во ордеров на усреднение
        self.scalp_spacing_percent: Decimal = Decimal('1.0')  # Процент падения для усреднения
        self.quick_profit_percent: Decimal = Decimal('1.0')  # Процент роста для фиксации прибыли
        self.stop_loss_percent: Decimal = Decimal('5.0')  # Глобальный стоп-лосс от первого входа

        # Состояние позиции
        self.position_entry_price: Optional[Decimal] = None
        self.position_avg_price: Optional[Decimal] = None
        self.position_size: Decimal = Decimal('0')
        self.averaging_orders_placed: int = 0
        self.active_limit_orders: Dict[str, Dict] = {}  # {order_id: {type: 'take_profit'/'average'}}

    def _get_strategy_type(self) -> StrategyType:
        return StrategyType.GRID_SCALPING

    async def _load_scalp_parameters(self):
        """Загрузка параметров из конфигурации."""
        await self._ensure_config_fresh()
        if not self.config: return

        self.scalp_levels = self.config.get("max_averaging_orders", 5)
        self.scalp_spacing_percent = self._convert_to_decimal(self.config.get("scalp_spacing_percent", 1.0))
        self.quick_profit_percent = self._convert_to_decimal(self.config.get("profit_percent", 1.0))
        self.stop_loss_percent = self._convert_to_decimal(self.config.get("stop_loss_percent", 5.0))
        log_info(self.user_id,
                 f"Параметры Grid Scalping загружены: уровни={self.scalp_levels}, шаг={self.scalp_spacing_percent}%, профит={self.quick_profit_percent}%",
                 module_name=__name__)

    async def calculate_order_size(self) -> Decimal:
        """Рассчитывает размер ордера на основе конфигурации."""
        return self._convert_to_decimal(self.get_config_value("order_amount", 10.0))



    async def _execute_strategy_logic(self):
        """Инициализация и вход в позицию."""
        try:
            log_info(self.user_id, f"Запуск Grid Scalping для {self.symbol}", module_name=__name__)
            await self._load_scalp_parameters()
            await self._set_leverage()

            if self.position_size > 0: return

            order_size_usdt = await self.calculate_order_size()
            if order_size_usdt <= 0:
                await self.stop("Invalid order size")
                return

            qty = await self.api.calculate_quantity_from_usdt(self.symbol, order_size_usdt)
            if qty > 0:
                order_id = await self._place_order(side="Buy", order_type="Market", qty=qty)
                if not order_id:
                    await self.stop("Failed to place initial order")
            else:
                await self.stop("Calculated qty is zero")
        except Exception as e:
            log_error(self.user_id, f"Ошибка инициализации Grid Scalping: {e}", module_name=__name__)
            await self.stop("Ошибка инициализации")

    async def _handle_order_filled(self, event: OrderFilledEvent):
        """Обработка исполнения ордера с расчетом чистого PnL."""
        try:
            log_info(self.user_id,f"[TRACE] GridScalping._handle_order_filled получил событие:"
            f" side={event.side}, price={event.price}, qty={event.qty}","grid_scalping")

            # --- Ордер на ПРОДАЖУ (фиксация прибыли или стоп-лосс) ---
            if event.side == "Sell":
                pnl_gross = Decimal('0')
                commission = Decimal('0')

                if self.position_avg_price and self.position_avg_price > 0 and self.position_size > 0:
                    pnl_gross = (event.price - self.position_avg_price) * self.position_size

                    # Расчет комиссии (предполагаем Taker)
                    fee_rate = EXCHANGE_FEES.get(ExchangeType.BYBIT, {}).get('taker', Decimal('0.055')) / 100
                    trade_volume = event.price * self.position_size
                    commission = trade_volume * fee_rate
                else:
                    log_warning(
                        self.user_id,
                        f"Не удалось рассчитать PnL для {self.symbol} из-за отсутствия данных о входе в позицию. "
                        f"(avg_price: {self.position_avg_price}, size: {self.position_size}). Вероятно, позиция была открыта вне контроля бота.",
                        "grid_scalping"
                    )

                pnl_net = pnl_gross - commission

                await self._send_trade_close_notification(pnl_net, commission)
                log_info(self.user_id,f"Прибыль по {self.symbol} зафиксирована. PnL (net): {pnl_net:.2f}. Перезапуск цикла.","grid_scalping")

                # Запрашиваем перезапуск стратегии
                await self.event_bus.publish(StrategyRestartRequestEvent(user_id=self.user_id, strategy_type=self.strategy_type.value, symbol=self.symbol))
                await self.stop("Profit/Loss taken, restarting cycle")
                return

            # --- Ордер на ПОКУПКУ (вход или усреднение) ---
            if event.side == "Buy":
                log_info(self.user_id, "[TRACE] Условие event.side == 'Buy' выполнено.", "grid_scalping")
                self.active_limit_orders.pop(event.order_id, None)

                log_info(self.user_id, f"[TRACE] Проверка условия для входа: self.position_size = {self.position_size}", "grid_scalping")
                if self.position_size == 0:  # Первый вход
                    self.position_entry_price = event.price
                    self.position_avg_price = event.price
                    self.position_size = event.qty

                    # Передаем запрошенную сумму для сравнения
                    intended_amount = await self.calculate_order_size()

                    log_info(self.user_id, "[TRACE] Вызов _send_trade_open_notification...", "grid_scalping")
                    await self._send_trade_open_notification(event.side, event.price, event.qty, intended_amount)
                else:  # Усреднение
                    log_info(self.user_id,"[TRACE] Условие self.position_size == 0 НЕ выполнено. Захожу в блок 'Усреднение'.","grid_scalping")
                    new_total_size = self.position_size + event.qty
                    new_avg_price = ((self.position_avg_price * self.position_size) + (event.price * event.qty)) / new_total_size

                    old_avg_price = self.position_avg_price  # сохраняем для лога
                    self.position_avg_price = new_avg_price
                    self.position_size = new_total_size
                    self.averaging_orders_placed += 1

                    # Вызываем новое уведомление об усреднении
                    await self._send_averaging_notification(event.price, event.qty, new_avg_price, new_total_size)

                    log_info(self.user_id,
                             f"Позиция по {self.symbol} усреднена. Ср. цена: {old_avg_price:.4f} -> {new_avg_price:.4f}, размер: {new_total_size}",
                             "grid_scalping")

                # Обновляем все лимитные ордера
                await self._update_limit_orders()
        except Exception as e:
            log_error(self.user_id, f"Ошибка обработки исполнения ордера в GridScalping: {e}", "grid_scalping")

    async def _update_limit_orders(self):
        """Отменяет старые и выставляет новые лимитные ордера."""
        await self._cancel_all_limit_orders()

        if not self.position_avg_price or self.position_size == 0: return

        # 1. Выставляем ордер Take Profit
        profit_price = self.position_avg_price * (1 + self.quick_profit_percent / 100)
        tp_id = await self._place_order(side="Sell", order_type="Limit", qty=self.position_size, price=profit_price,
                                        reduce_only=True)
        if tp_id: self.active_limit_orders[tp_id] = {'type': 'take_profit'}

        # 2. Выставляем ордер Stop Loss
        stop_loss_price = self.position_entry_price * (1 - self.stop_loss_percent / 100)
        sl_id = await self._place_order(side="Sell", order_type="Stop", qty=self.position_size, price=stop_loss_price,
                                        reduce_only=True)
        if sl_id: self.active_limit_orders[sl_id] = {'type': 'stop_loss'}

        # 3. Выставляем ордера на усреднение
        order_size_usdt = await self.calculate_order_size()
        for i in range(self.averaging_orders_placed, self.scalp_levels):
            averaging_price = self.position_avg_price * (1 - self.scalp_spacing_percent * (i + 1) / 100)
            qty_to_buy = await self.api.calculate_quantity_from_usdt(self.symbol, order_size_usdt, averaging_price)
            if qty_to_buy > 0:
                avg_id = await self._place_order(side="Buy", order_type="Limit", qty=qty_to_buy, price=averaging_price)
                if avg_id: self.active_limit_orders[avg_id] = {'type': 'average'}

    async def _cancel_all_limit_orders(self):
        """Отменяет все активные лимитные ордера этой стратегии."""
        if not self.active_limit_orders: return
        log_info(self.user_id, f"Отмена {len(self.active_limit_orders)} активных лимитных ордеров...",
                 module_name=__name__)
        for order_id in list(self.active_limit_orders.keys()):
            await self._cancel_order(order_id)
        self.active_limit_orders.clear()

    async def stop(self, reason: str = "Manual stop"):
        """Переопределяем stop для полной очистки."""
        await self._cancel_all_limit_orders()
        await super().stop(reason)

    async def _handle_price_update(self, event: PriceUpdateEvent):
        """
        Обработка обновления цены.
        Для данной стратегии, основанной на лимитных ордерах,
        активная обработка тиков цены не требуется.
        Логика срабатывает при исполнении ордеров.
        """
        pass