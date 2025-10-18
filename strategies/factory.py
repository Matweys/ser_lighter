# strategies/factory.py
"""
Фабрика для создания экземпляров торговых стратегий.
Разрывает циклические зависимости между базовой и конкретными стратегиями.
"""
from typing import Dict, Any, Optional
from aiogram import Bot
# Импортируем все необходимые компоненты
from api.bybit_api import BybitAPI
from core.events import EventBus
from .base_strategy import BaseStrategy
from .signal_scalper_strategy import SignalScalperStrategy
from .flash_drop_catcher_strategy import FlashDropCatcherStrategy
from core.logger import log_error

def create_strategy(
    strategy_type: str,
    user_id: int,
    symbol: str,
    signal_data: Dict[str, Any],
    api: BybitAPI,
    event_bus: EventBus,
    bot: Bot,
    config: Optional[Dict[str, Any]] = None
) -> Optional[BaseStrategy]:
    """
    Фабричная функция для создания стратегий.
    """
    strategy_map = {
        "signal_scalper": SignalScalperStrategy,
        "flash_drop_catcher": FlashDropCatcherStrategy,
    }

    strategy_class = strategy_map.get(strategy_type)
    if not strategy_class:
        log_error(user_id, f"Неподдерживаемый тип стратегии: {strategy_type}", module_name="strategy_factory")
        return None

    # Передаем все необходимые аргументы в конструктор
    return strategy_class(user_id, symbol, signal_data, api, event_bus, bot, config)