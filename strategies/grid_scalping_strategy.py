# strategies/grid_scalping_strategy.py
"""
Стратегия грид-скальпинга для многопользовательской торговой системы
Реализует быстрые сделки с небольшой прибылью в условиях низкой волатильности
"""
import asyncio
from typing import Dict, Any, Optional, List
from decimal import Decimal, getcontext
from datetime import datetime, timedelta
from api.bybit_api import BybitAPI
from .base_strategy import BaseStrategy
from core.enums import StrategyType, OrderType
from core.logger import log_info, log_error
from core.events import PriceUpdateEvent, OrderFilledEvent, EventBus

# Настройка точности для Decimal
getcontext().prec = 28


class GridScalpingStrategy(BaseStrategy):
    """
    Стратегия грид-скальпинга для быстрых сделок
    
    Особенности:
    - Множественные мелкие ордера с быстрой прибылью
    - Адаптация к спреду и ликвидности
    - Быстрое исполнение и закрытие позиций
    - Работа в условиях низкой волатильности
    """

    def __init__(self, user_id: int, symbol: str, signal_data: Dict[str, Any], api: BybitAPI, event_bus: EventBus,
                 config: Optional[Dict] = None):
        super().__init__(user_id, symbol, signal_data, api, event_bus, config)
        
        # Параметры скальпинга (загружаются из конфигурации)
        self.scalp_levels: int = 3
        self.scalp_spacing_percent: Decimal = Decimal('0.3')  # 0.3% между уровнями
        self.quick_profit_percent: Decimal = Decimal('0.5')   # 0.5% быстрая прибыль
        
        # Адаптивные параметры
        self.current_spread: Optional[Decimal] = None
        self.min_spread_multiplier: Decimal = Decimal('2.0')
        self.adaptive_to_spread: bool = True
        
        # Состояние скальпинга
        self.scalp_center_price: Optional[Decimal] = None
        self.active_scalp_orders: Dict[str, Dict] = {}  # order_id -> order_data
        self.profit_orders: Dict[str, Dict] = {}        # order_id -> order_data
        
        # Статистика скальпинга
        self.scalp_stats = {
            "scalp_cycles": 0,
            "quick_profits": 0,
            "total_scalp_volume": Decimal('0'),
            "average_profit_per_cycle": Decimal('0')
        }
        
        # Таймауты
        self.order_timeout = timedelta(seconds=30)
        self.last_order_time = datetime.min

    def _get_strategy_type(self) -> StrategyType:
        return StrategyType.GRID_SCALPING

    async def _execute_strategy_logic(self):
        """Основная логика стратегии - инициализация скальпинга"""
        try:
            log_info(self.user_id, f"Инициализация грид-скальпинга для {self.symbol}", module_name=__name__)

            # Загрузка параметров из конфигурации
            await self._load_scalp_parameters()

            # Установка плеча
            await self._set_leverage()

            # Получение текущей цены и спреда из данных сигнала
            current_price = self._convert_to_decimal(self.signal_data.get('current_price', '0'))

            if not current_price > 0:
                log_info(self.user_id, "Цена не найдена в сигнале, запрашиваю через API...", module_name=__name__)
                ticker_data = await self.api.get_ticker(self.symbol)
                if ticker_data and 'lastPrice' in ticker_data:
                    current_price = ticker_data['lastPrice']
                else:
                    log_error(self.user_id, f"Не удалось получить текущую цену для {self.symbol} через API.",
                              module_name=__name__)
                    await self.stop("Не удалось получить цену")
                    return

            # Анализ спреда и ликвидности
            await self._analyze_market_conditions(current_price)
            
            # Расчет параметров скальпинга
            await self._calculate_scalp_parameters(current_price)
            
            # Создание начальных ордеров скальпинга
            await self._create_initial_scalp_orders()
            
            log_info(
                self.user_id,
                f"Скальпинг инициализирован: центр={self.scalp_center_price}, "
                f"уровни={self.scalp_levels}, spacing={self.scalp_spacing_percent}%",
                module_name=__name__
            )
            
        except Exception as e:
            log_error(self.user_id, f"Ошибка инициализации скальпинга: {e}", module_name=__name__)
            await self.stop("Ошибка инициализации")
            
    async def _handle_price_update(self, event: PriceUpdateEvent):
        """Обработка обновления цены"""
        try:
            current_price = event.price
            
            # Проверка таймаутов ордеров
            await self._check_order_timeouts()
            
            # Проверка необходимости корректировки ордеров
            if await self._should_adjust_orders(current_price):
                await self._adjust_scalp_orders(current_price)
                
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
                f"Скальп-ордер исполнен: {side} {qty} по {price} (ID: {order_id})",
                module_name=__name__
            )
            
            # Определение типа ордера
            if order_id in self.active_scalp_orders:
                # Исполнен ордер скальпинга - размещаем ордер прибыли
                await self._handle_scalp_order_filled(order_id, side, price, qty)
                del self.active_scalp_orders[order_id]
                
            elif order_id in self.profit_orders:
                # Исполнен ордер прибыли - завершаем цикл скальпинга
                await self._handle_profit_order_filled(order_id, side, price, qty)
                del self.profit_orders[order_id]
                
            # Обновление статистики
            self.scalp_stats["total_scalp_volume"] += price * qty
            
        except Exception as e:
            log_error(self.user_id, f"Ошибка обработки исполнения ордера: {e}", module_name=__name__)
            
    async def _load_scalp_parameters(self):
        """Загрузка параметров скальпинга из конфигурации"""
        try:
            await self._ensure_config_fresh()
            
            if not self.config:
                return
                
            # Основные параметры
            self.scalp_levels = self.config.get("scalp_levels", 3)
            self.scalp_spacing_percent = self._convert_to_decimal(
                self.config.get("scalp_spacing_percent", 0.3)
            )
            self.quick_profit_percent = self._convert_to_decimal(
                self.config.get("quick_profit_percent", 0.5)
            )
            
            # Адаптивные параметры
            self.adaptive_to_spread = self.config.get("adaptive_to_spread", True)
            self.min_spread_multiplier = self._convert_to_decimal(
                self.config.get("min_spread_multiplier", 2.0)
            )
            
            # Таймауты
            order_timeout_seconds = self.config.get("order_timeout_seconds", 30)
            self.order_timeout = timedelta(seconds=order_timeout_seconds)
            
            log_info(
                self.user_id,
                f"Параметры скальпинга: уровни={self.scalp_levels}, "
                f"spacing={self.scalp_spacing_percent}%, profit={self.quick_profit_percent}%",
                module_name=__name__
            )
            
        except Exception as e:
            log_error(self.user_id, f"Ошибка загрузки параметров скальпинга: {e}", module_name=__name__)
            
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
            

        
    async def _analyze_market_conditions(self, current_price: Decimal):
        """Анализ рыночных условий для скальпинга"""
        try:
            if not self.api:
                return
                
            # Получение данных стакана для анализа спреда
            order_book = await self.api.get_order_book(self.symbol, limit=5)
            
            if order_book and "bids" in order_book and "asks" in order_book:
                best_bid = Decimal(str(order_book["bids"][0][0]))
                best_ask = Decimal(str(order_book["asks"][0][0]))
                
                # Расчет спреда
                self.current_spread = best_ask - best_bid
                spread_percent = (self.current_spread / current_price) * 100
                
                log_info(
                    self.user_id,
                    f"Анализ рынка: спред={self.current_spread} ({spread_percent:.4f}%)",
                    module_name=__name__
                )
                
                # Адаптация параметров к спреду
                if self.adaptive_to_spread and self.current_spread > 0:
                    min_spacing = (self.current_spread / current_price) * 100 * self.min_spread_multiplier
                    self.scalp_spacing_percent = max(self.scalp_spacing_percent, min_spacing)
                    
        except Exception as e:
            log_error(self.user_id, f"Ошибка анализа рыночных условий: {e}", module_name=__name__)
            
    async def _calculate_scalp_parameters(self, center_price: Decimal):
        """Расчет параметров скальпинга"""
        try:
            self.scalp_center_price = center_price
            
            log_info(
                self.user_id,
                f"Параметры скальпинга рассчитаны: центр={center_price}, "
                f"spacing={self.scalp_spacing_percent}%",
                module_name=__name__
            )
            
        except Exception as e:
            log_error(self.user_id, f"Ошибка расчета параметров скальпинга: {e}", module_name=__name__)
            
    async def _create_initial_scalp_orders(self):
        """Создание начальных ордеров скальпинга"""
        try:
            if not self.scalp_center_price:
                return
                
            # Размер ордера
            order_size = await self.calculate_order_size()
            if order_size <= 0:
                log_error(self.user_id, "Недостаточно средств для скальпинга", module_name=__name__)
                return
                
            # Создание ордеров на покупку и продажу
            for i in range(1, self.scalp_levels + 1):
                # Ордер на покупку (ниже центра)
                buy_price = self.scalp_center_price * (1 - (self.scalp_spacing_percent / 100) * i)
                await self._place_scalp_order("Buy", buy_price, order_size)
                
                # Ордер на продажу (выше центра)
                sell_price = self.scalp_center_price * (1 + (self.scalp_spacing_percent / 100) * i)
                await self._place_scalp_order("Sell", sell_price, order_size)
                
            log_info(
                self.user_id,
                f"Скальп-ордера созданы: {len(self.active_scalp_orders)} ордеров",
                module_name=__name__
            )
            
        except Exception as e:
            log_error(self.user_id, f"Ошибка создания скальп-ордеров: {e}", module_name=__name__)
            
    async def calculate_order_size(self) -> Decimal:
        """Расчет размера ордера для скальпинга"""
        try:
            if not self.config:
                return Decimal('0')
                
            # Общий размер позиции для скальпинга
            total_position_size = self._convert_to_decimal(self.config.get("order_amount", 10.0))
            position_size_percent = self.config.get("position_size_percent", 30)
            
            # Размер для скальпинга
            scalp_size = total_position_size * (self._convert_to_decimal(position_size_percent) / Decimal('100'))
            
            # Размер одного ордера
            order_size = scalp_size / (self.scalp_levels * 2)  # *2 для buy и sell
            
            # Минимальный размер ордера
            min_order_size = Decimal('5.0')
            
            return max(min_order_size, order_size)
            
        except Exception as e:
            log_error(self.user_id, f"Ошибка расчета размера ордера: {e}", module_name=__name__)
            return Decimal('0')
            
    async def _place_scalp_order(self, side: str, price: Decimal, size: Decimal) -> Optional[str]:
        """Размещение ордера скальпинга"""
        try:
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
                    # Сохранение ордера скальпинга
                    self.active_scalp_orders[order_id] = {
                        "order_id": order_id,
                        "side": side,
                        "price": price,
                        "qty": qty,
                        "size_usdt": size,
                        "created_at": datetime.now()
                    }
                    
                    log_info(
                        self.user_id,
                        f"Скальп-ордер размещен: {side} {qty} по {price}",
                        module_name=__name__
                    )
                    
                return order_id
                
        except Exception as e:
            log_error(self.user_id, f"Ошибка размещения скальп-ордера: {e}", module_name=__name__)
            
        return None
        
    async def _handle_scalp_order_filled(self, order_id: str, side: str, price: Decimal, qty: Decimal):
        """Обработка исполнения ордера скальпинга"""
        try:
            # Определение цены прибыли
            if side == "Buy":
                profit_side = "Sell"
                profit_price = price * (1 + self.quick_profit_percent / 100)
            else:
                profit_side = "Buy"
                profit_price = price * (1 - self.quick_profit_percent / 100)
                
            # Размещение ордера прибыли
            profit_order_id = await self._place_order(
                side=profit_side,
                order_type="Limit",
                qty=qty,
                price=profit_price
            )
            
            if profit_order_id:
                # Сохранение ордера прибыли
                self.profit_orders[profit_order_id] = {
                    "order_id": profit_order_id,
                    "side": profit_side,
                    "price": profit_price,
                    "qty": qty,
                    "original_order_id": order_id,
                    "original_price": price,
                    "created_at": datetime.now()
                }
                
                log_info(
                    self.user_id,
                    f"Ордер прибыли размещен: {profit_side} {qty} по {profit_price}",
                    module_name=__name__
                )
                
            # Размещение нового скальп-ордера на том же уровне
            order_data = self.active_scalp_orders.get(order_id, {})
            if order_data:
                size_usdt = order_data.get("size_usdt", Decimal('5'))
                await self._place_scalp_order(side, price, size_usdt)
                
        except Exception as e:
            log_error(self.user_id, f"Ошибка обработки исполнения скальп-ордера: {e}", module_name=__name__)

    async def _handle_profit_order_filled(self, order_id: str, side: str, price: Decimal, qty: Decimal):
        """Обработка исполнения ордера прибыли"""
        try:
            # Обновление статистики
            self.scalp_stats["quick_profits"] += 1
            self.scalp_stats["scalp_cycles"] += 1
            
            # Расчет прибыли цикла
            profit_order = self.profit_orders.get(order_id, {})
            if profit_order:
                original_price = profit_order.get("original_price", price)
                cycle_profit = abs(price - original_price) * qty
                
                # Обновление средней прибыли
                total_cycles = self.scalp_stats["scalp_cycles"]
                current_avg = self.scalp_stats["average_profit_per_cycle"]
                self.scalp_stats["average_profit_per_cycle"] = (
                    (current_avg * (total_cycles - 1) + cycle_profit) / total_cycles
                )
            log_info(self.user_id,
                f"Цикл скальпинга завершен: ордер прибыли {side} исполнен. (цикл #{self.scalp_stats['scalp_cycles']})",
                module_name=__name__)
        except Exception as e:
            log_error(self.user_id, f"Ошибка обработки исполнения ордера прибыли: {e}", module_name=__name__)
            
    async def _check_order_timeouts(self):
        """Проверка таймаутов ордеров"""
        try:
            current_time = datetime.now()
            
            # Проверка таймаутов скальп-ордеров
            expired_orders = []
            for order_id, order_data in self.active_scalp_orders.items():
                if current_time - order_data["created_at"] > self.order_timeout:
                    expired_orders.append(order_id)
                    
            # Отмена просроченных ордеров
            for order_id in expired_orders:
                if await self._cancel_order(order_id):
                    del self.active_scalp_orders[order_id]
                    log_info(self.user_id, f"Просроченный скальп-ордер отменен: {order_id}", module_name=__name__)
                    
        except Exception as e:
            log_error(self.user_id, f"Ошибка проверки таймаутов: {e}", module_name=__name__)
            
    async def _should_adjust_orders(self, current_price: Decimal) -> bool:
        """Проверка необходимости корректировки ордеров"""
        try:
            if not self.scalp_center_price:
                return False
                
            # Корректировка если цена сильно отклонилась от центра
            price_deviation = abs(current_price - self.scalp_center_price) / self.scalp_center_price * 100
            
            # Порог для корректировки (например, 2%)
            adjustment_threshold = 2.0
            
            return price_deviation > adjustment_threshold
            
        except Exception as e:
            log_error(self.user_id, f"Ошибка проверки необходимости корректировки: {e}", module_name=__name__)
            return False
            
    async def _adjust_scalp_orders(self, new_center_price: Decimal):
        """Корректировка ордеров скальпинга"""
        try:
            log_info(self.user_id, f"Корректировка скальп-ордеров: новый центр {new_center_price}", module_name=__name__)
            
            # Отмена текущих ордеров
            await self._cancel_all_scalp_orders()
            
            # Пересчет параметров
            await self._calculate_scalp_parameters(new_center_price)
            
            # Создание новых ордеров
            await self._create_initial_scalp_orders()
            
        except Exception as e:
            log_error(self.user_id, f"Ошибка корректировки ордеров: {e}", module_name=__name__)
            
    async def _cancel_all_scalp_orders(self):
        """Отмена всех скальп-ордеров"""
        try:
            order_ids = list(self.active_scalp_orders.keys())
            
            for order_id in order_ids:
                if await self._cancel_order(order_id):
                    del self.active_scalp_orders[order_id]
                    
            log_info(self.user_id, f"Отменено {len(order_ids)} скальп-ордеров", module_name=__name__)
            
        except Exception as e:
            log_error(self.user_id, f"Ошибка отмены скальп-ордеров: {e}", module_name=__name__)
            
    async def get_strategy_stats(self) -> Dict[str, Any]:
        """Получение статистики стратегии"""
        base_stats = await self.get_status()
        
        scalp_specific_stats = {
            "scalp_stats": {
                "scalp_cycles": self.scalp_stats["scalp_cycles"],
                "quick_profits": self.scalp_stats["quick_profits"],
                "total_scalp_volume": float(self.scalp_stats["total_scalp_volume"]),
                "average_profit_per_cycle": float(self.scalp_stats["average_profit_per_cycle"]),
                "success_rate": (
                    self.scalp_stats["quick_profits"] / max(1, self.scalp_stats["scalp_cycles"]) * 100
                    if self.scalp_stats["scalp_cycles"] > 0 else 0
                )
            },
            "scalp_parameters": {
                "scalp_levels": self.scalp_levels,
                "scalp_spacing_percent": float(self.scalp_spacing_percent),
                "quick_profit_percent": float(self.quick_profit_percent),
                "scalp_center_price": float(self.scalp_center_price) if self.scalp_center_price else None,
                "current_spread": float(self.current_spread) if self.current_spread else None
            },
            "active_orders": {
                "scalp_orders_count": len(self.active_scalp_orders),
                "profit_orders_count": len(self.profit_orders),
                "scalp_orders": [
                    {
                        "side": order["side"],
                        "price": float(order["price"]),
                        "qty": float(order["qty"])
                    }
                    for order in self.active_scalp_orders.values()
                ],
                "profit_orders": [
                    {
                        "side": order["side"],
                        "price": float(order["price"]),
                        "qty": float(order["qty"])
                    }
                    for order in self.profit_orders.values()
                ]
            }
        }
        
        return {**base_stats, **scalp_specific_stats}

