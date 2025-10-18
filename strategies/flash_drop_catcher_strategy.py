# strategies/flash_drop_catcher_strategy.py
"""
🚀 Flash Drop Catcher Strategy - Стратегия ловли резких падений
Обнаруживает резкие падения цены и открывает LONG позиции для отскока
"""
import asyncio
import json
import websockets
from decimal import Decimal, getcontext
from typing import Dict, Any, Optional, List
from datetime import datetime
from collections import defaultdict, deque
from aiogram import Bot
from core.logger import log_info, log_error, log_warning, log_debug
from core.enums import StrategyType, PositionSide
from core.events import EventType, EventBus, PriceUpdateEvent
from api.bybit_api import BybitAPI
from .base_strategy import BaseStrategy
from aiogram.utils.markdown import hbold, hcode

# Настройка точности для Decimal
getcontext().prec = 28


class FlashDropCatcherStrategy(BaseStrategy):
    """
    Стратегия для обнаружения резких падений и входа в LONG позиции.

    Логика работы:
    1. Сканирует все фьючерсные символы через WebSocket
    2. Обнаруживает резкое падение (сравнивая с 5-свечным средним)
    3. Входит в LONG на отскок
    4. Выходит по trailing stop или при достижении -3$ убытка
    """

    def __init__(self, user_id: int, symbol: str, signal_data: Dict[str, Any],
                 api: BybitAPI, event_bus: EventBus, bot: Bot, config: Optional[Dict] = None):
        """Инициализация стратегии Flash Drop Catcher"""
        super().__init__(user_id, symbol, signal_data, api, event_bus, bot, config)

        # === ОРИГИНАЛЬНАЯ ЛОГИКА СКАНЕРА (НЕ ТРОГАТЬ!) ===
        # Параметры для обнаружения падений
        self.DROP_PCT = Decimal('0.02')  # 2% падение от среднего
        self.CANDLE_HISTORY_SIZE = 5  # Сколько свечей учитывать для среднего

        # Хранение исторических данных по свечам для каждого символа
        self.candle_data: Dict[str, deque] = defaultdict(lambda: deque(maxlen=self.CANDLE_HISTORY_SIZE))

        # WebSocket задача
        self._scanner_task: Optional[asyncio.Task] = None
        self._ws_url = "wss://stream.bybit.com/v5/public/linear"

        # === ПАРАМЕТРЫ ТОРГОВЛИ ===
        # Ограничение одновременных позиций
        self.MAX_CONCURRENT_POSITIONS = 2  # Максимум 2 позиции одновременно

        # Параметры позиции
        self.position_active = False
        self.entry_price: Decimal = Decimal('0')
        self.position_size: Decimal = Decimal('0')
        self.active_direction = "LONG"

        # Trailing stop параметры (копируются из signal_scalper)
        self.highest_pnl = Decimal('0')
        self.current_trailing_level = 0
        self.last_trailing_notification_level = -1

        # Hard stop loss при -15$
        self.HARD_STOP_LOSS_USDT = Decimal('-15.0')

        # Мониторинг позиции
        self._position_monitor_task: Optional[asyncio.Task] = None

        log_info(self.user_id,
                f"🚀 FlashDropCatcher инициализирована для {self.symbol}",
                "FlashDropCatcher")

    def _get_strategy_type(self) -> StrategyType:
        """Возвращает тип стратегии"""
        return StrategyType.FLASH_DROP_CATCHER

    async def start(self):
        """Запуск стратегии"""
        if self.is_running:
            log_warning(self.user_id, "FlashDropCatcher уже запущена", "FlashDropCatcher")
            return

        self.is_running = True

        # Загружаем конфигурацию
        await self._load_config()

        # Подписываемся на события обновления цен
        await self.event_bus.subscribe(EventType.PRICE_UPDATE, self.handle_price_update, user_id=self.user_id)

        # Запускаем WebSocket сканер
        self._scanner_task = asyncio.create_task(self._run_websocket_scanner())

        log_info(self.user_id,
                f"✅ FlashDropCatcher запущена! Сканирование всех символов на падения...",
                "FlashDropCatcher")

    async def stop(self, reason: str = "Manual stop") -> bool:
        """Остановка стратегии"""
        if not self.is_running:
            return True

        self.is_running = False

        # Останавливаем WebSocket сканер
        if self._scanner_task and not self._scanner_task.done():
            self._scanner_task.cancel()
            try:
                await self._scanner_task
            except asyncio.CancelledError:
                pass

        # Останавливаем мониторинг позиции
        if self._position_monitor_task and not self._position_monitor_task.done():
            self._position_monitor_task.cancel()
            try:
                await self._position_monitor_task
            except asyncio.CancelledError:
                pass

        # Отписываемся от событий
        await self.event_bus.unsubscribe(self.handle_price_update)

        log_info(self.user_id, f"⏹️ FlashDropCatcher остановлена: {reason}", "FlashDropCatcher")
        return True

    # ============================================================================
    # === ОРИГИНАЛЬНАЯ ЛОГИКА WEBSOCKET СКАНЕРА (НЕ ТРОГАТЬ!) ===
    # ============================================================================

    async def _run_websocket_scanner(self):
        """
        ОРИГИНАЛЬНАЯ ЛОГИКА СКАНЕРА - НЕ ИЗМЕНЯТЬ!
        Подключается к WebSocket и мониторит все символы на резкие падения
        """
        while self.is_running:
            try:
                log_info(self.user_id, "🔌 Подключение к WebSocket сканеру...", "FlashDropCatcher")

                async with websockets.connect(self._ws_url) as ws:
                    # Получаем список всех фьючерсных символов
                    symbols = await self._get_all_futures_symbols()

                    if not symbols:
                        log_error(self.user_id, "Не удалось получить список символов", "FlashDropCatcher")
                        await asyncio.sleep(10)
                        continue

                    # Подписываемся на kline.1 (1-минутные свечи) для всех символов
                    subscribe_message = {
                        "op": "subscribe",
                        "args": [f"kline.1.{symbol}" for symbol in symbols]
                    }

                    await ws.send(json.dumps(subscribe_message))
                    log_info(self.user_id,
                            f"✅ Подписка на {len(symbols)} символов для мониторинга падений",
                            "FlashDropCatcher")

                    # Обрабатываем входящие сообщения
                    async for message in ws:
                        if not self.is_running:
                            break

                        await self._process_websocket_message(message)

            except asyncio.CancelledError:
                log_info(self.user_id, "WebSocket сканер отменен", "FlashDropCatcher")
                break
            except Exception as e:
                log_error(self.user_id, f"Ошибка WebSocket сканера: {e}", "FlashDropCatcher")
                if self.is_running:
                    log_info(self.user_id, "Переподключение через 5 секунд...", "FlashDropCatcher")
                    await asyncio.sleep(5)

    async def _get_all_futures_symbols(self) -> List[str]:
        """
        ОРИГИНАЛЬНАЯ ЛОГИКА - НЕ ИЗМЕНЯТЬ!
        Получает список всех доступных фьючерсных символов
        """
        try:
            response = await self.api.get_instruments_info()

            if response and "result" in response and "list" in response["result"]:
                symbols = [item["symbol"] for item in response["result"]["list"]
                          if item.get("status") == "Trading"]
                log_info(self.user_id,
                        f"📊 Получено {len(symbols)} торгуемых символов",
                        "FlashDropCatcher")
                return symbols

            return []

        except Exception as e:
            log_error(self.user_id, f"Ошибка получения списка символов: {e}", "FlashDropCatcher")
            return []

    async def _process_websocket_message(self, message: str):
        """
        ОРИГИНАЛЬНАЯ ЛОГИКА - НЕ ИЗМЕНЯТЬ!
        Обрабатывает сообщения WebSocket и обнаруживает падения
        """
        try:
            data = json.loads(message)

            # Проверяем, что это обновление свечи
            if data.get("topic", "").startswith("kline.1."):
                kline_data = data.get("data", [])

                if not kline_data:
                    return

                for candle in kline_data:
                    symbol = data["topic"].split(".")[-1]

                    # Проверяем, что свеча закрыта (confirm=True)
                    if not candle.get("confirm", False):
                        continue

                    close_price = Decimal(str(candle["close"]))

                    # Добавляем свечу в историю
                    self.candle_data[symbol].append(close_price)

                    # Проверяем падение только если накопили достаточно истории
                    if len(self.candle_data[symbol]) >= self.CANDLE_HISTORY_SIZE:
                        await self._check_for_drop(symbol, close_price)

        except json.JSONDecodeError:
            pass  # Игнорируем некорректные JSON
        except Exception as e:
            log_error(self.user_id, f"Ошибка обработки WebSocket сообщения: {e}", "FlashDropCatcher")

    async def _check_for_drop(self, symbol: str, current_close: Decimal):
        """
        ОРИГИНАЛЬНАЯ ЛОГИКА - НЕ ИЗМЕНЯТЬ!
        Проверяет, произошло ли резкое падение для символа
        """
        try:
            # Вычисляем среднюю цену закрытия за последние N свечей (без текущей)
            history = list(self.candle_data[symbol])
            if len(history) < 2:
                return

            avg_close = sum(history[:-1]) / len(history[:-1])

            # Рассчитываем процент падения
            drop_pct = (avg_close - current_close) / avg_close

            # Если падение превышает порог - генерируем сигнал
            if drop_pct >= self.DROP_PCT:
                await self._handle_drop_signal(symbol, current_close, drop_pct)

        except Exception as e:
            log_error(self.user_id, f"Ошибка проверки падения для {symbol}: {e}", "FlashDropCatcher")

    # ============================================================================
    # === ТОРГОВАЯ ЛОГИКА (ИНТЕГРАЦИЯ) ===
    # ============================================================================

    async def _handle_drop_signal(self, symbol: str, price: Decimal, drop_pct: Decimal):
        """
        Обрабатывает сигнал резкого падения.
        Открывает LONG позицию если нет активной позиции.
        """
        try:
            # Проверка 1: Подсчитываем количество открытых позиций на бирже
            all_positions = await self.api.get_positions()
            open_positions_count = 0

            if all_positions and "result" in all_positions and "list" in all_positions["result"]:
                for pos in all_positions["result"]["list"]:
                    position_size = float(pos.get("size", 0))
                    if position_size > 0:
                        open_positions_count += 1
                        # Проверяем, есть ли уже позиция на этот символ
                        if pos["symbol"] == symbol:
                            log_warning(self.user_id,
                                       f"⚠️ Пропускаем сигнал {symbol} - уже есть открытая позиция на этот символ! Размер: {pos.get('size')}",
                                       "FlashDropCatcher")
                            return

            # Проверка 2: Достигнут ли лимит одновременных позиций
            if open_positions_count >= self.MAX_CONCURRENT_POSITIONS:
                log_warning(self.user_id,
                           f"⚠️ Пропускаем сигнал {symbol} - достигнут лимит одновременных позиций ({open_positions_count}/{self.MAX_CONCURRENT_POSITIONS})",
                           "FlashDropCatcher")
                return

            # Проверяем, что это наш символ (если стратегия работает для конкретного символа)
            if self.symbol != "ALL" and symbol != self.symbol:
                return

            # Генерируем сообщение сигнала
            drop_percent = drop_pct * 100
            signal_message = f"🔔 РЕЗКОЕ ПАДЕНИЕ: {symbol} Последняя цена: {price:.8f} (падение {drop_percent:.2f}%)"

            log_warning(self.user_id, signal_message, "FlashDropCatcher")

            # Отправляем уведомление пользователю
            if self.bot:
                await self.bot.send_message(
                    self.user_id,
                    f"{hbold('🔔 СИГНАЛ ПАДЕНИЯ')}\n\n"
                    f"Символ: {hcode(symbol)}\n"
                    f"Цена: {hcode(f'{price:.8f}')}\n"
                    f"Падение: {hcode(f'{drop_percent:.2f}%')}\n\n"
                    f"Открываем LONG позицию..."
                )

            # Обновляем символ для этой сделки
            self.symbol = symbol

            # Открываем LONG позицию
            await self._open_long_position(price)

        except Exception as e:
            log_error(self.user_id, f"Ошибка обработки сигнала падения: {e}", "FlashDropCatcher")

    async def _open_long_position(self, entry_price: Decimal):
        """Открывает LONG позицию по текущей цене"""
        try:
            # Получаем параметры из конфигурации
            order_amount = self._convert_to_decimal(self.get_config_value("order_amount", 200.0))
            leverage = int(self.get_config_value("leverage", 3))

            # Устанавливаем плечо
            await self.api.set_leverage(symbol=self.symbol, leverage=leverage)

            # Получаем информацию о символе для правильного размера позиции
            instruments = await self.api.get_instruments_info(symbol=self.symbol)
            if not instruments or "result" not in instruments:
                log_error(self.user_id, "Не удалось получить информацию о символе", "FlashDropCatcher")
                return

            instrument = instruments["result"]["list"][0]
            qty_step = Decimal(str(instrument.get("lotSizeFilter", {}).get("qtyStep", "0.001")))

            # Рассчитываем размер позиции
            position_size = (order_amount * leverage) / entry_price
            position_size = (position_size // qty_step) * qty_step

            # Открываем LONG позицию
            log_info(self.user_id,
                    f"📈 Открываем LONG позицию: {self.symbol}, размер: {position_size}, плечо: {leverage}x",
                    "FlashDropCatcher")

            order_result = await self.api.place_order(
                symbol=self.symbol,
                side="Buy",
                order_type="Market",
                qty=Decimal(str(position_size))
            )

            if order_result and "result" in order_result:
                self.position_active = True
                self.entry_price = entry_price
                self.position_size = position_size
                self.active_direction = "LONG"

                # Сбрасываем trailing stop параметры
                self.highest_pnl = Decimal('0')
                self.current_trailing_level = 0
                self.last_trailing_notification_level = -1

                # Запускаем мониторинг позиции
                self._position_monitor_task = asyncio.create_task(self._monitor_position())

                log_info(self.user_id,
                        f"✅ LONG позиция открыта по цене {entry_price}",
                        "FlashDropCatcher")

                # Уведомление
                if self.bot:
                    await self.bot.send_message(
                        self.user_id,
                        f"{hbold('✅ ПОЗИЦИЯ ОТКРЫТА')}\n\n"
                        f"Символ: {hcode(self.symbol)}\n"
                        f"Направление: {hcode('LONG')}\n"
                        f"Цена входа: {hcode(f'{entry_price:.8f}')}\n"
                        f"Размер: {hcode(f'{position_size:.6f}')}\n"
                        f"Плечо: {hcode(f'{leverage}x')}\n"
                        f"Сумма: {hcode(f'${order_amount:.2f}')}"
                    )
            else:
                log_error(self.user_id, "Не удалось открыть позицию", "FlashDropCatcher")

        except Exception as e:
            log_error(self.user_id, f"Ошибка открытия LONG позиции: {e}", "FlashDropCatcher")

    async def _monitor_position(self):
        """Мониторит активную позицию для выхода по trailing stop или stop loss"""
        try:
            log_info(self.user_id, "🎯 Запущен мониторинг позиции", "FlashDropCatcher")

            while self.is_running and self.position_active:
                await asyncio.sleep(1)  # Проверка каждую секунду

        except asyncio.CancelledError:
            log_info(self.user_id, "Мониторинг позиции отменен", "FlashDropCatcher")
        except Exception as e:
            log_error(self.user_id, f"Ошибка мониторинга позиции: {e}", "FlashDropCatcher")

    async def handle_price_update(self, event: PriceUpdateEvent):
        """Обработчик обновлений цены для активной позиции"""
        if not self.position_active or event.symbol != self.symbol:
            return

        try:
            current_price = self._convert_to_decimal(event.price)

            # Рассчитываем текущий PnL
            current_pnl = await self._calculate_current_pnl(current_price)

            # Проверка 1: Hard stop loss при -3$
            if current_pnl <= self.HARD_STOP_LOSS_USDT:
                log_warning(self.user_id,
                           f"🛑 HARD STOP LOSS! PnL={current_pnl:.2f}$ достиг -3$",
                           "FlashDropCatcher")
                await self._close_position("hard_stop_loss")
                return

            # Проверка 2: Trailing stop в прибыли
            if current_pnl > Decimal('0'):
                await self._check_trailing_stop(current_pnl)

        except Exception as e:
            log_error(self.user_id, f"Ошибка обработки обновления цены: {e}", "FlashDropCatcher")

    # ============================================================================
    # === TRAILING STOP ЛОГИКА (СКОПИРОВАНО ИЗ SIGNAL_SCALPER) ===
    # ============================================================================

    def _calculate_dynamic_levels(self) -> Dict[int, Decimal]:
        """
        Динамическая система трейлинга (копия из signal_scalper_strategy.py)
        Автоматически подстраивается под размер депозита и плечо
        """
        order_amount = max(self._convert_to_decimal(self.get_config_value("order_amount", 200.0)), Decimal('10.0'))
        leverage = self._convert_to_decimal(self.get_config_value("leverage", 2.0))

        # Номинальная стоимость позиции
        notional_value = order_amount * leverage

        # Проценты для уровней трейлинга
        level_percentages = {
            1: Decimal('0.0035'),   # 0.20% - МГНОВЕННЫЙ
            2: Decimal('0.0065'),   # 0.45% - РАННИЙ
            3: Decimal('0.0095'),   # 0.85% - СРЕДНИЙ
            4: Decimal('0.0145'),   # 1.30% - ХОРОШИЙ
            5: Decimal('0.0195'),   # 1.85% - ОТЛИЧНЫЙ
            6: Decimal('0.0350')    # 2.50% - МАКСИМАЛЬНЫЙ
        }

        # Рассчитываем пороги в USDT
        levels = {0: Decimal('0')}

        for level, percentage in level_percentages.items():
            levels[level] = notional_value * percentage

        return levels

    def _get_trailing_level(self, current_pnl: Decimal) -> int:
        """
        Определяет текущий уровень трейлинга (копия из signal_scalper_strategy.py)
        """
        levels = self._calculate_dynamic_levels()

        if current_pnl < levels[1]:
            return 0
        elif current_pnl < levels[2]:
            return 1
        elif current_pnl < levels[3]:
            return 2
        elif current_pnl < levels[4]:
            return 3
        elif current_pnl < levels[5]:
            return 4
        elif current_pnl < levels[6]:
            return 5
        else:
            return 6

    def _get_level_name(self, level: int) -> str:
        """Возвращает название уровня"""
        if level == 0:
            return "ОЖИДАНИЕ"

        levels = self._calculate_dynamic_levels()

        level_names = {
            1: f"МГНОВЕННЫЙ УРОВЕНЬ (${levels[1]:.2f}+, 0.35%)",
            2: f"РАННИЙ УРОВЕНЬ (${levels[2]:.2f}+, 0.65%)",
            3: f"СРЕДНИЙ УРОВЕНЬ (${levels[3]:.2f}+, 0.95%)",
            4: f"ХОРОШИЙ УРОВЕНЬ (${levels[4]:.2f}+, 1.45%)",
            5: f"ОТЛИЧНЫЙ УРОВЕНЬ (${levels[5]:.2f}+, 1.95%)",
            6: f"МАКСИМАЛЬНЫЙ УРОВЕНЬ (${levels[6]:.2f}+, 2.50%)"
        }
        return level_names.get(level, "НЕИЗВЕСТНЫЙ УРОВЕНЬ")

    async def _check_trailing_stop(self, current_pnl: Decimal):
        """
        Проверяет условия trailing stop (адаптировано из signal_scalper_strategy.py)
        """
        # Обновляем максимальный PnL
        if current_pnl > self.highest_pnl:
            self.highest_pnl = current_pnl

            # Определяем новый уровень
            new_level = self._get_trailing_level(current_pnl)

            if new_level > self.current_trailing_level:
                self.current_trailing_level = new_level

                # Уведомление о новом уровне
                if new_level != self.last_trailing_notification_level:
                    log_info(self.user_id,
                            f"📈 Новый уровень трейлинга: {self._get_level_name(new_level)}, PnL=${current_pnl:.2f}",
                            "FlashDropCatcher")
                    self.last_trailing_notification_level = new_level

        # Проверяем откат для закрытия (25% от максимума)
        if self.current_trailing_level > 0:
            # Откат 25%
            pullback_threshold = self.highest_pnl * Decimal('0.75')

            if current_pnl <= pullback_threshold:
                log_warning(self.user_id,
                           f"💰 TRAILING STOP! Откат 25% от максимума. Max PnL=${self.highest_pnl:.2f}, Current=${current_pnl:.2f}",
                           "FlashDropCatcher")
                await self._close_position("trailing_stop_profit")

    async def _calculate_current_pnl(self, current_price: Decimal) -> Decimal:
        """Рассчитывает текущий PnL позиции"""
        if not self.position_active or not self.entry_price:
            return Decimal('0')

        # LONG позиция: PnL = (текущая_цена - цена_входа) × размер
        pnl = (current_price - self.entry_price) * self.position_size

        return pnl

    async def _close_position(self, reason: str):
        """Закрывает текущую позицию"""
        if not self.position_active:
            return

        try:
            log_info(self.user_id,
                    f"🔄 Закрытие позиции: {self.symbol}, причина: {reason}",
                    "FlashDropCatcher")

            # Закрываем позицию на бирже
            close_result = await self.api.place_order(
                symbol=self.symbol,
                side="Sell",  # Закрываем LONG через Sell
                order_type="Market",
                qty=Decimal(str(self.position_size)),
                reduce_only=True
            )

            if close_result and "result" in close_result:
                # Получаем финальный PnL из позиции
                positions = await self.api.get_positions(symbol=self.symbol)
                final_pnl = Decimal('0')

                if positions and "result" in positions and "list" in positions["result"]:
                    for pos in positions["result"]["list"]:
                        if pos["symbol"] == self.symbol:
                            final_pnl = self._convert_to_decimal(pos.get("unrealisedPnl", 0))
                            break

                self.position_active = False

                log_info(self.user_id,
                        f"✅ Позиция закрыта. Итоговый PnL: ${final_pnl:.2f}",
                        "FlashDropCatcher")

                # Уведомление
                if self.bot:
                    reason_emoji = "💰" if "profit" in reason else "🛑"
                    await self.bot.send_message(
                        self.user_id,
                        f"{hbold(f'{reason_emoji} ПОЗИЦИЯ ЗАКРЫТА')}\n\n"
                        f"Символ: {hcode(self.symbol)}\n"
                        f"Причина: {hcode(reason)}\n"
                        f"PnL: {hcode(f'${final_pnl:.2f}')}"
                    )

                # Сбрасываем параметры
                self.entry_price = Decimal('0')
                self.position_size = Decimal('0')
                self.highest_pnl = Decimal('0')
                self.current_trailing_level = 0
                self.last_trailing_notification_level = -1

            else:
                log_error(self.user_id, "Не удалось закрыть позицию", "FlashDropCatcher")

        except Exception as e:
            log_error(self.user_id, f"Ошибка закрытия позиции: {e}", "FlashDropCatcher")

    # ============================================================================
    # === ВСПОМОГАТЕЛЬНЫЕ МЕТОДЫ ===
    # ============================================================================

    def _convert_to_decimal(self, value: Any) -> Decimal:
        """Безопасное преобразование в Decimal"""
        if isinstance(value, Decimal):
            return value
        elif isinstance(value, (int, float)):
            return Decimal(str(value))
        elif isinstance(value, str):
            try:
                return Decimal(value)
            except (ValueError, TypeError, ArithmeticError):
                return Decimal('0')
        return Decimal('0')

    async def _execute_strategy_logic(self):
        """Базовый метод выполнения логики (не используется в этой стратегии)"""
        pass

    async def _handle_order_filled(self, event):
        """Обработка исполнения ордера (не используется в этой стратегии)"""
        pass