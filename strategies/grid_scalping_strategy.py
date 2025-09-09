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
    Стратегия грид-скальпинга (LONG-only) с пошаговым усреднением и перезапуском.
    Реализует сценарии, описанные пользователем.
    """

    def __init__(self, user_id: int, symbol: str, signal_data: Dict[str, Any], api: BybitAPI, event_bus: EventBus,
                 bot: "Bot", config: Optional[Dict] = None):
        super().__init__(user_id, symbol, signal_data, api, event_bus, bot, config)

        # Параметры из конфигурации
        self.scalp_levels: int = 5
        self.scalp_spacing_percent: Decimal = Decimal('1.0')
        self.quick_profit_percent: Decimal = Decimal('1.0')
        self.stop_loss_percent: Decimal = Decimal('5.0')
        self.cooldown_after_stop: int = 60  # Время ожидания в секундах после SL

        # Состояние позиции
        self.position_entry_price: Optional[Decimal] = None
        self.position_avg_price: Optional[Decimal] = None
        self.position_size: Decimal = Decimal('0')
        self.averaging_orders_placed: int = 0
        self.active_tp_order_id: Optional[str] = None
        self.next_averaging_price: Optional[Decimal] = None

    async def validate_config(self) -> bool:
        if not await super().validate_config(): return False
        required_fields = ['profit_percent', 'stop_loss_percent', 'max_averaging_orders', 'scalp_spacing_percent']
        for field in required_fields:
            if field not in self.config:
                log_error(self.user_id, f"Отсутствует поле в конфиге Grid Scalping: {field}", module_name=__name__)
                return False
        return True

    def _get_strategy_type(self) -> StrategyType:
        return StrategyType.GRID_SCALPING

    async def _load_scalp_parameters(self):
        await self._ensure_config_fresh()
        if not self.config: return
        self.scalp_levels = int(self.config.get("max_averaging_orders", 5))
        self.scalp_spacing_percent = self._convert_to_decimal(self.config.get("scalp_spacing_percent", 1.0))
        self.quick_profit_percent = self._convert_to_decimal(self.config.get("profit_percent", 1.0))
        self.stop_loss_percent = self._convert_to_decimal(self.config.get("stop_loss_percent", 5.0))
        self.cooldown_after_stop = int(self.config.get("cooldown_after_stop_seconds", 60))
        log_info(self.user_id,
                 f"Параметры Grid Scalping: уровни={self.scalp_levels}, шаг={self.scalp_spacing_percent}%, профит={self.quick_profit_percent}%",
                 module_name=__name__)

    async def calculate_order_size(self) -> Decimal:
        return self._convert_to_decimal(self.get_config_value("order_amount", 10.0))

    # 1. ИНИЦИАЛИЗАЦИЯ И ПЕРВЫЙ ВХОД
    async def _execute_strategy_logic(self):
        """Инициализация и вход в первую позицию."""
        try:
            if self.position_size > 0: return  # Если позиция уже есть, ничего не делаем
            log_info(self.user_id, f"Запуск Grid Scalping для {self.symbol}", module_name=__name__)
            await self._load_scalp_parameters()
            await self._set_leverage()

            order_size_usdt = await self.calculate_order_size()
            if order_size_usdt <= 0:
                await self.stop("Неверный размер ордера")
                return

            qty = await self.api.calculate_quantity_from_usdt(self.symbol, order_size_usdt)
            if qty > 0:
                # Рассчитываем Stop Loss ЗАРАНЕЕ
                current_price = await self.api.get_current_price(self.symbol)
                if not current_price:
                    await self.stop("Не удалось получить цену для расчета SL")
                    return

                stop_loss_price = current_price * (1 - self.stop_loss_percent / 100)

                # Размещаем рыночный ордер сразу со стоп-лоссом
                order_id = await self._place_order(side="Buy", order_type="Market", qty=qty, stop_loss=stop_loss_price)
                if not order_id:
                    await self.stop("Не удалось разместить начальный ордер")
            else:
                await self.stop("Рассчитанное кол-во равно нулю")
        except Exception as e:
            log_error(self.user_id, f"Ошибка инициализации Grid Scalping: {e}", module_name=__name__)
            await self.stop("Ошибка инициализации")

    # 2. ОБРАБОТКА ИСПОЛНЕННЫХ ОРДЕРОВ
    async def _handle_order_filled(self, event: OrderFilledEvent):
        """Обработка всех исполненных ордеров: вход, усреднение, тейк-профит."""
        try:
            # --- Сценарий 1: Исполнен ордер на ПРОДАЖУ (Take Profit) ---
            if event.side == "Sell":
                pnl_net = await self._calculate_pnl(event)
                await self._send_trade_close_notification(pnl_net, event.fee)
                log_info(self.user_id,
                         f"Прибыль по {self.symbol} зафиксирована. PnL (net): {pnl_net:.2f}. Перезапуск цикла.",
                         "grid_scalping")
                await self.request_restart()  # Запрашиваем перезапуск
                return

            # --- Сценарий 2: Исполнен ордер на ПОКУПКУ (Первый вход или Усреднение) ---
            if event.side == "Buy":
                is_first_entry = self.position_size == 0

                # Обновляем состояние позиции
                new_total_size = self.position_size + event.qty
                new_avg_price = ((self.position_avg_price * self.position_size) + (
                            event.price * event.qty)) / new_total_size if not is_first_entry else event.price

                if is_first_entry:
                    self.position_entry_price = event.price
                    await self._send_trade_open_notification(event.side, event.price, event.qty,
                                                             await self.calculate_order_size())
                    log_info(self.user_id,
                             f"Первый вход в позицию по {self.symbol}: цена={event.price:.4f}, размер={event.qty}",
                             "grid_scalping")
                else:
                    self.averaging_orders_placed += 1
                    await self._send_averaging_notification(event.price, event.qty, new_avg_price, new_total_size)
                    log_info(self.user_id, f"Позиция по {self.symbol} усреднена. Новая ср. цена: {new_avg_price:.4f}",
                             "grid_scalping")

                self.position_avg_price = new_avg_price
                self.position_size = new_total_size

                # После любого входа/усреднения обновляем TP и следующий уровень усреднения
                await self._update_take_profit_and_next_average()
        except Exception as e:
            log_error(self.user_id, f"Ошибка обработки исполнения ордера в GridScalping: {e}", "grid_scalping")

    # 3. ОБРАБОТКА ТЕКУЩЕЙ ЦЕНЫ (ДЛЯ УСРЕДНЕНИЯ)
    async def _handle_price_update(self, event: PriceUpdateEvent):
        """Следит за ценой для инициации усреднения."""
        if not self.is_running or not self.position_size > 0:
            return

        # Проверяем, нужно ли усредняться
        if self.next_averaging_price and event.price <= self.next_averaging_price:
            if self.averaging_orders_placed < self.scalp_levels:
                log_info(self.user_id,
                         f"Цена {self.symbol} достигла уровня усреднения {self.next_averaging_price}. Размещаю ордер.",
                         "grid_scalping")

                # Сбрасываем цену следующего усреднения, чтобы избежать повторных ордеров
                self.next_averaging_price = None

                order_size_usdt = await self.calculate_order_size()
                qty_to_buy = await self.api.calculate_quantity_from_usdt(self.symbol, order_size_usdt)

                if qty_to_buy > 0:
                    # Важно: отменяем старый TP перед усреднением
                    await self._cancel_tp_order()

                    # Размещаем рыночный ордер на усреднение
                    await self._place_order(side="Buy", order_type="Market", qty=qty_to_buy)
            else:
                # Лимит усреднений исчерпан, просто ждем TP или SL
                self.next_averaging_price = None

                # 4. УПРАВЛЕНИЕ ОРДЕРАМИ

    async def _update_take_profit_and_next_average(self):
        """Отменяет старый TP и выставляет новый. Рассчитывает следующий уровень усреднения."""
        await self._cancel_tp_order()

        if not self.position_avg_price or self.position_size == 0: return

        # 1. Выставляем новый ордер Take Profit
        profit_price = self.position_avg_price * (1 + self.quick_profit_percent / 100)
        tp_id = await self._place_order(side="Sell", order_type="Limit", qty=self.position_size, price=profit_price,
                                        reduce_only=True)
        if tp_id:
            self.active_tp_order_id = tp_id
            log_info(self.user_id, f"Новый Take Profit для {self.symbol} установлен на {profit_price:.4f}",
                     "grid_scalping")

        # 2. Рассчитываем следующий уровень для усреднения
        if self.averaging_orders_placed < self.scalp_levels:
            self.next_averaging_price = self.position_avg_price * (1 - self.scalp_spacing_percent / 100)
            log_info(self.user_id,
                     f"Следующий уровень усреднения для {self.symbol} установлен на {self.next_averaging_price:.4f}",
                     "grid_scalping")
        else:
            self.next_averaging_price = None  # Лимит исчерпан
            log_info(self.user_id, f"Лимит усреднений для {self.symbol} исчерпан.", "grid_scalping")

    async def _cancel_tp_order(self):
        """Отменяет активный ордер тейк-профита."""
        if self.active_tp_order_id:
            await self._cancel_order(self.active_tp_order_id)
            self.active_tp_order_id = None

    # 5. ЛОГИКА ЗАВЕРШЕНИЯ И ПЕРЕЗАПУСКА
    async def stop(self, reason: str = "Manual stop"):
        """Переопределяем stop для полной очистки и обработки SL."""
        # --- Сценарий 3: Обработка Stop Loss ---
        if "Stop-Loss" in reason or "stop-loss" in reason:
            log_warning(self.user_id,
                        f"Стратегия {self.symbol} остановлена по Stop-Loss. Ожидание {self.cooldown_after_stop} сек. перед перезапуском.",
                        "grid_scalping")
            await self._cancel_tp_order()
            await super().stop(reason)  # Вызываем базовый stop для очистки
            await self.request_restart(delay_seconds=self.cooldown_after_stop)  # Запрашиваем отложенный перезапуск
        else:
            await self._cancel_tp_order()
            await super().stop(reason)

    async def request_restart(self, delay_seconds: int = 0):
        """Публикует событие с просьбой о перезапуске стратегии."""
        log_info(self.user_id, f"Запрос на перезапуск стратегии {self.symbol} с задержкой {delay_seconds} сек.",
                 "grid_scalping")
        # Сначала полностью останавливаем текущую инстанцию
        await self.stop("Requesting restart")
        # Затем публикуем событие
        await self.event_bus.publish(
            StrategyRestartRequestEvent(
                user_id=self.user_id,
                strategy_type=self.strategy_type.value,
                symbol=self.symbol,
                delay_seconds=delay_seconds
            )
        )

    async def _calculate_pnl(self, event: OrderFilledEvent) -> Decimal:
        """Вспомогательный метод для расчета чистого PnL."""
        if self.position_avg_price and self.position_avg_price > 0 and self.position_size > 0:
            pnl_gross = (event.price - self.position_avg_price) * self.position_size
            return pnl_gross - event.fee
        return Decimal('0')

