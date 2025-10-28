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
    
    def __init__(self, user_id: int, symbol: str, signal_data: Dict[str, Any], api: BybitAPI, event_bus: EventBus, bot: "Bot", config: Optional[Dict] = None, account_priority: int = 1):
        """
        Инициализация базовой стратегии

        Args:
            user_id: ID пользователя
            symbol: Торговый символ
            signal_data: Данные сигнала от MetaStrategist
            account_priority: Приоритет аккаунта (1=PRIMARY, 2=SECONDARY, 3=TERTIARY)
        """
        self.user_id = user_id
        self.symbol = symbol
        self.signal_data = signal_data
        self.api: BybitAPI = api
        self.event_bus = event_bus
        self.bot = bot
        self.config: Dict[str, Any] = config or {}
        self.account_priority = account_priority  # Multi-Account Support

        # КРИТИЧНО: Флаг восстановления после перезапуска бота
        # True = бот перезапущен, проверка БД/биржи РАЗРЕШЕНА
        # False = обычный старт, проверка БД/биржи ЗАПРЕЩЕНА
        self.is_bot_restart_recovery = False

        # Добавлена проверка для раннего обнаружения проблем временно потом удалить
        if not self.bot:
            log_warning(self.user_id,
                        f"Экземпляр Telegram бота не был передан в стратегию {self.strategy_type.value}. Уведомления будут отключены.",
                        module_name=__name__)

        # Состояние стратегии
        self.is_running = False
        # КРИТИЧНО: Добавляем account_priority в ID для уникальности каждого бота
        bot_suffix = f"_bot{account_priority}" if account_priority else ""
        self.strategy_id = f"{self.user_id}_{symbol}{bot_suffix}_{datetime.now().strftime('%Y%m%d_%H%M%S')}"
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

        # Recovery handler (инициализируется в дочерних классах)
        self.recovery_handler = None

        log_info(self.user_id,f"Инициализирована стратегия {self.strategy_type.value} для {symbol} (ID: {self.strategy_id})", module_name=__name__)


    def _get_bot_prefix(self) -> str:
        """
        Возвращает префикс для уведомлений с приоритетом бота (Multi-Account Support).

        Returns:
            str: Префикс вида "🥇 Bot 1" или "🥈 Bot 2" или "🥉 Bot 3"
        """
        priority_emojis = {1: "🥇", 2: "🥈", 3: "🥉"}
        emoji = priority_emojis.get(self.account_priority, "🤖")
        return f"{emoji} Bot {self.account_priority}"

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
    def _format_duration_russian(duration_seconds: int) -> str:
        """
        Форматирует длительность в читаемый вид на русском языке.

        Args:
            duration_seconds: Длительность в секундах

        Returns:
            Строка вида "1 минуту 20 секунд" или "2 часа 15 минут 30 секунд"
        """
        hours = duration_seconds // 3600
        minutes = (duration_seconds % 3600) // 60
        seconds = duration_seconds % 60

        parts = []

        # Часы
        if hours > 0:
            if hours == 1:
                parts.append("1 час")
            elif 2 <= hours <= 4:
                parts.append(f"{hours} часа")
            else:
                parts.append(f"{hours} часов")

        # Минуты
        if minutes > 0:
            if minutes == 1:
                parts.append("1 минуту")
            elif 2 <= minutes <= 4:
                parts.append(f"{minutes} минуты")
            else:
                parts.append(f"{minutes} минут")

        # Секунды
        if seconds > 0 or len(parts) == 0:  # Показываем секунды если есть, или если ничего нет
            if seconds == 1:
                parts.append("1 секунду")
            elif 2 <= seconds <= 4:
                parts.append(f"{seconds} секунды")
            else:
                parts.append(f"{seconds} секунд")

        return " ".join(parts)

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
        # Комиссия тейкера из конфига
        from core.settings_config import EXCHANGE_FEES
        from core.enums import ExchangeType
        taker_fee_rate = EXCHANGE_FEES[ExchangeType.BYBIT]['taker'] / Decimal('100')  # 0.1%

        # Расчет комиссии при закрытии позиции
        estimated_close_fee = entry_price * qty * taker_fee_rate

        # ТЕХНИЧЕСКИЙ ПАРАМЕТР: Добавляем 5% буфер для точности расчётов
        # Компенсирует погрешности и микро-движения цены, чтобы SL не сработал раньше времени
        # НЕ является стратегическим параметром - это математическая константа
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

            # Добавляем комиссию при закрытии (из конфига)
            from core.settings_config import EXCHANGE_FEES
            from core.enums import ExchangeType
            taker_fee_rate = EXCHANGE_FEES[ExchangeType.BYBIT]['taker'] / Decimal('100')
            estimated_close_fee = sl_price * quantity * taker_fee_rate
            total_expected_loss = actual_loss + estimated_close_fee

            return sl_price, total_expected_loss

        except Exception as e:
            log_error(self.user_id, f"КРИТИЧЕСКАЯ ОШИБКА расчета SL: {e}. Ордер НЕ будет создан!", "base_strategy")
            # Выбрасываем исключение - не даём открыть сделку с неправильным SL
            # Уведомление пользователю будет отправлено на уровне вызывающего кода
            raise ValueError(f"Не удалось рассчитать Stop Loss для {self.symbol}. Проверьте параметры конфигурации (max_loss_usd, averaging_stop_loss_percent).")

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

    # МЕТОД _await_order_fill() УДАЛЁН!
    # Market ордера исполняются мгновенно - WebSocket обработает событие исполнения.
    # API-polling был медленным и вызывал ложные отмены уже исполненных ордеров.


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

    async def _handle_order_update(self, event: OrderUpdateEvent):
        """
        Обработка обновления статуса ордера (отмена, отклонение и т.д.)

        КРИТИЧНО: Обновляет статус ордера в БД при любых изменениях со стороны биржи.
        Это гарантирует, что БД всегда актуальна, даже если ордер закрыт вручную.
        """
        try:
            order_data = event.order_data
            order_id = order_data.get("orderId")
            status = order_data.get("orderStatus")
            symbol = order_data.get("symbol")

            # Проверяем, что это ордер нашей стратегии
            if symbol != self.symbol:
                return

            # Проверяем, что это наш ордер (есть в active_orders или в БД)
            if order_id not in self.active_orders:
                # Проверяем в БД
                from database.db_trades import db_manager
                db_order = await db_manager.get_order_by_exchange_id(order_id, self.user_id)
                if not db_order or db_order.get('user_id') != self.user_id:
                    return  # Это не наш ордер

            log_info(self.user_id,
                    f"📋 Обновление ордера {order_id}: {status} ({symbol})",
                    module_name=__name__)

            # КРИТИЧНО: Обновляем статус в БД
            from database.db_trades import db_manager

            # Маппинг статусов Bybit → БД
            status_map = {
                "Cancelled": "CANCELLED",
                "Rejected": "REJECTED",
                "Filled": "FILLED",
                "PartiallyFilled": "PARTIALLY_FILLED",
                "New": "NEW",
                "Untriggered": "PENDING"
            }

            db_status = status_map.get(status, status.upper())

            # Обновляем статус в БД
            await db_manager.update_order_status(
                order_id=order_id,
                status=db_status,
                filled_quantity=Decimal(str(order_data.get("cumExecQty", "0"))),
                average_price=Decimal(str(order_data.get("avgPrice", "0"))) if order_data.get("avgPrice") else None
            )

            # Если ордер отменён/отклонён, удаляем из active_orders
            if status in ["Cancelled", "Rejected"]:
                if order_id in self.active_orders:
                    del self.active_orders[order_id]
                    log_warning(self.user_id,
                              f"⚠️ Ордер {order_id} отменён/отклонён - удалён из активных",
                              module_name=__name__)

                    # Уведомляем пользователя
                    reason = "отменён" if status == "Cancelled" else "отклонён биржей"
                    await self._send_notification_async(
                        f"⚠️ <b>Ордер {reason}</b>\n\n"
                        f"Символ: <code>{symbol}</code>\n"
                        f"ID: <code>{order_id[:8]}...</code>\n"
                        f"Статус: {status}"
                    )

        except Exception as e:
            log_error(self.user_id, f"Ошибка обработки OrderUpdateEvent: {e}", module_name=__name__)



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

            # КРИТИЧНО: Обработка обновления статуса ордера (отмена, отклонение и т.д.)
            elif isinstance(event, OrderUpdateEvent):
                await self._handle_order_update(event)

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

            # Загрузка конфигурации конкретной стратегии
            strategy_config_enum = ConfigType[f"STRATEGY_{self.strategy_type.value.upper()}"]
            strategy_config = await redis_manager.get_config(self.user_id, strategy_config_enum)

            if not strategy_config:
                log_error(self.user_id,f"Конфигурация стратегии {self.strategy_type.value} не найдена", module_name=__name__)
                return

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

        КРИТИЧНО: Сохраняет ордер в БД ПЕРЕД отправкой на биржу (предотвращает race condition с WebSocket)
        """
        try:
            if not self.api:
                log_error(self.user_id, "API клиент не инициализирован в стратегии.", module_name=__name__)
                return None

            from database.db_trades import db_manager
            import time
            import random

            # Генерируем уникальный client_order_id для раннего сохранения
            timestamp_ms = int(time.time() * 1000)
            random_suffix = random.randint(1000, 9999)
            client_order_id = f"bot{self.account_priority}_{self.symbol}_{timestamp_ms}_{random_suffix}"

            # Определяем order_purpose ДО размещения
            if reduce_only:
                order_purpose = 'CLOSE'
            else:
                has_active_position = getattr(self, 'position_active', False)
                if has_active_position:
                    order_purpose = 'AVERAGING'
                else:
                    order_purpose = 'OPEN'

            # Получаем параметры
            leverage = int(float(self.get_config_value("leverage", 1)))
            trade_id = getattr(self, 'active_trade_db_id', None)

            # ШАГ 1: СОХРАНЯЕМ В БД ПЕРЕД отправкой на биржу (order_id пока неизвестен)
            try:
                db_id = await db_manager.save_order_full(
                    user_id=self.user_id,
                    symbol=self.symbol,
                    side=side,
                    order_type=order_type,
                    quantity=qty,
                    price=price or Decimal('0'),
                    order_id="PENDING",  # Временное значение, обновим после размещения
                    strategy_type=self.strategy_type.value,
                    order_purpose=order_purpose,
                    leverage=leverage,
                    trade_id=trade_id,
                    client_order_id=client_order_id,
                    bot_priority=getattr(self, 'account_priority', 1),
                    metadata={
                        "stop_loss": str(stop_loss) if stop_loss else None,
                        "take_profit": str(take_profit) if take_profit else None,
                        "reduce_only": reduce_only,
                        "created_by": "base_strategy_place_order"
                    }
                )
                log_info(self.user_id, f"📝 Ордер сохранён в БД ПЕРЕД отправкой (DB_ID={db_id}, client={client_order_id})", module_name=__name__)
            except Exception as db_error:
                log_error(self.user_id, f"КРИТИЧЕСКАЯ ОШИБКА: не удалось сохранить ордер в БД перед отправкой: {db_error}", module_name=__name__)
                return None

            # ДИАГНОСТИКА: Логируем API ключ для проверки правильности распределения
            api_key_masked = f"{self.api.api_key[:4]}...{self.api.api_key[-4:]}" if len(self.api.api_key) > 8 else "***"
            log_info(self.user_id, f"[Bot #{self.account_priority}] Размещение ордера {side} {qty} {self.symbol} | API: {api_key_masked}", module_name=__name__)

            # ШАГ 2: ОТПРАВЛЯЕМ на биржу
            order_id = await self.api.place_order(
                symbol=self.symbol, side=side, order_type=order_type, qty=qty, price=price,
                stop_loss=stop_loss, take_profit=take_profit, reduce_only=reduce_only
            )

            if order_id:
                self.active_orders[order_id] = {"order_id": order_id, "status": "New"}

                # ШАГ 3: ОБНОВЛЯЕМ order_id и status в БД (меняем "PENDING" на настоящий ID и status на 'NEW')
                try:
                    await db_manager._execute_query(
                        "UPDATE orders SET order_id = $1, status = 'NEW', updated_at = NOW() WHERE client_order_id = $2",
                        (order_id, client_order_id)
                    )
                    log_info(self.user_id, f"✅ Ордер обновлён в БД: order_id={order_id} status=NEW (client={client_order_id})", module_name=__name__)
                except Exception as update_error:
                    log_error(self.user_id, f"Ошибка обновления order_id в БД: {update_error}", module_name=__name__)

                # Сохраняем состояние после размещения ордера
                await self.save_strategy_state({"last_action": "order_placed", "order_id": order_id})
                log_info(self.user_id, f"Ордер {order_id} ({side} {qty} {self.symbol}) отправлен на биржу.",
                         module_name=__name__)
                return order_id
            else:
                # Биржа не вернула ID - удаляем запись из БД
                log_error(self.user_id, f"Не удалось разместить ордер для {self.symbol} (API не вернул ID). Удаляю из БД.", module_name=__name__)
                try:
                    await db_manager._execute_query(
                        "DELETE FROM orders WHERE client_order_id = $1",
                        (client_order_id,)
                    )
                except:
                    pass
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
                        filled_quantity=None,
                        average_price=None
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

    def _send_notification_async(self, text: str, parse_mode: str = "HTML"):
        """
        Отправляет уведомление асинхронно в фоне, не блокируя основной код.

        КРИТИЧНО: Используется для предотвращения блокировок при отправке Telegram-сообщений
        из методов, защищённых @strategy_locked.

        Args:
            text: Текст уведомления
            parse_mode: Режим парсинга (HTML/Markdown)
        """
        async def _send():
            try:
                # ДИАГНОСТИКА: Логируем попытку отправки
                log_debug(self.user_id, f"🔔 Попытка отправки уведомления: bot={'существует' if self.bot else 'None'}", "base_strategy")

                if not self.bot:
                    log_error(self.user_id, "❌ Telegram bot не инициализирован! Уведомление не отправлено.", "base_strategy")
                    log_error(self.user_id, f"Текст пропущенного уведомления:\n{text[:200]}...", "base_strategy")
                    return

                # Отправляем уведомление
                await self.bot.send_message(self.user_id, text, parse_mode=parse_mode)
                log_debug(self.user_id, "✅ Уведомление успешно отправлено в Telegram", "base_strategy")

            except Exception as e:
                log_error(self.user_id, f"❌ Ошибка асинхронной отправки уведомления: {e}", "base_strategy")
                log_error(self.user_id, f"Текст неотправленного уведомления:\n{text[:200]}...", "base_strategy")

        # Запускаем отправку в фоне с обработчиком завершения
        task = asyncio.create_task(_send())

        # Добавляем callback для логирования необработанных исключений
        def _task_done_callback(task_obj):
            try:
                task_obj.result()  # Получаем результат, чтобы поймать исключение
            except Exception as e:
                log_error(self.user_id, f"🔥 НЕОБРАБОТАННОЕ ИСКЛЮЧЕНИЕ в задаче отправки уведомления: {e}", "base_strategy")

        task.add_done_callback(_task_done_callback)

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

            # Отправляем асинхронно чтобы не блокировать логику стратегии
            self._send_notification_async(text)
            log_info(self.user_id, f"Уведомление о запуске стратегии {strategy_name} отправлено", "base_strategy")

        except Exception as e:
            log_error(self.user_id, f"Ошибка отправки уведомления о запуске стратегии: {e}", "base_strategy")

    async def _send_trade_open_notification(self, side: str, price: Decimal, quantity: Decimal,
                                            intended_amount: Optional[Decimal] = None, signal_price: Optional[Decimal] = None):
        """
        Отправляет уведомление об открытии позиции.

        ИСПРАВЛЕНО: Создание trade в БД теперь происходит в _handle_order_filled() для OPEN ордеров.
        Этот метод ТОЛЬКО отправляет уведомление пользователю.
        """
        try:
            # ДИАГНОСТИКА: Логируем вызов метода и состояние bot
            log_info(self.user_id, f"🔔 _send_trade_open_notification вызван: side={side}, price={price}, qty={quantity}", "base_strategy")
            log_info(self.user_id, f"🤖 Состояние self.bot: {type(self.bot).__name__ if self.bot else 'None'}", "base_strategy")

            # Проверяем, что бот инициализирован
            if not self.bot:
                log_error(self.user_id, "Telegram бот не инициализирован. Уведомление об открытии сделки не отправлено.", "base_strategy")
                return

            side_text = "LONG 🟢" if side.lower() == 'buy' else "SHORT 🔴"
            strategy_name = self.strategy_type.value.replace('_', ' ').title()
            # Расчет actual_amount - реальная маржа (сумма, списанная с баланса)
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

            # КРИТИЧНО: Проверяем настройку enable_stop_loss (может меняться динамически!)
            # Блок SL показываем ВСЕГДА, но содержимое зависит от текущей настройки пользователя
            enable_stop_loss = self.get_config_value("enable_stop_loss", True)
            if enable_stop_loss:
                # SL включен - рассчитываем и показываем цену SL и убыток
                sl_price, sl_loss = self._get_stop_loss_info(side, price, quantity)
                sl_text = (
                    f"\n🛡️ {hbold('Stop Loss:')}\n"
                    f"▫️ {hbold('SL цена:')} {hcode(f'{sl_price:.4f} USDT')}\n"
                    f"▫️ {hbold('Ожидаемый убыток:')} {hcode(f'-{sl_loss:.2f} USDT')}"
                )
            else:
                # SL отключен - показываем предупреждение
                sl_text = (
                    f"\n🛡️ {hbold('Stop Loss:')} ⚠️ {hcode('ОТКЛЮЧЕН')}\n"
                    f"▫️ Позиция открыта без стоп-лосса"
                )

            # Multi-Account Support: добавляем префикс бота
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

            # Добавляем предупреждение, если фактическая сумма отличается от запрошенной
            if intended_amount:
                difference = abs(intended_amount - actual_amount)
                # Показываем предупреждение если:
                # 1. Разница больше 0.10 USDT (абсолютная разница)
                # 2. ИЛИ разница больше 0.5% от запрошенной суммы (относительная разница)
                absolute_threshold = Decimal('0.10')
                relative_threshold = intended_amount * Decimal('0.005')  # 0.5%

                if difference >= absolute_threshold or difference >= relative_threshold:
                    difference_percent = (difference / intended_amount) * Decimal('100')
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

            # Отправляем асинхронно чтобы не блокировать логику стратегии
            self._send_notification_async(text)
            log_info(self.user_id, "[TRACE] Уведомление об открытии сделки отправлено успешно.", "base_strategy")
        except Exception as e:
            log_error(self.user_id, f"Ошибка отправки уведомления об открытии сделки: {e}", "base_strategy")
            # Дополнительное логирование для диагностики
            log_error(self.user_id, f"Состояние бота: {'инициализирован' if self.bot else 'не инициализирован'}", "base_strategy")

    async def _send_averaging_notification(self, price: Decimal, quantity: Decimal, new_avg_price: Decimal,
                                           new_total_size: Decimal, side: Optional[str] = None,
                                           old_entry_price: Optional[Decimal] = None, old_size: Optional[Decimal] = None,
                                           current_pnl: Optional[Decimal] = None, loss_percent: Optional[Decimal] = None,
                                           trigger_percent: Optional[Decimal] = None, averaging_amount: Optional[Decimal] = None):
        """
        Отправляет МАКСИМАЛЬНО ИНФОРМАТИВНОЕ уведомление об усреднении позиции.

        Параметры:
            price: Цена усреднения
            quantity: Добавленный объем
            new_avg_price: Новая средняя цена входа
            new_total_size: Новый общий размер позиции
            side: Направление ("Buy"/"Sell")
            old_entry_price: Старая цена входа ДО усреднения
            old_size: Старый размер позиции ДО усреднения
            current_pnl: Текущий PnL на момент усреднения
            loss_percent: Процент убытка от маржи
            trigger_percent: Триггер усреднения (процент)
            averaging_amount: Сумма добавленной маржи в USDT
        """
        try:
            # Проверяем, что бот инициализирован
            if not self.bot:
                log_error(self.user_id, "Telegram бот не инициализирован. Уведомление об усреднении не отправлено.", "base_strategy")
                return

            strategy_name = self.strategy_type.value.replace('_', ' ').title()
            side_text = "LONG 🟢" if side and side.lower() == 'buy' else "SHORT 🔴"
            # Multi-Account Support: добавляем префикс бота
            bot_prefix = self._get_bot_prefix()

            # Рассчитываем общую маржу
            leverage = self._convert_to_decimal(self.get_config_value("leverage", 1.0))
            old_margin = (old_entry_price * old_size) / leverage if old_entry_price and old_size else Decimal('0')

            # ПРАВИЛЬНЫЙ расчёт: используем реальную накопленную маржу (initial + усреднения)
            # Для SignalScalper это self.current_total_margin, для других стратегий - расчёт от позиции
            if hasattr(self, 'current_total_margin') and self.current_total_margin > 0:
                total_margin = self.current_total_margin  # Реальная маржа (initial + все усреднения)
            else:
                # Fallback для стратегий без current_total_margin
                total_margin = (new_avg_price * new_total_size) / leverage

            # КРИТИЧНО: Проверяем настройку enable_stop_loss (может меняться динамически!)
            # Блок SL показываем ВСЕГДА, но содержимое зависит от текущей настройки пользователя
            enable_stop_loss = self.get_config_value("enable_stop_loss", True)

            if enable_stop_loss:
                # SL включен - проверяем наличие side для расчёта
                if side:
                    # Рассчитываем и показываем информацию о SL для новой позиции
                    # Используем реальный SL если он есть (для стратегий с усреднением)
                    if hasattr(self, 'stop_loss_price') and self.stop_loss_price:
                        sl_price = self.stop_loss_price
                        # Точный расчёт убытка на основе реального SL
                        is_long = side.lower() == 'buy'
                        if is_long:
                            actual_loss = (new_avg_price - sl_price) * new_total_size
                        else:
                            actual_loss = (sl_price - new_avg_price) * new_total_size

                        # Добавляем комиссию при закрытии (из конфига)
                        from core.settings_config import EXCHANGE_FEES
                        from core.enums import ExchangeType
                        taker_fee_rate = EXCHANGE_FEES[ExchangeType.BYBIT]['taker'] / Decimal('100')
                        estimated_close_fee = sl_price * new_total_size * taker_fee_rate
                        sl_loss = actual_loss + estimated_close_fee
                    else:
                        sl_price, sl_loss = self._get_stop_loss_info(side, new_avg_price, new_total_size)

                    # Формируем блок с информацией о SL
                    sl_text = (
                        f"🛡️ {hbold('ОБНОВЛЕННЫЙ STOP LOSS:')}\n"
                        f"▫️ {hbold('SL цена:')} {hcode(f'{sl_price:.4f} USDT')}\n"
                        f"▫️ {hbold('Ожидаемый убыток:')} {hcode(f'-{sl_loss:.2f} USDT')}"
                    )
                else:
                    # SL включен, но side не передан - невозможно рассчитать
                    log_warning(self.user_id, f"⚠️ SL включен, но параметр 'side' не передан для {self.symbol}. Расчёт SL невозможен.", "base_strategy")
                    sl_text = (
                        f"🛡️ {hbold('STOP LOSS:')} ⚠️ {hcode('НЕ РАССЧИТАН')}\n"
                        f"▫️ Направление позиции не определено"
                    )
            else:
                # SL отключен пользователем - показываем предупреждение
                sl_text = (
                    f"🛡️ {hbold('STOP LOSS:')} ⚠️ {hcode('ОТКЛЮЧЕН')}\n"
                    f"▫️ Усреднённая позиция без стоп-лосса"
                )

            # Формируем блок "ПОЧЕМУ произошло усреднение"
            trigger_reason = ""
            if loss_percent and trigger_percent and current_pnl:
                trigger_reason = (
                    f"⚠️ {hbold('ПРИЧИНА УСРЕДНЕНИЯ:')}\n"
                    f"▫️ {hbold('Убыток достиг:')} {hcode(f'{loss_percent:.2f}% от маржи')}\n"
                    f"▫️ {hbold('Триггер установлен на:')} {hcode(f'{trigger_percent:.2f}%')}\n"
                    f"▫️ {hbold('PnL на момент триггера:')} {hcode(f'{current_pnl:.2f} USDT')}\n\n"
                )

            # Формируем блок "ДО усреднения"
            before_block = ""
            if old_entry_price and old_size:
                before_block = (
                    f"📊 {hbold('ДО УСРЕДНЕНИЯ:')}\n"
                    f"▫️ {hbold('Цена входа:')} {hcode(f'{old_entry_price:.4f} USDT')}\n"
                    f"▫️ {hbold('Размер позиции:')} {hcode(str(old_size))}\n"
                    f"▫️ {hbold('Маржа:')} {hcode(f'{old_margin:.2f} USDT')}\n\n"
                )

            # Формируем блок "Операция усреднения"
            averaging_block = (
                f"➕ {hbold('ОПЕРАЦИЯ УСРЕДНЕНИЯ:')}\n"
                f"▫️ {hbold('Цена добавления:')} {hcode(f'{price:.4f} USDT')}\n"
                f"▫️ {hbold('Добавленный объем:')} {hcode(str(quantity))}\n"
            )
            if averaging_amount:
                averaging_block += f"▫️ {hbold('Добавленная маржа:')} {hcode(f'{averaging_amount:.2f} USDT')}\n"
            averaging_block += "\n"

            # Формируем блок "ПОСЛЕ усреднения"
            after_block = (
                f"📈 {hbold('ПОСЛЕ УСРЕДНЕНИЯ:')}\n"
                f"▫️ {hbold('Новая ср. цена:')} {hcode(f'{new_avg_price:.4f} USDT')}\n"
                f"▫️ {hbold('Новый общий размер:')} {hcode(str(new_total_size))}\n"
                f"▫️ {hbold('Общая маржа:')} {hcode(f'{total_margin:.2f} USDT')}\n\n"
            )

            # Собираем финальное уведомление
            text = (
                f"🔄 {hbold('ПОЗИЦИЯ УСРЕДНЕНА')} 🔄\n\n"
                f"▫️ {hbold('Аккаунт:')} {hcode(bot_prefix)}\n"
                f"▫️ {hbold('Стратегия:')} {hcode(strategy_name)}\n"
                f"▫️ {hbold('Символ:')} {hcode(self.symbol)}\n"
                f"▫️ {hbold('Направление:')} {side_text}\n\n"
                f"{trigger_reason}"
                f"{before_block}"
                f"{averaging_block}"
                f"{after_block}"
                f"{sl_text}"
            )

            # Отправляем асинхронно чтобы не блокировать логику стратегии
            self._send_notification_async(text)
            log_info(self.user_id, "✅ Максимально информативное уведомление об усреднении отправлено успешно.", "base_strategy")
        except Exception as e:
            log_error(self.user_id, f"Ошибка отправки уведомления об усреднении: {e}", "base_strategy")


    # strategies/base_strategy.py -> _send_trade_close_notification
    async def _send_trade_close_notification(self, pnl: Decimal, commission: Decimal = Decimal('0'), exit_price: Optional[Decimal] = None, entry_price: Optional[Decimal] = None, entry_time: Optional[datetime] = None):
        """
        Отправляет уведомление и обновляет статистику.

        ИСПРАВЛЕНО: Обновление trade в БД теперь происходит в _handle_order_filled() для CLOSE ордеров.
        Этот метод ТОЛЬКО отправляет уведомление и обновляет статистику.
        """
        try:
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

            # 3. Общая статистика пользователя вычисляется динамически из таблицы trades в get_user()

            # 4. Отправляем уведомление, если бот доступен
            if not self.bot:
                log_error(self.user_id, "Бот не инициализирован. Уведомление о закрытии сделки не отправлено.",
                          "base_strategy")
                return

            strategy_name = self.strategy_type.value.replace('_', ' ').title()
            # Multi-Account Support: добавляем префикс бота
            bot_prefix = self._get_bot_prefix()

            # Рассчитываем длительность сделки если доступно entry_time
            duration_line = ""
            if entry_time:
                # КРИТИЧНО: Правильно конвертируем entry_time в локальное время
                if entry_time.tzinfo is not None:
                    # КОНВЕРТИРУЕМ в локальный timezone ПЕРЕД удалением timezone info
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
                # Прибыльная сделка
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
                # Убыточная сделка
                icon = "📉"
                result_text = "УБЫТОК 🔻"
                # Показываем общий убыток: модуль PnL уже включает комиссии
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
            # Отправляем асинхронно чтобы не блокировать логику стратегии
            self._send_notification_async(text)
            log_info(self.user_id, "Уведомление о закрытии сделки отправлено успешно.", "base_strategy")
        except Exception as e:
            log_error(self.user_id, f"Ошибка отправки уведомления о закрытии сделки: {e}", "base_strategy")

    async def _monitor_active_position(self):
        """
        Фоновая задача-"сторож". Раз в минуту проверяет через API,
        не закрылась ли позиция без ведома стратегии (из-за сбоя WebSocket).
        """
        log_info(self.user_id, f"Запущен API-монитор для позиции {self.symbol}", "BaseStrategy")

        while self.is_running and self.position_size and self.position_size > 0:
            try:
                await asyncio.sleep(60)  # Проверка раз в минуту

                if not self.is_running or not self.position_size or self.position_size == 0:
                    break  # Выходим, если стратегия остановлена или позиция закрыта штатно

                positions = await self.api.get_positions(symbol=self.symbol)

                # Если API вернул пустой список, значит позиции на бирже больше нет
                if not positions:
                    log_warning(self.user_id,
                                f"ДЕСИНХРОНИЗАЦИЯ! API сообщает, что позиции по {self.symbol} нет, но стратегия об этом не знала. Принудительное закрытие.",
                                "BaseStrategy")

                    # ИСПРАВЛЕНО: Метод get_last_trade не существует в BybitAPI
                    # Вместо этого просто сбрасываем флаг position_active и останавливаем стратегию
                    log_warning(self.user_id,
                               f"Позиция для {self.symbol} закрыта вне стратегии. Останавливаю отслеживание.",
                               "BaseStrategy")

                    # Сбрасываем флаг активной позиции
                    self.position_active = False
                    self.position_size = Decimal('0')

                    # Останавливаем стратегию
                    await self.stop("position_closed_externally")

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
                'active_trade_config', 'peak_profit_usd', 'hold_signal_counter',
                '_last_known_price', 'initial_margin_usd'  # КРИТИЧНО для координатора: расчет PnL%
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

            # КРИТИЧНО: Устанавливаем is_running = True чтобы стратегия получала события
            # Без этого handle_event() будет игнорировать все события (проверка if not self.is_running: return)
            self.is_running = True

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
                                   'last_averaging_percent', 'intended_order_amount', 'initial_margin_usd',
                                   '_last_known_price', 'current_total_margin']:
                        setattr(self, attr_name, Decimal(str(attr_value)) if attr_value else None)
                    elif attr_name == 'processed_orders':
                        setattr(self, attr_name, set(attr_value) if attr_value else set())
                    else:
                        setattr(self, attr_name, attr_value)

                    log_debug(self.user_id, f"Восстановлен атрибут {attr_name} = {attr_value}", "BaseStrategy")

            # КРИТИЧЕСКАЯ ПРОВЕРКА: Синхронизируем состояние position_active с реальной позицией на бирже
            await self._sync_position_state_with_exchange()

            # Специальная проверка и восстановление связи с БД
            if hasattr(self, 'active_trade_db_id') and self.active_trade_db_id:
                log_info(self.user_id, f"Восстановлена связь с записью БД: trade_id={self.active_trade_db_id}", "BaseStrategy")

            # Проверяем актуальные ордера на бирже и синхронизируем состояние
            await self._sync_orders_after_restart()

            # КРИТИЧНО: Конкретная стратегия может переопределить этот метод для дополнительного восстановления
            # Вызываем ДО уведомления, чтобы все компоненты были восстановлены
            await self._strategy_specific_recovery(saved_state.get("additional_data", {}))

            # Уведомляем пользователя о восстановлении ПОСЛЕ полного восстановления всех компонентов
            await self._notify_user_about_recovery(saved_state)

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

                    # КРИТИЧНО: Проверяем статус в БД ПЕРЕД обработкой
                    from database.db_trades import db_manager
                    db_order = await db_manager.get_order_by_exchange_id(order_id, self.user_id)
                    db_status = db_order.get('status') if db_order else None

                    if db_status == 'FILLED':
                        # Ордер УЖЕ обработан ранее - пропускаем
                        log_info(self.user_id, f"⏭️ Ордер {order_id} уже обработан (статус в БД: FILLED), пропускаю", "BaseStrategy")
                        orders_to_remove.append(order_id)
                        continue

                    # Проверяем статус ордера через историю биржи
                    order_status = await self.api.get_order_status(order_id)

                    # КРИТИЧНО: Проверка границ и валидности данных
                    if not order_status or not isinstance(order_status, dict):
                        log_warning(self.user_id, f"⚠️ Не удалось получить статус ордера {order_id} с биржи", "BaseStrategy")
                        orders_to_remove.append(order_id)
                        continue

                    if order_status:
                        status = order_status.get("orderStatus", "Unknown")
                        if status == "Filled":
                            # Ордер исполнен - проверяем его назначение (order_purpose)
                            log_info(self.user_id, f"📈 Ордер {order_id} был исполнен во время перезагрузки", "BaseStrategy")

                            order_purpose = db_order.get('order_purpose') if db_order else None

                            # ✅ КРИТИЧНО: Проверяем, был ли этот ордер УЖЕ обработан до перезагрузки
                            # Если ордер OPEN/AVERAGING и позиция уже активна - значит ордер был обработан ранее
                            # ИЗБЕГАЕМ ПОВТОРНОЙ ОБРАБОТКИ старых ордеров!
                            if order_purpose in ('OPEN', 'AVERAGING') and getattr(self, 'position_active', False):
                                log_warning(self.user_id,
                                          f"⚠️ Ордер {order_id} ({order_purpose}) уже обработан ранее (позиция активна). "
                                          f"ПРОПУСКАЕМ повторную обработку для предотвращения ложных усреднений!",
                                          "BaseStrategy")

                                # Обновляем статус в БД без отправки события
                                try:
                                    await db_manager.update_order_status(
                                        order_id=order_id,
                                        status="FILLED",
                                        filled_quantity=Decimal(str(order_status.get("cumExecQty", "0"))),
                                        average_price=Decimal(str(order_status.get("avgPrice", "0")))
                                    )
                                    log_info(self.user_id,
                                           f"✅ Статус старого ордера {order_id} обновлён в БД без повторной обработки",
                                           "BaseStrategy")
                                except Exception as db_error:
                                    log_error(self.user_id, f"Ошибка обновления статуса ордера {order_id}: {db_error}", "BaseStrategy")

                                orders_to_remove.append(order_id)
                                continue

                            # ✅ УНИФИЦИРОВАНО: ВСЕ типы ордеров (OPEN, AVERAGING, CLOSE) обрабатываются ОДИНАКОВО
                            # Генерируем OrderFilledEvent и передаём в _handle_order_filled()
                            # Это гарантирует что CLOSE ордера обновляют trade через ЕДИНУЮ точку входа в стратегиях

                            log_info(self.user_id, f"🔄 Восстановление {order_purpose or 'UNKNOWN'} ордера {order_id}", "BaseStrategy")

                            # КРИТИЧЕСКИ ВАЖНО: Обновляем статус ордера в БД
                            try:
                                await db_manager.update_order_status(
                                    order_id=order_id,
                                    status="FILLED",
                                    filled_quantity=Decimal(str(order_status.get("cumExecQty", "0"))),
                                    average_price=Decimal(str(order_status.get("avgPrice", "0")))
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

            # ИСПРАВЛЕНО: Проверяем наличие ПОЗИЦИИ, а не только ордеров
            has_position = getattr(self, 'position_active', False)
            position_size = getattr(self, 'position_size', Decimal('0'))
            entry_price = getattr(self, 'entry_price', Decimal('0'))
            active_direction = getattr(self, 'active_direction', None)
            active_orders_count = len(self.active_orders)

            message = (
                f"🔄 <b>Восстановление после перезагрузки</b>\n\n"
                f"📊 Стратегия: <b>{self.strategy_type.value}</b>\n"
                f"💱 Символ: <b>{self.symbol}</b>\n"
                f"⏰ Время простоя: <b>{downtime_str}</b>\n"
            )

            # Показываем информацию о позиции если она есть
            if has_position and position_size > 0:
                direction_emoji = "🟢" if active_direction == "LONG" else "🔴"
                message += (
                    f"\n{direction_emoji} <b>Активная позиция:</b>\n"
                    f"▫️ Направление: <b>{active_direction}</b>\n"
                    f"▫️ Размер: <b>{position_size}</b>\n"
                    f"▫️ Цена входа: <b>{entry_price:.4f} USDT</b>\n"
                )

            # Показываем информацию об ордерах
            if active_orders_count > 0:
                message += f"\n📋 Активных ордеров: <b>{active_orders_count}</b>\n"
                message += f"✅ Отслеживание возобновлено"
            elif not has_position:
                # Показываем "ордеров нет" только если НЕТ и позиции
                message += f"\nℹ️ Активных позиций и ордеров нет"
            else:
                # Есть позиция, но нет ордеров - это нормально
                message += f"\n✅ Мониторинг позиции возобновлён"

            # Отправляем асинхронно чтобы не блокировать логику стратегии
            self._send_notification_async(message)

        except Exception as e:
            log_error(self.user_id, f"Ошибка отправки уведомления о восстановлении: {e}", "BaseStrategy")

    async def _strategy_specific_recovery(self, additional_data: Dict[str, Any]):
        """
        Переопределяется в конкретных стратегиях для дополнительного восстановления состояния.
        Стратегии с recovery handler делегируют восстановление ему.
        """
        # Если у стратегии есть recovery handler - используем его
        if self.recovery_handler:
            log_info(
                self.user_id,
                f"Делегирование восстановления recovery handler для {self.symbol}",
                "BaseStrategy"
            )
            return await self.recovery_handler.recover(additional_data)

        # Для стратегий без recovery handler - пустая реализация (переопределяется в наследниках)
        pass

    async def clear_strategy_state(self):
        """Очищает сохранённое состояние стратегии из Redis при штатном завершении"""
        try:
            state_key = f"strategy_state:{self.user_id}:{self.symbol}:{self.strategy_type.value}"
            await redis_manager.redis_client.delete(state_key)
            log_debug(self.user_id, f"Состояние стратегии {self.symbol} очищено из Redis", "BaseStrategy")
        except Exception as e:
            log_error(self.user_id, f"Ошибка очистки состояния стратегии {self.symbol}: {e}", "BaseStrategy")

    async def _sync_position_state_with_exchange(self):
        """
        КРИТИЧЕСКИ ВАЖНЫЙ МЕТОД!
        Синхронизирует состояние position_active с реальной позицией на бирже.
        Предотвращает повторное открытие позиции при восстановлении.
        """
        try:
            log_info(self.user_id, f"🔍 Синхронизирую состояние позиции для {self.symbol} с биржей...", "BaseStrategy")

            # Получаем реальные позиции с биржи
            positions = await self.api.get_positions(symbol=self.symbol)

            has_real_position = False
            real_position_size = Decimal('0')
            real_entry_price = Decimal('0')
            real_side = None

            if positions:
                for position in positions:
                    position_size = self._convert_to_decimal(position.get('size', 0))
                    if position_size > 0:
                        has_real_position = True
                        real_position_size = position_size
                        # ИСПРАВЛЕНО: Используем avgPrice вместо entryPrice (Bybit API v5)
                        real_entry_price = self._convert_to_decimal(position.get('avgPrice', 0))
                        real_side = position.get('side', 'Buy')
                        break

            # Получаем состояние position_active из восстановленных данных
            saved_position_active = getattr(self, 'position_active', False)

            log_info(self.user_id, f"📊 Анализ состояния позиции {self.symbol}:", "BaseStrategy")
            log_info(self.user_id, f"   Сохранённое состояние: position_active={saved_position_active}", "BaseStrategy")
            log_info(self.user_id, f"   Реальная позиция на бирже: размер={real_position_size}, цена={real_entry_price}", "BaseStrategy")

            if has_real_position:
                # КРИТИЧЕСКИ ВАЖНО: Есть позиция на бирже
                if not saved_position_active:
                    # Состояние не синхронно! Восстанавливаем из биржи
                    log_warning(self.user_id, f"⚠️ НАЙДЕНА РАССИНХРОНИЗАЦИЯ! На бирже есть позиция {self.symbol}, но стратегия не знает об этом", "BaseStrategy")

                    # Восстанавливаем состояние позиции из биржи
                    self.position_active = True
                    self.position_size = real_position_size
                    self.entry_price = real_entry_price
                    self.active_direction = "LONG" if real_side == "Buy" else "SHORT"

                    # КРИТИЧНО: Восстанавливаем initial_margin_usd для SignalScalper
                    if hasattr(self, 'initial_margin_usd'):
                        leverage = self._convert_to_decimal(self.get_config_value("leverage", 1.0))
                        position_value = real_entry_price * real_position_size
                        self.initial_margin_usd = position_value / leverage
                        log_info(self.user_id, f"💰 Восстановлена начальная маржа: ${self.initial_margin_usd:.2f} (leverage={leverage})", "BaseStrategy")

                    # КРИТИЧНО: Сбрасываем флаг ожидания для разблокировки обработки цен
                    if hasattr(self, 'is_waiting_for_trade'):
                        self.is_waiting_for_trade = False
                        log_info(self.user_id, f"🔓 Сброшен флаг is_waiting_for_trade для разблокировки обработки цен", "BaseStrategy")

                    log_info(self.user_id, f"✅ ВОССТАНОВЛЕНО состояние позиции: размер={real_position_size}, цена={real_entry_price}, направление={self.active_direction}", "BaseStrategy")
                else:
                    log_info(self.user_id, f"✅ Состояние позиции синхронно с биржей", "BaseStrategy")
            else:
                # На бирже нет позиции
                if saved_position_active:
                    # Состояние не синхронно! Сбрасываем флаг
                    log_warning(self.user_id, f"⚠️ НАЙДЕНА РАССИНХРОНИЗАЦИЯ! Стратегия считает что есть позиция, но на бирже её нет", "BaseStrategy")

                    self.position_active = False
                    self.position_size = Decimal('0')
                    self.entry_price = Decimal('0')
                    self.active_direction = None

                    log_info(self.user_id, f"✅ СБРОШЕНО состояние позиции (на бирже позиции нет)", "BaseStrategy")
                else:
                    log_info(self.user_id, f"✅ Состояние корректно: нет позиции ни в стратегии, ни на бирже", "BaseStrategy")

        except Exception as e:
            log_error(self.user_id, f"❌ Ошибка синхронизации состояния позиции для {self.symbol}: {e}", "BaseStrategy")