# core/enums.py
"""
Перечисления для многопользовательской торговой системы
Содержит все типы данных, состояния и константы системы
"""
from enum import Enum, IntEnum


class StrategyType(Enum):
    """Типы торговых стратегий"""
    GRID_SCALPING = "grid_scalping"
    BIDIRECTIONAL_GRID = "bidirectional_grid"
    IMPULSE_TRAILING = "impulse_trailing"


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


class MarketCondition(Enum):
    """Рыночные условия для анализа"""
    STRONG_TREND = "STRONG_TREND"
    TREND = "TREND"
    WEAK_TREND = "WEAK_TREND"
    STRONG_FLAT = "STRONG_FLAT"
    FLAT = "FLAT"
    UNCERTAIN = "UNCERTAIN"


class TrendDirection(Enum):
    """Направления тренда"""
    BULLISH = "BULLISH"
    BEARISH = "BEARISH"
    SIDEWAYS = "SIDEWAYS"
    NEUTRAL = "NEUTRAL"


class SignalDirection(Enum):
    """Направления торговых сигналов"""
    LONG = "LONG"
    SHORT = "SHORT"
    CLOSE_LONG = "CLOSE_LONG"
    CLOSE_SHORT = "CLOSE_SHORT"
    HOLD = "HOLD"


class EventType(Enum):
    """Типы событий в системе"""
    # Рыночные события
    PRICE_UPDATE = "price_update"
    VOLUME_UPDATE = "volume_update"
    TICKER_UPDATE = "ticker_update"
    
    # Торговые события
    ORDER_PLACED = "order_placed"
    ORDER_FILLED = "order_filled"
    ORDER_CANCELLED = "order_cancelled"
    ORDER_REJECTED = "order_rejected"
    
    # События позиций
    POSITION_OPENED = "position_opened"
    POSITION_UPDATED = "position_updated"
    POSITION_CLOSED = "position_closed"
    
    # События стратегий
    STRATEGY_STARTED = "strategy_started"
    STRATEGY_STOPPED = "strategy_stopped"
    STRATEGY_ERROR = "strategy_error"
    SIGNAL_GENERATED = "signal_generated"
    
    # События риск-менеджмента
    RISK_LIMIT_EXCEEDED = "risk_limit_exceeded"
    DRAWDOWN_WARNING = "drawdown_warning"
    EMERGENCY_STOP = "emergency_stop"
    
    # Системные события
    USER_SETTINGS_CHANGED = "user_settings_changed"
    SESSION_STARTED = "session_started"
    SESSION_STOPPED = "session_stopped"
    WEBSOCKET_CONNECTED = "websocket_connected"
    WEBSOCKET_DISCONNECTED = "websocket_disconnected"
    
    # События анализа
    MARKET_ANALYSIS_COMPLETED = "market_analysis_completed"
    TREND_CHANGED = "trend_changed"
    VOLATILITY_ALERT = "volatility_alert"


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


class IndicatorType(Enum):
    """Типы технических индикаторов"""
    EMA = "ema"
    SMA = "sma"
    RSI = "rsi"
    MACD = "macd"
    BOLLINGER = "bollinger"
    ADX = "adx"
    ATR = "atr"
    STOCHASTIC = "stochastic"
    WILLIAMS_R = "williams_r"
    CCI = "cci"


class OrderPriority(IntEnum):
    """Приоритеты ордеров"""
    LOW = 1
    NORMAL = 2
    HIGH = 3
    URGENT = 4
    EMERGENCY = 5


class ConfigType(Enum):
    """Типы конфигураций"""
    GLOBAL = "global_config"

    # Типы для каждой стратегии
    STRATEGY_BIDIRECTIONAL_GRID = "strategy_bidirectional_grid"
    STRATEGY_GRID_SCALPING = "strategy_grid_scalping"
    STRATEGY_IMPULSE_TRAILING = "strategy_impulse_trailing"

    # Типы для каждого компонента
    COMPONENT_META_STRATEGIST = "component_meta_strategist"
    COMPONENT_RISK_MANAGER = "component_risk_manager"

    USER_SETTINGS = "user_settings"


class LogLevel(Enum):
    """Уровни логирования"""
    DEBUG = "debug"
    INFO = "info"
    WARNING = "warning"
    ERROR = "error"
    CRITICAL = "critical"


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
    
    # Точность вычислений
    DECIMAL_PRECISION = 28
    
    # Таймауты (в секундах)
    API_TIMEOUT = 30
    WEBSOCKET_TIMEOUT = 60
    ORDER_TIMEOUT = 300
    POSITION_TIMEOUT = 3600
    
    # Лимиты
    MAX_RETRIES = 3
    MAX_ORDERS_PER_SYMBOL = 100
    MAX_POSITIONS_PER_USER = 50
    MAX_STRATEGIES_PER_USER = 10
    
    # Rate limits
    API_RATE_LIMIT = 0.1  # 100ms между запросами
    WEBSOCKET_RATE_LIMIT = 0.05  # 50ms между сообщениями
    
    # Кэширование
    CACHE_EXPIRY_SECONDS = 300  # 5 минут
    CONFIG_CACHE_SECONDS = 60   # 1 минута
    
    # Минимальные значения
    MIN_ORDER_SIZE_USDT = 5.0
    MIN_BALANCE_USDT = 10.0
    MIN_SIGNAL_STRENGTH = 50
    
    # Максимальные значения
    MAX_LEVERAGE = 100
    MAX_POSITION_SIZE_PERCENT = 50
    MAX_DAILY_DRAWDOWN_PERCENT = 20
    
    # Форматирование
    PRICE_DECIMAL_PLACES = 8
    QUANTITY_DECIMAL_PLACES = 6
    PERCENTAGE_DECIMAL_PLACES = 4

