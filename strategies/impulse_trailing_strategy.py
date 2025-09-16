from typing import Dict, Any, Optional
from decimal import Decimal, getcontext
from datetime import datetime
import json
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

        # Добавляем переменные для агрессивного трейлинга
        self.trailing_active: bool = False
        self.min_profit_threshold_usdt: Optional[Decimal] = None
        self.last_peak_for_step_check: Optional[Decimal] = None  # Для проверки минимального шага

        # ДОБАВЛЕНО: Ключ для блокировки в Redis, уникальный для каждого пользователя
        self.redis_lock_key = f"user:{self.user_id}:impulse_trailing_lock"


    async def start(self) -> bool:
        """Переопределяем start для добавления логики блокировки."""
        if await redis_manager.get_cached_data(self.redis_lock_key):
            log_warning(self.user_id, f"Запуск Impulse Trailing для {self.symbol} отменен: другая impulse-сделка уже активна.", "impulse_trailing")
            return False

        # Устанавливаем ВРЕМЕННУЮ блокировку на время инициализации
        await redis_manager.cache_data(
            self.redis_lock_key,
            json.dumps({"status": "initializing", "symbol": self.symbol}),
            ttl=600  # 10 минут на случай, если что-то пойдет не так до входа в сделку
        )
        log_info(self.user_id, f"Установлена предварительная блокировка Impulse Trailing для символа {self.symbol}.",
                 "impulse_trailing")

        # Подписываемся на события цены в EventBus
        from core.enums import EventType
        await self.event_bus.subscribe(
            event_type=EventType.PRICE_UPDATE,
            handler=self._handle_price_update,
            user_id=self.user_id
        )
        log_info(self.user_id, f"🔔 Подписка на события цены в EventBus активирована", "impulse_trailing")

        return await super().start()

    # ДОБАВЛЕНО: Переопределяем stop для снятия блокировки
    async def stop(self, reason: str = "Manual stop"):
        """Переопределяем stop для гарантированного снятия блокировки."""
        # 1. Снимаем блокировку, чтобы освободить слот для следующей сделки
        await redis_manager.delete_cached_data(self.redis_lock_key)
        log_info(self.user_id, f"Снята блокировка Impulse Trailing. Причина: {reason}", "impulse_trailing")

        # Отписываемся от событий при остановке
        try:
            await self.event_bus.unsubscribe(self._handle_price_update)
            log_info(self.user_id, f"🔕 Отписка от событий цены выполнена", "impulse_trailing")
        except Exception as e:
            log_error(self.user_id, f"Ошибка отписки от событий: {e}", "impulse_trailing")

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
            analysis = self.signal_data
            if not analysis or 'atr' not in analysis:
                log_error(self.user_id, f"Отсутствуют данные анализа для {self.symbol}. Проверьте market_analyzer.",
                          "impulse_trailing")
                await self.stop("Insufficient analysis data in signal")
                return

            current_price = self._convert_to_decimal(analysis['current_price'])
            friction_level = analysis.get('friction_level', 'NEUTRAL')
            ema_trend = analysis.get('ema_trend')
            is_consolidating = analysis.get('is_consolidating_now')
            is_panic = analysis.get('is_panic_bar')


            # --- Логика для СИГНАЛА ЛОНГ (открываем ШОРТ) ---
            # Устанавливаем минимальный порог прибыли в USDT для активации трейлинга
            self.min_profit_threshold_usdt = self._convert_to_decimal(
                self.config.get('min_profit_activation_usdt', 3.0))

            if ema_trend == "UP" and is_consolidating:
                if friction_level == "HIGH":
                    await self.stop("Signal skipped: High friction")
                    return
                breakout_level = self._convert_to_decimal(analysis['consolidation_high']) * (
                        1 + self._convert_to_decimal(self.config.get('long_breakout_buffer', '0.001')))
                if current_price > breakout_level:
                    log_warning(self.user_id, f"ИНВЕРСИЯ: LONG-сигнал для {self.symbol}. Открываю SHORT.",
                                "impulse_trailing")
                    self.position_side = "Sell"  # <-- ИНВЕРСИЯ НАПРАВЛЕНИЯ
                    # Используем процентный стоп-лосс вместо ATR
                    initial_sl_percent = self._convert_to_decimal(self.config.get('initial_sl_percent', 3.0))
                    self.stop_loss_price = current_price * (1 + initial_sl_percent / 100)
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
                self.position_side = "Buy"  # <-- ИНВЕРСИЯ НАПРАВЛЕНИЯ
                # Используем процентный стоп-лосс вместо ATR
                initial_sl_percent = self._convert_to_decimal(self.config.get('initial_sl_percent', 3.0))
                self.stop_loss_price = current_price * (1 - initial_sl_percent / 100)
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

        # --- Сценарий: Вход в позицию (ордер на покупку) ---
        if self.position_side and event.side == self.position_side:
            self.entry_price = event.price
            self.position_size = event.qty
            self.peak_price = event.price

            # Инициализируем переменные агрессивного трейлинга
            self.trailing_active = False
            self.last_peak_for_step_check = event.price

            # >>> НОВЫЙ БЛОК: ОБНОВЛЕНИЕ БЛОКИРОВКИ ПОЛНОЙ ИНФОРМАЦИЕЙ О СДЕЛКЕ <<<
            try:
                lock_data = {
                    "status": "active",
                    "strategy_id": self.strategy_id,
                    "symbol": self.symbol,
                    "side": self.position_side,
                    "entry_price": str(self.entry_price),
                    "position_size": str(self.position_size),
                    "order_id": event.order_id
                }
                # Сохраняем на 24 часа. Если сделка длится дольше, это уже аномалия.
                await redis_manager.cache_data(self.redis_lock_key, json.dumps(lock_data), ttl=86400)
                log_info(self.user_id,
                         f"Блокировка Impulse Trailing для {self.symbol} обновлена с деталями активной сделки.",
                         "impulse_trailing")
            except Exception as e:
                log_error(self.user_id, f"Не удалось обновить данные блокировки в Redis: {e}", "impulse_trailing")
            # --- КОНЕЦ НОВОГО БЛОКА ---

            await self._send_trade_open_notification(event.side, event.price, event.qty)

            # ИСПРАВЛЕНИЕ: Подписываемся на обновления цены через глобальный websocket_manager
            try:
                # Используем глобальный экземпляр websocket_manager
                from websocket.websocket_manager import GlobalWebSocketManager
                # Создаем временный экземпляр для подписки (он подключится к существующему соединению)
                temp_ws_manager = GlobalWebSocketManager(self.event_bus)
                await temp_ws_manager.subscribe_symbol(self.user_id, self.symbol)
                log_info(self.user_id, f"🔔 Подписка на обновления цены {self.symbol} активирована", "impulse_trailing")
            except Exception as e:
                log_error(self.user_id, f"❌ Ошибка подписки на обновления цены {self.symbol}: {e}", "impulse_trailing")

            return

        # --- Сценарий: Закрытие позиции (ордер на продажу) ---
        if self.position_side and event.side != self.position_side:
            pnl_gross = (event.price - self.entry_price) * self.position_size if self.position_side == "Buy" else (self.entry_price - event.price) * self.position_size
            pnl_net = pnl_gross - event.fee
            await self._send_trade_close_notification(pnl_net, event.fee)
            await self.stop("Position closed by TP/SL")

    async def _handle_price_update(self, event: PriceUpdateEvent):
        """АГРЕССИВНАЯ логика трейлинг-стопа с активацией по прибыли и закрытием при откате."""
        # ДИАГНОСТИКА: Логируем каждое обновление цены
        log_info(self.user_id, f"🔍 PRICE UPDATE: {self.symbol} = {event.price}", "impulse_trailing")

        if not self.position_side or not self.entry_price:
            log_info(self.user_id, f"⏸️ Нет активной позиции для трейлинга {self.symbol}", "impulse_trailing")
            return

        current_price = event.price

        # 1. РАСЧЕТ ТЕКУЩЕЙ ПРИБЫЛИ С УЧЕТОМ ПЛЕЧА
        order_amount = self._convert_to_decimal(self.config.get("order_amount", 50.0))
        leverage = self._convert_to_decimal(self.config.get("leverage", 1.0))

        if self.position_side == "Buy":
            price_change_percent = (current_price - self.entry_price) / self.entry_price
            current_profit_usdt = price_change_percent * order_amount * leverage
        elif self.position_side == "Sell":
            price_change_percent = (self.entry_price - current_price) / self.entry_price
            current_profit_usdt = price_change_percent * order_amount * leverage
        else:
            return
        # ДИАГНОСТИКА: Логируем расчет прибыли
        current_profit_usdt = price_change_percent * order_amount * leverage
        log_info(self.user_id,
                 f"💰 Текущая прибыль: {current_profit_usdt:.2f} USDT ({price_change_percent * 100:.2f}%)",
                 "impulse_trailing")
        # конец временной диагностики

        # 2. АКТИВАЦИЯ ТРЕЙЛИНГА ПРИ ДОСТИЖЕНИИ МИНИМАЛЬНОЙ ПРИБЫЛИ
        if not self.trailing_active:
            if current_profit_usdt >= self.min_profit_threshold_usdt:
                self.trailing_active = True
                log_info(self.user_id,
                         f"🎯 ТРЕЙЛИНГ АКТИВИРОВАН! Прибыль {current_profit_usdt:.2f} >= {self.min_profit_threshold_usdt}",
                         "impulse_trailing")
            else:
                log_info(self.user_id,
                         f"⏳ Трейлинг НЕ активен. Прибыль {current_profit_usdt:.2f} < {self.min_profit_threshold_usdt}",
                         "impulse_trailing")
                return

        # 3. ИНИЦИАЛИЗАЦИЯ ПИКОВОЙ ЦЕНЫ
        if self.peak_price is None:
            self.peak_price = self.entry_price

        # 4. ПРОВЕРКА ОТКАТА ОТ ПИКА (ПРИОРИТЕТНАЯ ПРОВЕРКА)
        pullback_percent = self._convert_to_decimal(self.config.get('pullback_close_percent', 0.7))

        if self.position_side == "Buy":
            pullback_threshold = self.peak_price * (1 - pullback_percent / 100)
            if current_price <= pullback_threshold:
                pullback_actual = ((self.peak_price - current_price) / self.peak_price * 100)
                log_info(self.user_id,
                         f"🚨 ЗАКРЫТИЕ ПО ОТКАТУ LONG {self.symbol}: откат {pullback_actual:.2f}% от пика {self.peak_price:.6f}",
                         "impulse_trailing")
                await self._close_position_market("Pullback exceeded threshold")
                return
        elif self.position_side == "Sell":
            pullback_threshold = self.peak_price * (1 + pullback_percent / 100)
            if current_price >= pullback_threshold:
                pullback_actual = ((current_price - self.peak_price) / self.peak_price * 100)
                log_info(self.user_id,
                         f"🚨 ЗАКРЫТИЕ ПО ОТКАТУ SHORT {self.symbol}: откат {pullback_actual:.2f}% от пика {self.peak_price:.6f}",
                         "impulse_trailing")
                await self._close_position_market("Pullback exceeded threshold")
                return

        # 5. ОБНОВЛЕНИЕ ПИКОВОЙ ЦЕНЫ И ПОДТЯГИВАНИЕ СТОПА
        price_improved = False
        min_step_percent = self._convert_to_decimal(self.config.get('min_trailing_step_percent', 0.2))

        if self.position_side == "Buy" and current_price > self.peak_price:
            # Проверяем минимальный шаг для подтягивания
            step_threshold = self.last_peak_for_step_check * (1 + min_step_percent / 100)
            if current_price >= step_threshold:
                old_peak = self.peak_price
                self.peak_price = current_price
                self.last_peak_for_step_check = current_price
                price_improved = True
                log_info(self.user_id, f"📈 НОВЫЙ ПИК для LONG {self.symbol}: {old_peak:.6f} → {self.peak_price:.6f}",
                         "impulse_trailing")
        elif self.position_side == "Sell" and current_price < self.peak_price:
            # Проверяем минимальный шаг для подтягивания
            step_threshold = self.last_peak_for_step_check * (1 - min_step_percent / 100)
            if current_price <= step_threshold:
                old_peak = self.peak_price
                self.peak_price = current_price
                self.last_peak_for_step_check = current_price
                price_improved = True
                log_info(self.user_id, f"📉 НОВЫЙ ПИК для SHORT {self.symbol}: {old_peak:.6f} → {self.peak_price:.6f}",
                         "impulse_trailing")

        # 6. ПОДТЯГИВАНИЕ СТОП-ЛОССА (только при улучшении пика)
        if price_improved:
            trailing_distance_percent = self._convert_to_decimal(self.config.get('trailing_distance_percent', 0.8))

            new_stop_price = None
            should_update = False

            if self.position_side == "Buy":
                new_stop_price = self.peak_price * (1 - trailing_distance_percent / 100)
                should_update = new_stop_price > self.stop_loss_price
            elif self.position_side == "Sell":
                new_stop_price = self.peak_price * (1 + trailing_distance_percent / 100)
                should_update = new_stop_price < self.stop_loss_price

            if should_update and new_stop_price:
                log_info(self.user_id,
                         f"🔄 ПОДТЯГИВАНИЕ SL для {self.position_side} {self.symbol}: {self.stop_loss_price:.6f} → {new_stop_price:.6f}",
                         "impulse_trailing")

                old_stop_price = self.stop_loss_price
                self.stop_loss_price = new_stop_price

                try:
                    result = await self.api.set_trading_stop(symbol=self.symbol, stop_loss=self.stop_loss_price)
                    if result:
                        log_info(self.user_id, f"✅ Стоп-лосс обновлен на бирже для {self.symbol}", "impulse_trailing")
                    else:
                        self.stop_loss_price = old_stop_price
                        log_error(self.user_id, f"❌ Не удалось обновить стоп-лосс для {self.symbol}",
                                  "impulse_trailing")
                except Exception as e:
                    self.stop_loss_price = old_stop_price
                    log_error(self.user_id, f"❌ Ошибка обновления стоп-лосса для {self.symbol}: {e}",
                              "impulse_trailing")

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
