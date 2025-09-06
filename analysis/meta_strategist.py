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
from core.logger import log_info, log_error
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
        if not symbol:
            return

        now = datetime.now()
        last_analysis = self.last_analysis_time.get(symbol)
        if last_analysis and (now - last_analysis) < self.analysis_cooldown:
            return

        try:
            # Проверяем, включена ли стратегия impulse_trailing у данного пользователя
            # Для этого нам нужно загрузить конфиг именно этой стратегии
            impulse_config = await redis_manager.get_config(self.user_id, ConfigType.STRATEGY_IMPULSE_TRAILING)
            if not impulse_config or not impulse_config.get("is_enabled", False):
                return  # Если стратегия выключена, просто игнорируем событие

            log_debug(self.user_id, f"Анализ рынка для {symbol} по глобальной свече", module_name=__name__)

            analysis_timeframes = self.user_config.get("analysis_timeframes", ["15m", "1h", "4h"])
            analysis = await self.analyzer.get_market_analysis(symbol, timeframes=analysis_timeframes)

            self.last_analysis_time[symbol] = now

            strategy_decision = await self._make_strategy_decision(analysis)

            if strategy_decision:
                analysis_dict = analysis.to_dict()

                # Создаем ПЕРСОНАЛЬНЫЙ сигнал для конкретного пользователя
                signal_event = SignalEvent(
                    user_id=self.user_id,
                    symbol=symbol,
                    strategy_type=strategy_decision["strategy_type"],
                    signal_strength=analysis.strength,
                    analysis_data=analysis_dict
                )

                await self.event_bus.publish(signal_event)
                log_info(self.user_id,
                         f"Сигнал отправлен: {strategy_decision['strategy_type']} для {symbol} (сила: {analysis.strength})",
                         module_name=__name__)
        except Exception as e:
            log_error(self.user_id, f"Ошибка обработки глобальной свечи {symbol}: {e}", module_name=__name__)
            
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


    async def _make_strategy_decision(self, analysis: 'MarketAnalysis') -> Optional[Dict[str, str]]:
        """
        Принятие решения о запуске стратегии на основе анализа.
        Генерирует сигнал только для impulse_trailing при наличии сильного тренда.
        """
        try:
            regime = analysis.regime.value if hasattr(analysis.regime, 'value') else analysis.regime
            signal_strength = analysis.strength

            # Получаем минимальную силу сигнала из конфигурации пользователя
            min_signal_strength = self.user_config.get("min_signal_strength", 70)

            if signal_strength < min_signal_strength:
                return None

            # Генерируем сигнал только для импульсной стратегии при сильном тренде
            if regime in ['STRONG_TREND', 'TREND']:
                return {
                    "strategy_type": "impulse_trailing",
                    "reason": f"Обнаружен трендовый рынок, сила сигнала: {signal_strength}"
                }

            return None

        except Exception as e:
            log_error(self.user_id, f"Ошибка принятия решения о стратегии: {e}", module_name=__name__)
            return None
