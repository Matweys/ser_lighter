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

        # Запрошенная сумма ордера для уведомлений
        self.intended_order_amount: Optional[Decimal] = None

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

        # Обязательные поля для Impulse Trailing стратегии на основе USDT
        required_fields = [
            'initial_sl_usdt',              # Начальный SL в USDT
            'min_profit_activation_usdt',   # Мин. прибыль для активации трейлинга
            'trailing_distance_usdt',       # Расстояние трейлинг-стопа от пика
            'pullback_close_usdt',          # Порог отката от пика для закрытия
            'long_breakout_buffer',         # Буфер для пробоя в лонг
        ]

        for field in required_fields:
            if field not in self.config:
                log_error(self.user_id, f"Отсутствует обязательное поле конфигурации для Impulse Trailing: {field}",
                          module_name=__name__)
                return False

        # Проверяем что все USDT параметры положительные
        usdt_fields = ['initial_sl_usdt', 'min_profit_activation_usdt', 'trailing_distance_usdt', 'pullback_close_usdt']
        for field in usdt_fields:
            value = self.config.get(field, 0)
            if float(value) <= 0:
                log_error(self.user_id, f"Параметр {field} должен быть положительным: {value}",
                          module_name=__name__)
                return False

        return True

    def _get_strategy_type(self) -> StrategyType:
        return StrategyType.IMPULSE_TRAILING


    async def _execute_strategy_logic(self):
        """Анализ сигнала и принятие решения о входе с обязательной остановкой, если вход не выполнен."""
        try:
            # Принудительная перезагрузка конфигурации перед каждым входом в сделку
            await self._force_config_reload()

            analysis = self.signal_data
            if not analysis or 'atr' not in analysis:
                log_error(self.user_id, f"Отсутствуют данные анализа для {self.symbol}. Проверьте market_analyzer.",
                          "impulse_trailing")
                await self.stop("Insufficient analysis data in signal")
                return

            # --- ЯВНОЕ ПОЛУЧЕНИЕ ПАРАМЕТРОВ ---
            current_price = self._convert_to_decimal(analysis['current_price'])
            friction_level = analysis.get('friction_level', 'NEUTRAL')
            ema_trend = analysis.get('ema_trend')
            is_consolidating = analysis.get('is_consolidating_now')
            is_panic = analysis.get('is_panic_bar')
            self.min_profit_threshold_usdt = self._convert_to_decimal(
                self.get_config_value('min_profit_activation_usdt', 3.0))
            long_breakout_buffer = self._convert_to_decimal(self.get_config_value('long_breakout_buffer', '0.0002'))

            # --- ОБЩИЙ БЛОК РАСЧЕТА QTY И SL ---
            order_size_usdt = self._convert_to_decimal(self.get_config_value("order_amount", 50.0))
            leverage = self._convert_to_decimal(self.get_config_value("leverage", 1.0))
            self.intended_order_amount = order_size_usdt  # Сохраняем запрошенную сумму
            qty = await self.api.calculate_quantity_from_usdt(self.symbol, order_size_usdt, leverage,
                                                              price=current_price)

            if not qty or qty <= 0:
                await self.stop("Calculated order quantity is zero")
                return

            initial_sl_usdt = self._convert_to_decimal(self.get_config_value("initial_sl_usdt", 1.5))

            # --- УЛУЧШЕННАЯ ЛОГИКА ДЛЯ LONG СИГНАЛОВ (ранний вход в импульсы) ---
            long_signal_triggered = False
            long_signal_reason = ""

            # 1. Классический пробой консолидации (оригинальная логика)
            if ema_trend == "UP" and is_consolidating:
                breakout_level = self._convert_to_decimal(analysis['consolidation_high']) * (1 + long_breakout_buffer)
                if current_price > breakout_level:
                    long_signal_triggered = True
                    long_signal_reason = "Пробой консолидации"

            # 2. НОВЫЙ: Импульс вверх при восходящем тренде (БЕЗ консолидации)
            elif ema_trend == "UP" and not is_consolidating:
                # Проверяем наличие импульсных сигналов
                volume_spike = analysis.get('volume_spike', False)
                price_momentum = analysis.get('price_momentum', 0)
                momentum_threshold = self._convert_to_decimal(self.get_config_value('momentum_threshold', 0.5))

                # АГРЕССИВНО: Если есть всплеск объема или сильный моментум вверх (более чувствительный)
                if volume_spike or price_momentum > momentum_threshold:
                    long_signal_triggered = True
                    long_signal_reason = "Импульс вверх при восходящем тренде"

            # 3. НОВЫЙ: Отскок от поддержки при восходящем тренде
            elif ema_trend == "UP":
                support_level = analysis.get('support_level')
                if support_level:
                    support_distance = (current_price - self._convert_to_decimal(support_level)) / current_price
                    support_distance_percent = self._convert_to_decimal(self.get_config_value('support_distance_percent', 0.015))
                    # РАСШИРЕНО: Если цена близко к поддержке (широкий диапазон 1.5%) и отскакивает
                    if 0 < support_distance < support_distance_percent:
                        long_signal_triggered = True
                        long_signal_reason = "Отскок от поддержки"

            if long_signal_triggered:
                if friction_level == "HIGH":
                    await self.stop("Signal skipped: High friction")
                    return

                log_info(self.user_id, f"⚠️ ИНВЕРСИЯ: Сигнал LONG -> Вход SHORT для {self.symbol}: {long_signal_reason}", "impulse_trailing")
                self.position_side = "Sell"  # ИНВЕРСИЯ: Сигнал LONG -> Вход SHORT
                # Для SHORT позиции: SL должен быть ВЫШЕ цены входа - используем точный расчет
                self.stop_loss_price = BaseStrategy._calculate_precise_stop_loss(current_price, qty, initial_sl_usdt, False)

                log_info(self.user_id,
                         f"⚠️ ИНВЕРСИЯ: Точный расчет SL для SHORT (на сигнал LONG): Цена={current_price:.4f}, SL={self.stop_loss_price:.4f}, убыток={initial_sl_usdt} USDT",
                         "impulse_trailing")

                await self._enter_position(qty=qty)
                return

            # --- УЛУЧШЕННАЯ ЛОГИКА ДЛЯ SHORT СИГНАЛОВ (ранний вход в импульсы) ---
            short_signal_triggered = False
            short_signal_reason = ""

            # 1. Классические панические бары (оригинальная логика)
            if is_panic:
                short_signal_triggered = True
                short_signal_reason = "Панический бар"

            # 2. НОВЫЙ: Импульс вниз при нисходящем тренде
            elif ema_trend == "DOWN":
                # Проверяем наличие импульсных сигналов вниз
                volume_spike = analysis.get('volume_spike', False)
                price_momentum = analysis.get('price_momentum', 0)
                momentum_threshold = self._convert_to_decimal(self.get_config_value('momentum_threshold', 0.5))

                # АГРЕССИВНО: Ловим ранние нисходящие импульсы (используем тот же порог что и для LONG)
                if volume_spike or price_momentum < -float(momentum_threshold):
                    short_signal_triggered = True
                    short_signal_reason = "Импульс вниз при нисходящем тренде"

            # 3. НОВЫЙ: Пробой консолидации вниз
            elif is_consolidating and ema_trend != "UP":
                consolidation_low = analysis.get('consolidation_low')
                if consolidation_low:
                    breakdown_level = self._convert_to_decimal(consolidation_low) * (1 - long_breakout_buffer)
                    if current_price < breakdown_level:
                        short_signal_triggered = True
                        short_signal_reason = "Пробой консолидации вниз"

            # 4. НОВЫЙ: Отскок от сопротивления при нисходящем тренде
            elif ema_trend == "DOWN":
                resistance_level = analysis.get('resistance_level')
                if resistance_level:
                    resistance_distance = (self._convert_to_decimal(resistance_level) - current_price) / current_price
                    support_distance_percent = self._convert_to_decimal(self.get_config_value('support_distance_percent', 0.015))
                    # РАСШИРЕНО: используем тот же широкий диапазон что и для LONG отскоков
                    if 0 < resistance_distance < support_distance_percent:
                        short_signal_triggered = True
                        short_signal_reason = "Отскок от сопротивления"

            if short_signal_triggered:
                if friction_level == "HIGH":
                    await self.stop("Signal skipped: High friction")
                    return

                log_info(self.user_id, f"⚠️ ИНВЕРСИЯ: Сигнал SHORT -> Вход LONG для {self.symbol}: {short_signal_reason}", "impulse_trailing")
                self.position_side = "Buy"  # ИНВЕРСИЯ: Сигнал SHORT -> Вход LONG
                # Для LONG позиции: SL должен быть ниже цены входа
                self.stop_loss_price = BaseStrategy._calculate_precise_stop_loss(current_price, qty, initial_sl_usdt, True)

                log_info(self.user_id,f"⚠️ ИНВЕРСИЯ: Точный расчет SL для LONG (на сигнал SHORT): Цена={current_price:.4f}, SL={self.stop_loss_price:.4f}, убыток={initial_sl_usdt} USDT",
                         "impulse_trailing")

                await self._enter_position(qty=qty)
                return

            # --- Если ни одно из условий не выполнено ---
            await self.stop("Signal conditions not met")

        except Exception as e:
            log_error(self.user_id, f"Критическая ошибка в логике стратегии impulse_trailing: {e}", "impulse_trailing")
            await self.stop("Strategy logic error")

    async def _enter_position(self, qty: Optional[Decimal] = None):
        """Вход в позицию, ожидание исполнения и установка ТОЛЬКО Stop Loss."""
        await self._set_leverage()

        # Если qty не был рассчитан заранее, вычисляем его сейчас
        if qty is None:
            order_size_usdt = self._convert_to_decimal(self.get_config_value("order_amount", 100.0))
            leverage = self._convert_to_decimal(self.get_config_value("leverage", 2.0))
            qty = await self.api.calculate_quantity_from_usdt(self.symbol, order_size_usdt, leverage)

        if not qty or qty <= 0:
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
                # Важно: используем правильные параметры для stop_loss
                sl_result = await self.api.set_trading_stop(
                    symbol=self.symbol,
                    stop_loss=str(self.stop_loss_price),
                    position_idx=0  # Для hedge mode - укажите правильный индекс если нужно
                )
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

            # КРИТИЧЕСКИ ВАЖНО: Обновляем ордер при исполнении через новый метод
            try:
                from database.db_trades import db_manager
                await db_manager.update_order_on_fill(
                    order_id=event.order_id,
                    filled_quantity=event.qty,
                    average_price=event.price,
                    commission=event.fee,
                    profit=None  # Для OPEN ордера profit = None
                )
                log_debug(self.user_id, f"Ордер {event.order_id} обновлён в БД через update_order_on_fill", "impulse_trailing")
            except Exception as db_error:
                log_error(self.user_id, f"Ошибка обновления ордера {event.order_id} в БД: {db_error}", "impulse_trailing")

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

            # Получаем цену сигнала из signal_data
            signal_price = self._convert_to_decimal(self.signal_data.get('current_price', event.price))
            await self._send_trade_open_notification(event.side, event.price, event.qty, self.intended_order_amount, signal_price)
            return

        # --- Сценарий: Закрытие позиции ---
        if self.position_side and event.side != self.position_side:
            # Рассчитываем PnL для CLOSE ордера
            pnl_gross = (event.price - self.entry_price) * self.position_size if self.position_side == "Buy" else (self.entry_price - event.price) * self.position_size
            pnl_net = pnl_gross - event.fee

            # КРИТИЧЕСКИ ВАЖНО: Обновляем ордер при исполнении с profit для CLOSE ордера
            try:
                from database.db_trades import db_manager
                await db_manager.update_order_on_fill(
                    order_id=event.order_id,
                    filled_quantity=event.qty,
                    average_price=event.price,
                    commission=event.fee,
                    profit=pnl_net  # Для CLOSE ордера передаём рассчитанный profit
                )
                log_debug(self.user_id, f"Ордер {event.order_id} обновлён в БД с profit={pnl_net:.2f}$", "impulse_trailing")
            except Exception as db_error:
                log_error(self.user_id, f"Ошибка обновления ордера {event.order_id} в БД: {db_error}", "impulse_trailing")

            await self._send_trade_close_notification(pnl_net, event.fee)
            await self.stop("Position closed by TP/SL")

    async def handle_price_update(self, event: PriceUpdateEvent):
        """Обработка обновления цены (реализация абстрактного метода)"""
        await self._handle_price_update(event)

    async def _handle_price_update(self, event: PriceUpdateEvent):
        """
        ФИНАЛЬНАЯ ВЕРСИЯ 3.2. Надежный трейлинг на основе USDT с закрытием по откату.
        """
        if not self.is_running or not self.position_initiated or event.symbol != self.symbol:
            return

        try:
            current_price = event.price
            if not self.position_side or not self.entry_price or not self.position_size:
                return

            # --- Шаг 1: Расчет текущей прибыли в USDT ---
            current_profit_usdt = (
                                              current_price - self.entry_price) * self.position_size if self.position_side == "Buy" else (
                                                                                                                                                     self.entry_price - current_price) * self.position_size

            log_debug(self.user_id,
                      f"[{self.symbol}] Trailing Check: Цена={current_price:.4f}, PnL={current_profit_usdt:.2f} USDT",
                      "impulse_trailing")

            # --- Шаг 2: Активация трейлинга ---
            if not self.trailing_active:
                min_profit_activation_usdt = self._convert_to_decimal(
                    self.get_config_value('min_profit_activation_usdt', 2.0))
                if current_profit_usdt >= min_profit_activation_usdt:
                    self.trailing_active = True
                    self.peak_price = current_price  # Инициализируем пик
                    log_info(self.user_id,
                             f"[{self.symbol}] ТРЕЙЛИНГ АКТИВИРОВАН! Прибыль {current_profit_usdt:.2f} >= порога {min_profit_activation_usdt}",
                             "impulse_trailing")
                else:
                    return  # Если трейлинг не активен и порог не достигнут, выходим.

            # --- Шаг 3: Основная логика активного трейлинга ---
            if self.trailing_active:
                # 3.1 Проверяем, достигла ли цена нового пика
                price_improved = (self.position_side == "Buy" and current_price > self.peak_price) or \
                                 (self.position_side == "Sell" and current_price < self.peak_price)

                if price_improved:
                    self.peak_price = current_price
                    log_info(self.user_id, f"[{self.symbol}] Новый пик {self.position_side}: {self.peak_price:.4f}",
                             "impulse_trailing")

                    # 3.2 Рассчитываем и подтягиваем стоп-лосс
                    trailing_distance_usdt = self._convert_to_decimal(
                        self.get_config_value('trailing_distance_usdt', 2.0))
                    price_offset = trailing_distance_usdt / self.position_size

                    # Правильный расчет нового SL в зависимости от направления позиции
                    if self.position_side == "Buy":
                        new_stop_price = self.peak_price - price_offset
                    else:  # Sell position
                        new_stop_price = self.peak_price + price_offset

                    # Проверяем, нужно ли обновлять SL (только в лучшую сторону)
                    should_update_sl = False
                    if self.position_side == "Buy" and new_stop_price > self.stop_loss_price:
                        should_update_sl = True
                    elif self.position_side == "Sell" and new_stop_price < self.stop_loss_price:
                        should_update_sl = True

                    if should_update_sl:
                        log_info(self.user_id,
                                 f"[{self.symbol}] ПОДТЯГИВАНИЕ SL: {self.stop_loss_price:.4f} -> {new_stop_price:.4f}",
                                 "impulse_trailing")

                        sl_result = await self.api.set_trading_stop(
                            symbol=self.symbol,
                            stop_loss=str(new_stop_price),
                            position_idx=0
                        )

                        if sl_result:
                            self.stop_loss_price = new_stop_price
                            log_info(self.user_id, f"[{self.symbol}] SL успешно обновлен на бирже.", "impulse_trailing")
                        else:
                            log_error(self.user_id, f"[{self.symbol}] НЕ удалось обновить SL на бирже.",
                                      "impulse_trailing")

                # 3.3 ВАЖНО: Проверка на откат от пика для закрытия по рынку
                pullback_close_usdt = self._convert_to_decimal(self.get_config_value('pullback_close_usdt', 3.0))

                # Расчет размера отката от пика
                if self.position_side == "Buy":
                    pullback_amount = (self.peak_price - current_price) * self.position_size
                else:  # Sell position
                    pullback_amount = (current_price - self.peak_price) * self.position_size

                # Проверяем, что откат от пика превысил порог
                if pullback_amount >= pullback_close_usdt:
                    log_warning(self.user_id,
                                f"[{self.symbol}] ЗАКРЫТИЕ ПО ОТКАТУ: Откат от пика {pullback_amount:.2f} USDT >= порога {pullback_close_usdt} USDT. Пик: {self.peak_price:.4f}, Текущая: {current_price:.4f}",
                                "impulse_trailing")
                    await self._close_position_market("Pullback from peak exceeded")
                    # Важно! После отправки рыночного ордера на закрытие выходим из функции
                    return

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
