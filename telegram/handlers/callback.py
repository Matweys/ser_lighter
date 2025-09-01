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
    get_strategy_selection_keyboard,
    get_strategy_config_keyboard,
    get_confirmation_keyboard,
    get_symbol_selection_keyboard,
    get_settings_keyboard,
    get_risk_settings_keyboard,
    get_strategy_settings_keyboard,
    get_back_keyboard,
    get_balance_keyboard,
    get_watchlist_keyboard
)
from .states import UserStates
from cache.redis_manager import redis_manager
from core.functions import format_currency, format_percentage, validate_symbol
from core.default_configs import DefaultConfigs
from core.logger import log_info, log_error, log_warning

router = Router()

class CallbackHandler:
    """Профессиональный обработчик callback запросов"""
    
    def __init__(self, event_bus: EventBus):
        self.event_bus = event_bus
        self.strategy_descriptions = {
            StrategyType.BIDIRECTIONAL_GRID.value: {
                "name": "🔄 Двунаправленная сетка",
                "description": (
                    "Стратегия для торговли в боковом рынке.\n"
                    "Размещает ордера на покупку и продажу вокруг текущей цены.\n"
                    "Подходит для флэтовых рынков с низкой волатильностью."
                ),
                "risk_level": "MEDIUM",
                "min_balance": Decimal('100')
            },
            StrategyType.GRID_SCALPING.value: {
                "name": "⚡ Сеточный скальпинг", 
                "description": (
                    "Быстрая скальпинговая стратегия с частыми сделками.\n"
                    "Использует узкие спреды для получения небольшой прибыли.\n"
                    "Требует высокой ликвидности и низких комиссий."
                ),
                "risk_level": "HIGH",
                "min_balance": Decimal('200')
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
        is_active = session_status.get('is_active', False) if session_status else False
        
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
            reply_markup=get_main_menu_keyboard(is_active),
            parse_mode="HTML"
        )
        
        log_info(user_id, "Пользователь вернулся в главное меню", module_name='callback')
        
    except Exception as e:
        log_error(user_id, f"Ошибка в главном меню: {e}", module_name='callback')
        await callback.answer("❌ Произошла ошибка", show_alert=True)

# Управление торговлей
@router.callback_query(F.data == "start_trading")
async def callback_start_trading(callback: CallbackQuery, state: FSMContext):
    """Запуск торговли"""
    user_id = callback.from_user.id
    
    try:
        # Проверяем доступ пользователя
        user_profile = await db_manager.get_user(user_id)
        if not user_profile or not user_profile.is_active:
            await callback.answer("🚫 У вас нет доступа к торговле", show_alert=True)
            return
        
        # Проверяем API ключи
        api_keys = await db_manager.get_api_keys(user_id, "bybit")
        if not api_keys:
            await callback.answer(
                "⚠️ Сначала настройте API ключи в разделе 'Настройки'",
                show_alert=True
            )
            return
        
        # Проверяем существующую сессию
        session_status = await redis_manager.get_user_session(user_id)
        if session_status and session_status.get('is_active'):
            await callback.answer("⚠️ Торговля уже запущена", show_alert=True)
            return
        
        # Публикуем событие запуска сессии
        if callback_handler.event_bus:
            await callback_handler.event_bus.publish(
                UserSessionStartRequestedEvent(user_id=user_id)
            )
        
        await callback.message.edit_text(
            "🚀 <b>Запуск торговли...</b>\n\n"
            "⏳ Инициализация торговой сессии...\n"
            "📊 Загрузка конфигураций...\n"
            "🔄 Подключение к рынку...",
            reply_markup=get_main_menu_keyboard(False),
            parse_mode="HTML"
        )
        
        log_info(user_id, "Запуск торговли", module_name='callback')
        
    except Exception as e:
        log_error(user_id, f"Ошибка запуска торговли: {e}", module_name='callback')
        await callback.answer("❌ Ошибка запуска торговли", show_alert=True)

@router.callback_query(F.data == "stop_trading")
async def callback_stop_trading(callback: CallbackQuery, state: FSMContext):
    """Остановка торговли"""
    user_id = callback.from_user.id
    
    try:
        # Проверяем существующую сессию
        session_status = await redis_manager.get_user_session(user_id)
        if not session_status or not session_status.get('is_active'):
            await callback.answer("⚠️ Торговля не запущена", show_alert=True)
            return
        
        # Публикуем событие остановки сессии
        if callback_handler.event_bus:
            await callback_handler.event_bus.publish(
                UserSessionStopRequestedEvent(user_id=user_id)
            )
        
        await callback.message.edit_text(
            "🛑 <b>Остановка торговли...</b>\n\n"
            "⏳ Закрытие активных позиций...\n"
            "📊 Сохранение статистики...\n"
            "🔄 Завершение сессии...",
            reply_markup=get_main_menu_keyboard(True),
            parse_mode="HTML"
        )
        
        log_info(user_id, "Остановка торговли", module_name='callback')
        
    except Exception as e:
        log_error(user_id, f"Ошибка остановки торговли: {e}", module_name='callback')
        await callback.answer("❌ Ошибка остановки торговли", show_alert=True)

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

@router.callback_query(F.data == "risk_settings")
async def callback_risk_settings(callback: CallbackQuery, state: FSMContext):
    """Настройки риск-менеджмента"""
    user_id = callback.from_user.id
    
    try:
        user_config = await redis_manager.get_config(user_id, ConfigType.GLOBAL)
        risk_config = user_config.get('risk_management', {})
        
        text = (
            f"🛡️ <b>Настройки риск-менеджмента</b>\n\n"
            f"🎯 <b>Риск на сделку:</b> {format_percentage(risk_config.get('risk_per_trade', 2))}\n"
            f"Процент от баланса, рискуемый в одной сделке\n\n"
            f"📉 <b>Макс. дневная просадка:</b> {format_percentage(risk_config.get('max_daily_drawdown', 10))}\n"
            f"Максимальная просадка за день\n\n"
            f"📊 <b>Одновременных сделок:</b> {risk_config.get('max_concurrent_trades', 3)}\n"
            f"Максимальное количество активных позиций\n\n"
            f"💰 <b>Минимальный баланс:</b> {format_currency(risk_config.get('min_balance', 100))}\n"
            f"Минимальный баланс для торговли\n\n"
            f"Выберите параметр для изменения:"
        )
        
        await callback.message.edit_text(
            text,
            reply_markup=get_risk_settings_keyboard(),
            parse_mode="HTML"
        )
        
    except Exception as e:
        log_error(user_id, f"Ошибка в настройках риска: {e}", module_name='callback')
        await callback.answer("❌ Ошибка загрузки настроек", show_alert=True)

@router.callback_query(F.data == "strategy_settings")
async def callback_strategy_settings(callback: CallbackQuery, state: FSMContext):
    """Настройки стратегий"""
    user_id = callback.from_user.id
    
    try:
        user_config = await redis_manager.get_config(user_id, ConfigType.GLOBAL)
        strategies_config = user_config.get('strategies', {})
        
        text = (
            f"📊 <b>Настройки стратегий</b>\n\n"
            f"Настройте параметры для каждого типа стратегии:\n\n"
        )
        
        # Добавляем информацию о каждой стратегии
        for strategy_type, info in callback_handler.strategy_descriptions.items():
            strategy_config = strategies_config.get(strategy_type, {})
            enabled = strategy_config.get('enabled', True)
            status = "✅" if enabled else "❌"
            
            text += f"{status} <b>{info['name']}</b>\n"
            text += f"   Риск: {info['risk_level']}\n"
            text += f"   Мин. баланс: {format_currency(info['min_balance'])}\n\n"
        
        text += "Выберите стратегию для настройки:"
        
        await callback.message.edit_text(
            text,
            reply_markup=get_strategy_settings_keyboard(),
            parse_mode="HTML"
        )
        
    except Exception as e:
        log_error(user_id, f"Ошибка в настройках стратегий: {e}", module_name='callback')
        await callback.answer("❌ Ошибка загрузки настроек", show_alert=True)

# Выбор стратегии для настройки
@router.callback_query(F.data.startswith("configure_strategy_"))
async def callback_configure_strategy(callback: CallbackQuery, state: FSMContext):
    """Настройка конкретной стратегии"""
    user_id = callback.from_user.id
    strategy_type = callback.data.replace("configure_strategy_", "")
    
    try:
        if strategy_type not in callback_handler.strategy_descriptions:
            await callback.answer("❌ Неизвестная стратегия", show_alert=True)
            return
        
        strategy_info = callback_handler.strategy_descriptions[strategy_type]
        user_config = await redis_manager.get_user_config(user_id)
        strategy_config = user_config.get('strategies', {}).get(strategy_type, {})
        
        # Сохраняем тип стратегии в состоянии
        await state.update_data(configuring_strategy=strategy_type)
        await state.set_state(UserStates.CONFIGURING_STRATEGY)
        
        text = (
            f"⚙️ <b>Настройка: {strategy_info['name']}</b>\n\n"
            f"📝 <b>Описание:</b>\n{strategy_info['description']}\n\n"
            f"🎯 <b>Уровень риска:</b> {strategy_info['risk_level']}\n"
            f"💰 <b>Мин. баланс:</b> {format_currency(strategy_info['min_balance'])}\n\n"
            f"<b>Текущие настройки:</b>\n"
        )
        
        # Добавляем текущие параметры стратегии
        if strategy_type == StrategyType.BIDIRECTIONAL_GRID.value:
            text += f"📏 Количество уровней: {strategy_config.get('grid_levels', 5)}\n"
            text += f"📊 Spacing (%): {strategy_config.get('spacing_percent', 0.5)}\n"
            text += f"💵 Размер ордера (USDT): {strategy_config.get('order_size_usdt', 10)}\n"
        elif strategy_type == StrategyType.GRID_SCALPING.value:
            text += f"⚡ Таймаут ордера (сек): {strategy_config.get('order_timeout', 30)}\n"
            text += f"📊 Мин. спред (%): {strategy_config.get('min_spread_percent', 0.1)}\n"
            text += f"💵 Размер ордера (USDT): {strategy_config.get('order_size_usdt', 20)}\n"
        elif strategy_type == StrategyType.IMPULSE_TRAILING.value:
            text += f"🎯 Мин. сила сигнала: {strategy_config.get('min_signal_strength', 70)}\n"
            text += f"📈 Трейлинг (%): {strategy_config.get('trailing_percent', 1.0)}\n"
            text += f"💵 Размер позиции (USDT): {strategy_config.get('position_size_usdt', 50)}\n"
        
        text += f"\nВыберите параметр для изменения:"
        
        await callback.message.edit_text(
            text,
            reply_markup=get_strategy_config_keyboard(strategy_type),
            parse_mode="HTML"
        )
        
    except Exception as e:
        log_error(user_id, f"Ошибка настройки стратегии {strategy_type}: {e}", module_name='callback')
        await callback.answer("❌ Ошибка настройки стратегии", show_alert=True)

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
        if session_status and session_status.get('status') == 'active':
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
        is_active_session = session_status.get('status') == 'active' if session_status else False
        await callback.message.edit_text(
            text,
            reply_markup=get_main_menu_keyboard(is_active_session),
            parse_mode="HTML"
        )
    except Exception as e:
        log_error(user_id, f"Ошибка получения статистики: {e}", module_name='callback')
        await callback.answer("❌ Ошибка загрузки статистики", show_alert=True)

# Подтверждение действий
@router.callback_query(F.data.startswith("confirm_"))
async def callback_confirm_action(callback: CallbackQuery, state: FSMContext):
    """Подтверждение различных действий"""
    user_id = callback.from_user.id
    action = callback.data.replace("confirm_", "")
    
    try:
        if action == "start_trading":
            # Подтверждение запуска торговли
            if callback_handler.event_bus:
                await callback_handler.event_bus.publish(
                    UserSessionStartRequestedEvent(user_id=user_id)
                )
            
            await callback.message.edit_text(
                "✅ <b>Торговля запущена!</b>\n\n"
                "🚀 Система начала мониторинг рынка\n"
                "📊 Стратегии активированы\n"
                "💼 Торговая сессия инициализирована",
                reply_markup=get_main_menu_keyboard(True),
                parse_mode="HTML"
            )
            
        elif action == "stop_trading":
            # Подтверждение остановки торговли
            if callback_handler.event_bus:
                await callback_handler.event_bus.publish(
                    UserSessionStopRequestedEvent(user_id=user_id)
                )
            
            await callback.message.edit_text(
                "🛑 <b>Торговля остановлена!</b>\n\n"
                "📊 Все стратегии деактивированы\n"
                "💼 Торговая сессия завершена\n"
                "📈 Статистика сохранена",
                reply_markup=get_main_menu_keyboard(False),
                parse_mode="HTML"
            )
        
        log_info(user_id, f"Подтверждено действие '{action}'", module_name='callback')
        
    except Exception as e:
        log_error(user_id, f"Ошибка подтверждения действия '{action}': {e}", module_name='callback')
        await callback.answer("❌ Ошибка выполнения действия", show_alert=True)

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


# --- Обработчики кнопок из главного меню ---

@router.callback_query(F.data == "show_balance")
async def callback_show_balance(callback: CallbackQuery, state: FSMContext):
    """Обработчик кнопки 'Баланс'"""
    user_id = callback.from_user.id
    await callback.answer("Запрашиваю баланс...")  # Быстрый ответ пользователю

    keys = await db_manager.get_api_keys(user_id, "bybit")
    if not keys:
        await callback.message.edit_text(
            "⚠️ <b>API ключи не настроены.</b>\nНе могу получить баланс. Перейдите в 'Настройки' -> 'API ключи' для их добавления.",
            parse_mode="HTML",
            reply_markup=get_back_keyboard("settings")
        )
        return

    try:
        # Создаем временный экземпляр API для запроса
        from api.bybit_api import BybitAPI
        api = BybitAPI(user_id=user_id, api_key=keys[0], api_secret=keys[1])
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
                reply_markup=get_balance_keyboard()
            )
        else:
            await callback.message.edit_text(
                "❌ Не удалось получить данные о балансе. Проверьте права ваших API ключей.",
                reply_markup=get_back_keyboard("main_menu")
            )
    except Exception as e:
        log_error(user_id, f"Ошибка получения баланса по кнопке: {e}", module_name='callback')
        await callback.message.edit_text(
            "❌ Произошла ошибка при запросе баланса.",
            reply_markup=get_back_keyboard("main_menu")
        )


@router.callback_query(F.data == "watchlist")
async def callback_watchlist(callback: CallbackQuery, state: FSMContext):
    """Обработчик кнопки 'Watchlist'"""
    user_id = callback.from_user.id
    await callback.answer("Загружаю список отслеживания...")

    try:
        from core.enums import ConfigType
        user_config = await redis_manager.get_config(user_id, ConfigType.GLOBAL)
        watchlist = user_config.get("watchlist_symbols", [])

        if not watchlist:
            text = "📋 <b>Список отслеживания пуст.</b>\n\nДобавьте торговые пары, за которыми бот будет следить и по которым будет открывать сделки."
        else:
            text = "📋 <b>Список отслеживаемых пар:</b>\n\n"
            # Преобразуем список в строку с нумерацией
            for i, symbol in enumerate(watchlist, 1):
                text += f"{i}. <code>{symbol}</code>\n"

        await callback.message.edit_text(
            text,
            parse_mode="HTML",
            reply_markup=get_watchlist_keyboard()
        )
    except Exception as e:
        log_error(user_id, f"Ошибка отображения watchlist: {e}", module_name='callback')
        await callback.message.edit_text(
            "❌ Ошибка загрузки списка отслеживания.",
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


@router.callback_query(F.data == "watchlist_settings")
async def callback_watchlist_settings(callback: CallbackQuery, state: FSMContext):
    """
    Обработчик кнопки 'Watchlist' в настройках.
    Отображает текущий список отслеживаемых пар и кнопки для управления им.
    """
    user_id = callback.from_user.id
    await callback.answer("Загружаю список отслеживания...")

    try:
        from core.enums import ConfigType
        user_config = await redis_manager.get_config(user_id, ConfigType.GLOBAL)

        # Если конфига нет, создаем его из шаблона по умолчанию
        if not user_config:
            user_config = DefaultConfigs.get_global_config()
            await redis_manager.save_config(user_id, ConfigType.GLOBAL, user_config)

        watchlist = user_config.get("watchlist_symbols", [])

        if not watchlist:
            text = "📋 <b>Список отслеживания пуст.</b>\n\nДобавьте торговые пары, за которыми бот будет следить и по которым будет открывать сделки."
        else:
            text = "📋 <b>Список отслеживаемых пар:</b>\n\n"
            # Преобразуем список в строку с нумерацией
            for i, symbol in enumerate(watchlist, 1):
                text += f"{i}. <code>{symbol}</code>\n"

        text += "\n\nВыберите действие:"

        await callback.message.edit_text(
            text,
            parse_mode="HTML",
            reply_markup=get_watchlist_keyboard()  # Используем специальную клавиатуру для watchlist
        )
    except Exception as e:
        log_error(user_id, f"Ошибка отображения watchlist: {e}", module_name='callback')
        await callback.message.edit_text(
            "❌ Ошибка загрузки списка отслеживания.",
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
    Подтверждение и выполнение сброса настроек.
    """
    user_id = callback.from_user.id
    await callback.answer("Сбрасываю настройки...", show_alert=True)

    try:
        # Получаем конфиг по умолчанию и сохраняем его для пользователя
        default_config = DefaultConfigs.get_global_config()
        from core.enums import ConfigType
        await redis_manager.save_config(user_id, ConfigType.GLOBAL, default_config)

        # Также сбрасываем конфиги для каждой стратегии
        default_strategies = DefaultConfigs.get_all_default_configs().get("strategy_configs", {})
        for strategy_type, strategy_config in default_strategies.items():
            await redis_manager.save_config(user_id, f"strategy:{strategy_type}", strategy_config)

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

@router.callback_query(F.data.startswith("manual_symbol_"))
async def callback_manual_select_symbol(callback: CallbackQuery, state: FSMContext):
    """Обрабатывает выбор символа для ручного запуска."""
    user_id = callback.from_user.id
    symbol = callback.data.split("_")[-1]

    await state.update_data(manual_symbol=symbol)

    await callback.message.edit_text(
        f"🛠️ <b>Ручной запуск стратегии</b>\n\n"
        f"<b>Символ:</b> <code>{symbol}</code>\n"
        f"<b>Шаг 2:</b> Теперь выберите стратегию, которую хотите запустить.",
        parse_mode="HTML",
        reply_markup=get_strategy_selection_keyboard() # Используем существующую клавиатуру
    )

@router.callback_query(F.data.startswith("strategy_"))
async def callback_manual_select_strategy(callback: CallbackQuery, state: FSMContext):
    """Обрабатывает выбор стратегии для ручного запуска."""
    user_id = callback.from_user.id
    strategy_type = callback.data.replace("strategy_", "")

    user_data = await state.get_data()
    symbol = user_data.get("manual_symbol")

    if not symbol:
        await callback.answer("❌ Ошибка: символ не выбран. Начните заново с /manual.", show_alert=True)
        await state.clear()
        return

    # Проверяем, запущена ли уже такая стратегия
    session_status = await redis_manager.get_user_session(user_id)
    if session_status:
        strategy_id = f"{strategy_type}_{symbol}"
        active_strategies = [s.get('id') for s in session_status.get('active_strategies', [])]
        if strategy_id in active_strategies:
            await callback.answer(f"⚠️ Стратегия {strategy_type} для {symbol} уже запущена.", show_alert=True)
            return

    # Запускаем событие, которое обработает UserSession
    from core.events import SignalEvent
    # Для ручного запуска создаем минимально необходимые данные
    mock_signal_data = {
        'symbol': symbol,
        'strategy_type': strategy_type,
        'signal_strength': 100, # Максимальная сила для ручного запуска
        'market_condition': 'MANUAL_TRIGGER',
        'direction': 'MANUAL'
    }

    if callback_handler.event_bus:
        await callback_handler.event_bus.publish(
            SignalEvent(
                user_id=user_id,
                symbol=symbol,
                strategy_type=strategy_type,
                signal_strength=100,
                analysis_data=mock_signal_data
            )
        )
        await callback.message.edit_text(
            f"✅ <b>Запрос на запуск отправлен!</b>\n\n"
            f"<b>Стратегия:</b> {strategy_type}\n"
            f"<b>Символ:</b> <code>{symbol}</code>\n\n"
            f"Используйте /status для отслеживания состояния.",
            parse_mode="HTML",
            reply_markup=get_back_keyboard("main_menu")
        )
        log_info(user_id, f"Ручной запуск стратегии {strategy_type} для {symbol}", module_name='callback')
    else:
        await callback.answer("❌ Системная ошибка: шина событий недоступна.", show_alert=True)
        log_error(user_id, "EventBus не доступен в callback_handler для ручного запуска", "callback")

    await state.clear()

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


@router.callback_query(F.data == "remove_from_watchlist")
async def callback_remove_from_watchlist(callback: CallbackQuery, state: FSMContext):
    """Начинает процесс удаления символа из watchlist."""
    user_id = callback.from_user.id
    await state.set_state(UserStates.ENTERING_SYMBOL)
    await state.update_data(action="remove")

    await callback.message.edit_text(
        "<b>Введите тикер торговой пары для удаления из списка.</b>\n\n"
        "Например: <code>BTCUSDT</code>",
        parse_mode="HTML",
        reply_markup=get_back_keyboard("watchlist_settings")
    )
    log_info(user_id, "Пользователь начал удаление символа из watchlist.", module_name='callback')


@router.callback_query(F.data == "show_watchlist")
async def callback_show_watchlist(callback: CallbackQuery, state: FSMContext):
    """Обработчик кнопки 'Показать список' в меню Watchlist"""
    user_id = callback.from_user.id
    await callback.answer()
    try:
        user_config = await redis_manager.get_config(user_id, ConfigType.GLOBAL)
        watchlist = user_config.get("watchlist_symbols", []) if user_config else []

        if not watchlist:
            text = "📋 <b>Список отслеживания пуст.</b>"
        else:
            text = "📋 <b>Список отслеживаемых пар:</b>\n\n"
            for i, symbol in enumerate(watchlist, 1):
                text += f"{i}. <code>{symbol}</code>\n"

        await callback.message.edit_text(
            text,
            parse_mode="HTML",
            reply_markup=get_watchlist_keyboard()
        )
    except Exception as e:
        log_error(user_id, f"Ошибка отображения watchlist: {e}", module_name='callback')
        await callback.message.edit_text("❌ Ошибка загрузки списка отслеживания.")


# --- ОБРАБОТЧИКИ НАСТРОЕК РИСКА ---

@router.callback_query(F.data.startswith("set_"))
async def callback_set_risk_parameter(callback: CallbackQuery, state: FSMContext):
    """Общий обработчик для кнопок установки параметров риска."""
    user_id = callback.from_user.id
    param_map = {
        "set_risk_per_trade": {"state": UserStates.SETTING_RISK_PER_TRADE,
                               "text": "риск на сделку в % (например, 1.5)"},
        "set_max_drawdown": {"state": UserStates.SETTING_MAX_DAILY_DRAWDOWN,
                             "text": "макс. дневную просадку в % (например, 5)"},
        "set_max_trades": {"state": UserStates.SETTING_MAX_CONCURRENT_TRADES,
                           "text": "макс. кол-во одновременных сделок (например, 3)"},
        "set_stop_loss": {"state": UserStates.SETTING_STOP_LOSS_PERCENT, "text": "стоп-лосс в % (например, 2)"},
        "set_take_profit": {"state": UserStates.SETTING_TAKE_PROFIT_PERCENT, "text": "тейк-профит в % (например, 4)"},
    }
    action = callback.data
    if action in param_map:
        info = param_map[action]
        await state.set_state(info["state"])
        await state.update_data(message_to_delete=callback.message.message_id)
        await callback.message.edit_text(
            f"Введите новое значение для параметра '<b>{info['text']}</b>':",
            parse_mode="HTML",
            reply_markup=get_back_keyboard("risk_settings")
        )
        log_info(user_id, f"Начал изменение параметра {action}", "callback")


async def process_risk_setting_input(message: Message, state: FSMContext, key: str, validator):
    """Общая функция для обработки и сохранения настроек риска."""
    user_id = message.from_user.id
    is_valid, value = validator(message.text)

    if not is_valid:
        await message.answer("❌ Некорректное значение. Попробуйте еще раз.")
        return

    try:
        current_config = await redis_manager.get_config(user_id, ConfigType.GLOBAL)
        if not current_config:
            current_config = DefaultConfigs.get_global_config()

        current_config[key] = value

        await redis_manager.save_config(user_id, ConfigType.GLOBAL, current_config)
        log_info(user_id, f"Обновлен параметр риска: {key} = {value}", "callback")

        state_data = await state.get_data()
        await bot_manager.bot.delete_message(user_id, state_data.get("message_to_delete"))
        await message.delete()
        await state.clear()

        # Обновляем и показываем меню настроек риска
        # Создаем mock CallbackQuery из Message для передачи в handler
        mock_callback = CallbackQuery(id="mock", from_user=message.from_user, chat_instance="", message=message)
        await callback_risk_settings(mock_callback, state)

    except Exception as e:
        log_error(user_id, f"Ошибка сохранения настройки {key}: {e}", "callback")
        await message.answer("❌ Произошла ошибка при сохранении настройки.")


@router.message(UserStates.SETTING_RISK_PER_TRADE)
async def process_risk_per_trade(message: Message, state: FSMContext):
    await process_risk_setting_input(message, state, "risk_per_trade_percent", state_validator.validate_risk_per_trade)


@router.message(UserStates.SETTING_MAX_DAILY_DRAWDOWN)
async def process_max_drawdown(message: Message, state: FSMContext):
    await process_risk_setting_input(message, state, "global_daily_drawdown_percent",
                                     state_validator.validate_max_daily_drawdown)


@router.message(UserStates.SETTING_MAX_CONCURRENT_TRADES)
async def process_max_trades(message: Message, state: FSMContext):
    await process_risk_setting_input(message, state, "max_simultaneous_trades",
                                     state_validator.validate_max_concurrent_trades)


@router.message(UserStates.SETTING_STOP_LOSS_PERCENT)
async def process_stop_loss(message: Message, state: FSMContext):
    await process_risk_setting_input(message, state, "stop_loss_percent",
                                     state_validator.validate_risk_per_trade)  # Используем тот же валидатор


@router.message(UserStates.SETTING_TAKE_PROFIT_PERCENT)
async def process_take_profit(message: Message, state: FSMContext):
    await process_risk_setting_input(message, state, "take_profit_percent",
                                     state_validator.validate_risk_per_trade)  # Используем тот же валидатор


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


# Обработчик неизвестных callback
@router.callback_query()
async def callback_unknown(callback: CallbackQuery):
    """Обработчик неизвестных callback запросов"""
    user_id = callback.from_user.id
    
    log_warning(user_id, f"Неизвестный callback: {callback.data}", module_name='callback')
    await callback.answer("❌ Неизвестная команда", show_alert=True)

