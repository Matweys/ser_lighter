"""
Базовая стратегия для многопользовательской торговой системы
Реализует событийно-ориентированную архитектуру с динамическими настройками
"""
import asyncio
import json
from abc import ABC, abstractmethod
from decimal import Decimal, getcontext
from typing import Dict, Any, Optional, List
from datetime import datetime, timedelta
from aiogram import Bot
from core.logger import log_info, log_error, log_warning
from core.enums import StrategyType, PositionSide, ConfigType
from core.events import (
    EventType, BaseEvent, SignalEvent, PriceUpdateEvent,
    OrderFilledEvent, PositionUpdateEvent, PositionClosedEvent,
    UserSettingsChangedEvent, EventBus
)
from cache.redis_manager import redis_manager
from api.bybit_api import BybitAPI
from aiogram.utils.markdown import hbold, hcode



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

        log_info(self.user_id,f"Инициализирована стратегия {self.strategy_type.value} для {symbol} (ID: {self.strategy_id})",
            module_name=__name__)


    @staticmethod
    def _convert_to_decimal( value: Any) -> Decimal:
        """Безопасное преобразование в Decimal"""
        if isinstance(value, Decimal):
            return value
        try:
            return Decimal(str(value))
        except (ValueError, TypeError):
            return Decimal('0')

    @abstractmethod
    def _get_strategy_type(self) -> StrategyType:
        """Возвращает тип стратегии"""
        pass
        
    @abstractmethod
    async def _execute_strategy_logic(self):
        """Основная логика стратегии (реализуется в наследниках)"""
        pass
        
    @abstractmethod
    async def _handle_price_update(self, event: PriceUpdateEvent):
        """Обработка обновления цены (реализуется в наследниках)"""
        pass
        
    @abstractmethod
    async def _handle_order_filled(self, event: OrderFilledEvent):
        """Обработка исполнения ордера (реализуется в наследниках)"""
        pass
        
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

            # Подписка на события
            await self._subscribe_to_events()
            
            # Сохранение состояния в Redis
            await self._save_strategy_state()
            
            # Выполнение начальной логики стратегии
            await self._execute_strategy_logic()
            self.is_running = True
            log_info(self.user_id,f"Стратегия {self.strategy_type.value} запущена для {self.symbol}",module_name=__name__)
            return True
        except Exception as e:
            log_error(self.user_id, f"Ошибка запуска стратегии: {e}", module_name=__name__)
            return False
            
    async def stop(self, reason: str = "Manual stop") -> bool:
        """
        Остановка стратегии
        
        Args:
            reason: Причина остановки
            
        Returns:
            bool: True если стратегия успешно остановлена
        """
        if not self.is_running:
            return True
        log_info(self.user_id, f"Остановка стратегии {self.strategy_type.value}: {reason}",module_name=__name__)
        try:
            async with self.lock:
                self.is_running = False
                
                # Отмена всех активных ордеров
                await self._cancel_all_orders()
                
                # Закрытие всех позиций (опционально)
                if self.config and self.config.get("close_positions_on_stop", False):
                    await self._close_all_positions()
                    
                # Отписка от событий
                await self._unsubscribe_from_events()
                
                # Сохранение финальной статистики
                await self._save_final_stats()
                
                # Удаление состояния из Redis
                await self._cleanup_redis_state()
            log_info(self.user_id,f"Стратегия {self.strategy_type.value} остановлена", module_name=__name__)
            return True
        except Exception as e:
            log_error(self.user_id, f"Ошибка остановки стратегии: {e}", module_name=__name__)
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

            
    async def _subscribe_to_events(self):
        """Подписка на события"""
        try:
            # Подписка на обновления цен
            self.event_bus.subscribe(EventType.PRICE_UPDATE, self._handle_price_update_wrapper)
            
            # Подписка на исполнение ордеров
            self.event_bus.subscribe(EventType.ORDER_FILLED, self._handle_order_filled_wrapper)
            
            # Подписка на обновления позиций
            self.event_bus.subscribe(EventType.POSITION_UPDATE, self._handle_position_update)
            
            # Подписка на изменения настроек
            self.event_bus.subscribe(EventType.USER_SETTINGS_CHANGED, self._handle_settings_changed)
            
            log_info(self.user_id, "Подписка на события выполнена", module_name=__name__)
            
        except Exception as e:
            log_error(self.user_id, f"Ошибка подписки на события: {e}", module_name=__name__)
            
    async def _unsubscribe_from_events(self):
        """Отписка от событий"""
        try:
            self.event_bus.unsubscribe(EventType.PRICE_UPDATE, self._handle_price_update_wrapper)
            self.event_bus.unsubscribe(EventType.ORDER_FILLED, self._handle_order_filled_wrapper)
            self.event_bus.unsubscribe(EventType.POSITION_UPDATE, self._handle_position_update)
            self.event_bus.unsubscribe(EventType.USER_SETTINGS_CHANGED, self._handle_settings_changed)
            
            log_info(self.user_id, "Отписка от событий выполнена", module_name=__name__)
            
        except Exception as e:
            log_error(self.user_id, f"Ошибка отписки от событий: {e}", module_name=__name__)
            
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
            
    async def _handle_order_filled_wrapper(self, event: OrderFilledEvent):
        """Обертка для обработки исполнения ордера"""
        # Фильтрация событий только для нашего пользователя и символа
        if event.user_id != self.user_id or event.symbol != self.symbol:
            return
            
        if not self.is_running:
            return
            
        try:
            # Обновление статистики
            self.stats["orders_count"] += 1
            
            # Определение прибыльности ордера
            if hasattr(event, 'pnl') and event.pnl:
                pnl = self._convert_to_decimal(event.pnl)
                self.stats["total_pnl"] += pnl
                
                if pnl > 0:
                    self.stats["profit_orders"] += 1
                else:
                    self.stats["loss_orders"] += 1
            
            # Удаление ордера из активных
            if event.order_id in self.active_orders:
                del self.active_orders[event.order_id]
                
            await self._handle_order_filled(event)
            
        except Exception as e:
            log_error(self.user_id, f"Ошибка обработки исполнения ордера: {e}", module_name=__name__)
            
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
                           reduce_only: bool = False) -> Optional[str]:  # <-- ДОБАВЛЕН reduce_only
        """Универсальное размещение ордера через API"""
        try:
            if not self.api:
                log_error(self.user_id, "API клиент не инициализирован в стратегии.", module_name=__name__)
                return None

            # Логика едина для testnet и production, т.к. BybitAPI сам переключает эндпоинты
            order_id = await self.api.place_order(
                symbol=self.symbol,
                side=side,
                order_type=order_type,
                qty=qty,
                price=price,
                stop_loss=stop_loss,
                take_profit=take_profit,
                reduce_only=reduce_only
            )

            if order_id:
                # Сохранение ордера в активных
                self.active_orders[order_id] = {
                    "order_id": order_id,
                    "symbol": self.symbol,
                    "side": side,
                    "order_type": order_type,
                    "qty": qty,
                    "price": price,
                    "stop_loss": stop_loss,
                    "take_profit": take_profit,
                    "created_at": datetime.now()
                }
                log_info(self.user_id, f"Ордер размещен: {side} {qty} {self.symbol} по {price} (ID: {order_id})", module_name=__name__)
                return order_id
            else:
                log_error(self.user_id, "Не удалось разместить ордер (API не вернул ID).", module_name=__name__)
                return None

        except Exception as e:
            log_error(self.user_id, f"Ошибка размещения ордера: {e}", module_name=__name__)
        return None


    async def _cancel_order(self, order_id: str) -> bool:
        """Отмена ордера"""
        try:
            if not self.api:
                return False
                
            result = await self.api.cancel_order(self.symbol, order_id)
            
            if result:
                # Удаление из активных ордеров
                if order_id in self.active_orders:
                    del self.active_orders[order_id]
                    
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

        logger.error(self.user_id, error_msg, module_name=__name__)

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

        Returns:
            bool: True если конфигурация валидна
        """
        required_fields = ['leverage', 'order_amount', 'profit_percent']

        for field in required_fields:
            if field not in self.config:
                log_error(self.user_id, f"Отсутствует обязательное поле конфигурации: {field}", module_name=__name__)
                return False

        # Проверяем диапазоны значений
        if not (1 <= self.leverage <= 100):
            log_error(self.user_id, f"Неверное значение плеча: {self.leverage}", module_name=__name__)
            return False

        if not (5.0 <= self.order_amount <= 10000):  # Минимальный ордер обычно около 5 USDT
            log_error(self.user_id, f"Неверная сумма ордера: {self.order_amount}", module_name=__name__)
            return False

        if not (0.1 <= self.profit_percent <= 10):
            log_error(self.user_id, f"Неверный процент прибыли: {self.profit_percent}", module_name=__name__)
            return False

        return True

    async def _send_trade_open_notification(self, side: str, price: Decimal, quantity: Decimal):
        """Отправляет уведомление об открытии новой сделки."""
        try:
            side_text = "LONG 🟢" if side.lower() == 'buy' else "SHORT 🔴"
            strategy_name = self.strategy_type.value.replace('_', ' ').title()
            text = (
                f"📈 {hbold('ОТКРЫТА НОВАЯ СДЕЛКА')} 📈\n\n"
                f"▫️ {hbold('Стратегия:')} {hcode(strategy_name)}\n"
                f"▫️ {hbold('Инструмент:')} {hcode(self.symbol)}\n"
                f"▫️ {hbold('Направление:')} {side_text}\n"
                f"▫️ {hbold('Цена входа:')} {hcode(f'{price:.4f} USDT')}\n"
                f"▫️ {hbold('Объем:')} {hcode(str(quantity))}"
            )
            await self.bot.send_message(self.user_id, text, parse_mode="HTML")
        except Exception as e:
            log_error(self.user_id, f"Ошибка отправки уведомления об открытии сделки: {e}", "base_strategy")

    async def _send_trade_close_notification(self, pnl: Decimal):
        """Отправляет уведомление о закрытии сделки."""
        try:
            win_rate = await db_manager.update_strategy_stats(
                user_id=self.user_id,
                strategy_type=self.strategy_type.value,
                pnl=pnl
            )
            if pnl >= 0:
                result_text, pnl_text, icon = "ПРИБЫЛЬ ✅", f"+{pnl:.2f} USDT", "💰"
            else:
                result_text, pnl_text, icon = "УБЫТОК 🔻", f"{pnl:.2f} USDT", "📉"
            strategy_name = self.strategy_type.value.replace('_', ' ').title()
            text = (
                f"{icon} {hbold('СДЕЛКА ЗАКРЫТА')} {icon}\n\n"
                f"▫️ {hbold('Стратегия:')} {hcode(strategy_name)}\n"
                f"▫️ {hbold('Инструмент:')} {hcode(self.symbol)}\n"
                f"▫️ {hbold('Результат:')} {result_text}\n"
                f"▫️ {hbold('PnL:')} {hcode(pnl_text)}\n"
                f"▫️ {hbold('Win Rate стратегии:')} {hcode(f'{win_rate:.2f}%')}"
            )
            await self.bot.send_message(self.user_id, text, parse_mode="HTML")
        except Exception as e:
            log_error(self.user_id, f"Ошибка отправки уведомления о закрытии сделки: {e}", "base_strategy")


    def __str__(self) -> str:
        """Строковое представление стратегии."""
        return f"{self.strategy_type.value}({self.symbol}, user={self.user_id})"

    def __repr__(self) -> str:
        """Представление стратегии для отладки."""
        return f"<{self.__class__.__name__}: {self.strategy_type.value}, {self.symbol}, user={self.user_id}>"



