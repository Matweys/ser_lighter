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

# --- ОПИСАНИЯ ПАРАМЕТРОВ ---

def get_parameter_description(strategy_type: str, param_key: str) -> str:
    """
    Возвращает подробное описание параметра для пользователя.
    Объясняет что делает параметр и как его настраивать.
    """
    descriptions = {
        "flash_drop_catcher": {
            "base_drop_percent": (
                "📉 <b>Базовый порог падения</b>\n\n"
                "Это <u>стартовая точка</u> для расчёта динамического порога.\n\n"
                "🎯 <b>Как работает:</b>\n"
                "Финальный порог = Базовый + (Волатильность × 4)\n\n"
                "📊 <b>Примеры:</b>\n"
                "• Базовый 4% + BTC (волатильность 0.3%) = 5.2% порог\n"
                "• Базовый 4% + ETH (волатильность 1.2%) = 8.8% порог\n\n"
                "💡 <b>Как настроить:</b>\n"
                "• <u>Увеличить</u> (5-6%) → Меньше сигналов, но качественнее\n"
                "• <u>Уменьшить</u> (3%) → Больше сигналов, чаще входы\n\n"
                "⚙️ <b>Рекомендация:</b> 4% (сбалансированно)"
            ),
            "min_drop_percent": (
                "⬇️ <b>Минимальный порог</b>\n\n"
                "Это <u>нижняя граница</u> для динамического порога.\n\n"
                "🎯 <b>Как работает:</b>\n"
                "Даже если волатильность очень низкая, порог НЕ опустится ниже этого значения.\n\n"
                "📊 <b>Примеры:</b>\n"
                "• Мин. порог 2.5% → BTC упал на 2% = ❌ НЕ входим\n"
                "• Мин. порог 2.5% → BTC упал на 3% = ✅ ВХОДИМ\n\n"
                "💡 <b>Как настроить:</b>\n"
                "• <u>Увеличить</u> (3-4%) → Только сильные падения для стабильных монет\n"
                "• <u>Уменьшить</u> (2%) → Ловить даже мелкие откаты BTC/ETH\n\n"
                "⚙️ <b>Рекомендация:</b> 2.5% (для частых входов по BTC/ETH)"
            ),
            "max_drop_percent": (
                "⬆️ <b>Максимальный порог (ограничивает расчёт)</b>\n\n"
                "Это <u>верхняя граница ТОЛЬКО для расчёта порога</u>.\n\n"
                "⚠️ <b>ВАЖНО:</b> НЕ блокирует вход при сильных падениях!\n\n"
                "🎯 <b>Как работает:</b>\n"
                "Если расчётный порог &gt; max, то используется max.\n"
                "НО если монета упала на 50% - всё равно войдёт!\n\n"
                "📊 <b>Примеры:</b>\n"
                "• Макс. порог 10%, PEPE упал на 12% = ✅ ВХОДИМ (12% &gt; 10%)\n"
                "• Макс. порог 10%, PEPE упал на 30% = ✅ ВХОДИМ (30% &gt; 10%)\n"
                "• Макс. порог 10%, PEPE упал на 8% = ❌ НЕ входим (8% &lt; 10%)\n\n"
                "💡 <b>Как настроить:</b>\n"
                "• <u>Увеличить</u> (12-15%) → Строже для мемкоинов\n"
                "• <u>Уменьшить</u> (8%) → Чаще входы по мемкоинам\n\n"
                "⚙️ <b>Рекомендация:</b> 10% (баланс)"
            ),
            "volume_spike_min": (
                "📈 <b>Минимальный всплеск объёма</b>\n\n"
                "Проверяет <u>интерес к падению</u> - растёт ли объём торгов.\n\n"
                "🎯 <b>Как работает:</b>\n"
                "Текущий объём / Средний объём &gt;= Этот параметр\n\n"
                "📊 <b>Примеры:</b>\n"
                "• Параметр 2.5x, средний объём $1M, текущий $2M = ❌ (2x &lt; 2.5x)\n"
                "• Параметр 2.5x, средний объём $1M, текущий $3M = ✅ (3x &gt; 2.5x)\n\n"
                "💡 <b>Зачем это нужно:</b>\n"
                "Падение с большим объёмом = реальный интерес → выше шанс отскока!\n\n"
                "💡 <b>Как настроить:</b>\n"
                "• <u>Увеличить</u> (3-4x) → Только сильные всплески, меньше шума\n"
                "• <u>Уменьшить</u> (2x) → Больше сигналов, но ниже качество\n\n"
                "⚙️ <b>Рекомендация:</b> 2.5x (золотая середина)"
            ),
        }
    }

    return descriptions.get(strategy_type, {}).get(param_key, f"Введите новое значение для <b>{param_key}</b>:")


# --- ОСНОВНЫЕ МЕНЮ ---

def get_main_menu_keyboard() -> InlineKeyboardMarkup:
    """Главное меню бота."""
    buttons = [
        [{"text": "💰 Баланс", "callback_data": "show_balance"}],
        [{"text": "🔑 API ключи", "callback_data": "api_keys"}, {"text": "⚙️ Настройки", "callback_data": "settings"}],
    ]
    return KeyboardBuilder.build_keyboard(buttons)

def get_settings_keyboard() -> InlineKeyboardMarkup:
    """Главное меню настроек."""
    buttons = [
        [{"text": "📊 Стратегии", "callback_data": "strategy_settings"}],
        [{"text": "📈 Торговые пары", "callback_data": "select_trading_pairs"}],
        [{"text": "🔄 Сбросить настройки", "callback_data": "reset_settings"}],
        [{"text": "🏠 Главное меню", "callback_data": "main_menu"}]
    ]
    return KeyboardBuilder.build_keyboard(buttons)

# --- МЕНЮ НАСТРОЕК ---

def get_strategy_settings_keyboard(configs: Dict[str, Dict[str, Any]]) -> InlineKeyboardMarkup:
    """Клавиатура выбора стратегии для настройки."""
    def get_status_icon(strategy_type: str) -> str:
        return "✅" if configs.get(strategy_type, {}).get("is_enabled", False) else "❌"

    buttons = [
        [{"text": f"{get_status_icon('signal_scalper')} Сигнальный скальпер", "callback_data": "configure_strategy_signal_scalper"}],
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
        # Toggle параметры (вкл/выкл) - отображаем статус
        enable_sl = config.get('enable_stop_loss', True)
        sl_status = "✅ Вкл" if enable_sl else "❌ Выкл"
        enable_stag = config.get('enable_stagnation_detector', True)
        stag_status = "✅ Вкл" if enable_stag else "❌ Выкл"
        enable_avg = config.get('enable_averaging', True)
        avg_status = "✅ Вкл" if enable_avg else "❌ Выкл"

        editable_params = {
            # Основные
            "leverage": f"⚖️ Плечо: x{config.get('leverage', 2)}",
            "order_amount": f"💰 Сумма ордера: {config.get('order_amount', 200)} USDT",

            # Координатор Multi-Account
            "stuck_threshold_percent": f"🔀 Порог застревания Бота 1: {config.get('stuck_threshold_percent', 4.0)}%",

            # Stop Loss
            "enable_stop_loss": f"🛡️ Stop Loss: {sl_status}",
            "averaging_stop_loss_percent": f"🛑 SL после усреднения: {config.get('averaging_stop_loss_percent', 45.0)}%",

            # Усреднение #1 (Детектор застрявшей цены)
            "enable_stagnation_detector": f"📍 Усреднение #1 (Детектор застревания): {stag_status}",
            "stagnation_trigger_min_percent": f"  ├─ Триггер От: {config.get('stagnation_trigger_min_percent', 15.0)}%",
            "stagnation_trigger_max_percent": f"  ├─ Триггер До: {config.get('stagnation_trigger_max_percent', 20.0)}%",
            "stagnation_check_interval_seconds": f"  └─ Время наблюдения: {config.get('stagnation_check_interval_seconds', 30)} сек",

            # Усреднение #2 (Основное)
            "enable_averaging": f"📊 Усреднение #2 (Основное): {avg_status}",
            "averaging_trigger_loss_percent": f"  └─ Триггер убытка: {config.get('averaging_trigger_loss_percent', 15.0)}%",
        }
    elif strategy_type == StrategyType.FLASH_DROP_CATCHER.value:
        # Toggle параметры (вкл/выкл) - отображаем статус
        enable_heartbeat = config.get('enable_heartbeat_notifications', True)
        heartbeat_status = "✅ Вкл" if enable_heartbeat else "❌ Выкл"

        editable_params = {
            "leverage": f"🎚️ Плечо: x{config.get('leverage', 2)}",
            "order_amount": f"💰 Сумма ордера: {config.get('order_amount', 1000)} USDT",

            # Пороги падений (динамические)
            "base_drop_percent": f"📉 Базовый порог падения: {config.get('base_drop_percent', 4.0)}%",
            "min_drop_percent": f"⬇️ Мин. порог (для BTC/ETH): {config.get('min_drop_percent', 2.5)}%",
            "max_drop_percent": f"⬆️ Макс. порог (ограничивает расчёт): {config.get('max_drop_percent', 10.0)}%",

            # Фильтры
            "volume_spike_min": f"📈 Мин. всплеск объёма: {config.get('volume_spike_min', 2.5)}x",

            "enable_heartbeat_notifications": f"💓 Heartbeat уведомления (каждый час): {heartbeat_status}",
        }

    # Определяем какие параметры toggle (boolean), а какие set (numeric)
    toggle_params = ['enable_stop_loss', 'enable_stagnation_detector', 'enable_averaging', 'enable_heartbeat_notifications']

    for key, text in editable_params.items():
        # Для boolean параметров используем toggle вместо set
        if key in toggle_params:
            buttons.append([{"text": text, "callback_data": f"toggle_param_{strategy_type}_{key}"}])
        else:
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

def get_api_keys_keyboard(api_keys_count: int = 0, api_keys_list: list = None) -> InlineKeyboardMarkup:
    """
    Клавиатура управления API ключами (Multi-Account Support).

    Args:
        api_keys_count: Количество сохраненных API ключей (0-3)
        api_keys_list: Список приоритетов существующих ключей [1, 2, 3]
    """
    buttons = []

    if api_keys_count == 0:
        # Нет ключей - предлагаем добавить PRIMARY
        buttons.append([{"text": "➕ Добавить PRIMARY ключ (Bot 1)", "callback_data": "add_api_key_priority_1"}])

    elif api_keys_count == 1:
        # Есть PRIMARY - предлагаем добавить SECONDARY или удалить PRIMARY
        buttons.append([{"text": "➕ Добавить SECONDARY ключ (Bot 2)", "callback_data": "add_api_key_priority_2"}])
        buttons.append([{"text": "🔄 Обновить PRIMARY ключ", "callback_data": "update_api_key_priority_1"}])
        buttons.append([{"text": "🗑️ Удалить PRIMARY ключ", "callback_data": "delete_api_key_priority_1"}])

    elif api_keys_count == 2:
        # Есть PRIMARY и SECONDARY - предлагаем добавить TERTIARY
        buttons.append([{"text": "➕ Добавить TERTIARY ключ (Bot 3)", "callback_data": "add_api_key_priority_3"}])
        buttons.append([
            {"text": "🔄 Обновить PRIMARY", "callback_data": "update_api_key_priority_1"},
            {"text": "🔄 Обновить SECONDARY", "callback_data": "update_api_key_priority_2"}
        ])
        buttons.append([
            {"text": "🗑️ Удалить PRIMARY", "callback_data": "delete_api_key_priority_1"},
            {"text": "🗑️ Удалить SECONDARY", "callback_data": "delete_api_key_priority_2"}
        ])

    elif api_keys_count >= 3:
        # Все 3 ключа настроены - только обновление и удаление
        buttons.append([{"text": "✅ Multi-Account режим АКТИВЕН (3 бота)", "callback_data": "noop"}])
        buttons.append([
            {"text": "🔄 PRIMARY", "callback_data": "update_api_key_priority_1"},
            {"text": "🔄 SECONDARY", "callback_data": "update_api_key_priority_2"},
            {"text": "🔄 TERTIARY", "callback_data": "update_api_key_priority_3"}
        ])
        buttons.append([
            {"text": "🗑️ PRIMARY", "callback_data": "delete_api_key_priority_1"},
            {"text": "🗑️ SECONDARY", "callback_data": "delete_api_key_priority_2"},
            {"text": "🗑️ TERTIARY", "callback_data": "delete_api_key_priority_3"}
        ])

    # Кнопка "Назад"
    buttons.append([{"text": "🏠 Главное меню", "callback_data": "main_menu"}])

    return KeyboardBuilder.build_keyboard(buttons)