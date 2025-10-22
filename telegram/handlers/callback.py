"""
Профессиональная система обработки callback запросов для многопользовательского торгового бота
"""
from aiogram import Router, F
from aiogram.types import CallbackQuery, Message
from aiogram.fsm.context import FSMContext
from typing import Dict, Any, Optional
from decimal import Decimal
import json
from ..bot import bot_manager
from database.db_trades import db_manager
from core.events import EventBus, UserSessionStartRequestedEvent, UserSessionStopRequestedEvent, UserSettingsChangedEvent, SignalEvent
from core.enums import StrategyType, PositionSide, NotificationType, ConfigType
from ..keyboards.inline import (
    get_main_menu_keyboard,
    get_strategy_config_keyboard,
    get_confirmation_keyboard,
    get_symbol_selection_keyboard,
    get_settings_keyboard,
    get_strategy_settings_keyboard,
    get_back_keyboard
)
from .states import UserStates
from cache.redis_manager import redis_manager
from core.functions import format_currency, format_percentage, validate_symbol
from core.default_configs import DefaultConfigs
from core.logger import log_info, log_error, log_warning
from core.settings_config import DEFAULT_SYMBOLS, system_config
from api.bybit_api import BybitAPI

# Глобальная переменная для доступа к BotApplication
_bot_application = None

def set_bot_application(bot_app):
    """Установка BotApplication для callback handler"""
    global _bot_application
    _bot_application = bot_app
from aiogram.exceptions import TelegramBadRequest
from aiogram.utils.markdown import hbold
from core.functions import to_decimal
from datetime import datetime, timedelta, timezone
from .multi_account_helpers import (
    PRIORITY_NAMES,
    PRIORITY_EMOJIS,
    validate_api_keys,
    is_multi_account_mode,
    get_multi_account_balance,
    format_multi_account_balance
)



router = Router()

class CallbackHandler:
    """Профессиональный обработчик callback запросов"""
    
    def __init__(self, event_bus: EventBus):
        self.event_bus = event_bus
        self.strategy_descriptions = {
            StrategyType.SIGNAL_SCALPER.value: {
                "name": "💡 Сигнальный Скальпер (EMA + RSI)",
                "description": (
                    "Интеллектуальная стратегия, работающая в обе стороны (LONG/SHORT).\n"
                    "Принимает решения на основе пересечения EMA и значений RSI.\n"
                    "Автоматически управляет выходом из сделки по трейлингу."
                ),
                "risk_level": "MEDIUM",
                "min_balance": Decimal('100')
            },
            StrategyType.FLASH_DROP_CATCHER.value: {
                "name": "🚀 Flash Drop Catcher",
                "description": (
                    "Стратегия ловли резких падений для входа в LONG.\n"
                    "Сканирует все фьючерсные пары на резкие движения вниз.\n"
                    "Открывает позиции на отскок с trailing stop."
                ),
                "risk_level": "MEDIUM",
                "min_balance": Decimal('100')
            }
        }

callback_handler = CallbackHandler(None)  # EventBus будет инициализирован позже


def set_event_bus(event_bus: EventBus):
    """Установка EventBus для callback handler"""
    callback_handler.event_bus = event_bus

# Главное меню
@router.callback_query(F.data == "main_menu")
async def callback_main_menu(callback: CallbackQuery, state: FSMContext):
    """Возврат в главное меню"""
    user_id = callback.from_user.id
    
    try:
        await state.clear()
        
        # Получаем статус пользователя
        user_profile = await db_manager.get_user(user_id)
        if not user_profile:
            await callback.answer("❌ Пользователь не найден", show_alert=True)
            return
        
        # Получаем статус сессии
        session_status = await redis_manager.get_user_session(user_id)
        # Проверяем ключ 'running', который реально сохраняется в UserSession
        is_active = session_status.get('running', False) if session_status else False
        
        status_text = "🟢 Активен" if is_active else "🔴 Неактивен"
        
        text = (
            f"🏠 <b>Главное меню</b>\n\n"
            f"👤 Пользователь: {user_profile.username or 'Не указано'}\n"
            f"📊 Статус: {status_text}\n"
            f"💰 Общая прибыль: {format_currency(user_profile.total_profit)}\n"
            f"📈 Всего сделок: {user_profile.total_trades}\n"
            f"🎯 Win Rate: {format_percentage(user_profile.win_rate)}\n\n"
            f"Выберите действие:"
        )
        
        await callback.message.edit_text(
            text,
            reply_markup=get_main_menu_keyboard(),
            parse_mode="HTML"
        )
        
        log_info(user_id, "Пользователь вернулся в главное меню", module_name='callback')
        
    except Exception as e:
        log_error(user_id, f"Ошибка в главном меню: {e}", module_name='callback')
        await callback.answer("❌ Произошла ошибка", show_alert=True)


# >>> НАЧАЛО НОВОГО БЛОКА: ОБРАБОТЧИКИ СТАТИСТИКИ <<<

async def _generate_stats_report(user_id: int, start_date: Optional[datetime] = None,
                                 end_date: Optional[datetime] = None, period_name: str = "за всё время") -> str:
    """Вспомогательная функция для генерации текста отчета по статистике за указанный период."""

    # Получаем статистику за указанный период
    user_stats = await db_manager.get_user_stats_by_period(user_id, start_date, end_date)
    strategy_stats = await db_manager.get_strategy_stats_by_period(user_id, start_date, end_date)

    if not user_stats or user_stats.get('total_trades', 0) == 0:
        return f"📊 <b>Статистика {period_name}</b>\n\n❌ Нет данных за указанный период."

    # Общая статистика
    total_trades = user_stats['total_trades']
    winning_trades = user_stats['winning_trades']
    net_profit = user_stats['net_profit']
    total_commission = user_stats['total_commission']
    win_rate = user_stats['win_rate']
    profit_percentage = user_stats['profit_percentage']

    profit_emoji = "🟢" if net_profit >= 0 else "🔴"
    profit_sign = "+" if net_profit >= 0 else ""

    stats_text = (
        f"📊 <b>Статистика {period_name}</b>\n"
        f"{'═' * 30}\n\n"
        f"💰 <b>Чистая прибыль:</b> {profit_emoji} {profit_sign}{format_currency(net_profit)}\n"
        f"📈 <b>Всего сделок:</b> {total_trades}\n"
        f"🎯 <b>Успешных сделок:</b> {winning_trades}\n"
        f"🏆 <b>Win Rate:</b> {format_percentage(win_rate)}\n"
        f"📊 <b>Доходность к депозиту:</b> {profit_emoji} {profit_sign}{format_percentage(profit_percentage)}\n"
        f"💸 <b>Комиссии биржи:</b> {format_currency(total_commission)}\n"
    )

    if strategy_stats:
        stats_text += f"\n━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━\n"
        stats_text += f"🏆 <b>Статистика по стратегиям:</b>\n\n"

        for stat in strategy_stats:
            strategy_name = stat['strategy_type'].replace('_', ' ').title()

            # Переводим названия стратегий на русский
            if strategy_name == 'Signal Scalper':
                strategy_name = 'Signal Scalper'

            net_pnl = stat['net_pnl']
            trades = stat['total_trades']
            wins = stat['winning_trades']
            strategy_win_rate = stat['win_rate']
            strategy_profit_percentage = stat['profit_percentage']

            pnl_emoji = "🟢" if net_pnl >= 0 else "🔴"
            pnl_sign = "+" if net_pnl >= 0 else ""
            percent_sign = "+" if strategy_profit_percentage >= 0 else ""

            stats_text += (
                f"🔹 <b>{strategy_name}</b>\n"
                f"   {pnl_emoji} <b>Прибыль:</b> {pnl_sign}{format_currency(net_pnl)}\n"
                f"   📊 <b>Доходность:</b> {pnl_emoji} {percent_sign}{format_percentage(strategy_profit_percentage)}\n"
                f"   📈 <b>Сделок:</b> {trades} | <b>Win Rate:</b> {format_percentage(strategy_win_rate)}\n\n"
            )
    else:
        stats_text += f"\n⚠️ Нет данных по стратегиям за указанный период."

    # Добавляем общую доходность в конце
    stats_text += f"━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━\n"
    stats_text += f"💯 <b>Общая доходность к депозиту {period_name}:</b> {profit_emoji} {profit_sign}{format_percentage(profit_percentage)}"

    return stats_text


@router.callback_query(F.data.startswith("stats_period_"))
async def callback_stats_period(callback: CallbackQuery, state: FSMContext):
    """Обрабатывает выбор периода и показывает статистику."""
    user_id = callback.from_user.id
    period = callback.data.replace("stats_period_", "")

    await callback.message.edit_text("⏳ <i>Генерирую отчет...</i>", parse_mode="HTML")

    if period == "day":
        # Статистика за сутки (с 00:00 по МСК до текущего времени)
        moscow_tz = timezone(timedelta(hours=3))
        now_msk = datetime.now(moscow_tz)
        start_of_day_msk = now_msk.replace(hour=0, minute=0, second=0, microsecond=0)

        report_text = await _generate_stats_report(
            user_id,
            start_date=start_of_day_msk,
            end_date=now_msk,
            period_name="за сегодня"
        )

    elif period == "all":
        # Статистика за всё время
        report_text = await _generate_stats_report(user_id, period_name="за всё время")

    elif period == "month_select":
        # Показываем меню выбора месяца
        available_months = await db_manager.get_available_months(user_id)

        if not available_months:
            await callback.message.edit_text(
                "📊 <b>Статистика по месяцам</b>\n\n❌ Нет данных о торговле за какие-либо месяцы.",
                parse_mode="HTML",
                reply_markup=get_back_keyboard("main_menu")
            )
            return

        # Создаём клавиатуру с доступными месяцами
        month_buttons = []
        for month in available_months:
            month_buttons.append([{
                "text": month['display'],  # "September 2024"
                "callback_data": f"stats_period_{month['key']}"  # "stats_period_2024-09"
            }])

        month_buttons.append([{"text": "🔙 Назад", "callback_data": "main_menu"}])

        from ..keyboards.inline import KeyboardBuilder
        month_keyboard = KeyboardBuilder.build_keyboard(month_buttons)

        await callback.message.edit_text(
            "📊 <b>Выберите месяц для статистики:</b>",
            parse_mode="HTML",
            reply_markup=month_keyboard
        )
        await callback.answer()
        return

    else:  # Обработка выбора конкретного месяца (формат: "2024-09")
        try:
            # Парсим год и месяц из строки
            year, month = map(int, period.split('-'))

            # Создаём границы месяца по московскому времени
            moscow_tz = timezone(timedelta(hours=3))
            start_date = datetime(year, month, 1, tzinfo=moscow_tz)

            # Конец месяца
            if month == 12:
                end_date = datetime(year + 1, 1, 1, tzinfo=moscow_tz) - timedelta(seconds=1)
            else:
                end_date = datetime(year, month + 1, 1, tzinfo=moscow_tz) - timedelta(seconds=1)

            # Название месяца для отображения
            month_names = {
                1: "январь", 2: "февраль", 3: "март", 4: "апрель", 5: "май", 6: "июнь",
                7: "июль", 8: "август", 9: "сентябрь", 10: "октябрь", 11: "ноябрь", 12: "декабрь"
            }
            period_name = f"за {month_names[month]} {year}"

            report_text = await _generate_stats_report(
                user_id,
                start_date=start_date,
                end_date=end_date,
                period_name=period_name
            )
        except (ValueError, KeyError, IndexError):
            report_text = "❌ Ошибка обработки выбранного периода."

    await callback.message.edit_text(report_text, parse_mode="HTML", reply_markup=get_back_keyboard("main_menu"))
    await callback.answer()

# Настройки
@router.callback_query(F.data == "settings")
async def callback_settings(callback: CallbackQuery, state: FSMContext):
    """Главное меню настроек"""
    user_id = callback.from_user.id

    try:
        text = (
            f"⚙️ <b>Настройки</b>\n\n"
            f"Выберите категорию настроек:"
        )

        await callback.message.edit_text(
            text,
            reply_markup=get_settings_keyboard(),
            parse_mode="HTML"
        )

    except Exception as e:
        log_error(user_id, f"Ошибка в настройках: {e}", module_name='callback')
        await callback.answer("❌ Ошибка загрузки настроек", show_alert=True)





@router.callback_query(F.data == "strategy_settings")
async def callback_strategy_settings(callback: CallbackQuery, state: FSMContext):
    """Настройки стратегий"""
    user_id = callback.from_user.id

    try:
        text = (
            f"📊 <b>Настройки стратегий</b>\n\n"
            f"Здесь вы можете настроить параметры для каждой стратегии, а также включить или отключить их для автоматической торговли.\n\n"
            f"Выберите стратегию для настройки:"
        )

        # Получаем статусы (включена/выключена) для отображения в меню
        all_strategy_configs = {}
        for s_type in callback_handler.strategy_descriptions.keys():
            config_enum = getattr(ConfigType, f"STRATEGY_{s_type.upper()}", None)
            if config_enum:
                config = await redis_manager.get_config(user_id, config_enum)
                all_strategy_configs[s_type] = config or {}

        await callback.message.edit_text(
            text,
            reply_markup=get_strategy_settings_keyboard(all_strategy_configs),
            parse_mode="HTML"
        )
        await callback.answer()

    except Exception as e:
        log_error(user_id, f"Ошибка в настройках стратегий: {e}", module_name='callback')
        await callback.answer("❌ Ошибка загрузки настроек", show_alert=True)


# --- НОВЫЙ БЛОК ДЛЯ УПРАВЛЕНИЯ НАСТРОЙКАМИ СТРАТЕГИЙ ---

# ЗАМЕНИТЕ ВАШУ СТАРУЮ ФУНКЦИЮ НА ЭТУ:

@router.callback_query(F.data.startswith("configure_strategy_"))
async def callback_configure_strategy(callback: CallbackQuery, state: FSMContext,
                                      strategy_type_override: Optional[str] = None):
    """Отображает меню настройки для конкретной стратегии."""
    user_id = callback.from_user.id
    strategy_type = "unknown"  # Инициализируем, чтобы избежать ошибок
    try:
        if strategy_type_override:
            strategy_type = strategy_type_override
        else:
            parts = callback.data.split('_')
            strategy_type = "_".join(parts[2:])

        # --- ИЗМЕНЕНИЕ 1: Надежное получение типа конфигурации ---
        # Вместо getattr, который вызывал ошибку, используем явную карту соответствия.
        strategy_enum_map = {
            StrategyType.SIGNAL_SCALPER.value: ConfigType.STRATEGY_SIGNAL_SCALPER,
            StrategyType.FLASH_DROP_CATCHER.value: ConfigType.STRATEGY_FLASH_DROP_CATCHER
        }
        config_enum = strategy_enum_map.get(strategy_type)

        if not config_enum:
            log_error(user_id, f"Не найдено соответствие ConfigType для стратегии: '{strategy_type}'", 'callback')
            await callback.answer("❌ Внутренняя ошибка конфигурации.", show_alert=True)
            return

        # --- 2: Гарантированное слияние настроек ---
        # Это гарантирует, что меню всегда будет полным, даже если у пользователя сохранены не все настройки.

        # 1. Загружаем шаблон с полным набором параметров по умолчанию
        all_defaults = DefaultConfigs.get_all_default_configs()["strategy_configs"]
        default_config = all_defaults.get(strategy_type, {})

        # 2. Загружаем сохраненный конфиг пользователя из Redis (если он есть)
        user_config = await redis_manager.get_config(user_id, config_enum) or {}

        # 3. Сливаем конфиги: пользовательские настройки перезаписывают дефолтные
        config = default_config.copy()
        config.update(user_config)

        strategy_info = callback_handler.strategy_descriptions[strategy_type]
        status_text = "✅ Включена" if config.get("is_enabled", False) else "❌ Отключена"

        text = (
            f"⚙️ <b>Настройка: {strategy_info['name']}</b>\n\n"
            f"<b>Статус для автоторговли:</b> {status_text}\n\n"
            f"Нажмите на параметр, чтобы изменить его значение."
        )

        await callback.message.edit_text(
            text,
            reply_markup=get_strategy_config_keyboard(strategy_type, config),
            parse_mode="HTML"
        )
        await callback.answer()

    except Exception as e:
        log_error(user_id, f"Ошибка настройки стратегии '{strategy_type}': {e}", module_name='callback')
        await callback.answer("❌ Ошибка при загрузке настроек.", show_alert=True)




# ---  ОБНОВЛЕННЫЙ ОБРАБОТЧИК ЗАПРОСА НОВОГО ЗНАЧЕНИЯ ---
@router.callback_query(F.data.startswith("set_param_"))
async def callback_set_strategy_parameter(callback: CallbackQuery, state: FSMContext):
    """Обрабатывает нажатие на кнопку параметра и запрашивает новое значение."""
    user_id = callback.from_user.id
    try:
        # Формат: set_param_{strategy_type}_{param_key}
        # Нужно правильно разобрать для flash_drop_catcher (3 части) и signal_scalper (2 части)
        parts = callback.data.split("_")

        # Определяем, где заканчивается имя стратегии
        # Известные стратегии: signal_scalper, flash_drop_catcher
        if len(parts) >= 5 and f"{parts[2]}_{parts[3]}_{parts[4]}" in ["flash_drop_catcher"]:
            strategy_type = f"{parts[2]}_{parts[3]}_{parts[4]}"
            param_key = "_".join(parts[5:])
        else:
            strategy_type = f"{parts[2]}_{parts[3]}"
            param_key = "_".join(parts[4:])

        # Используем НОВОЕ, единое состояние
        await state.set_state(UserStates.AWAITING_STRATEGY_PARAM_VALUE)
        await state.update_data(
            # Добавляем флаг, чтобы понимать, что мы редактируем сохраненный конфиг
            is_manual_config=False,
            editing_strategy_type=strategy_type,
            editing_param_key=param_key,
            menu_message_id=callback.message.message_id
        )

        await callback.message.edit_text(
            f"✏️ Введите новое значение для <b>{param_key}</b>:",
            parse_mode="HTML",
            reply_markup=get_back_keyboard(f"reconfigure_{strategy_type}")
        )
        await callback.answer()
    except Exception as e:
        log_error(user_id, f"Ошибка входа в режим редактирования параметра: {e}", "callback")


# --- ОБНОВЛЕННЫЙ ОБРАБОТЧИК СОХРАНЕНИЯ ЗНАЧЕНИЯ ---

@router.message(UserStates.AWAITING_STRATEGY_PARAM_VALUE)
async def process_strategy_param_value(message: Message, state: FSMContext):
    """
    Принимает, валидирует и сохраняет новое значение параметра.
    Работает как для настроек, так и для ручного запуска.
    """
    user_id = message.from_user.id
    try:
        user_data = await state.get_data()

        # Проверяем флаг, чтобы понять, какой сценарий выполнять
        is_manual = user_data.get("is_manual_config", False)

        # Валидация
        new_value_str = message.text.strip().replace(',', '.')
        new_value = float(new_value_str)

        if is_manual:
            # --- Сценарий ручной настройки ---
            param_key = user_data.get("param_to_edit")
            strategy_type = user_data.get("manual_strategy_type")
            current_config = user_data.get("manual_config", {})
            current_config[param_key] = new_value

            await state.update_data(manual_config=current_config)
            await state.set_state(UserStates.MANUAL_STRATEGY_CONFIGURE)

            await message.delete()

            # Обновляем меню ручной настройки
            # (Предполагаем, что message_id был сохранен ранее)
            menu_message_id = user_data.get("menu_message_id")
            if menu_message_id:
                await bot_manager.bot.edit_message_text(
                    chat_id=user_id,
                    message_id=menu_message_id,
                    text=f"🛠️ <b>Ручной запуск:</b> значение {param_key} обновлено.",
                    parse_mode="HTML",
                    reply_markup=get_strategy_config_keyboard(strategy_type, current_config)

                )

        else:
            # --- Сценарий редактирования сохраненных настроек ---
            strategy_type = user_data.get("editing_strategy_type")
            param_key = user_data.get("editing_param_key")
            menu_message_id = user_data.get("menu_message_id")

            # Надежное сохранение слиянием
            config_enum = getattr(ConfigType, f"STRATEGY_{strategy_type.upper()}")
            all_defaults = DefaultConfigs.get_all_default_configs()["strategy_configs"]
            final_config = all_defaults.get(strategy_type, {}).copy()
            user_config = await redis_manager.get_config(user_id, config_enum) or {}
            final_config.update(user_config)
            final_config[param_key] = new_value
            await redis_manager.save_config(user_id, config_enum, final_config)

            log_info(user_id, f"Обновлен параметр {param_key}={new_value} для стратегии {strategy_type}", "callback")

            await message.delete()
            await state.clear()

            # Обновляем меню настроек
            await _show_strategy_config_menu(message.bot, user_id, menu_message_id, strategy_type, user_id)

    except (ValueError, TypeError):
        await message.answer("❌ Некорректный формат. Введите числовое значение.")
    except Exception as e:
        log_error(user_id, f"Ошибка сохранения параметра стратегии: {e}", "callback")
        await message.answer("❌ Произошла ошибка при сохранении значения.")


@router.callback_query(F.data.startswith("toggle_strategy_"))
async def callback_toggle_strategy(callback: CallbackQuery, state: FSMContext):
    """Включает или отключает стратегию для автоторговли."""
    user_id = callback.from_user.id
    strategy_type = callback.data.replace("toggle_strategy_", "")

    try:
        config_enum = getattr(ConfigType, f"STRATEGY_{strategy_type.upper()}")
        config = await redis_manager.get_config(user_id, config_enum)
        if not config:
            config = DefaultConfigs.get_all_default_configs()["strategy_configs"][strategy_type]

        is_enabled = not config.get("is_enabled", False)
        config["is_enabled"] = is_enabled
        await redis_manager.save_config(user_id, config_enum, config)

        status_text = "включена" if is_enabled else "отключена"
        await callback.answer(f"Стратегия {status_text} для автоторговли.", show_alert=True)

        # ВАЖНО: Если стратегия включена И автоторговля активна - запускаем стратегию
        if is_enabled and _bot_application:
            user_session = _bot_application.active_sessions.get(user_id)
            if user_session and user_session.running:
                log_info(user_id, f"Автоторговля активна, запускаем стратегию {strategy_type}", "callback")

                # Получаем список символов для торговли
                global_config = await redis_manager.get_config(user_id, ConfigType.GLOBAL)
                watchlist_symbols = global_config.get("watchlist_symbols", []) if global_config else []

                if not watchlist_symbols:
                    log_warning(user_id, f"Нет символов в watchlist для запуска {strategy_type}", "callback")
                else:
                    # Запускаем стратегию для каждого символа (как при /autotrade_start)
                    for symbol in watchlist_symbols:
                        try:
                            await user_session.start_strategy(strategy_type, symbol)
                            log_info(user_id, f"Стратегия {strategy_type} запущена для {symbol}", "callback")
                        except Exception as e:
                            log_error(user_id, f"Ошибка запуска {strategy_type} для {symbol}: {e}", "callback")

        await callback_configure_strategy(callback, state, strategy_type_override=strategy_type)

    except Exception as e:
        log_error(user_id, f"Ошибка переключения стратегии {strategy_type}: {e}", module_name='callback')


@router.callback_query(F.data.startswith("toggle_param_"))
async def callback_toggle_param(callback: CallbackQuery, state: FSMContext):
    """Переключает boolean параметр стратегии (вкл/выкл)."""
    user_id = callback.from_user.id

    try:
        # Парсим callback_data: toggle_param_{strategy_type}_{param_name}
        # ИСПРАВЛЕНО: Используем правильный парсинг для multi-word strategy names
        parts = callback.data.split("_")

        # Определяем, где заканчивается имя стратегии
        # Известные стратегии: signal_scalper, flash_drop_catcher
        if len(parts) >= 5 and f"{parts[2]}_{parts[3]}_{parts[4]}" in ["flash_drop_catcher"]:
            strategy_type = f"{parts[2]}_{parts[3]}_{parts[4]}"
            param_name = "_".join(parts[5:])
        else:
            # signal_scalper и другие 2-словные стратегии
            strategy_type = f"{parts[2]}_{parts[3]}"
            param_name = "_".join(parts[4:])

        # Получаем конфигурацию
        config_enum = getattr(ConfigType, f"STRATEGY_{strategy_type.upper()}")
        config = await redis_manager.get_config(user_id, config_enum)
        if not config:
            config = DefaultConfigs.get_all_default_configs()["strategy_configs"][strategy_type]

        # Переключаем boolean значение
        current_value = config.get(param_name, True)
        new_value = not current_value
        config[param_name] = new_value

        # Сохраняем
        await redis_manager.save_config(user_id, config_enum, config)

        # Определяем человекочитаемое название
        param_names_ru = {
            "enable_stop_loss": "Stop Loss",
            "enable_stagnation_detector": "Усреднение #1 (Детектор застревания)",
            "enable_averaging": "Усреднение #2 (Основное)"
        }
        param_name_ru = param_names_ru.get(param_name, param_name)
        status_text = "включено" if new_value else "отключено"

        await callback.answer(f"{param_name_ru}: {status_text}", show_alert=False)
        log_info(user_id, f"Параметр {param_name}={new_value} для {strategy_type}", "callback")

        # Обновляем меню
        await callback_configure_strategy(callback, state, strategy_type_override=strategy_type)

    except Exception as e:
        log_error(user_id, f"Ошибка переключения параметра: {e}", module_name='callback')
        await callback.answer("❌ Произошла ошибка", show_alert=True)


@router.callback_query(F.data == "save_and_exit_strategy_config")
async def callback_save_and_exit_strategy_config(callback: CallbackQuery, state: FSMContext):
    """Обрабатывает кнопку 'Сохранить и выйти'."""
    await callback.answer("✅ Настройки сохранены!", show_alert=False)
    await callback_strategy_settings(callback, state)


@router.callback_query(F.data.startswith("reconfigure_"))
async def callback_reconfigure_strategy(callback: CallbackQuery, state: FSMContext):
    """Возврат в меню настройки конкретной стратегии из режима ввода значения."""
    strategy_type = callback.data.replace("reconfigure_", "")
    # Переиспользуем основной обработчик для отображения меню
    await callback_configure_strategy(callback, state, strategy_type_override=strategy_type)


# -- ОБРАБОИЧИКИ ВЫБОРА СТРАТЕГИИ для настройки

# --- 1. НОВАЯ ВСПОМОГАТЕЛЬНАЯ ФУНКЦИЯ ---
async def _show_strategy_config_menu(bot, chat_id: int, message_id: int, strategy_type: str, user_id: int):
    """
    Отображает меню настройки стратегии, гарантируя слияние
    конфигурации по умолчанию с пользовательской. (ИСПРАВЛЕННАЯ ВЕРСИЯ)
    """
    # !!! ИСПРАВЛЕНИЕ: Инициализируем переменные до блока try
    text = "❌ Ошибка: не удалось сформировать меню настроек."
    reply_markup = get_back_keyboard("strategy_settings") # Клавиатура "Назад" по умолчанию

    try:
        # Шаг 1: Загружаем шаблон с полным набором параметров
        all_defaults = DefaultConfigs.get_all_default_configs()["strategy_configs"]
        default_config = all_defaults.get(strategy_type, {})

        # Шаг 2: Загружаем конфиг пользователя из Redis
        config_enum = getattr(ConfigType, f"STRATEGY_{strategy_type.upper()}")
        user_config = await redis_manager.get_config(user_id, config_enum) or {}

        # Шаг 3: Сливаем конфиги. Пользовательские настройки перезаписывают дефолтные.
        final_config = default_config.copy()
        final_config.update(user_config)

        strategy_info = callback_handler.strategy_descriptions[strategy_type]
        status_text = "✅ Включена" if final_config.get("is_enabled", False) else "❌ Отключена"

        # Переопределяем переменные с корректными данными
        text = (
            f"⚙️ <b>Настройка: {strategy_info['name']}</b>\n\n"
            f"<b>Статус для автоторговли:</b> {status_text}\n\n"
            f"Нажмите на параметр, чтобы изменить его значение."
        )
        reply_markup = get_strategy_config_keyboard(strategy_type, final_config)

        # --- ОТКАЗОУСТОЙЧИВОЕ ОБНОВЛЕНИЕ ---
        await bot.edit_message_text(
            text=text,
            chat_id=chat_id,
            message_id=message_id,
            reply_markup=reply_markup,
            parse_mode="HTML"
        )
    except TelegramBadRequest as e:
        if "message is not modified" in e.message:
            pass
        else:
            log_error(user_id, f"Ошибка Telegram API при обновлении меню стратегии: {e}", "callback")
            await bot.send_message(chat_id, text, reply_markup=reply_markup, parse_mode="HTML")
    except Exception as e:
        log_error(user_id, f"Критическая ошибка в _show_strategy_config_menu: {e}", "callback")



# Статистика
@router.callback_query(F.data == "statistics")
async def callback_statistics(callback: CallbackQuery, state: FSMContext):
    """Показ статистики пользователя"""
    user_id = callback.from_user.id

    try:
        # Получаем профиль пользователя из БД
        user_profile = await db_manager.get_user(user_id)
        if not user_profile:
            await callback.answer("❌ Профиль не найден", show_alert=True)
            return

        # Получаем последние сделки из БД
        recent_trades = await db_manager.get_user_trades(user_id, limit=10)
        # Получаем статус сессии из Redis, а не через прямое обращение к API
        session_status = await redis_manager.get_user_session(user_id)

        text = (
            f"📊 <b>Статистика торговли</b>\n\n"
            f"👤 <b>Пользователь:</b> {user_profile.username or 'Не указано'}\n"
            f"📅 <b>Регистрация:</b> {user_profile.registration_date.strftime('%d.%m.%Y') if user_profile.registration_date else 'Не указано'}\n\n"
            f"💰 <b>Общая прибыль:</b> {format_currency(user_profile.total_profit)}\n"
            f"📈 <b>Всего сделок:</b> {user_profile.total_trades}\n"
            f"🎯 <b>Win Rate:</b> {format_percentage(user_profile.win_rate)}\n"
            f"📉 <b>Макс. просадка:</b> {format_percentage(user_profile.max_drawdown)}\n\n"
        )

        # Используем данные из сессии Redis для отображения статуса
        if session_status and session_status.get('running'):
            active_strategies = session_status.get('active_strategies', [])
            text += f"🟢 <b>Статус:</b> Активен\n"
            text += f"📊 <b>Активных стратегий:</b> {len(active_strategies)}\n"
            if active_strategies:
                # Преобразуем список словарей в строку
                strategy_names = [s.get('strategy_type', 'N/A') for s in active_strategies]
                text += f"🔄 <b>Стратегии:</b> {', '.join(strategy_names)}\n"
        else:
            text += f"🔴 <b>Статус:</b> Неактивен\n"

        text += f"\n📋 <b>Последние сделки:</b>\n"

        if recent_trades:
            for i, trade in enumerate(recent_trades[:5], 1):
                profit_emoji = "📈" if trade.profit > 0 else "📉"
                text += (
                    f"{i}. {profit_emoji} {trade.symbol} "
                    f"{format_currency(trade.profit)} "
                    f"({trade.entry_time.strftime('%d.%m %H:%M') if trade.entry_time else 'N/A'})\n"
                )
        else:
            text += "Сделок пока нет\n"
        await callback.message.edit_text(
            text,
            reply_markup=get_main_menu_keyboard(),
            parse_mode="HTML"
        )
    except Exception as e:
        log_error(user_id, f"Ошибка получения статистики: {e}", module_name='callback')
        await callback.answer("❌ Ошибка загрузки статистики", show_alert=True)




@router.callback_query(F.data == "cancel")
async def callback_cancel(callback: CallbackQuery, state: FSMContext):
    """Отмена текущего действия"""
    user_id = callback.from_user.id
    
    try:
        await state.clear()
        await callback_main_menu(callback, state)
        log_info(user_id, "Пользователь отменил действие", module_name='callback')
        
    except Exception as e:
        log_error(user_id, f"Ошибка отмены: {e}", module_name='callback')
        await callback.answer("❌ Ошибка отмены", show_alert=True)


# --- Обработчики кнопок из главного меню подтверждение экстренной остановки---

@router.callback_query(F.data == "confirm_emergency_stop")
async def callback_confirm_emergency_stop(callback: CallbackQuery, state: FSMContext):
    """
    Обрабатывает подтверждение экстренной остановки.
    """
    user_id = callback.from_user.id
    await callback.answer("🚨 Выполняю экстренную остановку...", show_alert=True)
    try:
        if callback_handler.event_bus:
            await callback_handler.event_bus.publish(
                UserSessionStopRequestedEvent(
                    user_id=user_id,
                    reason="EMERGENCY_STOP"
                )
            )
            await callback.message.edit_text(
                "🚨 <b>ЭКСТРЕННАЯ ОСТАНОВКА АКТИВИРОВАНА</b>\n\n"
                "Все торговые операции будут немедленно прекращены, а позиции закрыты.",
                parse_mode="HTML"
            )
            log_warning(user_id, "Пользователь подтвердил экстренную остановку", module_name='callback')
        else:
            await callback.message.edit_text("❌ Системная ошибка: шина событий недоступна.", parse_mode="HTML")

    except Exception as e:
        log_error(user_id, f"Ошибка при подтверждении экстренной остановки: {e}", module_name='callback')


@router.callback_query(F.data == "cancel_emergency_stop")
async def callback_cancel_emergency_stop(callback: CallbackQuery, state: FSMContext):
    """
    Обрабатывает отмену экстренной остановки.
    """
    user_id = callback.from_user.id
    await callback.answer("❌ Экстренная остановка отменена", show_alert=True)
    try:
        await callback.message.edit_text(
            "✅ <b>Экстренная остановка отменена</b>\n\n"
            "Торговля продолжается в обычном режиме.",
            parse_mode="HTML"
        )
        log_info(user_id, "Пользователь отменил экстренную остановку", module_name='callback')

    except Exception as e:
        log_error(user_id, f"Ошибка при отмене экстренной остановки: {e}", module_name='callback')


# --- Обработчики кнопок из главного меню ---

@router.callback_query(F.data == "show_balance")
async def callback_show_balance(callback: CallbackQuery, state: FSMContext):
    """Обработчик кнопки 'Баланс'"""
    user_id = callback.from_user.id
    await callback.answer("Запрашиваю баланс...")

    # === MULTI-ACCOUNT SUPPORT - проверяем все ключи ===
    all_api_keys = await db_manager.get_all_user_api_keys(user_id, "bybit")

    if not all_api_keys or len(all_api_keys) == 0:
        await callback.message.edit_text(
            "⚠️ <b>API ключи не настроены.</b>\n\nНе могу получить баланс. Для начала работы необходимо добавить API ключи от вашего аккаунта Bybit.\n\nПерейдите в 'API ключи' в главном меню.",
            parse_mode="HTML",
            reply_markup=get_back_keyboard("main_menu")
        )
        return

    try:
        exchange_config = system_config.get_exchange_config("bybit")
        use_demo = exchange_config.demo if exchange_config else False

        # === MULTI-ACCOUNT РЕЖИМ (3 аккаунта) ===
        if is_multi_account_mode(all_api_keys):
            log_info(user_id, "Получение баланса (callback) в multi-account режиме", "callback")

            # Получаем баланс со всех 3 аккаунтов через helper функцию
            balance_info = await get_multi_account_balance(user_id, all_api_keys, use_demo)

            if not balance_info['accounts_data']:
                await callback.message.edit_text(
                    "❌ Не удалось получить баланс ни с одного аккаунта.",
                    reply_markup=get_back_keyboard("main_menu")
                )
                return

            # Формируем сообщение для multi-account режима через helper функцию
            balance_text = format_multi_account_balance(
                total_equity=balance_info['total_equity'],
                total_available=balance_info['total_available'],
                total_unrealised_pnl=balance_info['total_unrealised_pnl'],
                accounts_data=balance_info['accounts_data']
                # verbose=True по умолчанию - полная версия как в оригинале
            )

            await callback.message.edit_text(
                balance_text,
                parse_mode="HTML",
                reply_markup=get_main_menu_keyboard()
            )

        # === ОБЫЧНЫЙ РЕЖИМ (1 аккаунт) ===
        else:
            log_info(user_id, "Получение баланса (callback) в обычном режиме", "callback")

            keys = await db_manager.get_api_keys(user_id, "bybit", account_priority=1)
            if not keys:
                await callback.message.edit_text(
                    "❌ PRIMARY ключ не найден.",
                    reply_markup=get_back_keyboard("main_menu")
                )
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
                await callback.message.edit_text(
                    balance_text,
                    parse_mode="HTML",
                    reply_markup=get_main_menu_keyboard()
                )
            else:
                error_message = balance_data.get("retMsg", "Проверьте права ваших API ключей")
                await callback.message.edit_text(
                    f"❌ Не удалось получить данные о балансе: {error_message}",
                    reply_markup=get_back_keyboard("main_menu")
                )

    except Exception as e:
        log_error(user_id, f"Ошибка получения баланса по кнопке: {e}", module_name='callback')
        await callback.message.edit_text(
            "❌ Произошла ошибка при запросе баланса.",
            reply_markup=get_back_keyboard("main_menu")
        )


@router.callback_query(F.data == "api_keys")
async def callback_api_keys(callback: CallbackQuery, state: FSMContext):
    """Обработчик кнопки 'API ключи' (Multi-Account Support)"""
    user_id = callback.from_user.id
    await callback.answer()

    try:
        # Получаем ВСЕ API ключи пользователя
        all_keys = await db_manager.get_all_user_api_keys(user_id, "bybit")
        api_keys_count = len(all_keys)

        if api_keys_count == 0:
            # Ключей нет
            text = (
                f"🔑 <b>Управление API ключами Bybit</b>\n\n"
                f"🔴 <b>API ключи не настроены</b>\n\n"
                f"Для работы бота необходимо добавить минимум 1 API ключ от Bybit.\n\n"
                f"💡 <b>Multi-Account режим:</b>\n"
                f"   • 1 ключ → обычный режим (1 бот)\n"
                f"   • 3 ключа → Multi-Account режим (3 бота с ротацией)\n\n"
                f"Нажмите кнопку ниже, чтобы добавить PRIMARY ключ."
            )
        elif api_keys_count == 1:
            # Есть PRIMARY ключ
            primary_key = all_keys[0]
            api_key_short = primary_key['api_key'][:4] + '...' + primary_key['api_key'][-4:]
            text = (
                f"🔑 <b>Управление API ключами Bybit</b>\n\n"
                f"✅ <b>Настроено ключей: {api_keys_count}/3</b>\n\n"
                f"<b>🥇 PRIMARY (Bot 1):</b> <code>{api_key_short}</code>\n"
                f"🔘 SECONDARY (Bot 2): <i>не настроен</i>\n"
                f"🔘 TERTIARY (Bot 3): <i>не настроен</i>\n\n"
                f"💡 Работает в обычном режиме (1 бот).\n"
                f"   Добавьте 2 дополнительных ключа для Multi-Account режима."
            )
        elif api_keys_count == 2:
            # Есть PRIMARY и SECONDARY
            primary_key = all_keys[0]
            secondary_key = all_keys[1]
            primary_short = primary_key['api_key'][:4] + '...' + primary_key['api_key'][-4:]
            secondary_short = secondary_key['api_key'][:4] + '...' + secondary_key['api_key'][-4:]
            text = (
                f"🔑 <b>Управление API ключами Bybit</b>\n\n"
                f"✅ <b>Настроено ключей: {api_keys_count}/3</b>\n\n"
                f"<b>🥇 PRIMARY (Bot 1):</b> <code>{primary_short}</code>\n"
                f"<b>🥈 SECONDARY (Bot 2):</b> <code>{secondary_short}</code>\n"
                f"🔘 TERTIARY (Bot 3): <i>не настроен</i>\n\n"
                f"⚠️ Почти готово!\n"
                f"   Добавьте 3-й ключ для полноценного Multi-Account режима."
            )
        else:  # api_keys_count >= 3
            # Все 3 ключа настроены - Multi-Account режим АКТИВЕН
            primary_key = all_keys[0]
            secondary_key = all_keys[1]
            tertiary_key = all_keys[2]
            primary_short = primary_key['api_key'][:4] + '...' + primary_key['api_key'][-4:]
            secondary_short = secondary_key['api_key'][:4] + '...' + secondary_key['api_key'][-4:]
            tertiary_short = tertiary_key['api_key'][:4] + '...' + tertiary_key['api_key'][-4:]
            text = (
                f"🔑 <b>Управление API ключами Bybit</b>\n\n"
                f"🎉 <b>Multi-Account режим АКТИВЕН!</b>\n"
                f"✅ Настроено ключей: {api_keys_count}/3\n\n"
                f"<b>🥇 PRIMARY (Bot 1):</b> <code>{primary_short}</code>\n"
                f"<b>🥈 SECONDARY (Bot 2):</b> <code>{secondary_short}</code>\n"
                f"<b>🥉 TERTIARY (Bot 3):</b> <code>{tertiary_short}</code>\n\n"
                f"🔀 Система автоматически управляет 3 ботами:\n"
                f"   • Bot 1 активен по умолчанию\n"
                f"   • Bot 2 активируется, если Bot 1 застрял\n"
                f"   • Bot 3 активируется, если Bot 2 застрял\n"
                f"   • Автоматическая деактивация свободных ботов"
            )

        from ..keyboards.inline import get_api_keys_keyboard
        await callback.message.edit_text(
            text,
            parse_mode="HTML",
            reply_markup=get_api_keys_keyboard(api_keys_count=api_keys_count)
        )
    except Exception as e:
        log_error(user_id, f"Ошибка отображения API ключей: {e}", module_name='callback')
        await callback.message.edit_text(
            "❌ Ошибка загрузки информации о ключах.",
            reply_markup=get_back_keyboard("main_menu")
        )



@router.callback_query(F.data == "general_settings")
async def callback_general_settings(callback: CallbackQuery, state: FSMContext):
    """Обработчик кнопки 'Общие'."""
    user_id = callback.from_user.id
    await callback.answer()

    # В будущем здесь можно будет настраивать, например, язык или часовой пояс
    text = (
        "🌐 <b>Общие настройки</b>\n\n"
        "В данный момент здесь нет доступных для изменения параметров. "
        "Этот раздел зарезервирован для будущих обновлений."
    )
    await callback.message.edit_text(
        text,
        parse_mode="HTML",
        reply_markup=get_back_keyboard("settings")
    )


@router.callback_query(F.data == "reset_settings")
async def callback_reset_settings(callback: CallbackQuery, state: FSMContext):
    """
    Обработчик кнопки 'Сбросить настройки'.
    Показывает пользователю предупреждение и клавиатуру для подтверждения.
    """
    text = (
        "⚠️ <b>Подтверждение действия</b> ⚠️\n\n"
        "Вы уверены, что хотите сбросить ВСЕ ваши настройки к значениям по умолчанию?\n\n"
        "Это действие затронет настройки риска, стратегий и список отслеживания. "
        "API ключи останутся без изменений. Это действие необратимо."
    )
    await callback.message.edit_text(
        text,
        parse_mode="HTML",
        reply_markup=get_confirmation_keyboard("do_reset_settings")
    )


@router.callback_query(F.data == "confirm_do_reset_settings")
async def callback_confirm_reset_settings(callback: CallbackQuery, state: FSMContext):
    """
    Подтверждение и выполнение сброса настроек. (ИСПРАВЛЕННАЯ ВЕРСИЯ)
    """
    user_id = callback.from_user.id
    await callback.answer("Сбрасываю настройки...", show_alert=True)

    try:
        # --- ИСПРАВЛЕННАЯ ЛОГИКА ---
        # 1. Сбрасываем глобальный конфиг
        default_global_config = DefaultConfigs.get_global_config()
        await redis_manager.save_config(user_id, ConfigType.GLOBAL, default_global_config)

        # 2. Сбрасываем конфиги для каждой стратегии, используя правильные ConfigType
        all_default_configs = DefaultConfigs.get_all_default_configs()
        default_strategies = all_default_configs.get("strategy_configs", {})

        for strategy_type, strategy_config in default_strategies.items():
            config_enum = getattr(ConfigType, f"STRATEGY_{strategy_type.upper()}")
            await redis_manager.save_config(user_id, config_enum, strategy_config)
        # --- КОНЕЦ ИСПРАВЛЕННОЙ ЛОГИКИ ---

        log_warning(user_id, "Пользователь сбросил все настройки к значениям по умолчанию.", module_name='callback')

        await callback.message.edit_text(
            "✅ <b>Настройки сброшены</b>\n\nВсе ваши конфигурации были возвращены к значениям по умолчанию.",
            parse_mode="HTML",
            reply_markup=get_back_keyboard("settings")
        )
    except Exception as e:
        log_error(user_id, f"Ошибка при сбросе настроек: {e}", module_name='callback')
        await callback.message.edit_text(
            "❌ Произошла ошибка при сбросе настроек.",
            reply_markup=get_back_keyboard("settings")
        )



@router.callback_query(F.data == "api_settings")
async def callback_api_settings(callback: CallbackQuery, state: FSMContext):
    """Обработчик кнопки 'API ключи' в настройках (Multi-Account Support)"""
    user_id = callback.from_user.id
    await callback.answer()
    try:
        # === MULTI-ACCOUNT SUPPORT - проверяем все ключи ===
        all_api_keys = await db_manager.get_all_user_api_keys(user_id, "bybit")

        if not all_api_keys or len(all_api_keys) == 0:
            # Нет ключей вообще
            text = (
                f"🔑 <b>Настройка API ключей</b>\n\n"
                f"🔴 Ключи не настроены.\n\n"
                f"Для работы бота необходимо добавить API ключи от Bybit."
            )
        elif len(all_api_keys) == 3:
            # Multi-Account режим - все 3 ключа настроены
            priority_names = {1: "🥇 PRIMARY (Bot 1)", 2: "🥈 SECONDARY (Bot 2)", 3: "🥉 TERTIARY (Bot 3)"}
            text = "🔑 <b>Multi-Account режим АКТИВЕН</b>\n\n"

            for key_data in sorted(all_api_keys, key=lambda x: x['priority']):
                priority = key_data['priority']
                api_key = key_data['api_key']
                api_key_short = api_key[:4] + '...' + api_key[-4:]

                text += f"{priority_names[priority]}\n"
                text += f"<b>API Key:</b> <code>{api_key_short}</code>\n\n"

            text += "✅ Все 3 бота настроены для автоматической ротации."
        else:
            # Частичная настройка (1 или 2 ключа)
            priority_names = {1: "🥇 PRIMARY (Bot 1)", 2: "🥈 SECONDARY (Bot 2)", 3: "🥉 TERTIARY (Bot 3)"}
            text = f"🔑 <b>Настроенные API ключи ({len(all_api_keys)}/3)</b>\n\n"

            for key_data in sorted(all_api_keys, key=lambda x: x['priority']):
                priority = key_data['priority']
                api_key = key_data['api_key']
                api_key_short = api_key[:4] + '...' + api_key[-4:]

                text += f"{priority_names[priority]}\n"
                text += f"<b>API Key:</b> <code>{api_key_short}</code>\n\n"

            text += f"⚠️ Для Multi-Account режима добавьте ещё {3 - len(all_api_keys)} ключа.\n"
            text += "Используйте меню '🔑 API ключи' из главного меню."

        await callback.message.edit_text(
            text,
            parse_mode="HTML",
            reply_markup=get_back_keyboard("settings")
        )
    except Exception as e:
        log_error(user_id, f"Ошибка отображения API ключей: {e}", module_name='callback')
        await callback.message.edit_text("❌ Ошибка загрузки информации о ключах.", reply_markup=get_back_keyboard("settings"))


# --- ОБРАБОТЧИКИ НАСТРОЕК СТРАТЕГИЙ ---
@router.callback_query(F.data.in_({"enable_all_strategies", "disable_all_strategies"}))
async def callback_toggle_all_strategies(callback: CallbackQuery, state: FSMContext):
    """Включает или отключает все стратегии."""
    user_id = callback.from_user.id
    enable = callback.data == "enable_all_strategies"

    try:
        current_config = await redis_manager.get_config(user_id, ConfigType.GLOBAL)
        if not current_config:
            await callback.answer("❌ Сначала зайдите в меню настроек.", show_alert=True)
            return

        all_strategy_types = list(DefaultConfigs.get_all_default_configs()["strategy_configs"].keys())
        current_config["enabled_strategies"] = all_strategy_types if enable else []

        await redis_manager.save_config(user_id, ConfigType.GLOBAL, current_config)

        status_text = "включены" if enable else "отключены"
        await callback.answer(f"✅ Все стратегии {status_text}.", show_alert=True)
        log_info(user_id, f"Все стратегии были {status_text}", "callback")
        await callback_strategy_settings(callback, state)  # Обновляем меню

    except Exception as e:
        log_error(user_id, f"Ошибка при переключении всех стратегий: {e}", "callback")
        await callback.answer("❌ Произошла ошибка.", show_alert=True)


async def send_or_edit_symbol_selection_menu(callback_or_message, state: FSMContext, is_edit: bool):
    """Вспомогательная функция для отображения/обновления меню выбора символов."""
    user_id = callback_or_message.from_user.id
    try:
        available_symbols = DEFAULT_SYMBOLS

        # Получаем текущие выборы пользователя из Redis
        user_config = await redis_manager.get_config(user_id, ConfigType.GLOBAL)
        selected_symbols = set(user_config.get("watchlist_symbols", []) if user_config else [])

        text = (
            "<b>📈 Выбор торговых пар</b>\n\n"
            "Выберите пары, по которым бот будет вести торговлю. "
            "Нажмите на символ, чтобы добавить или убрать его (✅).\n\n"
            "После выбора нажмите 'Сохранить'."
        )

        keyboard = get_symbol_selection_keyboard(available_symbols, selected_symbols)

        if is_edit:
            # Используем message из CallbackQuery для редактирования
            await callback_or_message.message.edit_text(text, reply_markup=keyboard, parse_mode="HTML")
        else:
            # Используем Message для отправки нового сообщения
            await callback_or_message.answer(text, reply_markup=keyboard, parse_mode="HTML")

    except Exception as e:
        log_error(user_id, f"Ошибка отображения меню выбора символов: {e}", "callback")
        if is_edit:
            await callback_or_message.answer("❌ Ошибка обновления меню.", show_alert=True)
        else:
            await callback_or_message.answer("❌ Ошибка открытия меню.")


@router.callback_query(F.data == "select_trading_pairs")
async def callback_select_trading_pairs(callback: CallbackQuery, state: FSMContext):
    """Отображает меню выбора торговых пар."""
    await callback.answer()
    await send_or_edit_symbol_selection_menu(callback, state, is_edit=True)


@router.callback_query(F.data.startswith("toggle_symbol_"))
async def callback_toggle_symbol(callback: CallbackQuery, state: FSMContext):
    """Обрабатывает нажатие на символ, добавляя/удаляя его из списка."""
    user_id = callback.from_user.id
    symbol_to_toggle = callback.data.replace("toggle_symbol_", "")

    try:
        user_config = await redis_manager.get_config(user_id, ConfigType.GLOBAL)
        if not user_config:
            user_config = DefaultConfigs.get_global_config()

        selected_symbols = set(user_config.get("watchlist_symbols", []))

        # Добавляем или удаляем символ
        if symbol_to_toggle in selected_symbols:
            selected_symbols.remove(symbol_to_toggle)
        else:
            selected_symbols.add(symbol_to_toggle)

        # Сохраняем обновленный список в конфиг
        user_config["watchlist_symbols"] = list(selected_symbols)
        await redis_manager.save_config(user_id, ConfigType.GLOBAL, user_config)

        # ИСПРАВЛЕНИЕ: Публикуем событие об изменении настроек для hot-reload
        if callback_handler.event_bus:
            settings_event = UserSettingsChangedEvent(
                user_id=user_id,
                changed_settings=["watchlist_symbols"],
                config_type="global"
            )
            log_info(user_id, f"🔄 Публикую событие изменения символов: {symbol_to_toggle}, новый список: {list(selected_symbols)}", "callback")
            await callback_handler.event_bus.publish(settings_event)
        else:
            log_error(user_id, "❌ EventBus недоступен для публикации события изменения настроек!", "callback")

        # Обновляем клавиатуру, чтобы показать изменение
        await send_or_edit_symbol_selection_menu(callback, state, is_edit=True)
        await callback.answer()  # Ответ, чтобы убрать "часики" с кнопки

    except Exception as e:
        log_error(user_id, f"Ошибка переключения символа {symbol_to_toggle}: {e}", "callback")
        await callback.answer("❌ Произошла ошибка.", show_alert=True)


@router.callback_query(F.data == "save_symbol_selection")
async def callback_save_symbol_selection(callback: CallbackQuery, state: FSMContext):
    """Сохраняет выбор и возвращает в меню настроек."""
    user_id = callback.from_user.id

    # ИСПРАВЛЕНИЕ: Публикуем финальное событие об изменении настроек для полного hot-reload
    if callback_handler.event_bus:
        settings_event = UserSettingsChangedEvent(
            user_id=user_id,
            changed_settings=["watchlist_symbols"],
            config_type="global"
        )
        await callback_handler.event_bus.publish(settings_event)

    await callback.answer("✅ Список торговых пар сохранен!", show_alert=True)
    await callback_settings(callback, state)  # Возвращаемся в главное меню настроек


@router.callback_query(F.data.startswith("enable_strategy_") | F.data.startswith("disable_strategy_"))
async def callback_toggle_strategy_global(callback: CallbackQuery, state: FSMContext):
    """Включает или отключает конкретную стратегию в глобальном списке."""
    user_id = callback.from_user.id
    parts = callback.data.split("_")
    action = parts[0]
    strategy_type = "_".join(parts[2:])
    enable = action == "enable"

    try:
        current_config = await redis_manager.get_config(user_id, ConfigType.GLOBAL)
        if not current_config:
            await callback.answer("❌ Сначала зайдите в меню настроек.", show_alert=True)
            return

        enabled_strategies = set(current_config.get("enabled_strategies", []))

        if enable:
            enabled_strategies.add(strategy_type)
        else:
            enabled_strategies.discard(strategy_type)

        current_config["enabled_strategies"] = list(enabled_strategies)
        await redis_manager.save_config(user_id, ConfigType.GLOBAL, current_config)

        status_text = "включена" if enable else "отключена"
        await callback.answer(f"✅ Стратегия {strategy_type} {status_text}.", show_alert=True)
        log_info(user_id, f"Стратегия {strategy_type} была {status_text}", "callback")

        # Обновляем меню, чтобы показать изменения
        await callback_strategy_settings(callback, state)
    except Exception as e:
        log_error(user_id, f"Ошибка при переключении стратегии {strategy_type}: {e}", "callback")
        await callback.answer("❌ Произошла ошибка.", show_alert=True)


@router.callback_query(F.data == "help")
async def callback_help(callback: CallbackQuery, state: FSMContext):
    """Обработчик кнопки 'Помощь'"""
    try:
        from .basic import cmd_help
        await cmd_help(callback.message, state)
        await callback.answer()
    except Exception as e:
        log_error(callback.from_user.id, f"Ошибка при вызове /help из callback: {e}", module_name='callback')
        await callback.answer("Не удалось загрузить справку.", show_alert=True)



@router.callback_query(F.data.in_({"user_guide", "faq", "support"}))
async def callback_help_sections_stub(callback: CallbackQuery, state: FSMContext):
    """Обработчик-заглушка для разделов помощи."""
    section_names = {
        "user_guide": "📖 Руководство пользователя",
        "faq": "❓ FAQ",
        "support": "📞 Поддержка"
    }
    section_name = section_names.get(callback.data, "Раздел")

    text = (
        f"🚧 <b>{section_name}</b>\n\n"
        "Этот раздел находится в разработке и скоро будет доступен."
    )
    await callback.message.edit_text(text, parse_mode="HTML", reply_markup=get_back_keyboard("help"))
    await callback.answer()


# --- ОБРАБОТЧИКИ УПРАВЛЕНИЯ API КЛЮЧАМИ ---

@router.callback_query(F.data.in_({"add_api_keys", "update_api_keys"}))
async def callback_add_update_api_keys(callback: CallbackQuery, state: FSMContext):
    """Начало процесса добавления/обновления API ключей"""
    user_id = callback.from_user.id
    action = "обновить" if callback.data == "update_api_keys" else "добавить"

    await state.set_state(UserStates.AWAITING_API_KEY)
    await state.update_data(menu_message_id=callback.message.message_id)

    text = (
        f"🔑 <b>Добавление API ключей Bybit</b>\n\n"
        f"Шаг 1 из 2: Введите <b>API Key</b>\n\n"
        f"⚠️ <b>ВАЖНО:</b>\n"
        f"• Сообщение с ключом будет автоматически удалено после ввода\n"
        f"• Ключ будет зашифрован перед сохранением в базе данных\n"
        f"• API ключ должен иметь права на торговлю (Trade)\n\n"
        f"Введите ваш API Key:"
    )

    await callback.message.edit_text(
        text,
        parse_mode="HTML",
        reply_markup=get_back_keyboard("api_keys")
    )
    await callback.answer()
    log_info(user_id, f"Пользователь начал процесс {action} API ключей", module_name='callback')


@router.message(UserStates.AWAITING_API_KEY)
async def process_api_key_input(message: Message, state: FSMContext):
    """Обработка ввода API Key с немедленным удалением сообщения"""
    user_id = message.from_user.id

    try:
        api_key = message.text.strip()

        # Валидация API ключа (базовая проверка формата)
        if len(api_key) < 10:
            await message.answer("❌ API ключ слишком короткий. Попробуйте еще раз.")
            await message.delete()
            return

        # Немедленно удаляем сообщение пользователя
        await message.delete()

        # Сохраняем API ключ в состояние
        await state.update_data(api_key=api_key)
        await state.set_state(UserStates.AWAITING_API_SECRET)

        # Обновляем сообщение с инструкцией для ввода API Secret
        state_data = await state.get_data()
        menu_message_id = state_data.get("menu_message_id")

        text = (
            f"✅ <b>API Key принят и удален из чата</b>\n\n"
            f"🔑 <b>Добавление API ключей Bybit</b>\n\n"
            f"Шаг 2 из 2: Введите <b>API Secret</b>\n\n"
            f"⚠️ <b>ВАЖНО:</b>\n"
            f"• Сообщение с секретом будет автоматически удалено\n"
            f"• Секрет будет зашифрован перед сохранением\n\n"
            f"Введите ваш API Secret:"
        )

        await bot_manager.bot.edit_message_text(
            chat_id=user_id,
            message_id=menu_message_id,
            text=text,
            parse_mode="HTML",
            reply_markup=get_back_keyboard("api_keys")
        )

        log_info(user_id, "API Key получен и удален из чата", module_name='callback')

    except Exception as e:
        log_error(user_id, f"Ошибка обработки API Key: {e}", module_name='callback')
        await message.answer("❌ Произошла ошибка. Попробуйте еще раз.")
        await message.delete()


@router.message(UserStates.AWAITING_API_SECRET)
async def process_api_secret_input(message: Message, state: FSMContext):
    """
    Обработка ввода API Secret с немедленным удалением и сохранением в БД (Multi-Account Support)

    ПЕРЕДЕЛАНО для поддержки Multi-Account системы с account_priority
    """
    user_id = message.from_user.id

    try:
        api_secret = message.text.strip()

        # Валидация API секрета (базовая проверка формата)
        if len(api_secret) < 10:
            await message.answer("❌ API Secret слишком короткий. Попробуйте еще раз.")
            await message.delete()
            return

        # Немедленно удаляем сообщение пользователя
        await message.delete()

        # Получаем сохраненные данные из состояния
        state_data = await state.get_data()
        api_key = state_data.get("api_key")
        menu_message_id = state_data.get("menu_message_id")
        priority = state_data.get("api_key_priority", 1)  # По умолчанию PRIMARY
        action = state_data.get("api_key_action", "add")

        if not api_key:
            await message.answer("❌ Ошибка: API Key не найден. Начните процесс заново.")
            await state.clear()
            return

        # Сохраняем ключи в базу данных с указанным priority
        success = await db_manager.save_api_keys(
            user_id=user_id,
            exchange="bybit",
            api_key=api_key,
            secret_key=api_secret,
            account_priority=priority  # КЛЮЧЕВОЕ ИЗМЕНЕНИЕ - передаем priority
        )

        if success:
            # Показываем короткую версию ключа для подтверждения
            api_key_short = api_key[:4] + '...' + api_key[-4:]

            priority_names = {
                1: "PRIMARY (Bot 1)",
                2: "SECONDARY (Bot 2)",
                3: "TERTIARY (Bot 3)"
            }
            priority_name = priority_names[priority]

            # Получаем ОБНОВЛЕННОЕ количество ключей для правильного отображения клавиатуры
            all_keys = await db_manager.get_all_user_api_keys(user_id, "bybit")
            api_keys_count = len(all_keys)

            action_text = "обновлены" if action == "update" else "сохранены"

            text = (
                f"✅ <b>{priority_name} API ключи успешно {action_text}!</b>\n\n"
                f"🔑 <b>API Key:</b> <code>{api_key_short}</code>\n"
                f"🔢 <b>Приоритет:</b> {priority}\n\n"
                f"🔒 Ваши ключи зашифрованы и надежно хранятся в базе данных.\n"
                f"🗑️ Все сообщения с ключами были удалены из чата.\n\n"
            )

            # Добавляем информацию о Multi-Account режиме
            if api_keys_count == 1:
                text += (
                    f"💡 <b>Текущий режим:</b> Обычный (1 бот)\n"
                    f"   Добавьте еще 2 ключа для активации Multi-Account режима!"
                )
            elif api_keys_count == 2:
                text += (
                    f"💡 <b>Текущий режим:</b> Переходный (2 бота)\n"
                    f"   Добавьте еще 1 ключ для полноценного Multi-Account режима!"
                )
            elif api_keys_count >= 3:
                text += (
                    f"🎉 <b>Multi-Account режим АКТИВЕН!</b>\n"
                    f"   Система автоматически управляет 3 ботами с ротацией!"
                )

            log_info(user_id, f"API ключи успешно {action_text} для пользователя {user_id}, priority={priority}, всего ключей={api_keys_count}", module_name='callback')
        else:
            api_keys_count = 0  # При ошибке показываем меню для 0 ключей
            text = (
                f"❌ <b>Ошибка сохранения ключей</b>\n\n"
                f"Не удалось сохранить API ключи в базу данных. "
                f"Попробуйте еще раз или обратитесь в поддержку."
            )
            log_error(user_id, f"Ошибка сохранения API ключей с priority={priority} в БД", module_name='callback')

        from ..keyboards.inline import get_api_keys_keyboard
        await bot_manager.bot.edit_message_text(
            chat_id=user_id,
            message_id=menu_message_id,
            text=text,
            parse_mode="HTML",
            reply_markup=get_api_keys_keyboard(api_keys_count=api_keys_count)  # ИСПРАВЛЕНО - используем api_keys_count
        )

        # Очищаем состояние
        await state.clear()

    except Exception as e:
        log_error(user_id, f"Ошибка обработки API Secret: {e}", module_name='callback')
        await message.answer("❌ Произошла ошибка при сохранении ключей.")
        await message.delete()
        await state.clear()


# ============================================================================
# MULTI-ACCOUNT API KEYS HANDLERS (добавлено для поддержки 3 ключей)
# ============================================================================

@router.callback_query(F.data.startswith("add_api_key_priority_") | F.data.startswith("update_api_key_priority_"))
async def callback_add_update_api_key_with_priority(callback: CallbackQuery, state: FSMContext):
    """
    Начало процесса добавления/обновления API ключа с указанным priority (Multi-Account Support)

    Обрабатывает callback_data:
    - add_api_key_priority_1 / add_api_key_priority_2 / add_api_key_priority_3
    - update_api_key_priority_1 / update_api_key_priority_2 / update_api_key_priority_3
    """
    user_id = callback.from_user.id

    try:
        # Парсим callback_data для получения action и priority
        if callback.data.startswith("add_api_key_priority_"):
            action = "add"
            priority = int(callback.data.replace("add_api_key_priority_", ""))
        else:  # update_api_key_priority_
            action = "update"
            priority = int(callback.data.replace("update_api_key_priority_", ""))

        # Валидация priority
        if priority not in [1, 2, 3]:
            await callback.answer("❌ Неверный приоритет ключа", show_alert=True)
            return

        # Определяем имя для отображения
        priority_names = {
            1: "PRIMARY (Bot 1)",
            2: "SECONDARY (Bot 2)",
            3: "TERTIARY (Bot 3)"
        }
        priority_name = priority_names[priority]
        action_text = "Обновление" if action == "update" else "Добавление"

        # Сохраняем priority в состояние
        await state.set_state(UserStates.AWAITING_API_KEY)
        await state.update_data(
            menu_message_id=callback.message.message_id,
            api_key_priority=priority,
            api_key_action=action
        )

        text = (
            f"🔑 <b>{action_text} {priority_name} API ключа Bybit</b>\n\n"
            f"Шаг 1 из 2: Введите <b>API Key</b>\n\n"
            f"⚠️ <b>ВАЖНО:</b>\n"
            f"• Сообщение с ключом будет автоматически удалено после ввода\n"
            f"• Ключ будет зашифрован перед сохранением в базе данных\n"
            f"• API ключ должен иметь права на торговлю (Trade)\n\n"
            f"💡 <b>Для Multi-Account режима:</b>\n"
            f"   У каждого ключа должны быть права на отдельный суб-аккаунт Bybit\n\n"
            f"Введите ваш API Key для {priority_name}:"
        )

        await callback.message.edit_text(
            text,
            parse_mode="HTML",
            reply_markup=get_back_keyboard("api_keys")
        )
        await callback.answer()

        log_info(user_id, f"Пользователь начал процесс {action} API ключа с priority={priority}", module_name='callback')

    except Exception as e:
        log_error(user_id, f"Ошибка начала добавления/обновления API ключа: {e}", module_name='callback')
        await callback.answer("❌ Произошла ошибка", show_alert=True)


@router.callback_query(F.data.startswith("delete_api_key_priority_"))
async def callback_delete_api_key_with_priority(callback: CallbackQuery, state: FSMContext):
    """
    Подтверждение удаления конкретного API ключа по priority (Multi-Account Support)

    Обрабатывает callback_data:
    - delete_api_key_priority_1 (PRIMARY)
    - delete_api_key_priority_2 (SECONDARY)
    - delete_api_key_priority_3 (TERTIARY)
    """
    user_id = callback.from_user.id

    try:
        # Парсим priority из callback_data
        priority = int(callback.data.replace("delete_api_key_priority_", ""))

        # Валидация priority
        if priority not in [1, 2, 3]:
            await callback.answer("❌ Неверный приоритет ключа", show_alert=True)
            return

        # Определяем имя для отображения
        priority_names = {
            1: "PRIMARY (Bot 1)",
            2: "SECONDARY (Bot 2)",
            3: "TERTIARY (Bot 3)"
        }
        priority_name = priority_names[priority]

        # Проверяем, существует ли ключ с таким priority
        all_keys = await db_manager.get_all_user_api_keys(user_id, "bybit")
        key_exists = any(key['priority'] == priority for key in all_keys)

        if not key_exists:
            await callback.answer(f"❌ {priority_name} ключ не найден", show_alert=True)
            return

        # Получаем информацию о ключе для отображения
        target_key = next(key for key in all_keys if key['priority'] == priority)
        api_key_short = target_key['api_key'][:4] + '...' + target_key['api_key'][-4:]

        text = (
            f"⚠️ <b>Подтверждение удаления {priority_name} API ключа</b>\n\n"
            f"🔑 <b>API Key:</b> <code>{api_key_short}</code>\n"
            f"🔢 <b>Приоритет:</b> {priority}\n\n"
            f"Вы уверены, что хотите удалить этот ключ?\n\n"
        )

        # Добавляем предупреждение в зависимости от количества ключей
        if len(all_keys) == 3:
            text += (
                f"⚠️ <b>ВНИМАНИЕ:</b> После удаления этого ключа у вас останется 2 ключа.\n"
                f"   Multi-Account режим будет частично деактивирован."
            )
        elif len(all_keys) == 2:
            text += (
                f"⚠️ <b>ВНИМАНИЕ:</b> После удаления этого ключа у вас останется 1 ключ.\n"
                f"   Система перейдет в обычный режим (1 бот)."
            )
        elif len(all_keys) == 1:
            text += (
                f"🚨 <b>ВНИМАНИЕ:</b> Это ваш единственный ключ!\n"
                f"   После удаления вы не сможете использовать автоматическую торговлю."
            )

        await callback.message.edit_text(
            text,
            parse_mode="HTML",
            reply_markup=get_confirmation_keyboard(f"delete_api_key_priority_{priority}")
        )
        await callback.answer()

    except Exception as e:
        log_error(user_id, f"Ошибка отображения подтверждения удаления API ключа: {e}", module_name='callback')
        await callback.answer("❌ Произошла ошибка", show_alert=True)


@router.callback_query(F.data.startswith("confirm_delete_api_key_priority_"))
async def callback_confirm_delete_api_key_with_priority(callback: CallbackQuery, state: FSMContext):
    """
    Выполнение удаления конкретного API ключа по priority (Multi-Account Support)
    """
    user_id = callback.from_user.id

    try:
        # Парсим priority из callback_data
        priority = int(callback.data.replace("confirm_delete_api_key_priority_", ""))

        # Валидация priority
        if priority not in [1, 2, 3]:
            await callback.answer("❌ Неверный приоритет ключа", show_alert=True)
            return

        # Удаляем конкретный ключ через деактивацию записи в БД
        query = """
            UPDATE user_api_keys
            SET is_active = FALSE, updated_at = NOW()
            WHERE user_id = $1 AND exchange = $2 AND account_priority = $3
        """

        async with db_manager.get_connection() as conn:
            result = await conn.execute(query, user_id, "bybit", priority)

        # Проверяем, был ли удален ключ
        if result == "UPDATE 0":
            await callback.answer("❌ Ключ не найден", show_alert=True)
            return

        # Получаем ОБНОВЛЕННОЕ количество ключей
        all_keys = await db_manager.get_all_user_api_keys(user_id, "bybit")
        api_keys_count = len(all_keys)

        priority_names = {
            1: "PRIMARY (Bot 1)",
            2: "SECONDARY (Bot 2)",
            3: "TERTIARY (Bot 3)"
        }
        priority_name = priority_names[priority]

        text = (
            f"✅ <b>{priority_name} API ключ успешно удален</b>\n\n"
            f"🔢 Удален ключ с приоритетом: {priority}\n"
            f"📊 Осталось ключей: {api_keys_count}/3\n\n"
        )

        # Добавляем информацию о текущем режиме
        if api_keys_count == 0:
            text += (
                f"⚠️ У вас больше нет API ключей.\n"
                f"   Добавьте новые ключи для возобновления торговли."
            )
        elif api_keys_count == 1:
            text += (
                f"💡 Система переключена в обычный режим (1 бот).\n"
                f"   Добавьте 2 дополнительных ключа для Multi-Account режима."
            )
        elif api_keys_count == 2:
            text += (
                f"💡 Multi-Account режим частично активен (2 бота).\n"
                f"   Добавьте еще 1 ключ для полного Multi-Account режима."
            )

        log_info(user_id, f"API ключ с priority={priority} удален для пользователя {user_id}, осталось {api_keys_count} ключей", module_name='callback')

        from ..keyboards.inline import get_api_keys_keyboard
        await callback.message.edit_text(
            text,
            parse_mode="HTML",
            reply_markup=get_api_keys_keyboard(api_keys_count=api_keys_count)
        )
        await callback.answer("Ключ удален", show_alert=False)

    except Exception as e:
        log_error(user_id, f"Ошибка удаления API ключа с priority: {e}", module_name='callback')
        await callback.answer("❌ Ошибка при удалении ключа", show_alert=True)


@router.callback_query(F.data == "noop")
async def callback_noop(callback: CallbackQuery, state: FSMContext):
    """
    Заглушка для информационной кнопки "Multi-Account режим АКТИВЕН"

    Эта кнопка не выполняет никаких действий, просто показывает уведомление
    """
    await callback.answer(
        "🎉 Multi-Account режим активен! Система автоматически управляет 3 ботами.",
        show_alert=True
    )


# Обработчик неизвестных callback
@router.callback_query()
async def callback_unknown(callback: CallbackQuery):
    """Обработчик неизвестных callback запросов"""
    user_id = callback.from_user.id

    log_warning(user_id, f"Неизвестный callback: {callback.data}", module_name='callback')
    await callback.answer("❌ Неизвестная команда", show_alert=True)

