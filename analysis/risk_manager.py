# core/risk_manager.py
"""
Профессиональный риск-менеджер для многопользовательской торговой системы
Реализует динамическое управление рисками с персональными настройками из Redis
"""
import asyncio
from decimal import Decimal, getcontext
from typing import Dict, Optional, List
from datetime import datetime, timedelta
from api.bybit_api import BybitAPI
from cache.redis_manager import redis_manager
from core.logger import log_info, log_error
from core.events import (
    EventType, OrderFilledEvent, PositionClosedEvent,
    RiskLimitExceededEvent, DrawdownWarningEvent, EventBus
)
from core.enums import ConfigType


# Настройка точности для Decimal
getcontext().prec = 28


class RiskManager:
    """
    Персональный риск-менеджер для пользователя
    Функции:
    - Контроль лимитов одновременных сделок
    - Динамический расчет размера позиции на основе ATR
    - Мониторинг дневной просадки
    - Контроль общего риска портфеля
    - Автоматическое управление рисками в реальном времени
    """
    
    def __init__(self, user_id: int, bybit_api: BybitAPI, event_bus: EventBus):
        self.user_id = user_id
        self.api = bybit_api
        self.event_bus = event_bus
        self.running = False

        # Кэш для оптимизации
        self.user_config: Optional[Dict] = None
        self.last_config_update = datetime.min
        self.config_cache_duration = timedelta(minutes=5)
        
        # Статистика риска
        self.daily_stats = {
            "trades_count": 0,
            "realized_pnl": Decimal('0'),
            "unrealized_pnl": Decimal('0'),
            "max_drawdown": Decimal('0'),
            "start_balance": Decimal('0')
        }
        
        # Активные позиции для мониторинга
        self.active_positions: Dict[str, Dict] = {}  # symbol -> position_data
        
    async def start(self):
        """Запуск риск-менеджера"""
        if self.running:
            return
            
        log_info(self.user_id, "Запуск RiskManager...", module_name=__name__)
        
        try:
            # Загрузка конфигурации
            await self._load_user_config()
            
            # Инициализация дневной статистики
            await self._initialize_daily_stats()
            
            # Подписка на события
            self.event_bus.subscribe(EventType.ORDER_FILLED, self._handle_order_filled)
            self.event_bus.subscribe(EventType.POSITION_CLOSED, self._handle_position_closed)
            
            self.running = True
            log_info(self.user_id, "RiskManager запущен", module_name=__name__)
            
        except Exception as e:
            log_error(self.user_id, f"Ошибка запуска RiskManager: {e}", module_name=__name__)
            raise
            
    async def stop(self):
        """Остановка риск-менеджера"""
        if not self.running:
            return
            
        self.running = False
        log_info(self.user_id, "RiskManager остановлен", module_name=__name__)

    async def get_account_balance(self) -> Decimal:
        """Получение баланса: реального для production или виртуального для testnet."""
        try:
            # Если API работает в тестовом режиме (BYBIT_SANDBOX=True), используем виртуальный баланс.
            if self.api.testnet:
                # Загружаем конфиг, чтобы взять оттуда сумму виртуального баланса
                await self._ensure_config_fresh()
                # Для гибкости, можно добавить сумму в конфиг, если ее нет - используем 10000.
                paper_balance = Decimal(str(self.user_config.get("paper_trading_balance", "10000.0")))
                log_info(self.user_id, f"РЕЖИМ TESTNET: Используется виртуальный баланс: {paper_balance} USDT", module_name=__name__)
                return paper_balance

            # В реальном режиме получаем баланс через API
            balance_data = await self.api.get_wallet_balance()
            if balance_data and "totalEquity" in balance_data:
                return balance_data["totalEquity"]

            log_warning(self.user_id, "Не удалось получить реальный баланс через API, возвращен 0.", module_name=__name__)
            return Decimal('0')
        except Exception as e:
            log_error(self.user_id, f"Ошибка получения баланса: {e}", module_name=__name__)
            return Decimal('0')

    async def can_start_new_strategy(self) -> bool:
        """
        Проверка возможности запуска новой стратегии
        
        Returns:
            bool: True если можно запустить новую стратегию
        """
        try:
            await self._ensure_config_fresh()
            
            if not self.user_config:
                return False
                
            # Проверка лимита одновременных сделок
            max_trades = self.user_config.get("max_simultaneous_trades", 2)
            current_trades = len(self.active_positions)
            
            if current_trades >= max_trades:
                log_info(
                    self.user_id,
                    f"Превышен лимит одновременных сделок: {current_trades}/{max_trades}",
                    module_name=__name__
                )
                return False
                
            # Проверка дневной просадки
            if await self._check_daily_drawdown_limit():
                return False
                
            # Проверка общего риска портфеля
            if await self._check_portfolio_risk_limit():
                return False
                
            return True
            
        except Exception as e:
            log_error(self.user_id, f"Ошибка проверки возможности запуска стратегии: {e}", module_name=__name__)
            return False
            
    async def can_open_new_trade(self, symbol: str) -> bool:
        """
        Проверка возможности открытия новой сделки по символу
        
        Args:
            symbol: Торговый символ
            
        Returns:
            bool: True если можно открыть сделку
        """
        try:
            # Проверка общих лимитов
            if not await self.can_start_new_strategy():
                return False
                
            # Проверка, нет ли уже активной позиции по символу
            if symbol in self.active_positions:
                log_info(
                    self.user_id,
                    f"Уже есть активная позиция по {symbol}",
                    module_name=__name__
                )
                return False
                
            return True
            
        except Exception as e:
            log_error(self.user_id, f"Ошибка проверки возможности открытия сделки {symbol}: {e}", module_name=__name__)
            return False
            
    async def calculate_position_size(self, symbol: str, analysis_data: Dict) -> Decimal:
        """
        Динамический расчет размера позиции на основе ATR и персональных настроек риска
        
        Args:
            symbol: Торговый символ
            analysis_data: Данные анализа рынка (содержит ATR, current_price)
            
        Returns:
            Decimal: Размер позиции в USDT
        """
        try:
            await self._ensure_config_fresh()
            
            if not self.user_config:
                return Decimal('10.0')  # Fallback
                
            # Получение баланса пользователя
            balance = await self.get_account_balance()
            if balance <= 0:
                return Decimal('0')
                
            # Процент риска на сделку
            risk_per_trade_percent = Decimal(str(self.user_config.get("risk_per_trade_percent", 1.0)))
            risk_amount_usd = balance * (risk_per_trade_percent / 100)
            
            # Получение данных для расчета
            current_price = analysis_data.get('current_price', Decimal('0'))
            atr = analysis_data.get('atr', Decimal('0'))
            
            if current_price <= 0 or atr <= 0:
                # Fallback на фиксированный размер
                fallback_amount = Decimal(str(self.user_config.get("order_amount", 10.0)))
                log_info(
                    self.user_id,
                    f"Недостаточно данных для динамического расчета, используем фиксированный размер: {fallback_amount}",
                    module_name=__name__
                )
                return fallback_amount
                
            # Динамический расчет на основе ATR
            # Стоп-лосс на расстоянии 2 * ATR
            stop_loss_distance_usd = atr * Decimal('2.0')
            stop_loss_distance_percent = stop_loss_distance_usd / current_price
            
            # Размер позиции = Риск на сделку / Процент стоп-лосса
            position_size = risk_amount_usd / stop_loss_distance_percent
            
            # Ограничения размера позиции
            min_position = Decimal('5.0')  # Минимум 5 USDT
            max_position = balance * Decimal('0.1')  # Максимум 10% от баланса
            
            position_size = max(min_position, min(position_size, max_position))
            
            log_info(
                self.user_id,
                f"Расчет размера позиции для {symbol}: "
                f"Баланс={balance:.2f} USDT, Риск={risk_per_trade_percent:.1f}%, "
                f"ATR={atr:.6f}, Цена={current_price:.4f}, "
                f"Размер позиции={position_size:.2f} USDT",
                module_name=__name__
            )
            
            return position_size
            
        except Exception as e:
            log_error(self.user_id, f"Ошибка расчета размера позиции для {symbol}: {e}", module_name=__name__)
            # Fallback на настройки пользователя
            await self._ensure_config_fresh()
            fallback = Decimal(str(self.user_config.get("order_amount", 10.0) if self.user_config else 10.0))
            return fallback
            
    async def calculate_stop_loss_price(self, symbol: str, entry_price: Decimal, side: str, analysis_data: Dict) -> Decimal:
        """
        Расчет цены стоп-лосса на основе ATR
        
        Args:
            symbol: Торговый символ
            entry_price: Цена входа
            side: Направление сделки (Buy/Sell)
            analysis_data: Данные анализа
            
        Returns:
            Decimal: Цена стоп-лосса
        """
        try:
            atr = analysis_data.get('atr', Decimal('0'))
            
            if atr <= 0:
                # Fallback на процентный стоп-лосс
                await self._ensure_config_fresh()
                stop_loss_percent = Decimal(str(self.user_config.get("stop_loss_percent", 2.0) if self.user_config else 2.0))
                multiplier = Decimal('1') - (stop_loss_percent / 100) if side == "Buy" else Decimal('1') + (stop_loss_percent / 100)
                return entry_price * multiplier
                
            # Динамический стоп-лосс на основе ATR
            atr_multiplier = Decimal('2.0')  # 2 * ATR
            
            if side == "Buy":
                stop_loss_price = entry_price - (atr * atr_multiplier)
            else:  # Sell
                stop_loss_price = entry_price + (atr * atr_multiplier)
                
            return stop_loss_price
            
        except Exception as e:
            log_error(self.user_id, f"Ошибка расчета стоп-лосса для {symbol}: {e}", module_name=__name__)
            # Fallback
            stop_loss_percent = Decimal('2.0')
            multiplier = Decimal('1') - (stop_loss_percent / 100) if side == "Buy" else Decimal('1') + (stop_loss_percent / 100)
            return entry_price * multiplier
            
    async def update_position(self, symbol: str, position_data: Dict):
        """Обновление данных позиции для мониторинга"""
        self.active_positions[symbol] = {
            'symbol': symbol,
            'side': position_data.get('side'),
            'size': Decimal(str(position_data.get('size', 0))),
            'entry_price': Decimal(str(position_data.get('entry_price', 0))),
            'mark_price': Decimal(str(position_data.get('mark_price', 0))),
            'unrealized_pnl': Decimal(str(position_data.get('unrealized_pnl', 0))),
            'updated_at': datetime.now()
        }
        
        # Обновление общей статистики
        await self._update_risk_stats()
        
    async def remove_position(self, symbol: str):
        """Удаление позиции из мониторинга"""
        if symbol in self.active_positions:
            del self.active_positions[symbol]
            await self._update_risk_stats()
            
    async def get_risk_stats(self) -> Dict:
        """Получение статистики рисков"""
        await self._update_risk_stats()
        
        return {
            "user_id": self.user_id,
            "active_positions_count": len(self.active_positions),
            "daily_stats": {
                "trades_count": self.daily_stats["trades_count"],
                "realized_pnl": float(self.daily_stats["realized_pnl"]),
                "unrealized_pnl": float(self.daily_stats["unrealized_pnl"]),
                "max_drawdown": float(self.daily_stats["max_drawdown"]),
                "current_drawdown_percent": await self._calculate_current_drawdown_percent()
            },
            "active_positions": {
                symbol: {
                    "side": pos["side"],
                    "size": float(pos["size"]),
                    "unrealized_pnl": float(pos["unrealized_pnl"])
                }
                for symbol, pos in self.active_positions.items()
            }
        }
        
    async def _handle_order_filled(self, event: OrderFilledEvent):
        """Обработчик исполнения ордера"""
        if event.user_id != self.user_id:
            return
            
        self.daily_stats["trades_count"] += 1
        log_info(self.user_id, f"Ордер исполнен: {event.symbol} {event.side} {event.qty}", module_name=__name__)
        
    async def _handle_position_closed(self, event: PositionClosedEvent):
        """Обработчик закрытия позиции"""
        if event.user_id != self.user_id:
            return
            
        # Обновление реализованного PnL
        self.daily_stats["realized_pnl"] += event.realized_pnl
        
        # Удаление позиции из мониторинга
        await self.remove_position(event.symbol)
        
        log_info(
            self.user_id,
            f"Позиция закрыта: {event.symbol} PnL={event.realized_pnl:.2f}",
            module_name=__name__
        )

    async def _load_user_config(self):
        """Загрузка конфигурации пользователя"""
        try:
            self.user_config = await redis_manager.get_config(self.user_id, ConfigType.GLOBAL)
            self.last_config_update = datetime.now()

            if not self.user_config:
                log_error(self.user_id, "Конфигурация пользователя не найдена", module_name=__name__)

        except Exception as e:
            log_error(self.user_id, f"Ошибка загрузки конфигурации: {e}", module_name=__name__)
            
    async def _ensure_config_fresh(self):
        """Обеспечение актуальности конфигурации"""
        if datetime.now() - self.last_config_update > self.config_cache_duration:
            await self._load_user_config()

    async def _initialize_daily_stats(self):
        """Инициализация дневной статистики"""
        try:
            balance = await self.get_account_balance()
            self.daily_stats["start_balance"] = balance

            # Загрузка сохраненной статистики из Redis (используем get_cached_data)
            saved_stats = await redis_manager.get_cached_data(f"user:{self.user_id}:daily_stats")
            if saved_stats:
                # Проверяем, что статистика за сегодня
                today = datetime.now().date().isoformat()
                if saved_stats.get("date") == today:
                    self.daily_stats.update({
                        "trades_count": saved_stats.get("trades_count", 0),
                        "realized_pnl": Decimal(str(saved_stats.get("realized_pnl", 0))),
                        "max_drawdown": Decimal(str(saved_stats.get("max_drawdown", 0)))
                    })
                    
        except Exception as e:
            log_error(self.user_id, f"Ошибка инициализации дневной статистики: {e}", module_name=__name__)

    async def _update_risk_stats(self):
        """Обновление статистики рисков"""
        try:
            # Расчет общего нереализованного PnL
            total_unrealized = sum((pos.get("unrealized_pnl", Decimal('0')) for pos in self.active_positions.values()), Decimal('0'))
            self.daily_stats["unrealized_pnl"] = total_unrealized

            # Расчет текущей просадки
            current_drawdown = await self._calculate_current_drawdown_percent()

            # Обновление максимальной просадки
            if current_drawdown > self.daily_stats["max_drawdown"]:
                self.daily_stats["max_drawdown"] = current_drawdown

            # Сохранение статистики в Redis
            await self._save_daily_stats()

            # Проверка лимитов
            await self._check_risk_limits()

        except Exception as e:
            log_error(self.user_id, f"Ошибка обновления статистики рисков: {e}", module_name=__name__)

    async def _calculate_current_drawdown_percent(self) -> Decimal:
        """Расчет текущей просадки в процентах"""
        try:
            if self.daily_stats["start_balance"] <= 0:
                return Decimal('0')

            total_pnl = self.daily_stats["realized_pnl"] + self.daily_stats["unrealized_pnl"]
            drawdown_percent = abs(min(Decimal('0'), total_pnl)) / self.daily_stats["start_balance"] * 100

            return drawdown_percent

        except Exception as e:
            log_error(self.user_id, f"Ошибка расчета просадки: {e}", module_name=__name__)
            return Decimal('0')
            
    async def _check_daily_drawdown_limit(self) -> bool:
        """Проверка лимита дневной просадки"""
        try:
            await self._ensure_config_fresh()
            
            if not self.user_config:
                return False
                
            max_drawdown = Decimal(str(self.user_config.get("global_daily_drawdown_percent", 5.0)))
            current_drawdown = await self._calculate_current_drawdown_percent()
            
            if current_drawdown >= max_drawdown:
                await self.event_bus.publish(RiskLimitExceededEvent(
                    user_id=self.user_id,
                    limit_type="daily_drawdown",
                    current_value=current_drawdown,
                    limit_value=max_drawdown,
                    action_required="stop_trading"
                ))
                return True
                
            # Предупреждение при 80% от лимита
            warning_threshold = max_drawdown * Decimal('0.8')
            if current_drawdown >= warning_threshold:
                await self.event_bus.publish(DrawdownWarningEvent(
                    user_id=self.user_id,
                    current_drawdown_percent=current_drawdown,
                    warning_threshold_percent=warning_threshold,
                    max_drawdown_percent=max_drawdown
                ))
                
            return False
            
        except Exception as e:
            log_error(self.user_id, f"Ошибка проверки лимита просадки: {e}", module_name=__name__)
            return False
            
    async def _check_portfolio_risk_limit(self) -> bool:
        """Проверка общего риска портфеля"""
        try:
            # Проверка общего размера открытых позиций
            total_position_size = sum(
                pos["size"] * pos["mark_price"] 
                for pos in self.active_positions.values()
            )
            
            balance = await self.get_account_balance()
            if balance <= 0:
                return True
                
            # Максимум 50% баланса в открытых позициях
            max_exposure = balance * Decimal('0.5')
            
            if total_position_size > max_exposure:
                log_info(
                    self.user_id,
                    f"Превышен лимит экспозиции портфеля: {total_position_size:.2f} > {max_exposure:.2f}",
                    module_name=__name__
                )
                return True
                
            return False
            
        except Exception as e:
            log_error(self.user_id, f"Ошибка проверки риска портфеля: {e}", module_name=__name__)
            return False
            
    async def _check_risk_limits(self):
        """Проверка всех лимитов риска"""
        await self._check_daily_drawdown_limit()
        await self._check_portfolio_risk_limit()
        
    async def _save_daily_stats(self):
        """Сохранение дневной статистики в Redis"""
        try:
            stats_data = {
                "date": datetime.now().date().isoformat(),
                "trades_count": self.daily_stats["trades_count"],
                "realized_pnl": float(self.daily_stats["realized_pnl"]),
                "max_drawdown": float(self.daily_stats["max_drawdown"])
            }
            
            await redis_manager.set_json(
                f"user:{self.user_id}:daily_stats",
                stats_data,
                expire=86400  # 24 часа
            )
            
        except Exception as e:
            log_error(self.user_id, f"Ошибка сохранения статистики: {e}", module_name=__name__)