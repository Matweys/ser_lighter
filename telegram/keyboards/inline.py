"""
Профессиональная система inline клавиатур для многопользовательского торгового бота
"""
from aiogram.types import InlineKeyboardMarkup, InlineKeyboardButton
from typing import List, Optional, Dict, Any
from core.enums import StrategyType, PositionSide, NotificationType

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
def get_main_menu_keyboard(is_trading_active: bool = False) -> InlineKeyboardMarkup:
    """Главное меню бота"""
    if is_trading_active:
        buttons = [
            [
                {"text": "🛑 Остановить торговлю", "callback_data": "stop_trading"},
                {"text": "📊 Статистика", "callback_data": "statistics"}
            ],
            [
                {"text": "💰 Баланс", "callback_data": "show_balance"},
                {"text": "📈 Позиции", "callback_data": "show_positions"}
            ],
            [
                {"text": "📋 Ордера", "callback_data": "show_orders"},
                {"text": "📊 Статус", "callback_data": "show_status"}
            ],
            [
                {"text": "⚙️ Настройки", "callback_data": "settings"},
                {"text": "ℹ️ Помощь", "callback_data": "help"}
            ]
        ]
    else:
        buttons = [
            [
                {"text": "🚀 Запустить торговлю", "callback_data": "start_trading"},
                {"text": "📊 Статистика", "callback_data": "statistics"}
            ],
            [
                {"text": "⚙️ Настройки", "callback_data": "settings"},
                {"text": "💰 Баланс", "callback_data": "show_balance"}
            ],
            [
                {"text": "📋 Watchlist", "callback_data": "watchlist"},
                {"text": "🔑 API ключи", "callback_data": "api_keys"}
            ],
            [
                {"text": "ℹ️ Помощь", "callback_data": "help"}
            ]
        ]

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
            {"text": "🔑 API ключи", "callback_data": "api_settings"},
            {"text": "📋 Watchlist", "callback_data": "watchlist_settings"}
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
            {"text": "🎯 Риск на сделку", "callback_data": "set_risk_per_trade"},
            {"text": "📉 Макс. просадка", "callback_data": "set_max_drawdown"}
        ],
        [
            {"text": "📊 Макс. сделок", "callback_data": "set_max_trades"},
            {"text": "💰 Мин. баланс", "callback_data": "set_min_balance"}
        ],
        [
            {"text": "🛑 Стоп-лосс", "callback_data": "set_stop_loss"},
            {"text": "🎯 Тейк-профит", "callback_data": "set_take_profit"}
        ],
        [
            {"text": "⚙️ Настройки", "callback_data": "settings"},
            {"text": "🏠 Главное меню", "callback_data": "main_menu"}
        ]
    ]
    
    return KeyboardBuilder.build_keyboard(buttons)

def get_strategy_settings_keyboard() -> InlineKeyboardMarkup:
    """Клавиатура настроек стратегий"""
    buttons = [
        [
            {"text": "🔄 Двунаправленная сетка", "callback_data": "configure_strategy_bidirectional_grid"},
            {"text": "⚡ Сеточный скальпинг", "callback_data": "configure_strategy_grid_scalping"}
        ],
        [
            {"text": "🚀 Импульсный трейлинг", "callback_data": "configure_strategy_impulse_trailing"}
        ],
        [
            {"text": "✅ Включить все", "callback_data": "enable_all_strategies"},
            {"text": "❌ Отключить все", "callback_data": "disable_all_strategies"}
        ],
        [
            {"text": "⚙️ Настройки", "callback_data": "settings"},
            {"text": "🏠 Главное меню", "callback_data": "main_menu"}
        ]
    ]
    
    return KeyboardBuilder.build_keyboard(buttons)

def get_strategy_config_keyboard(strategy_type: str) -> InlineKeyboardMarkup:
    """Клавиатура настройки конкретной стратегии"""
    if strategy_type == StrategyType.BIDIRECTIONAL_GRID.value:
        buttons = [
            [
                {"text": "📏 Уровни сетки", "callback_data": f"set_{strategy_type}_levels"},
                {"text": "📊 Spacing (%)", "callback_data": f"set_{strategy_type}_spacing"}
            ],
            [
                {"text": "💵 Размер ордера", "callback_data": f"set_{strategy_type}_order_size"},
                {"text": "🔄 Ребаланс", "callback_data": f"set_{strategy_type}_rebalance"}
            ],
            [
                {"text": "📈 Границы сетки", "callback_data": f"set_{strategy_type}_bounds"},
                {"text": "💰 Макс. позиция", "callback_data": f"set_{strategy_type}_max_position"}
            ]
        ]
    elif strategy_type == StrategyType.GRID_SCALPING.value:
        buttons = [
            [
                {"text": "⏰ Таймаут ордера", "callback_data": f"set_{strategy_type}_timeout"},
                {"text": "📊 Мин. спред", "callback_data": f"set_{strategy_type}_spread"}
            ],
            [
                {"text": "💵 Размер ордера", "callback_data": f"set_{strategy_type}_order_size"},
                {"text": "📈 Макс. ордеров", "callback_data": f"set_{strategy_type}_max_orders"}
            ],
            [
                {"text": "🎯 Цель прибыли", "callback_data": f"set_{strategy_type}_profit_target"},
                {"text": "💧 Проверка ликвидности", "callback_data": f"set_{strategy_type}_liquidity"}
            ]
        ]
    elif strategy_type == StrategyType.IMPULSE_TRAILING.value:
        buttons = [
            [
                {"text": "🎯 Мин. сила сигнала", "callback_data": f"set_{strategy_type}_signal_strength"},
                {"text": "📈 Трейлинг (%)", "callback_data": f"set_{strategy_type}_trailing"}
            ],
            [
                {"text": "💵 Размер позиции", "callback_data": f"set_{strategy_type}_position_size"},
                {"text": "⏰ Макс. время", "callback_data": f"set_{strategy_type}_max_time"}
            ],
            [
                {"text": "📊 Частичное закрытие", "callback_data": f"set_{strategy_type}_partial_close"},
                {"text": "🔍 Подтверждение тренда", "callback_data": f"set_{strategy_type}_trend_confirm"}
            ]
        ]
    else:
        buttons = [
            [
                {"text": "⚙️ Основные параметры", "callback_data": f"set_{strategy_type}_basic"}
            ]
        ]
    
    # Добавляем общие кнопки
    buttons.extend([
        [
            {"text": "✅ Включить стратегию", "callback_data": f"enable_strategy_{strategy_type}"},
            {"text": "❌ Отключить стратегию", "callback_data": f"disable_strategy_{strategy_type}"}
        ],
        [
            {"text": "📊 Настройки стратегий", "callback_data": "strategy_settings"},
            {"text": "🏠 Главное меню", "callback_data": "main_menu"}
        ]
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

# Управление символами
def get_symbol_selection_keyboard(symbols: List[str]) -> InlineKeyboardMarkup:
    """Клавиатура выбора символа"""
    buttons = []
    
    # Добавляем символы по 2 в ряд
    for i in range(0, len(symbols), 2):
        row = []
        for j in range(2):
            if i + j < len(symbols):
                symbol = symbols[i + j]
                row.append({"text": symbol, "callback_data": f"select_symbol_{symbol}"})
        buttons.append(row)
    
    # Добавляем кнопки управления
    buttons.extend([
        [
            {"text": "➕ Добавить символ", "callback_data": "add_symbol"},
            {"text": "➖ Удалить символ", "callback_data": "remove_symbol"}
        ],
        [
            {"text": "🔄 Обновить список", "callback_data": "refresh_symbols"},
            {"text": "🏠 Главное меню", "callback_data": "main_menu"}
        ]
    ])
    
    return KeyboardBuilder.build_keyboard(buttons)

def get_watchlist_keyboard() -> InlineKeyboardMarkup:
    """Клавиатура управления watchlist"""
    buttons = [
        [
            {"text": "📋 Показать список", "callback_data": "show_watchlist"},
            {"text": "➕ Добавить символ", "callback_data": "add_to_watchlist"}
        ],
        [
            {"text": "➖ Удалить символ", "callback_data": "remove_from_watchlist"},
            {"text": "🔄 Обновить цены", "callback_data": "refresh_watchlist"}
        ],
        [
            {"text": "📊 Популярные символы", "callback_data": "popular_symbols"},
            {"text": "🔍 Поиск символа", "callback_data": "search_symbol"}
        ],
        [
            {"text": "⚙️ Настройки", "callback_data": "settings"},
            {"text": "🏠 Главное меню", "callback_data": "main_menu"}
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
            {"text": "🔄 Обновить", "callback_data": "refresh_balance"},
            {"text": "📊 Детали", "callback_data": "balance_details"}
        ],
        [
            {"text": "💰 История", "callback_data": "balance_history"},
            {"text": "📈 Статистика", "callback_data": "statistics"}
        ],
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



