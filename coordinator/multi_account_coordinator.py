"""
Multi-Account Coordinator - главный координатор 3 ботов

Управляет активацией/деактивацией ботов на основе их статусов.
"""
import asyncio
from typing import Dict, List, Optional
from decimal import Decimal
from dataclasses import dataclass

from core.logger import log_info, log_warning, log_error, log_debug
from strategies.signal_scalper_strategy import SignalScalperStrategy
from cache.redis_manager import redis_manager
from core.enums import ConfigType


@dataclass
class BotData:
    """Данные одного бота"""
    priority: int  # 1=PRIMARY, 2=SECONDARY, 3=TERTIARY
    strategy: SignalScalperStrategy
    status: str  # 'free', 'stuck', 'active'


class MultiAccountCoordinator:
    """
    Координатор для управления 3 ботами на одном символе.

    Ключевые принципы:
    1. НЕ влияет на логику стратегии
    2. Только активирует/деактивирует ботов
    3. Мониторит PnL для определения "застревания"
    4. Приоритет: Бот 1 > Бот 2 > Бот 3
    """

    MONITOR_INTERVAL = 5  # Проверка каждые 5 секунд

    def __init__(self, user_id: int, symbol: str, bot_strategies: List[SignalScalperStrategy]):
        """
        Инициализация координатора.

        Args:
            user_id: ID пользователя
            symbol: Торгуемый символ
            bot_strategies: Список из 3 готовых экземпляров SignalScalperStrategy
        """
        self.user_id = user_id
        self.symbol = symbol
        self.bots: Dict[int, BotData] = {}
        self.active_bots = set()  # Множество ID активных ботов
        self.running = False
        self._watchlist_removal_logged = False  # Флаг для однократного логирования удаления из watchlist
        self._lock = asyncio.Lock()  # Блокировка для предотвращения race conditions при активации/деактивации
        self._stop_event = asyncio.Event()  # Event для сигнализации остановки
        self._monitor_task: Optional[asyncio.Task] = None  # Ссылка на задачу мониторинга

        # Поля для отложенной остановки
        self.deferred_stop_marked = False  # Флаг отложенной остановки координатора
        self.deferred_stop_reason: Optional[str] = None  # Причина отложенной остановки

        # Создаём BotData для каждой стратегии
        if len(bot_strategies) != 3:
            raise ValueError(f"Необходимо ровно 3 стратегии, получено: {len(bot_strategies)}")

        for priority, strategy in enumerate(bot_strategies, start=1):
            self.bots[priority] = BotData(
                priority=priority,
                strategy=strategy,
                status='free'
            )

        log_info(self.user_id,
                f"🔀 MultiAccountCoordinator создан для {symbol}: 3 бота готовы",
                "Coordinator")

    @property
    def is_running(self) -> bool:
        """Возвращает текущий статус работы координатора"""
        return self.running

    async def start(self, initial_bot_priority: int = 1):
        """
        Запуск координатора.

        Args:
            initial_bot_priority: Приоритет бота для активации (1, 2 или 3).
                                  При восстановлении после перезапуска указывается
                                  тот бот, который реально держит позицию на бирже.
        """
        if self.running:
            log_warning(self.user_id, f"Coordinator для {self.symbol} уже запущен", "Coordinator")
            return

        self.running = True
        self._stop_event.clear()  # Сбрасываем event при запуске
        log_info(self.user_id, f"🟢 Запуск Coordinator для {self.symbol}", "Coordinator")

        # Активируем бота с указанным приоритетом (по дефолту Бот 1)
        log_info(self.user_id,
                f"🎯 Активация Бота {initial_bot_priority} при старте координатора",
                "Coordinator")
        await self._activate_bot(initial_bot_priority)

        # Запускаем фоновый мониторинг и сохраняем ссылку на задачу
        self._monitor_task = asyncio.create_task(self._monitor_loop())

    async def stop(self):
        """
        Умная остановка координатора с Event-based сигнализацией.

        Логика:
        1. Устанавливаем флаг running=False
        2. Ждём установки _stop_event (сигнал от _monitor_loop о завершении)
        3. Если event не установился за 7 секунд → таймаут (принудительная остановка)
        4. Деактивируем всех ботов
        5. Очищаем память
        """
        if not self.running:
            return

        # ШАГ 1: Останавливаем флаг мониторинга
        self.running = False
        log_info(self.user_id, f"🔴 Остановка Coordinator для {self.symbol}", "Coordinator")

        # ШАГ 2: Ждём завершения цикла мониторинга
        # Цикл мониторинга установит _stop_event когда полностью завершится
        # Таймаут: 7 секунд (интервал мониторинга 5 сек + запас 2 сек)
        try:
            await asyncio.wait_for(self._stop_event.wait(), timeout=7.0)
            log_info(self.user_id,
                    f"✅ Цикл мониторинга для {self.symbol} корректно завершился",
                    "Coordinator")
        except asyncio.TimeoutError:
            log_warning(self.user_id,
                       f"⚠️ Таймаут ожидания завершения цикла мониторинга для {self.symbol} (7 сек). "
                       f"Принудительная остановка.",
                       "Coordinator")
            # Если таймаут, пытаемся отменить задачу мониторинга
            if self._monitor_task and not self._monitor_task.done():
                self._monitor_task.cancel()
                try:
                    await self._monitor_task
                except asyncio.CancelledError:
                    pass

        # ШАГ 3: Деактивируем всех активных ботов
        for priority in list(self.active_bots):
            await self._deactivate_bot(priority)

        # ШАГ 4: Очищаем память
        # Очищаем словарь ботов и множество активных ботов для предотвращения утечек памяти
        self.bots.clear()
        self.active_bots.clear()
        self._monitor_task = None
        self._watchlist_removal_logged = False  # Сбрасываем флаг

        log_info(self.user_id, f"✅ Coordinator для {self.symbol} полностью остановлен и очищен", "Coordinator")

    async def mark_for_deferred_stop(self, reason: str) -> bool:
        """
        Помечает координатор для отложенной остановки после завершения всех активных позиций.

        ✅ КРИТИЧНО: Эта функция вызывается когда пользователь удаляет символ из watchlist.

        Логика:
        1. Устанавливаем флаг deferred_stop_marked
        2. Помечаем каждого бота с активной позицией для deferred_stop
        3. Деактивируем ботов БЕЗ позиций (они могут остановиться сразу)
        4. Боты С позициями продолжат работу до закрытия позиции
        5. После закрытия всех позиций координатор остановится автоматически

        Args:
            reason: Причина отложенной остановки

        Returns:
            bool: True если координатор успешно помечен для остановки
        """
        try:
            self.deferred_stop_marked = True
            self.deferred_stop_reason = reason

            log_info(self.user_id,
                    f"🔄 Coordinator для {self.symbol} помечен для отложенной остановки: {reason}",
                    "Coordinator")

            # Анализируем состояние всех ботов
            bots_with_positions = []
            bots_without_positions = []

            for priority, bot_data in self.bots.items():
                strategy = bot_data.strategy
                if strategy.position_active:
                    bots_with_positions.append((priority, strategy))
                    log_info(self.user_id,
                            f"📝 Бот {priority} имеет активную позицию → помечаю для отложенной остановки",
                            "Coordinator")
                else:
                    # Бот без позиции, но проверим, активен ли он
                    if priority in self.active_bots:
                        bots_without_positions.append(priority)
                        log_info(self.user_id,
                                f"⏹️ Бот {priority} без позиции → будет деактивирован немедленно",
                                "Coordinator")

            # Помечаем каждого бота С позицией для deferred_stop
            for priority, strategy in bots_with_positions:
                if hasattr(strategy, 'mark_for_deferred_stop'):
                    await strategy.mark_for_deferred_stop(reason)
                    log_info(self.user_id,
                            f"✅ Бот {priority} помечен для остановки после завершения позиции",
                            "Coordinator")

            # Деактивируем ботов БЕЗ позиций (они свободны и могут остановиться сразу)
            for priority in bots_without_positions:
                await self._deactivate_bot(priority)
                log_info(self.user_id,
                        f"🔴 Бот {priority} деактивирован (не имел позиции)",
                        "Coordinator")

            # Итоговый лог
            if bots_with_positions:
                log_info(self.user_id,
                        f"⏳ Coordinator для {self.symbol}: {len(bots_with_positions)} бот(ов) продолжат работу до закрытия позиций. "
                        f"После закрытия всех позиций координатор остановится автоматически.",
                        "Coordinator")
            else:
                log_info(self.user_id,
                        f"✅ Coordinator для {self.symbol}: нет активных позиций, можно остановить немедленно",
                        "Coordinator")

            return True

        except Exception as e:
            log_error(self.user_id, f"Ошибка пометки координатора для отложенной остановки: {e}", "Coordinator")
            return False

    async def check_deferred_stop(self) -> bool:
        """
        Проверяет, должен ли координатор быть остановлен отложенно.

        ✅ КРИТИЧНО: Вызывается в цикле мониторинга после обновления статусов.

        Логика:
        1. Проверяем флаг deferred_stop_marked
        2. Проверяем, остались ли боты с активными позициями
        3. Если ВСЕ боты закрыли позиции → останавливаем координатор
        4. Если есть хотя бы один бот с позицией → продолжаем работу

        Returns:
            bool: True если координатор был остановлен
        """
        try:
            if not self.deferred_stop_marked:
                return False

            # Проверяем, остались ли боты с активными позициями
            bots_with_positions = []
            for priority, bot_data in self.bots.items():
                strategy = bot_data.strategy
                if strategy.position_active:
                    bots_with_positions.append(priority)

            if bots_with_positions:
                # Есть боты с позициями - продолжаем работу
                # Логируем только раз в 60 секунд (каждые 12 циклов мониторинга по 5 сек)
                # чтобы не спамить
                return False

            # ВСЕ боты закрыли позиции - можем остановить координатор
            reason = self.deferred_stop_reason or 'deferred_stop_triggered'
            log_info(self.user_id,
                    f"✅ Выполнение отложенной остановки Coordinator для {self.symbol}: все боты закрыли позиции. Причина: {reason}",
                    "Coordinator")

            # Сбрасываем флаги отложенной остановки
            self.deferred_stop_marked = False
            self.deferred_stop_reason = None

            # Останавливаем координатор
            await self.stop()
            return True

        except Exception as e:
            log_error(self.user_id, f"Ошибка проверки отложенной остановки координатора: {e}", "Coordinator")
            return False

    def has_active_positions(self) -> bool:
        """
        Проверяет, есть ли у какого-то из ботов активная позиция.

        Returns:
            bool: True если хотя бы один бот имеет активную позицию
        """
        for priority, bot_data in self.bots.items():
            strategy = bot_data.strategy
            if hasattr(strategy, 'position_active') and strategy.position_active:
                return True
        return False

    async def _monitor_loop(self):
        """
        Бесконечный цикл мониторинга и ротации.

        Порядок операций:
        1. Обновить статусы всех ботов
        2. Проверить необходимость активации следующего бота
        3. Проверить необходимость деактивации бота
        """
        log_info(self.user_id, f"👁️ Запущен мониторинг для {self.symbol}", "Coordinator")

        while self.running:
            await asyncio.sleep(self.MONITOR_INTERVAL)

            # КРИТИЧНО: Проверяем флаг перед любыми операциями
            if not self.running:
                log_info(self.user_id, f"🛑 Цикл мониторинга для {self.symbol} завершён (флаг остановки)", "Coordinator")
                break

            try:
                # ШАГ 1: Обновляем статусы всех ботов
                await self._update_statuses()

                # Проверяем флаг после каждого шага
                if not self.running:
                    break

                # ШАГ 2: Проверяем отложенную остановку
                # КРИТИЧНО: Проверяем ДО активации/деактивации, чтобы остановиться как только все позиции закрыты
                stopped = await self.check_deferred_stop()
                if stopped:
                    # Координатор остановился - выходим из цикла
                    log_info(self.user_id, f"🛑 Цикл мониторинга для {self.symbol} завершён (deferred stop выполнен)", "Coordinator")
                    break

                if not self.running:
                    break

                # ШАГ 3: Проверяем необходимость активации
                await self._check_activation_needed()

                if not self.running:
                    break

                # ШАГ 4: Проверяем необходимость деактивации
                await self._check_deactivation_needed()

            except Exception as e:
                log_error(self.user_id, f"Ошибка в monitor_loop для {self.symbol}: {e}", "Coordinator")

        log_info(self.user_id, f"✅ Цикл мониторинга для {self.symbol} полностью остановлен", "Coordinator")

        # Устанавливаем event для сигнализации о завершении
        self._stop_event.set()

    async def _update_statuses(self):
        """
        Обновляет статусы всех ботов.

        Статусы:
        - 'free': Бот НЕ в позиции
        - 'active': Бот в позиции, PnL >= -10%
        - 'stuck': Бот в позиции, PnL < -10%
        """
        for priority, bot_data in self.bots.items():
            strategy = bot_data.strategy
            old_status = bot_data.status

            if not strategy.position_active:
                bot_data.status = 'free'
            else:
                pnl_percent = self._calculate_pnl_percent(strategy)

                # Получаем порог застревания из конфига стратегии
                stuck_threshold = -Decimal(str(strategy.get_config_value("stuck_threshold_percent", 4.0)))

                # ДИАГНОСТИКА: Логируем расчет PnL% только раз в 5 секунд (цикл мониторинга)
                # log_info(self.user_id,
                #          f"📊 [Бот {priority}] PnL: {pnl_percent:.2f}% | Маржа: ${strategy.initial_margin_usd:.2f} | Порог: {stuck_threshold}%",
                #          "Coordinator")

                if pnl_percent < stuck_threshold:
                    bot_data.status = 'stuck'

                    # Логируем только изменение статуса
                    if old_status != 'stuck':
                        log_warning(self.user_id,
                                   f"🔴 БОТ {priority} ({self.symbol}) ЗАСТРЯЛ! PnL={pnl_percent:.1f}%",
                                   "Coordinator")
                else:
                    bot_data.status = 'active'

    async def _check_activation_needed(self):
        """
        Логика активации следующего бота.

        АКТИВИРУЕМ Бот N+1 если:
        - Бот N застрял (status='stuck')
        - Бот N+1 существует и НЕ активен
        - НЕТ более приоритетных СВОБОДНЫХ ботов
        - Символ ВСЁ ЕЩЁ в watchlist (не удален пользователем)

        ВАЖНО: НЕ требует блокировки, т.к. координатор работает в своём event loop
        и каждый координатор изолирован по символу.
        """
        # КРИТИЧНО: Проверяем, что символ всё ещё в watchlist
        # Если пользователь удалил символ - НЕ активируем новых ботов
        try:
            global_config = await redis_manager.get_config(self.user_id, ConfigType.GLOBAL)
            watchlist = set(global_config.get("watchlist_symbols", []))

            if self.symbol not in watchlist:
                # Символ удален из watchlist - не активируем новых ботов
                # Боты в позициях продолжат работу до закрытия сделки
                # Логируем только один раз
                if not self._watchlist_removal_logged:
                    log_info(self.user_id,
                            f"⏸️ Символ {self.symbol} удален из watchlist → НЕ активирую новых ботов (текущие позиции продолжают работу)",
                            "Coordinator")
                    self._watchlist_removal_logged = True
                return
            else:
                # Символ снова добавлен в watchlist - сбрасываем флаг
                self._watchlist_removal_logged = False
        except Exception as e:
            log_error(self.user_id, f"Ошибка проверки watchlist: {e}", "Coordinator")
            # В случае ошибки не блокируем активацию (fail-safe)

        for priority in [1, 2]:  # Проверяем Бот 1 и Бот 2
            bot_data = self.bots[priority]
            next_priority = priority + 1

            if bot_data.status == 'stuck' and next_priority not in self.active_bots:
                # КРИТИЧНО: Проверяем, есть ли более приоритетные СВОБОДНЫЕ боты
                # Если Бот 1 свободен, не нужно активировать Бот 3 (даже если Бот 2 застрял)
                has_higher_priority_free = any(
                    self.bots[p].status == 'free'
                    for p in range(1, priority)
                )

                if has_higher_priority_free:
                    # Есть более приоритетный свободный бот - не активируем следующего
                    log_info(self.user_id,
                            f"⏸️ Бот {priority} ({self.symbol}) застрял, но есть свободный более приоритетный бот → НЕ активирую Бот {next_priority}",
                            "Coordinator")
                    continue

                # Нет свободных более приоритетных - активируем следующего
                log_warning(self.user_id,
                           f"🟡 Бот {priority} ({self.symbol}) застрял → Активирую Бот {next_priority}",
                           "Coordinator")
                await self._activate_bot(next_priority)

    async def _check_deactivation_needed(self):
        """
        Логика деактивации и ротации ботов.

        ПРАВИЛА:
        1. АКТИВИРОВАТЬ более приоритетного бота ТОЛЬКО если он свободен И все вышеприоритетные тоже свободны
        2. ДЕАКТИВИРОВАТЬ менее приоритетного бота если есть свободный более приоритетный бот
        3. НЕ трогать ботов в позиции или ожидающих исполнения ордера

        Логика:
        - Ищем самого приоритетного СВОБОДНОГО бота
        - Если он не активен, активируем его
        - Деактивируем всех менее приоритетных СВОБОДНЫХ ботов

        ВАЖНО: НЕ требует блокировки, т.к. координатор работает в своём event loop
        и каждый координатор изолирован по символу.
        """
        # 🔍 ДИАГНОСТИКА только при проблемах (закомментировано для уменьшения спама)
        # log_debug(self.user_id, f"Активные боты: {list(self.active_bots)}", "Coordinator")

        # ШАГ 1: Проверяем более приоритетных ботов - не ждут ли они исполнения ордера
        # Если более приоритетный бот ждёт - НЕ активируем менее приоритетного
        for priority in [1, 2, 3]:
            bot_data = self.bots[priority]
            strategy = bot_data.strategy
            is_waiting = getattr(strategy, 'is_waiting_for_trade', False)

            if is_waiting:
                # Бот ждёт исполнения - не делаем ротацию
                return

        # ШАГ 2: Находим самого приоритетного СВОБОДНОГО И УЖЕ АКТИВНОГО бота
        # КРИТИЧНО: Ротация должна происходить только СРЕДИ АКТИВНЫХ ботов!
        most_priority_free_bot = None

        for priority in [1, 2, 3]:
            # ПРОВЕРЯЕМ ТОЛЬКО АКТИВНЫХ БОТОВ
            if priority not in self.active_bots:
                continue

            bot_data = self.bots[priority]
            strategy = bot_data.strategy

            is_really_free = not strategy.position_active

            # Бот считается СВОБОДНЫМ только если НЕ в позиции
            if is_really_free:
                most_priority_free_bot = priority
                break  # Нашли - останавливаемся

        # Если НЕТ свободных АКТИВНЫХ ботов - ничего не делаем
        if most_priority_free_bot is None:
            return

        # ШАГ 3: Самый приоритетный свободный бот уже активен (мы его нашли среди активных)
        # Просто деактивируем всех менее приоритетных свободных ботов

        # ШАГ 3: Деактивируем всех менее приоритетных СВОБОДНЫХ ботов
        for lower_priority in range(most_priority_free_bot + 1, 4):
            lower_bot = self.bots.get(lower_priority)
            if not lower_bot:
                continue

            # Проверяем что нижеприоритетный бот действительно свободен
            lower_is_really_free = not lower_bot.strategy.position_active
            lower_is_waiting = getattr(lower_bot.strategy, 'is_waiting_for_trade', False)

            # Деактивируем только если бот активен И свободен (не в позиции)
            if lower_priority in self.active_bots and lower_is_really_free and not lower_is_waiting:
                log_info(self.user_id,
                        f"🔵 Бот {most_priority_free_bot} ({self.symbol}) свободен → Деактивирую свободного Бота {lower_priority}",
                        "Coordinator")
                await self._deactivate_bot(lower_priority)

    async def _activate_bot(self, priority: int):
        """
        Активирует бота - запускает стратегию.

        После активации бот НАЧИНАЕТ обрабатывать события (свечи, цены).

        ВАЖНО: Защищено блокировкой для предотвращения race conditions.
        """
        async with self._lock:
            if priority in self.active_bots:
                return  # Уже активен

            log_info(self.user_id,
                    f"🟢 АКТИВАЦИЯ БОТА {priority} для {self.symbol}",
                    "Coordinator")

            strategy = self.bots[priority].strategy
            success = await strategy.start()

            if success:
                self.active_bots.add(priority)
            else:
                log_error(self.user_id,
                         f"❌ Не удалось активировать Бота {priority} для {self.symbol}",
                         "Coordinator")

    async def _deactivate_bot(self, priority: int):
        """
        Деактивирует бота - останавливает стратегию.

        После деактивации бот ПЕРЕСТАЁТ обрабатывать события.

        ВАЖНО: Защищено блокировкой для предотвращения race conditions.
        """
        async with self._lock:
            if priority not in self.active_bots:
                return  # Уже неактивен

            bot_data = self.bots[priority]

            # ЗАЩИТА: НЕ деактивируем если в позиции
            if bot_data.status != 'free':
                log_warning(self.user_id,
                           f"⚠️ Попытка деактивации Бота {priority} ({self.symbol}), но он в позиции! Пропускаю.",
                           "Coordinator")
                return

            log_info(self.user_id,
                    f"🔴 ДЕАКТИВАЦИЯ БОТА {priority} для {self.symbol}",
                    "Coordinator")

            strategy = bot_data.strategy
            await strategy.stop("Coordinator rotation")

            self.active_bots.discard(priority)

    async def can_bot_trade(self, priority: int) -> tuple[bool, str]:
        """
        Проверяет, может ли бот с данным приоритетом открывать НОВУЮ позицию.

        КРИТИЧНО: Вызывается ПЕРЕД КАЖДЫМ открытием позиции!

        Логика:
        - Бот 1 всегда может торговать
        - Бот 2 может торговать только если Бот 1 застрял (PnL < порога)
        - Бот 3 может торговать только если Боты 1 И 2 застряли

        Args:
            priority: Приоритет бота (1, 2 или 3)

        Returns:
            tuple[bool, str]: (можно ли торговать, причина если нельзя)
        """
        # Бот 1 всегда может торговать
        if priority == 1:
            return True, ""

        # Для ботов 2 и 3 проверяем состояние более приоритетных ботов
        # КРИТИЧНО: Обновляем статусы ПЕРЕД проверкой (получаем актуальный PnL)
        await self._update_statuses()

        # Проверяем ВСЕ более приоритетные боты
        for higher_priority in range(1, priority):
            higher_bot = self.bots[higher_priority]

            # Если более приоритетный бот свободен → текущий бот НЕ должен торговать
            if higher_bot.status == 'free':
                reason = f"Бот {higher_priority} свободен (нет позиции) → Бот {priority} НЕ должен открывать новые позиции"
                log_warning(self.user_id,
                           f"⛔ {reason}",
                           "Coordinator")
                return False, reason

            # Если более приоритетный бот НЕ застрял (active) → текущий бот НЕ должен торговать
            if higher_bot.status == 'active':
                pnl_percent = self._calculate_pnl_percent(higher_bot.strategy)
                reason = f"Бот {higher_priority} НЕ застрял (PnL={pnl_percent:.1f}%, статус={higher_bot.status}) → Бот {priority} НЕ должен открывать новые позиции"
                log_warning(self.user_id,
                           f"⛔ {reason}",
                           "Coordinator")
                return False, reason

        # Все более приоритетные боты застряли → можно торговать
        log_info(self.user_id,
                f"✅ Бот {priority} может торговать: все более приоритетные боты застряли",
                "Coordinator")
        return True, ""

    def _calculate_pnl_percent(self, strategy: SignalScalperStrategy) -> Decimal:
        """
        Рассчитывает PnL в процентах от маржи.

        Формула: (current_pnl / initial_margin) * 100

        Returns:
            Decimal: PnL в процентах (может быть отрицательным)
        """
        if not strategy.position_active or strategy.initial_margin_usd == 0:
            return Decimal('0')

        try:
            # КРИТИЧНО: Проверяем на None и конвертируем в Decimal
            avg_entry = strategy.average_entry_price
            base_entry = strategy.entry_price

            # Выбираем цену входа с защитой от None
            if avg_entry and avg_entry > 0:
                entry_price = Decimal(str(avg_entry))
            elif base_entry and base_entry > 0:
                entry_price = Decimal(str(base_entry))
            else:
                log_error(self.user_id, f"⚠️ Не удалось получить цену входа для расчета PnL% (avg={avg_entry}, base={base_entry})", "Coordinator")
                return Decimal('0')

            # Выбираем размер позиции с защитой от None
            total_size = strategy.total_position_size
            base_size = strategy.position_size

            if total_size and total_size > 0:
                position_size = Decimal(str(total_size))
            elif base_size and base_size > 0:
                position_size = Decimal(str(base_size))
            else:
                log_error(self.user_id, f"⚠️ Не удалось получить размер позиции для расчета PnL% (total={total_size}, base={base_size})", "Coordinator")
                return Decimal('0')

            # Получаем последнюю известную цену из стратегии
            last_price = getattr(strategy, '_last_known_price', None)

            if not last_price or last_price <= 0:
                # КРИТИЧЕСКАЯ ОШИБКА: Нет текущей цены для расчёта PnL
                # Это значит стратегия не получает обновления цен!
                log_error(self.user_id,
                         f"❌ КРИТИЧЕСКАЯ ОШИБКА: _last_known_price не установлена для {self.symbol}! "
                         f"Невозможно рассчитать PnL% для определения застревания. "
                         f"Проблема: стратегия не получает обновления цен через WebSocket. "
                         f"Возвращаю -100% для безопасной активации следующего бота.",
                         "Coordinator")
                # Возвращаем максимальный убыток для безопасности
                # Это заставит координатор активировать следующего бота
                return Decimal('-100')

            current_price = Decimal(str(last_price))

            # Расчет PnL
            if strategy.active_direction == "LONG":
                current_pnl = (current_price - entry_price) * position_size
            else:  # SHORT
                current_pnl = (entry_price - current_price) * position_size

            pnl_percent = (current_pnl / strategy.initial_margin_usd) * Decimal('100')

            return pnl_percent

        except Exception as e:
            log_error(self.user_id, f"Ошибка расчёта PnL% для бота: {e}", "Coordinator")
            return Decimal('0')

    async def get_aggregated_stats(self) -> Dict:
        """
        Возвращает агрегированную статистику по всем 3 ботам.

        Используется для отображения пользователю в Telegram.

        Returns:
            Dict с полями:
            - total_balance: Общий баланс по 3 аккаунтам
            - active_positions: Список активных позиций
            - active_bots: Список ID активных ботов
            - bots_status: Статус каждого бота
        """
        total_balance = Decimal('0')
        active_positions = []
        bots_status = {}

        for priority, bot_data in self.bots.items():
            strategy = bot_data.strategy

            # Получаем баланс с биржи
            try:
                balance = await strategy.api.get_wallet_balance()
                if balance:
                    total_balance += balance.get('totalEquity', Decimal('0'))
            except Exception as e:
                log_error(self.user_id,
                         f"Ошибка получения баланса для Бота {priority}: {e}",
                         "Coordinator")

            # Собираем информацию о статусе бота
            bots_status[priority] = {
                'status': bot_data.status,
                'active': priority in self.active_bots,
                'in_position': strategy.position_active
            }

            # Собираем активные позиции
            if strategy.position_active:
                pnl_percent = self._calculate_pnl_percent(strategy)

                active_positions.append({
                    'bot': priority,
                    'symbol': self.symbol,
                    'direction': strategy.active_direction,
                    'entry_price': strategy.average_entry_price if strategy.average_entry_price > 0 else strategy.entry_price,
                    'position_size': strategy.total_position_size if strategy.total_position_size > 0 else strategy.position_size,
                    'pnl_percent': float(pnl_percent),
                    'status': bot_data.status
                })

        return {
            'symbol': self.symbol,
            'total_balance': total_balance,
            'active_positions': active_positions,
            'active_bots': list(self.active_bots),
            'bots_status': bots_status
        }