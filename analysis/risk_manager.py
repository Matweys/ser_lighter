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
from core.logger import log_info, log_error, log_warning
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
            "start_balance": Decimal('0'),
            "peak_daily_balance": Decimal('0')
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
            await self.event_bus.subscribe(EventType.ORDER_FILLED, self._handle_order_filled, user_id=self.user_id)
            await self.event_bus.subscribe(EventType.POSITION_CLOSED, self._handle_position_closed, user_id=self.user_id)
            
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
        """Получение баланса: реального для production или виртуального для demo."""
        try:
            # Если API работает в демо режиме, используем виртуальный баланс.
            if self.api.demo:
                await self._ensure_config_fresh()
                paper_balance = Decimal(str(self.user_config.get("paper_trading_balance", "10000.0")))
                log_info(self.user_id, f"РЕЖИМ DEMO: Используется виртуальный баланс: {paper_balance} USDT", module_name=__name__)
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
            max_trades = self.user_config.get("max_concurrent_trades", 3)
            current_trades = len(self.active_positions)
            
            if current_trades >= max_trades:
                log_info(self.user_id, f"Превышен лимит одновременных сделок: {current_trades}/{max_trades}", module_name=__name__)
                return False

            # Проверка дневной просадки
            if await self._check_daily_loss_limit():
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


    async def calculate_position_size(self, symbol: str) -> Decimal:
        """
        Возвращает фиксированный размер ордера из настроек пользователя.
        """
        try:
            await self._ensure_config_fresh()
            if not self.user_config:
                return Decimal('10.0')  # Аварийное значение

            order_amount = Decimal(str(self.user_config.get("order_amount", 10.0)))
            log_info(self.user_id, f"Используется фиксированный размер позиции для {symbol}: {order_amount} USDT",
                     module_name=__name__)
            return order_amount

        except Exception as e:
            log_error(self.user_id, f"Ошибка получения размера позиции для {symbol}: {e}", module_name=__name__)
            return Decimal('10.0')

            
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
            self.daily_stats["peak_daily_balance"] = balance

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


            # Обновление пикового баланса за день
            current_balance = self.daily_stats["start_balance"] + self.daily_stats["realized_pnl"] + self.daily_stats["unrealized_pnl"]
            if current_balance > self.daily_stats["peak_daily_balance"]:
                self.daily_stats["peak_daily_balance"] = current_balance

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

    async def _check_daily_loss_limit(self) -> bool:
        """
        Проверка лимита суточного убытка с "умным" трейлингом от пика прибыли.
        """
        try:
            await self._ensure_config_fresh()
            if not self.user_config:
                return False

            # 1. Получаем лимит убытка из настроек (например, 10 USDT)
            max_loss_usdt = Decimal(str(self.user_config.get("max_daily_loss_usdt", 10.0)))

            # 2. Определяем пиковый баланс за сегодня
            peak_balance = self.daily_stats.get("peak_daily_balance", self.daily_stats["start_balance"])

            # 3. Рассчитываем уровень стоп-аута (например, 1050 - 10 = 1040 USDT)
            stop_out_level = peak_balance - max_loss_usdt

            # 4. Получаем текущий баланс
            current_balance = self.daily_stats["start_balance"] + self.daily_stats["realized_pnl"] + self.daily_stats["unrealized_pnl"]

            # 5. Сравниваем
            if current_balance <= stop_out_level:
                log_warning(self.user_id,
                            f"ПРЕВЫШЕН СУТОЧНЫЙ ЛИМИТ УБЫТКА! "
                            f"Текущий баланс: {current_balance:.2f} USDT опустился ниже стоп-уровня: {stop_out_level:.2f} USDT "
                            f"(Пик: {peak_balance:.2f}, Лимит: {max_loss_usdt} USDT)",
                            module_name=__name__)

                await self.event_bus.publish(RiskLimitExceededEvent(
                    user_id=self.user_id,
                    limit_type="daily_loss_trailing",
                    current_value=current_balance,
                    limit_value=stop_out_level,
                    action_required="stop_trading"
                ))
                return True  # Лимит превышен

            return False  # Лимит не превышен

        except Exception as e:
            log_error(self.user_id, f"Ошибка проверки лимита суточного убытка: {e}", module_name=__name__)
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
        await self._check_daily_loss_limit()  # Заменено
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

            await redis_manager.cache_data(
                f"user:{self.user_id}:daily_stats",
                stats_data,
                ttl=86400  # 24 часа
            )
            
        except Exception as e:
            log_error(self.user_id, f"Ошибка сохранения статистики: {e}", module_name=__name__)