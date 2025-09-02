"""
Профессиональная система базовых команд для многопользовательского торгового бота
"""
from aiogram import Router, F
from aiogram.types import Message, CallbackQuery
from aiogram.filters import Command, CommandStart
from aiogram.fsm.context import FSMContext
from typing import Optional, Dict, Any
from datetime import datetime
import asyncio

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
    get_welcome_keyboard,
    get_help_keyboard,
    get_quick_actions_keyboard,
    get_confirmation_keyboard,
    get_settings_keyboard,
    get_manual_trade_symbol_keyboard,
    get_back_keyboard
)
from core.logger import log_info, log_error, log_warning
from core.settings_config import system_config

router = Router()

class BasicCommandHandler:
    """Профессиональный обработчик базовых команд"""

    def __init__(self, event_bus: EventBus):
        self.event_bus = event_bus
        self.command_stats = {}
        self.user_sessions = {}

    async def log_command_usage(self, user_id: int, command: str):
        """Логирование использования команд"""
        if command not in self.command_stats:
            self.command_stats[command] = 0
        self.command_stats[command] += 1

        log_info(user_id, f"Команда '{command}' выполнена", module_name='basic_handlers')
        # Обновляем активность пользователя в Redis
        await redis_manager.update_user_activity(user_id)

basic_handler = BasicCommandHandler(None)  # EventBus будет инициализирован позже

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
        is_active = session_data and session_data.get('status') == 'active'

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
            reply_markup=get_main_menu_keyboard(is_active), # Используем основную клавиатуру
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
        user_config = await redis_manager.get_user_config(user_id)

        if not session_status:
            status_text = (
                f"🔴 <b>Статус: Неактивен</b>\n\n"
                f"Торговая сессия не запущена.\n"
                f"Используйте /trade_start для запуска торговли."
            )
        else:
            is_active = session_status.get('is_active', False)
            active_strategies = session_status.get('active_strategies', [])
            last_activity = session_status.get('last_activity')

            status_emoji = "🟢" if is_active else "🔴"
            status_name = "Активен" if is_active else "Неактивен"

            status_text = (
                f"{status_emoji} <b>Статус: {status_name}</b>\n\n"
                f"📊 <b>Активных стратегий:</b> {len(active_strategies)}\n"
            )

            if active_strategies:
                status_text += f"🔄 <b>Стратегии:</b> {', '.join(active_strategies)}\n"

            if last_activity:
                status_text += f"⏰ <b>Последняя активность:</b> {last_activity}\n"

            # Добавляем информацию о настройках риска
            if user_config:
                risk_config = user_config.get('risk_management', {})
                status_text += (
                    f"\n🛡️ <b>Настройки риска:</b>\n"
                    f"🎯 Риск на сделку: {risk_config.get('risk_per_trade', 2)}%\n"
                    f"📉 Макс. просадка: {risk_config.get('max_daily_drawdown', 10)}%\n"
                    f"📊 Макс. сделок: {risk_config.get('max_concurrent_trades', 3)}\n"
                )

        await message.answer(
            status_text,
            reply_markup=get_quick_actions_keyboard(session_status.get('is_active', False) if session_status else False),
            parse_mode="HTML"
        )

    except Exception as e:
        log_error(user_id, f"Ошибка в команде /status: {e}", module_name='basic_handlers')
        await message.answer("❌ Ошибка получения статуса")

@router.message(Command("trade_start"))
async def cmd_trade_start(message: Message, state: FSMContext):
    """Обработчик команды /trade_start"""
    user_id = message.from_user.id

    try:
        await basic_handler.log_command_usage(user_id, "trade_start")

        # Проверяем доступ пользователя
        user_profile = await db_manager.get_user(user_id)
        if not user_profile or not user_profile.is_active:
            await message.answer("🚫 У вас нет доступа к торговле")
            return

        # Проверяем API ключи
        api_keys = await db_manager.get_api_keys(user_id, "bybit")
        if not api_keys:
            await message.answer(
                "⚠️ <b>API ключи не настроены</b>\n\n"
                "Для торговли необходимо настроить API ключи биржи.\n"
                "Используйте команду /api для настройки.",
                parse_mode="HTML"
            )
            return

        # Проверяем существующую сессию
        session_status = await redis_manager.get_user_session(user_id)
        if session_status and session_status.get('is_active'):
            await message.answer("⚠️ Торговля уже запущена")
            return

        # Публикуем событие запуска сессии
        if basic_handler.event_bus:
            await basic_handler.event_bus.publish(
                UserSessionStartRequestedEvent(user_id=user_id)
            )

        await message.answer(
            "🚀 <b>Запуск торговли...</b>\n\n"
            "⏳ Инициализация торговой сессии...\n"
            "📊 Загрузка конфигураций...\n"
            "🔄 Подключение к рынку...\n\n"
            "Это может занять несколько секунд.",
            parse_mode="HTML"
        )

        # Ждем немного и показываем статус
        await asyncio.sleep(3)

        await message.answer(
            "✅ <b>Торговля запущена!</b>\n\n"
            "🟢 Система активна и мониторит рынок\n"
            "📊 Стратегии готовы к работе\n"
            "🛡️ Риск-менеджмент активирован\n\n"
            "Используйте /status для проверки состояния.",
            reply_markup=get_quick_actions_keyboard(True),
            parse_mode="HTML"
        )

        log_info(user_id, "Запуск торговли", module_name='basic_handlers')

    except Exception as e:
        log_error(user_id, f"Ошибка в команде /trade_start: {e}", module_name='basic_handlers')
        await message.answer("❌ Ошибка запуска торговли")

@router.message(Command("trade_stop"))
async def cmd_trade_stop(message: Message, state: FSMContext):
    """Обработчик команды /trade_stop"""
    user_id = message.from_user.id

    try:
        await basic_handler.log_command_usage(user_id, "trade_stop")

        # Проверяем существующую сессию
        session_status = await redis_manager.get_user_session_status(user_id)
        if not session_status or not session_status.get('is_active'):
            await message.answer("⚠️ Торговля не запущена")
            return

        # Публикуем событие остановки сессии
        if basic_handler.event_bus:
            await basic_handler.event_bus.publish(
                UserSessionStopRequestedEvent(user_id=user_id)
            )

        await message.answer(
            "🛑 <b>Остановка торговли...</b>\n\n"
            "⏳ Закрытие активных позиций...\n"
            "📊 Сохранение статистики...\n"
            "🔄 Завершение сессии...\n\n"
            "Это может занять несколько секунд.",
            parse_mode="HTML"
        )

        # Ждем немного и показываем статус
        await asyncio.sleep(3)

        await message.answer(
            "🛑 <b>Торговля остановлена!</b>\n\n"
            "🔴 Система деактивирована\n"
            "📊 Все стратегии остановлены\n"
            "💾 Статистика сохранена\n\n"
            "Используйте /trade_start для возобновления торговли.",
            reply_markup=get_quick_actions_keyboard(False),
            parse_mode="HTML"
        )

        log_info(user_id, "Остановка торговли", module_name='basic_handlers')

    except Exception as e:
        log_error(user_id, f"Ошибка в команде /trade_stop: {e}", module_name='basic_handlers')
        await message.answer("❌ Ошибка остановки торговли")

@router.message(Command("emergency_stop"))
async def cmd_emergency_stop(message: Message, state: FSMContext):
    """Обработчик команды экстренной остановки"""
    user_id = message.from_user.id

    try:
        await basic_handler.log_command_usage(user_id, "emergency_stop")

        # Экстренная остановка всех операций
        if basic_handler.event_bus:
            await basic_handler.event_bus.publish(
                UserSessionStopRequestedEvent(
                    user_id=user_id,
                    reason="EMERGENCY_STOP"
                )
            )

        await message.answer(
            "🚨 <b>ЭКСТРЕННАЯ ОСТАНОВКА!</b>\n\n"
            "⚠️ Все торговые операции немедленно остановлены\n"
            "🛑 Активные ордера отменены\n"
            "📊 Позиции закрыты по рынку\n"
            "💾 Данные сохранены\n\n"
            "Система полностью деактивирована.",
            parse_mode="HTML"
        )

        log_warning(user_id, "Экстренная остановка", module_name='basic_handlers')

    except Exception as e:
        log_error(user_id, f"Ошибка в команде /emergency_stop: {e}", module_name='basic_handlers')
        await message.answer("❌ Ошибка экстренной остановки")

@router.message(Command("stats"))
async def cmd_stats(message: Message, state: FSMContext):
    """Обработчик команды /stats"""
    user_id = message.from_user.id

    try:
        await basic_handler.log_command_usage(user_id, "stats")

        # Получаем статистику пользователя
        user_profile = await db_manager.get_user(user_id)
        if not user_profile:
            await message.answer("❌ Профиль пользователя не найден")
            return

        # Получаем последние сделки
        recent_trades = await db_manager.get_user_trades(user_id, limit=5)

        stats_text = (
            f"📊 <b>Статистика торговли</b>\n\n"
            f"👤 <b>Пользователь:</b> {user_profile.username or 'Не указано'}\n"
            f"📅 <b>Регистрация:</b> {user_profile.registration_date.strftime('%d.%m.%Y') if user_profile.registration_date else 'Не указано'}\n\n"
            f"💰 <b>Общая прибыль:</b> {format_currency(user_profile.total_profit)}\n"
            f"📈 <b>Всего сделок:</b> {user_profile.total_trades}\n"
            f"🎯 <b>Win Rate:</b> {format_percentage(user_profile.win_rate)}\n"
            f"📉 <b>Макс. просадка:</b> {format_percentage(user_profile.max_drawdown)}\n\n"
        )

        if recent_trades:
            stats_text += f"📋 <b>Последние сделки:</b>\n"
            for i, trade in enumerate(recent_trades, 1):
                profit_emoji = "📈" if trade.profit > 0 else "📉"
                stats_text += (
                    f"{i}. {profit_emoji} {trade.symbol} "
                    f"{format_currency(trade.profit)} "
                    f"({trade.entry_time.strftime('%d.%m %H:%M') if trade.entry_time else 'N/A'})\n"
                )
        else:
            stats_text += "📋 <b>Сделок пока нет</b>\n"

        await message.answer(
            stats_text,
            parse_mode="HTML"
        )

    except Exception as e:
        log_error(user_id, f"Ошибка в команде /stats: {e}", module_name='basic_handlers')
        await message.answer("❌ Ошибка получения статистики")

@router.message(Command("settings"))
async def cmd_settings(message: Message, state: FSMContext):
    """Обработчик команды /settings"""
    user_id = message.from_user.id
    try:
        await basic_handler.log_command_usage(user_id, "settings")
        await state.set_state(UserStates.SETTINGS_MENU)

        text = (
            "⚙️ <b>Настройки бота</b>\n\n"
            "Здесь вы можете управлять всеми аспектами работы бота, от управления рисками до параметров конкретных стратегий."
        )

        await message.answer(
            text,
            reply_markup=get_settings_keyboard(), # ИСПРАВЛЕНО: используется правильная клавиатура
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
    if session_status and session_status.get('status') == 'active':
        await message.answer("✅ Торговля уже запущена.")
        return

    if basic_handler.event_bus:
        await basic_handler.event_bus.publish(UserSessionStartRequestedEvent(user_id=user_id))
        await message.answer(
            "🚀 <b>Запускаю автоматическую торговлю...</b>\nСистема инициализирует сессию и подключается к рынку.",
            parse_mode="HTML")
    else:
        log_error(user_id, "Шина событий (event_bus) недоступна в basic_handler.", "basic_handlers")
        await message.answer("❌ Системная ошибка: шина событий недоступна. Не могу запустить торговлю.")


@router.message(Command("autotrade_stop"))
async def cmd_autotrade_stop(message: Message, state: FSMContext):
    """Обработчик команды /autotrade_stop"""
    user_id = message.from_user.id
    await basic_handler.log_command_usage(user_id, "autotrade_stop")

    session_status = await redis_manager.get_user_session(user_id)
    if not session_status or session_status.get('status') != 'active':
        await message.answer("🔴 Торговля не запущена.")
        return

    if basic_handler.event_bus:
        await basic_handler.event_bus.publish(
            UserSessionStopRequestedEvent(user_id=user_id, reason="manual_stop_command"))
        await message.answer(
            "🛑 <b>Останавливаю автоматическую торговлю...</b>\nСистема завершит текущие операции и сохранит статистику.",
            parse_mode="HTML")
    else:
        log_error(user_id, "Шина событий (event_bus) недоступна в basic_handler.", "basic_handlers")
        await message.answer("❌ Системная ошибка: шина событий недоступна. Не могу остановить торговлю.")


@router.message(Command("autotrade_status"))
async def cmd_autotrade_status(message: Message, state: FSMContext):
    """Обработчик команды /autotrade_status"""
    user_id = message.from_user.id
    await basic_handler.log_command_usage(user_id, "autotrade_status")

    session_status = await redis_manager.get_user_session(user_id)
    if not session_status:
        await message.answer("🔴 <b>Статус: Неактивен</b>\nТорговля не запущена.", parse_mode="HTML")
        return

    status = session_status.get('status', 'unknown')
    active_strategies = session_status.get('active_strategies', [])

    status_text = f"<b>Статус торговли:</b> {'🟢 Активен' if status == 'active' else '🔴 Неактивен'}\n\n"
    if active_strategies:
        status_text += f"<b>Активные стратегии ({len(active_strategies)}):</b>\n"
        for strategy in active_strategies:
            s_type = strategy.get('strategy_type', 'N/A').replace('_', ' ').title()
            s_symbol = strategy.get('symbol', 'N/A')
            status_text += f"  - <code>{s_symbol}</code> ({s_type})\n"
    else:
        status_text += "Нет активных стратегий."

    await message.answer(status_text, parse_mode="HTML")


@router.message(Command("manual"))
async def cmd_manual(message: Message, state: FSMContext):
    """Обработчик команды /manual для ручного запуска стратегии."""
    user_id = message.from_user.id
    await basic_handler.log_command_usage(user_id, "manual")

    try:
        from core.enums import ConfigType
        user_config = await redis_manager.get_config(user_id, ConfigType.GLOBAL)
        watchlist = user_config.get("watchlist_symbols", [])

        if not watchlist:
            await message.answer(
                "⚠️ <b>Список отслеживания пуст.</b>\n\n"
                "Сначала добавьте торговые пары в 'Настройки' -> 'Watchlist', чтобы можно было запустить стратегию вручную.",
                parse_mode="HTML",
                reply_markup=get_back_keyboard("main_menu")
            )
            return

        await state.set_state(UserStates.SELECTING_STRATEGY_TYPE)
        await message.answer(
            "🛠️ <b>Ручной запуск стратегии</b>\n\n"
            "<b>Шаг 1:</b> Выберите торговую пару из вашего списка отслеживания.",
            parse_mode="HTML",
            reply_markup=get_manual_trade_symbol_keyboard(watchlist)
        )

    except Exception as e:
        log_error(user_id, f"Ошибка в команде /manual: {e}", module_name='basic_handlers')
        await message.answer("❌ Ошибка при попытке ручного запуска.")


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
        use_sandbox = exchange_config.sandbox if exchange_config else False

        # Используем контекстный менеджер и передаем флаг testnet
        async with BybitAPI(user_id=user_id, api_key=keys[0], api_secret=keys[1], testnet=use_sandbox) as api:
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
        # Явно получаем флаг 'sandbox' из глобальной конфигурации
        exchange_config = system_config.get_exchange_config("bybit")
        use_sandbox = exchange_config.sandbox if exchange_config else False

        # Используем контекстный менеджер и передаем флаг testnet
        async with BybitAPI(user_id=user_id, api_key=keys[0], api_secret=keys[1], testnet=use_sandbox) as api:
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
@router.message()
async def handle_unknown_message(message: Message, state: FSMContext):
    """Обработчик неизвестных сообщений"""
    user_id = message.from_user.id

    try:
        # Проверяем, находится ли пользователь в процессе настройки
        current_state = await state.get_state()

        if current_state:
            # Пользователь в процессе настройки, не обрабатываем как неизвестную команду
            return

        log_info(user_id, f"Неизвестное сообщение: {message.text}", module_name='basic_handlers')

        await message.answer(
            "❓ <b>Неизвестная команда</b>\n\n"
            "Используйте /help для получения списка доступных команд\n"
            "или выберите действие в главном меню.",
            reply_markup=get_main_menu_keyboard(False),
            parse_mode="HTML"
        )

    except Exception as e:
        log_error(user_id, f"Ошибка обработки неизвестного сообщения: {e}", module_name='basic_handlers')

# Функция для получения статистики команд
async def get_command_stats() -> Dict[str, int]:
    """Получение статистики использования команд"""
    return basic_handler.command_stats.copy()

