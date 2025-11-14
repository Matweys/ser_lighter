"""
Вспомогательные функции для работы с multi-account режимом
Содержит общие функции получения данных и форматирования для 3-х аккаунтов
"""
from typing import Dict, List, Tuple, Optional, Any
from api.bybit_api import BybitAPI
from core.logger import log_info, log_error, log_warning
from core.functions import format_currency, format_percentage
from core.settings_config import system_config


# ============================================================================
# КОНСТАНТЫ
# ============================================================================

PRIORITY_NAMES = {1: "PRIMARY", 2: "SECONDARY", 3: "TERTIARY"}
PRIORITY_EMOJIS = {1: "🥇", 2: "🥈", 3: "🥉"}

# Названия ботов для отображения
BOT_NAMES = {1: "BOT #1", 2: "BOT #2", 3: "BOT #3"}

# Названия стратегий для отображения
STRATEGY_NAMES = {
    "signal_scalper": "SignalScalper"
}

# Заголовки стратегий с эмодзи для отображения
STRATEGY_HEADERS = {
    "signal_scalper": "📊 Signal Scalper"
}

# Названия месяцев на русском для статистики
MONTH_NAMES_RU = {
    1: "январь", 2: "февраль", 3: "март", 4: "апрель", 5: "май", 6: "июнь",
    7: "июль", 8: "август", 9: "сентябрь", 10: "октябрь", 11: "ноябрь", 12: "декабрь"
}

# Названия параметров стратегий на русском
PARAM_NAMES_RU = {
    "enable_stop_loss": "Stop Loss",
    "enable_stagnation_detector": "Усреднение #1 (Детектор застревания)",
    "enable_averaging": "Усреднение #2 (Основное)"
}

# Названия приоритетов с эмодзи для отображения в UI
PRIORITY_NAMES_WITH_EMOJI = {
    1: "🥇 PRIMARY (Bot 1)",
    2: "🥈 SECONDARY (Bot 2)",
    3: "🥉 TERTIARY (Bot 3)"
}


# ============================================================================
# ВСПОМОГАТЕЛЬНЫЕ ФУНКЦИИ ПРОВЕРКИ
# ============================================================================

def validate_api_keys(all_api_keys: Optional[List[Dict]]) -> bool:
    """
    Проверяет наличие API ключей.

    Args:
        all_api_keys: Список API ключей

    Returns:
        True если ключи есть, False если нет
    """
    return all_api_keys is not None and len(all_api_keys) > 0


def is_multi_account_mode(all_api_keys: List[Dict]) -> bool:
    """
    Проверяет, активен ли multi-account режим (3 аккаунта).

    Args:
        all_api_keys: Список API ключей

    Returns:
        True если 3 аккаунта, False если 1 аккаунт
    """
    return len(all_api_keys) == 3


def is_active_position(pos: Dict) -> bool:
    """
    Проверяет, является ли позиция активной (размер != 0).

    Args:
        pos: Данные позиции

    Returns:
        True если позиция активна
    """
    return float(pos.get('size', 0)) != 0


def is_active_order(order: Dict) -> bool:
    """
    Проверяет, является ли ордер активным.

    Args:
        order: Данные ордера

    Returns:
        True если ордер активен (New или PartiallyFilled)
    """
    return order.get('orderStatus') in ['New', 'PartiallyFilled']


# ============================================================================
# ФУНКЦИИ ПОЛУЧЕНИЯ ДАННЫХ (MULTI-ACCOUNT)
# ============================================================================

async def get_multi_account_balance(
    user_id: int,
    all_api_keys: List[Dict],
    use_demo: bool
) -> Dict[str, Any]:
    """
    Получает баланс со всех 3 аккаунтов в multi-account режиме.

    Args:
        user_id: ID пользователя
        all_api_keys: Список всех API ключей (должно быть 3)
        use_demo: Флаг демо-режима

    Returns:
        Dict с ключами:
            - accounts_data: List[Dict] - данные по каждому аккаунту
            - total_equity: float - суммарный капитал
            - total_available: float - суммарный доступный баланс
            - total_unrealised_pnl: float - суммарный нереализованный PnL
    """
    total_equity_sum = 0
    total_available_sum = 0
    total_unrealised_pnl_sum = 0
    accounts_data = []

    # Получаем баланс для каждого аккаунта
    for key_data in sorted(all_api_keys, key=lambda x: x['priority']):
        priority = key_data['priority']
        try:
            async with BybitAPI(
                user_id=user_id,
                api_key=key_data['api_key'],
                api_secret=key_data['secret_key'],
                demo=use_demo
            ) as api:
                balance_data = await api.get_wallet_balance()

            if balance_data and 'totalEquity' in balance_data:
                equity = float(balance_data['totalEquity'])
                available = float(balance_data['totalAvailableBalance'])
                unrealised_pnl = float(balance_data['totalUnrealisedPnl'])

                total_equity_sum += equity
                total_available_sum += available
                total_unrealised_pnl_sum += unrealised_pnl

                accounts_data.append({
                    'priority': priority,
                    'equity': equity,
                    'available': available,
                    'unrealised_pnl': unrealised_pnl
                })
            else:
                log_warning(user_id, f"Не удалось получить баланс для аккаунта {priority}", "multi_account_helpers")
        except Exception as account_error:
            log_error(user_id, f"Ошибка получения баланса для аккаунта {priority}: {account_error}", "multi_account_helpers")

    return {
        'accounts_data': accounts_data,
        'total_equity': total_equity_sum,
        'total_available': total_available_sum,
        'total_unrealised_pnl': total_unrealised_pnl_sum
    }


async def get_multi_account_positions(
    user_id: int,
    all_api_keys: List[Dict],
    use_demo: bool
) -> List[Dict]:
    """
    Получает позиции со всех 3 аккаунтов в multi-account режиме.

    Args:
        user_id: ID пользователя
        all_api_keys: Список всех API ключей (должно быть 3)
        use_demo: Флаг демо-режима

    Returns:
        List[Dict] - список всех активных позиций с добавленным полем '_bot_priority'
    """
    all_positions = []

    # Получаем позиции для каждого аккаунта
    for key_data in sorted(all_api_keys, key=lambda x: x['priority']):
        priority = key_data['priority']
        try:
            async with BybitAPI(
                user_id=user_id,
                api_key=key_data['api_key'],
                api_secret=key_data['secret_key'],
                demo=use_demo
            ) as api:
                positions = await api.get_positions()

            if positions:
                # Фильтруем только активные позиции и добавляем маркер приоритета
                for pos in positions:
                    if is_active_position(pos):
                        pos['_bot_priority'] = priority  # Добавляем метку бота
                        all_positions.append(pos)
        except Exception as account_error:
            log_error(user_id, f"Ошибка получения позиций для аккаунта {priority}: {account_error}", "multi_account_helpers")

    return all_positions


async def get_multi_account_orders(
    user_id: int,
    all_api_keys: List[Dict],
    use_demo: bool
) -> List[Dict]:
    """
    Получает ордера со всех 3 аккаунтов в multi-account режиме.

    Args:
        user_id: ID пользователя
        all_api_keys: Список всех API ключей (должно быть 3)
        use_demo: Флаг демо-режима

    Returns:
        List[Dict] - список всех активных ордеров с добавленным полем '_bot_priority'
    """
    all_orders = []

    # Получаем ордера для каждого аккаунта
    for key_data in sorted(all_api_keys, key=lambda x: x['priority']):
        priority = key_data['priority']
        try:
            async with BybitAPI(
                user_id=user_id,
                api_key=key_data['api_key'],
                api_secret=key_data['secret_key'],
                demo=use_demo
            ) as api:
                orders = await api.get_open_orders()

            if orders:
                # Добавляем маркер приоритета к каждому ордеру
                for order in orders:
                    order['_bot_priority'] = priority  # Добавляем метку бота
                    all_orders.append(order)
        except Exception as account_error:
            log_error(user_id, f"Ошибка получения ордеров для аккаунта {priority}: {account_error}", "multi_account_helpers")

    return all_orders


async def get_multi_account_positions_and_orders(
    user_id: int,
    all_api_keys: List[Dict],
    use_demo: bool
) -> Tuple[List[Dict], List[Dict]]:
    """
    Получает позиции И ордера со всех 3 аккаунтов в multi-account режиме.
    Оптимизировано: один запрос к API на аккаунт вместо двух.

    Args:
        user_id: ID пользователя
        all_api_keys: Список всех API ключей (должно быть 3)
        use_demo: Флаг демо-режима

    Returns:
        Tuple[List[Dict], List[Dict]] - (all_positions, all_orders)
    """
    all_positions = []
    all_orders = []

    # Собираем позиции и ордера со всех 3 аккаунтов
    for key_data in sorted(all_api_keys, key=lambda x: x['priority']):
        priority = key_data['priority']
        try:
            async with BybitAPI(
                user_id=user_id,
                api_key=key_data['api_key'],
                api_secret=key_data['secret_key'],
                demo=use_demo
            ) as api:
                positions = await api.get_positions()
                orders = await api.get_open_orders()

            if positions:
                for pos in positions:
                    if is_active_position(pos):
                        pos['_bot_priority'] = priority
                        all_positions.append(pos)

            if orders:
                # api.get_open_orders() уже возвращает только открытые ордера
                for order in orders:
                    order['_bot_priority'] = priority
                    all_orders.append(order)

        except Exception as account_error:
            log_error(user_id, f"Ошибка получения данных для аккаунта {priority}: {account_error}", "multi_account_helpers")

    return all_positions, all_orders


# ============================================================================
# ФУНКЦИИ ФОРМАТИРОВАНИЯ СООБЩЕНИЙ
# ============================================================================

def format_multi_account_balance(
    total_equity: float,
    total_available: float,
    total_unrealised_pnl: float,
    accounts_data: List[Dict],
    verbose: bool = True
) -> str:
    """
    Форматирует сообщение о балансе в multi-account режиме.

    Args:
        total_equity: Общий капитал по всем аккаунтам
        total_available: Общий доступный баланс
        total_unrealised_pnl: Общий нереализованный PnL
        accounts_data: Список данных по аккаунтам
        verbose: Подробный вывод (с заголовком и деталями по каждому аккаунту)

    Returns:
        Отформатированная строка HTML
    """
    balance_text = ""

    if verbose:
        balance_text += "💰 <b>БАЛАНС (Multi-Account Режим)</b>\n"
        balance_text += "═" * 30 + "\n\n"

    # Агрегированные данные
    pnl_emoji = "📈" if total_unrealised_pnl >= 0 else "📉"
    balance_text += f"🌟 <b>ОБЩИЙ БАЛАНС ПО ВСЕМ АККАУНТАМ:</b>\n"
    balance_text += f"  • Капитал: {format_currency(total_equity)}\n"
    balance_text += f"  • Доступно: {format_currency(total_available)}\n"
    balance_text += f"  • PnL: {pnl_emoji} {format_currency(total_unrealised_pnl)}\n\n"

    if verbose:
        balance_text += "─" * 30 + "\n\n"

        # Детали по каждому аккаунту
        for acc in accounts_data:
            priority = acc['priority']
            name = PRIORITY_NAMES.get(priority, f"Бот {priority}")
            emoji = PRIORITY_EMOJIS.get(priority, "🔹")
            pnl_emoji_acc = "📈" if acc['unrealised_pnl'] >= 0 else "📉"

            balance_text += f"{emoji} <b>{name} (Бот {priority})</b>\n"
            balance_text += f"  • Капитал: {format_currency(acc['equity'])}\n"
            balance_text += f"  • Доступно: {format_currency(acc['available'])}\n"
            balance_text += f"  • PnL: {pnl_emoji_acc} {format_currency(acc['unrealised_pnl'])}\n\n"

    return balance_text


def format_multi_account_positions(all_positions: List[Dict]) -> str:
    """
    Форматирует сообщение о позициях в multi-account режиме.

    Args:
        all_positions: Список всех позиций с полем '_bot_priority'

    Returns:
        Отформатированная строка HTML
    """
    positions_text = "📈 <b>ОТКРЫТЫЕ ПОЗИЦИИ (Multi-Account Режим)</b>\n"
    positions_text += "═" * 35 + "\n\n"

    # Группируем позиции по ботам
    for priority in [1, 2, 3]:
        bot_positions = [p for p in all_positions if p.get('_bot_priority') == priority]

        if bot_positions:
            name = PRIORITY_NAMES.get(priority, f"Бот {priority}")
            emoji = PRIORITY_EMOJIS.get(priority, "🔹")

            positions_text += f"{emoji} <b>{name} (Бот {priority})</b>\n"
            positions_text += "─" * 30 + "\n"

            for pos in bot_positions:
                symbol = pos['symbol']
                side_emoji = "🟢 LONG" if pos['side'] == 'Buy' else "🔴 SHORT"
                pnl_emoji = "📈" if float(pos.get('unrealisedPnl', 0)) >= 0 else "📉"
                pnl_value = float(pos.get('unrealisedPnl', 0))

                # Определяем статус (для multi-account координатора)
                status_emoji = ""
                avg_price = float(pos.get('avgPrice', 1))
                size = float(pos.get('size', 1))
                if avg_price > 0 and size > 0:
                    pnl_percent = (pnl_value / (avg_price * size)) * 100
                    if pnl_percent < -10:
                        status_emoji = " 🔴 STUCK"  # Застрял
                    elif pnl_value > 0:
                        status_emoji = " 🟢 ACTIVE"  # Активный в прибыли
                    else:
                        status_emoji = " 🟡 ACTIVE"  # Активный в убытке

                positions_text += f"\n<b>{symbol}</b> | {side_emoji}{status_emoji}\n"
                positions_text += f"  • Размер: {pos['size']} {pos.get('baseCoin', '')}\n"
                positions_text += f"  • Вход: {format_currency(pos.get('avgPrice', 0))}\n"
                positions_text += f"  • PnL: {pnl_emoji} {format_currency(pnl_value)}\n"

            positions_text += "\n"

    # Агрегированная статистика
    total_pnl = sum(float(p.get('unrealisedPnl', 0)) for p in all_positions)
    total_pnl_emoji = "📈" if total_pnl >= 0 else "📉"

    positions_text += "═" * 35 + "\n"
    positions_text += f"🌟 <b>ИТОГО:</b>\n"
    positions_text += f"  • Всего позиций: {len(all_positions)}\n"
    positions_text += f"  • Общий PnL: {total_pnl_emoji} {format_currency(total_pnl)}\n"

    return positions_text


def format_multi_account_orders(all_orders: List[Dict]) -> str:
    """
    Форматирует сообщение об ордерах в multi-account режиме.

    Args:
        all_orders: Список всех ордеров с полем '_bot_priority'

    Returns:
        Отформатированная строка HTML
    """
    orders_text = "📋 <b>ОТКРЫТЫЕ ОРДЕРА (Multi-Account Режим)</b>\n"
    orders_text += "═" * 35 + "\n\n"

    # Группируем ордера по ботам
    for priority in [1, 2, 3]:
        bot_orders = [o for o in all_orders if o.get('_bot_priority') == priority]

        if bot_orders:
            name = PRIORITY_NAMES.get(priority, f"Бот {priority}")
            emoji = PRIORITY_EMOJIS.get(priority, "🔹")

            orders_text += f"{emoji} <b>{name} (Бот {priority})</b>\n"
            orders_text += "─" * 30 + "\n"

            for order in bot_orders:
                side_emoji = "🟢" if order['side'] == 'Buy' else "🔴"
                orders_text += f"\n<b>{order['symbol']}</b> | {side_emoji} {order['side']}\n"
                orders_text += f"  • Тип: {order['orderType']}\n"
                orders_text += f"  • Кол-во: {order['qty']}\n"
                orders_text += f"  • Цена: {format_currency(order.get('price', 0))}\n"
                orders_text += f"  • Статус: {order['orderStatus']}\n"

            orders_text += "\n"

    # Агрегированная статистика
    orders_text += "═" * 35 + "\n"
    orders_text += f"🌟 <b>ИТОГО:</b> {len(all_orders)} ордеров\n"

    return orders_text


# ============================================================================
# ВСПОМОГАТЕЛЬНЫЕ ФУНКЦИИ КОНФИГУРАЦИИ
# ============================================================================

def get_demo_mode(exchange_name: str = "bybit") -> bool:
    """
    Определяет режим торговли (demo/live) из конфигурации.

    Args:
        exchange_name: Название биржи (по умолчанию "bybit")

    Returns:
        bool: True если демо-режим, False если реальная торговля
    """
    exchange_config = system_config.get_exchange_config(exchange_name)
    return exchange_config.demo if exchange_config else False