"""
Профессиональная система базовых команд для многопользовательского торгового бота
"""
from aiogram import Router, F
from aiogram.types import Message, CallbackQuery
from aiogram.filters import Command, CommandStart, StateFilter
from aiogram.fsm.context import FSMContext
from typing import Optional, Dict, Any
from datetime import datetime
import asyncio
import time
from core.bot_application import BotApplication
from database.db_trades import db_manager, UserProfile
from core.events import EventBus, UserSessionStartRequestedEvent, UserSessionStopRequestedEvent
from .states import UserStates
from cache.redis_manager import redis_manager
from core.functions import format_currency, format_percentage
from core.default_configs import DefaultConfigs
from api.bybit_api import BybitAPI
from core.enums import ConfigType
from ..keyboards.inline import (
    get_main_menu_keyboard,
    get_help_keyboard,
    get_quick_actions_keyboard,
    get_confirmation_keyboard,
    get_settings_keyboard,
    get_back_keyboard,
    KeyboardBuilder
)
from core.logger import log_info, log_error, log_warning
from core.settings_config import system_config, DEFAULT_SYMBOLS



router = Router()

class BasicCommandHandler:
    """Профессиональный обработчик базовых команд"""

    def __init__(self):
        self.command_stats = {}
        self.user_sessions = {}
        self.event_bus: Optional[EventBus] = None


    async def log_command_usage(self, user_id: int, command: str):
        """Логирование использования команд"""
        if command not in self.command_stats:
            self.command_stats[command] = 0
        self.command_stats[command] += 1

        log_info(user_id, f"Команда '{command}' выполнена", module_name='basic_handlers')
        # Обновляем активность пользователя в Redis
        await redis_manager.update_user_activity(user_id)

basic_handler = BasicCommandHandler()


def set_event_bus(event_bus: EventBus):
    """Установка EventBus для basic handler"""
    basic_handler.event_bus = event_bus

@router.message(CommandStart())
async def cmd_start(message: Message, state: FSMContext):
    """Обработчик команды /start"""
    user_id = message.from_user.id
    username = message.from_user.username or f"user_{user_id}"
    first_name = message.from_user.first_name or "Пользователь"
    last_name = message.from_user.last_name or ""

    try:
        await basic_handler.log_command_usage(user_id, "start")

        # 1. Создаем или обновляем профиль пользователя в БД
        user_profile = UserProfile(
            user_id=user_id,
            username=username,
            first_name=first_name,
            last_name=last_name,
            is_active=True
        )
        await db_manager.create_user(user_profile)

        # 2. Проверяем и создаем конфигурации по умолчанию в Redis, если их нет
        global_config = await redis_manager.get_config(user_id, ConfigType.GLOBAL)
        if not global_config:
            log_info(user_id, f"Копирование конфигов по умолчанию для нового пользователя {user_id}",
                     module_name='basic_handlers')
            template_user_id = 0
            all_defaults = DefaultConfigs.get_all_default_configs()

            # Копируем глобальный конфиг
            default_global = await redis_manager.get_config(template_user_id, ConfigType.GLOBAL)
            if default_global:
                await redis_manager.save_config(user_id, ConfigType.GLOBAL, default_global)

            # Копируем конфиги стратегий
            for s_type in all_defaults["strategy_configs"].keys():
                config_enum = getattr(ConfigType, f"STRATEGY_{s_type.upper()}")
                default_strategy_config = await redis_manager.get_config(template_user_id, config_enum)
                if default_strategy_config:
                    await redis_manager.save_config(user_id, config_enum, default_strategy_config)

            # Копируем конфиги компонентов
            for c_type in all_defaults["component_configs"].keys():
                config_enum = getattr(ConfigType, f"COMPONENT_{c_type.upper()}")
                default_component_config = await redis_manager.get_config(template_user_id, config_enum)
                if default_component_config:
                    await redis_manager.save_config(user_id, config_enum, default_component_config)

        # 3. Очищаем FSM состояние
        await state.clear()
        await state.set_state(UserStates.MAIN_MENU)

        # 4. Получаем актуальные данные для приветственного сообщения
        session_data = await redis_manager.get_user_session(user_id)
        is_active = session_data.get('running', False) if session_data else False

        user_db_data = await db_manager.get_user(user_id)
        total_profit = user_db_data.total_profit if user_db_data else 0
        total_trades = user_db_data.total_trades if user_db_data else 0

        welcome_text = (
            f"👋 <b>Добро пожаловать, {first_name}!</b>\n\n"
            f"🤖 <b>Профессиональный торговый бот</b>\n"
            f"Ваш персональный помощник для торговли криптовалютными фьючерсами.\n\n"
            f"📊 <b>Ваша статистика:</b>\n"
            f"💰 Общая прибыль: {format_currency(total_profit)}\n"
            f"📈 Всего сделок: {total_trades}\n"
            f"🔄 Статус торговли: {'🟢 Активен' if is_active else '🔴 Неактивен'}\n\n"
            f"Выберите действие в меню ниже:"
        )

        await message.answer(
            welcome_text,
            reply_markup=get_main_menu_keyboard(),
            parse_mode="HTML"
        )

        log_info(user_id, f"Пользователь {user_id} ({username}) запустил бота", module_name='basic_handlers')

    except Exception as e:
        log_error(user_id, f"Ошибка в команде /start: {e}", module_name='basic_handlers', extra_data={"traceback": str(e.__traceback__)})
        await message.answer(
            "❌ Произошла критическая ошибка при инициализации вашего профиля. Пожалуйста, сообщите администратору.",
            reply_markup=None
        )

@router.message(Command("help"))
async def cmd_help(message: Message, state: FSMContext):
    """Обработчик команды /help"""
    user_id = message.from_user.id

    try:
        await basic_handler.log_command_usage(user_id, "help")

        help_text = (
            f"📚 <b>Справка по командам</b>\n\n"
            f"<b>🔧 Основные команды:</b>\n"
            f"/start - Запуск бота и главное меню\n"
            f"/help - Показать эту справку\n"
            f"/status - Текущий статус торговли\n"
            f"/settings - Настройки бота\n"
            f"/stats - Статистика торговли\n\n"
            f"<b>🚀 Управление торговлей:</b>\n"
            f"/trade_start - Запустить торговлю\n"
            f"/trade_stop - Остановить торговлю\n"
            f"/emergency_stop - Экстренная остановка\n\n"
            f"<b>📊 Информация:</b>\n"
            f"/balance - Баланс аккаунта\n"
            f"/positions - Активные позиции\n"
            f"/orders - Активные ордера\n"
            f"/history - История сделок\n\n"
            f"<b>⚙️ Настройки:</b>\n"
            f"/risk - Настройки риск-менеджмента\n"
            f"/strategies - Настройки стратегий\n"
            f"/watchlist - Управление списком символов\n"
            f"/api - Настройка API ключей\n\n"
            f"<b>💡 Советы:</b>\n"
            f"• Всегда настройте API ключи перед торговлей\n"
            f"• Начните с консервативных настроек риска\n"
            f"• Регулярно проверяйте статистику\n"
            f"• Используйте стоп-лоссы для защиты капитала\n\n"
            f"Для получения подробной информации используйте кнопки меню."
        )

        await message.answer(
            help_text,
            reply_markup=get_help_keyboard(),
            parse_mode="HTML"
        )

    except Exception as e:
        log_error(user_id, f"Ошибка в команде /help: {e}", module_name='basic_handlers')
        await message.answer("❌ Ошибка получения справки")

@router.message(Command("status"))
async def cmd_status(message: Message, state: FSMContext):
    """Обработчик команды /status"""
    user_id = message.from_user.id

    try:
        await basic_handler.log_command_usage(user_id, "status")

        # Получаем статус сессии
        session_status = await redis_manager.get_user_session(user_id)
        user_config = await redis_manager.get_config(user_id, ConfigType.GLOBAL)

        if not session_status:
            status_text = (
                f"🔴 <b>Статус: Неактивен</b>\n\n"
                f"Торговая сессия не запущена.\n"
                f"Используйте /autotrade_start для запуска торговли."
            )
        else:
            # ИСПРАВЛЕНО: Ключ 'is_active' заменен на 'running' в соответствии с user_session.py
            is_active = session_status.get('running', False)
            active_strategies = session_status.get('active_strategies', [])
            last_activity = session_status.get('last_activity')

            status_emoji = "🟢" if is_active else "🔴"
            status_name = "Активен" if is_active else "Неактивен"

            status_text = (
                f"{status_emoji} <b>Статус: {status_name}</b>\n\n"
                f"📊 <b>Активных стратегий:</b> {len(active_strategies)}\n"
            )

            if active_strategies:
                # обработка списка стратегий
                status_text += f"🔄 <b>Стратегии:</b> {', '.join(active_strategies)}\n"

            if last_activity:
                status_text += f"⏰ <b>Последняя активность:</b> {last_activity}\n"

            # Добавляем информацию о настройках риска
            if user_config:
                status_text += (
                    f"\n🛡️ <b>Настройки риска:</b>\n"
                    f"🎯 Риск на сделку: {user_config.get('risk_per_trade_percent', 2)}%\n"
                    f"📉 Макс. просадка: {user_config.get('global_daily_drawdown_percent', 10)}%\n"
                    f"📊 Макс. сделок: {user_config.get('max_concurrent_trades', 3)}\n"
                )

        await message.answer(
            status_text,
            reply_markup=get_quick_actions_keyboard(session_status.get('running', False) if session_status else False),
            parse_mode="HTML"
        )

    except Exception as e:
        log_error(user_id, f"Ошибка в команде /status: {e}", module_name='basic_handlers')
        await message.answer("❌ Ошибка получения статуса")



@router.message(Command("orders"))
async def cmd_orders(message: Message, state: FSMContext):
    """Обработчик команды /orders с поддержкой multi-account режима"""
    user_id = message.from_user.id
    await basic_handler.log_command_usage(user_id, "orders")

    try:
        exchange_config = system_config.get_exchange_config("bybit")
        use_demo = exchange_config.demo if exchange_config else False

        # === ПРОВЕРКА MULTI-ACCOUNT РЕЖИМА ===
        all_api_keys = await db_manager.get_all_user_api_keys(user_id, "bybit")

        if not all_api_keys or len(all_api_keys) == 0:
            await message.answer("⚠️ API ключи не настроены. Не могу получить список ордеров.")
            return

        # === MULTI-ACCOUNT РЕЖИМ (3 аккаунта) ===
        if len(all_api_keys) == 3:
            log_info(user_id, "Получение ордеров в multi-account режиме (3 аккаунта)", "orders")

            all_orders = []  # Все ордера со всех аккаунтов

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
                    log_error(user_id, f"Ошибка получения ордеров для аккаунта {priority}: {account_error}", "orders")

            if not all_orders:
                await message.answer("✅ У вас нет открытых ордеров на всех аккаунтах.")
                return

            # Формируем сообщение для multi-account режима
            orders_text = "📋 <b>ОТКРЫТЫЕ ОРДЕРА (Multi-Account Режим)</b>\n"
            orders_text += "═" * 35 + "\n\n"

            # Группируем ордера по ботам
            priority_names = {1: "PRIMARY", 2: "SECONDARY", 3: "TERTIARY"}
            priority_emojis = {1: "🥇", 2: "🥈", 3: "🥉"}

            for priority in [1, 2, 3]:
                bot_orders = [o for o in all_orders if o['_bot_priority'] == priority]

                if bot_orders:
                    name = priority_names.get(priority, f"Бот {priority}")
                    emoji = priority_emojis.get(priority, "🔹")

                    orders_text += f"{emoji} <b>{name} (Бот {priority})</b>\n"
                    orders_text += "─" * 30 + "\n"

                    for order in bot_orders:
                        side_emoji = "🟢" if order['side'] == 'Buy' else "🔴"
                        orders_text += f"\n<b>{order['symbol']}</b> | {side_emoji} {order['side']}\n"
                        orders_text += f"  • Тип: {order['orderType']}\n"
                        orders_text += f"  • Кол-во: {order['qty']}\n"
                        orders_text += f"  • Цена: {format_currency(order['price'])}\n"
                        orders_text += f"  • Статус: {order['orderStatus']}\n"

                    orders_text += "\n"

            # Агрегированная статистика
            orders_text += "═" * 35 + "\n"
            orders_text += f"🌟 <b>ИТОГО:</b> {len(all_orders)} ордеров\n"

            await message.answer(orders_text, parse_mode="HTML")

        # === ОБЫЧНЫЙ РЕЖИМ (1 аккаунт) ===
        else:
            log_info(user_id, "Получение ордеров в обычном режиме (1 аккаунт)", "orders")

            # Получаем PRIMARY ключ
            keys = await db_manager.get_api_keys(user_id, "bybit", account_priority=1)
            if not keys:
                await message.answer("⚠️ API ключи не настроены. Не могу получить список ордеров.")
                return

            async with BybitAPI(user_id=user_id, api_key=keys[0], api_secret=keys[1], demo=use_demo) as api:
                orders = await api.get_open_orders()

            if not orders:
                await message.answer("✅ У вас нет открытых ордеров.")
                return

            orders_text = "📋 <b>Открытые ордера:</b>\n\n"
            for order in orders:
                side_emoji = "🟢" if order['side'] == 'Buy' else "🔴"
                orders_text += (
                    f"<b>{order['symbol']}</b> | {side_emoji} {order['side']}\n"
                    f"  - <b>Тип:</b> {order['orderType']}\n"
                    f"  - <b>Кол-во:</b> {order['qty']}\n"
                    f"  - <b>Цена:</b> {format_currency(order['price'])}\n"
                    f"  - <b>Статус:</b> {order['orderStatus']}\n\n"
                )

            await message.answer(orders_text, parse_mode="HTML")

    except Exception as e:
        log_error(user_id, f"Ошибка получения ордеров: {e}", module_name='basic_handlers')
        await message.answer("❌ Произошла ошибка при запросе открытых ордеров.")


@router.message(Command("stats"))
async def cmd_stats(message: Message, state: FSMContext):
    """Шаг 1: Предлагает пользователю выбрать период для статистики."""
    user_id = message.from_user.id
    await basic_handler.log_command_usage(user_id, "stats")

    # Создаем клавиатуру для выбора периода
    period_buttons = [
        [
            {"text": "📊 За сутки", "callback_data": "stats_period_day"},
            {"text": "🗓 За месяц", "callback_data": "stats_period_month_select"}
        ],
        [{"text": "📈 За всё время", "callback_data": "stats_period_all"}]
    ]
    keyboard = KeyboardBuilder.build_keyboard(period_buttons)

    await message.answer(
        "⏳ <b>Выберите период для отображения статистики:</b>",
        reply_markup=keyboard,
        parse_mode="HTML"
    )


@router.message(Command("settings"))
async def cmd_settings(message: Message, state: FSMContext):
    """Обработчик команды /settings"""
    user_id = message.from_user.id
    try:
        # !!! ГЛАВНЫЙ ФИКС ПРОТИВ "ЗАСТРЕВАНИЯ" !!!
        await state.clear()

        await basic_handler.log_command_usage(user_id, "settings")
        await state.set_state(UserStates.SETTINGS_MENU)

        text = (
            "⚙️ <b>Настройки бота</b>\n\n"
            "Здесь вы можете управлять всеми аспектами работы бота, от управления рисками до параметров конкретных стратегий."
        )
        await message.answer(
            text,
            reply_markup=get_settings_keyboard(),
            parse_mode="HTML"
        )
    except Exception as e:
        log_error(user_id, f"Ошибка в команде /settings: {e}", module_name='basic_handlers')
        await message.answer("❌ Ошибка открытия настроек")


# --- Команды управления торговлей ---


@router.message(Command("autotrade_start"))
async def cmd_autotrade_start(message: Message, state: FSMContext):
    """Обработчик команды /autotrade_start"""
    user_id = message.from_user.id
    await basic_handler.log_command_usage(user_id, "autotrade_start")

    # ИСПРАВЛЕНО: Multi-Account Support - проверяем наличие хотя бы одного API ключа
    all_api_keys = await db_manager.get_all_user_api_keys(user_id, "bybit")
    if not all_api_keys or len(all_api_keys) == 0:
        await message.answer(
            "⚠️ <b>API ключи не настроены.</b>\nПерейдите в 'Настройки' -> 'API ключи' для их добавления.",
            parse_mode="HTML")
        return

    session_status = await redis_manager.get_user_session(user_id)
    if session_status and session_status.get('running', False):
        await message.answer("✅ Торговля уже запущена.")
        return

    # Отправляем только первое сообщение
    await message.answer(
        "🚀 <b>Запускаю автоматическую торговлю...</b>\nСистема инициализирует сессию и подключается к рынку. Вы получите уведомление по завершении.",
        parse_mode="HTML")

    # Отправляем событие в шину
    if basic_handler.event_bus:
        await basic_handler.event_bus.publish(UserSessionStartRequestedEvent(user_id=user_id))
    else:
        log_error(user_id, "EventBus не инициализирован для отправки команды запуска торговли", module_name='basic_handlers')
        await message.answer("❌ Внутренняя ошибка системы. Попробуйте позже.")


@router.message(Command("autotrade_stop"))
async def cmd_autotrade_stop(message: Message, state: FSMContext):
    """Обработчик команды /autotrade_stop - умная остановка с ожиданием закрытия ордеров"""
    user_id = message.from_user.id
    await basic_handler.log_command_usage(user_id, "autotrade_stop")

    session_status = await redis_manager.get_user_session(user_id)
    # Проверяем флаг, который реально управляет торговлей
    if not session_status or not session_status.get('running', False):
        await message.answer("🔴 Торговля и так неактивна.")
        return

    # Получаем информацию о текущих позициях и ордерах
    try:
        # Определяем режим торговли (demo/live)
        exchange_config = system_config.get_exchange_config("bybit")
        use_demo = exchange_config.demo if exchange_config else False

        # === ПРОВЕРКА MULTI-ACCOUNT РЕЖИМА ===
        all_api_keys = await db_manager.get_all_user_api_keys(user_id, "bybit")

        if not all_api_keys or len(all_api_keys) == 0:
            await message.answer("❌ API ключи не найдены.")
            return

        from api.bybit_api import BybitAPI

        # === MULTI-ACCOUNT РЕЖИМ (3 аккаунта) ===
        if len(all_api_keys) == 3:
            log_info(user_id, "Остановка торговли в multi-account режиме (3 аккаунта)", "autotrade_stop")

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
                                if float(pos.get('size', 0)) != 0:
                                    pos['_bot_priority'] = priority
                                    all_positions.append(pos)

                        if orders:
                            for order in orders:
                                if order.get('orderStatus') in ['New', 'PartiallyFilled']:
                                    order['_bot_priority'] = priority
                                    all_orders.append(order)

                except Exception as account_error:
                    log_error(user_id, f"Ошибка получения данных для аккаунта {priority}: {account_error}", "autotrade_stop")

            total_active = len(all_positions) + len(all_orders)

            if total_active == 0:
                # Нет активных позиций/ордеров - можно остановить сразу
                await basic_handler.event_bus.publish(UserSessionStopRequestedEvent(user_id=user_id, reason="manual_stop_command"))
                await message.answer("✅ <b>Торговля остановлена</b>\n\nАктивных позиций и ордеров не обнаружено на всех аккаунтах.", parse_mode="HTML")
                return
            else:
                # Есть активные позиции/ордера - показываем детальную информацию
                position_info = f"🌟 <b>MULTI-ACCOUNT РЕЖИМ</b>\n\n"

                if all_positions:
                    position_info += f"📈 Всего открытых позиций: {len(all_positions)}\n"
                    # Группируем по ботам
                    for priority in [1, 2, 3]:
                        bot_positions = [p for p in all_positions if p.get('_bot_priority') == priority]
                        if bot_positions:
                            priority_names = {1: "PRIMARY", 2: "SECONDARY", 3: "TERTIARY"}
                            position_info += f"  • {priority_names[priority]}: {len(bot_positions)} поз.\n"

                if all_orders:
                    position_info += f"\n📋 Всего активных ордеров: {len(all_orders)}\n"
                    # Группируем по ботам
                    for priority in [1, 2, 3]:
                        bot_orders = [o for o in all_orders if o.get('_bot_priority') == priority]
                        if bot_orders:
                            priority_names = {1: "PRIMARY", 2: "SECONDARY", 3: "TERTIARY"}
                            position_info += f"  • {priority_names[priority]}: {len(bot_orders)} орд.\n"

                await message.answer(
                    f"🛑 <b>Останавливаю автоторговлю...</b>\n\n"
                    f"❗️ Обнаружены незакрытые позиции/ордера:\n\n"
                    f"{position_info}"
                    f"\n🔄 <b>Ожидаю завершения всех операций</b>\n"
                    f"Новые сделки запрещены, текущие доводятся до результата.",
                    parse_mode="HTML"
                )

                # Отправляем событие остановки стратегий
                await basic_handler.event_bus.publish(UserSessionStopRequestedEvent(user_id=user_id, reason="manual_stop_command"))

                # Мониторим закрытие позиций/ордеров (передаем все ключи)
                await _monitor_pending_trades_multi(user_id, message, all_api_keys, use_demo)

        # === ОБЫЧНЫЙ РЕЖИМ (1 аккаунт) ===
        else:
            log_info(user_id, "Остановка торговли в обычном режиме (1 аккаунт)", "autotrade_stop")

            user_api_keys = await db_manager.get_api_keys(user_id, "bybit", account_priority=1)
            if not user_api_keys:
                await message.answer("❌ API ключи не найдены.")
                return

            async with BybitAPI(
                user_id=user_id,
                api_key=user_api_keys[0],
                api_secret=user_api_keys[1],
                demo=use_demo
            ) as api:
                # Получаем открытые позиции и ордера
                positions = await api.get_positions()
                open_orders = await api.get_open_orders()

                # Подсчитываем активные позиции и ордера
                active_positions = []
                active_orders = []

                if positions:
                    active_positions = [pos for pos in positions if float(pos.get('size', 0)) != 0]

                if open_orders:
                    active_orders = [order for order in open_orders if order.get('orderStatus') in ['New', 'PartiallyFilled']]

                total_active = len(active_positions) + len(active_orders)

                if total_active == 0:
                    # Нет активных позиций/ордеров - можно остановить сразу
                    await basic_handler.event_bus.publish(UserSessionStopRequestedEvent(user_id=user_id, reason="manual_stop_command"))
                    await message.answer("✅ <b>Торговля остановлена</b>\n\nАктивных позиций и ордеров не обнаружено.", parse_mode="HTML")
                    return
                else:
                    # Есть активные позиции/ордера - останавливаем стратегии но ждём закрытия
                    position_info = ""
                    if active_positions:
                        position_info += f"📈 Открытых позиций: {len(active_positions)}\n"
                    if active_orders:
                        position_info += f"📋 Активных ордеров: {len(active_orders)}\n"

                    await message.answer(
                        f"🛑 <b>Останавливаю автоторговлю...</b>\n\n"
                        f"❗️ Обнаружены незакрытые позиции/ордера:\n"
                        f"{position_info}"
                        f"\n🔄 <b>Ожидаю завершения всех операций</b>\n"
                        f"Новые сделки запрещены, текущие доводятся до результата.",
                        parse_mode="HTML"
                    )

                    # Отправляем событие остановки стратегий
                    await basic_handler.event_bus.publish(UserSessionStopRequestedEvent(user_id=user_id, reason="manual_stop_command"))

                    # Мониторим закрытие позиций/ордеров
                    await _monitor_pending_trades(user_id, message, api)

    except Exception as e:
        log_error(user_id, f"Ошибка при проверке позиций для умной остановки: {e}", module_name='basic_handlers')
        # Fallback к стандартной остановке
        await basic_handler.event_bus.publish(UserSessionStopRequestedEvent(user_id=user_id, reason="manual_stop_command"))
        await message.answer(
            "🛑 <b>Останавливаю автоматическую торговлю...</b>\nСистема завершит текущие операции и сохранит статистику.",
            parse_mode="HTML"
        )


async def _monitor_pending_trades(user_id: int, message: Message, api):
    """Мониторинг незакрытых позиций и ордеров"""
    last_update_time = 0
    update_interval = 300  # Обновляем сообщение каждые 5 минут
    start_time = time.time()

    while True:
        try:
            current_time = time.time()

            # Проверяем активные позиции и ордера
            positions = await api.get_positions()
            open_orders = await api.get_open_orders()

            active_positions = []
            active_orders = []

            if positions:
                active_positions = [pos for pos in positions if float(pos.get('size', 0)) != 0]

            if open_orders:
                active_orders = [order for order in open_orders if order.get('orderStatus') in ['New', 'PartiallyFilled']]

            total_active = len(active_positions) + len(active_orders)

            # Если всё закрыто - завершаем
            if total_active == 0:
                await message.answer(
                    "✅ <b>Все позиции и ордера завершены</b>\n\n"
                    "🛑 <b>Автоторговля полностью остановлена</b>",
                    parse_mode="HTML"
                )
                return

            # Обновляем статус каждые 5 минут
            if current_time - last_update_time >= update_interval:
                elapsed_minutes = int((current_time - start_time) / 60)
                elapsed_hours = elapsed_minutes // 60
                elapsed_mins_remainder = elapsed_minutes % 60

                status_text = f"⏳ <b>Ожидание завершения операций</b>\n\n"

                if active_positions:
                    status_text += f"📈 Открытых позиций: {len(active_positions)}\n"
                    # Показываем детали по символам
                    symbol_summary = {}
                    for pos in active_positions:
                        symbol = pos.get('symbol', 'Unknown')
                        side = pos.get('side', 'Unknown')
                        unrealized_pnl = float(pos.get('unrealisedPnl', 0))

                        if symbol not in symbol_summary:
                            symbol_summary[symbol] = {'long': 0, 'short': 0, 'pnl': 0}

                        if side.lower() == 'buy':
                            symbol_summary[symbol]['long'] += 1
                        else:
                            symbol_summary[symbol]['short'] += 1
                        symbol_summary[symbol]['pnl'] += unrealized_pnl

                    for symbol, data in symbol_summary.items():
                        pnl_emoji = "🟢" if data['pnl'] >= 0 else "🔴"
                        status_text += f"  • {symbol}: {data['long']}L/{data['short']}S {pnl_emoji}{data['pnl']:.2f}$\n"

                if active_orders:
                    status_text += f"📋 Активных ордеров: {len(active_orders)}\n"

                # Красивое отображение времени
                if elapsed_hours > 0:
                    status_text += f"\n⏰ Ожидание: {elapsed_hours}ч {elapsed_mins_remainder}мин"
                else:
                    status_text += f"\n⏰ Ожидание: {elapsed_minutes} мин"

                status_text += f"\n\n💡 <i>Сделки доводятся до естественного завершения</i>"

                try:
                    await message.answer(status_text, parse_mode="HTML")
                except Exception:
                    # Игнорируем ошибки отправки обновлений
                    pass

                last_update_time = current_time

            await asyncio.sleep(30)  # Проверяем каждые 30 секунд

        except Exception as e:
            log_error(user_id, f"Ошибка мониторинга позиций: {e}", module_name='basic_handlers')
            await asyncio.sleep(60)  # При ошибке ждём минуту


async def _monitor_pending_trades_multi(user_id: int, message: Message, all_api_keys: list, use_demo: bool):
    """
    Мониторинг незакрытых позиций и ордеров для MULTI-ACCOUNT режима (3 аккаунта).

    Args:
        user_id: ID пользователя
        message: Сообщение для отправки обновлений
        all_api_keys: Список всех API ключей (PRIMARY, SECONDARY, TERTIARY)
        use_demo: Флаг демо-режима
    """
    last_update_time = 0
    update_interval = 300  # Обновляем сообщение каждые 5 минут
    start_time = time.time()

    while True:
        try:
            current_time = time.time()

            # Собираем позиции и ордера со всех 3 аккаунтов
            all_positions = []
            all_orders = []

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
                                if float(pos.get('size', 0)) != 0:
                                    pos['_bot_priority'] = priority
                                    all_positions.append(pos)

                        if orders:
                            for order in orders:
                                if order.get('orderStatus') in ['New', 'PartiallyFilled']:
                                    order['_bot_priority'] = priority
                                    all_orders.append(order)

                except Exception as account_error:
                    log_error(user_id, f"Ошибка мониторинга аккаунта {priority}: {account_error}", "monitor_multi")

            total_active = len(all_positions) + len(all_orders)

            # Если всё закрыто на всех аккаунтах - завершаем
            if total_active == 0:
                await message.answer(
                    "✅ <b>Все позиции и ордера завершены на всех аккаунтах</b>\n\n"
                    "🛑 <b>Автоторговля полностью остановлена</b>",
                    parse_mode="HTML"
                )
                return

            # Обновляем статус каждые 5 минут
            if current_time - last_update_time >= update_interval:
                elapsed_minutes = int((current_time - start_time) / 60)
                elapsed_hours = elapsed_minutes // 60
                elapsed_mins_remainder = elapsed_minutes % 60

                status_text = f"⏳ <b>Ожидание завершения операций (Multi-Account)</b>\n\n"

                # Группируем по ботам
                priority_names = {1: "PRIMARY", 2: "SECONDARY", 3: "TERTIARY"}
                priority_emojis = {1: "🥇", 2: "🥈", 3: "🥉"}

                for priority in [1, 2, 3]:
                    bot_positions = [p for p in all_positions if p.get('_bot_priority') == priority]
                    bot_orders = [o for o in all_orders if o.get('_bot_priority') == priority]

                    if bot_positions or bot_orders:
                        name = priority_names.get(priority, f"Бот {priority}")
                        emoji = priority_emojis.get(priority, "🔹")

                        status_text += f"{emoji} <b>{name}:</b>\n"

                        if bot_positions:
                            status_text += f"  📈 Позиций: {len(bot_positions)}\n"

                            # Детали по символам
                            symbol_summary = {}
                            for pos in bot_positions:
                                symbol = pos.get('symbol', 'Unknown')
                                side = pos.get('side', 'Unknown')
                                unrealized_pnl = float(pos.get('unrealisedPnl', 0))

                                if symbol not in symbol_summary:
                                    symbol_summary[symbol] = {'long': 0, 'short': 0, 'pnl': 0}

                                if side.lower() == 'buy':
                                    symbol_summary[symbol]['long'] += 1
                                else:
                                    symbol_summary[symbol]['short'] += 1
                                symbol_summary[symbol]['pnl'] += unrealized_pnl

                            for symbol, data in symbol_summary.items():
                                pnl_emoji = "🟢" if data['pnl'] >= 0 else "🔴"
                                symbol_short = symbol.replace('USDT', '')
                                status_text += f"    • {symbol_short}: {data['long']}L/{data['short']}S {pnl_emoji}{data['pnl']:.2f}$\n"

                        if bot_orders:
                            status_text += f"  📋 Ордеров: {len(bot_orders)}\n"

                        status_text += "\n"

                # Общая статистика
                status_text += f"🌟 <b>ИТОГО:</b> {len(all_positions)} поз. + {len(all_orders)} орд.\n"

                # Красивое отображение времени
                if elapsed_hours > 0:
                    status_text += f"⏰ Ожидание: {elapsed_hours}ч {elapsed_mins_remainder}мин\n"
                else:
                    status_text += f"⏰ Ожидание: {elapsed_minutes} мин\n"

                status_text += f"\n💡 <i>Сделки доводятся до естественного завершения</i>"

                try:
                    await message.answer(status_text, parse_mode="HTML")
                except Exception:
                    # Игнорируем ошибки отправки обновлений
                    pass

                last_update_time = current_time

            await asyncio.sleep(30)  # Проверяем каждые 30 секунд

        except Exception as e:
            log_error(user_id, f"Ошибка в monitor_multi для user {user_id}: {e}", module_name='basic_handlers')
            await asyncio.sleep(60)  # При ошибке ждём минуту


@router.message(Command("autotrade_status"))
async def cmd_autotrade_status(message: Message, state: FSMContext):
    """Расширенный обработчик команды /autotrade_status с детальной информацией"""
    user_id = message.from_user.id
    await basic_handler.log_command_usage(user_id, "autotrade_status")

    try:
        # Получаем сессию пользователя
        session_status = await redis_manager.get_user_session(user_id)

        # ИСПРАВЛЕНО: Если сессии нет или running=False - показываем неактивна
        if not session_status or not session_status.get('running', False):
            await message.answer("🔴 <b>Статус: Неактивен</b>\nТорговля не запущена.", parse_mode="HTML")
            return

        is_active = session_status.get('running', False)
        active_strategies = session_status.get('active_strategies', [])

        # Начинаем формировать статус
        status_text = "📊 <b>СТАТУС АВТОТОРГОВЛИ</b>\n"
        status_text += "═" * 25 + "\n\n"

        # Общий статус
        status_icon = "🟢" if is_active else "🔴"
        status_text += f"🔘 <b>Автоторговля:</b> {status_icon} {'Активна' if is_active else 'Неактивна'}\n\n"

        if not is_active:
            status_text += "ℹ️ Для запуска торговли используйте /autotrade_start"
            await message.answer(status_text, parse_mode="HTML")
            return

        if not active_strategies:
            status_text += "⚠️ <b>Активных стратегий нет</b>\n"
            status_text += "Проверьте настройки символов в /settings"
            await message.answer(status_text, parse_mode="HTML")
            return

        # Получаем настройки пользователя для сравнения с активными стратегиями
        user_config = await redis_manager.get_config(user_id, ConfigType.GLOBAL)
        configured_symbols = set()
        inactive_strategies = {}

        if user_config:
            # Получаем список символов из настроек пользователя
            watchlist = user_config.get('watchlist_symbols', [])
            for symbol in watchlist:
                configured_symbols.add(symbol)

            # Проверяем, какие стратегии должны быть активны, но не запущены
            strategy_configs = [
                (ConfigType.STRATEGY_SIGNAL_SCALPER, "SIGNAL_SCALPER"),
                (ConfigType.STRATEGY_FLASH_DROP_CATCHER, "FLASH_DROP_CATCHER")
            ]

            for config_type, strategy_name in strategy_configs:
                strategy_config = await redis_manager.get_config(user_id, config_type)
                if strategy_config and strategy_config.get('enabled', False):
                    # Стратегия включена в настройках, проверяем какие символы не активны
                    for symbol in watchlist:
                        strategy_id = f"{strategy_name}_{symbol}"
                        if strategy_id not in active_strategies:
                            if strategy_name not in inactive_strategies:
                                inactive_strategies[strategy_name] = []
                            inactive_strategies[strategy_name].append(symbol)

        # === ПОЛУЧАЕМ ПОЗИЦИИ ПО ВСЕМ АККАУНТАМ (MULTI-ACCOUNT SUPPORT) ===
        all_api_keys = await db_manager.get_all_user_api_keys(user_id, "bybit")
        positions_data = {}  # {symbol: {aggregated data from all accounts}}

        if all_api_keys:
            try:
                # Определяем режим торговли (demo/live)
                exchange_config = system_config.get_exchange_config("bybit")
                use_demo = exchange_config.demo if exchange_config else False

                # === MULTI-ACCOUNT РЕЖИМ (3 аккаунта) - агрегируем позиции ===
                if len(all_api_keys) == 3:
                    log_info(user_id, "Получение позиций в autotrade_status (multi-account режим)", "autotrade_status")

                    # Собираем позиции со всех 3 аккаунтов
                    for key_data in sorted(all_api_keys, key=lambda x: x['priority']):
                        priority = key_data['priority']
                        try:
                            async with BybitAPI(
                                user_id=user_id,
                                api_key=key_data['api_key'],
                                api_secret=key_data['secret_key'],
                                demo=use_demo
                            ) as api:
                                account_positions = await api.get_positions()

                                if account_positions:
                                    for pos in account_positions:
                                        symbol = pos.get('symbol', '')
                                        size = float(pos.get('size', 0))
                                        if size != 0:  # Только активные позиции
                                            # Агрегируем позиции по символу (суммируем PnL)
                                            if symbol not in positions_data:
                                                positions_data[symbol] = {
                                                    'side': pos.get('side', ''),
                                                    'size': 0,
                                                    'unrealizedPnl': 0,
                                                    'avgPrice': float(pos.get('avgPrice', 0)),
                                                    'markPrice': float(pos.get('markPrice', 0)),
                                                    'accounts': []
                                                }
                                            # Суммируем размер и PnL
                                            positions_data[symbol]['size'] += size
                                            positions_data[symbol]['unrealizedPnl'] += float(pos.get('unrealisedPnl', 0))
                                            positions_data[symbol]['accounts'].append(priority)

                        except Exception as account_error:
                            log_warning(user_id, f"Ошибка получения позиций для аккаунта {priority}: {account_error}", "autotrade_status")

                # === ОБЫЧНЫЙ РЕЖИМ (1 аккаунт) ===
                else:
                    log_info(user_id, "Получение позиций в autotrade_status (обычный режим)", "autotrade_status")

                    api_keys = await db_manager.get_api_keys(user_id, "bybit", account_priority=1)
                    if api_keys:
                        async with BybitAPI(
                            user_id=user_id,
                            api_key=api_keys[0],
                            api_secret=api_keys[1],
                            demo=use_demo
                        ) as api:
                            # Получаем все позиции пользователя
                            all_positions = await api.get_positions()
                            if all_positions:
                                for pos in all_positions:
                                    symbol = pos.get('symbol', '')
                                    size = float(pos.get('size', 0))
                                    if size != 0:  # Только активные позиции (и лонги, и шорты)
                                        positions_data[symbol] = {
                                            'side': pos.get('side', ''),
                                            'size': size,
                                            'unrealizedPnl': float(pos.get('unrealisedPnl', 0)),
                                            'avgPrice': float(pos.get('avgPrice', 0)),
                                            'markPrice': float(pos.get('markPrice', 0))
                                        }
            except Exception as e:
                log_warning(user_id, f"Не удалось получить данные позиций: {e}", "autotrade_status")

        # Группируем стратегии по типам
        strategies_by_type = {}
        for strategy_id in active_strategies:
            try:
                # Парсим strategy_id: "SIGNAL_SCALPER_SOLUSDT" -> ("SIGNAL_SCALPER", "SOLUSDT")
                parts = strategy_id.split('_')
                if len(parts) >= 3:
                    strategy_type = '_'.join(parts[:-1])  # SIGNAL_SCALPER
                    symbol = parts[-1]  # SOLUSDT

                    if strategy_type not in strategies_by_type:
                        strategies_by_type[strategy_type] = []
                    strategies_by_type[strategy_type].append(symbol)
            except Exception as e:
                log_warning(user_id, f"Ошибка парсинга strategy_id {strategy_id}: {e}", "autotrade_status")

        # Отображаем информацию по стратегиям
        for strategy_type, symbols in strategies_by_type.items():
            # Переводим название стратегии
            if strategy_type == "SIGNAL_SCALPER":
                display_name = "📈 Signal Scalper"
            elif strategy_type == "FLASH_DROP_CATCHER":
                display_name = "🚀 Flash Drop Catcher"
            else:
                display_name = f"🔧 {strategy_type.replace('_', ' ').title()}"

            status_text += f"<b>{display_name}</b>\n"

            for symbol in symbols:
                symbol_short = symbol.replace('USDT', '')  # SOLUSDT -> SOL
                status_text += f"  ▫️ <b>{symbol_short}:</b> "

                # Проверяем состояние позиции
                if symbol in positions_data:
                    pos = positions_data[symbol]
                    pnl = pos['unrealizedPnl']

                    if pnl > 0:
                        status_text += f"🟢 В прибыли +${pnl:.2f}"
                    elif pnl < 0:
                        status_text += f"🔴 В убытке ${pnl:.2f}"
                    else:
                        status_text += f"⚪ Без изменений (${pnl:.2f})"

                    # Добавляем информацию о позиции
                    side_icon = "📈" if pos['side'] == 'Buy' else "📉"
                    status_text += f"\n     {side_icon} {pos['side']} {pos['size']}, "
                    status_text += f"вход: ${pos['avgPrice']:.4f}"

                else:
                    status_text += "⏳ В ожидании сигнала"

                status_text += "\n"

            status_text += "\n"

        # Добавляем информацию о неактивных (отключенных) стратегиях
        if inactive_strategies:
            status_text += "⚫ <b>ОТКЛЮЧЕННЫЕ СТРАТЕГИИ</b>\n"
            status_text += "─" * 20 + "\n"

            for strategy_type, symbols in inactive_strategies.items():
                # Переводим название стратегии
                if strategy_type == "SIGNAL_SCALPER":
                    display_name = "📈 Signal Scalper"
                elif strategy_type == "FLASH_DROP_CATCHER":
                    display_name = "🚀 Flash Drop Catcher"
                else:
                    display_name = f"🔧 {strategy_type.replace('_', ' ').title()}"

                status_text += f"<b>{display_name}</b>\n"

                for symbol in symbols:
                    symbol_short = symbol.replace('USDT', '')  # SOLUSDT -> SOL
                    status_text += f"  ▫️ <b>{symbol_short}:</b> 🔴 Отключена пользователем\n"

                status_text += "\n"

            status_text += "ℹ️ <i>Для включения перейдите в /settings</i>\n\n"

        # Добавляем информацию о времени обновления
        from datetime import datetime, timezone, timedelta
        moscow_tz = timezone(timedelta(hours=3))
        current_time = datetime.now(moscow_tz).strftime('%H:%M:%S')
        status_text += f"🕐 Обновлено: {current_time} МСК"

        await message.answer(status_text, parse_mode="HTML")

    except Exception as e:
        log_error(user_id, f"Ошибка в команде /autotrade_status: {e}", "autotrade_status")
        await message.answer("❌ Произошла ошибка при получении статуса торговли.", parse_mode="HTML")




# --- Команды получения информации ---

@router.message(Command("balance"))
async def cmd_balance(message: Message, state: FSMContext):
    """Обработчик команды /balance с поддержкой multi-account режима"""
    user_id = message.from_user.id
    await basic_handler.log_command_usage(user_id, "balance")

    try:
        exchange_config = system_config.get_exchange_config("bybit")
        use_demo = exchange_config.demo if exchange_config else False

        # === ПРОВЕРКА MULTI-ACCOUNT РЕЖИМА ===
        all_api_keys = await db_manager.get_all_user_api_keys(user_id, "bybit")

        if not all_api_keys or len(all_api_keys) == 0:
            await message.answer("⚠️ API ключи не настроены. Не могу получить баланс.")
            return

        # === MULTI-ACCOUNT РЕЖИМ (3 аккаунта) ===
        if len(all_api_keys) == 3:
            log_info(user_id, "Получение баланса в multi-account режиме (3 аккаунта)", "balance")

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
                        log_warning(user_id, f"Не удалось получить баланс для аккаунта {priority}", "balance")
                except Exception as account_error:
                    log_error(user_id, f"Ошибка получения баланса для аккаунта {priority}: {account_error}", "balance")

            if not accounts_data:
                await message.answer("❌ Не удалось получить баланс ни с одного аккаунта.")
                return

            # Формируем сообщение для multi-account режима
            balance_text = "💰 <b>БАЛАНС (Multi-Account Режим)</b>\n"
            balance_text += "═" * 30 + "\n\n"

            # Агрегированные данные
            pnl_emoji = "📈" if total_unrealised_pnl_sum >= 0 else "📉"
            balance_text += f"🌟 <b>ОБЩИЙ БАЛАНС ПО ВСЕМ АККАУНТАМ:</b>\n"
            balance_text += f"  • Капитал: {format_currency(total_equity_sum)}\n"
            balance_text += f"  • Доступно: {format_currency(total_available_sum)}\n"
            balance_text += f"  • PnL: {pnl_emoji} {format_currency(total_unrealised_pnl_sum)}\n\n"

            balance_text += "─" * 30 + "\n\n"

            # Детали по каждому аккаунту
            priority_names = {1: "PRIMARY", 2: "SECONDARY", 3: "TERTIARY"}
            priority_emojis = {1: "🥇", 2: "🥈", 3: "🥉"}

            for acc in accounts_data:
                priority = acc['priority']
                name = priority_names.get(priority, f"Бот {priority}")
                emoji = priority_emojis.get(priority, "🔹")
                pnl_emoji_acc = "📈" if acc['unrealised_pnl'] >= 0 else "📉"

                balance_text += f"{emoji} <b>{name} (Бот {priority})</b>\n"
                balance_text += f"  • Капитал: {format_currency(acc['equity'])}\n"
                balance_text += f"  • Доступно: {format_currency(acc['available'])}\n"
                balance_text += f"  • PnL: {pnl_emoji_acc} {format_currency(acc['unrealised_pnl'])}\n\n"

            await message.answer(balance_text, parse_mode="HTML")

        # === ОБЫЧНЫЙ РЕЖИМ (1 аккаунт) ===
        else:
            log_info(user_id, "Получение баланса в обычном режиме (1 аккаунт)", "balance")

            # Получаем PRIMARY ключ
            keys = await db_manager.get_api_keys(user_id, "bybit", account_priority=1)
            if not keys:
                await message.answer("⚠️ API ключи не настроены. Не могу получить баланс.")
                return

            async with BybitAPI(user_id=user_id, api_key=keys[0], api_secret=keys[1], demo=use_demo) as api:
                balance_data = await api.get_wallet_balance()

            if balance_data and 'totalEquity' in balance_data:
                total_equity = format_currency(balance_data['totalEquity'])
                available_balance = format_currency(balance_data['totalAvailableBalance'])
                unrealised_pnl = format_currency(balance_data['totalUnrealisedPnl'])

                pnl_emoji = "📈" if balance_data['totalUnrealisedPnl'] >= 0 else "📉"

                balance_text = (
                    f"💰 <b>Баланс аккаунта (Bybit)</b>\n\n"
                    f"<b>Общий капитал:</b> {total_equity}\n"
                    f"<b>Доступно для вывода:</b> {available_balance}\n"
                    f"<b>Нереализованный PnL:</b> {pnl_emoji} {unrealised_pnl}"
                )
                await message.answer(balance_text, parse_mode="HTML")
            else:
                await message.answer("❌ Не удалось получить данные о балансе. Проверьте права API ключей.")

    except Exception as e:
        log_error(user_id, f"Ошибка получения баланса: {e}", module_name='basic_handlers')
        await message.answer("❌ Произошла ошибка при запросе баланса.")


@router.message(Command("positions"))
async def cmd_positions(message: Message, state: FSMContext):
    """Обработчик команды /positions с поддержкой multi-account режима"""
    user_id = message.from_user.id
    await basic_handler.log_command_usage(user_id, "positions")

    try:
        exchange_config = system_config.get_exchange_config("bybit")
        use_demo = exchange_config.demo if exchange_config else False

        # === ПРОВЕРКА MULTI-ACCOUNT РЕЖИМА ===
        all_api_keys = await db_manager.get_all_user_api_keys(user_id, "bybit")

        if not all_api_keys or len(all_api_keys) == 0:
            await message.answer("⚠️ API ключи не настроены. Не могу получить позиции.")
            return

        # === MULTI-ACCOUNT РЕЖИМ (3 аккаунта) ===
        if len(all_api_keys) == 3:
            log_info(user_id, "Получение позиций в multi-account режиме (3 аккаунта)", "positions")

            all_positions = []  # Все позиции со всех аккаунтов

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
                            if float(pos.get('size', 0)) != 0:  # Только открытые позиции
                                pos['_bot_priority'] = priority  # Добавляем метку бота
                                all_positions.append(pos)
                except Exception as account_error:
                    log_error(user_id, f"Ошибка получения позиций для аккаунта {priority}: {account_error}", "positions")

            if not all_positions:
                await message.answer("✅ У вас нет открытых позиций на всех аккаунтах.")
                return

            # Формируем сообщение для multi-account режима
            positions_text = "📈 <b>ОТКРЫТЫЕ ПОЗИЦИИ (Multi-Account Режим)</b>\n"
            positions_text += "═" * 35 + "\n\n"

            # Группируем позиции по ботам
            priority_names = {1: "PRIMARY", 2: "SECONDARY", 3: "TERTIARY"}
            priority_emojis = {1: "🥇", 2: "🥈", 3: "🥉"}

            for priority in [1, 2, 3]:
                bot_positions = [p for p in all_positions if p['_bot_priority'] == priority]

                if bot_positions:
                    name = priority_names.get(priority, f"Бот {priority}")
                    emoji = priority_emojis.get(priority, "🔹")

                    positions_text += f"{emoji} <b>{name} (Бот {priority})</b>\n"
                    positions_text += "─" * 30 + "\n"

                    for pos in bot_positions:
                        symbol = pos['symbol']
                        side_emoji = "🟢 LONG" if pos['side'] == 'Buy' else "🔴 SHORT"
                        pnl_emoji = "📈" if pos['unrealisedPnl'] >= 0 else "📉"
                        pnl_value = float(pos['unrealisedPnl'])

                        # Определяем статус (для multi-account координатора)
                        status_emoji = ""
                        pnl_percent = (pnl_value / float(pos.get('avgPrice', 1)) / float(pos.get('size', 1))) * 100
                        if pnl_percent < -10:
                            status_emoji = " 🔴 STUCK"  # Застрял
                        elif pnl_value > 0:
                            status_emoji = " 🟢 ACTIVE"  # Активный в прибыли
                        else:
                            status_emoji = " 🟡 ACTIVE"  # Активный в убытке

                        positions_text += f"\n<b>{symbol}</b> | {side_emoji}{status_emoji}\n"
                        positions_text += f"  • Размер: {pos['size']} {pos.get('baseCoin', '')}\n"
                        positions_text += f"  • Вход: {format_currency(pos['avgPrice'])}\n"
                        positions_text += f"  • PnL: {pnl_emoji} {format_currency(pos['unrealisedPnl'])}\n"

                    positions_text += "\n"

            # Агрегированная статистика
            total_pnl = sum(float(p['unrealisedPnl']) for p in all_positions)
            total_pnl_emoji = "📈" if total_pnl >= 0 else "📉"

            positions_text += "═" * 35 + "\n"
            positions_text += f"🌟 <b>ИТОГО:</b>\n"
            positions_text += f"  • Всего позиций: {len(all_positions)}\n"
            positions_text += f"  • Общий PnL: {total_pnl_emoji} {format_currency(total_pnl)}\n"

            await message.answer(positions_text, parse_mode="HTML")

        # === ОБЫЧНЫЙ РЕЖИМ (1 аккаунт) ===
        else:
            log_info(user_id, "Получение позиций в обычном режиме (1 аккаунт)", "positions")

            # Получаем PRIMARY ключ
            keys = await db_manager.get_api_keys(user_id, "bybit", account_priority=1)
            if not keys:
                await message.answer("⚠️ API ключи не настроены. Не могу получить позиции.")
                return

            async with BybitAPI(user_id=user_id, api_key=keys[0], api_secret=keys[1], demo=use_demo) as api:
                positions = await api.get_positions()

            if not positions:
                await message.answer("✅ У вас нет открытых позиций.")
                return

            positions_text = "📈 <b>Открытые позиции:</b>\n\n"
            for pos in positions:
                side_emoji = "🟢 LONG" if pos['side'] == 'Buy' else "🔴 SHORT"
                pnl_emoji = "📈" if pos['unrealisedPnl'] >= 0 else "📉"

                positions_text += (
                    f"<b>{pos['symbol']}</b> | {side_emoji}\n"
                    f"  - <b>Размер:</b> {pos['size']} {pos.get('baseCoin', '')}\n"
                    f"  - <b>Цена входа:</b> {format_currency(pos['avgPrice'])}\n"
                    f"  - <b>PnL:</b> {pnl_emoji} {format_currency(pos['unrealisedPnl'])} ({format_percentage(pos.get('percentage', 0) * 100)})\n\n"
                )

            await message.answer(positions_text, parse_mode="HTML")

    except Exception as e:
        log_error(user_id, f"Ошибка получения позиций: {e}", module_name='basic_handlers')
        await message.answer("❌ Произошла ошибка при запросе позиций.")


@router.message(Command("stop_all"))
async def cmd_stop_all(message: Message, state: FSMContext):
    """Обработчик команды /stop_all (экстренная остановка с показом статистики)"""
    user_id = message.from_user.id
    await basic_handler.log_command_usage(user_id, "stop_all")

    # Получаем информацию о текущих позициях и стратегиях
    try:
        session_status = await redis_manager.get_user_session(user_id)
        if not session_status or not session_status.get('running', False):
            await message.answer("🔴 Торговля уже неактивна.")
            return

        # === ПОЛУЧАЕМ API КЛЮЧИ И ДАННЫЕ (MULTI-ACCOUNT SUPPORT) ===
        all_api_keys = await db_manager.get_all_user_api_keys(user_id, "bybit")

        if not all_api_keys or len(all_api_keys) == 0:
            await message.answer("❌ API ключи не найдены.")
            return

        # Получаем данные о позициях
        from api.bybit_api import BybitAPI

        # Определяем режим торговли (demo/live)
        exchange_config = system_config.get_exchange_config("bybit")
        use_demo = exchange_config.demo if exchange_config else False

        # Собираем позиции и ордера со ВСЕХ аккаунтов
        all_positions = []
        all_orders = []

        # === MULTI-ACCOUNT РЕЖИМ (3 аккаунта) - агрегируем данные ===
        if len(all_api_keys) == 3:
            log_info(user_id, "Экстренная остановка в multi-account режиме (3 аккаунта)", "stop_all")

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
                                if float(pos.get('size', 0)) != 0:
                                    pos['_bot_priority'] = priority
                                    all_positions.append(pos)

                        if orders:
                            for order in orders:
                                if order.get('orderStatus') in ['New', 'PartiallyFilled']:
                                    order['_bot_priority'] = priority
                                    all_orders.append(order)

                except Exception as account_error:
                    log_error(user_id, f"Ошибка получения данных для аккаунта {priority} (stop_all): {account_error}", "stop_all")

        # === ОБЫЧНЫЙ РЕЖИМ (1 аккаунт) ===
        else:
            log_info(user_id, "Экстренная остановка в обычном режиме (1 аккаунт)", "stop_all")

            user_api_keys = await db_manager.get_api_keys(user_id, "bybit", account_priority=1)
            if not user_api_keys:
                await message.answer("❌ API ключи не найдены.")
                return

            async with BybitAPI(
                user_id=user_id,
                api_key=user_api_keys[0],
                api_secret=user_api_keys[1],
                demo=use_demo
            ) as api:
                positions = await api.get_positions()
                orders = await api.get_open_orders()

                if positions:
                    all_positions = [pos for pos in positions if float(pos.get('size', 0)) != 0]

                if orders:
                    all_orders = [order for order in orders if order.get('orderStatus') in ['New', 'PartiallyFilled']]

        # === ФОРМИРУЕМ СООБЩЕНИЕ СО СТАТИСТИКОЙ ===
        warning_text = "🚨 <b>ЭКСТРЕННАЯ ОСТАНОВКА</b>\n"
        warning_text += "═" * 30 + "\n\n"
        warning_text += "⚠️ <b>ВНИМАНИЕ!</b> Все открытые позиции будут закрыты по рыночной цене.\n\n"

        # Анализируем позиции
        total_pnl = 0
        profitable_count = 0
        losing_count = 0

        if all_positions:
            # Для multi-account показываем группировку
            if len(all_api_keys) == 3:
                warning_text += f"📈 <b>Открытые позиции ({len(all_positions)}) - MULTI-ACCOUNT:</b>\n"

                priority_names = {1: "PRIMARY", 2: "SECONDARY", 3: "TERTIARY"}
                priority_emojis = {1: "🥇", 2: "🥈", 3: "🥉"}

                for priority in [1, 2, 3]:
                    bot_positions = [p for p in all_positions if p.get('_bot_priority') == priority]
                    if bot_positions:
                        emoji = priority_emojis.get(priority, "🔹")
                        warning_text += f"\n{emoji} <b>{priority_names[priority]}:</b>\n"

                        for pos in bot_positions:
                            symbol = pos.get('symbol', 'Unknown')
                            side = pos.get('side', 'Unknown')
                            size = float(pos.get('size', 0))
                            unrealized_pnl = float(pos.get('unrealisedPnl', 0))
                            total_pnl += unrealized_pnl

                            if unrealized_pnl >= 0:
                                profitable_count += 1
                                pnl_emoji = "🟢"
                                pnl_text = f"+${unrealized_pnl:.2f}"
                            else:
                                losing_count += 1
                                pnl_emoji = "🔴"
                                pnl_text = f"${unrealized_pnl:.2f}"

                            side_emoji = "📈" if side == 'Buy' else "📉"
                            symbol_short = symbol.replace('USDT', '')

                            warning_text += f"  • {symbol_short} {side_emoji} {size} {pnl_emoji} {pnl_text}\n"

            # Для обычного режима - простой список
            else:
                warning_text += f"📈 <b>Открытые позиции ({len(all_positions)}):</b>\n"

                for pos in all_positions:
                    symbol = pos.get('symbol', 'Unknown')
                    side = pos.get('side', 'Unknown')
                    size = float(pos.get('size', 0))
                    unrealized_pnl = float(pos.get('unrealisedPnl', 0))
                    total_pnl += unrealized_pnl

                    if unrealized_pnl >= 0:
                        profitable_count += 1
                        pnl_emoji = "🟢"
                        pnl_text = f"+${unrealized_pnl:.2f}"
                    else:
                        losing_count += 1
                        pnl_emoji = "🔴"
                        pnl_text = f"${unrealized_pnl:.2f}"

                    side_emoji = "📈" if side == 'Buy' else "📉"
                    symbol_short = symbol.replace('USDT', '')

                    warning_text += f"  • {symbol_short} {side_emoji} {size} {pnl_emoji} {pnl_text}\n"

            warning_text += f"\n💰 <b>Общий нереализованный PnL:</b> "
            if total_pnl >= 0:
                warning_text += f"🟢 +${total_pnl:.2f}\n"
            else:
                warning_text += f"🔴 ${total_pnl:.2f}\n"

            warning_text += f"📊 В прибыли: {profitable_count} | В убытке: {losing_count}\n\n"
        else:
            warning_text += "✅ Открытых позиций нет\n\n"

        # Проверяем открытые ордера
        if all_orders:
            warning_text += f"📋 <b>Активные ордера:</b> {len(all_orders)}\n\n"

        # Проверяем активные стратегии
        active_strategies = session_status.get('active_strategies', [])
        if active_strategies:
            warning_text += f"🔄 <b>Активных стратегий:</b> {len(active_strategies)}\n\n"

        # Добавляем предупреждение о последствиях
        warning_text += "⚠️ <b>Последствия экстренной остановки:</b>\n"
        warning_text += "• Все позиции закроются по рыночной цене\n"
        warning_text += "• Все ордера будут отменены\n"
        warning_text += "• Автоторговля будет остановлена\n"
        warning_text += "• Действие необратимо\n\n"

        if total_pnl < 0:
            warning_text += f"🚨 <b>Внимание:</b> Убыток составит ${abs(total_pnl):.2f}\n\n"

        warning_text += "Вы уверены, что хотите продолжить?"

        # Создаём специальную клавиатуру с ясным подтверждением
        emergency_buttons = [
            [
                {"text": "🚨 ДА, остановить немедленно", "callback_data": "confirm_emergency_stop"},
                {"text": "❌ НЕТ, отменить", "callback_data": "cancel_emergency_stop"}
            ]
        ]
        emergency_keyboard = KeyboardBuilder.build_keyboard(emergency_buttons)

        await message.answer(
            warning_text,
            parse_mode="HTML",
            reply_markup=emergency_keyboard
        )

    except Exception as e:
        log_error(user_id, f"Ошибка при подготовке экстренной остановки: {e}", module_name='basic_handlers')
        # Fallback к простому подтверждению
        await message.answer(
            "🚨 <b>ВНИМАНИЕ!</b>\nВы собираетесь экстренно остановить всю торговлю и закрыть все открытые позиции. Это действие необратимо.",
            parse_mode="HTML",
            reply_markup=get_confirmation_keyboard("emergency_stop")
        )


# Обработчик неизвестных команд
@router.message(StateFilter(None))
async def handle_unknown_message(message: Message, state: FSMContext):
    """Обработчик неизвестных сообщений, который не мешает FSM."""
    user_id = message.from_user.id
    try:
        log_info(user_id, f"Неизвестное сообщение: {message.text}", module_name='basic_handlers')

        await message.answer(
            "❓ <b>Неизвестная команда</b>\n\n"
            "Используйте /help для получения списка доступных команд\n"
            "или выберите действие в главном меню.",
            reply_markup=get_main_menu_keyboard(),
            parse_mode="HTML"
        )
    except Exception as e:
        log_error(user_id, f"Ошибка обработки неизвестного сообщения: {e}", module_name='basic_handlers')

# Функция для получения статистики команд
async def get_command_stats() -> Dict[str, int]:
    """Получение статистики использования команд"""
    return basic_handler.command_stats.copy()

