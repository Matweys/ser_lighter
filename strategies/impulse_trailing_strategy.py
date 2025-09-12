from typing import Dict, Any, Optional
from decimal import Decimal, getcontext
from datetime import datetime

from api.bybit_api import BybitAPI
from .base_strategy import BaseStrategy
from core.enums import StrategyType
from core.logger import log_info, log_error, log_warning
from core.events import EventBus, PriceUpdateEvent, OrderFilledEvent
from core.settings_config import EXCHANGE_FEES
from core.enums import ExchangeType
from cache.redis_manager import redis_manager  # <-- ВАЖНО: Импортируем redis_manager

getcontext().prec = 28


class ImpulseTrailingStrategy(BaseStrategy):
    """
    Асимметричная стратегия с гибридным управлением позицией (SL/TP + Trailing).
    УЛУЧШЕННАЯ ВЕРСИЯ: с надежным механизмом блокировки через Redis,
    гарантирующим исполнение только одной сделки за раз для пользователя.
    """

    def __init__(self, user_id: int, symbol: str, signal_data: Dict[str, Any], api: BybitAPI, event_bus: EventBus,
                 bot: "Bot", config: Optional[Dict] = None):
        # Вызываем оригинальный __init__
        super().__init__(user_id, symbol, signal_data, api, event_bus, bot, config)

        # Инициализируем все поля, как в исходной версии
        self.position_side: Optional[str] = None
        self.entry_price: Optional[Decimal] = None
        self.position_size: Optional[Decimal] = None
        self.stop_loss_price: Optional[Decimal] = None
        self.take_profit_price: Optional[Decimal] = None
        self.peak_price: Optional[Decimal] = None  # Пиковая цена для трейлинга

        # ДОБАВЛЕНО: Ключ для блокировки в Redis, уникальный для каждого пользователя
        self.redis_lock_key = f"user:{self.user_id}:impulse_trailing_lock"

    # ДОБАВЛЕНО: Переопределяем start для добавления логики блокировки
    async def start(self) -> bool:
        """Переопределяем start для добавления логики блокировки."""
        # 1. Проверяем блокировку ПЕРЕД любыми другими действиями
        if await redis_manager.get_cached_data(self.redis_lock_key):
            log_warning(self.user_id,
                        f"Запуск Impulse Trailing для {self.symbol} отменен: другая impulse-сделка уже активна.",
                        "impulse_trailing")
            return False

        # 2. Если блокировки нет, устанавливаем ее с TTL на случай сбоя
        await redis_manager.cache_data(self.redis_lock_key, "locked", ttl=3600)  # TTL на 1 час
        log_info(self.user_id, f"Установлена блокировка Impulse Trailing для символа {self.symbol}.",
                 "impulse_trailing")

        # 3. Вызываем оригинальный метод start из BaseStrategy
        return await super().start()

    # ДОБАВЛЕНО: Переопределяем stop для снятия блокировки
    async def stop(self, reason: str = "Manual stop"):
        """Переопределяем stop для гарантированного снятия блокировки."""
        # 1. Снимаем блокировку, чтобы освободить слот для следующей сделки
        await redis_manager.delete_cached_data(self.redis_lock_key)
        log_info(self.user_id, f"Снята блокировка Impulse Trailing. Причина: {reason}", "impulse_trailing")

        # 2. Вызываем оригинальный метод stop из BaseStrategy для выполнения остальной логики
        await super().stop(reason)

    # --- ВСЕ ОСТАЛЬНЫЕ МЕТОДЫ ОСТАЮТСЯ В ПОЛНОЙ, ДЕТАЛИЗИРОВАННОЙ ВЕРСИИ ---

    async def validate_config(self) -> bool:
        """Валидирует специфичные для Impulse Trailing параметры."""
        if not await super().validate_config():
            return False

        required_fields = ['long_sl_atr', 'long_tp_atr', 'short_sl_atr', 'short_tp_atr', 'trailing_sl_atr']
        for field in required_fields:
            if field not in self.config:
                log_error(self.user_id, f"Отсутствует обязательное поле конфигурации для Impulse Trailing: {field}",
                          module_name=__name__)
                return False
        return True

    def _get_strategy_type(self) -> StrategyType:
        return StrategyType.IMPULSE_TRAILING

    async def _execute_strategy_logic(self):
        """Анализ сигнала и принятие решения о входе с обязательной остановкой, если вход не выполнен."""
        try:
            # Проверяем реальную позицию на бирже, чтобы избежать конфликтов
            existing_position = await self.api.get_positions(self.symbol)
            if existing_position:
                log_warning(self.user_id,
                            f"Impulse Trailing для {self.symbol} не запущена: уже существует активная позиция.",
                            "impulse_trailing")
                await self.stop("Position already exists")
                return

            if self.position_side:  # Внутренняя проверка состояния
                return

            # self.signal_data теперь гарантированно содержит актуальный анализ
            analysis = self.signal_data
            if not analysis or 'atr' not in analysis:
                log_error(self.user_id, f"Отсутствуют данные анализа для {self.symbol}. Проверьте market_analyzer.",
                          "impulse_trailing")
                await self.stop("Insufficient analysis data in signal")
                return

            current_price = self._convert_to_decimal(analysis['current_price'])
            atr = self._convert_to_decimal(analysis['atr'])
            friction_level = analysis.get('friction_level', 'NEUTRAL')
            ema_trend = analysis.get('ema_trend')
            is_consolidating = analysis.get('is_consolidating_now')
            is_panic = analysis.get('is_panic_bar')

            # --- Логика для ЛОНГА ---
            if ema_trend == "UP" and is_consolidating:
                if friction_level == "HIGH":
                    log_info(self.user_id, f"LONG сигнал для {self.symbol} пропущен: высокая фрикция.",
                             "impulse_trailing")
                    await self.stop("Signal skipped: High friction")
                    return
                breakout_level = self._convert_to_decimal(analysis['consolidation_high']) * (
                        1 + self._convert_to_decimal(self.config.get('long_breakout_buffer', '0.001')))
                if current_price > breakout_level:
                    log_info(self.user_id, f"LONG СИГНАЛ для {self.symbol}: Пробой уровня консолидации. Вход.",
                             "impulse_trailing")
                    self.position_side = "Buy"
                    self.stop_loss_price = current_price - (atr * self._convert_to_decimal(self.config['long_sl_atr']))
                    self.take_profit_price = current_price + (
                            atr * self._convert_to_decimal(self.config['long_tp_atr']))
                    await self._enter_position()
                    return
                else:
                    log_info(self.user_id, f"LONG сигнал для {self.symbol} пропущен: цена не пробила уровень.",
                             "impulse_trailing")
                    await self.stop("Signal skipped: No breakout")
                    return

            # --- Логика для ШОРТА ---
            if is_panic:
                if friction_level == "HIGH":
                    log_info(self.user_id, f"SHORT сигнал для {self.symbol} пропущен: высокая фрикция.",
                             "impulse_trailing")
                    await self.stop("Signal skipped: High friction")
                    return
                log_info(self.user_id, f"SHORT СИГНАЛ для {self.symbol}: Обнаружена паническая свеча. Вход.",
                         "impulse_trailing")
                self.position_side = "Sell"
                self.stop_loss_price = current_price + (atr * self._convert_to_decimal(self.config['short_sl_atr']))
                self.take_profit_price = current_price - (atr * self._convert_to_decimal(self.config['short_tp_atr']))
                await self._enter_position()
                return

            # --- Если ни одно из условий не выполнено ---
            log_info(self.user_id, f"Сигнал для {self.symbol} не соответствует условиям входа. Стратегия остановлена.",
                     "impulse_trailing")
            await self.stop("Signal conditions not met")

        except Exception as e:
            log_error(self.user_id, f"Критическая ошибка в логике стратегии impulse_trailing: {e}", "impulse_trailing")
            await self.stop("Strategy logic error")

    async def _enter_position(self):
        """Вход в позицию, ожидание исполнения и установка SL/TP."""
        await self._set_leverage()
        order_size_usdt = self._convert_to_decimal(self.get_config_value("order_amount", 50.0))
        qty = await self.api.calculate_quantity_from_usdt(self.symbol, order_size_usdt)

        if qty <= 0:
            await self.stop("Calculated order quantity is zero")
            return

        log_info(self.user_id,
                 f"Размещаю ордер на вход для {self.symbol} с SL={self.stop_loss_price} и TP={self.take_profit_price}",
                 "impulse_trailing")
        order_id = await self._place_order(
            side=self.position_side,
            order_type="Market",
            qty=qty,
            stop_loss=self.stop_loss_price,
            take_profit=self.take_profit_price
        )

        if order_id:
            filled = await self._await_order_fill(order_id, side=self.position_side, qty=qty)
            if not filled:
                await self.stop("Failed to fill entry order")
        else:
            await self.stop("Failed to place entry order")

    async def _handle_order_filled(self, event: OrderFilledEvent):
        """Обработка исполненных ордеров с расчетом чистого PnL."""
        log_info(self.user_id, f"[TRACE] ImpulseTrailing._handle_order_filled: side={event.side}, price={event.price}",
                 "impulse_trailing")
        if self.position_side and event.side == self.position_side:
            log_info(self.user_id, "[TRACE] Условие для входа выполнено.", "impulse_trailing")
            self.entry_price = event.price
            self.position_size = event.qty
            self.peak_price = event.price

            # Обновляем состояние позиции в базовом классе
            position_key = f"{self.symbol}_{self.position_side}"
            self.active_positions[position_key] = {
                "symbol": self.symbol,
                "side": self.position_side,
                "size": self.position_size,
                "entry_price": self.entry_price,
                "updated_at": datetime.now()
            }

            intended_amount = self._convert_to_decimal(self.get_config_value("order_amount", 50.0))
            await self._send_trade_open_notification(event.side, event.price, event.qty, intended_amount)
            return

        if self.position_side and event.side != self.position_side:
            pnl_gross = (event.price - self.entry_price) * self.position_size if self.position_side == "Buy" else (
                                                                                                                          self.entry_price - event.price) * self.position_size
            pnl_net = pnl_gross - event.fee
            await self._send_trade_close_notification(pnl_net, event.fee)
            await self.stop("Position closed by TP/SL")

    async def _handle_price_update(self, event: PriceUpdateEvent):
        """Логика трейлинг-стопа при обновлении цены."""
        if not self.position_side:
            return

        # Проверяем, есть ли активная позиция для данного символа
        position_key = f"{self.symbol}_{self.position_side}"
        if position_key not in self.active_positions:
            return
        current_price = event.price
        if self.peak_price is None or (self.position_side == "Buy" and current_price > self.peak_price) or (
                self.position_side == "Sell" and current_price < self.peak_price):
            self.peak_price = current_price
        else:
            return
        analysis_data = self.signal_data.get('analysis_data', {})
        atr = self._convert_to_decimal(analysis_data.get('atr', self.entry_price * Decimal('0.01')))
        trailing_atr_mult = self._convert_to_decimal(self.config.get('trailing_sl_atr', 1.5))
        if self.position_side == "Buy":
            new_stop_price = self.peak_price - (atr * trailing_atr_mult)
            if new_stop_price > self.stop_loss_price:
                self.stop_loss_price = new_stop_price
                await self.api.set_trading_stop(symbol=self.symbol, stop_loss=self.stop_loss_price)
        elif self.position_side == "Sell":
            new_stop_price = self.peak_price + (atr * trailing_atr_mult)
            if new_stop_price < self.stop_loss_price:
                self.stop_loss_price = new_stop_price
                await self.api.set_trading_stop(symbol=self.symbol, stop_loss=self.stop_loss_price)