from aiogram.fsm.state import State, StatesGroup

class UserStates(StatesGroup):
    """Основные состояния FSM для навигации пользователя."""

    # Главное меню и навигация
    MAIN_MENU = State()
    SETTINGS_MENU = State()

    # Настройки
    RISK_SETTINGS = State()
    STRATEGY_SETTINGS = State()

    # Состояния ожидания ввода от пользователя
    SETTING_MAX_DAILY_LOSS_USDT = State()
    AWAITING_STRATEGY_PARAM_VALUE = State()

    # Состояния для ввода API ключей
    AWAITING_API_KEY = State()
    AWAITING_API_SECRET = State()