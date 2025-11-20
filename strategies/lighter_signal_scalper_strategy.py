"""
Lighter Signal Scalper Strategy
Полная адаптация SignalScalperStrategy для биржи Lighter
"""
import asyncio
import time
from typing import Dict, Any, Optional
from decimal import Decimal, getcontext
from datetime import datetime, timezone

from api.lighter_simulator import LighterSimulator
from strategies.base_strategy import BaseStrategy
from core.enums import StrategyType, EventType
from core.logger import log_info, log_error, log_warning, log_debug
from core.events import EventBus, PriceUpdateEvent, OrderFilledEvent
from analysis.lighter_signal_analyzer import LighterSignalAnalyzer, SignalAnalysisResult
from analysis.spike_detector import SpikeDetector
from core.concurrency_manager import strategy_locked
from database.sqlite_db import sqlite_db

getcontext().prec = 28


class LighterSignalScalperStrategy(BaseStrategy):
    """
    Стратегия скальпинга для Lighter биржи
    Основана на внешних сигналах от LighterSignalAnalyzer (EMA + RSI)
    """
    
    def __init__(self, user_id: int, symbol: str, signal_data: Dict[str, Any], 
                 api: LighterSimulator, event_bus: EventBus, bot: "Bot", 
                 config: Optional[Dict] = None, account_priority: int = 1, data_feed=None):
        super().__init__(user_id, symbol, signal_data, api, event_bus, bot, config, account_priority, data_feed)
        
        # Компоненты
        self.signal_analyzer: Optional[LighterSignalAnalyzer] = None
        self.spike_detector: Optional[SpikeDetector] = None
        
        # Состояние стратегии
        self.position_active = False
        self.active_direction: Optional[str] = None
        self.entry_price: Optional[Decimal] = None
        self.entry_time: Optional[datetime] = None
        self.position_size: Optional[Decimal] = None
        self.last_closed_direction: Optional[str] = None
        self.hold_signal_counter = 0
        self.peak_profit_usd: Decimal = Decimal('0')
        self.is_waiting_for_trade = False
        self.processed_orders: set = set()
        self.current_order_id: Optional[str] = None
        self.intended_order_amount: Optional[Decimal] = None
        self.close_reason: Optional[str] = None
        self._last_known_price: Optional[Decimal] = None
        self.signal_price: Optional[Decimal] = None
        
        # Стоп-лосс управление
        self.stop_loss_order_id: Optional[str] = None
        self.stop_loss_price: Optional[Decimal] = None
        
        # Система подтверждения сигналов и кулдауна
        self.last_signal: Optional[str] = None
        self.signal_confirmation_count = 0
        self.required_confirmations = 2
        self.last_trade_close_time: Optional[float] = None
        self.cooldown_seconds = 60
        self.last_trade_was_loss = False
        
        # Система усреднения
        self.averaging_enabled = False
        self.averaging_executed = False
        self.averaging_count = 0
        self.max_averaging_count = 1
        self.averaging_trigger_loss_percent = Decimal('15.0')
        self.averaging_multiplier = Decimal('1.0')
        self.averaging_stop_loss_percent = Decimal('55.0')
        self.total_position_size = Decimal('0')
        self.average_entry_price = Decimal('0')
        self.initial_margin_usd = Decimal('0')
        self.current_total_margin = Decimal('0')
        self.total_fees_paid = Decimal('0')
        
        # Изоляция настроек
        self.active_trade_config = None
        self.config_frozen = False
        
        # Задачи
        self._price_monitor_task: Optional[asyncio.Task] = None
        self._signal_check_task: Optional[asyncio.Task] = None
        
        log_info(user_id, f"LighterSignalScalperStrategy инициализирована для {symbol}", "LighterSignalScalper")
    
    def _get_strategy_type(self) -> StrategyType:
        return StrategyType.SIGNAL_SCALPER  # Используем тот же тип для совместимости
    
    async def start(self):
        """Запуск стратегии"""
        await super().start()
        
        # Инициализация компонентов
        await self._init_components()
        
        # Запуск мониторинга цены
        self._price_monitor_task = asyncio.create_task(self._price_monitor_loop())
        
        # Запуск цикла проверки сигналов
        self._signal_check_task = asyncio.create_task(self._signal_check_loop())
        
        log_info(self.user_id, f"✅ LighterSignalScalperStrategy запущена для {self.symbol}", "LighterSignalScalper")
    
    async def stop(self):
        """Остановка стратегии"""
        if self._price_monitor_task:
            self._price_monitor_task.cancel()
            try:
                await self._price_monitor_task
            except asyncio.CancelledError:
                pass
        
        if self._signal_check_task:
            self._signal_check_task.cancel()
            try:
                await self._signal_check_task
            except asyncio.CancelledError:
                pass
        
        await super().stop()
        log_info(self.user_id, f"LighterSignalScalperStrategy остановлена для {self.symbol}", "LighterSignalScalper")
    
    async def _init_components(self):
        """Инициализация анализаторов"""
        try:
            await self._force_config_reload()
            
            # Signal Analyzer
            self.signal_analyzer = LighterSignalAnalyzer(self.user_id, self.api, self.config)
            
            # Spike Detector
            self.spike_detector = SpikeDetector(
                user_id=self.user_id,
                symbol=self.symbol,
                lookback=50,
                threshold=0.0005
            )
            
            log_info(self.user_id, "✅ Компоненты инициализированы", "LighterSignalScalper")
        except Exception as e:
            log_error(self.user_id, f"Ошибка инициализации компонентов: {e}", "LighterSignalScalper")
    
    async def _price_monitor_loop(self):
        """
        Основной цикл мониторинга цены (каждую секунду)
        Заменяет WebSocket PriceUpdateEvent для Lighter
        """
        try:
            while self.is_running:
                if self.position_active and not self.is_waiting_for_trade:
                    try:
                        # Получаем текущую цену
                        current_price = await self.api.get_current_price(self.symbol)
                        
                        if current_price and current_price > 0:
                            self._last_known_price = current_price
                            await self._handle_price_update_internal(current_price)
                    except Exception as e:
                        log_error(self.user_id, f"Ошибка в цикле мониторинга цены: {e}", "LighterSignalScalper")
                
                await asyncio.sleep(1.0)  # Обновление каждую секунду
                
        except asyncio.CancelledError:
            log_info(self.user_id, "Мониторинг цены остановлен", "LighterSignalScalper")
        except Exception as e:
            log_error(self.user_id, f"Критическая ошибка в цикле мониторинга: {e}", "LighterSignalScalper")
    
    async def _handle_price_update_internal(self, current_price: Decimal):
        """Обработка обновления цены (аналог handle_price_update)"""
        if not self.position_active or not self.entry_price:
            return
        
        entry_price_to_use, position_size_to_use = self._get_effective_entry_data()
        pnl = self._calculate_pnl_gross(entry_price_to_use, current_price, position_size_to_use, self.active_direction)
        
        # Обновление пика прибыли
        if pnl > self.peak_profit_usd:
            self.peak_profit_usd = pnl
        
        # Проверка усреднения
        if pnl < 0 and not self.averaging_executed:
            loss_percent = (abs(pnl) / self.initial_margin_usd * Decimal('100')) if self.initial_margin_usd > 0 else Decimal('0')
            if loss_percent >= self.averaging_trigger_loss_percent:
                await self._execute_averaging(current_price)
        
        # Трейлинг стоп
        if self.peak_profit_usd > 0:
            trailing_distance = self.peak_profit_usd * Decimal('0.20')
            if pnl < (self.peak_profit_usd - trailing_distance):
                log_info(self.user_id,
                        f"💎 ЗАКРЫТИЕ ПО ТРЕЙЛИНГУ! Пик: ${self.peak_profit_usd:.2f}, PnL: ${pnl:.2f}, откат: ${trailing_distance:.2f}",
                        "LighterSignalScalper")
                await self._close_position("trailing_stop")
    
    async def _signal_check_loop(self):
        """
        Цикл проверки сигналов (каждые 5 минут при новой свече)
        """
        try:
            while self.is_running:
                if not self.position_active and not self.is_waiting_for_trade:
                    # Проверяем сигнал
                    await self._check_and_process_signal()
                
                # Ждем 5 минут до следующей проверки
                await asyncio.sleep(300)
                
        except asyncio.CancelledError:
            log_info(self.user_id, "Цикл проверки сигналов остановлен", "LighterSignalScalper")
        except Exception as e:
            log_error(self.user_id, f"Ошибка в цикле проверки сигналов: {e}", "LighterSignalScalper")
    
    async def _check_and_process_signal(self):
        """Проверка и обработка сигнала"""
        try:
            if not self.signal_analyzer:
                return
            
            analysis_result = await self.signal_analyzer.get_analysis(self.symbol)
            
            if not analysis_result:
                return
            
            signal = analysis_result.direction
            price = analysis_result.price
            
            # Проверка кулдауна
            if self._is_cooldown_active():
                return
            
            # Проверка подтверждения
            if not self._is_signal_confirmed(signal):
                return
            
            # Проверка SpikeDetector
            if self.spike_detector:
                should_enter, final_signal, spike_reason = self.spike_detector.should_enter_on_pullback(signal)
                if not should_enter:
                    log_info(self.user_id, f"⏸️ Spike Detector блокирует вход: {spike_reason}", "LighterSignalScalper")
                    return
                signal = final_signal
            
            # Вход в позицию
            if signal in ["LONG", "SHORT"]:
                await self._enter_position(signal, price)
                
        except Exception as e:
            log_error(self.user_id, f"Ошибка проверки сигнала: {e}", "LighterSignalScalper")
    
    async def _enter_position(self, direction: str, signal_price: Decimal):
        """Вход в позицию"""
        if self.position_active:
            log_warning(self.user_id, f"⚠️ Позиция уже активна, игнорирую сигнал {direction}", "LighterSignalScalper")
            return
        
        self.is_waiting_for_trade = True
        self.signal_price = signal_price
        
        try:
            await self._force_config_reload()
            
            # Замораживаем конфигурацию
            self.active_trade_config = self.config.copy()
            self.config_frozen = True
            
            # Получаем параметры
            order_amount = self._convert_to_decimal(self.config.get("order_amount", 200.0))
            self.intended_order_amount = order_amount
            
            # Размещаем ордер
            side = "Buy" if direction == "LONG" else "Sell"
            order_result = await self.api.place_market_order(side, float(order_amount))
            
            if not order_result:
                log_error(self.user_id, "Не удалось разместить ордер", "LighterSignalScalper")
                self.is_waiting_for_trade = False
                return
            
            # Ждем исполнения и получаем позицию
            await asyncio.sleep(2.0)  # Даем время на исполнение
            
            positions = await self.api.get_positions(self.symbol)
            
            if positions:
                pos = positions[0]
                self.position_active = True
                self.active_direction = direction
                self.entry_price = pos["avgPrice"]
                self.position_size = pos["size"]
                self.entry_time = datetime.now(timezone.utc)
                self.peak_profit_usd = Decimal('0')
                
                # Инициализация усреднения
                self.averaging_executed = False
                self.total_position_size = Decimal('0')
                self.average_entry_price = Decimal('0')
                self.initial_margin_usd = order_amount
                self.current_total_margin = order_amount
                # Симулированная комиссия из результата ордера
                self.total_fees_paid = Decimal(str(order_result.get("commission", 0)))
                
                # Сохранение в БД
                await self._save_trade_to_db(side, self.entry_price, self.position_size)
                
                # Уведомление
                await self._send_trade_open_notification(side, self.entry_price, self.position_size, order_amount, signal_price)
                
                log_info(self.user_id, f"✅ Позиция открыта: {direction} @ ${self.entry_price:.4f}", "LighterSignalScalper")
            
            self.is_waiting_for_trade = False
            
        except Exception as e:
            log_error(self.user_id, f"Ошибка входа в позицию: {e}", "LighterSignalScalper")
            self.is_waiting_for_trade = False
    
    async def _close_position(self, reason: str):
        """Закрытие позиции"""
        if not self.position_active:
            return
        
        try:
            self.is_waiting_for_trade = True
            self.close_reason = reason
            
            # Получаем текущую цену
            exit_price = self._last_known_price if self._last_known_price else self.entry_price
            
            # Размещаем ордер закрытия (противоположное направление)
            close_side = "Sell" if self.active_direction == "LONG" else "Buy"
            
            # Получаем размер позиции
            positions = await self.api.get_positions(self.symbol)
            if positions:
                pos_size = positions[0]["size"]
                
                # Для Lighter нужно закрыть через reduce_only ордер
                # Используем текущую цену как целевую (market close)
                order_result = await self.api.place_market_order(close_side, float(pos_size * exit_price))
                
                if order_result:
                    await asyncio.sleep(2.0)
                    
                    # Проверяем закрытие
                    positions_after = await self.api.get_positions(self.symbol)
                    if not positions_after or all(abs(p["size"]) < 1e-12 for p in positions_after):
                        # Позиция закрыта
                        await self._handle_position_closed(exit_price)
            
            self.is_waiting_for_trade = False
            
        except Exception as e:
            log_error(self.user_id, f"Ошибка закрытия позиции: {e}", "LighterSignalScalper")
            self.is_waiting_for_trade = False
    
    async def _handle_position_closed(self, exit_price: Decimal):
        """Обработка закрытой позиции"""
        try:
            # Расчет PnL
            entry_price_to_use, position_size_to_use = self._get_effective_entry_data()
            pnl_gross = self._calculate_pnl_gross(entry_price_to_use, exit_price, position_size_to_use, self.active_direction)
            
            # Добавляем комиссию закрытия (симулируем)
            close_commission = Decimal(str(position_size_to_use * exit_price * Decimal('0.00055')))
            total_commission = self.total_fees_paid + close_commission
            
            pnl_net = pnl_gross - total_commission
            
            # Обновление БД
            if hasattr(self, 'active_trade_db_id') and self.active_trade_db_id:
                from database.sqlite_db import sqlite_db
                # Рассчитываем общую комиссию (вход + усреднения + выход)
                close_commission = Decimal(str(position_size_to_use * exit_price * Decimal('0.00055')))
                total_commission = self.total_fees_paid + close_commission
                
                await sqlite_db.update_trade_on_close(
                    trade_id=self.active_trade_db_id,
                    exit_price=exit_price,
                    pnl=pnl_net,
                    commission=total_commission,
                    exit_time=datetime.now(timezone.utc),
                    bot_priority=self.account_priority
                )
            
            # Уведомление
            close_commission = Decimal(str(position_size_to_use * exit_price * Decimal('0.00055')))
            total_commission = self.total_fees_paid + close_commission
            
            await self._send_trade_close_notification(
                pnl=pnl_net,
                commission=total_commission,
                exit_price=exit_price,
                entry_price=entry_price_to_use,
                entry_time=self.entry_time
            )
            
            # Сброс состояния
            self._reset_position_state_after_close(pnl_net)
            
            log_info(self.user_id, f"✅ Позиция закрыта: PnL=${pnl_net:.2f}, причина={self.close_reason}", "LighterSignalScalper")
            
        except Exception as e:
            log_error(self.user_id, f"Ошибка обработки закрытия: {e}", "LighterSignalScalper")
    
    async def _execute_averaging(self, current_price: Decimal):
        """Выполнение усреднения"""
        if not self.averaging_enabled or self.averaging_count >= self.max_averaging_count:
            return
        
        try:
            self.is_waiting_for_trade = True
            
            order_amount = self._convert_to_decimal(self._get_frozen_config_value("order_amount", 200.0))
            averaging_amount = order_amount * self.averaging_multiplier
            
            side = "Buy" if self.active_direction == "LONG" else "Sell"
            order_result = await self.api.place_market_order(side, float(averaging_amount))
            
            if order_result:
                await asyncio.sleep(2.0)
                
                # Обновляем среднюю цену
                positions = await self.api.get_positions(self.symbol)
                if positions:
                    new_pos = positions[0]
                    new_size = new_pos["size"]
                    new_avg_price = new_pos["avgPrice"]
                    
                    # Пересчет средней цены
                    if self.total_position_size == 0:
                        self.total_position_size = new_size
                        self.average_entry_price = new_avg_price
                    else:
                        old_value = self.average_entry_price * self.total_position_size
                        new_value = new_avg_price * (new_size - self.total_position_size)
                        self.total_position_size = new_size
                        self.average_entry_price = (old_value + new_value) / new_size
                    
                    self.averaging_count += 1
                    self.averaging_executed = True
                    self.current_total_margin += averaging_amount
                    
                    # Добавляем комиссию усреднения
                    averaging_commission = Decimal(str(order_result.get("commission", 0)))
                    self.total_fees_paid += averaging_commission
                    
                    await self._send_averaging_notification(
                        price=new_avg_price,
                        quantity=new_size - self.position_size,
                        new_avg_price=self.average_entry_price,
                        new_total_size=self.total_position_size,
                        side=side,
                        old_entry_price=self.entry_price,
                        old_size=self.position_size,
                        current_pnl=self._calculate_pnl_gross(self.entry_price, current_price, self.position_size, self.active_direction),
                        loss_percent=Decimal('0'),
                        trigger_percent=self.averaging_trigger_loss_percent,
                        averaging_amount=averaging_amount
                    )
            
            self.is_waiting_for_trade = False
            
        except Exception as e:
            log_error(self.user_id, f"Ошибка усреднения: {e}", "LighterSignalScalper")
            self.is_waiting_for_trade = False
    
    def _get_effective_entry_data(self) -> tuple[Decimal, Decimal]:
        """Возвращает актуальные данные позиции с учётом усреднений"""
        entry_price = self.average_entry_price if self.average_entry_price > 0 else self.entry_price
        position_size = self.total_position_size if self.total_position_size > 0 else self.position_size
        return entry_price or Decimal('0'), position_size or Decimal('0')
    
    def _is_signal_confirmed(self, signal: str) -> bool:
        """Проверка подтверждения сигнала"""
        if signal == self.last_signal:
            self.signal_confirmation_count += 1
        else:
            self.last_signal = signal
            self.signal_confirmation_count = 1
        
        required = self.required_confirmations
        if self.last_trade_was_loss:
            required = max(required, 2)
        
        return self.signal_confirmation_count >= required
    
    def _is_cooldown_active(self) -> bool:
        """Проверка кулдауна"""
        if self.last_trade_close_time is None:
            return False
        
        time_since_close = time.time() - self.last_trade_close_time
        return time_since_close < self.cooldown_seconds
    
    def _reset_position_state_after_close(self, pnl_net: Decimal):
        """Сброс состояния после закрытия"""
        self.last_closed_direction = self.active_direction
        self.last_trade_close_time = time.time()
        self.last_trade_was_loss = pnl_net < 0
        
        self.position_active = False
        self.active_direction = None
        self.entry_price = None
        self.entry_time = None
        self.position_size = None
        self.peak_profit_usd = Decimal('0')
        
        self.averaging_executed = False
        self.averaging_count = 0
        self.total_position_size = Decimal('0')
        self.average_entry_price = Decimal('0')
        self.initial_margin_usd = Decimal('0')
        self.current_total_margin = Decimal('0')
        self.total_fees_paid = Decimal('0')
        
        self.config_frozen = False
        self.active_trade_config = None
        
        self.signal_confirmation_count = 0
        self.last_signal = None
    
    async def _save_trade_to_db(self, side: str, entry_price: Decimal, quantity: Decimal):
        """Сохранение сделки в БД"""
        try:
            from database.sqlite_db import sqlite_db
            
            trade_data = {
                'user_id': self.user_id,
                'symbol': self.symbol,
                'side': side,
                'entry_price': entry_price,
                'exit_price': Decimal('0'),
                'quantity': quantity,
                'leverage': int(float(self.config.get("leverage", 1))),
                'profit': Decimal('0'),
                'commission': Decimal('0'),
                'status': 'ACTIVE',
                'strategy_type': self.strategy_type.value,
                'bot_priority': self.account_priority,
                'entry_time': datetime.now(timezone.utc)
            }
            
            trade_id = await sqlite_db.save_trade(trade_data)
            if trade_id:
                self.active_trade_db_id = trade_id
                log_info(self.user_id, f"✅ Сделка сохранена в SQLite: trade_id={trade_id}", "LighterSignalScalper")
        except Exception as e:
            log_error(self.user_id, f"Ошибка сохранения сделки в БД: {e}", "LighterSignalScalper")
    
    # Реализация абстрактных методов BaseStrategy
    
    async def _execute_strategy_logic(self):
        """Основная логика стратегии (управляется событиями свечей)"""
        # Логика управляется через мониторинг цены и обработку сигналов
        pass
    
    async def handle_price_update(self, event: "PriceUpdateEvent"):
        """Обработка обновления цены из EventBus"""
        # КРИТИЧЕСКИ ВАЖНО: Проверяем что это цена НАШЕГО символа!
        if event.symbol != self.symbol:
            return
        
        if not self.position_active or not self.entry_price or self.is_waiting_for_trade:
            return
        
        current_price = event.price
        
        # Защита от неправильных цен
        if current_price <= 0:
            log_warning(self.user_id, f"⚠️ Получена недопустимая цена: {current_price}", "LighterSignalScalper")
            return
        
        # Вызываем внутренний обработчик
        await self._handle_price_update_internal(current_price)
    
    async def _handle_order_filled(self, event: "OrderFilledEvent"):
        """Обработка исполнения ордера"""
        # КРИТИЧНО: АТОМАРНАЯ ЗАЩИТА ОТ RACE CONDITION!
        if event.order_id in self.processed_orders:
            log_debug(self.user_id, f"[ДУПЛИКАТ] Ордер {event.order_id} уже обработан, игнорируем EventBus дубликат.", "LighterSignalScalper")
            return
        
        # АТОМАРНО добавляем в set
        self.processed_orders.add(event.order_id)
        log_debug(self.user_id, f"🔒 Ордер {event.order_id} заблокирован от повторной обработки", "LighterSignalScalper")
        
        # MULTI-ACCOUNT: РАННЯЯ фильтрация по bot_priority
        if hasattr(event, 'bot_priority') and event.bot_priority is not None:
            if event.bot_priority != self.account_priority:
                log_debug(self.user_id,
                         f"[РАННИЙ ФИЛЬТР] Событие для Bot_{event.bot_priority}, а это Bot_{self.account_priority}. ИГНОРИРУЮ.",
                         "LighterSignalScalper")
                return
        
        # Проверяем что ордер принадлежит БОТУ (есть в БД)
        try:
            order_in_db = await sqlite_db.get_open_order_for_position(self.active_trade_db_id)
            
            if not order_in_db or order_in_db.get('order_id') != event.order_id:
                log_warning(self.user_id,
                           f"⚠️ [НЕ НАШ ОРДЕР] Ордер {event.order_id} НЕ найден в БД бота! ИГНОРИРУЮ.",
                           "LighterSignalScalper")
                return
            
            # Обработка исполнения ордера
            log_info(self.user_id, f"✅ Ордер {event.order_id} исполнен: {event.side} {event.filled_qty} @ {event.filled_price}", "LighterSignalScalper")
            
            # Обновляем информацию о позиции если это вход
            if event.side in ["Buy", "Sell"] and not self.position_active:
                # Это вход в позицию
                self.position_active = True
                self.active_direction = "LONG" if event.side == "Buy" else "SHORT"
                self.entry_price = Decimal(str(event.filled_price))
                self.position_size = Decimal(str(event.filled_qty))
                self.entry_time = datetime.now(timezone.utc)
                
                log_info(self.user_id, 
                        f"✅ Позиция открыта: {self.active_direction} {self.position_size} @ {self.entry_price}",
                        "LighterSignalScalper")
            elif event.side in ["Buy", "Sell"] and self.position_active:
                # Это выход из позиции
                exit_price = Decimal(str(event.filled_price))
                await self._handle_position_closed(exit_price)
        
        except Exception as e:
            log_error(self.user_id, f"Ошибка обработки исполнения ордера {event.order_id}: {e}", "LighterSignalScalper")

