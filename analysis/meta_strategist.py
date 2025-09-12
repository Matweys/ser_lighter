# core/meta_strategist.py
"""
Профессиональный анализатор рынка и стратег для многопользовательской системы
Реализует многотаймфреймовый анализ с динамическими настройками из Redis
"""
import asyncio
import pandas as pd
import pandas_ta as ta
from decimal import Decimal, getcontext
from typing import Dict, Any, Optional, List
from datetime import datetime, timedelta
from api.bybit_api import BybitAPI
from core.logger import log_info, log_error, log_debug, log_warning
from core.events import EventType, SignalEvent, UserSettingsChangedEvent, EventBus, GlobalCandleEvent
from cache.redis_manager import redis_manager, ConfigType

# Настройка точности для Decimal
getcontext().prec = 28


class MetaStrategist:
    """
    Персональный стратег для пользователя
    Анализирует рынок и принимает решения о запуске стратегий на основе событий
    """

    def __init__(self, user_id: int, analyzer: 'MarketAnalyzer', event_bus: EventBus):
        self.user_id = user_id
        self.analyzer = analyzer
        self.event_bus = event_bus
        self.running = False

        # Кэш для предотвращения спама анализа
        self.last_analysis_time: Dict[str, datetime] = {}
        self.analysis_cooldown = timedelta(minutes=5)  # Минимальный интервал между анализами

        # Настройки пользователя
        self.user_config: Optional[Dict] = None

    async def start(self):
        """Запуск MetaStrategist"""
        if self.running:
            return
        log_info(self.user_id, "Запуск MetaStrategist...", module_name=__name__)
        try:
            await self._load_user_config()

            # 2. Меняем подписку с NewCandleEvent на GlobalCandleEvent
            self.event_bus.subscribe(EventType.GLOBAL_CANDLE, self._handle_global_candle)
            self.event_bus.subscribe(EventType.USER_SETTINGS_CHANGED, self._handle_settings_changed)

            self.running = True
            log_info(self.user_id, "MetaStrategist запущен", module_name=__name__)
        except Exception as e:
            log_error(self.user_id, f"Ошибка запуска MetaStrategist: {e}", module_name=__name__)
            raise

    async def stop(self):
        """Остановка MetaStrategist"""
        if not self.running:
            return

        self.running = False
        log_info(self.user_id, "MetaStrategist остановлен", module_name=__name__)

    async def on_settings_changed(self, event: UserSettingsChangedEvent):
        """
        Публичный метод для обработки события изменения настроек.
        Вызывает внутренний защищенный обработчик.
        """
        await self._handle_settings_changed(event)

    async def _handle_global_candle(self, event: GlobalCandleEvent):
        """Обработчик глобального события о новой свече от ImpulseScanner."""
        symbol = event.candle_data.get("symbol")
        if not symbol: return

        # --- ДОБАВЛЕН ФИЛЬТР СРОЧНЫХ КОНТРАКТОВ ---
        if '-' in symbol:
            return # Немедленно выходим, если это срочный контракт

        now = datetime.now()
        last_analysis = self.last_analysis_time.get(symbol)
        if last_analysis and (now - last_analysis) < self.analysis_cooldown:
            return

        try:
            impulse_config = await redis_manager.get_config(self.user_id, ConfigType.STRATEGY_IMPULSE_TRAILING)
            if not impulse_config or not impulse_config.get("is_enabled", False):
                return

            # --- Анализируем только один, нужный нам таймфрейм или подмена динамически---
            analysis_timeframe = impulse_config.get("analysis_timeframe", "5m")
            analysis = await self.analyzer.get_market_analysis(symbol, timeframe=analysis_timeframe)
            log_info(self.user_id,
                     f"[TRACE] Получен analysis для {symbol}: {type(analysis)}, is None: {analysis is None}",
                     "meta_strategist")
            if analysis:
                log_info(self.user_id, f"[TRACE] analysis.to_dict() для {symbol}: {analysis.to_dict()}",
                         "meta_strategist")

            self.last_analysis_time[symbol] = now

            # --- Решение принимается напрямую на основе анализа ---
            if analysis and (analysis.is_panic_bar or (analysis.ema_trend == "UP" and analysis.is_consolidating_now)):
                # analysis_dict больше не нужен для события
                # log_info можно убрать или оставить для отладки
                log_info(self.user_id,
                         f"Условия для сигнала 'impulse_trailing' для {symbol} выполнены. Отправка триггера.",
                         "meta_strategist")

                signal_event = SignalEvent(
                    user_id=self.user_id,
                    symbol=symbol,
                    strategy_type="impulse_trailing",
                    signal_strength=100
                )
                await self.event_bus.publish(signal_event)
                log_info(self.user_id, f"Сигнал 'impulse_trailing' отправлен для {symbol}", "meta_strategist")
            else:
                if not analysis:
                    log_warning(self.user_id, f"[TRACE] Нет анализа для {symbol}", "meta_strategist")
                else:
                    log_info(self.user_id,
                             f"[TRACE] Условия сигнала не выполнены для {symbol}: panic={analysis.is_panic_bar}, trend={analysis.ema_trend}, consol={analysis.is_consolidating_now}",
                             "meta_strategist")
        except Exception as e:
            log_error(self.user_id, f"Ошибка обработки глобальной свечи {symbol}: {e}", "meta_strategist")

    async def _handle_settings_changed(self, event: UserSettingsChangedEvent):
        """Обработчик изменения настроек пользователя"""
        if event.user_id != self.user_id:
            return

        log_info(self.user_id, "Перезагрузка конфигурации после изменения настроек", module_name=__name__)
        await self._load_user_config()

    async def _load_user_config(self):
        """Загрузка конфигурации пользователя"""
        try:
            self.user_config = await redis_manager.get_config(self.user_id, ConfigType.GLOBAL)
            if not self.user_config:
                log_error(self.user_id, "Конфигурация пользователя не найдена", module_name=__name__)
                raise ValueError("Конфигурация пользователя не найдена")
        except Exception as e:
            log_error(self.user_id, f"Ошибка загрузки конфигурации: {e}", module_name=__name__)
            raise
