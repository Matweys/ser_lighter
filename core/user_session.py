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
    EventType,
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
from core.enums import StrategyType
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

                # Отправка уведомления пользователю после УСПЕШНОГО запуска и добавления в active_strategies
                await self._send_strategy_start_notification(strategy)

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

                for symbol in added:
                    await self.global_ws_manager.subscribe_symbol(self.user_id, symbol)
                for symbol in removed:
                    await self.global_ws_manager.unsubscribe_symbol(self.user_id, symbol)

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