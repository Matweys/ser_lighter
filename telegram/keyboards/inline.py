"""
Профессиональная система inline клавиатур для многопользовательского торгового бота
"""
from aiogram.types import InlineKeyboardMarkup, InlineKeyboardButton
from typing import List, Optional, Dict, Any
from core.enums import StrategyType, PositionSide, NotificationType
from typing import List, Set


class KeyboardBuilder:
    """Профессиональный конструктор клавиатур"""
    
    @staticmethod
    def build_keyboard(buttons: List[List[Dict[str, str]]]) -> InlineKeyboardMarkup:
        """Универсальный конструктор клавиатур"""
        keyboard = []
        
        for row in buttons:
            keyboard_row = []
            for button in row:
                keyboard_row.append(
                    InlineKeyboardButton(
                        text=button['text'],
                        callback_data=button['callback_data']
                    )
                )
            keyboard.append(keyboard_row)
        
        return InlineKeyboardMarkup(inline_keyboard=keyboard)

# Главные меню
def get_main_menu_keyboard() -> InlineKeyboardMarkup:
    """Главное меню бота"""
    buttons = [
        [
            {"text": "📊 Статистика", "callback_data": "statistics"},
            {"text": "💰 Баланс", "callback_data": "show_balance"}
        ],
        [
            {"text": "📈 Позиции", "callback_data": "show_positions"},
            {"text": "📋 Ордера", "callback_data": "show_orders"}
        ],
        [
            {"text": "📊 Статус", "callback_data": "show_status"},
            {"text": "⚙️ Настройки", "callback_data": "settings"}
        ],
        [
            {"text": "ℹ️ Помощь", "callback_data": "help"}
        ]
    ]
    return KeyboardBuilder.build_keyboard(buttons)


def get_symbol_selection_keyboard(available_symbols: List[str], selected_symbols: Set[str]) -> InlineKeyboardMarkup:
    """
    Создает динамическую клавиатуру для выбора торговых пар.
    Отмечает уже выбранные символы галочкой.
    """
    buttons = []
    row = []
    # Рекомендуется не более 3 кнопок в ряду для читаемости
    buttons_in_row = 3

    for symbol in available_symbols:
        text = f"✅ {symbol}" if symbol in selected_symbols else symbol
        callback_data = f"toggle_symbol_{symbol}"
        row.append({"text": text, "callback_data": callback_data})

        if len(row) == buttons_in_row:
            buttons.append(row)
            row = []

    if row:  # Добавить оставшиеся кнопки, если их количество не кратно buttons_in_row
        buttons.append(row)

    # Добавляем кнопку для сохранения
    buttons.append([{"text": "💾 Сохранить и вернуться", "callback_data": "save_symbol_selection"}])

    return KeyboardBuilder.build_keyboard(buttons)


def get_welcome_keyboard(is_trading_active: bool = False) -> InlineKeyboardMarkup:
    """Приветственная клавиатура для новых пользователей"""
    if is_trading_active:
        # Если торговля уже идет, предлагаем перейти в главное меню
        primary_action_button = {"text": "🏠 Главное меню", "callback_data": "main_menu"}
    else:
        # Если нет, предлагаем начать
        primary_action_button = {"text": "🚀 Начать торговлю", "callback_data": "start_trading"}

    buttons = [
        [
            primary_action_button,
            {"text": "⚙️ Настройки", "callback_data": "settings"}
        ],
        [
            {"text": "📚 Обучение", "callback_data": "tutorial"},
            {"text": "ℹ️ О боте", "callback_data": "about"}
        ]
    ]

    return KeyboardBuilder.build_keyboard(buttons)

def get_manual_trade_symbol_keyboard(symbols: List[str]) -> InlineKeyboardMarkup:
    """Клавиатура выбора символа для ручной торговли."""
    buttons = []
    row = []
    for symbol in symbols:
        row.append({"text": symbol, "callback_data": f"manual_symbol_{symbol}"})
        if len(row) == 2:
            buttons.append(row)
            row = []
    if row:
        buttons.append(row)

    buttons.append([{"text": "❌ Отменить", "callback_data": "cancel"}])
    return KeyboardBuilder.build_keyboard(buttons)


def get_help_keyboard() -> InlineKeyboardMarkup:
    """Клавиатура справки"""
    buttons = [
        [
            {"text": "📖 Руководство", "callback_data": "user_guide"},
            {"text": "🎯 Стратегии", "callback_data": "strategies_help"}
        ],
        [
            {"text": "🛡️ Риск-менеджмент", "callback_data": "risk_help"},
            {"text": "🔧 Настройки", "callback_data": "settings_help"}
        ],
        [
            {"text": "❓ FAQ", "callback_data": "faq"},
            {"text": "📞 Поддержка", "callback_data": "support"}
        ],
        [
            {"text": "🏠 Главное меню", "callback_data": "main_menu"}
        ]
    ]
    
    return KeyboardBuilder.build_keyboard(buttons)

def get_quick_actions_keyboard(is_trading_active: bool = False) -> InlineKeyboardMarkup:
    """Клавиатура быстрых действий"""
    if is_trading_active:
        buttons = [
            [
                {"text": "🛑 Остановить", "callback_data": "stop_trading"},
                {"text": "🚨 Экстренная остановка", "callback_data": "emergency_stop"}
            ],
            [
                {"text": "📊 Статус", "callback_data": "show_status"},
                {"text": "📈 Позиции", "callback_data": "show_positions"}
            ]
        ]
    else:
        buttons = [
            [
                {"text": "🚀 Запустить", "callback_data": "start_trading"},
                {"text": "⚙️ Настройки", "callback_data": "settings"}
            ],
            [
                {"text": "📊 Статистика", "callback_data": "statistics"},
                {"text": "💰 Баланс", "callback_data": "show_balance"}
            ]
        ]
    
    return KeyboardBuilder.build_keyboard(buttons)

# Настройки
def get_settings_keyboard() -> InlineKeyboardMarkup:
    """Главное меню настроек"""
    buttons = [
        [
            {"text": "🛡️ Риск-менеджмент", "callback_data": "risk_settings"},
            {"text": "📊 Стратегии", "callback_data": "strategy_settings"}
        ],
        [
            {"text": "🔑 API ключи", "callback_data": "api_keys"},
            {"text": "📈 Торговые пары", "callback_data": "select_trading_pairs"}
        ],
        [
            {"text": "🌐 Общие", "callback_data": "general_settings"},
            {"text": "🔄 Сбросить настройки", "callback_data": "reset_settings"}
        ],
        [
            {"text": "🏠 Главное меню", "callback_data": "main_menu"}
        ]
    ]

    return KeyboardBuilder.build_keyboard(buttons)


def get_risk_settings_keyboard() -> InlineKeyboardMarkup:
    """Клавиатура настроек риск-менеджмента"""
    buttons = [
        [
            {"text": "💰 Максимальная сумма убытка", "callback_data": "set_max_daily_loss_usdt"}
        ],
        [
            {"text": "⚖️ Кредитное плечо", "callback_data": "set_leverage"}
        ],
        [
            {"text": "⚙️ Назад в Настройки", "callback_data": "settings"}
        ]
    ]

    return KeyboardBuilder.build_keyboard(buttons)


def get_strategy_settings_keyboard(configs: Dict[str, Dict[str, Any]]) -> InlineKeyboardMarkup:
    """Клавиатура настроек стратегий с отображением статуса (вкл/выкл)."""

    def get_status_icon(strategy_type: str) -> str:
        # is_enabled - наш новый флаг в конфиге стратегии
        return "✅" if configs.get(strategy_type, {}).get("is_enabled", False) else "❌"

    buttons = [
        [
            {"text": f"{get_status_icon('bidirectional_grid')} Двунаправленная сетка",
             "callback_data": "configure_strategy_bidirectional_grid"},
            {"text": f"{get_status_icon('grid_scalping')} Сеточный скальпинг",
             "callback_data": "configure_strategy_grid_scalping"}
        ],
        [
            {"text": f"{get_status_icon('impulse_trailing')} Импульсный трейлинг",
             "callback_data": "configure_strategy_impulse_trailing"}
        ],
        [
            {"text": "⚙️ Назад в Настройки", "callback_data": "settings"},
            {"text": "🏠 Главное меню", "callback_data": "main_menu"}
        ]
    ]

    return KeyboardBuilder.build_keyboard(buttons)


# telegram/keyboards/inline.py

def get_strategy_config_keyboard(strategy_type: str, config: Dict[str, Any]) -> InlineKeyboardMarkup:
    """Динамическая клавиатура настройки конкретной стратегии."""
    buttons = []

    # Определяем параметры, которые пользователь может редактировать
    editable_params = {}
    if strategy_type == StrategyType.BIDIRECTIONAL_GRID.value:
        editable_params = {
            "order_amount": f"Сумма ордера: {config.get('order_amount', 0)} USDT",
            "grid_levels": f"Уровни (в каждую сторону): {config.get('grid_levels', 0)}",
            "profit_percent": f"Процент прибыли: {config.get('profit_percent', 0)}%",
            "stop_loss_percent": f"Стоп-лосс: {config.get('stop_loss_percent', 0)}%",
        }
    elif strategy_type == StrategyType.GRID_SCALPING.value:
        editable_params = {
            "order_amount": f"Сумма ордера: {config.get('order_amount', 0)} USDT",
            "max_averaging_orders": f"Макс. ордеров усреднения: {config.get('max_averaging_orders', 0)}",
            "profit_percent": f"Процент прибыли: {config.get('profit_percent', 0)}%",
            "stop_loss_percent": f"Стоп-лосс: {config.get('stop_loss_percent', 0)}%",
        }
    elif strategy_type == StrategyType.IMPULSE_TRAILING.value:
        editable_params = {
            "order_amount": f"Сумма ордера: {config.get('order_amount', 0)} USDT",
            "min_signal_strength": f"Мин. сила сигнала: {config.get('min_signal_strength', 0)}",
            "stop_loss_percent": f"Стоп-лосс: {config.get('stop_loss_percent', 0)}%",
            "trailing_percent": f"Трейлинг: {config.get('trailing_percent', 0)}%",
        }

    for key, text in editable_params.items():
        buttons.append([{"text": text, "callback_data": f"set_param_{strategy_type}_{key}"}])

    # Динамическая кнопка включения/отключения
    is_enabled = config.get("is_enabled", False)
    toggle_button_text = "❌ Отключить для автоторговли" if is_enabled else "✅ Включить для автоторговли"
    buttons.append([{"text": toggle_button_text, "callback_data": f"toggle_strategy_{strategy_type}"}])

    # Кнопки управления
    buttons.extend([
        [{"text": "💾 Сохранить и выйти", "callback_data": "save_and_exit_strategy_config"}],
        [{"text": "📊 Назад к стратегиям", "callback_data": "strategy_settings"}]
    ])

    return KeyboardBuilder.build_keyboard(buttons)

# Выбор стратегий
def get_strategy_selection_keyboard() -> InlineKeyboardMarkup:
    """Клавиатура выбора стратегии"""
    buttons = [
        [
            {"text": "🔄 Двунаправленная сетка", "callback_data": "strategy_bidirectional_grid"},
            {"text": "⚡ Сеточный скальпинг", "callback_data": "strategy_grid_scalping"}
        ],
        [
            {"text": "🚀 Импульсный трейлинг", "callback_data": "strategy_impulse_trailing"}
        ],
        [
            {"text": "🔙 Назад", "callback_data": "main_menu"}
        ]
    ]
    
    return KeyboardBuilder.build_keyboard(buttons)


# Подтверждения
def get_confirmation_keyboard(action: str, additional_data: str = "") -> InlineKeyboardMarkup:
    """Клавиатура подтверждения действий"""
    buttons = [
        [
            {"text": "✅ Подтвердить", "callback_data": f"confirm_{action}_{additional_data}".rstrip("_")},
            {"text": "❌ Отменить", "callback_data": "cancel"}
        ]
    ]
    
    return KeyboardBuilder.build_keyboard(buttons)

def get_yes_no_keyboard(action: str) -> InlineKeyboardMarkup:
    """Простая клавиатура Да/Нет"""
    buttons = [
        [
            {"text": "✅ Да", "callback_data": f"yes_{action}"},
            {"text": "❌ Нет", "callback_data": f"no_{action}"}
        ]
    ]
    
    return KeyboardBuilder.build_keyboard(buttons)

# Информационные клавиатуры
def get_balance_keyboard() -> InlineKeyboardMarkup:
    """Клавиатура для баланса"""
    buttons = [
        [
            {"text": "🏠 Главное меню", "callback_data": "main_menu"}
        ]
    ]

    return KeyboardBuilder.build_keyboard(buttons)


def get_positions_keyboard() -> InlineKeyboardMarkup:
    """Клавиатура для позиций"""
    buttons = [
        [
            {"text": "🔄 Обновить", "callback_data": "refresh_positions"},
            {"text": "📊 Детали", "callback_data": "positions_details"}
        ],
        [
            {"text": "🛑 Закрыть все", "callback_data": "close_all_positions"},
            {"text": "📈 PnL", "callback_data": "show_pnl"}
        ],
        [
            {"text": "🏠 Главное меню", "callback_data": "main_menu"}
        ]
    ]
    
    return KeyboardBuilder.build_keyboard(buttons)

def get_orders_keyboard() -> InlineKeyboardMarkup:
    """Клавиатура для ордеров"""
    buttons = [
        [
            {"text": "🔄 Обновить", "callback_data": "refresh_orders"},
            {"text": "📊 Детали", "callback_data": "orders_details"}
        ],
        [
            {"text": "❌ Отменить все", "callback_data": "cancel_all_orders"},
            {"text": "📋 История", "callback_data": "orders_history"}
        ],
        [
            {"text": "🏠 Главное меню", "callback_data": "main_menu"}
        ]
    ]
    
    return KeyboardBuilder.build_keyboard(buttons)

# Административные клавиатуры
def get_admin_keyboard() -> InlineKeyboardMarkup:
    """Административная клавиатура"""
    buttons = [
        [
            {"text": "👥 Пользователи", "callback_data": "admin_users"},
            {"text": "📊 Статистика", "callback_data": "admin_stats"}
        ],
        [
            {"text": "📢 Рассылка", "callback_data": "admin_broadcast"},
            {"text": "⚙️ Система", "callback_data": "admin_system"}
        ],
        [
            {"text": "📋 Логи", "callback_data": "admin_logs"},
            {"text": "🔧 Настройки", "callback_data": "admin_settings"}
        ],
        [
            {"text": "🏠 Главное меню", "callback_data": "main_menu"}
        ]
    ]
    
    return KeyboardBuilder.build_keyboard(buttons)

def get_admin_users_keyboard() -> InlineKeyboardMarkup:
    """Клавиатура управления пользователями"""
    buttons = [
        [
            {"text": "📋 Список пользователей", "callback_data": "admin_users_list"},
            {"text": "➕ Добавить пользователя", "callback_data": "admin_add_user"}
        ],
        [
            {"text": "🔑 Управление API", "callback_data": "admin_manage_api"},
            {"text": "💎 Премиум статус", "callback_data": "admin_premium"}
        ],
        [
            {"text": "📊 Статистика пользователей", "callback_data": "admin_user_stats"},
            {"text": "🚫 Заблокировать", "callback_data": "admin_block_user"}
        ],
        [
            {"text": "🔙 Админ меню", "callback_data": "admin_menu"},
            {"text": "🏠 Главное меню", "callback_data": "main_menu"}
        ]
    ]
    
    return KeyboardBuilder.build_keyboard(buttons)

# Навигационные клавиатуры
def get_back_keyboard(back_to: str = "main_menu") -> InlineKeyboardMarkup:
    """Простая клавиатура с кнопкой назад"""
    buttons = [
        [
            {"text": "🔙 Назад", "callback_data": back_to}
        ]
    ]
    
    return KeyboardBuilder.build_keyboard(buttons)

def get_cancel_keyboard() -> InlineKeyboardMarkup:
    """Клавиатура отмены"""
    buttons = [
        [
            {"text": "❌ Отменить", "callback_data": "cancel"}
        ]
    ]
    
    return KeyboardBuilder.build_keyboard(buttons)

def get_pagination_keyboard(current_page: int, total_pages: int, 
                          callback_prefix: str) -> InlineKeyboardMarkup:
    """Клавиатура пагинации"""
    buttons = []
    
    # Кнопки навигации
    nav_buttons = []
    if current_page > 1:
        nav_buttons.append({"text": "⬅️", "callback_data": f"{callback_prefix}_page_{current_page - 1}"})
    
    nav_buttons.append({"text": f"{current_page}/{total_pages}", "callback_data": "current_page"})
    
    if current_page < total_pages:
        nav_buttons.append({"text": "➡️", "callback_data": f"{callback_prefix}_page_{current_page + 1}"})
    
    buttons.append(nav_buttons)
    
    # Кнопка назад
    buttons.append([{"text": "🔙 Назад", "callback_data": "main_menu"}])
    
    return KeyboardBuilder.build_keyboard(buttons)

# Специальные клавиатуры
def get_emergency_keyboard() -> InlineKeyboardMarkup:
    """Клавиатура экстренных действий"""
    buttons = [
        [
            {"text": "🚨 ЭКСТРЕННАЯ ОСТАНОВКА", "callback_data": "emergency_stop"}
        ],
        [
            {"text": "🛑 Закрыть все позиции", "callback_data": "close_all_positions"},
            {"text": "❌ Отменить все ордера", "callback_data": "cancel_all_orders"}
        ],
        [
            {"text": "🔙 Назад", "callback_data": "main_menu"}
        ]
    ]
    
    return KeyboardBuilder.build_keyboard(buttons)


def get_strategy_dynamic_config_keyboard(strategy_type: str, config: Dict[str, Any]) -> InlineKeyboardMarkup:
    """Создает клавиатуру для динамической настройки параметров стратегии. (ИСПРАВЛЕННАЯ ВЕРСИЯ)"""
    buttons = []

    # Определяем параметры, которые пользователь может редактировать
    editable_params = {}
    if strategy_type == StrategyType.BIDIRECTIONAL_GRID.value:
        editable_params = {
            "order_amount": f"Сумма ордера: {config.get('order_amount', 0)} USDT",
            "grid_levels": f"Уровни (в каждую сторону): {config.get('grid_levels', 0)}",
            "profit_percent": f"Процент прибыли: {config.get('profit_percent', 0)}%",
            "stop_loss_percent": f"Стоп-лосс: {config.get('stop_loss_percent', 0)}%",
        }
    elif strategy_type == StrategyType.GRID_SCALPING.value:
        editable_params = {
            "order_amount": f"Сумма ордера: {config.get('order_amount', 0)} USDT",
            "max_averaging_orders": f"Макс. ордеров усреднения: {config.get('max_averaging_orders', 0)}",
            "profit_percent": f"Процент прибыли: {config.get('profit_percent', 0)}%",
            "stop_loss_percent": f"Стоп-лосс: {config.get('stop_loss_percent', 0)}%",
        }
    elif strategy_type == StrategyType.IMPULSE_TRAILING.value:
        editable_params = {
            "order_amount": f"Сумма ордера: {config.get('order_amount', 0)} USDT",
            "min_signal_strength": f"Мин. сила сигнала: {config.get('min_signal_strength', 0)}",
            "stop_loss_percent": f"Стоп-лосс: {config.get('stop_loss_percent', 0)}%",
            "trailing_percent": f"Трейлинг: {config.get('trailing_percent', 0)}%",
        }

    # Создаем кнопки для параметров
    for key, text in editable_params.items():
        buttons.append([{"text": text, "callback_data": f"manual_cfg_{key}"}])

    # Добавляем кнопки управления
    buttons.append([
        {"text": "🚀 Запустить стратегию", "callback_data": "manual_launch"},
        {"text": "🔙 Назад к выбору стратегии", "callback_data": "manual_back_to_strategy_select"}
    ])

    return KeyboardBuilder.build_keyboard(buttons)
