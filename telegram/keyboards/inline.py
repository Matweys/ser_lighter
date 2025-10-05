# telegram/keyboards/inline.py

"""
Профессиональная и актуальная система inline клавиатур для торгового бота.
Содержит только используемые в проекте клавиатуры.
"""
from aiogram.types import InlineKeyboardMarkup, InlineKeyboardButton
from typing import List, Dict, Any, Set
from core.enums import StrategyType

class KeyboardBuilder:
    """Профессиональный конструктор клавиатур"""
    @staticmethod
    def build_keyboard(buttons: List[List[Dict[str, str]]]) -> InlineKeyboardMarkup:
        keyboard = [[InlineKeyboardButton(text=btn['text'], callback_data=btn['callback_data']) for btn in row] for row in buttons]
        return InlineKeyboardMarkup(inline_keyboard=keyboard)

# --- ОСНОВНЫЕ МЕНЮ ---

def get_main_menu_keyboard() -> InlineKeyboardMarkup:
    """Главное меню бота."""
    buttons = [
        [{"text": "📊 Статистика", "callback_data": "statistics"}, {"text": "💰 Баланс", "callback_data": "show_balance"}],
        [{"text": "📈 Позиции", "callback_data": "show_positions"}, {"text": "📋 Ордера", "callback_data": "show_orders"}],
        [{"text": "📊 Статус", "callback_data": "show_status"}, {"text": "⚙️ Настройки", "callback_data": "settings"}],
    ]
    return KeyboardBuilder.build_keyboard(buttons)

def get_settings_keyboard() -> InlineKeyboardMarkup:
    """Главное меню настроек."""
    buttons = [
        [{"text": "🛡️ Риск-менеджмент", "callback_data": "risk_settings"}, {"text": "📊 Стратегии", "callback_data": "strategy_settings"}],
        [{"text": "🔑 API ключи", "callback_data": "api_keys"}, {"text": "📈 Торговые пары", "callback_data": "select_trading_pairs"}],
        [{"text": "🔄 Сбросить настройки", "callback_data": "reset_settings"}],
        [{"text": "🏠 Главное меню", "callback_data": "main_menu"}]
    ]
    return KeyboardBuilder.build_keyboard(buttons)

# --- МЕНЮ НАСТРОЕК ---

def get_risk_settings_keyboard() -> InlineKeyboardMarkup:
    """Клавиатура настроек риск-менеджмента."""
    buttons = [
        [{"text": "💰 Макс. дневной убыток (USDT)", "callback_data": "set_max_daily_loss_usdt"}],
        [{"text": "⚙️ Назад в Настройки", "callback_data": "settings"}]
    ]
    return KeyboardBuilder.build_keyboard(buttons)

def get_strategy_settings_keyboard(configs: Dict[str, Dict[str, Any]]) -> InlineKeyboardMarkup:
    """Клавиатура выбора стратегии для настройки."""
    def get_status_icon(strategy_type: str) -> str:
        return "✅" if configs.get(strategy_type, {}).get("is_enabled", False) else "❌"

    buttons = [
        [{"text": f"{get_status_icon('signal_scalper')} Сигнальный скальпер", "callback_data": "configure_strategy_signal_scalper"}],
        [{"text": f"{get_status_icon('impulse_trailing')} Асимметричный Импульс", "callback_data": "configure_strategy_impulse_trailing"}],
        [{"text": f"{get_status_icon('flash_drop_catcher')} 🚀 Flash Drop Catcher", "callback_data": "configure_strategy_flash_drop_catcher"}],
        [{"text": "⚙️ Назад в Настройки", "callback_data": "settings"}]
    ]
    return KeyboardBuilder.build_keyboard(buttons)

def get_strategy_config_keyboard(strategy_type: str, config: Dict[str, Any]) -> InlineKeyboardMarkup:
    """
    Динамическая клавиатура настройки конкретной стратегии.
    Отображает актуальные параметры для каждой стратегии.
    """
    buttons = []
    editable_params = {}

    if strategy_type == StrategyType.SIGNAL_SCALPER.value:
        editable_params = {
            "leverage": f"Кредитное плечо: x{config.get('leverage', 2)}",
            "order_amount": f"Сумма ордера: {config.get('order_amount', 50)} USDT",
            "max_loss_usd": f"Стоп-лосс: {config.get('max_loss_usd', 15.0)} USDT",
        }
    elif strategy_type == StrategyType.IMPULSE_TRAILING.value:
        editable_params = {
            "leverage": f"🎚️ Плечо: x{config.get('leverage', 2)}",
            "order_amount": f"💰 Сумма ордера: {config.get('order_amount', 50)} USDT",
            "initial_sl_usdt": f"🛡️ Начальный SL: {config.get('initial_sl_usdt', 20.91)} USDT",
            "min_profit_activation_usdt": f"🎯 Активация трейлинга: +{config.get('min_profit_activation_usdt', 5.0)} USDT",
            "trailing_distance_usdt": f"📏 Расстояние трейлинга: {config.get('trailing_distance_usdt', 11.77)} USDT",
            "pullback_close_usdt": f"📉 Откат для закрытия: {config.get('pullback_close_usdt', 5.87)} USDT",
        }
    elif strategy_type == StrategyType.FLASH_DROP_CATCHER.value:
        editable_params = {
            "leverage": f"🎚️ Плечо: x{config.get('leverage', 10)}",
            "order_amount": f"💰 Сумма ордера: {config.get('order_amount', 50)} USDT",
            "drop_percent": f"📉 Процент падения: {config.get('drop_percent', 2.0)}%",
            "hard_stop_loss_usdt": f"🛑 Hard Stop Loss: {config.get('hard_stop_loss_usdt', -15.0)} USDT",
        }

    for key, text in editable_params.items():
        buttons.append([{"text": text, "callback_data": f"set_param_{strategy_type}_{key}"}])

    is_enabled = config.get("is_enabled", False)
    toggle_button_text = "❌ Отключить для автоторговли" if is_enabled else "✅ Включить для автоторговли"
    buttons.append([{"text": toggle_button_text, "callback_data": f"toggle_strategy_{strategy_type}"}])

    buttons.append([{"text": "📊 Назад к стратегиям", "callback_data": "strategy_settings"}])
    return KeyboardBuilder.build_keyboard(buttons)

def get_symbol_selection_keyboard(available_symbols: List[str], selected_symbols: Set[str]) -> InlineKeyboardMarkup:
    """Создает динамическую клавиатуру для выбора торговых пар."""
    buttons = []
    row = []
    buttons_in_row = 3

    for symbol in available_symbols:
        text = f"✅ {symbol}" if symbol in selected_symbols else symbol
        row.append({"text": text, "callback_data": f"toggle_symbol_{symbol}"})
        if len(row) == buttons_in_row:
            buttons.append(row)
            row = []
    if row:
        buttons.append(row)

    buttons.append([{"text": "💾 Сохранить и вернуться", "callback_data": "save_symbol_selection"}])
    return KeyboardBuilder.build_keyboard(buttons)

# --- НАВИГАЦИОННЫЕ И ВСПОМОГАТЕЛЬНЫЕ КЛАВИАТУРЫ ---

def get_confirmation_keyboard(action: str, additional_data: str = "") -> InlineKeyboardMarkup:
    """Клавиатура подтверждения действий."""
    buttons = [[
        {"text": "✅ Подтвердить", "callback_data": f"confirm_{action}_{additional_data}".rstrip("_")},
        {"text": "❌ Отменить", "callback_data": "cancel"}
    ]]
    return KeyboardBuilder.build_keyboard(buttons)

def get_back_keyboard(back_to: str = "main_menu") -> InlineKeyboardMarkup:
    """Простая клавиатура с кнопкой 'Назад'."""
    return KeyboardBuilder.build_keyboard([[{"text": "🔙 Назад", "callback_data": back_to}]])

def get_cancel_keyboard() -> InlineKeyboardMarkup:
    """Клавиатура отмены текущего действия (возврат в главное меню)."""
    return KeyboardBuilder.build_keyboard([[{"text": "❌ Отменить", "callback_data": "main_menu"}]])

# Не давно добавленные
def get_help_keyboard() -> InlineKeyboardMarkup:
    """Клавиатура помощи."""
    buttons = [
        [{"text": "📖 Руководство пользователя", "callback_data": "user_guide"}],
        [{"text": "❓ FAQ", "callback_data": "faq"}],
        [{"text": "📞 Поддержка", "callback_data": "support"}],
        [{"text": "🏠 Главное меню", "callback_data": "main_menu"}]
    ]
    return KeyboardBuilder.build_keyboard(buttons)

def get_quick_actions_keyboard(session_running: bool = False) -> InlineKeyboardMarkup:
    """Клавиатура быстрых действий."""
    if session_running:
        buttons = [
            [{"text": "📊 Статус позиций", "callback_data": "show_positions"}],
            [{"text": "💰 Баланс", "callback_data": "show_balance"}],
            [{"text": "🏠 Главное меню", "callback_data": "main_menu"}]
        ]
    else:
        buttons = [
            [{"text": "⚙️ Настройки", "callback_data": "settings"}],
            [{"text": "📊 Статистика", "callback_data": "statistics"}],
            [{"text": "🏠 Главное меню", "callback_data": "main_menu"}]
        ]
    return KeyboardBuilder.build_keyboard(buttons)