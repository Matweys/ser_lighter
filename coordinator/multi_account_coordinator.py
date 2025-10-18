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

    # Константы (можно будет вынести в конфиг позже)
    STUCK_THRESHOLD_PERCENT = Decimal('-10')  # Застрял если PnL < -10% от маржи
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

    async def start(self):
        """Запуск координатора"""
        if self.running:
            log_warning(self.user_id, f"Coordinator для {self.symbol} уже запущен", "Coordinator")
            return

        self.running = True
        log_info(self.user_id, f"🟢 Запуск Coordinator для {self.symbol}", "Coordinator")

        # Активируем ТОЛЬКО Бот 1 (PRIMARY) при старте
        await self._activate_bot(1)

        # Запускаем фоновый мониторинг
        asyncio.create_task(self._monitor_loop())

    async def stop(self):
        """Остановка координатора"""
        if not self.running:
            return

        self.running = False
        log_info(self.user_id, f"🔴 Остановка Coordinator для {self.symbol}", "Coordinator")

        # Останавливаем все активные боты
        for priority in list(self.active_bots):
            await self._deactivate_bot(priority)

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

            try:
                # ШАГ 1: Обновляем статусы всех ботов
                await self._update_statuses()

                # ШАГ 2: Проверяем необходимость активации
                await self._check_activation_needed()

                # ШАГ 3: Проверяем необходимость деактивации
                await self._check_deactivation_needed()

            except Exception as e:
                log_error(self.user_id, f"Ошибка в monitor_loop для {self.symbol}: {e}", "Coordinator")

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

                if pnl_percent < self.STUCK_THRESHOLD_PERCENT:
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
        """
        for priority in [1, 2]:  # Проверяем Бот 1 и Бот 2
            bot_data = self.bots[priority]
            next_priority = priority + 1

            if bot_data.status == 'stuck' and next_priority not in self.active_bots:
                log_warning(self.user_id,
                           f"🟡 Бот {priority} ({self.symbol}) застрял → Активирую Бот {next_priority}",
                           "Coordinator")
                await self._activate_bot(next_priority)

    async def _check_deactivation_needed(self):
        """
        Логика деактивации бота.

        ДЕАКТИВИРУЕМ Бот N если:
        - Существует более приоритетный бот M (M < N)
        - Бот M свободен (status='free')
        - Бот N свободен (status='free')

        ВАЖНО: НЕ деактивируем если Бот N в позиции!
        """
        # Проверяем от самого приоритетного к менее приоритетным
        for priority in [1, 2, 3]:
            bot_data = self.bots[priority]
            strategy = bot_data.strategy

            # КРИТИЧНО: Проверяем РЕАЛЬНОЕ состояние позиции, не полагаемся только на status
            # Статус обновляется каждые 5 секунд, но позиция может открыться между обновлениями
            is_really_free = not strategy.position_active

            if is_really_free:
                # Этот бот свободен - деактивируем менее приоритетных СВОБОДНЫХ
                for lower_priority in range(priority + 1, 4):
                    lower_bot = self.bots.get(lower_priority)
                    if not lower_bot:
                        continue

                    # Также проверяем реальное состояние для нижеприоритетных ботов
                    lower_is_really_free = not lower_bot.strategy.position_active

                    if lower_is_really_free and lower_priority in self.active_bots:
                        log_info(self.user_id,
                                f"🔵 Бот {priority} ({self.symbol}) свободен → Деактивирую свободного Бота {lower_priority}",
                                "Coordinator")
                        await self._deactivate_bot(lower_priority)

                # Активируем этот бот если он не активен И действительно свободен
                if priority not in self.active_bots:
                    log_info(self.user_id,
                            f"🟢 Возвращаю Бота {priority} ({self.symbol}) как приоритетного",
                            "Coordinator")
                    await self._activate_bot(priority)

                break  # Нашли самого приоритетного свободного - останавливаемся

    async def _activate_bot(self, priority: int):
        """
        Активирует бота - запускает стратегию.

        После активации бот НАЧИНАЕТ обрабатывать события (свечи, цены).
        """
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

        ВАЖНО: Вызывается ТОЛЬКО если бот свободен (НЕ в позиции)!
        """
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
            # Используем данные из стратегии
            entry_price = strategy.average_entry_price if strategy.average_entry_price > 0 else strategy.entry_price
            position_size = strategy.total_position_size if strategy.total_position_size > 0 else strategy.position_size

            # Получаем последнюю известную цену из стратегии
            # ВАЖНО: Нужно будет добавить это поле в SignalScalperStrategy
            current_price = getattr(strategy, '_last_known_price', entry_price)

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