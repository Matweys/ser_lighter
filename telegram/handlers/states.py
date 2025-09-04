"""
Профессиональная система состояний FSM для многопользовательского торгового бота
"""
from aiogram.fsm.state import State, StatesGroup
from enum import Enum
from typing import Dict, Any, Optional

class UserStates(StatesGroup):
    """Состояния пользователя для FSM"""
    
    # Основные состояния
    MAIN_MENU = State()
    LOADING = State()
    ERROR = State()
    
    # Состояния настройки профиля
    SETTING_UP_PROFILE = State()
    ENTERING_API_KEY = State()
    ENTERING_SECRET_KEY = State()
    ENTERING_PASSPHRASE = State()
    CONFIRMING_API_KEYS = State()
    
    # Состояния управления торговлей
    STARTING_TRADING = State()
    STOPPING_TRADING = State()
    CONFIRMING_START = State()
    CONFIRMING_STOP = State()
    
    # Состояния настроек
    SETTINGS_MENU = State()
    RISK_SETTINGS = State()
    STRATEGY_SETTINGS = State()
    GENERAL_SETTINGS = State()

    # Состояния настройки риск-менеджмента
    SETTING_MAX_DAILY_LOSS_USDT = State()
    SETTING_LEVERAGE = State()
    
    # Состояния настройки стратегий
    CONFIGURING_STRATEGY = State()
    SELECTING_STRATEGY_TYPE = State()
    ENABLING_STRATEGY = State()
    DISABLING_STRATEGY = State()

    # --- СОСТОЯНИЯ ДЛЯ РУЧНОГО ЗАПУСКА ---
    MANUAL_STRATEGY_SELECT_SYMBOL = State()
    MANUAL_STRATEGY_SELECT_TYPE = State()
    MANUAL_STRATEGY_CONFIGURE = State()
    MANUAL_STRATEGY_AWAIT_VALUE = State()

    # ---СОСТОЯНИЕ РЕДАКТИРОВАНИЯ НАСТРОЕК СТРАТЕГИЙ ---
    EDITING_STRATEGY_PARAMETER = State()
    
    # Состояния для Bidirectional Grid Strategy
    BIDIRECTIONAL_GRID_LEVELS = State()
    BIDIRECTIONAL_SPACING_PERCENT = State()
    BIDIRECTIONAL_ORDER_SIZE = State()
    BIDIRECTIONAL_REBALANCE_THRESHOLD = State()
    BIDIRECTIONAL_MAX_POSITION_SIZE = State()
    BIDIRECTIONAL_GRID_BOUNDS = State()
    
    # Состояния для Grid Scalping Strategy
    GRID_SCALPING_ORDER_TIMEOUT = State()
    GRID_SCALPING_MIN_SPREAD = State()
    GRID_SCALPING_ORDER_SIZE = State()
    GRID_SCALPING_MAX_ORDERS = State()
    GRID_SCALPING_PROFIT_TARGET = State()
    GRID_SCALPING_LIQUIDITY_CHECK = State()
    
    # Состояния для Impulse Trailing Strategy
    IMPULSE_MIN_SIGNAL_STRENGTH = State()
    IMPULSE_TRAILING_PERCENT = State()
    IMPULSE_POSITION_SIZE = State()
    IMPULSE_MAX_POSITION_TIME = State()
    IMPULSE_PARTIAL_CLOSE_PERCENT = State()
    IMPULSE_TREND_CONFIRMATION = State()

    # Состояния статистики и мониторинга
    VIEWING_STATISTICS = State()
    VIEWING_TRADES_HISTORY = State()
    VIEWING_POSITIONS = State()
    VIEWING_ORDERS = State()
    EXPORTING_DATA = State()
    
    # Состояния подтверждения критических действий
    CONFIRMING_RESET_SETTINGS = State()
    CONFIRMING_DELETE_ACCOUNT = State()
    CONFIRMING_EMERGENCY_STOP = State()
    CONFIRMING_CLEAR_HISTORY = State()

class AdminStates(StatesGroup):
    """Состояния администратора"""
    
    # Основные административные состояния
    ADMIN_MENU = State()
    SYSTEM_MONITORING = State()
    USER_MANAGEMENT = State()
    
    # Управление пользователями
    VIEWING_USERS_LIST = State()
    SELECTING_USER = State()
    ENTERING_USER_ID = State()
    ADDING_USER = State()
    REMOVING_USER = State()
    EDITING_USER = State()
    
    # Управление доступом
    SETTING_USER_ACCESS = State()
    SETTING_PREMIUM_STATUS = State()
    SETTING_SUBSCRIPTION_DAYS = State()
    ENTERING_SUBSCRIPTION_PERIOD = State()
    
    # Управление API ключами пользователей
    MANAGING_USER_API_KEYS = State()
    VIEWING_USER_API_KEYS = State()
    ADDING_USER_API_KEYS = State()
    REMOVING_USER_API_KEYS = State()
    ENTERING_USER_API_KEY = State()
    ENTERING_USER_SECRET_KEY = State()
    
    # Системная статистика
    VIEWING_SYSTEM_STATS = State()
    VIEWING_TRADING_STATS = State()
    VIEWING_ERROR_LOGS = State()
    VIEWING_PERFORMANCE_METRICS = State()
    EXPORTING_SYSTEM_DATA = State()
    
    # Управление стратегиями
    STRATEGY_MANAGEMENT = State()
    ENABLING_STRATEGY_GLOBALLY = State()
    DISABLING_STRATEGY_GLOBALLY = State()
    SETTING_GLOBAL_LIMITS = State()
    
    # Рассылка и уведомления
    BROADCAST_MENU = State()
    BROADCAST_MESSAGE = State()
    BROADCAST_TO_ALL = State()
    BROADCAST_TO_PREMIUM = State()
    BROADCAST_TO_ACTIVE = State()
    ENTERING_BROADCAST_TEXT = State()
    CONFIRMING_BROADCAST = State()
    
    # Управление системой
    SYSTEM_CONTROL = State()
    RESTARTING_SYSTEM = State()
    UPDATING_CONFIGS = State()
    BACKUP_MANAGEMENT = State()
    MAINTENANCE_MODE = State()
    
    # Мониторинг и алерты
    SETTING_SYSTEM_ALERTS = State()
    CONFIGURING_MONITORING = State()
    VIEWING_ACTIVE_SESSIONS = State()
    MANAGING_RATE_LIMITS = State()
    
    # Финансовая отчетность
    FINANCIAL_REPORTS = State()
    GENERATING_PROFIT_REPORT = State()
    GENERATING_LOSS_REPORT = State()
    GENERATING_COMMISSION_REPORT = State()
    SETTING_REPORT_PERIOD = State()

class SystemStates(StatesGroup):
    """Системные состояния для внутренних процессов"""
    
    # Состояния инициализации
    INITIALIZING = State()
    LOADING_CONFIGS = State()
    CONNECTING_DATABASE = State()
    CONNECTING_REDIS = State()
    CONNECTING_EXCHANGES = State()
    
    # Состояния работы
    RUNNING = State()
    MONITORING = State()
    PROCESSING_EVENTS = State()
    EXECUTING_STRATEGIES = State()
    
    # Состояния обслуживания
    MAINTENANCE = State()
    UPDATING = State()
    BACKING_UP = State()
    CLEANING_UP = State()
    
    # Состояния ошибок
    ERROR_RECOVERY = State()
    RECONNECTING = State()
    FAILOVER = State()
    
    # Состояния остановки
    SHUTTING_DOWN = State()
    SAVING_STATE = State()
    CLOSING_CONNECTIONS = State()
    STOPPED = State()

class StateManager:
    """Менеджер состояний для управления переходами"""
    
    def __init__(self):
        self.state_transitions: Dict[str, Dict[str, str]] = {
            # Переходы пользовательских состояний
            'user': {
                UserStates.MAIN_MENU.state: [
                    UserStates.SETTINGS_MENU.state,
                    UserStates.STARTING_TRADING.state,
                    UserStates.VIEWING_STATISTICS.state,
                ],
                UserStates.SETTINGS_MENU.state: [
                    UserStates.RISK_SETTINGS.state,
                    UserStates.STRATEGY_SETTINGS.state,
                    UserStates.GENERAL_SETTINGS.state,
                    UserStates.MAIN_MENU.state
                ],
                UserStates.RISK_SETTINGS.state: [
                    UserStates.SETTING_MAX_DAILY_LOSS_USDT.state,
                    UserStates.SETTINGS_MENU.state
                ],
                UserStates.STRATEGY_SETTINGS.state: [
                    UserStates.CONFIGURING_STRATEGY.state,
                    UserStates.SELECTING_STRATEGY_TYPE.state,
                    UserStates.SETTINGS_MENU.state
                ]
            },
            
            # Переходы административных состояний
            'admin': {
                AdminStates.ADMIN_MENU.state: [
                    AdminStates.USER_MANAGEMENT.state,
                    AdminStates.SYSTEM_MONITORING.state,
                    AdminStates.BROADCAST_MENU.state,
                    AdminStates.SYSTEM_CONTROL.state
                ],
                AdminStates.USER_MANAGEMENT.state: [
                    AdminStates.VIEWING_USERS_LIST.state,
                    AdminStates.ADDING_USER.state,
                    AdminStates.MANAGING_USER_API_KEYS.state,
                    AdminStates.ADMIN_MENU.state
                ]
            },
            
            # Переходы системных состояний
            'system': {
                SystemStates.INITIALIZING.state: [
                    SystemStates.LOADING_CONFIGS.state,
                    SystemStates.CONNECTING_DATABASE.state,
                    SystemStates.RUNNING.state,
                    SystemStates.ERROR_RECOVERY.state
                ],
                SystemStates.RUNNING.state: [
                    SystemStates.MONITORING.state,
                    SystemStates.PROCESSING_EVENTS.state,
                    SystemStates.MAINTENANCE.state,
                    SystemStates.SHUTTING_DOWN.state
                ]
            }
        }
    
    def is_valid_transition(self, state_type: str, from_state: str, to_state: str) -> bool:
        """Проверка валидности перехода между состояниями"""
        if state_type not in self.state_transitions:
            return False
        
        if from_state not in self.state_transitions[state_type]:
            return False
        
        return to_state in self.state_transitions[state_type][from_state]
    
    def get_available_transitions(self, state_type: str, current_state: str) -> list:
        """Получение доступных переходов из текущего состояния"""
        if state_type not in self.state_transitions:
            return []
        
        if current_state not in self.state_transitions[state_type]:
            return []
        
        return self.state_transitions[state_type][current_state]

class StateValidator:
    """Валидатор состояний для проверки корректности данных"""
    
    @staticmethod
    def validate_risk_per_trade(value: str) -> tuple[bool, Optional[float]]:
        """Валидация риска на сделку (0.1-10%)"""
        try:
            risk = float(value)
            if 0.1 <= risk <= 10.0:
                return True, risk
            return False, None
        except ValueError:
            return False, None
    
    @staticmethod
    def validate_max_daily_drawdown(value: str) -> tuple[bool, Optional[float]]:
        """Валидация максимальной дневной просадки (1-50%)"""
        try:
            drawdown = float(value)
            if 1.0 <= drawdown <= 50.0:
                return True, drawdown
            return False, None
        except ValueError:
            return False, None
    
    @staticmethod
    def validate_max_concurrent_trades(value: str) -> tuple[bool, Optional[int]]:
        """Валидация максимального количества одновременных сделок (1-20)"""
        try:
            trades = int(value)
            if 1 <= trades <= 20:
                return True, trades
            return False, None
        except ValueError:
            return False, None
    
    @staticmethod
    def validate_min_balance(value: str) -> tuple[bool, Optional[float]]:
        """Валидация минимального баланса (10-10000 USDT)"""
        try:
            balance = float(value)
            if 10.0 <= balance <= 10000.0:
                return True, balance
            return False, None
        except ValueError:
            return False, None
    
    @staticmethod
    def validate_symbol(symbol: str) -> tuple[bool, Optional[str]]:
        """Валидация торгового символа"""
        if not symbol:
            return False, None
        
        symbol = symbol.upper().strip()
        
        # Проверяем формат символа (например, BTCUSDT)
        if len(symbol) < 6 or len(symbol) > 12:
            return False, None
        
        # Проверяем, что символ заканчивается на USDT
        if not symbol.endswith('USDT'):
            return False, None
        
        return True, symbol
    
    @staticmethod
    def validate_api_key(api_key: str) -> tuple[bool, Optional[str]]:
        """Валидация API ключа"""
        if not api_key or len(api_key) < 20:
            return False, None
        
        # Удаляем пробелы
        api_key = api_key.strip()
        
        return True, api_key
    
    @staticmethod
    def validate_secret_key(secret_key: str) -> tuple[bool, Optional[str]]:
        """Валидация секретного ключа"""
        if not secret_key or len(secret_key) < 20:
            return False, None
        
        # Удаляем пробелы
        secret_key = secret_key.strip()
        
        return True, secret_key

# Глобальные экземпляры
state_manager = StateManager()
state_validator = StateValidator()

# Константы состояний
class StateConstants:
    """Константы для работы с состояниями"""
    
    # Таймауты состояний (в секундах)
    DEFAULT_STATE_TIMEOUT = 300  # 5 минут
    CRITICAL_ACTION_TIMEOUT = 60  # 1 минута
    API_INPUT_TIMEOUT = 600  # 10 минут
    
    # Максимальные попытки ввода
    MAX_INPUT_ATTEMPTS = 3
    MAX_VALIDATION_ATTEMPTS = 5
    
    # Сообщения об ошибках
    INVALID_INPUT_MESSAGE = "❌ Неверный формат данных. Попробуйте еще раз."
    TIMEOUT_MESSAGE = "⏰ Время ожидания истекло. Возвращаемся в главное меню."
    MAX_ATTEMPTS_MESSAGE = "❌ Превышено максимальное количество попыток. Возвращаемся в главное меню."
    
    # Сообщения подтверждения
    CONFIRM_START_TRADING = "⚠️ Вы уверены, что хотите запустить торговлю?"
    CONFIRM_STOP_TRADING = "⚠️ Вы уверены, что хотите остановить торговлю?"
    CONFIRM_RESET_SETTINGS = "⚠️ Это действие сбросит все настройки к значениям по умолчанию. Продолжить?"
    CONFIRM_DELETE_ACCOUNT = "⚠️ Это действие удалит все данные аккаунта. Это действие необратимо!"

