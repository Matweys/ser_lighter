"""
Базовая стратегия для многопользовательской торговой системы
Реализует событийно-ориентированную архитектуру с динамическими настройками
"""
import asyncio
import json
from abc import ABC, abstractmethod
from decimal import Decimal, getcontext
from typing import Dict, Any, Optional, List
from datetime import datetime, timedelta, timezone
from aiogram import Bot
from core.logger import log_info, log_error, log_warning, log_debug
from core.enums import StrategyType, PositionSide, ConfigType, ExchangeType
from core.events import (
    EventType, BaseEvent, SignalEvent, PriceUpdateEvent,
    OrderFilledEvent, PositionUpdateEvent, PositionClosedEvent,
    UserSettingsChangedEvent, EventBus, OrderUpdateEvent
)
from cache.redis_manager import redis_manager
from api.bybit_api import BybitAPI
from aiogram.utils.markdown import hbold, hcode
from core.settings_config import EXCHANGE_FEES
from database.db_trades import db_manager


# Настройка точности для Decimal
getcontext().prec = 28



class BaseStrategy(ABC):
    """
    Базовый абстрактный класс для всех торговых стратегий
    
    Функции:
    - Событийно-ориентированная архитектура
    - Динамические настройки из Redis
    - Точные вычисления через Decimal
    - Автоматическое управление жизненным циклом
    - Мониторинг состояния и статистики
    """
    
    def __init__(self, user_id: int, symbol: str, signal_data: Dict[str, Any], api: BybitAPI, event_bus: EventBus, bot: "Bot", config: Optional[Dict] = None):
        """
        Инициализация базовой стратегии
        
        Args:
            user_id: ID пользователя
            symbol: Торговый символ
            signal_data: Данные сигнала от MetaStrategist
        """
        self.user_id = user_id
        self.symbol = symbol
        self.signal_data = signal_data
        self.api: BybitAPI = api
        self.event_bus = event_bus
        self.bot = bot
        self.config: Dict[str, Any] = config or {}
        # Добавлена проверка для раннего обнаружения проблем временно потом удалить
        if not self.bot:
            log_warning(self.user_id,
                        f"Экземпляр Telegram бота не был передан в стратегию {self.strategy_type.value}. Уведомления будут отключены.",
                        module_name=__name__)

        # Состояние стратегии
        self.is_running = False
        self.strategy_id = f"{self.user_id}_{symbol}_{datetime.now().strftime('%Y%m%d_%H%M%S')}"
        self.strategy_type = self._get_strategy_type()
        
        # Конфигурация (загружается динамически)
        self.config: Dict[str, Any] = {}
        self.last_config_update = datetime.min
        self.config_cache_duration = timedelta(minutes=5)
        
        # Базовые параметры (будут обновлены из конфигурации)
        self.leverage = 10
        self.order_amount = 100.0
        self.profit_percent = 1.0
        
        # Блокировка для thread-safety
        self.lock = asyncio.Lock()
        
        # Статистика стратегии
        self.stats = {
            "start_time": datetime.now(),
            "orders_count": 0,
            "profit_orders": 0,
            "loss_orders": 0,
            "total_pnl": Decimal('0'),
            "max_drawdown": Decimal('0'),
            "current_drawdown": Decimal('0')
        }
        
        # Активные ордера и позиции
        self.active_orders: Dict[str, Dict] = {}  # order_id -> order_data
        self.active_positions: Dict[str, Dict] = {}  # position_key -> position_data

        # Система отложенной остановки
        self.deferred_stop_marked: bool = False
        self.deferred_stop_reason: Optional[str] = None

        self._position_monitor_task: Optional[asyncio.Task] = None
        log_info(self.user_id,f"Инициализирована стратегия {self.strategy_type.value} для {symbol} (ID: {self.strategy_id})", module_name=__name__)


    @staticmethod
    def _convert_to_decimal( value: Any) -> Decimal:
        """Безопасное преобразование в Decimal"""
        if isinstance(value, Decimal):
            return value
        try:
            return Decimal(str(value))
        except (ValueError, TypeError):
            return Decimal('0')


    @staticmethod
    def _calculate_precise_stop_loss(entry_price: Decimal, qty: Decimal, sl_usdt: Decimal, is_long: bool) -> Decimal:
        """
        Более точный расчет стоп-лосса с учетом комиссий и буфера.

        Args:
            entry_price: Цена входа
            qty: Количество
            sl_usdt: Желаемый убыток в USDT
            is_long: True для LONG позиций, False для SHORT

        Returns:
            Цена стоп-лосса с учетом комиссий
        """
        # Комиссия тейкера (обычно 0.055% на Bybit)
        taker_fee_rate = Decimal('0.0006')  # 0.06% с небольшим буфером

        # Расчет комиссии при закрытии позиции
        estimated_close_fee = entry_price * qty * taker_fee_rate

        # Добавляем 5% буфер для точности
        buffer = Decimal('1.05')

        # Корректируем желаемый убыток с учетом комиссии
        adjusted_sl_usdt = (sl_usdt + estimated_close_fee) * buffer

        # Рассчитываем цену стопа
        price_offset = adjusted_sl_usdt / qty

        if is_long:
            stop_price = entry_price - price_offset
        else:
            stop_price = entry_price + price_offset

        return stop_price

    def _get_stop_loss_info(self, side: str, price: Decimal, quantity: Decimal) -> tuple[Decimal, Decimal]:
        """
        Рассчитывает цену SL и потенциальный убыток для уведомлений.

        Returns:
            tuple[Decimal, Decimal]: (цена_SL, ожидаемый_убыток_USDT)
        """
        try:
            # Получаем максимальный убыток из конфигурации стратегии
            max_loss_usd = self._convert_to_decimal(self.get_config_value("max_loss_usd", 15.0))

            # Определяем направление позиции
            is_long = side.lower() == 'buy'

            # Рассчитываем цену стоп-лосса
            sl_price = self._calculate_precise_stop_loss(price, quantity, max_loss_usd, is_long)

            # ТОЧНЫЙ расчёт реального убытка при срабатывании SL
            if is_long:
                actual_loss = (price - sl_price) * quantity
            else:
                actual_loss = (sl_price - price) * quantity

            # Добавляем комиссию при закрытии
            taker_fee_rate = Decimal('0.0006')  # 0.06% комиссия тейкера
            estimated_close_fee = sl_price * quantity * taker_fee_rate
            total_expected_loss = actual_loss + estimated_close_fee

            return sl_price, total_expected_loss

        except Exception as e:
            log_error(self.user_id, f"Ошибка расчета информации SL: {e}", "base_strategy")
            # Возвращаем безопасные значения по умолчанию
            return price, Decimal('15.0')

    async def _set_leverage(self):
        """Устанавливает кредитное плечо для торгуемого символа."""
        try:
            if not self.config:
                log_warning(self.user_id, "Конфигурация не загружена, установка плеча пропущена.", module_name=__name__)
                return

            leverage = self.config.get("leverage", 1)

            if self.api:
                result = await self.api.set_leverage(self.symbol, leverage)
                if result:
                    log_info(self.user_id, f"Плечо для {self.symbol} установлено: {leverage}x", module_name=__name__)
                else:
                    log_error(self.user_id, f"Не удалось установить плечо для {self.symbol}", module_name=__name__)

        except Exception as e:
            log_error(self.user_id, f"Критическая ошибка при установке плеча для {self.symbol}: {e}",
                      module_name=__name__)

    async def _await_order_fill(self, order_id: str, side: str, qty: Decimal, max_retries: int = 15,
                                delay: float = 1.5) -> bool:
        """
        Ожидает исполнения ордера, опрашивая API. При успехе, СОЗДАЕТ событие
        и НАПРЯМУЮ вызывает обработчик _handle_order_filled.
        Является единственным источником правды об исполнении ордера.
        """
        log_info(self.user_id,
                 f"Ожидание исполнения ордера {order_id} через API-polling (до {int(max_retries * delay)} сек)...",
                 module_name=__name__)
        for attempt in range(max_retries):
            try:
                # БЫСТРАЯ ПРОВЕРКА: Если ордер уже обработан через WebSocket - выходим немедленно
                if hasattr(self, 'processed_orders') and order_id in self.processed_orders:
                    log_info(self.user_id, f"[БЫСТРЫЙ ВЫХОД] Ордер {order_id} уже обработан WebSocket'ом, прекращаем API-polling", module_name=__name__)
                    return True

                # ДОПОЛНИТЕЛЬНАЯ ПРОВЕРКА: Если стратегия больше не ждёт ордер (is_waiting_for_trade=False)
                if hasattr(self, 'is_waiting_for_trade') and not self.is_waiting_for_trade:
                    log_info(self.user_id, f"[БЫСТРЫЙ ВЫХОД] Стратегия больше не ждёт ордер {order_id}, прекращаем API-polling", module_name=__name__)
                    return True

                await asyncio.sleep(delay)
                order_status = await self.api.get_order_status(order_id)

                if order_status:
                    status = order_status.get("orderStatus")
                    if status == "Filled":
                        log_info(self.user_id, f"ПОДТВЕРЖДЕН API: Ордер {order_id} исполнен.", module_name=__name__)

                        # Создаем объект события со всеми данными из ответа API
                        filled_event = OrderFilledEvent(
                            user_id=self.user_id,
                            order_id=order_id,
                            symbol=self.symbol,
                            side=side,
                            qty=self._convert_to_decimal(order_status.get("cumExecQty", qty)),
                            price=self._convert_to_decimal(order_status.get("avgPrice", "0")),
                            fee=self._convert_to_decimal(order_status.get("cumExecFee", "0"))
                        )
                        # Напрямую вызываем внутренний обработчик
                        await self._handle_order_filled(filled_event)
                        return True

                    elif status in ["Cancelled", "Rejected", "Expired"]:
                        # КРИТИЧЕСКИ ВАЖНО: Обновляем статус ордера в БД
                        try:
                            from database.db_trades import db_manager
                            await db_manager.update_order_status(
                                order_id=order_id,
                                status=status.upper(),
                                filled_price=None,
                                filled_qty=None,
                                fee=None
                            )
                            log_debug(self.user_id, f"Статус ордера {order_id} обновлён в БД: {status.upper()}", module_name=__name__)
                        except Exception as db_error:
                            log_error(self.user_id, f"Ошибка обновления статуса ордера {order_id} в БД: {db_error}", module_name=__name__)

                        log_error(self.user_id, f"Ордер {order_id} не будет исполнен. Статус: {status}",
                                  module_name=__name__)
                        return False

            except Exception as e:
                log_error(self.user_id, f"Ошибка при ожидании исполнения ордера {order_id}: {e}", module_name=__name__)

        log_error(self.user_id, f"Таймаут ожидания исполнения ордера {order_id}. Отменяю ордер для безопасности.",
                  module_name=__name__)
        await self._cancel_order(order_id)
        return False


    @abstractmethod
    def _get_strategy_type(self) -> StrategyType:
        """Возвращает тип стратегии"""
        pass
        
    @abstractmethod
    async def _execute_strategy_logic(self):
        """Основная логика стратегии (реализуется в наследниках)"""
        pass
        
    @abstractmethod
    async def handle_price_update(self, event: PriceUpdateEvent):
        """Обработка обновления цены (реализуется в наследниках)"""
        pass
        
    @abstractmethod
    async def _handle_order_filled(self, event: OrderFilledEvent):
        """Обработка исполнения ордера (реализуется в наследниках)"""
        pass



    async def handle_event(self, event: BaseEvent):
        """
        ФИНАЛЬНАЯ ВЕРСИЯ.
        Четко разделяет обязанности: доверяет событию OrderFilledEvent и передает его дальше.
        """
        if not self.is_running:
            return

        try:
            # Прямая и быстрая обработка подтвержденного исполнения ордера
            if isinstance(event, OrderFilledEvent):
                if event.symbol == self.symbol:
                    log_info(self.user_id,
                             f"Стратегия {self.symbol} получила событие OrderFilledEvent для ордера {event.order_id}",
                             "BaseStrategy")
                    await self._handle_order_filled(event)
                    # Сохраняем состояние после обработки исполненного ордера
                    await self.save_strategy_state({
                        "last_action": "order_filled",
                        "order_id": event.order_id,
                        "fill_price": str(event.price),
                        "fill_qty": str(event.qty)
                    })

            # Обработка остальных событий без изменений
            elif isinstance(event, PriceUpdateEvent):
                await self._handle_price_update_wrapper(event)
            elif isinstance(event, PositionUpdateEvent):
                await self._handle_position_update(event)
            elif isinstance(event, UserSettingsChangedEvent):
                await self._handle_settings_changed(event)

        except Exception as e:
            log_error(self.user_id, f"Критическая ошибка в BaseStrategy.handle_event: {e}", "BaseStrategy")



    async def start(self) -> bool:
        if self.is_running:
            return True
            
        try:
            log_info(self.user_id, f"Запуск стратегии {self.strategy_type.value}...", module_name=__name__)
            
            # Загрузка конфигурации
            await self._load_strategy_config()
            
            if not self.config:
                log_error(self.user_id, "Не удалось загрузить конфигурацию стратегии", module_name=__name__)
                return False
                
            # Валидация конфигурации
            if not await self.validate_config():
                log_error(self.user_id, "Конфигурация стратегии не прошла валидацию", module_name=__name__)
                return False
            
            # Устанавливаем флаг перед выполнением логики
            self.is_running = True

            # Сохранение состояния в Redis после установки флага
            await self.save_strategy_state({"last_action": "strategy_started"})
            # Выполнение начальной логики стратегии
            await self._execute_strategy_logic()

            # Если после выполнения логики стратегия все еще активна, считаем запуск успешным
            if self.is_running:
                log_info(self.user_id, f"Стратегия {self.strategy_type.value} успешно запущена для {self.symbol}",
                         module_name=__name__)

                # Уведомление будет отправлено из UserSession после успешного добавления в active_strategies

                return True
            else:
                # _execute_strategy_logic вызвал self.stop(), запуск не удался
                log_warning(self.user_id,
                            f"Запуск стратегии {self.strategy_type.value} для {self.symbol} был отменен в процессе инициализации.",
                            module_name=__name__)
                return False
        except Exception as e:
            log_error(self.user_id, f"Ошибка запуска стратегии: {e}", module_name=__name__)
            return False

    async def stop(self, reason: str = "Manual stop") -> bool:
        if not self.is_running:
            return True

        if self._position_monitor_task and not self._position_monitor_task.done():
            self._position_monitor_task.cancel()
            self._position_monitor_task = None

        log_info(self.user_id, f"Остановка стратегии {self.strategy_type.value}: {reason}", module_name=__name__)
        try:
            async with self.lock:
                self.is_running = False
                
                # Отмена всех активных ордеров
                await self._cancel_all_orders()
                
                # Закрытие всех позиций (опционально)
                if self.config and self.config.get("close_positions_on_stop", False):
                    await self._close_all_positions()
                
                # Сохранение финальной статистики
                await self._save_final_stats()
                
                # Сохранение финального состояния и очистка
                await self.save_strategy_state({"last_action": "strategy_stopped", "reason": reason})
                # Удаление состояния из Redis
                await self._cleanup_redis_state()
                await self.clear_strategy_state()
            log_info(self.user_id,f"Стратегия {self.strategy_type.value} остановлена", module_name=__name__)
            return True
        except Exception as e:
            log_error(self.user_id, f"Ошибка остановки стратегии: {e}", module_name=__name__)
            return False

    async def mark_for_deferred_stop(self, reason: str) -> bool:
        """
        Помечает стратегию для отложенной остановки после завершения активной позиции.

        Args:
            reason: Причина отложенной остановки

        Returns:
            bool: True если стратегия успешно помечена для остановки
        """
        try:
            self.deferred_stop_reason = reason
            self.deferred_stop_marked = True

            log_info(self.user_id, f"Стратегия {self.strategy_type.value} для {self.symbol} помечена для отложенной остановки: {reason}", module_name=__name__)

            # Сохраняем информацию о отложенной остановке в состоянии
            await self.save_strategy_state({
                "deferred_stop_marked": True,
                "deferred_stop_reason": reason,
                "deferred_stop_time": datetime.now().isoformat()
            })

            return True

        except Exception as e:
            log_error(self.user_id, f"Ошибка пометки стратегии для отложенной остановки: {e}", module_name=__name__)
            return False

    async def check_deferred_stop(self) -> bool:
        """
        Проверяет, должна ли стратегия быть остановлена отложенно.
        Вызывается после закрытия позиции или при отсутствии активной позиции.

        Returns:
            bool: True если стратегия была остановлена
        """
        try:
            if not self.deferred_stop_marked:
                return False

            # Проверяем, есть ли активная позиция
            has_active_position = getattr(self, 'position_active', False)

            if not has_active_position:
                reason = self.deferred_stop_reason or 'deferred_stop_triggered'
                log_info(self.user_id, f"Выполнение отложенной остановки стратегии {self.strategy_type.value} для {self.symbol}: {reason}", module_name=__name__)

                # Сбрасываем флаги отложенной остановки
                self.deferred_stop_marked = False
                self.deferred_stop_reason = None

                # Останавливаем стратегию
                await self.stop(reason)
                return True

            return False

        except Exception as e:
            log_error(self.user_id, f"Ошибка проверки отложенной остановки: {e}", module_name=__name__)
            return False
            
    async def execute(self) -> None:
        """
        Выполняет один цикл стратегии.
        Вызывается периодически из основного цикла.
        """
        if not self.is_running:
            return
            
        try:
            # Обновление конфигурации при необходимости
            await self._ensure_config_fresh()
            
            # Выполнение основной логики стратегии
            await self._execute_strategy_logic()
            
        except Exception as e:
            await self.handle_error(e, "execute")
            
    async def get_status(self) -> Dict[str, Any]:
        """Получение статуса стратегии"""
        return {
            "strategy_id": self.strategy_id,
            "strategy_type": self.strategy_type.value,
            "symbol": self.symbol,
            "running": self.is_running,
            "stats": {
                "start_time": self.stats["start_time"].isoformat(),
                "runtime": str(datetime.now() - self.stats["start_time"]),
                "orders_count": self.stats["orders_count"],
                "profit_orders": self.stats["profit_orders"],
                "loss_orders": self.stats["loss_orders"],
                "total_pnl": float(self.stats["total_pnl"]),
                "win_rate": (
                    self.stats["profit_orders"] / max(1, self.stats["orders_count"]) * 100
                    if self.stats["orders_count"] > 0 else 0
                )
            },
            "active_orders_count": len(self.active_orders),
            "active_positions_count": len(self.active_positions)
        }

    async def _load_strategy_config(self):
        """Загрузка конфигурации стратегии"""
        try:
            # Загрузка глобальной конфигурации пользователя
            global_config = await redis_manager.get_config(self.user_id, ConfigType.GLOBAL)

            if not global_config:
                log_error(self.user_id, "Глобальная конфигурация не найдена", module_name=__name__)
                return

            # Проверка версии конфигурации и автоматическое обновление
            from core.default_configs import DefaultConfigs
            current_version = DefaultConfigs.get_global_config().get("config_version", "1.0.0")
            stored_version = global_config.get("config_version", "1.0.0")

            if current_version != stored_version:
                log_info(self.user_id,
                        f"Обнаружено обновление конфигурации: {stored_version} -> {current_version}. Обновляю...",
                        module_name=__name__)
                # Автоматически обновляем конфигурации из default_configs.py
                await DefaultConfigs.create_default_user_config(self.user_id)
                # Перезагружаем конфигурацию
                global_config = await redis_manager.get_config(self.user_id, ConfigType.GLOBAL)

            # Загрузка конфигурации конкретной стратегии
            strategy_config_enum = ConfigType[f"STRATEGY_{self.strategy_type.value.upper()}"]
            strategy_config = await redis_manager.get_config(self.user_id, strategy_config_enum)

            if not strategy_config:
                log_error(self.user_id,f"Конфигурация стратегии {self.strategy_type.value} не найдена", module_name=__name__)
                return

            # Проверяем версию конфигурации стратегии тоже
            default_strategy_config = DefaultConfigs.get_all_default_configs()["strategy_configs"].get(self.strategy_type.value.lower(), {})
            current_strategy_version = default_strategy_config.get("config_version", current_version)
            stored_strategy_version = strategy_config.get("config_version", stored_version)

            if current_strategy_version != stored_strategy_version:
                log_info(self.user_id,
                        f"Обнаружено обновление конфигурации стратегии {self.strategy_type.value}: {stored_strategy_version} -> {current_strategy_version}",
                        module_name=__name__)
                # Перезагружаем конфигурацию стратегии
                strategy_config = await redis_manager.get_config(self.user_id, strategy_config_enum)

            # Объединение конфигураций
            self.config = {
                **global_config,
                **strategy_config,
                "signal_data": self.signal_data
            }

            # Обновление базовых параметров
            self.leverage = self.config.get('leverage', self.leverage)
            self.order_amount = self.config.get('order_amount', self.order_amount)
            self.profit_percent = self.config.get('profit_percent', self.profit_percent)

            self.last_config_update = datetime.now()
            log_info(self.user_id,f"Конфигурация стратегии {self.strategy_type.value} загружена",module_name=__name__)
        except Exception as e:
            log_error(self.user_id, f"Ошибка загрузки конфигурации: {e}", module_name=__name__)


    async def _ensure_config_fresh(self):
        """Обеспечение актуальности конфигурации"""
        if datetime.now() - self.last_config_update > self.config_cache_duration:
            await self._load_strategy_config()

    async def _force_config_reload(self):
        """Принудительная перезагрузка конфигурации перед каждым входом в сделку"""
        log_info(self.user_id, f"Принудительная перезагрузка конфигурации для стратегии {self.strategy_type.value}", module_name=__name__)
        await self._load_strategy_config()

            
    async def _handle_price_update_wrapper(self, event: PriceUpdateEvent):
        """Обертка для обработки обновления цены"""
        # Фильтрация событий только для нашего пользователя и символа
        if event.user_id != self.user_id or event.symbol != self.symbol:
            return
            
        if not self.is_running:
            return
            
        try:
            await self._handle_price_update(event)
        except Exception as e:
            log_error(self.user_id, f"Ошибка обработки обновления цены: {e}", module_name=__name__)


            
    async def _handle_position_update(self, event: PositionUpdateEvent):
        """Обработка обновления позиции"""
        if event.user_id != self.user_id or event.symbol != self.symbol:
            return
            
        try:
            # Обновление данных позиции
            position_key = f"{event.symbol}_{event.side}"
            self.active_positions[position_key] = {
                "symbol": event.symbol,
                "side": event.side,
                "size": self._convert_to_decimal(event.size),
                "entry_price": self._convert_to_decimal(event.entry_price),
                "mark_price": self._convert_to_decimal(event.mark_price),
                "unrealized_pnl": self._convert_to_decimal(event.unrealized_pnl),
                "updated_at": datetime.now()
            }

            # Обновление статистики
            unrealized_pnl = self._convert_to_decimal(event.unrealized_pnl)
            self.stats["current_drawdown"] = min(Decimal('0'), unrealized_pnl)
            if abs(self.stats["current_drawdown"]) > self.stats["max_drawdown"]:
                self.stats["max_drawdown"] = abs(self.stats["current_drawdown"])

            # Сохраняем состояние после обновления позиции
            await self.save_strategy_state({
                "last_action": "position_updated",
                "position_key": position_key,
                "unrealized_pnl": str(unrealized_pnl)
            })
                
        except Exception as e:
            log_error(self.user_id, f"Ошибка обработки обновления позиции: {e}", module_name=__name__)
            
    async def _handle_settings_changed(self, event: UserSettingsChangedEvent):
        """Обработка изменения настроек пользователя"""
        if event.user_id != self.user_id:
            return
            
        log_info(self.user_id, "Перезагрузка конфигурации после изменения настроек", module_name=__name__)
        await self._load_strategy_config()

    async def _place_order(self, side: str, order_type: str, qty: Decimal, price: Optional[Decimal] = None,
                           stop_loss: Optional[Decimal] = None, take_profit: Optional[Decimal] = None,
                           reduce_only: bool = False) -> Optional[str]:
        """
        Универсальное размещение ордера через API. Возвращает orderId или None.
        """
        try:
            if not self.api:
                log_error(self.user_id, "API клиент не инициализирован в стратегии.", module_name=__name__)
                return None

            order_id = await self.api.place_order(
                symbol=self.symbol, side=side, order_type=order_type, qty=qty, price=price,
                stop_loss=stop_loss, take_profit=take_profit, reduce_only=reduce_only
            )

            if order_id:
                self.active_orders[order_id] = {"order_id": order_id, "status": "New"}

                # КРИТИЧЕСКИ ВАЖНО: Сохраняем ордер в БД для системы восстановления
                try:
                    from database.db_trades import db_manager
                    await db_manager.save_order(
                        user_id=self.user_id,
                        symbol=self.symbol,
                        side=side,
                        order_type=order_type,
                        quantity=qty,
                        price=price or Decimal('0'),
                        order_id=order_id,
                        strategy_type=self.strategy_type.value,
                        metadata={
                            "stop_loss": str(stop_loss) if stop_loss else None,
                            "take_profit": str(take_profit) if take_profit else None,
                            "reduce_only": reduce_only,
                            "created_by": "base_strategy_place_order"
                        }
                    )
                    log_debug(self.user_id, f"Ордер {order_id} сохранён в БД", module_name=__name__)
                except Exception as db_error:
                    log_error(self.user_id, f"Ошибка сохранения ордера в БД: {db_error}", module_name=__name__)

                # Сохраняем состояние после размещения ордера
                await self.save_strategy_state({"last_action": "order_placed", "order_id": order_id})
                log_info(self.user_id, f"Ордер {order_id} ({side} {qty} {self.symbol}) отправлен на биржу.",
                         module_name=__name__)
                return order_id
            else:
                log_error(self.user_id, f"Не удалось разместить ордер для {self.symbol} (API не вернул ID).",
                          module_name=__name__)
                return None

        except Exception as e:
            log_error(self.user_id, f"Критическая ошибка в _place_order: {e}", module_name=__name__)
            return None


    async def _cancel_order(self, order_id: str) -> bool:
        """Отмена ордера"""
        try:
            if not self.api:
                return False
                
            result = await self.api.cancel_order(self.symbol, order_id)
            
            if result:
                # КРИТИЧЕСКИ ВАЖНО: Обновляем статус ордера в БД
                try:
                    from database.db_trades import db_manager
                    await db_manager.update_order_status(
                        order_id=order_id,
                        status="CANCELLED",
                        filled_price=None,
                        filled_qty=None,
                        fee=None
                    )
                    log_debug(self.user_id, f"Статус ордера {order_id} обновлён в БД: CANCELLED", module_name=__name__)
                except Exception as db_error:
                    log_error(self.user_id, f"Ошибка обновления статуса ордера {order_id} в БД: {db_error}", module_name=__name__)

                # Удаление из активных ордеров
                if order_id in self.active_orders:
                    del self.active_orders[order_id]

                # Сохраняем состояние после отмены ордера
                await self.save_strategy_state({"last_action": "order_cancelled", "order_id": order_id})
                log_info(self.user_id, f"Ордер отменен: {order_id}", module_name=__name__)
                return True
                
        except Exception as e:
            log_error(self.user_id, f"Ошибка отмены ордера {order_id}: {e}", module_name=__name__)
            
        return False
        
    async def _cancel_all_orders(self):
        """Отмена всех активных ордеров"""
        try:
            order_ids = list(self.active_orders.keys())
            
            for order_id in order_ids:
                await self._cancel_order(order_id)
                
            log_info(self.user_id, f"Отменено {len(order_ids)} ордеров", module_name=__name__)
            
        except Exception as e:
            log_error(self.user_id, f"Ошибка отмены всех ордеров: {e}", module_name=__name__)
            
    async def _close_all_positions(self):
        """Закрытие всех позиций"""
        try:
            if not self.api:
                return
                
            for position_key, position in self.active_positions.items():
                if position["size"] > 0:
                    # Закрытие позиции рыночным ордером
                    close_side = "Sell" if position["side"] == "Buy" else "Buy"
                    
                    await self.api.place_order(
                        symbol=position["symbol"],
                        side=close_side,
                        order_type="Market",
                        qty=position["size"],
                        reduce_only=True
                    )
                    
            log_info(self.user_id, "Все позиции закрыты", module_name=__name__)
            
        except Exception as e:
            log_error(self.user_id, f"Ошибка закрытия позиций: {e}", module_name=__name__)
            
    async def _save_strategy_state(self):
        """Сохранение состояния стратегии в Redis"""
        try:
            state_data = {
                "strategy_id": self.strategy_id,
                "strategy_type": self.strategy_type.value,
                "symbol": self.symbol,
                "running": self.is_running,
                "stats": {
                    "start_time": self.stats["start_time"].isoformat(),
                    "orders_count": self.stats["orders_count"],
                    "profit_orders": self.stats["profit_orders"],
                    "loss_orders": self.stats["loss_orders"],
                    "total_pnl": float(self.stats["total_pnl"])
                },
                "active_orders": {
                    order_id: {
                        **order_data,
                        "created_at": order_data["created_at"].isoformat(),
                        "qty": float(order_data["qty"]),
                        "price": float(order_data["price"]) if order_data["price"] else None
                    }
                    for order_id, order_data in self.active_orders.items()
                }
            }

            await redis_manager.save_strategy_state(
                user_id=self.user_id,
                strategy_name=self.strategy_type.value,
                symbol=self.symbol,
                state_data=state_data
            )
            
        except Exception as e:
            log_error(self.user_id, f"Ошибка сохранения состояния стратегии: {e}", module_name=__name__)
            
    async def _save_final_stats(self):
        """Сохранение финальной статистики"""
        try:
            final_stats = {
                "strategy_id": self.strategy_id,
                "strategy_type": self.strategy_type.value,
                "symbol": self.symbol,
                "start_time": self.stats["start_time"].isoformat(),
                "end_time": datetime.now().isoformat(),
                "runtime_seconds": (datetime.now() - self.stats["start_time"]).total_seconds(),
                "orders_count": self.stats["orders_count"],
                "profit_orders": self.stats["profit_orders"],
                "loss_orders": self.stats["loss_orders"],
                "total_pnl": float(self.stats["total_pnl"]),
                "max_drawdown": float(self.stats["max_drawdown"]),
                "win_rate": (
                    self.stats["profit_orders"] / max(1, self.stats["orders_count"]) * 100
                    if self.stats["orders_count"] > 0 else 0
                )
            }
            
            # Сохранение в историю стратегий
            await redis_manager.add_to_list(
                f"user:{self.user_id}:strategy_history",
                json.dumps(final_stats),
                max_length=100
            )
            
        except Exception as e:
            log_error(self.user_id, f"Ошибка сохранения финальной статистики: {e}", module_name=__name__)
            
    async def _cleanup_redis_state(self):
        """Очистка состояния в Redis"""
        try:
            await redis_manager.delete_strategy_state(user_id=self.user_id, strategy_name=self.strategy_type.value,
                                                      symbol=self.symbol)
        except Exception as e:
            log_error(self.user_id, f"Ошибка очистки состояния: {e}", module_name=__name__)

            
    async def save_state(self, state_data: Dict[str, Any]) -> None:
        """
        Сохраняет состояние стратегии в Redis для восстановления после перезапуска.

        Args:
            state_data: Данные состояния для сохранения
        """
        try:
            # Используем символ в ключе, чтобы поддерживать несколько стратегий для одного пользователя
            key = f"{self.strategy_type.value}:{self.symbol}"
            await redis_manager.save_strategy_state(self.user_id, key, state_data)
            log_info(self.user_id, f"Состояние стратегии {self.strategy_type.value} для {self.symbol} сохранено", module_name=__name__)
        except Exception as e:
            log_error(self.user_id,f"Ошибка сохранения состояния стратегии: {e}", module_name=__name__)

    async def load_state(self) -> Optional[Dict[str, Any]]:
        """
        Загружает состояние стратегии из Redis.

        Returns:
            Optional[Dict[str, Any]]: Данные состояния или None если не найдены
        """
        try:
            # Используем символ в ключе для загрузки правильного состояния
            key = f"{self.strategy_type.value}:{self.symbol}"
            state_data = await redis_manager.get_strategy_state(self.user_id, key)
            if state_data:
                log_info(self.user_id, f"Состояние стратегии {self.strategy_type.value} для {self.symbol} загружено", module_name=__name__)
            return state_data
        except Exception as e:
            log_error(self.user_id,f"Ошибка загрузки состояния стратегии: {e}", module_name=__name__)
            return None

    async def clear_state(self) -> None:
        """Очищает сохраненное состояние стратегии."""
        try:
            # Используем символ в ключе для очистки правильного состояния
            key = f"{self.strategy_type.value}:{self.symbol}"
            await redis_manager.delete_strategy_state(self.user_id, key)
            log_info(self.user_id, f"Состояние стратегии {self.strategy_type.value} для {self.symbol} очищено", module_name=__name__)
        except Exception as e:
            log_error(self.user_id, f"Ошибка очистки состояния стратегии: {e}", module_name=__name__)

    def get_config_value(self, key: str, default: Any = None) -> Any:
        """
        Получает значение из конфигурации стратегии.

        Args:
            key: Ключ конфигурации
            default: Значение по умолчанию

        Returns:
            Any: Значение конфигурации
        """
        return self.config.get(key, default)

    def update_config(self, new_config: Dict[str, Any]) -> None:
        """
        Обновляет конфигурацию стратегии.

        Args:
            new_config: Новая конфигурация
        """
        self.config.update(new_config)

        # Обновляем базовые параметры
        self.leverage = self.config.get('leverage', self.leverage)
        self.order_amount = self.config.get('order_amount', self.order_amount)
        self.profit_percent = self.config.get('profit_percent', self.profit_percent)

        log_info(self.user_id, f"Конфигурация стратегии {self.strategy_type.value} обновлена", module_name=__name__)

    async def handle_error(self, error: Exception, context: str = "") -> None:
        """
        Обрабатывает ошибки стратегии.

        Args:
            error: Исключение
            context: Контекст ошибки
        """
        error_msg = f"Ошибка в стратегии {self.strategy_type.value} для {self.symbol}"
        if context:
            error_msg += f" ({context})"
        error_msg += f": {str(error)}"

        log_error(self.user_id, error_msg, module_name=__name__)

        # В критических случаях можно остановить стратегию
        if isinstance(error, (ConnectionError, TimeoutError)):
            log_error(self.user_id, f"Критическая ошибка соединения, останавливаем стратегию {self.symbol}", module_name=__name__)
            await self.stop()

    def is_strategy_running(self) -> bool:
        """
        Проверяет, запущена ли стратегия.

        Returns:
            bool: True если стратегия запущена
        """
        return self.is_running

    async def validate_config(self) -> bool:
        """
        Валидирует конфигурацию стратегии.
        """
        required_fields = ['leverage', 'order_amount']

        for field in required_fields:
            if field not in self.config:
                log_error(self.user_id, f"Отсутствует обязательное поле конфигурации: {field}", module_name=__name__)
                return False

        # Проверяем диапазоны значений
        if not (1 <= self.config.get('leverage', 1) <= 100):
            log_error(self.user_id, f"Неверное значение плеча: {self.config.get('leverage')}", module_name=__name__)
            return False

        if not (5.0 <= self.config.get('order_amount', 10.0) <= 10000):  # Минимальный ордер обычно около 5 USDT
            log_error(self.user_id, f"Неверная сумма ордера: {self.config.get('order_amount')}", module_name=__name__)
            return False

        return True

    async def _send_strategy_start_notification(self):
        """Отправляет уведомление о запуске стратегии"""
        try:
            if not self.bot:
                log_warning(self.user_id, "Telegram бот не инициализирован. Уведомление о запуске стратегии не отправлено.", "base_strategy")
                return

            strategy_name = self.strategy_type.value.replace('_', ' ').title()

            # Разные сообщения для разных типов стратегий
            if self.strategy_type == StrategyType.SIGNAL_SCALPER:
                text = (
                    f"🚀 {hbold('СТРАТЕГИЯ ЗАПУЩЕНА')} 🚀\n\n"
                    f"▫️ {hbold('Стратегия:')} {hcode(strategy_name)}\n"
                    f"▫️ {hbold('Символ:')} {hcode(self.symbol)}\n"
                    f"▫️ {hbold('Статус:')} Ожидает сигналы для открытия позиций\n"
                    f"▫️ {hbold('Размер ордера:')} {hcode(f'{self.order_amount} USDT')}\n"
                    f"▫️ {hbold('Плечо:')} {hcode(f'{self.leverage}x')}"
                )
            elif self.strategy_type == StrategyType.IMPULSE_TRAILING:
                text = (
                    f"🔍 {hbold('СТРАТЕГИЯ ЗАПУЩЕНА')} 🔍\n\n"
                    f"▫️ {hbold('Стратегия:')} {hcode(strategy_name)}\n"
                    f"▫️ {hbold('Символ:')} {hcode(self.symbol)}\n"
                    f"▫️ {hbold('Статус:')} Активное сканирование и поиск импульсов\n"
                    f"▫️ {hbold('Размер ордера:')} {hcode(f'{self.order_amount} USDT')}\n"
                    f"▫️ {hbold('Плечо:')} {hcode(f'{self.leverage}x')}\n\n"
                    f"🎯 Стратегия отслеживает рыночные импульсы и готова к работе!"
                )
            else:
                # Общее сообщение для других стратегий
                text = (
                    f"✅ {hbold('СТРАТЕГИЯ ЗАПУЩЕНА')} ✅\n\n"
                    f"▫️ {hbold('Стратегия:')} {hcode(strategy_name)}\n"
                    f"▫️ {hbold('Символ:')} {hcode(self.symbol)}\n"
                    f"▫️ {hbold('Статус:')} Активна и готова к работе\n"
                    f"▫️ {hbold('Размер ордера:')} {hcode(f'{self.order_amount} USDT')}\n"
                    f"▫️ {hbold('Плечо:')} {hcode(f'{self.leverage}x')}"
                )

            await self.bot.send_message(self.user_id, text, parse_mode="HTML")
            log_info(self.user_id, f"Уведомление о запуске стратегии {strategy_name} отправлено", "base_strategy")

        except Exception as e:
            log_error(self.user_id, f"Ошибка отправки уведомления о запуске стратегии: {e}", "base_strategy")

    async def _send_trade_open_notification(self, side: str, price: Decimal, quantity: Decimal,
                                            intended_amount: Optional[Decimal] = None):
        """Отправляет уведомление и СОЗДАЕТ запись о сделке в БД."""
        try:
            # --- БЛОК ДЛЯ ЗАПИСИ В БД ПРИ ОТКРЫТИИ ---
            from database.db_trades import TradeRecord
            new_trade = TradeRecord(
                user_id=self.user_id,
                symbol=self.symbol,
                side=side,
                entry_price=price,
                quantity=quantity,
                leverage=int(self.get_config_value("leverage", 1)),
                status="ACTIVE",
                strategy_type=self.strategy_type.value,
                entry_time=datetime.now(timezone.utc),
                profit=Decimal('0'), # PnL при открытии всегда 0
                commission=Decimal('0')
            )
            trade_id = await db_manager.save_trade(new_trade)
            if trade_id:
                # Сохраняем ID для будущего обновления при закрытии
                self.active_trade_db_id = trade_id

            # Проверяем, что бот инициализирован
            if not self.bot:
                log_error(self.user_id, "Telegram бот не инициализирован. Уведомление об открытии сделки не отправлено.", "base_strategy")
                return

            side_text = "LONG 🟢" if side.lower() == 'buy' else "SHORT 🔴"
            strategy_name = self.strategy_type.value.replace('_', ' ').title()
            # Расчет actual_amount - реальная маржа (сумма, списанная с баланса)
            leverage = self._convert_to_decimal(self.get_config_value("leverage", 1.0))
            actual_amount = (price * quantity) / leverage

            # Получаем информацию о SL
            sl_price, sl_loss = self._get_stop_loss_info(side, price, quantity)

            text = (
                f"📈 {hbold('ОТКРЫТА НОВАЯ СДЕЛКА')} 📈\n\n"
                f"▫️ {hbold('Стратегия:')} {hcode(strategy_name)}\n"
                f"▫️ {hbold('Символ:')} {hcode(self.symbol)}\n"
                f"▫️ {hbold('Направление:')} {side_text}\n"
                f"▫️ {hbold('Цена входа:')} {hcode(f'{price:.4f} USDT')}\n"
                f"▫️ {hbold('Объем:')} {hcode(str(quantity))}\n"
                f"▫️ {hbold('Стоимость позиции:')} {hcode(f'{actual_amount:.2f} USDT')}\n\n"
                f"🛡️ {hbold('Stop Loss:')}\n"
                f"▫️ {hbold('SL цена:')} {hcode(f'{sl_price:.4f} USDT')}\n"
                f"▫️ {hbold('Ожидаемый убыток:')} {hcode(f'-{sl_loss:.2f} USDT')}"
            )

            # Добавляем предупреждение, если фактическая сумма отличается от запрошенной
            if intended_amount:
                difference = abs(intended_amount - actual_amount)
                # Показываем предупреждение если:
                # 1. Разница больше 0.10 USDT (абсолютная разница)
                # 2. ИЛИ разница больше 0.5% от запрошенной суммы (относительная разница)
                absolute_threshold = Decimal('0.10')
                relative_threshold = intended_amount * Decimal('0.005')  # 0.5%

                if difference >= absolute_threshold or difference >= relative_threshold:
                    difference_percent = (difference / intended_amount) * 100
                    text += (
                        f"\n\n⚠️ {hbold('Внимание:')}\n"
                        f"Запрошенная сумма ордера ({hcode(f'{intended_amount:.2f} USDT')}) была скорректирована "
                        f"биржей до {hcode(f'{actual_amount:.2f} USDT')} из-за требований к минимальному размеру лота.\n"
                        f"Разница: {hcode(f'{difference:.2f} USDT')} ({hcode(f'{difference_percent:.1f}%')})"
                    )

                # Финальная диагностика
                log_info(self.user_id,
                         f"[TRACE] Проверка перед отправкой: self.bot существует? {'Да' if self.bot else 'Нет'}",
                         "base_strategy")

            await self.bot.send_message(self.user_id, text, parse_mode="HTML")
            log_info(self.user_id, "[TRACE] Уведомление об открытии сделки отправлено успешно.", "base_strategy")
        except Exception as e:
            log_error(self.user_id, f"Ошибка отправки уведомления об открытии сделки: {e}", "base_strategy")
            # Дополнительное логирование для диагностики
            log_error(self.user_id, f"Состояние бота: {'инициализирован' if self.bot else 'не инициализирован'}", "base_strategy")

    async def _send_averaging_notification(self, price: Decimal, quantity: Decimal, new_avg_price: Decimal,
                                           new_total_size: Decimal, side: Optional[str] = None):
        """Отправляет уведомление об усреднении позиции."""
        try:
            # Проверяем, что бот инициализирован
            if not self.bot:
                log_error(self.user_id, "Telegram бот не инициализирован. Уведомление об усреднении не отправлено.", "base_strategy")
                return

            strategy_name = self.strategy_type.value.replace('_', ' ').title()

            # Получаем информацию о SL для новой позиции
            if side:
                sl_price, sl_loss = self._get_stop_loss_info(side, new_avg_price, new_total_size)
            else:
                # Если side не передан, используем значения по умолчанию
                sl_price, sl_loss = new_avg_price, Decimal('15.0')

            text = (
                f"🔄 {hbold('ПОЗИЦИЯ УСРЕДНЕНА')} 🔄\n\n"
                f"▫️ {hbold('Стратегия:')} {hcode(strategy_name)}\n"
                f"▫️ {hbold('Символ:')} {hcode(self.symbol)}\n"
                f"▫️ {hbold('Цена усреднения:')} {hcode(f'{price:.4f} USDT')}\n"
                f"▫️ {hbold('Добавленный объем:')} {hcode(str(quantity))}\n\n"
                f"ℹ️ {hbold('Новые параметры позиции:')}\n"
                f"▫️ {hbold('Новая ср. цена:')} {hcode(f'{new_avg_price:.4f} USDT')}\n"
                f"▫️ {hbold('Новый общий объем:')} {hcode(str(new_total_size))}\n\n"
                f"🛡️ {hbold('Обновленный Stop Loss:')}\n"
                f"▫️ {hbold('SL цена:')} {hcode(f'{sl_price:.4f} USDT')}\n"
                f"▫️ {hbold('Ожидаемый убыток:')} {hcode(f'-{sl_loss:.2f} USDT')}"
            )
            # Финальная диагностика
            log_info(self.user_id,
                     f"[TRACE] Проверка перед отправкой (усреднение): self.bot существует? {'Да' if self.bot else 'Нет'}",
                     "base_strategy")

            await self.bot.send_message(self.user_id, text, parse_mode="HTML")
            log_info(self.user_id, "[TRACE] Уведомление об усреднении отправлено успешно.", "base_strategy")
        except Exception as e:
            log_error(self.user_id, f"Ошибка отправки уведомления об усреднении: {e}", "base_strategy")
            # Дополнительное логирование для диагностики
            log_error(self.user_id, f"Состояние бота: {'инициализирован' if self.bot else 'не инициализирован'}", "base_strategy")


    # strategies/base_strategy.py -> _send_trade_close_notification
    async def _send_trade_close_notification(self, pnl: Decimal, commission: Decimal = Decimal('0'), exit_price: Optional[Decimal] = None):
        """Отправляет уведомление, обновляет статистику и ОБНОВЛЯЕТ запись о сделке в БД."""
        try:
            # --- БЛОК ДЛЯ ОБНОВЛЕНИЯ В БД ПРИ ЗАКРЫТИИ ---
            if hasattr(self, 'active_trade_db_id') and self.active_trade_db_id:
                await db_manager.update_trade_on_close(
                    trade_id=self.active_trade_db_id,
                    exit_price=exit_price if exit_price else Decimal('0'),
                    pnl=pnl,
                    commission=commission,
                    exit_time=datetime.now(timezone.utc)
                )
                del self.active_trade_db_id # Очищаем ID после использования

            # 1. Обновляем статистику самой стратегии
            self.stats["orders_count"] += 1
            self.stats["total_pnl"] += pnl
            if pnl > 0:
                self.stats["profit_orders"] += 1
            else:
                self.stats["loss_orders"] += 1

            # 2. Обновляем статистику по этой стратегии в БД
            win_rate = await db_manager.update_strategy_stats(
                user_id=self.user_id,
                strategy_type=self.strategy_type.value,
                pnl=pnl
            )

            # 3. ВАЖНО: Обновляем общую статистику пользователя в БД
            await db_manager.update_user_totals(user_id=self.user_id, pnl=pnl)

            # 4. Отправляем уведомление, если бот доступен
            if not self.bot:
                log_error(self.user_id, "Бот не инициализирован. Уведомление о закрытии сделки не отправлено.",
                          "base_strategy")
                return

            if pnl >= 0:
                result_text, pnl_text, icon = "ПРИБЫЛЬ ✅", f"+{pnl:.2f} USDT", "💰"
            else:
                result_text, pnl_text, icon = "УБЫТОК 🔻", f"{pnl:.2f} USDT", "📉"

            strategy_name = self.strategy_type.value.replace('_', ' ').title()

            text = (
                f"{icon} {hbold('СДЕЛКА ЗАКРЫТА')} {icon}\n\n"
                f"▫️ {hbold('Стратегия:')} {hcode(strategy_name)}\n"
                f"▫️ {hbold('Символ:')} {hcode(self.symbol)}\n"
                f"▫️ {hbold('Результат:')} {result_text}\n"
                f"▫️ {hbold('Чистый PnL:')} {hcode(pnl_text)}\n"
                f"▫️ {hbold('Комиссия:')} {hcode(f'~{commission:.2f} USDT')}\n"
                f"▫️ {hbold('Win Rate стратегии:')} {hcode(f'{win_rate:.2f}%')}"
            )
            await self.bot.send_message(self.user_id, text, parse_mode="HTML")
            log_info(self.user_id, "Уведомление о закрытии сделки отправлено успешно.", "base_strategy")
        except Exception as e:
            log_error(self.user_id, f"Ошибка отправки уведомления о закрытии сделки: {e}", "base_strategy")

    async def _monitor_active_position(self):
        """
        Фоновая задача-"сторож". Раз в минуту проверяет через API,
        не закрылась ли позиция без ведома стратегии (из-за сбоя WebSocket).
        """
        log_info(self.user_id, f"Запущен API-монитор для позиции {self.symbol}", "BaseStrategy")

        while self.is_running and self.position_size > 0:
            try:
                await asyncio.sleep(60)  # Проверка раз в минуту

                if not self.is_running or self.position_size == 0:
                    break  # Выходим, если стратегия остановлена или позиция закрыта штатно

                positions = await self.api.get_positions(symbol=self.symbol)

                # Если API вернул пустой список, значит позиции на бирже больше нет
                if not positions:
                    log_warning(self.user_id,
                                f"ДЕСИНХРОНИЗАЦИЯ! API сообщает, что позиции по {self.symbol} нет, но стратегия об этом не знала. Принудительное закрытие.",
                                "BaseStrategy")

                    # Получаем последнюю сделку, чтобы рассчитать PnL
                    last_trade = await self.api.get_last_trade(self.symbol)
                    if last_trade:
                        # Создаем "фейковое" событие исполнения, чтобы запустить всю стандартную логику
                        fake_filled_event = OrderFilledEvent(
                            user_id=self.user_id,
                            order_id=last_trade.get('orderId', 'unknown_fallback'),
                            symbol=self.symbol,
                            side="Sell",  # Закрытие всегда Sell для LONG-only
                            qty=self._convert_to_decimal(last_trade.get('execQty', '0')),
                            price=self._convert_to_decimal(last_trade.get('execPrice', '0')),
                            fee=self._convert_to_decimal(last_trade.get('execFee', '0'))
                        )
                        await self._handle_order_filled(fake_filled_event)
                    else:
                        # Если не удалось получить сделку, просто останавливаемся
                        await self.stop("position_desync_no_trade_history")

                    break  # Выходим из цикла мониторинга

            except asyncio.CancelledError:
                break  # Штатный выход при отмене задачи
            except Exception as e:
                log_error(self.user_id, f"Ошибка в API-мониторе для {self.symbol}: {e}", "BaseStrategy")
                await asyncio.sleep(60)  # В случае ошибки ждем дольше

        log_info(self.user_id, f"Остановка API-монитора для позиции {self.symbol}", "BaseStrategy")


    def __str__(self) -> str:
        """Строковое представление стратегии."""
        return f"{self.strategy_type.value}({self.symbol}, user={self.user_id})"

    def __repr__(self) -> str:
        """Представление стратегии для отладки."""
        return f"<{self.__class__.__name__}: {self.strategy_type.value}, {self.symbol}, user={self.user_id}>"

    # ===============================================================================
    # СИСТЕМА ВОССТАНОВЛЕНИЯ СОСТОЯНИЯ ПОСЛЕ ПЕРЕЗАГРУЗКИ СЕРВЕРА
    # ===============================================================================

    async def save_strategy_state(self, additional_data: Dict[str, Any] = None):
        """
        Сохраняет текущее состояние стратегии в Redis для восстановления после перезагрузки.
        Вызывается при каждом важном изменении состояния.
        """
        try:
            state_key = f"strategy_state:{self.user_id}:{self.symbol}:{self.strategy_type.value}"

            # Собираем все атрибуты стратегии для полного восстановления
            strategy_attributes = {}

            # Ключевые атрибуты для всех стратегий
            critical_attributes = [
                'position_active', 'entry_price', 'position_size', 'active_direction',
                'current_order_id', 'stop_loss_order_id', 'stop_loss_price',
                'is_waiting_for_trade', 'processed_orders', 'intended_order_amount',
                'active_trade_db_id'  # Важно для связи с БД
            ]

            # Дополнительные атрибуты для SignalScalper
            scalper_attributes = [
                'averaging_count', 'total_position_size', 'average_entry_price',
                'last_averaging_percent', 'sl_extended', 'config_frozen',
                'active_trade_config', 'peak_profit_usd', 'hold_signal_counter'
            ]

            # Сохраняем все доступные атрибуты
            all_attributes = critical_attributes + scalper_attributes
            for attr in all_attributes:
                if hasattr(self, attr):
                    value = getattr(self, attr)
                    # Конвертируем специальные типы для JSON
                    if isinstance(value, Decimal):
                        strategy_attributes[attr] = str(value)
                    elif isinstance(value, set):
                        strategy_attributes[attr] = list(value)
                    elif value is not None:
                        strategy_attributes[attr] = value

            strategy_state = {
                "user_id": self.user_id,
                "symbol": self.symbol,
                "strategy_type": self.strategy_type.value,
                "strategy_id": self.strategy_id,
                "is_running": self.is_running,
                "config": self.config,
                "stats": {
                    "start_time": self.stats["start_time"].isoformat(),
                    "orders_count": self.stats["orders_count"],
                    "profit_orders": self.stats["profit_orders"],
                    "loss_orders": self.stats["loss_orders"],
                    "total_pnl": float(self.stats["total_pnl"]),
                    "max_drawdown": float(self.stats["max_drawdown"]),
                    "current_drawdown": float(self.stats["current_drawdown"])
                },
                "active_orders": self.active_orders,
                "active_positions": self.active_positions,
                "signal_data": self.signal_data,
                "strategy_attributes": strategy_attributes,  # Полное состояние стратегии
                "last_saved": datetime.now().isoformat(),
                # Дополнительные данные от конкретных стратегий
                "additional_data": additional_data or {}
            }

            # Сохраняем состояние в Redis с TTL 7 дней
            await redis_manager.redis_client.setex(
                state_key,
                604800,  # 7 дней в секундах
                json.dumps(strategy_state, default=str)
            )

            log_debug(self.user_id, f"Состояние стратегии {self.symbol} сохранено в Redis", "BaseStrategy")

        except Exception as e:
            log_error(self.user_id, f"Ошибка сохранения состояния стратегии {self.symbol}: {e}", "BaseStrategy")

    @classmethod
    async def restore_strategy_state(cls, user_id: int, symbol: str, strategy_type: StrategyType) -> Optional[Dict[str, Any]]:
        """
        Восстанавливает состояние стратегии из Redis после перезагрузки сервера.
        Возвращает сохранённое состояние или None если состояние не найдено.
        """
        try:
            state_key = f"strategy_state:{user_id}:{symbol}:{strategy_type.value}"

            saved_state = await redis_manager.redis_client.get(state_key)
            if not saved_state:
                return None

            strategy_state = json.loads(saved_state)

            # Логируем время последнего сохранения для информации
            last_saved = datetime.fromisoformat(strategy_state["last_saved"])
            downtime = datetime.now() - last_saved
            if downtime.total_seconds() < 60:
                downtime_str = f"{int(downtime.total_seconds())} сек."
            elif downtime.total_seconds() < 3600:
                downtime_str = f"{int(downtime.total_seconds() / 60)} мин."
            else:
                hours = int(downtime.total_seconds() / 3600)
                minutes = int((downtime.total_seconds() % 3600) / 60)
                downtime_str = f"{hours}ч {minutes}мин"

            log_info(user_id, f"Найдено сохранённое состояние стратегии {symbol} от {last_saved} (простой: {downtime_str})", "BaseStrategy")
            return strategy_state

        except Exception as e:
            log_error(user_id, f"Ошибка восстановления состояния стратегии {symbol}: {e}", "BaseStrategy")
            return None

    async def recover_after_restart(self, saved_state: Dict[str, Any]) -> bool:
        """
        Восстанавливает состояние стратегии после перезагрузки сервера.
        Возвращает True если восстановление прошло успешно.
        """
        try:
            log_info(self.user_id, f"🔄 Начинаю восстановление стратегии {self.symbol} после перезагрузки сервера...", "BaseStrategy")

            # Восстанавливаем базовые параметры
            self.strategy_id = saved_state.get("strategy_id", self.strategy_id)
            self.config = saved_state.get("config", {})
            self.signal_data = saved_state.get("signal_data", {})
            self.active_orders = saved_state.get("active_orders", {})
            self.active_positions = saved_state.get("active_positions", {})

            # Восстанавливаем статистику
            saved_stats = saved_state.get("stats", {})
            if saved_stats.get("start_time"):
                self.stats["start_time"] = datetime.fromisoformat(saved_stats["start_time"])
            self.stats["orders_count"] = saved_stats.get("orders_count", 0)
            self.stats["profit_orders"] = saved_stats.get("profit_orders", 0)
            self.stats["loss_orders"] = saved_stats.get("loss_orders", 0)
            self.stats["total_pnl"] = Decimal(str(saved_stats.get("total_pnl", 0)))
            self.stats["max_drawdown"] = Decimal(str(saved_stats.get("max_drawdown", 0)))
            self.stats["current_drawdown"] = Decimal(str(saved_stats.get("current_drawdown", 0)))

            # КРИТИЧЕСКИ ВАЖНО: Восстанавливаем все атрибуты стратегии
            strategy_attributes = saved_state.get("strategy_attributes", {})
            for attr_name, attr_value in strategy_attributes.items():
                if hasattr(self, attr_name):
                    # Восстанавливаем типы данных
                    if attr_name in ['entry_price', 'position_size', 'stop_loss_price',
                                   'total_position_size', 'average_entry_price', 'peak_profit_usd',
                                   'last_averaging_percent', 'intended_order_amount']:
                        setattr(self, attr_name, Decimal(str(attr_value)) if attr_value else None)
                    elif attr_name == 'processed_orders':
                        setattr(self, attr_name, set(attr_value) if attr_value else set())
                    else:
                        setattr(self, attr_name, attr_value)

                    log_debug(self.user_id, f"Восстановлен атрибут {attr_name} = {attr_value}", "BaseStrategy")

            # Специальная проверка и восстановление связи с БД
            if hasattr(self, 'active_trade_db_id') and self.active_trade_db_id:
                log_info(self.user_id, f"Восстановлена связь с записью БД: trade_id={self.active_trade_db_id}", "BaseStrategy")

            # Проверяем актуальные ордера на бирже и синхронизируем состояние
            await self._sync_orders_after_restart()

            # Уведомляем пользователя о восстановлении
            await self._notify_user_about_recovery(saved_state)

            # Конкретная стратегия может переопределить этот метод для дополнительного восстановления
            await self._strategy_specific_recovery(saved_state.get("additional_data", {}))

            log_info(self.user_id, f"✅ Стратегия {self.symbol} успешно восстановлена после перезагрузки", "BaseStrategy")
            return True

        except Exception as e:
            log_error(self.user_id, f"❌ Ошибка восстановления стратегии {self.symbol}: {e}", "BaseStrategy")
            return False

    async def _sync_orders_after_restart(self):
        """
        Синхронизирует состояние ордеров с биржей после перезагрузки.
        Проверяет какие ордера всё ещё активны, какие исполнены или отменены.
        """
        try:
            if not self.active_orders:
                log_info(self.user_id, f"Нет сохранённых ордеров для синхронизации по {self.symbol}", "BaseStrategy")
                return

            log_info(self.user_id, f"🔄 Синхронизирую {len(self.active_orders)} ордеров с биржей для {self.symbol}", "BaseStrategy")

            # Получаем все открытые ордера с биржи
            exchange_orders = await self.api.get_open_orders(symbol=self.symbol)
            exchange_order_ids = set()

            if exchange_orders:
                exchange_order_ids = {order.get("orderId") for order in exchange_orders}

            orders_to_remove = []

            for order_id, order_data in self.active_orders.items():
                if order_id in exchange_order_ids:
                    # Ордер всё ещё активен на бирже
                    log_info(self.user_id, f"✅ Ордер {order_id} по {self.symbol} всё ещё активен, продолжаю отслеживание", "BaseStrategy")
                else:
                    # Ордер не найден в активных - возможно исполнен или отменён
                    log_warning(self.user_id, f"⚠️ Ордер {order_id} по {self.symbol} не найден в активных, проверяю статус", "BaseStrategy")

                    # Проверяем статус ордера через историю
                    order_status = await self.api.get_order_status(order_id)
                    if order_status:
                        status = order_status.get("orderStatus", "Unknown")
                        if status == "Filled":
                            # Ордер исполнен - создаём событие об исполнении
                            log_info(self.user_id, f"📈 Ордер {order_id} был исполнен во время перезагрузки", "BaseStrategy")

                            # КРИТИЧЕСКИ ВАЖНО: Обновляем статус ордера в БД
                            try:
                                from database.db_trades import db_manager
                                await db_manager.update_order_status(
                                    order_id=order_id,
                                    status="FILLED",
                                    filled_price=Decimal(str(order_status.get("avgPrice", "0"))),
                                    filled_qty=Decimal(str(order_status.get("cumExecQty", "0"))),
                                    fee=Decimal(str(order_status.get("cumExecFee", "0")))
                                )
                                log_debug(self.user_id, f"Статус ордера {order_id} обновлён в БД: FILLED", "BaseStrategy")
                            except Exception as db_error:
                                log_error(self.user_id, f"Ошибка обновления статуса ордера {order_id} в БД: {db_error}", "BaseStrategy")

                            filled_event = OrderFilledEvent(
                                user_id=self.user_id,
                                order_id=order_id,
                                symbol=self.symbol,
                                side=order_data.get("side", "Buy"),
                                qty=Decimal(str(order_status.get("cumExecQty", "0"))),
                                price=Decimal(str(order_status.get("avgPrice", "0"))),
                                fee=Decimal(str(order_status.get("cumExecFee", "0")))
                            )
                            await self._handle_order_filled(filled_event)
                        else:
                            # КРИТИЧЕСКИ ВАЖНО: Обновляем статус отменённых ордеров в БД
                            log_info(self.user_id, f"ℹ️ Ордер {order_id} имеет статус {status}, удаляю из отслеживания", "BaseStrategy")
                            try:
                                from database.db_trades import db_manager
                                await db_manager.update_order_status(
                                    order_id=order_id,
                                    status=status.upper(),
                                    filled_price=None,
                                    filled_qty=None,
                                    fee=None
                                )
                                log_debug(self.user_id, f"Статус ордера {order_id} обновлён в БД: {status.upper()}", "BaseStrategy")
                            except Exception as db_error:
                                log_error(self.user_id, f"Ошибка обновления статуса ордера {order_id} в БД: {db_error}", "BaseStrategy")

                    orders_to_remove.append(order_id)

            # Удаляем неактивные ордера
            for order_id in orders_to_remove:
                self.active_orders.pop(order_id, None)

            if orders_to_remove:
                log_info(self.user_id, f"🧹 Удалено {len(orders_to_remove)} неактивных ордеров для {self.symbol}", "BaseStrategy")

        except Exception as e:
            log_error(self.user_id, f"Ошибка синхронизации ордеров после перезагрузки для {self.symbol}: {e}", "BaseStrategy")

    async def _notify_user_about_recovery(self, saved_state: Dict[str, Any]):
        """Уведомляет пользователя о восстановлении стратегии после перезагрузки"""
        try:
            if not self.bot:
                return

            last_saved = datetime.fromisoformat(saved_state["last_saved"])
            downtime = datetime.now() - last_saved

            # Формируем красивое время простоя
            if downtime.total_seconds() < 60:
                downtime_str = f"{int(downtime.total_seconds())} сек."
            elif downtime.total_seconds() < 3600:
                downtime_str = f"{int(downtime.total_seconds() / 60)} мин."
            else:
                hours = int(downtime.total_seconds() / 3600)
                minutes = int((downtime.total_seconds() % 3600) / 60)
                downtime_str = f"{hours}ч {minutes}мин"

            active_orders_count = len(self.active_orders)

            message = (
                f"🔄 <b>Восстановление после перезагрузки</b>\n\n"
                f"📊 Стратегия: <b>{self.strategy_type.value}</b>\n"
                f"💱 Символ: <b>{self.symbol}</b>\n"
                f"⏰ Время простоя: <b>{downtime_str}</b>\n"
            )

            if active_orders_count > 0:
                message += f"📋 Активных ордеров: <b>{active_orders_count}</b>\n"
                message += f"✅ Отслеживание ордеров возобновлено"
            else:
                message += f"ℹ️ Активных ордеров не обнаружено"

            await self.bot.send_message(
                chat_id=self.user_id,
                text=message,
                parse_mode="HTML"
            )

        except Exception as e:
            log_error(self.user_id, f"Ошибка отправки уведомления о восстановлении: {e}", "BaseStrategy")

    async def _strategy_specific_recovery(self, additional_data: Dict[str, Any]):
        """
        Переопределяется в конкретных стратегиях для дополнительного восстановления состояния.
        Например, Signal Scalper может восстановить состояние усреднения.
        """
        pass

    async def clear_strategy_state(self):
        """Очищает сохранённое состояние стратегии из Redis при штатном завершении"""
        try:
            state_key = f"strategy_state:{self.user_id}:{self.symbol}:{self.strategy_type.value}"
            await redis_manager.redis_client.delete(state_key)
            log_debug(self.user_id, f"Состояние стратегии {self.symbol} очищено из Redis", "BaseStrategy")
        except Exception as e:
            log_error(self.user_id, f"Ошибка очистки состояния стратегии {self.symbol}: {e}", "BaseStrategy")