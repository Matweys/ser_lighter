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
from core.events import EventType, NewCandleEvent, SignalEvent, UserSettingsChangedEvent, EventBus
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
            # Загрузка конфигурации пользователя
            await self._load_user_config()
            
            # Подписка на события
            self.event_bus.subscribe(EventType.NEW_CANDLE, self._handle_new_candle)
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


    async def _handle_new_candle(self, event: NewCandleEvent):
        """Обработчик события новой свечи"""
        # Фильтруем события только для нашего пользователя
        if event.user_id != self.user_id:
            return
            
        # Проверяем, нужно ли анализировать этот символ
        if not await self._should_analyze_symbol(event.symbol):
            return
            
        # Проверяем cooldown
        now = datetime.now()
        last_analysis = self.last_analysis_time.get(event.symbol)
        if last_analysis and (now - last_analysis) < self.analysis_cooldown:
            return
            
        try:
            log_info(self.user_id, f"Анализ рынка для {event.symbol} по новой свече", module_name=__name__)
            
            # Проводим анализ рынка
            analysis = await self.analyzer.get_market_analysis(event.symbol)
            
            # Обновляем время последнего анализа
            self.last_analysis_time[event.symbol] = now
            
            # Принимаем решение о запуске стратегии
            strategy_decision = await self._make_strategy_decision(analysis)
            
            if strategy_decision:
                # Публикуем сигнал
                signal_event = SignalEvent(
                    user_id=self.user_id,
                    symbol=event.symbol,
                    strategy_type=strategy_decision["strategy_type"],
                    signal_strength=analysis.get("signal_strength", 50),
                    analysis_data=analysis
                )
                
                await self.event_bus.publish(signal_event)
                log_info(self.user_id,f"Сигнал отправлен: {strategy_decision['strategy_type']} для {event.symbol} "
                    f"(сила: {analysis.get('signal_strength', 50)})",module_name=__name__)
        except Exception as e:
            log_error(self.user_id, f"Ошибка обработки новой свечи {event.symbol}: {e}", module_name=__name__)
            
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
            
    async def _should_analyze_symbol(self, symbol: str) -> bool:
        """Проверка, нужно ли анализировать символ"""
        if not self.user_config:
            return False
            
        # Проверяем, есть ли символ в watchlist
        watchlist = self.user_config.get("watchlist_symbols", [])
        return symbol in watchlist
        
    async def _make_strategy_decision(self, analysis: Dict[str, Any]) -> Optional[Dict[str, str]]:
        """
        Принятие решения о запуске стратегии на основе анализа
        Returns:
            Dict с типом стратегии или None если стратегию запускать не нужно
        """
        try:
            regime = analysis.get('regime', 'UNCERTAIN')
            signal_strength = analysis.get('signal_strength', 0)
            
            # Минимальная сила сигнала для запуска стратегии
            min_signal_strength = 40
            
            if signal_strength < min_signal_strength:
                return None
                
            # Выбор стратегии на основе режима рынка
            if regime in ['STRONG_TREND', 'TREND']:
                # Для трендового рынка - импульсная стратегия
                return {
                    "strategy_type": "impulse_trailing",
                    "reason": f"Трендовый рынок, сила сигнала: {signal_strength}"
                }
            elif regime in ['STRONG_FLAT', 'FLAT']:
                # Для флэтового рынка - сеточная стратегия
                return {
                    "strategy_type": "bidirectional_grid",
                    "reason": f"Флэтовый рынок, сила сигнала: {signal_strength}"
                }
            elif regime == 'WEAK_TREND' and signal_strength > 60:
                # Для слабого тренда при высокой силе сигнала - скальпинг
                return {
                    "strategy_type": "grid_scalping",
                    "reason": f"Слабый тренд с высокой силой сигнала: {signal_strength}"
                }
                
            return None
        except Exception as e:
            log_error(self.user_id, f"Ошибка принятия решения о стратегии: {e}", module_name=__name__)
            return None
