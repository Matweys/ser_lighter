"""
Пользовательская торговая сессия
Управляет всеми компонентами торговли для одного пользователя
Объединенная версия с полной функциональностью
"""
import asyncio
import json
from typing import Dict, Optional, Any, List, Set
from decimal import Decimal, getcontext
from datetime import datetime
from core.logger import log_info, log_error, log_warning, log_debug
from core.events import (
    BaseEvent,
    SignalEvent,
    OrderFilledEvent,
    PositionUpdateEvent,
    PriceUpdateEvent,
    RiskLimitExceededEvent,
    StrategyRestartRequestEvent,
    StrategyStartEvent,
    StrategyStopEvent,
    UserSettingsChangedEvent,
    EventBus
)
from cache.redis_manager import redis_manager, ConfigType
from core.enums import StrategyType, EventType
from analysis.meta_strategist import MetaStrategist
from analysis.market_analyzer import MarketAnalyzer
from analysis.risk_manager import RiskManager
from api.bybit_api import BybitAPI
from websocket.websocket_manager import GlobalWebSocketManager, DataFeedHandler
from database.db_trades import db_manager
from core.settings_config import system_config
from aiogram import Bot
# Импорт стратегий
from strategies.base_strategy import BaseStrategy
from strategies.factory import create_strategy

from telegram.bot import bot_manager



# Установка точности Decimal
getcontext().prec = 28


class UserSession:
    """
    Пользовательская торговая сессия

    Управляет всеми компонентами торговли для одного пользователя:
    - MetaStrategist (анализ и принятие решений)
    - RiskManager (управление рисками)
    - DataFeedHandler (получение данных)
    - Активные стратегии

    Принципы работы:
    - Полная изоляция между пользователями
    - Динамическая загрузка настроек из Redis
    - Событийно-ориентированная архитектура
    - Автоматическое управление жизненным циклом стратегий
    """

    def __init__(self, user_id: int, event_bus: EventBus, global_ws_manager: GlobalWebSocketManager, bot: Bot):
        self.user_id = user_id
        self.event_bus = event_bus
        self.global_ws_manager = global_ws_manager
        self.bot = bot
        self.running = False

        # API клиент сессии
        self.api: Optional[BybitAPI] = None
        # Основные компоненты
        self.meta_strategist: Optional[MetaStrategist] = None
        self.risk_manager: Optional[RiskManager] = None
        self.data_feed_handler: Optional[DataFeedHandler] = None

        # Управление стратегиями
        self.active_strategies: Dict[str, BaseStrategy] = {}
        self.strategy_tasks: Dict[str, asyncio.Task] = {}

        # Система управления многосимвольными стратегиями
        self.MAX_STRATEGY_SLOTS = 3  # Максимум символов для одной стратегии
        self.strategy_queues: Dict[str, List[str]] = {}  # strategy_type -> [symbols] в очереди ожидания

        # Статистика сессии
        self.session_stats = {
            "start_time": datetime.now(),
            "strategies_launched": 0,
            "strategies_stopped": 0,
            "total_signals": 0,
            "risk_violations": 0,
            "successful_trades": 0,
            "failed_trades": 0,
            "total_pnl": Decimal("0")
        }

        # Блокировка для thread-safety
        self.lock = asyncio.Lock()

        # Задачи компонентов
        self._component_tasks: List[asyncio.Task] = []

    async def start(self) -> bool:
        """
        Запуск пользовательской сессии

        Returns:
            bool: True если сессия запущена успешно
        """
        if self.running:
            return True

        log_info(self.user_id, "Запуск пользовательской сессии...", module_name=__name__)

        try:
            # Загрузка конфигураций пользователя
            global_config = await redis_manager.get_config(self.user_id, ConfigType.GLOBAL)
            if not global_config:
                log_error(self.user_id, "Конфигурация пользователя не найдена", module_name=__name__)
                return False

            # Инициализация компонентов
            await self._initialize_components()

            await self._cleanup_stale_locks()

            # Подписка на события
            await self._subscribe_to_events()

            # Запуск компонентов
            await self._start_components()

            self.running = True

            # Автоматический запуск постоянных стратегий (например, Grid Scalping)
            await self._launch_persistent_strategies()

            # Сохранение состояния сессии в Redis
            await self._save_session_state()

            log_info(self.user_id, "Пользовательская сессия запущена", module_name=__name__)
            # 2. Отправляем уведомление об успешном запуске
            if bot_manager and bot_manager.bot:
                await bot_manager.bot.send_message(self.user_id,"✅ <b>Торговля успешно запущена!</b>",parse_mode="HTML")
            return True
        except Exception as e:
            log_error(self.user_id, f"Ошибка запуска сессии: {e}", module_name=__name__)
            # 3. Отправляем уведомление об ошибке
            if bot_manager and bot_manager.bot:
                await bot_manager.bot.send_message(
                    self.user_id,
                    f"❌ <b>Не удалось запустить торговлю.</b>\nПричина: <code>{e}</code>\nСмотрите логи для деталей.",
                    parse_mode="HTML"
                )
            await self.stop("Startup error")
            return False

    async def _cleanup_stale_locks(self):
        """
        Проверяет и удаляет "залипшие" блокировки, сверяясь с реальными
        позициями на бирже. Добавлено детальное логирование для отладки.
        """
        try:
            lock_key = f"user:{self.user_id}:impulse_trailing_lock"
            log_debug(self.user_id, f"Проверка на 'залипший' лок: ключ '{lock_key}'", "UserSession")

            lock_data_raw = await redis_manager.get_cached_data(lock_key)
            if not lock_data_raw:
                log_debug(self.user_id, "Блокировка не найдена. Очистка не требуется.", "UserSession")
                return

            log_info(self.user_id, f"Обнаружены данные блокировки: {lock_data_raw}", "UserSession")

            try:
                lock_data = json.loads(lock_data_raw) if isinstance(lock_data_raw, str) else lock_data_raw
            except (json.JSONDecodeError, TypeError):
                log_warning(self.user_id, f"Поврежденные JSON-данные в ключе блокировки. Удаляю ключ.", "UserSession")
                await redis_manager.delete_cached_data(lock_key)
                return

            symbol = lock_data.get("symbol")
            order_id = lock_data.get("order_id", "N/A")
            if not symbol:
                log_warning(self.user_id, "В ключе блокировки отсутствует символ. Удаляю ключ.", "UserSession")
                await redis_manager.delete_cached_data(lock_key)
                return

            log_info(self.user_id, f"Проверяю реальную позицию на бирже для символа {symbol} (ордер {order_id})...", "UserSession")
            positions_on_exchange = await self.api.get_positions(symbol=symbol)

            if not positions_on_exchange:
                log_warning(self.user_id, f"Позиция по {symbol} на бирже НЕ найдена. Блокировка считается 'залипшей'. Безопасно удаляю.", "UserSession")
                await redis_manager.delete_cached_data(lock_key)
            else:
                log_info(self.user_id, f"Позиция по {symbol} на бирже НАЙДЕНА. Блокировка подтверждена. Очистка не требуется.", "UserSession")

        except Exception as e:
            log_error(self.user_id, f"Критическая ошибка при очистке 'залипших' блокировок: {e}", "UserSession")


    async def stop(self, reason: str = "Manual stop"):
        """
        Остановка пользовательской сессии

        Args:
            reason: Причина остановки
        """
        if not self.running:
            return

        log_info(self.user_id, f"Остановка пользовательской сессии: {reason}", module_name=__name__)

        try:
            async with self.lock:
                self.running = False

                # Остановка всех стратегий
                await self._stop_all_strategies("Session stopping")

                # Остановка компонентов
                await self._stop_components()

                # Отписка от событий
                await self._unsubscribe_from_events()

                # Сохранение финальной статистики
                await self._save_final_stats()

                # Удаление состояния сессии из Redis
                await redis_manager.delete_user_session(self.user_id)

            log_info(self.user_id, "Пользовательская сессия остановлена", module_name=__name__)

        except Exception as e:
            log_error(self.user_id, f"Ошибка остановки сессии: {e}", module_name=__name__)

    async def _launch_persistent_strategies(self):
        """
        Запускает стратегии, которые должны работать постоянно
        для символов из watchlist пользователя.
        """
        log_info(self.user_id, "Проверка и запуск постоянных стратегий...", module_name=__name__)
        try:
            # 1. Загружаем конфиг для signal_scalper, чтобы проверить, включена ли стратегия
            grid_config_enum = ConfigType.STRATEGY_SIGNAL_SCALPER
            grid_config = await redis_manager.get_config(self.user_id, grid_config_enum)

            if not grid_config or not grid_config.get("is_enabled", False):
                log_info(self.user_id, "Стратегия Signal Scalper отключена в настройках. Пропускаем запуск.",
                         module_name=__name__)
                return

            # 2. Загружаем глобальный конфиг, чтобы получить watchlist
            global_config = await redis_manager.get_config(self.user_id, ConfigType.GLOBAL)
            if not global_config or not global_config.get("watchlist_symbols"):
                log_info(self.user_id, "Watchlist пуст. Стратегии Signal Scalper не запущены.", module_name=__name__)
                return

            watchlist = global_config.get("watchlist_symbols", [])
            log_info(self.user_id, f"Запуск Signal Scalper для символов из watchlist: {watchlist}",
                     module_name=__name__)

            # 3. Запускаем по одной стратегии для каждого символа
            for symbol in watchlist:
                await self.start_strategy(
                    strategy_type=StrategyType.SIGNAL_SCALPER.value,
                    symbol=symbol,
                    analysis_data={'trigger': 'persistent_start'}
                )

        except Exception as e:
            log_error(self.user_id, f"Ошибка при запуске постоянных стратегий: {e}", module_name=__name__)

    async def get_status(self) -> Dict[str, Any]:
        """
        Получение статуса сессии

        Returns:
            Dict: Статус сессии
        """
        try:
            # Статус стратегий
            strategies_status = {}
            for strategy_id, strategy in self.active_strategies.items():
                strategies_status[strategy_id] = await strategy.get_status()

            # Статус компонентов
            components_status = {
                "meta_strategist": self.meta_strategist.running if self.meta_strategist else False,
                "risk_manager": self.risk_manager.running if self.risk_manager else False,
                "data_feed_handler": self.data_feed_handler.running if self.data_feed_handler else False
            }

            return {
                "user_id": self.user_id,
                "running": self.running,
                "session_stats": {
                    "start_time": self.session_stats["start_time"].isoformat(),
                    "runtime": str(datetime.now() - self.session_stats["start_time"]),
                    "strategies_launched": self.session_stats["strategies_launched"],
                    "strategies_stopped": self.session_stats["strategies_stopped"],
                    "total_signals": self.session_stats["total_signals"],
                    "risk_violations": self.session_stats["risk_violations"],
                    "successful_trades": self.session_stats["successful_trades"],
                    "failed_trades": self.session_stats["failed_trades"],
                    "total_pnl": str(self.session_stats["total_pnl"])
                },
                "components_status": components_status,
                "active_strategies": strategies_status
            }

        except Exception as e:
            log_error(self.user_id, f"Ошибка получения статуса сессии: {e}", module_name=__name__)
            return {"user_id": self.user_id, "running": self.running, "error": str(e)}

    async def start_strategy(self, strategy_type: str, symbol: str, analysis_data: Optional[Dict] = None) -> bool:
        """
        Запускает стратегию, предварительно получая для нее самые свежие аналитические данные.
        """
        try:
            # --- ФИНАЛЬНАЯ ЛОГИКА: УМНАЯ ОБРАБОТКА ДАННЫХ ---
            # Инициализируем данные по умолчанию. Если пришли данные (для grid/restart), используем их.
            signal_data_for_strategy = analysis_data or {}

            # Для Impulse Trailing мы ВСЕГДА игнорируем пришедшие данные и запрашиваем свежие.
            if strategy_type == "impulse_trailing":
                lock_key = f"user:{self.user_id}:impulse_trailing_lock"
                if await redis_manager.get_cached_data(lock_key):
                    log_warning(self.user_id,
                                f"Запуск impulse_trailing для {symbol} отклонен: другая импульсная сделка уже активна.",
                                module_name=__name__)
                    return False

                analyzer = MarketAnalyzer(self.user_id, self.api)
                impulse_config = await redis_manager.get_config(self.user_id, ConfigType.STRATEGY_IMPULSE_TRAILING)
                timeframe = impulse_config.get("analysis_timeframe", "5m")
                fresh_analysis = await analyzer.get_market_analysis(symbol, timeframe)

                if not fresh_analysis:
                    log_warning(self.user_id, f"Повторный анализ для {symbol} не дал результата. Сигнал пропущен.",
                                module_name=__name__)
                    return False

                # Перезаписываем данные на самые свежие
                signal_data_for_strategy = fresh_analysis.to_dict()
            # --- КОНЕЦ ФИНАЛЬНОЙ ЛОГИКИ ---

            strategy_id = f"{strategy_type}_{symbol}"

            if strategy_id in self.active_strategies:
                log_warning(self.user_id, f"Стратегия {strategy_id} уже запущена", module_name=__name__)
                return True

            # СИСТЕМА УПРАВЛЕНИЯ СЛОТАМИ: Проверяем возможность запуска
            # ВАЖНО: impulse_trailing использует собственную Redis-блокировку, слоты не проверяем
            if strategy_type != "impulse_trailing":
                slot_check_result = await self._check_strategy_slots(strategy_type, symbol)

                if slot_check_result == "start_immediately":
                    log_info(self.user_id, f"✅ Есть свободный слот для {strategy_type}_{symbol}", module_name=__name__)
                elif slot_check_result == "replaced_inactive":
                    log_info(self.user_id, f"🔄 Заменена неактивная стратегия для запуска {strategy_type}_{symbol}", module_name=__name__)
                elif slot_check_result == "queued":
                    log_info(self.user_id, f"⏳ Стратегия {strategy_type}_{symbol} добавлена в очередь ожидания", module_name=__name__)
                    return True  # Стратегия добавлена в очередь, но не запущена
                elif slot_check_result == "blocked":
                    log_warning(self.user_id, f"🚫 Запуск {strategy_type}_{symbol} заблокирован: все слоты заняты активными позициями", module_name=__name__)
                    return False

            if not await self.risk_manager.can_open_new_trade(symbol):
                log_warning(self.user_id, f"Открытие новой сделки для {symbol} отклонено риск-менеджером.",
                            module_name=__name__)
                return False

            if not self.api:
                log_error(self.user_id,
                          "Критическая ошибка: попытка создать стратегию без инициализированного API клиента.",
                          module_name=__name__)
                return False

            strategy = create_strategy(
                strategy_type=strategy_type,
                bot=self.bot,
                user_id=self.user_id,
                symbol=symbol,
                signal_data=signal_data_for_strategy,  # Передаем подготовленные данные
                api=self.api,
                event_bus=self.event_bus,
                config=None
            )

            if not strategy:
                log_error(self.user_id, f"Не удалось создать стратегию типа: {strategy_type}", module_name=__name__)
                return False

            # Запуск стратегии
            if await strategy.start():
                # ВАЖНО: Отправляем уведомление СРАЗУ после start(), когда strategy_id уже создан
                # но ДО добавления в active_strategies (чтобы пользователь получил уведомление раньше)
                await self._send_strategy_start_notification(strategy)

                self.active_strategies[strategy_id] = strategy

                # Обновление статистики
                self.session_stats["strategies_launched"] += 1

                # Публикация события
                event = StrategyStartEvent(
                    user_id=self.user_id,
                    strategy_type=strategy_type,
                    symbol=symbol,
                    strategy_id=strategy.strategy_id  # <-- Добавлен обязательный параметр
                )
                await self.event_bus.publish(event)

                log_info(self.user_id, f"Стратегия {strategy_id} запущена", module_name=__name__)
                return True
            else:
                log_error(self.user_id, f"Не удалось запустить стратегию {strategy_id}", module_name=__name__)
                return False

        except Exception as e:
            log_error(self.user_id, f"Ошибка запуска стратегии {strategy_type}: {e}", module_name=__name__)
            return False

    async def stop_strategy(self, strategy_id: str, reason: str = "Manual stop") -> bool:
        """
        Остановка стратегии

        Args:
            strategy_id: ID стратегии
            reason: Причина остановки

        Returns:
            bool: True если стратегия остановлена успешно
        """
        try:
            if strategy_id not in self.active_strategies:
                log_warning(self.user_id, f"Стратегия {strategy_id} не найдена", module_name=__name__)
                return True

            strategy = self.active_strategies[strategy_id]

            # Остановка стратегии
            await strategy.stop(reason)

            # Остановка задачи
            if strategy_id in self.strategy_tasks:
                task = self.strategy_tasks[strategy_id]
                if not task.done():
                    task.cancel()
                    try:
                        await task
                    except asyncio.CancelledError:
                        pass
                del self.strategy_tasks[strategy_id]

            # Удаление из активных стратегий
            del self.active_strategies[strategy_id]

            # Обновление статистики
            self.session_stats["strategies_stopped"] += 1

            # Публикация события
            event = StrategyStopEvent(
                user_id=self.user_id,
                strategy_id=strategy_id,
                reason=reason,
                symbol=strategy.symbol,
                strategy_type=strategy.strategy_type.value
            )
            await self.event_bus.publish(event)
            log_info(self.user_id, f"Стратегия {strategy_id} остановлена: {reason}", module_name=__name__)
            return True
        except Exception as e:
            log_error(self.user_id, f"Ошибка остановки стратегии {strategy_id}: {e}", module_name=__name__)
            return False

    async def _initialize_components(self):

        """Инициализация компонентов сессии"""
        try:
            # Получаем ключи из БД и создаем API клиент
            keys = await db_manager.get_api_keys(self.user_id, "bybit")
            if not keys or not keys[0] or not keys[1]:
                raise ValueError(f"API ключи для пользователя {self.user_id} не найдены или неполные в БД.")

            api_key, secret_key, _ = keys

            exchange_config = system_config.get_exchange_config("bybit")
            use_demo = exchange_config.demo if exchange_config else False

            self.api = BybitAPI(
                user_id=self.user_id,
                api_key=api_key,
                api_secret=secret_key,
                demo=use_demo,
                event_bus=self.event_bus
            )

            # Инициализация компонентов
            self.risk_manager = RiskManager(self.user_id, self.api, self.event_bus)
            self.data_feed_handler = DataFeedHandler(self.user_id, self.event_bus, self.global_ws_manager)

            # Создаем независимый анализатор
            market_analyzer = MarketAnalyzer(user_id=self.user_id, bybit_api=self.api)

            # Передаем анализатор и шину событий в MetaStrategist как зависимости
            self.meta_strategist = MetaStrategist(
                user_id=self.user_id,
                analyzer=market_analyzer,
                event_bus=self.event_bus
            )

            log_info(self.user_id, "Компоненты сессии инициализированы", module_name=__name__)

        except Exception as e:
            log_error(self.user_id, f"Ошибка инициализации компонентов: {e}", module_name=__name__)
            raise


    async def _start_components(self):
        """Запуск компонентов сессии"""
        try:
            # Запуск RiskManager
            await self.risk_manager.start()

            # Запуск DataFeedHandler
            await self.data_feed_handler.start()

            # Запуск MetaStrategist
            await self.meta_strategist.start()

            log_info(self.user_id, "Компоненты сессии запущены", module_name=__name__)

        except Exception as e:
            log_error(self.user_id, f"Ошибка запуска компонентов: {e}", module_name=__name__)
            raise

    async def _stop_components(self):
        """Остановка компонентов сессии"""
        try:
            # Остановка задач компонентов
            for task in self._component_tasks:
                if not task.done():
                    task.cancel()
                    try:
                        await task
                    except asyncio.CancelledError:
                        pass

            self._component_tasks.clear()

            # Остановка компонентов
            if self.meta_strategist:
                await self.meta_strategist.stop()

            if self.risk_manager:
                await self.risk_manager.stop()

            if self.data_feed_handler:
                await self.data_feed_handler.stop()

            # Закрытие API соединения
            if self.api:
                await self.api.close()
                log_info(self.user_id, "API соединение закрыто", module_name=__name__)

            log_info(self.user_id, "Компоненты сессии остановлены", module_name=__name__)

        except Exception as e:
            log_error(self.user_id, f"Ошибка остановки компонентов: {e}", module_name=__name__)

    async def _subscribe_to_events(self):
        """Подписка на события. Явно указываем все события, которые обрабатывает сессия."""
        try:
            # Этот список - самодокументируемый контракт. Сразу видно, что умеет делать сессия.
            events_for_session = [
                EventType.SIGNAL,
                EventType.RISK_LIMIT_EXCEEDED,
                EventType.USER_SETTINGS_CHANGED,
                EventType.STRATEGY_RESTART_REQUESTED,
                EventType.ORDER_FILLED,
                EventType.PRICE_UPDATE,
                EventType.POSITION_UPDATE,
                EventType.ORDER_UPDATE,
            ]
            for event_type in events_for_session:
                await self.event_bus.subscribe(event_type, self._user_event_handler, user_id=self.user_id)

            log_info(self.user_id, "Успешная подписка на пользовательские события.", module_name=__name__)
        except Exception as e:
            log_error(self.user_id, f"Ошибка подписки на события: {e}", module_name=__name__)

    async def _unsubscribe_from_events(self):
        """Отписка от событий"""
        try:
            # Новый метод отписки удаляет все подписки для данного обработчика
            await self.event_bus.unsubscribe(self._user_event_handler)
        except Exception as e:
            log_error(self.user_id, f"Ошибка отписки от событий: {e}", module_name=__name__)

    async def _save_session_state(self):
        """Сохранение состояния сессии в Redis"""
        try:
            session_state = {
                "user_id": self.user_id,
                "running": self.running,
                "start_time": self.session_stats["start_time"].isoformat(),
                "active_strategies": list(self.active_strategies.keys())
            }

            # Используем специальный метод для создания/обновления сессии
            await redis_manager.create_user_session(self.user_id, session_state)

        except Exception as e:
            log_error(self.user_id, f"Ошибка сохранения состояния сессии: {e}", module_name=__name__)

    async def _save_final_stats(self):
        """Сохранение финальной статистики"""
        try:
            final_stats = {
                **self.session_stats,
                "end_time": datetime.now().isoformat(),
                "total_runtime": str(datetime.now() - self.session_stats["start_time"])
            }

            # Кэшируем статистику на 7 дней
            await redis_manager.cache_data(f"user:{self.user_id}:session_stats", final_stats, ttl=86400 * 7)

        except Exception as e:
            log_error(self.user_id, f"Ошибка сохранения финальной статистики: {e}", module_name=__name__)

    # Обработчики событий
    async def _user_event_handler(self, event: BaseEvent):
        """
        Единый обработчик, который распределяет события по нужным компонентам и активным стратегиям.
        """
        if not self.running:
            return

        try:
            # 1. Маршрутизация событий для самой сессии
            if isinstance(event, SignalEvent):
                await self._handle_signal_event(event)
                return  # Сигнал обрабатывается только сессией
            elif isinstance(event, RiskLimitExceededEvent):
                await self._handle_risk_event(event)
                return  # Событие риска обрабатывается только сессией
            elif isinstance(event, UserSettingsChangedEvent):
                await self._handle_settings_changed(event)
                # Не выходим, чтобы событие дошло и до стратегий
            elif isinstance(event, StrategyRestartRequestEvent):
                await self._handle_strategy_restart_request(event)
                return

            # 2. Маршрутизация событий для активных стратегий
            if hasattr(event, 'symbol'):
                symbol = event.symbol
                # Ищем стратегию, которая работает с этим символом
                for strategy in self.active_strategies.values():
                    if strategy.symbol == symbol:
                        # Просто передаем событие в публичный обработчик стратегии
                        await strategy.handle_event(event)

            # 3. Маршрутизация для компонентов сессии (если потребуется в будущем)
            # Например, _handle_order_event для общей статистики
            if isinstance(event, OrderFilledEvent):
                await self._handle_order_event(event)

        except Exception as e:
            log_error(self.user_id,
                      f"Ошибка в главном обработчике событий для события типа {type(event).__name__}: {e}",
                      module_name=__name__)

    async def _handle_order_event(self, event: OrderFilledEvent):
        """Обработчик событий исполненных ордеров для глобальной статистики сессии"""
        # Эта логика должна быть в стратегии, но для общей статистики сессии можно оставить здесь
        pnl = event.fee  # Пример, реальный PnL рассчитывается при закрытии позиции
        if pnl > 0:
            self.session_stats["successful_trades"] += 1
        else:
            self.session_stats["failed_trades"] += 1
        self.session_stats["total_pnl"] += pnl
        pass

    async def _handle_risk_event(self, event: RiskLimitExceededEvent):
        """Обработчик событий риска"""
        self.session_stats["risk_violations"] += 1
        log_error(self.user_id, f"Превышен лимит риска: {event.limit_type}", module_name=__name__)
        if event.action_required == "stop_trading":
            await self.stop(f"Risk limit exceeded: {event.limit_type}")

    async def _handle_settings_changed(self, event: UserSettingsChangedEvent):
        """Обработчик изменения настроек пользователя"""
        if event.user_id != self.user_id:
            return

        log_info(self.user_id, "Получено событие изменения настроек. Перезагрузка конфигураций...",
                 module_name=__name__)
        try:
            # Получаем старый watchlist для сравнения
            old_watchlist = set()
            if self.meta_strategist and self.meta_strategist.user_config:
                old_watchlist = set(self.meta_strategist.user_config.get("watchlist_symbols", []))

            # Передаем событие в дочерние компоненты, у которых есть свой обработчик
            if self.meta_strategist:
                await self.meta_strategist.on_settings_changed(event)

            # Обновляем watchlist в DataFeedHandler
            if self.data_feed_handler:
                new_config = await redis_manager.get_config(self.user_id, ConfigType.GLOBAL)
                new_watchlist = set(new_config.get("watchlist_symbols", []))

                added = new_watchlist - old_watchlist
                removed = old_watchlist - new_watchlist

                # Подписываемся на новые символы в WebSocket
                for symbol in added:
                    await self.global_ws_manager.subscribe_symbol(self.user_id, symbol)

                # Отписываемся от удаленных символов в WebSocket
                for symbol in removed:
                    await self.global_ws_manager.unsubscribe_symbol(self.user_id, symbol)

                # УМНАЯ СИСТЕМА ЗАМЕНЫ СИМВОЛОВ
                await self._handle_smart_symbol_replacement(new_watchlist, old_watchlist, added, removed)

            log_info(self.user_id, "Конфигурации и подписки обновлены после изменения настроек.", module_name=__name__)

        except Exception as e:
            log_error(self.user_id, f"Ошибка обработки изменения настроек: {e}", module_name=__name__)

    async def stop_all_strategies(self, reason: str):
        """
        Публичный метод для остановки всех активных стратегий сессии.
        Безопасно вызывает внутренний метод _stop_all_strategies.
        """
        await self._stop_all_strategies(reason)

    async def _stop_all_strategies(self, reason: str):
        """Остановка всех стратегий"""
        try:
            strategy_ids = list(self.active_strategies.keys())

            # Для manual_stop_command используем мягкую остановку
            if reason == "manual_stop_command":
                # Анализируем активные стратегии для определения мягкой остановки
                active_strategies_analysis = await self._analyze_active_strategies()
                strategies_with_positions = []
                strategies_without_positions = []

                for strategy_id in strategy_ids:
                    analysis = active_strategies_analysis.get(strategy_id, {})
                    if analysis.get('has_active_position', False):
                        strategies_with_positions.append(strategy_id)
                    else:
                        strategies_without_positions.append(strategy_id)

                # Стратегии с активными позициями - мягкая остановка
                for strategy_id in strategies_with_positions:
                    strategy = self.active_strategies.get(strategy_id)
                    if strategy and hasattr(strategy, 'mark_for_deferred_stop'):
                        await strategy.mark_for_deferred_stop(reason=reason)
                        log_info(self.user_id, f"🔄 Стратегия {strategy_id} помечена для мягкой остановки (есть позиция)", module_name=__name__)
                    else:
                        await self.stop_strategy(strategy_id, reason)

                # Стратегии без позиций - обычная остановка
                for strategy_id in strategies_without_positions:
                    await self.stop_strategy(strategy_id, reason)
                    log_info(self.user_id, f"⏹️ Стратегия {strategy_id} остановлена немедленно (нет позиции)", module_name=__name__)

                log_info(self.user_id, f"Применена мягкая остановка: {len(strategies_with_positions)} с позициями, {len(strategies_without_positions)} без позиций", module_name=__name__)

            else:
                # Для остальных причин - обычная остановка
                for strategy_id in strategy_ids:
                    await self.stop_strategy(strategy_id, reason)

                log_info(self.user_id, f"Остановлено {len(strategy_ids)} стратегий", module_name=__name__)

        except Exception as e:
            log_error(self.user_id, f"Ошибка остановки всех стратегий: {e}", module_name=__name__)

    async def _handle_signal_event(self, event: SignalEvent):
        """Обработчик сигналов от MetaStrategist для запуска стратегий."""
        try:
            log_info(self.user_id,
                     f"Получен сигнал-триггер {event.strategy_type} для {event.symbol} (сила: {event.signal_strength})",
                     module_name=__name__)

            self.session_stats["total_signals"] += 1

            # Запускаем стратегию. Метод start_strategy САМ получит свежие данные.
            success = await self.start_strategy(
                strategy_type=event.strategy_type,
                symbol=event.symbol
            )

            if success:
                log_info(self.user_id, f"Стратегия {event.strategy_type} для {event.symbol} запущена по сигналу",
                         module_name=__name__)
            else:
                log_warning(self.user_id, f"Не удалось запустить стратегию {event.strategy_type} для {event.symbol}",
                            module_name=__name__)
        except Exception as e:
            log_error(self.user_id, f"Ошибка обработки сигнала {event.strategy_type} для {event.symbol}: {e}",
                      module_name=__name__)

    async def _handle_strategy_restart_request(self, event: StrategyRestartRequestEvent):
        """Обработчик запроса на перезапуск стратегии с поддержкой задержки."""
        log_info(self.user_id,
                 f"Получен запрос на перезапуск стратегии {event.strategy_type} для {event.symbol} с задержкой {event.delay_seconds} сек.",
                 module_name=__name__)

        strategy_id = f"{event.strategy_type}_{event.symbol}"

        # Принудительно останавливаем старую стратегию, если она еще активна
        if strategy_id in self.active_strategies:
            log_warning(self.user_id, f"Принудительная остановка {strategy_id} перед перезапуском.",
                        module_name=__name__)
            await self.stop_strategy(strategy_id, reason="forced_restart")
            await asyncio.sleep(1)

        # Создаем асинхронную задачу для выполнения перезапуска
        asyncio.create_task(self._delayed_strategy_start(event))

    async def _delayed_strategy_start(self, event: StrategyRestartRequestEvent):
        """Выполняет запуск стратегии после указанной задержки."""
        try:
            if event.delay_seconds > 0:
                log_info(self.user_id, f"Ожидание {event.delay_seconds} секунд перед перезапуском {event.symbol}...",
                         module_name=__name__)
                await asyncio.sleep(event.delay_seconds)

            log_info(self.user_id, f"Выполнение перезапуска стратегии {event.strategy_type} для {event.symbol}.",
                     module_name=__name__)
            await self.start_strategy(
                strategy_type=event.strategy_type,
                symbol=event.symbol,
                analysis_data={'trigger': 'restart_request', 'reason': event.reason}
            )
        except Exception as e:
            log_error(self.user_id, f"Ошибка при отложенном запуске стратегии {event.symbol}: {e}",
                      module_name=__name__)

    async def recover_strategy_from_state(self, strategy_type: StrategyType, symbol: str, saved_state: Dict[str, Any]) -> bool:
        """
        Восстанавливает стратегию из сохраненного состояния после перезагрузки сервера.

        Args:
            strategy_type: Тип стратегии для восстановления
            symbol: Символ для торговли
            saved_state: Сохраненное состояние стратегии

        Returns:
            bool: True если стратегия успешно восстановлена
        """
        try:
            strategy_id = f"{strategy_type.value}_{symbol}"

            # Проверяем, не запущена ли уже такая стратегия
            if strategy_id in self.active_strategies:
                log_warning(self.user_id, f"Стратегия {strategy_id} уже активна, пропускаем восстановление", module_name=__name__)
                return True

            # Создаем стратегию с использованием factory
            strategy = create_strategy(
                strategy_type=strategy_type.value,
                bot=self.bot,
                user_id=self.user_id,
                symbol=symbol,
                signal_data=saved_state.get("signal_data", {}),
                api=self.api,
                event_bus=self.event_bus,
                config=None
            )

            if not strategy:
                log_error(self.user_id, f"Не удалось создать стратегию типа: {strategy_type.value} для восстановления", module_name=__name__)
                return False

            # Восстанавливаем состояние стратегии
            success = await strategy.recover_after_restart(saved_state)

            if success:
                # Добавляем в активные стратегии
                self.active_strategies[strategy_id] = strategy

                # Обновляем статистику
                self.session_stats["strategies_launched"] += 1

                # Публикуем событие о запуске стратегии
                event = StrategyStartEvent(
                    user_id=self.user_id,
                    strategy_type=strategy_type.value,
                    symbol=symbol,
                    strategy_id=strategy.strategy_id
                )
                await self.event_bus.publish(event)

                log_info(self.user_id, f"Стратегия {strategy_id} успешно восстановлена из состояния", module_name=__name__)
                return True
            else:
                log_error(self.user_id, f"Не удалось восстановить состояние стратегии {strategy_id}", module_name=__name__)
                return False

        except Exception as e:
            log_error(self.user_id, f"Ошибка восстановления стратегии {strategy_type.value}_{symbol}: {e}", module_name=__name__)
            return False

    async def _send_strategy_start_notification(self, strategy: BaseStrategy):
        """Отправка уведомления о запуске стратегии пользователю"""
        try:
            strategy_display_names = {
                "signal_scalper": "Signal Scalper",
                "impulse_trailing": "Impulse Trailing"
            }

            strategy_name = strategy_display_names.get(strategy.strategy_type.value, strategy.strategy_type.value)

            message = f"🚀 <b>Стратегия {strategy_name} запущена!</b>\n" \
                     f"📊 Символ: <code>{strategy.symbol}</code>\n" \
                     f"🎯 ID стратегии: <code>{strategy.strategy_id}</code>"

            if bot_manager and bot_manager.bot:
                await bot_manager.bot.send_message(
                    chat_id=self.user_id,
                    text=message,
                    parse_mode="HTML"
                )
                log_info(self.user_id, f"Уведомление о запуске стратегии {strategy.strategy_id} отправлено", module_name=__name__)
        except Exception as e:
            log_error(self.user_id, f"Ошибка отправки уведомления о запуске стратегии: {e}", module_name=__name__)

    async def create_strategy_from_active_position(self, position_info: dict, strategy_type: StrategyType = None) -> bool:
        """
        Создаёт стратегию для мониторинга существующей активной позиции.

        Args:
            position_info: Информация о позиции с биржи
            strategy_type: Тип стратегии (по умолчанию SIGNAL_SCALPER)

        Returns:
            bool: True если стратегия успешно создана
        """
        try:
            symbol = position_info.get('symbol')
            if not symbol:
                log_error(self.user_id, "Не указан символ позиции", module_name=__name__)
                return False

            # По умолчанию используем SIGNAL_SCALPER
            if not strategy_type:
                strategy_type = StrategyType.SIGNAL_SCALPER

            strategy_id = f"{strategy_type.value}_{symbol}"

            # Проверяем, не запущена ли уже такая стратегия
            if strategy_id in self.active_strategies:
                log_warning(self.user_id, f"Стратегия {strategy_id} уже активна", module_name=__name__)
                return True

            # Создаем стратегию с использованием factory
            strategy = create_strategy(
                strategy_type=strategy_type.value,
                bot=self.bot,
                user_id=self.user_id,
                symbol=symbol,
                signal_data={},
                api=self.api,
                event_bus=self.event_bus,
                config=None
            )

            if not strategy:
                log_error(self.user_id, f"Не удалось создать стратегию типа: {strategy_type.value}", module_name=__name__)
                return False

            # КРИТИЧЕСКИ ВАЖНО: Синхронизируем состояние стратегии с активной позицией
            success = await self._sync_strategy_with_position(strategy, position_info)

            if success:
                # Добавляем в активные стратегии
                self.active_strategies[strategy_id] = strategy

                # Обновляем статистику
                self.session_stats["strategies_launched"] += 1

                # Публикуем событие о запуске стратегии
                event = StrategyStartEvent(
                    user_id=self.user_id,
                    strategy_type=strategy_type.value,
                    symbol=symbol,
                    strategy_id=strategy.strategy_id
                )
                await self.event_bus.publish(event)

                # Отправляем уведомление о восстановлении мониторинга
                await self._send_position_monitoring_notification(strategy, position_info)

                log_info(self.user_id, f"✅ Стратегия {strategy_id} создана для мониторинга активной позиции", module_name=__name__)
                return True
            else:
                log_error(self.user_id, f"❌ Не удалось синхронизировать стратегию с позицией {symbol}", module_name=__name__)
                return False

        except Exception as e:
            log_error(self.user_id, f"Ошибка создания стратегии из позиции: {e}", module_name=__name__)
            return False

    async def _sync_strategy_with_position(self, strategy, position_info: dict) -> bool:
        """
        УМНАЯ СИНХРОНИЗАЦИЯ: Восстанавливает состояние стратегии на основе данных из БД и биржи.
        НЕ создаёт новые ордера, а восстанавливает связь с существующими.
        """
        try:
            from database.db_trades import db_manager

            # Извлекаем данные позиции
            symbol = position_info.get('symbol')
            side = position_info.get('side')  # 'Buy' или 'Sell'
            size = float(position_info.get('size', 0))
            entry_price = float(position_info.get('entry_price', 0))

            if size <= 0 or entry_price <= 0:
                log_error(self.user_id, f"Некорректные данные позиции: size={size}, entry_price={entry_price}", module_name=__name__)
                return False

            # Конвертируем направление позиции
            direction = "LONG" if side == "Buy" else "SHORT"

            log_info(self.user_id, f"🔍 Восстанавливаю стратегию для позиции {symbol} {direction} {size} @ {entry_price}", module_name=__name__)

            # ШАГ 1: Получаем ВСЕ активные ордера бота для этого символа из БД
            active_orders_from_db = await db_manager.get_active_orders_by_user(
                user_id=self.user_id,
                symbol=symbol,
                strategy_type=strategy.strategy_type.value
            )

            log_info(self.user_id, f"🗄️ Найдено {len(active_orders_from_db)} активных ордеров в БД для {symbol}", module_name=__name__)

            # ШАГ 2: Синхронизируем каждый ордер с биржей
            restored_orders = {}
            for db_order in active_orders_from_db:
                exchange_order_id = db_order['order_id']

                try:
                    # Проверяем статус ордера на бирже
                    order_status_on_exchange = await self.api.get_order_status(exchange_order_id)

                    if order_status_on_exchange:
                        # Ордер существует на бирже - синхронизируем статус
                        exchange_status = order_status_on_exchange.get('status', 'UNKNOWN')
                        db_status = db_order['status']

                        if exchange_status != db_status:
                            # Обновляем статус в БД
                            await db_manager.update_order_status(
                                order_id=exchange_order_id,
                                status=exchange_status,
                                filled_quantity=Decimal(str(order_status_on_exchange.get('filled_qty', 0))),
                                average_price=Decimal(str(order_status_on_exchange.get('avg_price', 0)))
                            )
                            log_info(self.user_id, f"🔄 Обновлён статус ордера {exchange_order_id}: {db_status} → {exchange_status}", module_name=__name__)

                        # Если ордер активен, добавляем в стратегию
                        if exchange_status in ['NEW', 'PENDING', 'PARTIALLY_FILLED']:
                            restored_orders[exchange_order_id] = {
                                "order_id": exchange_order_id,
                                "status": exchange_status,
                                "type": db_order['order_type'],
                                "side": db_order['side'],
                                "quantity": db_order['quantity'],
                                "price": db_order['price']
                            }
                            log_info(self.user_id, f"✅ Восстановлен ордер {exchange_order_id} ({db_order['order_type']})", module_name=__name__)
                    else:
                        # Ордер не найден на бирже - помечаем как потерянный
                        await db_manager.update_order_status(
                            order_id=exchange_order_id,
                            status='LOST',
                            metadata={"lost_at": datetime.now().isoformat(), "reason": "not_found_on_exchange"}
                        )
                        log_warning(self.user_id, f"⚠️ Ордер {exchange_order_id} не найден на бирже, помечен как потерянный", module_name=__name__)

                except Exception as order_error:
                    log_error(self.user_id, f"Ошибка проверки ордера {exchange_order_id}: {order_error}", module_name=__name__)

            # ШАГ 3: Устанавливаем состояние позиции в стратегии
            strategy.position_active = True
            strategy.active_direction = direction
            strategy.entry_price = Decimal(str(entry_price))
            strategy.position_size = Decimal(str(size))
            strategy.peak_profit_usd = Decimal('0')
            strategy.hold_signal_counter = 0

            # ШАГ 4: Восстанавливаем ордера в стратегии
            strategy.active_orders = restored_orders

            # Определяем специфичные ордера (стоп-лосс, тейк-профит)
            for order_id, order_data in restored_orders.items():
                if order_data['type'] == 'STOP' or 'stop' in order_data.get('metadata', {}).get('purpose', '').lower():
                    strategy.stop_loss_order_id = order_id
                    log_info(self.user_id, f"🛡️ Восстановлен стоп-лосс: {order_id}", module_name=__name__)

            # ШАГ 5: Подписываемся на события цен для мониторинга
            await strategy.event_bus.subscribe(EventType.PRICE_UPDATE, strategy.handle_price_update, user_id=strategy.user_id)

            # ШАГ 6: Сохраняем состояние для будущего восстановления
            await strategy.save_strategy_state({
                "restored_from_position": True,
                "original_position_info": position_info,
                "restored_orders_count": len(restored_orders),
                "restoration_time": datetime.now().isoformat()
            })

            log_info(self.user_id, f"✅ Стратегия восстановлена: {symbol} {direction} {size} @ {entry_price}, ордеров: {len(restored_orders)}", module_name=__name__)
            return True

        except Exception as e:
            log_error(self.user_id, f"Ошибка умной синхронизации стратегии с позицией: {e}", module_name=__name__)
            return False

    async def _send_position_monitoring_notification(self, _strategy, position_info: dict):
        """Отправляет уведомление о начале мониторинга активной позиции."""
        try:
            symbol = position_info.get('symbol')
            side = position_info.get('side')
            size = position_info.get('size', 0)
            entry_price = position_info.get('entry_price', 0)
            direction = "LONG 🟢" if side == "Buy" else "SHORT 🔴"

            message = (
                f"🛡️ <b>МОНИТОРИНГ ПОЗИЦИИ ВОССТАНОВЛЕН</b>\n\n"
                f"▫️ Стратегия: Signal Scalper\n"
                f"▫️ Символ: {symbol}\n"
                f"▫️ Направление: {direction}\n"
                f"▫️ Размер: {size}\n"
                f"▫️ Цена входа: {entry_price} USDT\n\n"
                f"✅ Стратегия автоматически возобновила:\n"
                f"• 📊 Мониторинг P&L\n"
                f"• 🛡️ Защитный стоп-лосс\n"
                f"• 🎯 Трейлинг прибыли\n"
                f"• 📈 Анализ сигналов\n\n"
                f"🔄 <b>Позиция под полным контролем!</b>"
            )

            if bot_manager and bot_manager.bot:
                await bot_manager.bot.send_message(
                    chat_id=self.user_id,
                    text=message,
                    parse_mode="HTML"
                )

        except Exception as e:
            log_error(self.user_id, f"Ошибка отправки уведомления о мониторинге позиции: {e}", module_name=__name__)

    # ===============================================================================
    # СИСТЕМА УПРАВЛЕНИЯ СЛОТАМИ МНОГОСИМВОЛЬНЫХ СТРАТЕГИЙ
    # ===============================================================================

    async def _check_strategy_slots(self, strategy_type: str, symbol: str) -> str:
        """
        Проверяет возможность запуска стратегии с учётом лимитов слотов.

        Returns:
            str: Одно из значений:
                - "start_immediately" - есть свободный слот, можно запускать
                - "replaced_inactive" - заменена неактивная стратегия того же типа
                - "queued" - добавлено в очередь ожидания (все слоты заняты)
                - "blocked" - запуск заблокирован (все слоты заняты активными позициями)
        """
        try:
            # Получаем все стратегии данного типа
            same_type_strategies = [
                (sid, strategy) for sid, strategy in self.active_strategies.items()
                if strategy.strategy_type.value == strategy_type
            ]

            log_info(self.user_id, f"🔍 Проверка слотов для {strategy_type}_{symbol}: найдено {len(same_type_strategies)} стратегий того же типа", module_name=__name__)

            # Если меньше лимита - можно запускать сразу
            if len(same_type_strategies) < self.MAX_STRATEGY_SLOTS:
                log_info(self.user_id, f"✅ Есть свободный слот: {len(same_type_strategies)}/{self.MAX_STRATEGY_SLOTS}", module_name=__name__)
                return "start_immediately"

            # Все слоты заняты - ищем неактивные стратегии для замены
            inactive_strategies = []
            active_strategies = []

            for strategy_id, strategy in same_type_strategies:
                has_position = getattr(strategy, 'position_active', False)
                if has_position:
                    active_strategies.append((strategy_id, strategy))
                else:
                    inactive_strategies.append((strategy_id, strategy))

            log_info(self.user_id, f"📊 Анализ слотов: активных {len(active_strategies)}, неактивных {len(inactive_strategies)}", module_name=__name__)

            # Если есть неактивные стратегии - заменяем первую
            if inactive_strategies:
                strategy_to_replace_id, strategy_to_replace = inactive_strategies[0]
                log_info(self.user_id, f"🔄 Заменяю неактивную стратегию {strategy_to_replace_id} на {strategy_type}_{symbol}", module_name=__name__)

                # Останавливаем старую стратегию
                await self.stop_strategy(strategy_to_replace_id, reason=f"replaced_by_{symbol}")
                return "replaced_inactive"

            # Все слоты заняты активными позициями - добавляем в очередь
            log_info(self.user_id, f"⏳ Все слоты заняты активными позициями, добавляю {symbol} в очередь", module_name=__name__)
            await self._add_to_strategy_queue(strategy_type, symbol)
            return "queued"

        except Exception as e:
            log_error(self.user_id, f"Ошибка проверки слотов стратегии: {e}", module_name=__name__)
            return "blocked"

    async def _add_to_strategy_queue(self, strategy_type: str, symbol: str):
        """Добавляет символ в очередь ожидания для стратегии."""
        try:
            if strategy_type not in self.strategy_queues:
                self.strategy_queues[strategy_type] = []

            if symbol not in self.strategy_queues[strategy_type]:
                self.strategy_queues[strategy_type].append(symbol)
                log_info(self.user_id, f"📝 Символ {symbol} добавлен в очередь для {strategy_type}. Очередь: {self.strategy_queues[strategy_type]}", module_name=__name__)

                # Отправляем уведомление пользователю
                await self._send_queue_notification(strategy_type, symbol, len(self.strategy_queues[strategy_type]))

        except Exception as e:
            log_error(self.user_id, f"Ошибка добавления в очередь: {e}", module_name=__name__)

    async def _process_strategy_queue(self, strategy_type: str):
        """Обрабатывает очередь ожидания при освобождении слота."""
        try:
            if strategy_type not in self.strategy_queues or not self.strategy_queues[strategy_type]:
                return

            # Проверяем, есть ли свободные слоты
            same_type_strategies = [
                strategy for strategy in self.active_strategies.values()
                if strategy.strategy_type.value == strategy_type
            ]

            if len(same_type_strategies) >= self.MAX_STRATEGY_SLOTS:
                log_debug(self.user_id, f"Слоты для {strategy_type} всё ещё заняты, очередь ожидает", module_name=__name__)
                return

            # Берём первый символ из очереди
            next_symbol = self.strategy_queues[strategy_type].pop(0)
            log_info(self.user_id, f"🎯 Обрабатываю очередь: запускаю {strategy_type} для {next_symbol}", module_name=__name__)

            # Запускаем стратегию
            success = await self.start_strategy(
                strategy_type=strategy_type,
                symbol=next_symbol,
                analysis_data={'trigger': 'queue_processing'}
            )

            if success:
                log_info(self.user_id, f"✅ Стратегия из очереди успешно запущена: {strategy_type}_{next_symbol}", module_name=__name__)
                await self._send_queue_processed_notification(strategy_type, next_symbol)
            else:
                log_warning(self.user_id, f"⚠️ Не удалось запустить стратегию из очереди: {strategy_type}_{next_symbol}", module_name=__name__)
                # Возвращаем символ в начало очереди для повторной попытки
                self.strategy_queues[strategy_type].insert(0, next_symbol)

        except Exception as e:
            log_error(self.user_id, f"Ошибка обработки очереди стратегий: {e}", module_name=__name__)

    async def _send_queue_notification(self, strategy_type: str, symbol: str, queue_position: int):
        """Отправляет уведомление о добавлении в очередь."""
        try:
            strategy_name = strategy_type.replace('_', ' ').title()
            message = (
                f"⏳ <b>СИМВОЛ ДОБАВЛЕН В ОЧЕРЕДЬ</b>\n\n"
                f"▫️ Стратегия: {strategy_name}\n"
                f"▫️ Символ: <code>{symbol}</code>\n"
                f"▫️ Позиция в очереди: {queue_position}\n\n"
                f"🔍 <b>Причина ожидания:</b>\n"
                f"Все {self.MAX_STRATEGY_SLOTS} слота заняты активными позициями.\n\n"
                f"✅ Символ автоматически запустится, как только освободится слот!"
            )

            if bot_manager and bot_manager.bot:
                await bot_manager.bot.send_message(
                    chat_id=self.user_id,
                    text=message,
                    parse_mode="HTML"
                )

        except Exception as e:
            log_error(self.user_id, f"Ошибка отправки уведомления об очереди: {e}", module_name=__name__)

    async def _send_queue_processed_notification(self, strategy_type: str, symbol: str):
        """Отправляет уведомление об обработке очереди."""
        try:
            strategy_name = strategy_type.replace('_', ' ').title()
            message = (
                f"🎯 <b>ОЧЕРЕДЬ ОБРАБОТАНА</b>\n\n"
                f"▫️ Стратегия: {strategy_name}\n"
                f"▫️ Символ: <code>{symbol}</code>\n\n"
                f"✅ Слот освободился и символ автоматически запущен!\n"
                f"🚀 Стратегия начала работу"
            )

            if bot_manager and bot_manager.bot:
                await bot_manager.bot.send_message(
                    chat_id=self.user_id,
                    text=message,
                    parse_mode="HTML"
                )

        except Exception as e:
            log_error(self.user_id, f"Ошибка отправки уведомления об обработке очереди: {e}", module_name=__name__)

    async def _handle_smart_symbol_replacement(self, new_watchlist: set, old_watchlist: set, added: set, removed: set):
        """
        Умная система замены символов в watchlist с учетом активных позиций и лимита одновременных торгов.

        Логика:
        1. Если символ удален и у него нет активной позиции - останавливаем стратегию сразу
        2. Если символ удален и у него есть активная позиция - помечаем для отложенной остановки
        3. Если добавлен новый символ и есть место - запускаем стратегию
        4. Максимум 3 одновременных торга (настраивается)
        """
        try:
            # Получаем лимит одновременных торгов из конфигурации
            risk_config = await redis_manager.get_config(self.user_id, ConfigType.GLOBAL)
            max_concurrent_trades = risk_config.get("max_concurrent_trades", 3)

            log_info(self.user_id, f"🔧 Умная замена символов: добавлено {len(added)}, удалено {len(removed)}, лимит торгов: {max_concurrent_trades}", module_name=__name__)

            # Анализируем текущие активные стратегии
            active_strategies_analysis = await self._analyze_active_strategies()
            current_trading_count = len([s for s in active_strategies_analysis.values() if s['has_active_position']])

            log_info(self.user_id, f"📊 Анализ активных стратегий: всего {len(active_strategies_analysis)}, с позициями {current_trading_count}/{max_concurrent_trades}", module_name=__name__)

            # === ОБРАБОТКА УДАЛЕННЫХ СИМВОЛОВ ===
            strategies_to_stop_immediately = []
            strategies_to_mark_for_deferred_stop = []

            for symbol in removed:
                for strategy_id, analysis in active_strategies_analysis.items():
                    if analysis['symbol'] == symbol:
                        if analysis['has_active_position']:
                            # У символа есть активная позиция - помечаем для отложенной остановки
                            strategies_to_mark_for_deferred_stop.append((strategy_id, symbol, analysis))
                            log_info(self.user_id, f"🔄 Символ {symbol} помечен для отложенной остановки (есть активная позиция)", module_name=__name__)
                        else:
                            # Нет активной позиции - можем остановить сразу
                            strategies_to_stop_immediately.append((strategy_id, symbol))
                            log_info(self.user_id, f"⏹️ Символ {symbol} будет остановлен немедленно (нет активной позиции)", module_name=__name__)

            # Останавливаем стратегии без активных позиций
            for strategy_id, symbol in strategies_to_stop_immediately:
                await self.stop_strategy(strategy_id, reason=f"symbol_{symbol}_removed_from_watchlist")
                current_trading_count -= 1

            # Помечаем стратегии с активными позициями для отложенной остановки
            for strategy_id, symbol, analysis in strategies_to_mark_for_deferred_stop:
                strategy = self.active_strategies.get(strategy_id)
                if strategy and hasattr(strategy, 'mark_for_deferred_stop'):
                    await strategy.mark_for_deferred_stop(reason=f"symbol_{symbol}_removed_from_watchlist")
                    log_info(self.user_id, f"📝 Стратегия {strategy_id} помечена для остановки после завершения позиции", module_name=__name__)

            # === ОБРАБОТКА ДОБАВЛЕННЫХ СИМВОЛОВ ===
            available_slots = max_concurrent_trades - current_trading_count
            symbols_to_start = []

            log_info(self.user_id, f"🎯 Доступно слотов для новых символов: {available_slots}", module_name=__name__)

            for symbol in added:
                if available_slots > 0:
                    # Проверяем, нет ли уже стратегии для этого символа
                    # ИСПРАВЛЕНО: используем корректное формирование strategy_id как в start_strategy
                    strategy_id = f"{StrategyType.SIGNAL_SCALPER.value}_{symbol}"
                    if strategy_id not in self.active_strategies:
                        symbols_to_start.append(symbol)
                        available_slots -= 1
                        log_info(self.user_id, f"✅ Символ {symbol} будет запущен немедленно (есть свободный слот)", module_name=__name__)
                    else:
                        log_info(self.user_id, f"ℹ️ Символ {symbol} уже имеет активную стратегию", module_name=__name__)
                else:
                    log_info(self.user_id, f"⏳ Символ {symbol} ожидает освобождения слота (лимит {max_concurrent_trades} достигнут)", module_name=__name__)
                    # Можно добавить в очередь ожидания для будущей реализации

            # Запускаем новые стратегии для добавленных символов
            for symbol in symbols_to_start:
                success = await self.start_strategy(
                    strategy_type=StrategyType.SIGNAL_SCALPER.value,
                    symbol=symbol,
                    analysis_data={'trigger': 'smart_symbol_replacement'}
                )
                if success:
                    log_info(self.user_id, f"🚀 Стратегия для {symbol} успешно запущена", module_name=__name__)
                else:
                    log_warning(self.user_id, f"⚠️ Не удалось запустить стратегию для {symbol}", module_name=__name__)

            # Отправляем уведомление пользователю о произведенных изменениях
            await self._send_symbol_replacement_notification(
                strategies_to_stop_immediately,
                strategies_to_mark_for_deferred_stop,
                symbols_to_start,
                available_slots,
                max_concurrent_trades
            )

        except Exception as e:
            log_error(self.user_id, f"Ошибка в умной системе замены символов: {e}", module_name=__name__)

    async def _analyze_active_strategies(self) -> Dict[str, Dict[str, Any]]:
        """
        Анализирует состояние всех активных стратегий.

        Returns:
            Dict: Информация о каждой стратегии {strategy_id: {symbol, has_active_position, strategy_type, ...}}
        """
        analysis = {}

        for strategy_id, strategy in self.active_strategies.items():
            try:
                has_position = False

                # Проверяем, есть ли активная позиция у стратегии
                if hasattr(strategy, 'position_active'):
                    has_position = strategy.position_active
                else:
                    # Fallback: проверяем по размеру позиции
                    position_size = getattr(strategy, 'position_size', 0)
                    has_position = position_size and position_size > 0

                analysis[strategy_id] = {
                    'symbol': strategy.symbol,
                    'strategy_type': strategy.strategy_type.value,
                    'has_active_position': has_position,
                    'is_waiting_for_trade': getattr(strategy, 'is_waiting_for_trade', False),
                    'position_size': getattr(strategy, 'position_size', 0),
                    'entry_price': getattr(strategy, 'entry_price', None)
                }

            except Exception as e:
                log_error(self.user_id, f"Ошибка анализа стратегии {strategy_id}: {e}", module_name=__name__)
                analysis[strategy_id] = {
                    'symbol': strategy.symbol,
                    'strategy_type': 'unknown',
                    'has_active_position': False,
                    'error': str(e)
                }

        return analysis

    async def _send_symbol_replacement_notification(self, stopped_immediately, marked_for_deferred_stop, started_symbols, available_slots, max_concurrent):
        """Отправляет уведомление пользователю о произведенных изменениях в watchlist."""
        try:
            if not (stopped_immediately or marked_for_deferred_stop or started_symbols):
                return  # Нет изменений для уведомления

            message_parts = ["🔄 <b>УМНАЯ ЗАМЕНА СИМВОЛОВ</b>\n"]

            if stopped_immediately:
                message_parts.append("⏹️ <b>Остановлено немедленно:</b>")
                for strategy_id, symbol in stopped_immediately:
                    message_parts.append(f"▫️ {symbol} (не было позиции)")
                message_parts.append("")

            if marked_for_deferred_stop:
                message_parts.append("⏳ <b>Помечено для остановки после закрытия позиции:</b>")
                for strategy_id, symbol, analysis in marked_for_deferred_stop:
                    message_parts.append(f"▫️ {symbol} (активная позиция)")
                message_parts.append("")

            if started_symbols:
                message_parts.append("🚀 <b>Запущено:</b>")
                for symbol in started_symbols:
                    message_parts.append(f"▫️ {symbol}")
                message_parts.append("")

            # Добавляем информацию о лимитах
            current_active = len(self.active_strategies)
            message_parts.append(f"📊 <b>Статус торговых слотов:</b>")
            message_parts.append(f"▫️ Активных стратегий: {current_active}")
            message_parts.append(f"▫️ Максимум одновременно: {max_concurrent}")
            message_parts.append(f"▫️ Доступно слотов: {available_slots}")

            message = "\n".join(message_parts)

            if bot_manager and bot_manager.bot:
                await bot_manager.bot.send_message(
                    chat_id=self.user_id,
                    text=message,
                    parse_mode="HTML"
                )

        except Exception as e:
            log_error(self.user_id, f"Ошибка отправки уведомления о замене символов: {e}", module_name=__name__)