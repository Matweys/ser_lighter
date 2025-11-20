"""
Система базовых команд для многопользовательского торгового бота
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
from core.functions import format_currency, format_percentage, get_moscow_time
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
from .multi_account_helpers import (
    PRIORITY_NAMES,
    PRIORITY_EMOJIS,
    BOT_NAMES,
    STRATEGY_NAMES,
    STRATEGY_HEADERS,
    validate_api_keys,
    is_multi_account_mode,
    is_active_position,
    is_active_order,
    get_multi_account_balance,
    get_multi_account_positions,
    get_multi_account_orders,
    get_multi_account_positions_and_orders,
    format_multi_account_balance,
    format_multi_account_positions,
    format_multi_account_orders,
    get_demo_mode
)



router = Router()

class BasicCommandHandler:
    """Профессиональный обработчик базовых команд"""

    def __init__(self):
        self.command_stats = {}
        self.event_bus: Optional[EventBus] = None
        self.bot_application: Optional[BotApplication] = None


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

def set_bot_application(bot_app: BotApplication):
    """Установка BotApplication для basic handler"""
    basic_handler.bot_application = bot_app

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
            f"<b>🏠 Основные:</b>\n"
            f"/start - Главное меню\n"
            f"/settings - Настройки бота\n"
            f"/help - Показать эту справку\n\n"
            f"<b>▶️ Управление торговлей:</b>\n"
            f"/autotrade_start - Начать торговлю\n"
            f"/autotrade_stop - Остановить торговлю\n"
            f"/autotrade_status - Статус торговли\n"
            f"/stop_all - Экстренная остановка\n\n"
            f"<b>📊 Информация и статистика:</b>\n"
            f"/balance - Баланс аккаунта\n"
            f"/trade_details - Детали позиций (усреднения)\n"
            f"/stats - Статистика торговли\n\n"
            f"<b>💡 Совет:</b>\n"
            f"Используйте inline-кнопки в главном меню для быстрого доступа ко всем функциям бота."
        )

        await message.answer(
            help_text,
            reply_markup=get_help_keyboard(),
            parse_mode="HTML"
        )

    except Exception as e:
        log_error(user_id, f"Ошибка в команде /help: {e}", module_name='basic_handlers')
        await message.answer("❌ Ошибка получения справки")


@router.message(Command("trade_details"))
async def cmd_trade_details(message: Message, state: FSMContext):
    """
    Обработчик команды /trade_details - детальная информация о текущих позициях.

    КРИТИЧНО: Показывает ТОЛЬКО позиции которые:
    1. Бот создал и записал в БД (есть OPEN ордер)
    2. РЕАЛЬНО открыты на бирже (проверка через API)

    Если позиция есть в БД но закрыта на бирже - НЕ показывает!
    Работает для ВСЕХ стратегий и ВСЕХ ботов (Bot_1, Bot_2, Bot_3).
    """
    user_id = message.from_user.id
    await basic_handler.log_command_usage(user_id, "trade_details")

    try:
        from decimal import Decimal

        # ШАГ 1: Получаем ВСЕ открытые позиции из БД (OPEN без CLOSE)
        db_positions = await db_manager.get_all_open_positions(user_id)

        log_info(user_id, f"[trade_details] Найдено открытых позиций в БД: {len(db_positions)}", module_name='basic_handlers')

        if not db_positions:
            await message.answer(
                "ℹ️ <b>Нет открытых позиций в БД</b>\n\n"
                "Все позиции, созданные ботом, будут отображаться здесь.\n"
                "Запустите автоторговлю командой /autotrade_start",
                parse_mode="HTML"
            )
            return

        # ШАГ 2: Получаем API ключи и РЕАЛЬНЫЕ позиции с биржи
        api_keys_list = await db_manager.get_all_user_api_keys(user_id, "bybit")
        if not api_keys_list:
            await message.answer(
                "❌ <b>API ключи не настроены</b>\n\n"
                "Настройте API ключи для просмотра позиций.",
                parse_mode="HTML"
            )
            return

        # ШАГ 3: Получаем РЕАЛЬНЫЕ позиции со ВСЕХ аккаунтов на бирже
        exchange_positions = {}  # {(symbol, bot_priority): position_data}

        # Определяем режим торговли (demo/live)
        use_demo = get_demo_mode()

        for key_data in api_keys_list:
            priority = key_data['priority']
            try:
                async with BybitAPI(
                    api_key=key_data['api_key'],
                    api_secret=key_data['secret_key'],
                    user_id=user_id,
                    demo=use_demo
                ) as api:
                    positions = await api.get_positions()

                    if positions:
                        log_info(user_id, f"[trade_details] Бот{priority}: найдено {len(positions)} позиций на бирже", module_name='basic_handlers')
                        for pos in positions:
                            size = float(pos.get('size', 0))
                            symbol = pos.get('symbol', '')
                            log_info(user_id, f"[trade_details] Бот{priority}: {symbol}, size={size}", module_name='basic_handlers')
                            if size != 0:  # Только активные позиции
                                key = (symbol, priority)
                                exchange_positions[key] = pos
                    else:
                        log_info(user_id, f"[trade_details] Бот{priority}: НЕТ позиций на бирже", module_name='basic_handlers')
            except Exception as e:
                log_error(user_id, f"Ошибка получения позиций для Bot{priority}: {e}", module_name='basic_handlers')

        log_info(user_id, f"[trade_details] Всего активных позиций на бирже: {len(exchange_positions)}", module_name='basic_handlers')

        # ШАГ 4: Сопоставляем DB позиции с реальными позициями на бирже
        # Показываем ТОЛЬКО если позиция есть И в БД И на бирже!
        verified_positions = []

        db_keys = [(db_pos["symbol"], db_pos["bot_priority"]) for db_pos in db_positions]
        exchange_keys = list(exchange_positions.keys())
        log_info(user_id, f"[trade_details] 🔑 Ключи из БД: {db_keys}", module_name='basic_handlers')
        log_info(user_id, f"[trade_details] 🔑 Ключи с биржи: {exchange_keys}", module_name='basic_handlers')

        for db_pos in db_positions:
            symbol = db_pos["symbol"]
            bot_priority = db_pos["bot_priority"]
            strategy_type = db_pos.get("strategy_type", "unknown")
            key = (symbol, bot_priority)

            log_info(user_id,
                    f"[trade_details] Проверка позиции: {key} (стратегия: {strategy_type})",
                    module_name='basic_handlers')

            # КРИТИЧНО: Проверяем что позиция РЕАЛЬНО открыта на бирже!
            if key in exchange_positions:
                # Позиция есть в БД И на бирже - показываем!
                log_info(user_id,
                        f"[trade_details] ✅ СОВПАДЕНИЕ: {key} найдена на бирже!",
                        module_name='basic_handlers')
                verified_positions.append({
                    "db_position": db_pos,
                    "exchange_position": exchange_positions[key]
                })
            else:
                # Позиция в БД но НЕТ на бирже - пропускаем!
                log_warning(user_id,
                    f"[trade_details] ❌ НЕТ СОВПАДЕНИЯ: Позиция {symbol} Bot{bot_priority} (стратегия: {strategy_type}) есть в БД но ОТСУТСТВУЕТ на бирже! Не показываем.",
                    module_name='basic_handlers')

        if not verified_positions:
            await message.answer(
                "ℹ️ <b>Нет активных позиций на бирже</b>\n\n"
                "Все позиции из БД закрыты или были закрыты вручную.",
                parse_mode="HTML"
            )
            return

        # ШАГ 5: Сортируем позиции для красивого отображения
        # Сначала Bot 1, потом Bot 2, потом Bot 3
        verified_positions.sort(key=lambda x: (
            x["db_position"]["bot_priority"],  # Сортировка по боту
            0 if x["db_position"]["strategy_type"] == "signal_scalper" else 1,  # Сортировка по стратегии
            x["db_position"]["symbol"]  # Сортировка по символу
        ))

        # ШАГ 6: Форматируем вывод для каждой ПРОВЕРЕННОЙ позиции
        status_text = "📊 <b>ДЕТАЛЬНАЯ ИНФОРМАЦИЯ О ПОЗИЦИЯХ</b>\n"
        status_text += "✅ <b>Проверено: БД + Реальное состояние биржи</b>\n"
        status_text += "═" * 40 + "\n\n"

        current_bot_priority = None
        current_strategy_type = None

        for verified_pos in verified_positions:
            db_pos = verified_pos["db_position"]
            exchange_pos = verified_pos["exchange_position"]

            # Данные ИЗ БД (источник истины для ордеров бота)
            symbol = db_pos["symbol"]
            strategy_type = db_pos["strategy_type"]
            bot_priority = db_pos["bot_priority"]
            open_order = db_pos["open_order"]
            averaging_orders = db_pos["averaging_orders"]

            symbol_short = symbol.replace('USDT', '')

            # Определяем приоритет бота
            priority_emoji = PRIORITY_EMOJIS.get(bot_priority, f"#{bot_priority}")

            # Стратегия
            strategy_name = STRATEGY_NAMES.get(strategy_type, strategy_type)

            # Направление позиции (определяем по стороне OPEN ордера из БД)
            open_side = open_order["side"]  # "Buy" или "Sell"
            direction = "LONG" if open_side == "Buy" else "SHORT"
            direction_emoji = "📈" if direction == "LONG" else "📉"

            # Цена входа ИЗ БД (OPEN ордер)
            entry_price = Decimal(str(open_order["average_price"]))
            initial_quantity = Decimal(str(open_order["filled_quantity"]))

            # Если есть усреднения ИЗ БД - рассчитываем среднюю цену
            total_quantity = initial_quantity
            total_cost = entry_price * initial_quantity

            for avg_order in averaging_orders:
                avg_price = Decimal(str(avg_order["average_price"]))
                avg_qty = Decimal(str(avg_order["filled_quantity"]))
                total_quantity += avg_qty
                total_cost += avg_price * avg_qty

            average_entry_price = total_cost / total_quantity if total_quantity > 0 else entry_price
            averaging_count = len(averaging_orders)

            # Текущая цена и PnL С БИРЖИ (реальное состояние)
            current_price = Decimal(str(exchange_pos.get("markPrice", exchange_pos.get("lastPrice", 0))))
            unrealized_pnl_from_exchange = Decimal(str(exchange_pos.get("unrealisedPnl", 0)))

            # Рассчитываем процент изменения
            if direction == "LONG":
                price_change_percent = ((current_price - average_entry_price) / average_entry_price) * Decimal('100')
            else:  # SHORT
                price_change_percent = ((average_entry_price - current_price) / average_entry_price) * Decimal('100')

            # Добавляем заголовок при смене бота
            if current_bot_priority != bot_priority:
                if current_bot_priority is not None:
                    # Разделитель между ботами
                    status_text += "═" * 40 + "\n\n"

                # Заголовок бота
                bot_name = BOT_NAMES.get(bot_priority, f"BOT #{bot_priority}")
                status_text += f"{priority_emoji} <b>{bot_name}</b>\n"
                status_text += "─" * 35 + "\n\n"
                current_bot_priority = bot_priority
                current_strategy_type = None  # Сбрасываем стратегию при смене бота

            # Добавляем заголовок при смене стратегии
            if current_strategy_type != strategy_type:
                if current_strategy_type is not None:
                    status_text += "\n"

                # Заголовок стратегии
                strategy_header = STRATEGY_HEADERS.get(strategy_type, strategy_type)
                status_text += f"<b>{strategy_header}</b>\n"
                status_text += "┈" * 35 + "\n"
                current_strategy_type = strategy_type

            # ФОРМАТИРУЕМ ВЫВОД ПОЗИЦИИ
            status_text += f"▸ <b>{symbol_short}</b> | {direction_emoji} {direction}\n"

            # ЦЕНЫ
            status_text += f"💵 <b>Цены:</b>\n"
            if averaging_count > 0:
                status_text += f"  • Первый вход: ${float(entry_price):.4f}\n"
                status_text += f"  • Средняя цена: ${float(average_entry_price):.4f}\n"
                status_text += f"  • Текущая цена: ${float(current_price):.4f}\n"
            else:
                status_text += f"  • Цена входа: ${float(entry_price):.4f}\n"
                status_text += f"  • Текущая цена: ${float(current_price):.4f}\n"

            # PnL (используем данные С БИРЖИ)
            pnl_emoji = "🟢" if unrealized_pnl_from_exchange >= 0 else "🔴"
            change_emoji = "📈" if price_change_percent >= 0 else "📉"
            status_text += f"\n{pnl_emoji} <b>{'Прибыль' if unrealized_pnl_from_exchange >= 0 else 'Просадка'}:</b> ${float(unrealized_pnl_from_exchange):.2f}\n"
            status_text += f"{change_emoji} <b>Изменение цены:</b> {float(price_change_percent):+.2f}%\n"

            # УСРЕДНЕНИЯ
            if averaging_count > 0:
                status_text += f"\n🔄 <b>Усреднения:</b> {averaging_count}\n"
                status_text += f"  • Начальный объем: {float(initial_quantity)}\n"
                status_text += f"  • Общий объем: {float(total_quantity)}\n"

            status_text += "\n"

        # Timestamp
        current_time = get_moscow_time().strftime('%H:%M:%S')
        status_text += f"🕐 Обновлено: {current_time} МСК"

        await message.answer(status_text, parse_mode="HTML")

    except Exception as e:
        log_error(user_id, f"Ошибка в команде /trade_details: {e}", module_name='basic_handlers')
        import traceback
        log_error(user_id, f"Traceback: {traceback.format_exc()}", module_name='basic_handlers')
        await message.answer("❌ Произошла ошибка при получении детальной информации о позициях.")


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

    # КРИТИЧНО: Проверяем Redis данные против реального состояния в BotApplication
    session_status = await redis_manager.get_user_session(user_id)

    # Валидация: проверяем реальное состояние в BotApplication
    is_actually_running = False
    if basic_handler.bot_application:
        is_actually_running = user_id in basic_handler.bot_application.active_sessions

        # Если сессия НЕ активна в BotApplication, но Redis говорит running=True - это stale data
        if not is_actually_running and session_status and session_status.get('running', False):
            log_warning(user_id,
                       f"⚠️ Обнаружены stale данные в Redis при запуске: running={session_status.get('running')}. Очищаю...",
                       module_name='basic_handlers')
            await redis_manager.delete_user_session(user_id)
            session_status = None

    # Проверяем по реальному состоянию, а не по Redis
    if is_actually_running:
        await message.answer("✅ Торговля уже запущена.")
        return

    # Отправляем только первое сообщение
    await message.answer(
        "🚀 <b>Запускаю автоматическую торговлю...</b>\nСистема инициализирует сессию и подключается к рынку. Вы получите уведомление по завершении.",
        parse_mode="HTML")

    # ДИАГНОСТИКА: Проверяем состояние EventBus
    log_info(user_id, f"🔍 ДИАГНОСТИКА: basic_handler.event_bus = {basic_handler.event_bus}", module_name='basic_handlers')
    log_info(user_id, f"🔍 ДИАГНОСТИКА: event_bus.is_running = {basic_handler.event_bus.is_running if basic_handler.event_bus else 'N/A'}", module_name='basic_handlers')

    # Отправляем событие в шину
    if basic_handler.event_bus:
        log_info(user_id, "📤 Публикую UserSessionStartRequestedEvent в EventBus...", module_name='basic_handlers')
        event = UserSessionStartRequestedEvent(user_id=user_id)
        log_info(user_id, f"📤 Событие создано: {event}", module_name='basic_handlers')
        await basic_handler.event_bus.publish(event)
        log_info(user_id, "✅ Событие UserSessionStartRequestedEvent успешно опубликовано", module_name='basic_handlers')
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
        use_demo = get_demo_mode()

        # === ПРОВЕРКА MULTI-ACCOUNT РЕЖИМА ===
        all_api_keys = await db_manager.get_all_user_api_keys(user_id, "bybit")

        if not all_api_keys or len(all_api_keys) == 0:
            await message.answer("❌ API ключи не найдены.")
            return

        from api.bybit_api import BybitAPI

        # === MULTI-ACCOUNT РЕЖИМ (3 аккаунта) ===
        if is_multi_account_mode(all_api_keys):
            log_info(user_id, "Остановка торговли в multi-account режиме (3 аккаунта)", "autotrade_stop")

            # Используем helper функцию для получения позиций и ордеров
            all_positions, all_orders = await get_multi_account_positions_and_orders(user_id, all_api_keys, use_demo)

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
                            position_info += f"  • {PRIORITY_NAMES[priority]}: {len(bot_positions)} поз.\n"

                if all_orders:
                    position_info += f"\n📋 Всего активных ордеров: {len(all_orders)}\n"
                    # Группируем по ботам
                    for priority in [1, 2, 3]:
                        bot_orders = [o for o in all_orders if o.get('_bot_priority') == priority]
                        if bot_orders:
                            position_info += f"  • {PRIORITY_NAMES[priority]}: {len(bot_orders)} орд.\n"

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

            # Используем helper функцию для получения позиций и ордеров
            all_positions, all_orders = await get_multi_account_positions_and_orders(user_id, all_api_keys, use_demo)

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
                for priority in [1, 2, 3]:
                    bot_positions = [p for p in all_positions if p.get('_bot_priority') == priority]
                    bot_orders = [o for o in all_orders if o.get('_bot_priority') == priority]

                    if bot_positions or bot_orders:
                        name = PRIORITY_NAMES.get(priority, f"Бот {priority}")
                        emoji = PRIORITY_EMOJIS.get(priority, "🔹")

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
        # Получаем сессию пользователя из Redis
        session_status = await redis_manager.get_user_session(user_id)

        # Логируем для отладки
        log_info(user_id, f"Проверка статуса: session_status={session_status}", "autotrade_status")

        # КРИТИЧНО: Валидируем Redis данные против реального состояния в BotApplication
        if basic_handler.bot_application:
            log_info(user_id, f"bot_application найден, active_sessions: {list(basic_handler.bot_application.active_sessions.keys())}", "autotrade_status")

            # Проверяем, действительно ли сессия активна в BotApplication
            is_actually_running = user_id in basic_handler.bot_application.active_sessions

            if is_actually_running:
                # Сессия активна в BotApplication - синхронизируем с Redis если нужно
                actual_session = basic_handler.bot_application.active_sessions[user_id]

                # Если в Redis нет данных или они не совпадают - обновляем из реального состояния
                if not session_status or session_status.get('running') != actual_session.running:
                    log_info(user_id,
                               f"Обновляю данные из active_session: running={actual_session.running}",
                               "autotrade_status")
                    # Принудительно обновляем Redis из реального состояния
                    await actual_session.save_session_state()
                    session_status = await redis_manager.get_user_session(user_id)
            # ВАЖНО: Если сессия НЕ в active_sessions, доверяем Redis (может быть задержка синхронизации)
            # НЕ удаляем данные из Redis в этом случае!
        else:
            log_warning(user_id, "bot_application не инициализирован, используем только данные из Redis", "autotrade_status")

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
                (ConfigType.STRATEGY_SIGNAL_SCALPER, "SIGNAL_SCALPER")
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
                use_demo = get_demo_mode()

                # === MULTI-ACCOUNT РЕЖИМ (3 аккаунта) - показываем позиции по каждому боту отдельно ===
                if is_multi_account_mode(all_api_keys):
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
                                            # НОВАЯ СТРУКТУРА: Храним позиции по (symbol, bot_priority)
                                            key = (symbol, priority)
                                            positions_data[key] = {
                                                'side': pos.get('side', ''),
                                                'size': size,
                                                'unrealizedPnl': float(pos.get('unrealisedPnl', 0)),
                                                'avgPrice': float(pos.get('avgPrice', 0)),
                                                'markPrice': float(pos.get('markPrice', 0)),
                                                'bot_priority': priority
                                            }

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
                # Парсим strategy_id с поддержкой multi-account режима
                # Форматы:
                # - Обычный: "SIGNAL_SCALPER_SOLUSDT"
                # - Multi-account: "signal_scalper_SOLUSDT_bot1", "signal_scalper_SOLUSDT_bot2", etc.

                # Убираем суффикс _botN если есть
                normalized_id = strategy_id
                if '_bot' in strategy_id:
                    # Убираем последнюю часть если это _botN
                    parts = strategy_id.split('_')
                    if parts[-1].startswith('bot') and parts[-1][3:].isdigit():
                        normalized_id = '_'.join(parts[:-1])

                # Теперь парсим нормализованный ID
                parts = normalized_id.split('_')
                if len(parts) >= 2:
                    # Последняя часть - символ (SOLUSDT)
                    symbol = parts[-1]
                    # Остальное - тип стратегии (signal_scalper)
                    strategy_type = '_'.join(parts[:-1]).upper()

                    if strategy_type not in strategies_by_type:
                        strategies_by_type[strategy_type] = set()  # Используем set чтобы избежать дублей
                    strategies_by_type[strategy_type].add(symbol)
            except Exception as e:
                log_warning(user_id, f"Ошибка парсинга strategy_id {strategy_id}: {e}", "autotrade_status")

        # Отображаем информацию по стратегиям
        for strategy_type, symbols in strategies_by_type.items():
            # Переводим название стратегии
            if strategy_type == "SIGNAL_SCALPER":
                display_name = "📈 Signal Scalper"
            else:
                display_name = f"🔧 {strategy_type.replace('_', ' ').title()}"

            status_text += f"<b>{display_name}</b>\n"

            # Преобразуем set в отсортированный list для стабильного отображения
            for symbol in sorted(symbols):
                symbol_short = symbol.replace('USDT', '')  # SOLUSDT -> SOL

                # MULTI-ACCOUNT: Проверяем позиции по всем ботам для этого символа
                symbol_positions = []
                for key, pos_data in positions_data.items():
                    # Ключ может быть symbol (для обычного режима) или (symbol, priority) (для multi-account)
                    if isinstance(key, tuple):
                        # Multi-account режим: (symbol, priority)
                        pos_symbol, priority = key
                        if pos_symbol == symbol:
                            symbol_positions.append({**pos_data, 'priority': priority})
                    elif key == symbol:
                        # Обычный режим: просто symbol
                        symbol_positions.append(pos_data)

                # Если есть позиции по этому символу - показываем каждую
                if symbol_positions:
                    for idx, pos in enumerate(symbol_positions):
                        pnl = pos['unrealizedPnl']
                        bot_label = ""

                        # Добавляем метку бота для multi-account режима
                        if 'priority' in pos:
                            bot_emoji = PRIORITY_EMOJIS.get(pos['priority'], f"#{pos['priority']}")
                            bot_label = f" {bot_emoji} Бот {pos['priority']}"

                        # Формируем строку статуса
                        if pnl > 0:
                            status_line = f"🟢 В прибыли +${pnl:.2f}{bot_label}"
                        elif pnl < 0:
                            status_line = f"🔴 В убытке ${pnl:.2f}{bot_label}"
                        else:
                            status_line = f"⚪ Без изменений (${pnl:.2f}){bot_label}"

                        # Добавляем информацию о позиции
                        side_icon = "📈" if pos['side'] == 'Buy' else "📉"

                        status_text += f"  ▫️ <b>{symbol_short}:</b> {status_line}\n"
                        status_text += f"     {side_icon} {pos['side']} {pos['size']}, "
                        status_text += f"вход: ${pos['avgPrice']:.4f}\n"

                        # Разделитель между позициями одного символа (кроме последней)
                        if idx < len(symbol_positions) - 1:
                            status_text += "     ─────────────\n"
                else:
                    # Нет позиций по этому символу
                    status_text += f"  ▫️ <b>{symbol_short}:</b> ⏳ В ожидании сигнала\n"

            status_text += "\n"

        # Добавляем информацию о неактивных (отключенных) стратегиях
        if inactive_strategies:
            status_text += "⚫ <b>ОТКЛЮЧЕННЫЕ СТРАТЕГИИ</b>\n"
            status_text += "─" * 20 + "\n"

            for strategy_type, symbols in inactive_strategies.items():
                # Переводим название стратегии
                if strategy_type == "SIGNAL_SCALPER":
                    display_name = "📈 Signal Scalper"
                else:
                    display_name = f"🔧 {strategy_type.replace('_', ' ').title()}"

                status_text += f"<b>{display_name}</b>\n"

                for symbol in symbols:
                    symbol_short = symbol.replace('USDT', '')  # SOLUSDT -> SOL
                    status_text += f"  ▫️ <b>{symbol_short}:</b> 🔴 Отключена пользователем\n"

                status_text += "\n"

            status_text += "ℹ️ <i>Для включения перейдите в /settings</i>\n\n"

        # Добавляем информацию о времени обновления
        current_time = get_moscow_time().strftime('%H:%M:%S')
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
        # Определяем режим торговли (demo/live)
        use_demo = get_demo_mode()

        # === ПРОВЕРКА MULTI-ACCOUNT РЕЖИМА ===
        all_api_keys = await db_manager.get_all_user_api_keys(user_id, "bybit")

        if not all_api_keys or len(all_api_keys) == 0:
            await message.answer("⚠️ API ключи не настроены. Не могу получить баланс.")
            return

        # === MULTI-ACCOUNT РЕЖИМ (3 аккаунта) ===
        if is_multi_account_mode(all_api_keys):
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
            for acc in accounts_data:
                priority = acc['priority']
                name = PRIORITY_NAMES.get(priority, f"Бот {priority}")
                emoji = PRIORITY_EMOJIS.get(priority, "🔹")
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
        use_demo = get_demo_mode()

        # Собираем позиции и ордера со ВСЕХ аккаунтов
        all_positions = []
        all_orders = []

        # === MULTI-ACCOUNT РЕЖИМ (3 аккаунта) - агрегируем данные ===
        if is_multi_account_mode(all_api_keys):
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

                for priority in [1, 2, 3]:
                    bot_positions = [p for p in all_positions if p.get('_bot_priority') == priority]
                    if bot_positions:
                        emoji = PRIORITY_EMOJIS.get(priority, "🔹")
                        warning_text += f"\n{emoji} <b>{PRIORITY_NAMES[priority]}:</b>\n"

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
