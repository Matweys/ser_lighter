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
from core.logger import log_error, log_info

def create_strategy(
    strategy_type: str,
    user_id: int,
    symbol: str,
    signal_data: Dict[str, Any],
    api: BybitAPI,
    event_bus: EventBus,
    bot: Bot,
    config: Optional[Dict[str, Any]] = None,
    account_priority: Optional[int] = None,
    data_feed=None
) -> Optional[BaseStrategy]:
    """
    Фабричная функция для создания стратегий.

    Args:
        strategy_type: Тип стратегии ("signal_scalper", "flash_drop_catcher")
        user_id: ID пользователя
        symbol: Торговый символ (например, "BTCUSDT")
        signal_data: Данные торгового сигнала
        api: Экземпляр API биржи Bybit
        event_bus: Шина событий для межкомпонентной коммуникации
        bot: Экземпляр Telegram бота для уведомлений
        config: Опциональная конфигурация стратегии
        account_priority: Приоритет торгового аккаунта (для мульти-аккаунта)
        data_feed: DataFeedHandler для IN-MEMORY tracking CLOSE операций (решает race condition)

    Returns:
        Optional[BaseStrategy]: Экземпляр стратегии или None при ошибке
    """
    strategy_map = {
        "signal_scalper": SignalScalperStrategy,
        "flash_drop_catcher": FlashDropCatcherStrategy,
    }

    strategy_class = strategy_map.get(strategy_type)
    if not strategy_class:
        log_error(user_id, f"Неподдерживаемый тип стратегии: {strategy_type}", module_name="strategy_factory")
        return None

    # КРИТИЧНО: Логируем передачу bot для диагностики пропадающих уведомлений
    if bot is None:
        log_error(user_id, f"❌ КРИТИЧЕСКАЯ ОШИБКА: bot=None при создании стратегии {strategy_type}! Уведомления работать НЕ БУДУТ!", module_name="strategy_factory")
    else:
        log_info(user_id, f"✅ Создание стратегии {strategy_type} с bot={type(bot).__name__} (OK)", module_name="strategy_factory")

    # Передаем все необходимые аргументы в конструктор (включая data_feed)
    return strategy_class(user_id, symbol, signal_data, api, event_bus, bot, config, account_priority, data_feed)