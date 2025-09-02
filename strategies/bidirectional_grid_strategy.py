# strategies/bidirectional_grid_strategy.py
"""
Стратегия двунаправленной сетки для многопользовательской торговой системы
Реализует событийно-ориентированную архитектуру с динамическими настройками
"""
import asyncio
from typing import Dict, Any, Optional, List
from decimal import Decimal, getcontext
from datetime import datetime
from api.bybit_api import BybitAPI
from .base_strategy import BaseStrategy
from core.enums import StrategyType, OrderType
from core.logger import log_info, log_error
from core.events import PriceUpdateEvent, OrderFilledEvent, EventBus

# Настройка точности для Decimal
getcontext().prec = 28


class BidirectionalGridStrategy(BaseStrategy):
    """
    Стратегия двунаправленной сетки для флэтового рынка
    
    Особенности:
    - Размещение ордеров на покупку и продажу в сетке вокруг текущей цены
    - Динамическое управление сеткой на основе волатильности (ATR)
    - Автоматическое восстановление сетки после исполнения ордеров
    - Адаптивные уровни прибыли на основе рыночных условий
    """

    def __init__(self, user_id: int, symbol: str, signal_data: Dict[str, Any], api: BybitAPI, event_bus: EventBus,
                 config: Optional[Dict] = None):
        super().__init__(user_id, symbol, signal_data, api, event_bus, config)
        
        # Параметры сетки (загружаются из конфигурации)
        self.grid_levels: int = 5
        self.grid_spacing_percent: Decimal = Decimal('0.5')  # 0.5% между уровнями
        self.profit_percent: Decimal = Decimal('1.0')  # 1% прибыль
        
        # Состояние сетки
        self.grid_center_price: Optional[Decimal] = None
        self.grid_upper_bound: Optional[Decimal] = None
        self.grid_lower_bound: Optional[Decimal] = None
        self.grid_step_price: Optional[Decimal] = None
        
        # Активные ордера сетки
        self.buy_orders: Dict[str, Dict] = {}   # order_id -> order_data
        self.sell_orders: Dict[str, Dict] = {}  # order_id -> order_data
        
        # Статистика сетки
        self.grid_stats = {
            "grid_rebuilds": 0,
            "buy_fills": 0,
            "sell_fills": 0,
            "grid_profit": Decimal('0')
        }



    def _get_strategy_type(self) -> StrategyType:
        return StrategyType.BIDIRECTIONAL_GRID
        
    async def _execute_strategy_logic(self):
        """Основная логика стратегии - инициализация сетки"""
        try:
            log_info(self.user_id, f"Инициализация двунаправленной сетки для {self.symbol}", module_name=__name__)
            
            # Загрузка параметров из конфигурации
            await self._load_grid_parameters()
            
            # Установка плеча
            await self._set_leverage()
            
            # Получение текущей цены
            current_price = await self._get_current_price()
            if not current_price:
                log_error(self.user_id, "Не удалось получить текущую цену", module_name=__name__)
                return
                
            # Расчет параметров сетки
            await self._calculate_grid_parameters(current_price)
            
            # Создание начальной сетки
            await self._create_initial_grid()
            
            log_info(
                self.user_id,
                f"Сетка инициализирована: центр={self.grid_center_price}, "
                f"диапазон={self.grid_lower_bound}-{self.grid_upper_bound}, "
                f"шаг={self.grid_step_price}",
                module_name=__name__
            )
            
        except Exception as e:
            log_error(self.user_id, f"Ошибка инициализации сетки: {e}", module_name=__name__)
            await self.stop("Ошибка инициализации")
            
    async def _handle_price_update(self, event: PriceUpdateEvent):
        """Обработка обновления цены"""
        try:
            current_price = event.price
            
            # Проверка, нужно ли перестроить сетку
            if await self._should_rebuild_grid(current_price):
                log_info(
                    self.user_id,
                    f"Перестройка сетки: цена {current_price} вышла за границы",
                    module_name=__name__
                )
                await self._rebuild_grid(current_price)
                
        except Exception as e:
            log_error(self.user_id, f"Ошибка обработки обновления цены: {e}", module_name=__name__)
            
    async def _handle_order_filled(self, event: OrderFilledEvent):
        """Обработка исполнения ордера"""
        try:
            order_id = event.order_id
            side = event.side
            price = event.price
            qty = event.qty
            
            log_info(
                self.user_id,
                f"Ордер исполнен: {side} {qty} по {price} (ID: {order_id})",
                module_name=__name__
            )
            
            # Обновление статистики
            if side == "Buy":
                self.grid_stats["buy_fills"] += 1
                if order_id in self.buy_orders:
                    del self.buy_orders[order_id]
            else:
                self.grid_stats["sell_fills"] += 1
                if order_id in self.sell_orders:
                    del self.sell_orders[order_id]
                    
            # Размещение противоположного ордера для получения прибыли
            await self._place_profit_order(side, price, qty)
            
            # Восстановление сетки если нужно
            await self._restore_grid_level(side, price)
            
        except Exception as e:
            log_error(self.user_id, f"Ошибка обработки исполнения ордера: {e}", module_name=__name__)
            
    async def _load_grid_parameters(self):
        """Загрузка параметров сетки из конфигурации"""
        try:
            await self._ensure_config_fresh()
            
            if not self.config:
                return
                
            # Параметры сетки
            self.grid_levels = self.config.get("grid_levels", 5)
            self.grid_spacing_percent = self._convert_to_decimal(
                self.config.get("grid_spacing_percent", 0.5)
            )
            self.profit_percent = self._convert_to_decimal(
                self.config.get("profit_percent", 1.0)
            )
            
            # Адаптация к волатильности из signal_data
            if "atr" in self.signal_data:
                atr = self.signal_data["atr"]
                current_price = self.signal_data.get("current_price", Decimal('0'))
                
                if current_price > 0:
                    # Адаптивный spacing на основе ATR
                    atr_percent = (atr / current_price) * 100
                    self.grid_spacing_percent = max(
                        Decimal('0.2'),  # Минимум 0.2%
                        min(Decimal('2.0'), atr_percent * Decimal('0.5'))  # Максимум 2%
                    )
                    
            log_info(
                self.user_id,
                f"Параметры сетки: уровни={self.grid_levels}, "
                f"spacing={self.grid_spacing_percent}%, profit={self.profit_percent}%",
                module_name=__name__
            )
            
        except Exception as e:
            log_error(self.user_id, f"Ошибка загрузки параметров сетки: {e}", module_name=__name__)
            
    async def _set_leverage(self):
        """Установка плеча"""
        try:
            if not self.config:
                return
                
            leverage = self.config.get("leverage", 1)
            
            if self.api:
                result = await self.api.set_leverage(self.symbol, leverage)
                if result:
                    log_info(self.user_id, f"Плечо установлено: {leverage}x", module_name=__name__)
                else:
                    log_error(self.user_id, "Ошибка установки плеча", module_name=__name__)
                    
        except Exception as e:
            log_error(self.user_id, f"Ошибка установки плеча: {e}", module_name=__name__)
            
    async def _get_current_price(self) -> Optional[Decimal]:
        """Получение текущей цены"""
        try:
            if not self.api:
                return None
                
            ticker = await self.api.get_ticker(self.symbol)
            if ticker and "lastPrice" in ticker:
                return Decimal(str(ticker["lastPrice"]))
                
        except Exception as e:
            log_error(self.user_id, f"Ошибка получения цены: {e}", module_name=__name__)
            
        return None
        
    async def _calculate_grid_parameters(self, center_price: Decimal):
        """Расчет параметров сетки"""
        try:
            self.grid_center_price = center_price
            
            # Шаг сетки в процентах от цены
            self.grid_step_price = center_price * (self.grid_spacing_percent / 100)
            
            # Границы сетки
            total_range = self.grid_step_price * self.grid_levels
            self.grid_upper_bound = center_price + total_range
            self.grid_lower_bound = center_price - total_range
            
            log_info(
                self.user_id,
                f"Параметры сетки рассчитаны: центр={center_price}, "
                f"шаг={self.grid_step_price}, границы={self.grid_lower_bound}-{self.grid_upper_bound}",
                module_name=__name__
            )
            
        except Exception as e:
            log_error(self.user_id, f"Ошибка расчета параметров сетки: {e}", module_name=__name__)
            
    async def _create_initial_grid(self):
        """Создание начальной сетки ордеров"""
        try:
            if not self.grid_center_price or not self.grid_step_price:
                return
                
            # Размер ордера
            order_size = await self.calculate_order_size()
            if order_size <= 0:
                log_error(self.user_id, "Недостаточно средств для создания сетки", module_name=__name__)
                return
                
            # Создание ордеров на покупку (ниже центра)
            for i in range(1, self.grid_levels + 1):
                buy_price = self.grid_center_price - (self.grid_step_price * i)
                if buy_price > 0:
                    await self._place_grid_order("Buy", buy_price, order_size)
                    
            # Создание ордеров на продажу (выше центра)
            for i in range(1, self.grid_levels + 1):
                sell_price = self.grid_center_price + (self.grid_step_price * i)
                await self._place_grid_order("Sell", sell_price, order_size)
                
            log_info(
                self.user_id,
                f"Начальная сетка создана: {len(self.buy_orders)} buy, {len(self.sell_orders)} sell",
                module_name=__name__
            )
            
        except Exception as e:
            log_error(self.user_id, f"Ошибка создания начальной сетки: {e}", module_name=__name__)
            
    async def calculate_order_size(self) -> Decimal:
        """Расчет размера ордера для сетки"""
        try:
            if not self.config:
                return Decimal('0')
                
            # Общий размер позиции
            total_position_size = self._convert_to_decimal(self.config.get("order_amount", 10.0))
            
            # Размер одного ордера = общий размер / количество уровней
            order_size = total_position_size / (self.grid_levels * 2)  # *2 для buy и sell
            
            # Минимальный размер ордера
            min_order_size = Decimal('5.0')
            
            return max(min_order_size, order_size)
            
        except Exception as e:
            log_error(self.user_id, f"Ошибка расчета размера ордера: {e}", module_name=__name__)
            return Decimal('0')
            
    async def _place_grid_order(self, side: str, price: Decimal, size: Decimal) -> Optional[str]:
        """Размещение ордера сетки"""
        try:
            # Конвертация размера в количество монет
            if self.api:
                qty = await self.api.calculate_quantity_from_usdt(self.symbol, size, price)
                if qty <= 0:
                    return None
                    
                order_id = await self._place_order(
                    side=side,
                    order_type="Limit",
                    qty=qty,
                    price=price
                )
                
                if order_id:
                    # Сохранение в соответствующий словарь
                    order_data = {
                        "order_id": order_id,
                        "side": side,
                        "price": price,
                        "qty": qty,
                        "size_usdt": size,
                        "created_at": datetime.now()
                    }
                    
                    if side == "Buy":
                        self.buy_orders[order_id] = order_data
                    else:
                        self.sell_orders[order_id] = order_data
                        
                    log_info(
                        self.user_id,
                        f"Ордер сетки размещен: {side} {qty} по {price}",
                        module_name=__name__
                    )
                    
                return order_id
                
        except Exception as e:
            log_error(self.user_id, f"Ошибка размещения ордера сетки: {e}", module_name=__name__)
            
        return None
        
    async def _place_profit_order(self, original_side: str, original_price: Decimal, qty: Decimal):
        """Размещение ордера для получения прибыли"""
        try:
            # Определение противоположной стороны и цены с прибылью
            if original_side == "Buy":
                profit_side = "Sell"
                profit_price = original_price * (1 + self.profit_percent / 100)
            else:
                profit_side = "Buy"
                profit_price = original_price * (1 - self.profit_percent / 100)
                
            order_id = await self._place_order(
                side=profit_side,
                order_type="Limit",
                qty=qty,
                price=profit_price
            )
            
            if order_id:
                log_info(
                    self.user_id,
                    f"Ордер прибыли размещен: {profit_side} {qty} по {profit_price}",
                    module_name=__name__
                )
                
                # Сохранение как обычный ордер (не сетки)
                self.active_orders[order_id] = {
                    "order_id": order_id,
                    "side": profit_side,
                    "price": profit_price,
                    "qty": qty,
                    "type": "profit",
                    "created_at": datetime.now()
                }
                
        except Exception as e:
            log_error(self.user_id, f"Ошибка размещения ордера прибыли: {e}", module_name=__name__)
            
    async def _restore_grid_level(self, filled_side: str, filled_price: Decimal):
        """Восстановление уровня сетки после исполнения"""
        try:
            # Размер ордера
            order_size = await self.calculate_order_size()
            if order_size <= 0:
                return
                
            # Размещение нового ордера на том же уровне
            await self._place_grid_order(filled_side, filled_price, order_size)
            
            log_info(
                self.user_id,
                f"Уровень сетки восстановлен: {filled_side} по {filled_price}",
                module_name=__name__
            )
            
        except Exception as e:
            log_error(self.user_id, f"Ошибка восстановления уровня сетки: {e}", module_name=__name__)
            
    async def _should_rebuild_grid(self, current_price: Decimal) -> bool:
        """Проверка необходимости перестройки сетки"""
        try:
            if not self.grid_upper_bound or not self.grid_lower_bound:
                return False
                
            # Перестройка если цена вышла за границы сетки
            if current_price >= self.grid_upper_bound or current_price <= self.grid_lower_bound:
                return True
                
            # Перестройка если слишком мало активных ордеров
            total_orders = len(self.buy_orders) + len(self.sell_orders)
            min_orders = self.grid_levels  # Минимум половина от общего количества
            
            if total_orders < min_orders:
                return True
                
            return False
            
        except Exception as e:
            log_error(self.user_id, f"Ошибка проверки необходимости перестройки: {e}", module_name=__name__)
            return False
            
    async def _rebuild_grid(self, new_center_price: Decimal):
        """Перестройка сетки с новым центром"""
        try:
            log_info(self.user_id, "Начало перестройки сетки...", module_name=__name__)
            
            # Отмена всех ордеров сетки
            await self._cancel_grid_orders()
            
            # Пересчет параметров сетки
            await self._calculate_grid_parameters(new_center_price)
            
            # Создание новой сетки
            await self._create_initial_grid()
            
            # Обновление статистики
            self.grid_stats["grid_rebuilds"] += 1
            
            log_info(
                self.user_id,
                f"Сетка перестроена с центром {new_center_price} (перестройка #{self.grid_stats['grid_rebuilds']})",
                module_name=__name__
            )
            
        except Exception as e:
            log_error(self.user_id, f"Ошибка перестройки сетки: {e}", module_name=__name__)
            
    async def _cancel_grid_orders(self):
        """Отмена всех ордеров сетки"""
        try:
            # Отмена ордеров на покупку
            for order_id in list(self.buy_orders.keys()):
                if await self._cancel_order(order_id):
                    del self.buy_orders[order_id]
                    
            # Отмена ордеров на продажу
            for order_id in list(self.sell_orders.keys()):
                if await self._cancel_order(order_id):
                    del self.sell_orders[order_id]
                    
            log_info(self.user_id, "Все ордера сетки отменены", module_name=__name__)
            
        except Exception as e:
            log_error(self.user_id, f"Ошибка отмены ордеров сетки: {e}", module_name=__name__)
            
    async def get_strategy_stats(self) -> Dict[str, Any]:
        """Получение статистики стратегии"""
        base_stats = await self.get_status()
        
        grid_specific_stats = {
            "grid_stats": {
                "grid_rebuilds": self.grid_stats["grid_rebuilds"],
                "buy_fills": self.grid_stats["buy_fills"],
                "sell_fills": self.grid_stats["sell_fills"],
                "grid_profit": float(self.grid_stats["grid_profit"]),
                "total_fills": self.grid_stats["buy_fills"] + self.grid_stats["sell_fills"]
            },
            "grid_parameters": {
                "grid_levels": self.grid_levels,
                "grid_spacing_percent": float(self.grid_spacing_percent),
                "profit_percent": float(self.profit_percent),
                "grid_center_price": float(self.grid_center_price) if self.grid_center_price else None,
                "grid_upper_bound": float(self.grid_upper_bound) if self.grid_upper_bound else None,
                "grid_lower_bound": float(self.grid_lower_bound) if self.grid_lower_bound else None
            },
            "active_grid_orders": {
                "buy_orders_count": len(self.buy_orders),
                "sell_orders_count": len(self.sell_orders),
                "buy_orders": [
                    {
                        "price": float(order["price"]),
                        "qty": float(order["qty"]),
                        "size_usdt": float(order["size_usdt"])
                    }
                    for order in self.buy_orders.values()
                ],
                "sell_orders": [
                    {
                        "price": float(order["price"]),
                        "qty": float(order["qty"]),
                        "size_usdt": float(order["size_usdt"])
                    }
                    for order in self.sell_orders.values()
                ]
            }
        }
        
        return {**base_stats, **grid_specific_stats}

