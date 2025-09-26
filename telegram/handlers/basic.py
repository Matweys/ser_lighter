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
from aiogram.utils.markdown import hbold



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
        is_active = session_data.get('autotrade_enabled', False) if session_data else False

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
                    f"📊 Макс. сделок: {user_config.get('max_simultaneous_trades', 3)}\n"
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
    """Обработчик команды /orders для отображения открытых ордеров"""
    user_id = message.from_user.id
    await basic_handler.log_command_usage(user_id, "orders")

    keys = await db_manager.get_api_keys(user_id, "bybit")
    if not keys:
        await message.answer("⚠️ API ключи не настроены. Не могу получить список ордеров.")
        return

    try:
        exchange_config = system_config.get_exchange_config("bybit")
        use_demo = exchange_config.demo if exchange_config else False

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

    api_keys = await db_manager.get_api_keys(user_id, "bybit")
    if not api_keys:
        await message.answer(
            "⚠️ <b>API ключи не настроены.</b>\nПерейдите в 'Настройки' -> 'API ключи' для их добавления.",
            parse_mode="HTML")
        return

    session_status = await redis_manager.get_user_session(user_id)
    if session_status and session_status.get('autotrade_enabled', False):
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
    """Обработчик команды /autotrade_stop"""
    user_id = message.from_user.id
    await basic_handler.log_command_usage(user_id, "autotrade_stop")

    session_status = await redis_manager.get_user_session(user_id)
    # Проверяем флаг, который реально управляет торговлей
    if not session_status or not session_status.get('autotrade_enabled', False):
        await message.answer("🔴 Торговля и так неактивна.")
        return

    # Отправляем событие в шину
    if basic_handler.event_bus:
        await basic_handler.event_bus.publish(UserSessionStopRequestedEvent(user_id=user_id, reason="manual_stop_command"))
    else:
        log_error(user_id, "EventBus не инициализирован для отправки команды остановки торговли", module_name='basic_handlers')
        await message.answer("❌ Внутренняя ошибка системы. Попробуйте позже.")
        return

    await message.answer(
        "🛑 <b>Останавливаю автоматическую торговлю...</b>\nСистема завершит текущие операции и сохранит статистику.",
        parse_mode="HTML")

    # Улучшенная проверка статуса остановки
    is_stopped = False
    for _ in range(15):  # Проверяем в течение 15 секунд
        await asyncio.sleep(1)
        session_data = await redis_manager.get_user_session(user_id)
        if not session_data or not session_data.get('autotrade_enabled', False):
            is_stopped = True
            break

    if is_stopped:
        await message.answer("✅ <b>Торговля успешно остановлена.</b>", parse_mode="HTML")
    else:
        await message.answer(
            "❌ <b>Не удалось подтвердить остановку торговли.</b> Проверьте статус через /autotrade_status.",
            parse_mode="HTML")


@router.message(Command("autotrade_status"))
async def cmd_autotrade_status(message: Message, state: FSMContext):
    """Расширенный обработчик команды /autotrade_status с детальной информацией"""
    user_id = message.from_user.id
    await basic_handler.log_command_usage(user_id, "autotrade_status")

    try:
        # Получаем сессию пользователя
        session_status = await redis_manager.get_user_session(user_id)
        if not session_status:
            await message.answer("🔴 <b>Статус: Неактивен</b>\nТорговля не запущена.", parse_mode="HTML")
            return

        is_active = session_status.get('autotrade_enabled', False)
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
            watchlist = user_config.get('watchlist', [])
            for symbol in watchlist:
                configured_symbols.add(symbol)

            # Проверяем, какие стратегии должны быть активны, но не запущены
            strategy_configs = [
                (ConfigType.STRATEGY_SIGNAL_SCALPER, "SIGNAL_SCALPER"),
                (ConfigType.STRATEGY_IMPULSE_TRAILING, "IMPULSE_TRAILING")
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

        # Получаем API для проверки позиций
        api_keys = await db_manager.get_api_keys(user_id, "bybit")
        api = None
        positions_data = {}

        if api_keys:
            try:
                api = BybitAPI(api_keys.api_key, api_keys.secret_key, testnet=False)
                # Получаем все позиции пользователя
                all_positions = await api.get_positions()
                if all_positions:
                    for pos in all_positions:
                        symbol = pos.get('symbol', '')
                        size = float(pos.get('size', 0))
                        if size > 0:  # Только активные позиции
                            positions_data[symbol] = {
                                'side': pos.get('side', ''),
                                'size': size,
                                'unrealizedPnl': float(pos.get('unrealizedPnl', 0)),
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
            elif strategy_type == "IMPULSE_TRAILING":
                display_name = "⚡ Impulse Trailing"
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
                elif strategy_type == "IMPULSE_TRAILING":
                    display_name = "⚡ Impulse Trailing"
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
    """Обработчик команды /balance"""
    user_id = message.from_user.id
    await basic_handler.log_command_usage(user_id, "balance")

    keys = await db_manager.get_api_keys(user_id, "bybit")
    if not keys:
        await message.answer("⚠️ API ключи не настроены. Не могу получить баланс.")
        return

    try:
        exchange_config = system_config.get_exchange_config("bybit")
        use_demo = exchange_config.demo if exchange_config else False

        # Используем контекстный менеджер и передаем флаг testnet
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
    """Обработчик команды /positions"""
    user_id = message.from_user.id
    await basic_handler.log_command_usage(user_id, "positions")

    keys = await db_manager.get_api_keys(user_id, "bybit")
    if not keys:
        await message.answer("⚠️ API ключи не настроены. Не могу получить позиции.")
        return

    try:
        # Явно получаем флаг 'demo' из глобальной конфигурации
        exchange_config = system_config.get_exchange_config("bybit")
        use_demo = exchange_config.demo if exchange_config else False

        # Используем контекстный менеджер и передаем флаг testnet
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
    """Обработчик команды /stop_all (экстренная остановка)"""
    user_id = message.from_user.id
    await basic_handler.log_command_usage(user_id, "stop_all")

    # Эта команда выполняет то же, что и кнопка экстренной остановки
    await message.answer(
        "🚨 <b>ВНИМАНИЕ!</b>\nВы собираетесь экстренно остановить всю торговлю и закрыть все открытые позиции. Это действие необратимо.",
        parse_mode="HTML",
        reply_markup=get_confirmation_keyboard("emergency_stop")  # Используем клавиатуру подтверждения
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

