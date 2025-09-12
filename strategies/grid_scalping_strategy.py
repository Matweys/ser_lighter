import asyncio
from typing import Dict, Any, Optional, List
from decimal import Decimal, getcontext

from api.bybit_api import BybitAPI
from .base_strategy import BaseStrategy
from core.enums import StrategyType
from core.logger import log_info, log_error, log_warning
from core.events import OrderFilledEvent, EventBus, StrategyRestartRequestEvent, PriceUpdateEvent

getcontext().prec = 28


class GridScalpingStrategy(BaseStrategy):
    """
    Улучшенная и полная реализация стратегии грид-скальпинга (LONG-only)
    с пошаговым усреднением, детальным логированием и надежным управлением состоянием.
    """

    def __init__(self, user_id: int, symbol: str, signal_data: Dict[str, Any], api: BybitAPI, event_bus: EventBus,
                 bot: "Bot", config: Optional[Dict] = None):
        super().__init__(user_id, symbol, signal_data, api, event_bus, bot, config)

        # --- Параметры, загружаемые из конфигурации ---
        self.scalp_levels: int = 5
        self.scalp_spacing_percent: Decimal = Decimal('10.0')
        self.quick_profit_percent: Decimal = Decimal('1.0')
        self.stop_loss_percent: Decimal = Decimal('5.0')
        self.cooldown_after_stop: int = 60

        # --- Внутреннее состояние стратегии ---
        self.position_entry_price: Optional[Decimal] = None  # Цена самого первого входа
        self.position_avg_price: Optional[Decimal] = None  # Текущая средняя цена позиции
        self.position_size: Decimal = Decimal('0')
        self.averaging_orders_placed: int = 0
        self.active_tp_order_id: Optional[str] = None
        self.is_waiting_for_fill: bool = False  # Флаг ожидания исполнения ордера (вход/усреднение)

    def _get_strategy_type(self) -> StrategyType:
        return StrategyType.GRID_SCALPING

    async def _load_scalp_parameters(self):
        """Загружает и валидирует параметры из конфига."""
        await self._ensure_config_fresh()
        if not self.config:
            log_error(self.user_id, "Конфигурация для Grid Scalping не загружена.", "grid_scalping")
            return

        self.scalp_levels = int(self.get_config_value("max_averaging_orders", 5))
        self.scalp_spacing_percent = self._convert_to_decimal(self.get_config_value("scalp_spacing_percent", 10.0))
        self.quick_profit_percent = self._convert_to_decimal(self.get_config_value("profit_percent", 1.0))
        self.stop_loss_percent = self._convert_to_decimal(self.get_config_value("stop_loss_percent", 5.0))
        self.cooldown_after_stop = int(self.get_config_value("cooldown_after_stop_seconds", 60))
        log_info(self.user_id,
                 f"Параметры Grid Scalping загружены: уровни={self.scalp_levels}, шаг={self.scalp_spacing_percent}%, профит={self.quick_profit_percent}%",
                 "grid_scalping")

    # 1. ЛОГИКА ЗАПУСКА И ПЕРВОГО ВХОДА
    async def _execute_strategy_logic(self):
        """Основная логика, запускает начальный вход в позицию."""
        # Если позиция уже есть или мы ждем исполнения ордера, ничего не делаем
        if self.position_size > 0 or self.is_waiting_for_fill:
            return

        try:
            log_info(self.user_id, f"Запуск нового цикла Grid Scalping для {self.symbol}", "grid_scalping")
            await self._load_scalp_parameters()
            await self._set_leverage()

            order_size_usdt = self._convert_to_decimal(self.get_config_value("order_amount", 10.0))
            qty = await self.api.calculate_quantity_from_usdt(self.symbol, order_size_usdt)

            if qty > 0:
                # Устанавливаем флаг, что мы ждем исполнения ордера
                self.is_waiting_for_fill = True
                log_info(self.user_id, f"Размещаю начальный рыночный ордер для {self.symbol} и ожидаю его исполнения.",
                         "grid_scalping")

                # Размещаем ордер. Логика подтверждения теперь внутри _handle_order_filled
                order_id = await self._place_order(side="Buy", order_type="Market", qty=qty)

                if not order_id:
                    log_error(self.user_id, "Не удалось разместить начальный ордер, API не вернул ID.", "grid_scalping")
                    self.is_waiting_for_fill = False
                    await self.stop("Failed to place initial order")
            else:
                await self.stop("Рассчитанное количество для ордера равно нулю.")
        except Exception as e:
            log_error(self.user_id, f"Критическая ошибка при инициализации Grid Scalping: {e}", "grid_scalping")
            await self.stop("Ошибка инициализации")

    # 2. ОБРАБОТКА СОБЫТИЙ ИСПОЛНЕНИЯ ОРДЕРОВ
    async def _handle_order_filled(self, event: OrderFilledEvent):
        """Центральный обработчик всех исполненных ордеров."""
        try:
            # --- Сценарий: Закрытие по Take Profit или Stop Loss ---
            # Так как стратегия только покупает, любой исполненный ордер на продажу является закрытием.
            if event.side == "Sell":
                pnl_net = await self._calculate_pnl(event)
                log_info(self.user_id, f"СДЕЛКА ЗАКРЫТА по TP или SL. PnL (net): {pnl_net:.2f}", "grid_scalping")
                await self._send_trade_close_notification(pnl_net, event.fee)
                await self.request_restart()
                return  # Важно завершить выполнение здесь

            # --- Сценарий: Открытие или Усреднение ---
            if event.side == "Buy":
                is_first_entry = not self.position_size > 0

                # Обновляем состояние позиции
                new_total_size = self.position_size + event.qty
                new_avg_price = ((self.position_avg_price * self.position_size) + (
                            event.price * event.qty)) / new_total_size if not is_first_entry else event.price

                # СНАЧАЛА обновляем внутреннее состояние
                if is_first_entry:
                    self.position_entry_price = event.price
                else:
                    self.averaging_orders_placed += 1

                self.position_avg_price = new_avg_price
                self.position_size = new_total_size
                self.is_waiting_for_fill = False

                # ЗАТЕМ отправляем уведомления с уже обновленным состоянием
                if is_first_entry:
                    log_info(self.user_id,
                             f"ПЕРВЫЙ ВХОД в позицию по {self.symbol}: цена={event.price:.4f}, размер={event.qty}",
                             "grid_scalping")
                    await self._send_trade_open_notification(event.side, event.price, event.qty,
                                                             self._convert_to_decimal(
                                                                 self.get_config_value("order_amount", 10.0)))
                else:
                    log_info(self.user_id,
                             f"УСРЕДНЕНИЕ ПОЗИЦИИ ({self.averaging_orders_placed}/{self.scalp_levels}) по {self.symbol}. Новая ср. цена: {new_avg_price:.4f}",
                             "grid_scalping")
                    await self._send_averaging_notification(event.price, event.qty, new_avg_price, new_total_size)

                # После любого входа/усреднения обновляем TP и SL
                await self._update_tp_and_sl()
        except Exception as e:
            log_error(self.user_id, f"Ошибка в _handle_order_filled: {e}", "grid_scalping")

    # 3. ОБРАБОТКА ИЗМЕНЕНИЯ ЦЕНЫ ДЛЯ УСРЕДНЕНИЯ
    async def _handle_price_update(self, event: PriceUpdateEvent):
        """Следит за ценой для инициации усреднения."""
        # Не работаем, если нет позиции или уже ждем исполнения другого ордера
        if not self.is_running or not self.position_size > 0 or self.is_waiting_for_fill:
            return

        # Рассчитываем следующий уровень усреднения "на лету"
        next_avg_price = self.position_avg_price * (1 - self.scalp_spacing_percent / 100)

        if event.price <= next_avg_price:
            if self.averaging_orders_placed < self.scalp_levels:
                log_info(self.user_id,
                         f"Цена {self.symbol} ({event.price}) достигла уровня усреднения ({next_avg_price:.4f}).",
                         "grid_scalping")

                # Устанавливаем флаг, чтобы избежать повторных ордеров
                self.is_waiting_for_fill = True

                order_size_usdt = self._convert_to_decimal(self.get_config_value("order_amount", 10.0))
                qty_to_buy = await self.api.calculate_quantity_from_usdt(self.symbol, order_size_usdt)

                if qty_to_buy > 0:
                    # Перед усреднением отменяем старый TP/SL ордер
                    await self._cancel_tp_order()
                    await self._place_order(side="Buy", order_type="Market", qty=qty_to_buy)
            else:
                # Лимит усреднений исчерпан, больше не проверяем
                pass

    # 4. УПРАВЛЕНИЕ ОРДЕРАМИ TP/SL
    async def _update_tp_and_sl(self):
        """Отменяет старый TP/SL и выставляет новый, единый ордер."""
        await self._cancel_tp_order()

        if not self.position_avg_price or self.position_size == 0:
            return

        # Рассчитываем новые уровни TP и SL
        profit_price = self.position_avg_price * (1 + self.quick_profit_percent / 100)
        # ИСПРАВЛЕНО: Для LONG позиции стоп-лосс должен вычитаться из цены входа
        stop_loss_price = self.position_entry_price * (1 - self.stop_loss_percent / 100)

        log_info(self.user_id, f"Установка нового лимитного ордера: TP={profit_price:.4f}, SL={stop_loss_price:.4f}",
                 "grid_scalping")

        # Выставляем TP и SL на всю позицию через специальный метод API
        success = await self.api.set_trading_stop(
            symbol=self.symbol,
            take_profit=profit_price,
            stop_loss=stop_loss_price,
            position_idx=0  # 0 для режима One-Way
        )

        if not success:
            log_error(self.user_id,
                      f"КРИТИЧЕСКАЯ ОШИБКА: Не удалось установить TP/SL для позиции {self.symbol}. Стратегия может быть беззащитна.",
                      "grid_scalping")
            # Можно добавить логику аварийной остановки
            # await self.stop("Failed to set protective orders")

    async def _cancel_tp_order(self):
        """Отменяет активные TP/SL, установленные на позицию."""
        # Для отмены нужно отправить нули в качестве значений
        log_info(self.user_id, f"Отмена предыдущих TP/SL для позиции {self.symbol}", "grid_scalping")
        await self.api.set_trading_stop(
            symbol=self.symbol,
            take_profit=Decimal('0'),
            stop_loss=Decimal('0'),
            position_idx=0
        )
        self.active_tp_order_id = None

    # 5. ЛОГИКА ЗАВЕРШЕНИЯ И ПЕРЕЗАПУСКА
    async def stop(self, reason: str = "Manual stop"):
        """Переопределяем stop для полной очистки."""
        log_info(self.user_id, f"Остановка Grid Scalping для {self.symbol}. Причина: {reason}", "grid_scalping")
        await self._cancel_tp_order()
        # Сбрасываем состояние перед полной остановкой
        self.position_size = Decimal('0')
        self.is_waiting_for_fill = False
        await super().stop(reason)

    async def request_restart(self, delay_seconds: int = 0):
        """Публикует событие с просьбой о перезапуске стратегии."""
        if delay_seconds > 0:
            log_info(self.user_id, f"Запрос на перезапуск {self.symbol} через {delay_seconds} сек.", "grid_scalping")
        else:
            log_info(self.user_id, f"Запрос на немедленный перезапуск {self.symbol}.", "grid_scalping")

        await self.stop("Requesting restart")
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
        if self.position_avg_price and self.position_size > 0:
            pnl_gross = (event.price - self.position_avg_price) * self.position_size
            return pnl_gross - event.fee
        return Decimal('0')
