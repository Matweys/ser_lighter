# core/enums.py
"""
Перечисления для многопользовательской торговой системы
Содержит все типы данных, состояния и константы системы
"""
from enum import Enum, IntEnum


class StrategyType(Enum):
    """Типы торговых стратегий"""
    SIGNAL_SCALPER = "signal_scalper"


class PositionSide(Enum):
    """Стороны позиции"""
    LONG = "Buy"
    SHORT = "Sell"


class OrderType(Enum):
    """Типы ордеров"""
    MARKET = "Market"
    LIMIT = "Limit"
    STOP = "Stop"
    STOP_LIMIT = "StopLimit"
    TAKE_PROFIT = "TakeProfit"
    TAKE_PROFIT_LIMIT = "TakeProfitLimit"


class OrderStatus(Enum):
    """Статусы ордеров"""
    NEW = "New"
    PARTIALLY_FILLED = "PartiallyFilled"
    FILLED = "Filled"
    CANCELLED = "Cancelled"
    REJECTED = "Rejected"
    PENDING_CANCEL = "PendingCancel"
    DEACTIVATED = "Deactivated"


class TimeInForce(Enum):
    """Время действия ордера"""
    GTC = "GTC"  # Good Till Cancel
    IOC = "IOC"  # Immediate Or Cancel
    FOK = "FOK"  # Fill Or Kill
    POST_ONLY = "PostOnly"


class PositionMode(Enum):
    """Режимы позиций"""
    ONE_WAY = 0
    HEDGE_LONG = 1
    HEDGE_SHORT = 2


class EventType(Enum):
    """Типы событий в системе"""
    # Рыночные события
    PRICE_UPDATE = "price_update"
    NEW_CANDLE = "new_candle"

    # Торговые события
    ORDER_FILLED = "order_filled"
    ORDER_UPDATE = "order_update"

    # События позиций
    POSITION_UPDATE = "position_update"
    POSITION_CLOSED = "position_closed"

    # События стратегий
    STRATEGY_STARTED = "strategy_started"
    STRATEGY_STOPPED = "strategy_stopped"
    STRATEGY_RESTART_REQUESTED = "strategy_restart_requested"
    SIGNAL = "signal"

    # События риск-менеджмента
    RISK_LIMIT_EXCEEDED = "risk_limit_exceeded"
    DRAWDOWN_WARNING = "drawdown_warning"

    # Системные события
    USER_SETTINGS_CHANGED = "user_settings_changed"
    USER_SESSION_START_REQUESTED = "user_session_start_requested"
    USER_SESSION_STOP_REQUESTED = "user_session_stop_requested"
    USER_SESSION_STARTED = "user_session_started"
    USER_SESSION_STOPPED = "user_session_stopped"


class StrategyStatus(Enum):
    """Статусы стратегий"""
    INITIALIZING = "initializing"
    RUNNING = "running"
    PAUSED = "paused"
    STOPPING = "stopping"
    STOPPED = "stopped"
    ERROR = "error"


class SessionStatus(Enum):
    """Статусы пользовательских сессий"""
    STARTING = "starting"
    ACTIVE = "active"
    PAUSED = "paused"
    STOPPING = "stopping"
    STOPPED = "stopped"
    ERROR = "error"


class RiskLevel(Enum):
    """Уровни риска"""
    LOW = "low"
    MEDIUM = "medium"
    HIGH = "high"
    CRITICAL = "critical"


class NotificationType(Enum):
    """Типы уведомлений"""
    INFO = "info"
    WARNING = "warning"
    ERROR = "error"
    SUCCESS = "success"
    TRADE = "trade"
    RISK = "risk"


class TimeFrame(Enum):
    """Таймфреймы для анализа"""
    M1 = "1m"
    M3 = "3m"
    M5 = "5m"
    M15 = "15m"
    M30 = "30m"
    H1 = "1h"
    H2 = "2h"
    H4 = "4h"
    H6 = "6h"
    H12 = "12h"
    D1 = "1d"
    W1 = "1w"


class ConfigType(Enum):
    """Типы конфигураций"""
    GLOBAL = "global_config"

    # Типы для каждой стратегии
    STRATEGY_SIGNAL_SCALPER = "strategy_signal_scalper"

    USER_SETTINGS = "user_settings"


class WebSocketChannel(Enum):
    """Каналы WebSocket подписок"""
    # Публичные каналы
    ORDERBOOK = "orderbook"
    TRADE = "publicTrade"
    TICKER = "tickers"
    KLINE = "kline"
    LIQUIDATION = "liquidation"
    
    # Приватные каналы
    POSITION = "position"
    EXECUTION = "execution"
    ORDER = "order"
    WALLET = "wallet"


class ExchangeType(Enum):
    """Типы бирж"""
    BYBIT = "bybit"
    BINANCE = "binance"
    OKX = "okx"
    BITGET = "bitget"


class AccountType(Enum):
    """Типы аккаунтов"""
    UNIFIED = "UNIFIED"
    CONTRACT = "CONTRACT"
    SPOT = "SPOT"
    OPTION = "OPTION"


class Category(Enum):
    """Категории инструментов"""
    LINEAR = "linear"
    INVERSE = "inverse"
    SPOT = "spot"
    OPTION = "option"


class TriggerBy(Enum):
    """Триггеры для условных ордеров"""
    LAST_PRICE = "LastPrice"
    INDEX_PRICE = "IndexPrice"
    MARK_PRICE = "MarkPrice"


class StopOrderType(Enum):
    """Типы стоп-ордеров"""
    TAKE_PROFIT = "TakeProfit"
    STOP_LOSS = "StopLoss"
    TRAILING_STOP = "TrailingStop"
    PARTIAL_TAKE_PROFIT = "PartialTakeProfit"
    PARTIAL_STOP_LOSS = "PartialStopLoss"


class PositionStatus(Enum):
    """Статусы позиций"""
    NORMAL = "Normal"
    LIQUIDATION = "Liq"
    AUTO_DELEVERAGE = "Adl"


class MarginMode(Enum):
    """Режимы маржи"""
    ISOLATED = "ISOLATED_MARGIN"
    CROSS = "REGULAR_MARGIN"


class TradingStatus(Enum):
    """Статусы торговли"""
    TRADING = "Trading"
    SETTLING = "Settling"
    CLOSED = "Closed"
    SUSPENDED = "Suspended"


# Константы для системы
class SystemConstants:
    """Системные константы"""

    # Кэширование
    CACHE_EXPIRY_SECONDS = 300  # 5 минут
    CONFIG_CACHE_SECONDS = 60   # 1 минута

