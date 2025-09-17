from typing import Dict, Any, Optional
from decimal import Decimal, getcontext
from datetime import datetime
import json
from api.bybit_api import BybitAPI
from .base_strategy import BaseStrategy
from core.enums import StrategyType
from core.logger import log_info, log_error, log_warning, log_debug
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

        # Добавляем переменные для агрессивного трейлинга
        self.trailing_active: bool = False
        self.min_profit_threshold_usdt: Optional[Decimal] = None
        self.last_peak_for_step_check: Optional[Decimal] = None # Для проверки минимального шага
        self.position_initiated: bool = False

        # Ключ для блокировки в Redis, уникальный для каждого пользователя
        self.redis_lock_key = f"user:{self.user_id}:impulse_trailing_lock"

    async def start(self) -> bool:
        """Переопределяем start для добавления логики блокировки."""
        if await redis_manager.get_cached_data(self.redis_lock_key):
            log_warning(self.user_id,
                        f"Запуск Impulse Trailing для {self.symbol} отменен: другая impulse-сделка уже активна.",
                        "impulse_trailing")
            return False

        # Устанавливаем ВРЕМЕННУЮ блокировку на время инициализации
        await redis_manager.cache_data(
            self.redis_lock_key,
            json.dumps({"status": "initializing", "symbol": self.symbol}),
            ttl=600  # 10 минут на случай, если что-то пойдет не так до входа в сделку
        )
        log_info(self.user_id, f"Установлена предварительная блокировка Impulse Trailing для символа {self.symbol}.",
                 "impulse_trailing")
        return await super().start()

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

    # impulse_trailing_strategy.py -> _execute_strategy_logic()

    async def _execute_strategy_logic(self):
        """Анализ сигнала и принятие решения о входе с обязательной остановкой, если вход не выполнен."""
        try:
            analysis = self.signal_data
            if not analysis or 'atr' not in analysis:
                log_error(self.user_id, f"Отсутствуют данные анализа для {self.symbol}. Проверьте market_analyzer.",
                          "impulse_trailing")
                await self.stop("Insufficient analysis data in signal")
                return

            # --- ЯВНОЕ ПОЛУЧЕНИЕ ВСЕХ ПАРАМЕТРОВ ---
            current_price = self._convert_to_decimal(analysis['current_price'])
            friction_level = analysis.get('friction_level', 'NEUTRAL')
            ema_trend = analysis.get('ema_trend')
            is_consolidating = analysis.get('is_consolidating_now')
            is_panic = analysis.get('is_panic_bar')
            self.min_profit_threshold_usdt = self._convert_to_decimal(
                self.config.get('min_profit_activation_usdt', 3.0))
            initial_sl_percent = self._convert_to_decimal(
                self.config.get('initial_sl_percent', 0.2))  # Используем 1.5% как в конфиге по умолчанию
            long_breakout_buffer = self._convert_to_decimal(self.config.get('long_breakout_buffer', '0.001'))
            # --- КОНЕЦ БЛОКА ПАРАМЕТРОВ ---

            # --- Логика для СИГНАЛА ЛОНГ (открываем ШОРТ) ---
            if ema_trend == "UP" and is_consolidating:
                if friction_level == "HIGH":
                    await self.stop("Signal skipped: High friction")
                    return
                breakout_level = self._convert_to_decimal(analysis['consolidation_high']) * (1 + long_breakout_buffer)
                if current_price > breakout_level:
                    log_warning(self.user_id, f"ИНВЕРСИЯ: LONG-сигнал для {self.symbol}. Открываю SHORT.",
                                "impulse_trailing")
                    self.position_side = "Sell"
                    self.stop_loss_price = current_price * (1 + initial_sl_percent / 100)
                    # --- ДЕТАЛЬНОЕ ЛОГИРОВАНИЕ РАСЧЕТА ---
                    log_info(self.user_id,
                             f"Расчет SL для SHORT: Цена={current_price:.4f} * (1 + {initial_sl_percent / 100}) = {self.stop_loss_price:.4f}",
                             "impulse_trailing")
                    await self._enter_position()
                    return
                else:
                    await self.stop("Signal skipped: No breakout")
                    return

            # --- Логика для СИГНАЛА ШОРТ (открываем ЛОНГ) ---
            if is_panic:
                if friction_level == "HIGH":
                    await self.stop("Signal skipped: High friction")
                    return
                log_warning(self.user_id, f"ИНВЕРСИЯ: SHORT-сигнал для {self.symbol}. Открываю LONG.",
                            "impulse_trailing")
                self.position_side = "Buy"
                self.stop_loss_price = current_price * (1 - initial_sl_percent / 100)
                # --- ДЕТАЛЬНОЕ ЛОГИРОВАНИЕ РАСЧЕТА ---
                log_info(self.user_id,
                         f"Расчет SL для LONG: Цена={current_price:.4f} * (1 - {initial_sl_percent / 100}) = {self.stop_loss_price:.4f}",
                         "impulse_trailing")
                await self._enter_position()
                return

            # --- Если ни одно из условий не выполнено ---
            await self.stop("Signal conditions not met")

        except Exception as e:
            log_error(self.user_id, f"Критическая ошибка в логике стратегии impulse_trailing: {e}", "impulse_trailing")
            await self.stop("Strategy logic error")

    async def _enter_position(self):
        """Вход в позицию, ожидание исполнения и установка ТОЛЬКО Stop Loss."""
        await self._set_leverage()
        order_size_usdt = self._convert_to_decimal(self.get_config_value("order_amount", 50.0))
        # ИСПРАВЛЕНИЕ: Получаем плечо из конфига и передаем его в функцию
        leverage = self._convert_to_decimal(self.get_config_value("leverage", 1.0))
        qty = await self.api.calculate_quantity_from_usdt(self.symbol, order_size_usdt, leverage)

        if qty <= 0:
            await self.stop("Calculated order quantity is zero")
            return

        log_info(self.user_id,
                 f"Размещаю ордер на вход для {self.symbol} с начальным SL={self.stop_loss_price}",
                 "impulse_trailing")
        order_id = await self._place_order(
            side=self.position_side,
            order_type="Market",
            qty=qty
        )

        if order_id:
            filled = await self._await_order_fill(order_id, side=self.position_side, qty=qty)
            if filled:
                # Устанавливаем SL через отдельный API вызов
                sl_result = await self.api.set_trading_stop(symbol=self.symbol, stop_loss=self.stop_loss_price)
                if sl_result:
                    log_info(self.user_id, f"✅ Начальный SL установлен: {self.stop_loss_price}", "impulse_trailing")
                else:
                    log_error(self.user_id, f"❌ Не удалось установить начальный SL", "impulse_trailing")
            else:
                await self.stop("Failed to fill entry order")
        else:
            await self.stop("Failed to place entry order")

    async def _handle_order_filled(self, event: OrderFilledEvent):
        """Обработка исполненных ордеров с расчетом чистого PnL."""
        log_info(self.user_id, f"[TRACE] ImpulseTrailing._handle_order_filled: side={event.side}, price={event.price}",
                 "impulse_trailing")

        # --- Сценарий: Вход в позицию ---
        if self.position_side and event.side == self.position_side:
            # === НАЧАЛО БЛОКА ЗАЩИТЫ ОТ ДУБЛИРОВАНИЯ ===
            if self.position_initiated:
                log_warning(self.user_id,
                            f"Получено дублирующее событие исполненного ордера {event.order_id}. Игнорируется.",
                            "impulse_trailing")
                return
            self.position_initiated = True
            # === КОНЕЦ БЛОКА ЗАЩИТЫ ===

            self.entry_price = event.price
            self.position_size = event.qty
            self.peak_price = event.price
            self.trailing_active = False
            self.last_peak_for_step_check = event.price

            # Обновление блокировки в Redis с полной информацией о сделке
            try:
                lock_data = {
                    "status": "active", "strategy_id": self.strategy_id, "symbol": self.symbol,
                    "side": self.position_side, "entry_price": str(self.entry_price),
                    "position_size": str(self.position_size), "order_id": event.order_id
                }
                await redis_manager.cache_data(self.redis_lock_key, json.dumps(lock_data), ttl=86400)
                log_info(self.user_id, f"Блокировка Impulse Trailing для {self.symbol} обновлена с деталями активной сделки.",
                         "impulse_trailing")
            except Exception as e:
                log_error(self.user_id, f"Не удалось обновить данные блокировки в Redis: {e}", "impulse_trailing")

            await self._send_trade_open_notification(event.side, event.price, event.qty)
            return

        # --- Сценарий: Закрытие позиции ---
        if self.position_side and event.side != self.position_side:
            pnl_gross = (event.price - self.entry_price) * self.position_size if self.position_side == "Buy" else (self.entry_price - event.price) * self.position_size
            pnl_net = pnl_gross - event.fee
            await self._send_trade_close_notification(pnl_net, event.fee)
            await self.stop("Position closed by TP/SL")

    # impulse_trailing_strategy.py -> _handle_price_update()

    async def _handle_price_update(self, event: PriceUpdateEvent):
        """АГРЕССИВНАЯ логика трейлинг-стопа с активацией по прибыли и закрытием при откате."""
        if not self.is_running or not self.position_initiated or event.symbol != self.symbol:
            return

        # --- НАЧАЛО БЛОКА ДЕТАЛЬНОГО ЛОГИРОВАНИЯ ---
        try:
            current_price = event.price
            if not self.position_side or not self.entry_price or not self.position_size:
                return

            # 1. Расчет текущей прибыли
            order_amount = self._convert_to_decimal(self.config.get("order_amount", 50.0))
            leverage = self._convert_to_decimal(self.config.get("leverage", 1.0))
            price_change_percent = (current_price - self.entry_price) / self.entry_price if self.position_side == "Buy" else (
                    self.entry_price - current_price) / self.entry_price
            current_profit_usdt = price_change_percent * order_amount * leverage
            log_info(self.user_id, f"[{self.symbol}] Trailing Check: Цена={current_price:.4f}, PnL={current_profit_usdt:.2f} USDT", "impulse_trailing")

            # 2. Активация трейлинга
            if not self.trailing_active:
                if current_profit_usdt >= self.min_profit_threshold_usdt:
                    self.trailing_active = True
                    log_info(self.user_id,
                             f"[{self.symbol}] ТРЕЙЛИНГ АКТИВИРОВАН! Прибыль {current_profit_usdt:.2f} >= порога {self.min_profit_threshold_usdt}",
                             "impulse_trailing")
                else:
                    return  # Если трейлинг не активен, дальше не идем

            # 3. Обновление пиковой цены
            if self.peak_price is None: self.peak_price = self.entry_price

            price_improved = False
            if self.position_side == "Buy" and current_price > self.peak_price:
                self.peak_price = current_price
                price_improved = True
                log_info(self.user_id, f"[{self.symbol}] Новый пик LONG: {self.peak_price:.4f}", "impulse_trailing")
            elif self.position_side == "Sell" and current_price < self.peak_price:
                self.peak_price = current_price
                price_improved = True
                log_info(self.user_id, f"[{self.symbol}] Новый пик SHORT: {self.peak_price:.4f}", "impulse_trailing")

            # 4. Проверка отката от пика (приоритет)
            pullback_percent = self._convert_to_decimal(self.config.get('pullback_close_percent', 1.0))
            if self.position_side == "Buy":
                pullback_threshold = self.peak_price * (1 - pullback_percent / 100)
                if current_price <= pullback_threshold:
                    log_warning(self.user_id,
                                f"[{self.symbol}] ЗАКРЫТИЕ ПО ОТКАТУ LONG: Цена {current_price:.4f} <= Порога {pullback_threshold:.4f}",
                                "impulse_trailing")
                    await self._close_position_market("Pullback exceeded")
                    return
            elif self.position_side == "Sell":
                pullback_threshold = self.peak_price * (1 + pullback_percent / 100)
                if current_price >= pullback_threshold:
                    log_warning(self.user_id,
                                f"[{self.symbol}] ЗАКРЫТИЕ ПО ОТКАТУ SHORT: Цена {current_price:.4f} >= Порога {pullback_threshold:.4f}",
                                "impulse_trailing")
                    await self._close_position_market("Pullback exceeded")
                    return

            # 5. Подтягивание стопа, если цена улучшилась и пройден минимальный шаг
            if price_improved:
                min_step_percent = self._convert_to_decimal(self.config.get('min_trailing_step_percent', 0.3))
                step_threshold_passed = False
                if self.position_side == "Buy":
                    step_threshold = self.last_peak_for_step_check * (1 + min_step_percent / 100)
                    if current_price >= step_threshold: step_threshold_passed = True
                else:  # Sell
                    step_threshold = self.last_peak_for_step_check * (1 - min_step_percent / 100)
                    if current_price <= step_threshold: step_threshold_passed = True

                if step_threshold_passed:
                    log_info(self.user_id, f"[{self.symbol}] Пройден минимальный шаг для подтягивания стопа.",
                             "impulse_trailing")
                    self.last_peak_for_step_check = self.peak_price  # Обновляем точку отсчета для шага

                    trailing_distance_percent = self._convert_to_decimal(
                        self.config.get('trailing_distance_percent', 1.2))
                    new_stop_price = self.peak_price * (
                                1 - trailing_distance_percent / 100) if self.position_side == "Buy" else self.peak_price * (
                                1 + trailing_distance_percent / 100)

                    should_update = (new_stop_price > self.stop_loss_price) if self.position_side == "Buy" else (
                                new_stop_price < self.stop_loss_price)

                    if should_update:
                        log_info(self.user_id,
                                 f"[{self.symbol}] ПОДТЯГИВАНИЕ SL: {self.stop_loss_price:.4f} -> {new_stop_price:.4f}",
                                 "impulse_trailing")
                        result = await self.api.set_trading_stop(symbol=self.symbol, stop_loss=new_stop_price)
                        if result:
                            self.stop_loss_price = new_stop_price
                            log_info(self.user_id, f"[{self.symbol}] SL успешно обновлен на бирже.", "impulse_trailing")
                        else:
                            log_error(self.user_id, f"[{self.symbol}] НЕ удалось обновить SL на бирже.",
                                      "impulse_trailing")
        except Exception as e:
            log_error(self.user_id, f"Критическая ошибка в логике трейлинга для {self.symbol}: {e}", "impulse_trailing")

    async def _close_position_market(self, reason: str):
        """Принудительное закрытие позиции рыночным ордером."""
        try:
            if not self.position_side or not self.position_size:
                return

            close_side = "Sell" if self.position_side == "Buy" else "Buy"

            log_info(self.user_id,
                     f"🔄 ПРИНУДИТЕЛЬНОЕ ЗАКРЫТИЕ {self.position_side} позиции {self.symbol}. Причина: {reason}",
                     "impulse_trailing")

            order_id = await self.api.place_order(
                symbol=self.symbol,
                side=close_side,
                order_type="Market",
                qty=self.position_size,
                reduce_only=True
            )

            if order_id:
                # Ждем исполнения
                filled = await self._await_order_fill(order_id, side=close_side, qty=self.position_size)
                if not filled:
                    log_error(self.user_id, f"Не удалось закрыть позицию {self.symbol}", "impulse_trailing")
            else:
                log_error(self.user_id, f"Не удалось разместить ордер на закрытие {self.symbol}", "impulse_trailing")

        except Exception as e:
            log_error(self.user_id, f"Ошибка принудительного закрытия позиции {self.symbol}: {e}", "impulse_trailing")
