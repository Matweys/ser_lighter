"""
Профессиональная система обработки callback запросов для многопользовательского торгового бота
"""
from aiogram import Router, F
from aiogram.types import CallbackQuery, Message
from aiogram.fsm.context import FSMContext
from typing import Dict, Any, Optional
from decimal import Decimal
import json
from .basic import cmd_positions, cmd_orders, cmd_status
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
    get_risk_settings_keyboard,
    get_strategy_settings_keyboard,
    get_back_keyboard
)
from .states import UserStates, state_validator
from cache.redis_manager import redis_manager
from core.functions import format_currency, format_percentage, validate_symbol
from core.default_configs import DefaultConfigs
from core.logger import log_info, log_error, log_warning
from core.settings_config import DEFAULT_SYMBOLS, system_config
from api.bybit_api import BybitAPI
from aiogram.exceptions import TelegramBadRequest
from aiogram.utils.markdown import hbold
from core.functions import to_decimal
from datetime import datetime, timedelta, timezone



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
            StrategyType.IMPULSE_TRAILING.value: {
                "name": "🚀 Импульсный трейлинг",
                "description": (
                    "Стратегия следования за трендом с трейлинг-стопом.\n"
                    "Входит в позицию при сильных импульсах.\n"
                    "Максимизирует прибыль в трендовых движениях."
                ),
                "risk_level": "HIGH", 
                "min_balance": Decimal('150')
            }
        }

callback_handler = CallbackHandler(None)  # EventBus будет инициализирован позже


def convert_decimals_to_floats(data: Any) -> Any:
    """Рекурсивно конвертирует Decimal в float для JSON-сериализации."""
    if isinstance(data, Decimal):
        return float(data)
    if isinstance(data, dict):
        return {k: convert_decimals_to_floats(v) for k, v in data.items()}
    if isinstance(data, list):
        return [convert_decimals_to_floats(i) for i in data]
    return data


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
                                 end_date: Optional[datetime] = None) -> str:
    """Вспомогательная функция для генерации текста отчета по статистике."""
    # !!! ВАЖНО: Здесь должна быть ваша логика получения статистики из БД для заданного периода.
    # Сейчас она будет использовать общую статистику для примера.
    # В будущем нужно будет дописать функции в db_manager.py для фильтрации по датам.

    user_profile = await db_manager.get_user(user_id)
    strategy_stats = await db_manager.get_strategy_stats(user_id)  # Эта функция тоже должна будет принимать даты

    if not user_profile:
        return "❌ Профиль пользователя не найден."

    stats_text = (
        f"📊 {hbold('Общая статистика торговли')}\n\n"
        f"💰 {hbold('Общая прибыль:')} {format_currency(user_profile.total_profit)}\n"
        f"📈 {hbold('Всего сделок:')} {user_profile.total_trades}\n"
        f"🎯 {hbold('Общий Win Rate:')} {format_percentage(user_profile.win_rate)}\n"
    )

    if strategy_stats:
        stats_text += f"\n───────────────\n\n🏆 {hbold('Статистика по стратегиям:')}\n"
        for stat in strategy_stats:
            strategy_name = stat['strategy_type'].replace('_', ' ').title()
            pnl = to_decimal(stat['total_pnl'])
            trades = stat['total_trades']
            wins = stat['winning_trades']
            win_rate = (Decimal(wins) / Decimal(trades) * 100) if trades > 0 else 0
            pnl_emoji = "🟢" if pnl >= 0 else "🔴"
            stats_text += (
                f"\n🔹 {hbold(strategy_name)}\n"
                f"   {pnl_emoji} {hbold('PnL:')} {format_currency(pnl)}\n"
                f"   {hbold('Сделок:')} {trades} | {hbold('Win Rate:')} {format_percentage(win_rate)}"
            )
    return stats_text


@router.callback_query(F.data.startswith("stats_period_"))
async def callback_stats_period(callback: CallbackQuery, state: FSMContext):
    """Обрабатывает выбор периода и показывает статистику."""
    user_id = callback.from_user.id
    period = callback.data.replace("stats_period_", "")

    await callback.message.edit_text("⏳ <i>Генерирую отчет...</i>", parse_mode="HTML")

    if period == "day":
        # Логика для статистики за сутки (по МСК)
        now_utc = datetime.now(timezone.utc)
        start_of_day_msk = now_utc.replace(hour=0, minute=0, second=0, microsecond=0, tzinfo=None) - timedelta(hours=3)
        report_text = await _generate_stats_report(user_id, start_date=start_of_day_msk)

    elif period == "all":
        # Логика для статистики за все время
        report_text = await _generate_stats_report(user_id)

    elif period == "month_select":
        # TODO: Реализовать выбор месяца. Сейчас покажем за все время.
        await callback.message.edit_text(
            "🗓️ <i>Функция выбора месяца в разработке. Показываю статистику за всё время.</i>", parse_mode="HTML")
        report_text = await _generate_stats_report(user_id)

    else:  # Обработка выбора конкретного месяца
        # TODO: Реализовать логику для `stats_period_2025-08`
        report_text = await _generate_stats_report(user_id)

    await callback.message.edit_text(report_text, parse_mode="HTML", reply_markup=get_back_keyboard("main_menu"))
    await callback.answer()


# >>> КОНЕЦ НОВОГО БЛОКА <<<



# Настройки
@router.callback_query(F.data == "settings")
async def callback_settings(callback: CallbackQuery, state: FSMContext):
    """Главное меню настроек"""
    user_id = callback.from_user.id
    
    try:
        # Получаем текущие настройки
        user_config = await redis_manager.get_config(user_id, ConfigType.GLOBAL)

        if not user_config:
            log_info(user_id, "Конфигурация не найдена, создаю по умолчанию.", module_name='callback')
            default_config_data = DefaultConfigs.get_global_config()
            await redis_manager.save_config(user_id, ConfigType.GLOBAL, default_config_data)
            user_config = default_config_data

        risk_config = user_config.get('risk_management', {})
        
        text = (
            f"⚙️ <b>Настройки</b>\n\n"
            f"🎯 Риск на сделку: {format_percentage(risk_config.get('risk_per_trade', 2))}\n"
            f"📉 Макс. дневная просадка: {format_percentage(risk_config.get('max_daily_drawdown', 10))}\n"
            f"📊 Одновременных сделок: {risk_config.get('max_concurrent_trades', 3)}\n"
            f"💰 Мин. баланс: {format_currency(risk_config.get('min_balance', 100))}\n\n"
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

@router.callback_query(F.data.startswith("configure_strategy_"))
async def callback_configure_strategy(callback: CallbackQuery, state: FSMContext,
                                      strategy_type_override: Optional[str] = None):
    """Отображает меню настройки для конкретной стратегии."""
    user_id = callback.from_user.id
    # --- ИСПРАВЛЕНИЕ: Инициализируем переменную до блока try ---
    strategy_type = "unknown"
    # --- КОНЕЦ ИСПРАВЛЕНИЯ ---
    try:
        if strategy_type_override:
            strategy_type = strategy_type_override
        else:
            parts = callback.data.split('_')
            # Собираем название стратегии из всех частей после префикса
            strategy_type = "_".join(parts[2:])

        if strategy_type not in callback_handler.strategy_descriptions:
            log_error(user_id,
                      f"Попытка настроить несуществующую стратегию: '{strategy_type}' из callback: '{callback.data}'",
                      module_name='callback')
            await callback.answer("❌ Неизвестный тип стратегии.", show_alert=True)
            return

        # Загружаем актуальный конфиг стратегии
        config_enum = getattr(ConfigType, f"STRATEGY_{strategy_type.upper()}")
        config = await redis_manager.get_config(user_id, config_enum)
        if not config:
            config = DefaultConfigs.get_all_default_configs()["strategy_configs"][strategy_type]

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
        parts = callback.data.split("_")
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

        await callback_configure_strategy(callback, state, strategy_type_override=strategy_type)

    except Exception as e:
        log_error(user_id, f"Ошибка переключения стратегии {strategy_type}: {e}", module_name='callback')


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
        is_active_session = session_status.get('running', False) if session_status else False
        await callback.message.edit_text(
            text,
            reply_markup=get_main_menu_keyboard(),
            parse_mode="HTML"
        )
    except Exception as e:
        log_error(user_id, f"Ошибка получения статистики: {e}", module_name='callback')
        await callback.answer("❌ Ошибка загрузки статистики", show_alert=True)


@router.callback_query(F.data == "show_status")
async def callback_show_status(callback: CallbackQuery, state: FSMContext):
    """Обработчик кнопки 'Статус'"""
    await callback.answer()
    await cmd_status(callback.message, state)


@router.callback_query(F.data == "show_positions")
async def callback_show_positions(callback: CallbackQuery, state: FSMContext):
    """Обработчик кнопки 'Позиции', вызывает логику команды /positions"""
    try:
        await callback.answer(text="Запрашиваю открытые позиции...")
        await cmd_positions(callback.message, state)
    except Exception as e:
        log_error(callback.from_user.id, f"Ошибка при вызове /positions из callback: {e}", module_name='callback')
        await callback.answer("Не удалось загрузить позиции.", show_alert=True)


@router.callback_query(F.data == "show_orders")
async def callback_show_orders(callback: CallbackQuery, state: FSMContext):
    """Обработчик кнопки 'Ордера', вызывает логику команды /orders"""
    try:
        await callback.answer(text="Запрашиваю открытые ордера...")
        await cmd_orders(callback.message, state)
    except Exception as e:
        log_error(callback.from_user.id, f"Ошибка при вызове /orders из callback: {e}", module_name='callback')
        await callback.answer("Не удалось загрузить ордера.", show_alert=True)



@router.callback_query(F.data == "cancel")
async def callback_cancel(callback: CallbackQuery, state: FSMContext):
    """Отмена текущего действия"""
    user_id = callback.from_user.id
    
    try:
        await state.clear()
        await callback_main_menu(callback, state)
        log_info(user_id, "Пользователь отменил действие", module_name='callback')
        
    except Exception as e:
        og_error(user_id, f"Ошибка отмены: {e}", module_name='callback')
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


# --- Обработчики кнопок из главного меню ---

@router.callback_query(F.data == "show_balance")
async def callback_show_balance(callback: CallbackQuery, state: FSMContext):
    """Обработчик кнопки 'Баланс'"""
    user_id = callback.from_user.id
    await callback.answer("Запрашиваю баланс...")

    keys = await db_manager.get_api_keys(user_id, "bybit")
    if not keys:
        await callback.message.edit_text(
            "⚠️ <b>API ключи не настроены.</b>\nНе могу получить баланс. Перейдите в 'Настройки' -> 'API ключи' для их добавления.",
            parse_mode="HTML",
            reply_markup=get_back_keyboard("settings")
        )
        return

    try:
        exchange_config = system_config.get_exchange_config("bybit")
        use_demo = exchange_config.demo if exchange_config else False

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
    """Обработчик кнопки 'API ключи'"""
    user_id = callback.from_user.id
    await callback.answer()

    try:
        keys = await db_manager.get_api_keys(user_id, "bybit")

        if keys:
            # Показываем только часть ключа для безопасности
            api_key_short = keys[0][:4] + '...' + keys[0][-4:]
            text = (
                f"🔑 <b>Настроенные API ключи (Bybit)</b>\n\n"
                f"<b>API Key:</b> <code>{api_key_short}</code>\n\n"
                f"✅ Ключи настроены. Вы можете обновить их в любой момент."
            )
        else:
            text = (
                f"🔑 <b>Настройка API ключей</b>\n\n"
                f"🔴 Ключи не настроены.\n\n"
                f"Для работы бота необходимо добавить API ключи от вашего аккаунта на бирже Bybit."
            )

        # TODO: Добавить клавиатуру для управления ключами (добавить/удалить)
        await callback.message.edit_text(
            text,
            parse_mode="HTML",
            reply_markup=get_back_keyboard("settings")
        )
    except Exception as e:
        log_error(user_id, f"Ошибка отображения API ключей: {e}", module_name='callback')
        await callback.message.edit_text(
            "❌ Ошибка загрузки информации о ключах.",
            reply_markup=get_back_keyboard("settings")
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
    """Обработчик кнопки 'API ключи' в настройках"""
    user_id = callback.from_user.id
    await callback.answer()
    try:
        keys = await db_manager.get_api_keys(user_id, "bybit")
        if keys:
            api_key_short = keys[0][:4] + '...' + keys[0][-4:]
            text = (
                f"🔑 <b>Настроенные API ключи (Bybit)</b>\n\n"
                f"<b>API Key:</b> <code>{api_key_short}</code>\n\n"
                f"✅ Ключи настроены. Вы можете обновить их."
            )
        else:
            text = (
                f"🔑 <b>Настройка API ключей</b>\n\n"
                f"🔴 Ключи не настроены.\n\n"
                f"Для работы бота необходимо добавить API ключи от Bybit."
            )
        # TODO: Добавить клавиатуру для управления ключами (добавить/удалить)
        await callback.message.edit_text(
            text,
            parse_mode="HTML",
            reply_markup=get_back_keyboard("settings")
        )
    except Exception as e:
        log_error(user_id, f"Ошибка отображения API ключей: {e}", module_name='callback')
        await callback.message.edit_text("❌ Ошибка загрузки информации о ключах.", reply_markup=get_back_keyboard("settings"))


# --- 1. НОВАЯ ВСПОМОГАТЕЛЬНАЯ ФУНКЦИЯ ДЛЯ ОТРИСОВКИ МЕНЮ РИСКА ---
async def _show_risk_settings_menu(bot, chat_id: int, message_id: int, user_id: int):
    """Надежно отображает и обновляет меню настроек риска. (ИСПРАВЛЕННАЯ ВЕРСИЯ)"""
    # !!! ИСПРАВЛЕНИЕ: Инициализируем переменные до блока try
    text = "❌ Ошибка: не удалось сформировать меню настроек риска."
    reply_markup = get_back_keyboard("settings") # Клавиатура "Назад" по умолчанию

    try:
        default_config = DefaultConfigs.get_global_config()
        user_config = await redis_manager.get_config(user_id, ConfigType.GLOBAL) or {}
        final_config = default_config.copy()
        final_config.update(user_config)

        # Переопределяем переменные с корректными данными
        text = (
            f"🛡️ <b>Настройки риск-менеджмента</b>\n\n"
            f"Здесь устанавливаются глобальные правила безопасности для вашего аккаунта.\n\n"
            f"<b>Текущие параметры:</b>\n"
            f"∙ Макс. убыток в день: <b>{final_config.get('max_daily_loss_usdt')} USDT</b>"
        )
        reply_markup = get_risk_settings_keyboard()

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
            log_error(user_id, f"Ошибка Telegram API при обновлении меню риска: {e}", "callback")
            await bot.send_message(chat_id, text, reply_markup=reply_markup, parse_mode="HTML")
    except Exception as e:
        log_error(user_id, f"Критическая ошибка в _show_risk_settings_menu: {e}", "callback")


# --- 2. ОБРАБОТЧИК ВХОДА В МЕНЮ РИСКА ---
@router.callback_query(F.data == "risk_settings")
async def callback_risk_settings(callback: CallbackQuery, state: FSMContext):
    """Отображает меню настроек риска."""
    await state.set_state(UserStates.RISK_SETTINGS)
    await _show_risk_settings_menu(
        bot=callback.bot,
        chat_id=callback.message.chat.id,
        message_id=callback.message.message_id,
        user_id=callback.from_user.id
    )
    await callback.answer()


# --- 3. ОБРАБОТЧИКИ НАЖАТИЯ НА КНОПКИ ПАРАМЕТРОВ ---
@router.callback_query(F.data == "set_max_daily_loss_usdt")
async def callback_set_max_daily_loss(callback: CallbackQuery, state: FSMContext):
    """Запрашивает ввод нового значения для максимального суточного убытка."""
    await state.set_state(UserStates.SETTING_MAX_DAILY_LOSS_USDT)
    await state.update_data(menu_message_id=callback.message.message_id)
    await callback.message.edit_text(
        "✏️ Введите новую максимальную сумму суточного убытка в USDT (например, 15):",
        reply_markup=get_back_keyboard("risk_settings"),
        parse_mode="HTML"
    )
    await callback.answer()


# --- 4. ОБРАБОТЧИКИ ВВОДА ЗНАЧЕНИЙ ОТ ПОЛЬЗОВАТЕЛЯ ---
@router.message(UserStates.SETTING_MAX_DAILY_LOSS_USDT)
async def process_max_daily_loss_usdt(message: Message, state: FSMContext):
    """Обрабатывает и сохраняет новое значение макс. суточного убытка."""
    user_id = message.from_user.id
    try:
        value = float(message.text.strip().replace(',', '.'))
        if value <= 0:
            await message.answer("❌ Значение должно быть больше нуля.")
            return

        default_config = DefaultConfigs.get_global_config()
        user_config = await redis_manager.get_config(user_id, ConfigType.GLOBAL) or {}
        final_config = default_config.copy()
        final_config.update(user_config)
        final_config["max_daily_loss_usdt"] = round(value, 2)
        await redis_manager.save_config(user_id, ConfigType.GLOBAL, final_config)

        log_info(user_id, f"Обновлен параметр риска: max_daily_loss_usdt = {value}", "callback")

        state_data = await state.get_data()
        menu_message_id = state_data.get("menu_message_id")
        await message.delete()  # Удаляем сообщение пользователя с числом
        await state.clear()  # Сбрасываем состояние

        # Вызываем нашу новую функцию для корректного обновления меню
        await _show_risk_settings_menu(message.bot, message.chat.id, menu_message_id, user_id)

    except (ValueError, TypeError):
        await message.answer("❌ Некорректный формат. Введите число (например, `50.5`).")



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

        # Обновляем клавиатуру, чтобы показать изменение
        await send_or_edit_symbol_selection_menu(callback, state, is_edit=True)
        await callback.answer()  # Ответ, чтобы убрать "часики" с кнопки

    except Exception as e:
        log_error(user_id, f"Ошибка переключения символа {symbol_to_toggle}: {e}", "callback")
        await callback.answer("❌ Произошла ошибка.", show_alert=True)


@router.callback_query(F.data == "save_symbol_selection")
async def callback_save_symbol_selection(callback: CallbackQuery, state: FSMContext):
    """Сохраняет выбор и возвращает в меню настроек."""
    await callback.answer("✅ Список торговых пар сохранен!", show_alert=True)
    await callback_settings(callback, state)  # Возвращаемся в главное меню настроек


@router.callback_query(F.data.startswith("enable_strategy_") | F.data.startswith("disable_strategy_"))
async def callback_toggle_strategy(callback: CallbackQuery, state: FSMContext):
    """Включает или отключает конкретную стратегию."""
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

# Обработчик неизвестных callback
@router.callback_query()
async def callback_unknown(callback: CallbackQuery):
    """Обработчик неизвестных callback запросов"""
    user_id = callback.from_user.id
    
    log_warning(user_id, f"Неизвестный callback: {callback.data}", module_name='callback')
    await callback.answer("❌ Неизвестная команда", show_alert=True)

