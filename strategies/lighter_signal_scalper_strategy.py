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
        self.max_trailing_level_reached: int = 0  # Максимальный достигнутый уровень трейлинга
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
        self._status_notification_task: Optional[asyncio.Task] = None
        
        # Отслеживание времени для уведомлений о работе
        self.last_signal_time: Optional[float] = None
        self.last_status_notification_time: Optional[float] = None
        
        log_info(user_id, f"LighterSignalScalperStrategy инициализирована для {symbol}", "LighterSignalScalper")
    
    def _get_strategy_type(self) -> StrategyType:
        return StrategyType.SIGNAL_SCALPER  # Используем тот же тип для совместимости
    
    async def _load_strategy_config(self):
        """
        Переопределяем загрузку конфигурации для Lighter.
        Используем уже переданную конфигурацию, если она есть.
        Если конфигурация не передана, пытаемся загрузить из Redis (как в базовом классе).
        """
        # Если конфигурация уже установлена (передана в конструктор), используем её
        if self.config:
            log_info(self.user_id, f"✅ Используется переданная конфигурация для {self.symbol}", "LighterSignalScalper")
            return
        
        # Если конфигурации нет, пытаемся загрузить из Redis (fallback)
        log_info(self.user_id, f"⚠️ Конфигурация не передана, пытаемся загрузить из Redis...", "LighterSignalScalper")
        try:
            await super()._load_strategy_config()
            if self.config:
                log_info(self.user_id, f"✅ Конфигурация загружена из Redis", "LighterSignalScalper")
            else:
                log_warning(self.user_id, f"⚠️ Конфигурация не найдена в Redis, используем конфигурацию по умолчанию", "LighterSignalScalper")
                # Используем конфигурацию по умолчанию
                from core.default_configs import DefaultConfigs
                self.config = DefaultConfigs.get_signal_scalper_config()
        except Exception as e:
            log_error(self.user_id, f"❌ Ошибка загрузки конфигурации из Redis: {e}, используем конфигурацию по умолчанию", "LighterSignalScalper")
            # Используем конфигурацию по умолчанию
            from core.default_configs import DefaultConfigs
            self.config = DefaultConfigs.get_signal_scalper_config()
    
    async def start(self):
        """Запуск стратегии"""
        log_info(self.user_id, f"🚀 Начало запуска LighterSignalScalperStrategy для {self.symbol}, is_running={self.is_running}", "LighterSignalScalper")
        
        result = await super().start()
        log_info(self.user_id, f"📊 super().start() завершен, результат={result}, is_running={self.is_running}", "LighterSignalScalper")
        
        if not result:
            log_error(self.user_id, f"❌ super().start() вернул False, стратегия не запущена", "LighterSignalScalper")
            return False
        
        if not self.is_running:
            log_error(self.user_id, f"❌ is_running=False после super().start(), стратегия не запущена", "LighterSignalScalper")
            return False
        
        # Инициализация компонентов
        await self._init_components()
        log_info(self.user_id, f"✅ Компоненты инициализированы, is_running={self.is_running}", "LighterSignalScalper")
        
        # Восстановление позиций при перезапуске
        await self._restore_positions_on_startup()
        
        # Запуск мониторинга цены
        self._price_monitor_task = asyncio.create_task(self._price_monitor_loop())
        log_info(self.user_id, f"✅ Задача мониторинга цены создана, is_running={self.is_running}", "LighterSignalScalper")
        
        # Запуск цикла проверки сигналов
        self._signal_check_task = asyncio.create_task(self._signal_check_loop())
        log_info(self.user_id, f"✅ Задача проверки сигналов создана, is_running={self.is_running}", "LighterSignalScalper")
        
        # Запуск задачи для уведомлений о работе
        self._status_notification_task = asyncio.create_task(self._status_notification_loop())
        log_info(self.user_id, f"✅ Задача уведомлений создана, is_running={self.is_running}", "LighterSignalScalper")
        
        # Инициализируем время последнего сигнала текущим временем
        self.last_signal_time = time.time()
        
        log_info(self.user_id, f"✅ LighterSignalScalperStrategy запущена для {self.symbol}, is_running={self.is_running}", "LighterSignalScalper")
        return True
    
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
        
        if self._status_notification_task:
            self._status_notification_task.cancel()
            try:
                await self._status_notification_task
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
    
    async def _restore_positions_on_startup(self):
        """
        Восстанавливает активные позиции при перезапуске бота.
        Проверяет открытые сделки в SQLite и позиции на бирже.
        """
        try:
            log_info(self.user_id, f"🔍 Проверка активных позиций для {self.symbol}...", "LighterSignalScalper")
            
            # Получаем активные сделки из SQLite
            async with sqlite_db.conn.execute("""
                SELECT id, side, entry_price, quantity, leverage, entry_time
                FROM trades
                WHERE user_id = ? AND symbol = ? AND status = 'ACTIVE' AND strategy_type = ?
                ORDER BY created_at DESC
                LIMIT 1
            """, (self.user_id, self.symbol, self.strategy_type.value)) as cursor:
                row = await cursor.fetchone()
                
                if not row:
                    log_info(self.user_id, f"✅ Активных позиций в БД для {self.symbol} не найдено", "LighterSignalScalper")
                    return
                
                trade_id, side, entry_price, quantity, leverage, entry_time_str = row
                log_info(self.user_id, f"📊 Найдена активная сделка в БД: trade_id={trade_id}, side={side}", "LighterSignalScalper")
                
                # Проверяем позиции на бирже
                positions = await self.api.get_positions(self.symbol)
                
                if positions and any(abs(p["size"]) > 1e-12 for p in positions):
                    # Позиция найдена на бирже - восстанавливаем состояние
                    pos = positions[0]
                    pos_size = pos["size"]
                    pos_avg_price = pos["avgPrice"]
                    
                    # Определяем направление
                    if pos_size > 0:
                        direction = "LONG"
                        expected_side = "Buy"
                    else:
                        direction = "SHORT"
                        expected_side = "Sell"
                        pos_size = abs(pos_size)
                    
                    # Проверяем соответствие стороны
                    if side.lower() != expected_side.lower():
                        log_warning(self.user_id, 
                                   f"⚠️ Несоответствие: БД side={side}, биржа direction={direction}", 
                                   "LighterSignalScalper")
                    
                    # Восстанавливаем состояние стратегии из позиции на бирже
                    self.position_active = True
                    self.active_direction = direction
                    self.entry_price = Decimal(str(pos_avg_price))
                    self.position_size = Decimal(str(pos_size))
                    self.active_trade_db_id = trade_id
                    self.is_waiting_for_trade = False
                    
                    # Восстанавливаем время входа
                    if entry_time_str:
                        try:
                            from datetime import datetime
                            self.entry_time = datetime.fromisoformat(entry_time_str.replace('Z', '+00:00'))
                        except:
                            self.entry_time = datetime.now(timezone.utc)
                    else:
                        self.entry_time = datetime.now(timezone.utc)
                    
                    # Восстанавливаем начальную маржу
                    leverage_decimal = Decimal(str(leverage))
                    position_value = self.entry_price * self.position_size
                    self.initial_margin_usd = position_value / leverage_decimal
                    
                    # Восстанавливаем пик прибыли и уровень трейлинга на основе текущего PnL
                    current_price = await self.api.get_current_price(self.symbol)
                    if current_price:
                        current_pnl = self._calculate_pnl_gross(self.entry_price, current_price, self.position_size, direction)
                        if current_pnl > 0:
                            # Устанавливаем текущий PnL как начальный пик (будет обновляться при мониторинге)
                            self.peak_profit_usd = current_pnl
                            # Определяем максимальный достигнутый уровень на основе текущего PnL
                            self.max_trailing_level_reached = self._get_trailing_level(current_pnl)
                            log_info(self.user_id,
                                    f"📊 Восстановлен пик прибыли: ${self.peak_profit_usd:.2f}, уровень: {self.max_trailing_level_reached}",
                                    "LighterSignalScalper")
                        else:
                            self.peak_profit_usd = Decimal('0')
                            self.max_trailing_level_reached = 0
                    else:
                        self.peak_profit_usd = Decimal('0')
                        self.max_trailing_level_reached = 0
                    
                    log_info(self.user_id,
                            f"✅ Позиция восстановлена из биржи: {direction} @ ${self.entry_price:.4f}, размер={self.position_size:.4f}, маржа=${self.initial_margin_usd:.2f}",
                            "LighterSignalScalper")
                else:
                    # Позиции на бирже нет, но в БД есть - восстанавливаем из БД (для симулятора)
                    log_info(self.user_id, f"⚠️ Позиции на бирже нет, восстанавливаем из БД", "LighterSignalScalper")
                    pos_size = float(quantity)
                    pos_avg_price = float(entry_price)
                    
                    # Определяем направление из БД
                    if side.lower() == "buy":
                        direction = "LONG"
                        expected_side = "Buy"
                    else:
                        direction = "SHORT"
                        expected_side = "Sell"
                    
                    # Восстанавливаем позицию в симуляторе из БД
                    if isinstance(self.api, LighterSimulator):
                        self.api.simulated_position = {
                            "symbol": self.symbol,
                            "side": side.lower(),
                            "size": float(pos_size) if direction == "LONG" else -float(pos_size),
                            "avg_price": float(pos_avg_price),
                            "avgPrice": float(pos_avg_price)
                        }
                        log_info(self.user_id, f"✅ Позиция восстановлена в симуляторе из БД", "LighterSignalScalper")
                    
                    # Восстанавливаем состояние стратегии из БД
                    self.position_active = True
                    self.active_direction = direction
                    self.entry_price = Decimal(str(pos_avg_price))
                    self.position_size = Decimal(str(pos_size))
                    self.active_trade_db_id = trade_id
                    self.is_waiting_for_trade = False
                    
                    # Восстанавливаем время входа
                    if entry_time_str:
                        try:
                            from datetime import datetime
                            self.entry_time = datetime.fromisoformat(entry_time_str.replace('Z', '+00:00'))
                        except:
                            self.entry_time = datetime.now(timezone.utc)
                    else:
                        self.entry_time = datetime.now(timezone.utc)
                    
                    # Восстанавливаем начальную маржу
                    leverage_decimal = Decimal(str(leverage))
                    position_value = self.entry_price * self.position_size
                    self.initial_margin_usd = position_value / leverage_decimal
                    
                    # Восстанавливаем пик прибыли и уровень трейлинга на основе текущего PnL
                    current_price = await self.api.get_current_price(self.symbol)
                    if current_price:
                        current_pnl = self._calculate_pnl_gross(self.entry_price, current_price, self.position_size, direction)
                        if current_pnl > 0:
                            # Устанавливаем текущий PnL как начальный пик (будет обновляться при мониторинге)
                            self.peak_profit_usd = current_pnl
                            # Определяем максимальный достигнутый уровень на основе текущего PnL
                            self.max_trailing_level_reached = self._get_trailing_level(current_pnl)
                            log_info(self.user_id,
                                    f"📊 Восстановлен пик прибыли: ${self.peak_profit_usd:.2f}, уровень: {self.max_trailing_level_reached}",
                                    "LighterSignalScalper")
                        else:
                            self.peak_profit_usd = Decimal('0')
                            self.max_trailing_level_reached = 0
                    else:
                        self.peak_profit_usd = Decimal('0')
                        self.max_trailing_level_reached = 0
                    
                    log_info(self.user_id,
                            f"✅ Позиция восстановлена из БД: {direction} @ ${self.entry_price:.4f}, размер={self.position_size:.4f}, маржа=${self.initial_margin_usd:.2f}",
                            "LighterSignalScalper")
                    
        except Exception as e:
            log_error(self.user_id, f"Ошибка восстановления позиций: {e}", "LighterSignalScalper")
    
    async def _price_monitor_loop(self):
        """
        Основной цикл мониторинга цены (каждую секунду)
        Заменяет WebSocket PriceUpdateEvent для Lighter
        """
        try:
            log_info(self.user_id, "🔄 Цикл мониторинга цены запущен", "LighterSignalScalper")
            iteration = 0
            
            while self.is_running:
                if self.position_active and not self.is_waiting_for_trade:
                    try:
                        # Получаем текущую цену
                        current_price = await self.api.get_current_price(self.symbol)
                        
                        if current_price and current_price > 0:
                            self._last_known_price = current_price
                            await self._handle_price_update_internal(current_price)
                            
                            # Логируем каждые 10 секунд для отладки
                            iteration += 1
                            if iteration % 10 == 0:
                                entry_price_to_use, position_size_to_use = self._get_effective_entry_data()
                                pnl = self._calculate_pnl_gross(entry_price_to_use, current_price, position_size_to_use, self.active_direction)
                                log_info(self.user_id, 
                                        f"📊 Мониторинг цены: {self.symbol} @ ${current_price:.4f}, PnL=${pnl:.2f}, пик=${self.peak_profit_usd:.2f}",
                                        "LighterSignalScalper")
                    except Exception as e:
                        log_error(self.user_id, f"Ошибка в цикле мониторинга цены: {e}", "LighterSignalScalper")
                elif self.position_active:
                    log_debug(self.user_id, f"⏸️ Мониторинг пропущен: is_waiting_for_trade={self.is_waiting_for_trade}", "LighterSignalScalper")
                
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
        
        # Проверка усреднения
        if pnl < 0 and not self.averaging_executed:
            loss_percent = (abs(pnl) / self.initial_margin_usd * Decimal('100')) if self.initial_margin_usd > 0 else Decimal('0')
            if loss_percent >= self.averaging_trigger_loss_percent:
                await self._execute_averaging(current_price)
        
        # ЛОГИКА ТРЕЙЛИНГ-СТОПА С 6 СТУПЕНЯМИ
        # Обновляем пиковую прибыль
        if pnl > self.peak_profit_usd:
            self.peak_profit_usd = pnl
        
        # Поэтапный трейлинг с динамическими порогами и 20% откатом
        current_trailing_level = self._get_trailing_level(pnl)
        
        # Обновляем максимальный достигнутый уровень
        if current_trailing_level > self.max_trailing_level_reached:
            self.max_trailing_level_reached = current_trailing_level
        
        # ИСПРАВЛЕНО: Проверяем трейлинг-стоп если пик был достигнут (независимо от текущего уровня)
        # Это важно, чтобы позиция закрылась при откате, даже если текущий PnL упал ниже уровня 1
        if self.peak_profit_usd > 0 and self.max_trailing_level_reached > 0:
            # Фиксированный 20% откат от пика на всех уровнях
            trailing_distance = self.peak_profit_usd * Decimal('0.20')
            
            # Проверяем условие закрытия: откат от пика >= 20%
            if pnl < (self.peak_profit_usd - trailing_distance):
                level_name = self._get_level_name(self.max_trailing_level_reached)
                log_info(self.user_id,
                        f"💎 ЗАКРЫТИЕ НА {level_name}! Пик: ${self.peak_profit_usd:.2f}, PnL: ${pnl:.2f}, откат: ${trailing_distance:.2f} (20%)",
                        "LighterSignalScalper")
                await self._close_position("level_trailing_profit")
            else:
                # Логируем текущий статус трейлинга
                level_name = self._get_level_name(current_trailing_level if current_trailing_level > 0 else self.max_trailing_level_reached)
                log_debug(self.user_id,
                         f"Трейлинг {level_name}: пик=${self.peak_profit_usd:.2f}, PnL=${pnl:.2f}, откат допустим=${trailing_distance:.2f}",
                         "LighterSignalScalper")
    
    async def _signal_check_loop(self):
        """
        Цикл проверки сигналов (каждые 5 минут при новой свече)
        """
        try:
            log_info(self.user_id, "🔄 Цикл проверки сигналов запущен", "LighterSignalScalper")
            
            # Ждем, пока стратегия полностью запустится (is_running станет True)
            max_wait = 10  # Максимум 10 секунд ожидания
            wait_count = 0
            while not self.is_running and wait_count < max_wait:
                await asyncio.sleep(0.5)
                wait_count += 0.5
                log_info(self.user_id, f"⏳ Ожидание запуска стратегии... is_running={self.is_running} (ждем {wait_count:.1f}с)", "LighterSignalScalper")
            
            if not self.is_running:
                log_error(self.user_id, "❌ Стратегия не запустилась за 10 секунд, останавливаем цикл проверки сигналов", "LighterSignalScalper")
                return
            
            log_info(self.user_id, f"✅ Стратегия запущена, is_running={self.is_running}, начинаем проверку сигналов", "LighterSignalScalper")
            
            # Первая проверка сразу при запуске
            if not self.position_active and not self.is_waiting_for_trade:
                log_info(self.user_id, f"🔍 Первая проверка сигнала для {self.symbol}...", "LighterSignalScalper")
                await self._check_and_process_signal()
                log_info(self.user_id, "✅ Первая проверка завершена, переходим в цикл", "LighterSignalScalper")
            else:
                log_info(self.user_id, f"⏸️ Пропуск первой проверки: позиция активна={self.position_active}, ожидание={self.is_waiting_for_trade}", "LighterSignalScalper")
            
            iteration = 0
            while self.is_running:
                log_info(self.user_id, f"✅ Вход в цикл while, is_running={self.is_running}", "LighterSignalScalper")
                iteration += 1
                log_info(self.user_id, f"🔄 Итерация цикла проверки сигналов #{iteration}", "LighterSignalScalper")
                
                if not self.position_active and not self.is_waiting_for_trade:
                    log_info(self.user_id, f"🔍 Проверка сигнала для {self.symbol}...", "LighterSignalScalper")
                    # Проверяем сигнал
                    await self._check_and_process_signal()
                    log_info(self.user_id, "✅ Проверка сигнала завершена", "LighterSignalScalper")
                else:
                    log_info(self.user_id, f"⏸️ Пропуск проверки: позиция активна={self.position_active}, ожидание={self.is_waiting_for_trade}", "LighterSignalScalper")
                
                # Ждем 5 минут до следующей проверки
                log_info(self.user_id, "⏳ Ожидание 5 минут до следующей проверки сигнала...", "LighterSignalScalper")
                await asyncio.sleep(300)
                log_info(self.user_id, "⏰ 5 минут прошло, следующая проверка...", "LighterSignalScalper")
                
        except asyncio.CancelledError:
            log_info(self.user_id, "Цикл проверки сигналов остановлен", "LighterSignalScalper")
        except Exception as e:
            log_error(self.user_id, f"Ошибка в цикле проверки сигналов: {e}", "LighterSignalScalper")
    
    async def _status_notification_loop(self):
        """
        Цикл отправки уведомлений о работе бота
        Отправляет уведомление, если прошёл час без сигналов
        """
        try:
            # Ждём 10 минут перед первой проверкой
            await asyncio.sleep(600)
            
            while self.is_running:
                try:
                    current_time = time.time()
                    
                    # Проверяем, прошёл ли час с последнего сигнала
                    if self.last_signal_time:
                        time_since_last_signal = current_time - self.last_signal_time
                        one_hour = 3600  # 1 час в секундах
                        
                        # Если прошёл час без сигналов и не отправляли уведомление в последний час
                        if time_since_last_signal >= one_hour:
                            time_since_last_notification = current_time - (self.last_status_notification_time or 0)
                            
                            # Отправляем уведомление не чаще раза в час
                            if time_since_last_notification >= one_hour:
                                await self._send_status_notification()
                                self.last_status_notification_time = current_time
                    
                    # Проверяем каждые 10 минут
                    await asyncio.sleep(600)
                    
                except Exception as e:
                    log_error(self.user_id, f"Ошибка в цикле уведомлений о статусе: {e}", "LighterSignalScalper")
                    await asyncio.sleep(600)
                    
        except asyncio.CancelledError:
            log_info(self.user_id, "Цикл уведомлений о статусе остановлен", "LighterSignalScalper")
        except Exception as e:
            log_error(self.user_id, f"Критическая ошибка в цикле уведомлений о статусе: {e}", "LighterSignalScalper")
    
    async def _send_status_notification(self):
        """Отправка уведомления о том, что бот работает, но сигналов нет"""
        try:
            from datetime import datetime
            
            moscow_time = datetime.now().strftime("%Y-%m-%d %H:%M:%S MSK")
            hours_without_signal = int((time.time() - self.last_signal_time) / 3600) if self.last_signal_time else 0
            
            status_message = (
                f"⏰ <b>Бот работает</b>\n\n"
                f"📊 <b>Символ:</b> <code>{self.symbol}</code>\n"
                f"⏰ <b>Время:</b> {moscow_time}\n"
                f"🔍 <b>Статус:</b> Мониторинг активен, сигналов нет\n"
                f"⏳ <b>Без сигналов:</b> {hours_without_signal} ч.\n\n"
                f"✅ Бот продолжает работу и ждёт подходящих сигналов"
            )
            
            await self._send_notification_async(status_message, parse_mode="HTML")
            log_info(self.user_id, f"✅ Отправлено уведомление о работе бота (без сигналов {hours_without_signal} ч.)", "LighterSignalScalper")
            
        except Exception as e:
            log_error(self.user_id, f"Ошибка отправки уведомления о статусе: {e}", "LighterSignalScalper")
    
    async def _check_and_process_signal(self):
        """Проверка и обработка сигнала"""
        try:
            if not self.signal_analyzer:
                log_warning(self.user_id, "⚠️ SignalAnalyzer не инициализирован", "LighterSignalScalper")
                return
            
            log_info(self.user_id, f"📊 Запрос анализа для {self.symbol}...", "LighterSignalScalper")
            analysis_result = await self.signal_analyzer.get_analysis(self.symbol)
            
            if not analysis_result:
                log_info(self.user_id, f"⚠️ Анализ не вернул результат для {self.symbol} (недостаточно данных)", "LighterSignalScalper")
                return
            
            signal = analysis_result.direction
            price = analysis_result.price
            
            log_info(self.user_id, 
                    f"📈 Анализ {self.symbol}: Сигнал={signal}, Цена={price:.4f}, EMA_short={analysis_result.indicators.get('ema_short', 0):.4f}, EMA_long={analysis_result.indicators.get('ema_long', 0):.4f}, RSI={analysis_result.indicators.get('rsi', 0):.2f}",
                    "LighterSignalScalper")
            
            # Обновляем время последнего сигнала (даже если это HOLD)
            self.last_signal_time = time.time()
            
            # Проверка кулдауна
            if self._is_cooldown_active():
                log_info(self.user_id, f"⏸️ Кулдаун активен, пропускаем сигнал {signal}", "LighterSignalScalper")
                return
            
            # Проверка подтверждения
            if not self._is_signal_confirmed(signal):
                log_info(self.user_id, f"⏸️ Сигнал {signal} не подтвержден (требуется {self.required_confirmations} подтверждений, текущее: {self.signal_confirmation_count})", "LighterSignalScalper")
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
                self.max_trailing_level_reached = 0
                
                # Инициализация усреднения
                self.averaging_executed = False
                self.total_position_size = Decimal('0')
                self.average_entry_price = Decimal('0')
                self.initial_margin_usd = order_amount
                self.current_total_margin = order_amount
                # Симулированная комиссия из результата ордера
                self.total_fees_paid = Decimal(str(order_result.get("commission", 0)))
                
                # КРИТИЧНО: Загружаем параметры усреднения из ЗАМОРОЖЕННОЙ конфигурации
                if self.active_trade_config:
                    self.averaging_enabled = self.active_trade_config.get("enable_averaging", True)
                    self.max_averaging_count = int(self.active_trade_config.get("max_averaging_count", 1))
                    self.averaging_trigger_loss_percent = self._convert_to_decimal(self.active_trade_config.get("averaging_trigger_loss_percent", "15.0"))
                    self.averaging_multiplier = self._convert_to_decimal(self.active_trade_config.get("averaging_multiplier", "1.0"))
                    self.averaging_stop_loss_percent = self._convert_to_decimal(self.active_trade_config.get("averaging_stop_loss_percent", "55.0"))
                    
                    log_info(self.user_id,
                            f"🔧 Параметры усреднения загружены:\n"
                            f"   📊 Усреднение: {'✅ ВКЛ' if self.averaging_enabled else '❌ ВЫКЛ'}\n"
                            f"      ├─ Триггер: {self.averaging_trigger_loss_percent}% от маржи\n"
                            f"      ├─ Множитель: {self.averaging_multiplier}x\n"
                            f"      ├─ Максимум усреднений: {self.max_averaging_count}\n"
                            f"      └─ SL после усреднений: {self.averaging_stop_loss_percent}%",
                            "LighterSignalScalper")
                
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
            if exit_price is None:
                exit_price = await self.api.get_current_price(self.symbol)
                if exit_price is None:
                    log_error(self.user_id, "Не удалось получить цену для закрытия позиции", "LighterSignalScalper")
                    self.is_waiting_for_trade = False
                    return
            
            # Убеждаемся, что exit_price - Decimal
            exit_price = self._convert_to_decimal(exit_price)
            
            # Размещаем ордер закрытия (противоположное направление)
            close_side = "Sell" if self.active_direction == "LONG" else "Buy"
            
            # Получаем размер позиции
            positions = await self.api.get_positions(self.symbol)
            if positions:
                # Конвертируем pos_size в Decimal для корректных вычислений
                pos_size_raw = positions[0]["size"]
                pos_size = self._convert_to_decimal(pos_size_raw)
                
                # Для Lighter нужно закрыть через reduce_only ордер
                # Используем текущую цену как целевую (market close)
                # Вычисляем notional в Decimal, затем конвертируем в float для API
                notional_usd = pos_size * exit_price
                order_result = await self.api.place_market_order(close_side, float(notional_usd))
                
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
        self.max_trailing_level_reached = 0
        
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
    
    async def _send_trade_close_notification(self, pnl: Decimal, commission: Decimal = Decimal('0'), exit_price: Optional[Decimal] = None, entry_price: Optional[Decimal] = None, entry_time: Optional[datetime] = None):
        """
        Переопределяем метод для использования SQLite вместо PostgreSQL.
        """
        try:
            # 1. Обновляем статистику самой стратегии
            self.stats["orders_count"] += 1
            self.stats["total_pnl"] += pnl
            if pnl > 0:
                self.stats["profit_orders"] += 1
            else:
                self.stats["loss_orders"] += 1

            # 2. Обновляем статистику по этой стратегии в SQLite
            from database.sqlite_db import sqlite_db
            win_rate = await sqlite_db.update_strategy_stats(
                user_id=self.user_id,
                strategy_type=self.strategy_type.value,
                pnl=pnl
            )

            # 3. Отправляем уведомление, если бот доступен
            if not self.bot:
                log_error(self.user_id, "Бот не инициализирован. Уведомление о закрытии сделки не отправлено.",
                          "LighterSignalScalper")
                return

            from aiogram.utils.markdown import hbold, hcode
            strategy_name = self.strategy_type.value.replace('_', ' ').title()
            bot_prefix = self._get_bot_prefix()

            # Рассчитываем длительность сделки если доступно entry_time
            duration_line = ""
            if entry_time:
                if entry_time.tzinfo is not None:
                    entry_time_naive = entry_time.astimezone().replace(tzinfo=None)
                else:
                    entry_time_naive = entry_time
                duration_seconds = int((datetime.now() - entry_time_naive).total_seconds())
                duration_formatted = self._format_duration_russian(duration_seconds)
                duration_line = f"▫️ {hbold('Время сделки:')} {hcode(duration_formatted)}\n"

            # Формируем строку с ценами входа/выхода если доступны обе цены
            prices_line = ""
            if entry_price and exit_price:
                prices_line = f"▫️ {hbold('Цены:')} Вход ${entry_price:.4f} → Выход ${exit_price:.4f}\n"

            # ПРОЗРАЧНОЕ УВЕДОМЛЕНИЕ: показываем честный результат с учётом всех комиссий
            if pnl >= 0:
                icon = "💰"
                result_text = "ПРИБЫЛЬ ✅"
                text = (
                    f"{icon} {hbold('СДЕЛКА ЗАКРЫТА')} {icon}\n\n"
                    f"▫️ {hbold('Аккаунт:')} {hcode(bot_prefix)}\n"
                    f"▫️ {hbold('Стратегия:')} {hcode(strategy_name)}\n"
                    f"▫️ {hbold('Символ:')} {hcode(self.symbol)}\n"
                    f"▫️ {hbold('Результат:')} {result_text}\n"
                    f"▫️ {hbold('Чистый доход:')} {hcode(f'+{pnl:.2f} USDT')}\n"
                    f"▫️ {hbold('Комиссии:')} {hcode(f'{commission:.2f} USDT')}\n"
                    f"{duration_line}"
                    f"{prices_line}"
                    f"▫️ {hbold('Win Rate стратегии:')} {hcode(f'{win_rate:.2f}%')}"
                )
            else:
                icon = "📉"
                result_text = "УБЫТОК 🔻"
                total_loss = abs(pnl)
                text = (
                    f"{icon} {hbold('СДЕЛКА ЗАКРЫТА')} {icon}\n\n"
                    f"▫️ {hbold('Аккаунт:')} {hcode(bot_prefix)}\n"
                    f"▫️ {hbold('Стратегия:')} {hcode(strategy_name)}\n"
                    f"▫️ {hbold('Символ:')} {hcode(self.symbol)}\n"
                    f"▫️ {hbold('Результат:')} {result_text}\n"
                    f"▫️ {hbold('Общий убыток:')} {hcode(f'-{total_loss:.2f} USDT')}\n"
                    f"▫️ {hbold('(включая комиссии:')} {hcode(f'{commission:.2f} USDT)')}\n"
                    f"{duration_line}"
                    f"{prices_line}"
                    f"▫️ {hbold('Win Rate стратегии:')} {hcode(f'{win_rate:.2f}%')}"
                )
            
            self._send_notification_async(text)
            log_info(self.user_id, "Уведомление о закрытии сделки отправлено успешно.", "LighterSignalScalper")
        except Exception as e:
            log_error(self.user_id, f"Ошибка отправки уведомления о закрытии сделки: {e}", "LighterSignalScalper")
    
    def _calculate_dynamic_levels(self) -> Dict[int, Decimal]:
        """
        Рассчитывает динамические уровни прибыли для трейлинг-стопа.
        
        Использует 6 уровней с процентами от номинальной стоимости позиции:
        - Уровень 1: 0.20% - МГНОВЕННЫЙ (самый быстрый выход)
        - Уровень 2: 0.45% - РАННИЙ
        - Уровень 3: 0.85% - СРЕДНИЙ
        - Уровень 4: 1.30% - ХОРОШИЙ
        - Уровень 5: 1.85% - ОТЛИЧНЫЙ
        - Уровень 6: 2.50% - МАКСИМАЛЬНЫЙ
        
        Returns:
            Dict[int, Decimal]: Словарь с уровнями {уровень: прибыль_в_USDT}
        """
        # Получаем параметры пользователя
        order_amount = max(self._convert_to_decimal(self.get_config_value("order_amount", 200.0)), Decimal('10.0'))
        leverage = self._convert_to_decimal(self.get_config_value("leverage", 2.0))
        
        # Номинальная стоимость позиции (реальный риск с учетом плеча)
        notional_value = order_amount * leverage
        
        # 🎯 ОПТИМИЗИРОВАННЫЕ ПРОЦЕНТЫ для скальпинга
        # Уровни растут примерно в 1.8-2x для плавного перехода
        level_percentages = {
            1: Decimal('0.0025'),   # 0.25% - МГНОВЕННЫЙ (самый быстрый выход)
            2: Decimal('0.0045'),   # 0.45% - РАННИЙ
            3: Decimal('0.0085'),   # 0.85% - СРЕДНИЙ
            4: Decimal('0.0130'),   # 1.30% - ХОРОШИЙ
            5: Decimal('0.0185'),   # 1.85% - ОТЛИЧНЫЙ
            6: Decimal('0.0250')    # 2.50% - МАКСИМАЛЬНЫЙ
        }
        
        # Рассчитываем пороги в USDT для всех уровней
        levels = {
            0: Decimal('0')  # Уровень 0 - трейлинг неактивен (без минимального порога)
        }
        
        for level, percentage in level_percentages.items():
            levels[level] = notional_value * percentage
        
        return levels
    
    def _get_trailing_level(self, current_pnl: Decimal) -> int:
        """
        Определяет текущий уровень трейлинга на основе динамически рассчитанной прибыли.
        
        Args:
            current_pnl: Текущая прибыль в USDT
        
        Returns:
            int: Уровень трейлинга (0-6)
        """
        levels = self._calculate_dynamic_levels()
        
        if current_pnl < levels[1]:
            return 0  # Не достигли минимального порога
        elif current_pnl < levels[2]:
            return 1  # МГНОВЕННЫЙ уровень (0.25%)
        elif current_pnl < levels[3]:
            return 2  # РАННИЙ уровень (0.45%)
        elif current_pnl < levels[4]:
            return 3  # СРЕДНИЙ уровень (0.85%)
        elif current_pnl < levels[5]:
            return 4  # ХОРОШИЙ уровень (1.30%)
        elif current_pnl < levels[6]:
            return 5  # ОТЛИЧНЫЙ уровень (1.85%)
        else:
            return 6  # МАКСИМАЛЬНЫЙ уровень (2.50%)
    
    def _get_level_name(self, level: int) -> str:
        """Возвращает человекочитаемое название уровня с динамическими значениями."""
        if level == 0:
            return "ОЖИДАНИЕ"
        
        levels = self._calculate_dynamic_levels()
        
        level_names = {
            1: f"МГНОВЕННЫЙ УРОВЕНЬ (${levels[1]:.2f}+, 0.25%)",
            2: f"РАННИЙ УРОВЕНЬ (${levels[2]:.2f}+, 0.45%)",
            3: f"СРЕДНИЙ УРОВЕНЬ (${levels[3]:.2f}+, 0.85%)",
            4: f"ХОРОШИЙ УРОВЕНЬ (${levels[4]:.2f}+, 1.30%)",
            5: f"ОТЛИЧНЫЙ УРОВЕНЬ (${levels[5]:.2f}+, 1.85%)",
            6: f"МАКСИМАЛЬНЫЙ УРОВЕНЬ (${levels[6]:.2f}+, 2.50%)"
        }
        return level_names.get(level, "НЕИЗВЕСТНЫЙ УРОВЕНЬ")
    
    async def _send_trade_open_notification(self, side: str, price: Decimal, quantity: Decimal,
                                            intended_amount: Optional[Decimal] = None, signal_price: Optional[Decimal] = None):
        """
        Переопределяем метод для режима симуляции - убираем предупреждение о корректировке суммы ордера.
        """
        try:
            if not self.bot:
                log_error(self.user_id, "Telegram бот не инициализирован. Уведомление об открытии сделки не отправлено.", "LighterSignalScalper")
                return

            from aiogram.utils.markdown import hbold, hcode
            side_text = "LONG 🟢" if side.lower() == 'buy' else "SHORT 🔴"
            strategy_name = self.strategy_type.value.replace('_', ' ').title()
            leverage = self._convert_to_decimal(self.get_config_value("leverage", 1.0))
            actual_amount = (price * quantity) / leverage

            # Формируем блок с ценой сигнала если она передана
            signal_price_text = ""
            if signal_price:
                slippage = price - signal_price if side.lower() == 'buy' else signal_price - price
                slippage_percent = (slippage / signal_price * 100) if signal_price > 0 else Decimal('0')
                signal_price_text = (
                    f"\n📊 {hbold('Цены:')}\n"
                    f"▫️ {hbold('Цена сигнала:')} {hcode(f'{signal_price:.4f} USDT')}\n"
                    f"▫️ {hbold('Фактическая цена:')} {hcode(f'{price:.4f} USDT')}\n"
                    f"▫️ {hbold('Проскальзывание:')} {hcode(f'{slippage:.4f} USDT ({slippage_percent:.3f}%)')}\n"
                )

            # Проверяем настройку enable_stop_loss
            enable_stop_loss = self.get_config_value("enable_stop_loss", True)
            if enable_stop_loss:
                sl_price, sl_loss = self._get_stop_loss_info(side, price, quantity)
                sl_text = (
                    f"\n🛡️ {hbold('Stop Loss:')}\n"
                    f"▫️ {hbold('SL цена:')} {hcode(f'{sl_price:.4f} USDT')}\n"
                    f"▫️ {hbold('Ожидаемый убыток:')} {hcode(f'-{sl_loss:.2f} USDT')}"
                )
            else:
                sl_text = (
                    f"\n🛡️ {hbold('Stop Loss:')} ⚠️ {hcode('ОТКЛЮЧЕН')}\n"
                    f"▫️ Позиция открыта без стоп-лосса"
                )

            bot_prefix = self._get_bot_prefix()

            text = (
                f"📈 {hbold('ОТКРЫТА НОВАЯ СДЕЛКА')} 📈\n\n"
                f"▫️ {hbold('Аккаунт:')} {hcode(bot_prefix)}\n"
                f"▫️ {hbold('Стратегия:')} {hcode(strategy_name)}\n"
                f"▫️ {hbold('Символ:')} {hcode(self.symbol)}\n"
                f"▫️ {hbold('Направление:')} {side_text}\n"
                f"▫️ {hbold('Объем:')} {hcode(str(quantity))}\n"
                f"▫️ {hbold('Стоимость позиции:')} {hcode(f'{actual_amount:.2f} USDT')}"
                f"{signal_price_text}"
                f"{sl_text}"
            )

            # В режиме симуляции НЕ показываем предупреждение о корректировке суммы ордера
            # так как мы сами контролируем размер ордера

            self._send_notification_async(text)
            log_info(self.user_id, "Уведомление об открытии сделки отправлено успешно.", "LighterSignalScalper")
        except Exception as e:
            log_error(self.user_id, f"Ошибка отправки уведомления об открытии сделки: {e}", "LighterSignalScalper")

