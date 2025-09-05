# strategies/impulse_trailing_strategy.py
"""
Стратегия импульсного трейлинга для многопользовательской торговой системы
Реализует следование за трендом с динамическим трейлинг-стопом
"""
import asyncio
from typing import Dict, Any, Optional, List
from decimal import Decimal, getcontext
from datetime import datetime, timedelta
from api.bybit_api import BybitAPI
from .base_strategy import BaseStrategy
from core.enums import StrategyType, OrderType
from core.logger import log_info, log_error
from core.events import PriceUpdateEvent, OrderFilledEvent, PositionUpdateEvent, EventBus

# Настройка точности для Decimal
getcontext().prec = 28


class ImpulseTrailingStrategy(BaseStrategy):
    """
    Стратегия импульсного трейлинга для трендовых рынков
    
    Особенности:
    - Вход по импульсным движениям с подтверждением объема
    - Динамический трейлинг-стоп для максимизации прибыли
    - Частичное закрытие позиций при достижении целей
    - Адаптивные параметры на основе волатильности
    """

    def __init__(self, user_id: int, symbol: str, signal_data: Dict[str, Any], api: BybitAPI, event_bus: EventBus,
                 config: Optional[Dict] = None):
        super().__init__(user_id, symbol, signal_data, api, event_bus, config)
        
        # Параметры трейлинга (загружаются из конфигурации)
        self.initial_stop_percent: Decimal = Decimal('2.0')      # Начальный стоп-лосс
        self.trailing_step_percent: Decimal = Decimal('0.5')     # Шаг трейлинга
        self.min_profit_for_trailing: Decimal = Decimal('1.0')   # Мин. прибыль для трейлинга
        
        # Импульсные параметры
        self.impulse_threshold_percent: Decimal = Decimal('3.0') # Порог импульса
        self.volume_confirmation: bool = True                    # Подтверждение объемом
        self.min_volume_ratio: Decimal = Decimal('1.5')         # Мин. отношение объема
        
        # Состояние позиции
        self.position_side: Optional[str] = None
        self.entry_price: Optional[Decimal] = None
        self.current_stop_price: Optional[Decimal] = None
        self.peak_price: Optional[Decimal] = None
        self.position_size: Optional[Decimal] = None
        self.position_opened_at: Optional[datetime] = None
        
        # Частичное закрытие
        self.partial_close_enabled: bool = True
        self.partial_close_percent: int = 50  # 50% позиции
        self.partial_close_executed: bool = False
        
        # Статистика трейлинга
        self.trailing_stats = {
            "impulses_detected": 0,
            "positions_opened": 0,
            "trailing_activations": 0,
            "partial_closes": 0,
            "max_favorable_excursion": Decimal('0'),
            "max_adverse_excursion": Decimal('0')
        }
        # Таймауты
        self.max_holding_time = timedelta(hours=12)

    async def validate_config(self) -> bool:
        """Валидирует конфигурацию для ImpulseTrailingStrategy."""
        # Используем собственный набор обязательных полей
        required_fields = ['leverage', 'order_amount', 'initial_stop_percent', 'trailing_step_percent']

        for field in required_fields:
            if field not in self.config:
                log_error(self.user_id, f"Отсутствует обязательное поле конфигурации: {field}", module_name=__name__)
                return False

        # Здесь можно добавить более детальные проверки диапазонов для этой стратегии
        if not (self._convert_to_decimal(self.config.get('initial_stop_percent', 0)) > 0):
            log_error(self.user_id, f"Неверное значение initial_stop_percent", module_name=__name__)
            return False

        return True

    def _get_strategy_type(self) -> StrategyType:
        return StrategyType.IMPULSE_TRAILING

    async def _execute_strategy_logic(self):
        """Основная логика стратегии - анализ импульса и вход в позицию"""
        try:
            log_info(self.user_id, f"Инициализация импульсного трейлинга для {self.symbol}", module_name=__name__)
            
            # Загрузка параметров из конфигурации
            await self._load_trailing_parameters()
            
            # Установка плеча
            await self._set_leverage()
            
            # Анализ импульса из signal_data
            impulse_detected = await self._analyze_impulse_signal()
            
            if impulse_detected:
                # Определение направления входа
                direction = await self._determine_entry_direction()
                
                if direction:
                    # Вход в позицию
                    await self._enter_position(direction)
                else:
                    log_info(self.user_id, "Направление входа не определено", module_name=__name__)
                    await self.stop("Нет четкого направления")
            else:
                log_info(self.user_id, "Импульс не подтвержден", module_name=__name__)
                await self.stop("Импульс не подтвержден")
                
        except Exception as e:
            log_error(self.user_id, f"Ошибка инициализации трейлинга: {e}", module_name=__name__)
            await self.stop("Ошибка инициализации")
            
    async def _handle_price_update(self, event: PriceUpdateEvent):
        """Обработка обновления цены"""
        try:
            current_price = event.price
            
            if self.position_side and self.entry_price:
                # Обновление пиковой цены
                await self._update_peak_price(current_price)
                
                # Проверка трейлинг-стопа
                await self._check_trailing_stop(current_price)
                
                # Проверка частичного закрытия
                await self._check_partial_close(current_price)
                
                # Проверка таймаута позиции
                await self._check_position_timeout()
                
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
                f"Ордер трейлинга исполнен: {side} {qty} по {price} (ID: {order_id})",
                module_name=__name__
            )
            
            # Если это ордер входа
            if not self.position_side:
                await self._handle_entry_order_filled(side, price, qty)
            else:
                # Если это ордер выхода
                await self._handle_exit_order_filled(side, price, qty)
                
        except Exception as e:
            log_error(self.user_id, f"Ошибка обработки исполнения ордера: {e}", module_name=__name__)
            
    async def _load_trailing_parameters(self):
        """Загрузка параметров трейлинга из конфигурации"""
        try:
            await self._ensure_config_fresh()
            
            if not self.config:
                return
                
            # Основные параметры трейлинга
            self.initial_stop_percent = self._convert_to_decimal(
                self.config.get("initial_stop_percent", 2.0)
            )
            self.trailing_step_percent = self._convert_to_decimal(
                self.config.get("trailing_step_percent", 0.5)
            )
            self.min_profit_for_trailing = self._convert_to_decimal(
                self.config.get("min_profit_for_trailing", 1.0)
            )
            
            # Импульсные параметры
            self.impulse_threshold_percent = self._convert_to_decimal(
                self.config.get("impulse_threshold_percent", 3.0)
            )
            self.volume_confirmation = self.config.get("volume_confirmation", True)
            self.min_volume_ratio = self._convert_to_decimal(
                self.config.get("min_volume_ratio", 1.5)
            )
            
            # Частичное закрытие
            self.partial_close_enabled = self.config.get("partial_close_enabled", True)
            self.partial_close_percent = self.config.get("partial_close_percent", 50)
            
            # Таймауты
            max_holding_hours = self.config.get("max_holding_time_hours")
            self.max_holding_time = timedelta(hours=max_holding_hours)
            
            log_info(
                self.user_id,
                f"Параметры трейлинга: стоп={self.initial_stop_percent}%, "
                f"шаг={self.trailing_step_percent}%, импульс={self.impulse_threshold_percent}%",
                module_name=__name__
            )
            
        except Exception as e:
            log_error(self.user_id, f"Ошибка загрузки параметров трейлинга: {e}", module_name=__name__)
            
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
            
    async def _analyze_impulse_signal(self) -> bool:
        """Анализ импульсного сигнала из signal_data"""
        try:
            # Проверка силы сигнала
            signal_strength = self.signal_data.get("strength", 0)
            min_strength = self.config.get("min_signal_strength", 75) if self.config else 75

            if signal_strength < min_strength:
                log_info(
                    self.user_id,
                    f"Сила сигнала недостаточна: {signal_strength} < {min_strength}",
                    module_name=__name__
                )
                return False

            # Проверка рыночных условий
            market_condition = self.signal_data.get("regime", "")
            required_conditions = ["STRONG_TREND", "TREND"]

            if market_condition not in required_conditions:
                log_info( self.user_id, f"Неподходящие рыночные условия: {market_condition}", module_name=__name__)
                return False
                
            # Проверка подтверждения трендом
            trend_confirmation = self.signal_data.get("trend_confirmation", False)
            if self.config and self.config.get("trend_confirmation_required", True):
                if not trend_confirmation:
                    log_info(self.user_id, "Отсутствует подтверждение тренда", module_name=__name__)
                    return False
                    
            # Обновление статистики
            self.trailing_stats["impulses_detected"] += 1
            
            log_info(
                self.user_id,
                f"Импульс подтвержден: сила={signal_strength}, условия={market_condition}",
                module_name=__name__
            )
            
            return True
            
        except Exception as e:
            log_error(self.user_id, f"Ошибка анализа импульсного сигнала: {e}", module_name=__name__)
            return False

    async def _determine_entry_direction(self) -> Optional[str]:
        """Определение направления входа"""
        try:
            # Получаем направление из данных анализа по ключу 'trend_direction'
            trend_direction = self.signal_data.get("trend_direction", "")

            if trend_direction == "UP":
                return "Buy"
            elif trend_direction == "DOWN":
                return "Sell"

            # Если направление не определено или "SIDEWAYS", не входим в сделку
            return None
            
        except Exception as e:
            log_error(self.user_id, f"Ошибка определения направления: {e}", module_name=__name__)
            return None

    async def _enter_position(self, direction: str):
        """Вход в позицию"""
        try:
            # Расчет размера позиции
            position_size = await self.calculate_order_size()
            if position_size <= 0:
                log_error(self.user_id, "Недостаточно средств для входа в позицию", module_name=__name__)
                return

            # Получение текущей цены из данных сигнала
            current_price = self._convert_to_decimal(self.signal_data.get('current_price', '0'))
            if not current_price > 0:
                log_error(self.user_id, "Не удалось получить текущую цену из сигнала", module_name=__name__)
                return

            # Конвертация размера в количество
            if self.api:
                qty = await self.api.calculate_quantity_from_usdt(self.symbol, position_size, current_price)
                if qty <= 0:
                    log_error(self.user_id, "Некорректное количество для ордера", module_name=__name__)
                    return

                # Размещение рыночного ордера
                order_id = await self._place_order(
                    side=direction,
                    order_type="Market",
                    qty=qty
                )

                if order_id:
                    log_info(
                        self.user_id,
                        f"Ордер входа размещен: {direction} {qty} по рынку (ID: {order_id})",
                        module_name=__name__
                    )
                else:
                    log_error(self.user_id, "Не удалось разместить ордер входа", module_name=__name__)
                    await self.stop("Ошибка размещения ордера")

        except Exception as e:
            log_error(self.user_id, f"Ошибка входа в позицию: {e}", module_name=__name__)
            await self.stop("Ошибка входа в позицию")
            
    async def calculate_order_size(self) -> Decimal:
        """Расчет размера позиции"""
        try:
            if not self.config:
                return Decimal('0')
                
            # Базовый размер из конфигурации
            base_size = self._convert_to_decimal(self.config.get("order_amount", 10.0))
            position_size_percent = self.config.get("position_size_percent", 50)
            
            # Размер для трейлинга
            position_size = base_size * (self._convert_to_decimal(position_size_percent) / Decimal('100'))
            
            # Максимальный размер позиции
            max_size = self._convert_to_decimal(self.config.get("max_position_size", 50.0))
            
            return min(position_size, max_size)
            
        except Exception as e:
            log_error(self.user_id, f"Ошибка расчета размера позиции: {e}", module_name=__name__)
            return Decimal('0')

        
    async def _handle_entry_order_filled(self, side: str, price: Decimal, qty: Decimal):
        """Обработка исполнения ордера входа"""
        try:
            self.position_side = side
            self.entry_price = price
            self.position_size = qty
            self.position_opened_at = datetime.now()
            self.peak_price = price
            
            # Расчет начального стоп-лосса
            if side == "Buy":
                self.current_stop_price = price * (1 - self.initial_stop_percent / 100)
            else:
                self.current_stop_price = price * (1 + self.initial_stop_percent / 100)
                
            # Обновление статистики
            self.trailing_stats["positions_opened"] += 1
            
            log_info(
                self.user_id,
                f"Позиция открыта: {side} {qty} по {price}, стоп={self.current_stop_price}",
                module_name=__name__
            )
            
        except Exception as e:
            log_error(self.user_id, f"Ошибка обработки входа в позицию: {e}", module_name=__name__)

    async def _handle_exit_order_filled(self, side: str, price: Decimal, qty: Decimal):
        """Обработка исполнения ордера выхода"""
        try:
            pnl = None  # Инициализируем pnl значением по умолчанию
            # Расчет PnL
            if self.entry_price and self.position_side:
                if self.position_side == "Buy":
                    pnl = (price - self.entry_price) * qty
                else:
                    pnl = (self.entry_price - price) * qty

                # Обновление статистики
                if pnl > 0:
                    self.stats["profit_orders"] += 1
                else:
                    self.stats["loss_orders"] += 1

                self.stats["total_pnl"] += pnl

            log_info(
                self.user_id,
                f"Позиция закрыта: {side} {qty} по {price}, PnL={pnl if pnl is not None else 'N/A'}",
                module_name=__name__
            )
            # Сброс состояния позиции
            await self._reset_position_state()

        except Exception as e:
            log_error(self.user_id, f"Ошибка обработки выхода из позиции: {e}", module_name=__name__)
            
    async def _update_peak_price(self, current_price: Decimal):
        """Обновление пиковой цены"""
        try:
            if not self.peak_price or not self.position_side:
                return
                
            # Обновление пика в зависимости от направления позиции
            if self.position_side == "Buy" and current_price > self.peak_price:
                self.peak_price = current_price
                await self._update_trailing_stop()
            elif self.position_side == "Sell" and current_price < self.peak_price:
                self.peak_price = current_price
                await self._update_trailing_stop()
                
            # Обновление максимальных экскурсий
            if self.entry_price:
                if self.position_side == "Buy":
                    favorable = current_price - self.entry_price
                    adverse = self.entry_price - current_price
                else:
                    favorable = self.entry_price - current_price
                    adverse = current_price - self.entry_price
                    
                if favorable > self.trailing_stats["max_favorable_excursion"]:
                    self.trailing_stats["max_favorable_excursion"] = favorable
                    
                if adverse > self.trailing_stats["max_adverse_excursion"]:
                    self.trailing_stats["max_adverse_excursion"] = adverse
                    
        except Exception as e:
            log_error(self.user_id, f"Ошибка обновления пиковой цены: {e}", module_name=__name__)
            
    async def _update_trailing_stop(self):
        """Обновление трейлинг-стопа"""
        try:
            if not self.peak_price or not self.entry_price or not self.position_side:
                return
                
            # Проверка минимальной прибыли для активации трейлинга
            if self.position_side == "Buy":
                current_profit_percent = ((self.peak_price - self.entry_price) / self.entry_price) * 100
                new_stop = self.peak_price * (1 - self.trailing_step_percent / 100)
            else:
                current_profit_percent = ((self.entry_price - self.peak_price) / self.entry_price) * 100
                new_stop = self.peak_price * (1 + self.trailing_step_percent / 100)
                
            # Активация трейлинга только при достижении минимальной прибыли
            if current_profit_percent >= self.min_profit_for_trailing:
                # Обновление стопа только в выгодную сторону
                if self.position_side == "Buy" and new_stop > self.current_stop_price:
                    self.current_stop_price = new_stop
                    self.trailing_stats["trailing_activations"] += 1
                    
                    log_info(
                        self.user_id,
                        f"Трейлинг-стоп обновлен: {self.current_stop_price} (прибыль: {current_profit_percent:.2f}%)",
                        module_name=__name__
                    )
                    
                elif self.position_side == "Sell" and new_stop < self.current_stop_price:
                    self.current_stop_price = new_stop
                    self.trailing_stats["trailing_activations"] += 1
                    
                    log_info(
                        self.user_id,
                        f"Трейлинг-стоп обновлен: {self.current_stop_price} (прибыль: {current_profit_percent:.2f}%)",
                        module_name=__name__
                    )
                    
        except Exception as e:
            log_error(self.user_id, f"Ошибка обновления трейлинг-стопа: {e}", module_name=__name__)
            
    async def _check_trailing_stop(self, current_price: Decimal):
        """Проверка срабатывания трейлинг-стопа"""
        try:
            if not self.current_stop_price or not self.position_side or not self.position_size:
                return
                
            stop_triggered = False
            
            if self.position_side == "Buy" and current_price <= self.current_stop_price:
                stop_triggered = True
            elif self.position_side == "Sell" and current_price >= self.current_stop_price:
                stop_triggered = True
                
            if stop_triggered:
                log_info(
                    self.user_id,
                    f"Трейлинг-стоп сработал: цена={current_price}, стоп={self.current_stop_price}",
                    module_name=__name__
                )
                
                # Закрытие позиции рыночным ордером
                close_side = "Sell" if self.position_side == "Buy" else "Buy"
                
                order_id = await self._place_order(
                    side=close_side,
                    order_type="Market",
                    qty=self.position_size
                )
                
                if order_id:
                    log_info(
                        self.user_id,
                        f"Ордер закрытия по стопу размещен: {close_side} {self.position_size}",
                        module_name=__name__
                    )
                else:
                    log_error(self.user_id, "Не удалось разместить ордер закрытия по стопу", module_name=__name__)
                    
        except Exception as e:
            log_error(self.user_id, f"Ошибка проверки трейлинг-стопа: {e}", module_name=__name__)
            
    async def _check_partial_close(self, current_price: Decimal):
        """Проверка частичного закрытия позиции"""
        try:
            if (not self.partial_close_enabled or self.partial_close_executed or 
                not self.entry_price or not self.position_side or not self.position_size):
                return
                
            # Расчет текущей прибыли
            if self.position_side == "Buy":
                profit_percent = ((current_price - self.entry_price) / self.entry_price) * 100
            else:
                profit_percent = ((self.entry_price - current_price) / self.entry_price) * 100
                
            # Порог для частичного закрытия (например, 3% прибыли)
            partial_close_threshold = self.config.get("partial_close_threshold", 3.0) if self.config else 3.0
            
            if profit_percent >= partial_close_threshold:
                # Размер частичного закрытия
                partial_qty = self.position_size * (self.partial_close_percent / 100)
                close_side = "Sell" if self.position_side == "Buy" else "Buy"
                
                order_id = await self._place_order(
                    side=close_side,
                    order_type="Market",
                    qty=partial_qty
                )
                
                if order_id:
                    self.partial_close_executed = True
                    self.position_size -= partial_qty
                    self.trailing_stats["partial_closes"] += 1
                    
                    log_info(
                        self.user_id,
                        f"Частичное закрытие: {close_side} {partial_qty} при прибыли {profit_percent:.2f}%",
                        module_name=__name__
                    )
                    
        except Exception as e:
            log_error(self.user_id, f"Ошибка частичного закрытия: {e}", module_name=__name__)
            
    async def _check_position_timeout(self):
        """Проверка таймаута позиции"""
        try:
            if not self.position_opened_at or not self.position_side:
                return
                
            if datetime.now() - self.position_opened_at > self.max_holding_time:
                log_info(
                    self.user_id,
                    f"Таймаут позиции: {self.max_holding_time}, принудительное закрытие",
                    module_name=__name__
                )
                
                # Закрытие позиции по таймауту
                close_side = "Sell" if self.position_side == "Buy" else "Buy"
                
                order_id = await self._place_order(
                    side=close_side,
                    order_type="Market",
                    qty=self.position_size
                )
                
                if order_id:
                    log_info(
                        self.user_id,
                        f"Ордер закрытия по таймауту размещен: {close_side} {self.position_size}",
                        module_name=__name__
                    )
                    
        except Exception as e:
            log_error(self.user_id, f"Ошибка проверки таймаута позиции: {e}", module_name=__name__)
            
    async def _reset_position_state(self):
        """Сброс состояния позиции"""
        try:
            self.position_side = None
            self.entry_price = None
            self.current_stop_price = None
            self.peak_price = None
            self.position_size = None
            self.position_opened_at = None
            self.partial_close_executed = False
            
            # Остановка стратегии после закрытия позиции
            await self.stop("Позиция закрыта")
            
        except Exception as e:
            log_error(self.user_id, f"Ошибка сброса состояния позиции: {e}", module_name=__name__)
            
    async def get_strategy_stats(self) -> Dict[str, Any]:
        """Получение статистики стратегии"""
        base_stats = await self.get_status()
        
        trailing_specific_stats = {
            "trailing_stats": {
                "impulses_detected": self.trailing_stats["impulses_detected"],
                "positions_opened": self.trailing_stats["positions_opened"],
                "trailing_activations": self.trailing_stats["trailing_activations"],
                "partial_closes": self.trailing_stats["partial_closes"],
                "max_favorable_excursion": float(self.trailing_stats["max_favorable_excursion"]),
                "max_adverse_excursion": float(self.trailing_stats["max_adverse_excursion"])
            },
            "trailing_parameters": {
                "initial_stop_percent": float(self.initial_stop_percent),
                "trailing_step_percent": float(self.trailing_step_percent),
                "min_profit_for_trailing": float(self.min_profit_for_trailing),
                "impulse_threshold_percent": float(self.impulse_threshold_percent),
                "partial_close_enabled": self.partial_close_enabled,
                "partial_close_percent": self.partial_close_percent
            },
            "current_position": {
                "position_side": self.position_side,
                "entry_price": float(self.entry_price) if self.entry_price else None,
                "current_stop_price": float(self.current_stop_price) if self.current_stop_price else None,
                "peak_price": float(self.peak_price) if self.peak_price else None,
                "position_size": float(self.position_size) if self.position_size else None,
                "position_opened_at": self.position_opened_at.isoformat() if self.position_opened_at else None,
                "partial_close_executed": self.partial_close_executed
            }
        }
        
        return {**base_stats, **trailing_specific_stats}

