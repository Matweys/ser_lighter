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


class MarketAnalyzer:
    """
    Профессиональный анализатор рынка с многотаймфреймовым анализом
    """

    def __init__(self, user_id: int, bybit_api: BybitAPI):
        self.user_id = user_id
        self.api = bybit_api
        self.default_timeframes = ["15m", "1h", "4h"]

    async def get_klines(self, symbol: str, timeframe: str, limit: int) -> List:
        """Получение исторических данных через API"""
        try:
            # Используем self.api, который теперь корректно инициализирован
            return await self.api.get_klines(symbol=symbol, interval=timeframe, limit=limit)
        except Exception as e:
            log_error(self.user_id, f"Ошибка получения klines: {e}", module_name=__name__)
            return []

    async def get_market_analysis(self, symbol: str) -> Dict[str, Any]:
        """
        Проводит многомерный анализ рынка с персональными настройками пользователя

        Args:
            symbol: Торговый символ

        Returns:
            Dict с результатами анализа
        """
        try:
            # Загружаем персональные настройки анализа, используя self.user_id
            analysis_config = await self._get_analysis_config(self.user_id)
            
            # Анализ по каждому таймфрейму
            timeframe_analyses = {}
            for timeframe in analysis_config["timeframes"]:
                tf_analysis = await self._analyze_timeframe(
                    user_id, symbol, timeframe, analysis_config
                )
                if tf_analysis:
                    timeframe_analyses[timeframe] = tf_analysis
                    
            if not timeframe_analyses:
                return {
                    'regime': 'UNCERTAIN',
                    'reason': 'Недостаточно данных для анализа',
                    'signal_strength': 0
                }
                
            # Сведение сигналов с разных таймфреймов
            final_analysis = await self._combine_timeframe_signals(
                timeframe_analyses
            )
            
            # Добавляем текущую цену и ATR для расчета позиции
            current_data = await self._get_current_market_data(user_id, symbol)
            final_analysis.update(current_data)
            return final_analysis
        except Exception as e:
            log_error(user_id, f"Ошибка анализа рынка для {symbol}: {e}", module_name=__name__)
            return {
                'regime': 'UNCERTAIN',
                'reason': f'Ошибка анализа: {str(e)}',
                'signal_strength': 0
            }

    async def _get_analysis_config(self, user_id: int) -> Dict[str, Any]:
        """Загрузка персональных настроек анализа пользователя"""
        try:
            global_config = await redis_manager.get_config(user_id, ConfigType.GLOBAL)
            if global_config and "analysis_config" in global_config:
                return global_config["analysis_config"]
            else:
                # Настройки по умолчанию
                return {
                    "fast_ema": 20,
                    "slow_ema": 50,
                    "adx_period": 14,
                    "atr_period": 14,
                    "timeframes": self.default_timeframes,
                    "trend_strength_threshold": 25,
                    "flat_threshold": 20
                }
        except Exception as e:
            log_error(user_id, f"Ошибка загрузки настроек анализа: {e}", module_name=__name__)
            return {
                "fast_ema": 20,
                "slow_ema": 50,
                "adx_period": 14,
                "atr_period": 14,
                "timeframes": self.default_timeframes,
                "trend_strength_threshold": 25,
                "flat_threshold": 20
            }
            
    async def _analyze_timeframe(self, user_id: int, symbol: str, timeframe: str, config: Dict) -> Optional[Dict]:
        """Анализ одного таймфрейма"""
        try:
            # Загружаем исторические данные
            klines = await self.get_klines(symbol, timeframe, limit=100)
            if len(klines) < max(config["slow_ema"], config["adx_period"]) + 10:
                return None
                
            # Создаем DataFrame и рассчитываем индикаторы
            df = self._create_dataframe(klines)
            
            # EMA
            df.ta.ema(length=config["fast_ema"], append=True)
            df.ta.ema(length=config["slow_ema"], append=True)
            
            # ADX для определения силы тренда
            df.ta.adx(length=config["adx_period"], append=True)
            
            # ATR для волатильности
            df.ta.atr(length=config["atr_period"], append=True)
            
            # RSI для перекупленности/перепроданности
            df.ta.rsi(length=14, append=True)

            # Анализ последней и предпоследней свечей
            last_row = df.iloc[-1]
            prev_row = df.iloc[-2]

            # Определение тренда по последней свече
            fast_ema = Decimal(str(last_row[f'EMA_{config["fast_ema"]}']))
            slow_ema = Decimal(str(last_row[f'EMA_{config["slow_ema"]}']))

            # Получение данных с предыдущей свечи для проверки пересечения
            prev_fast_ema = Decimal(str(prev_row[f'EMA_{config["fast_ema"]}']))
            prev_slow_ema = Decimal(str(prev_row[f'EMA_{config["slow_ema"]}']))

            adx = Decimal(str(last_row[f'ADX_{config["adx_period"]}']))
            atr = Decimal(str(last_row[f'ATRr_{config["atr_period"]}']))
            rsi = Decimal(str(last_row['RSI_14']))

            # Направление тренда с учетом пересечения EMA для большей надежности
            is_bullish_cross = fast_ema > slow_ema and prev_fast_ema <= prev_slow_ema

            if is_bullish_cross:
                trend_direction = 'up'
            # Если пересечения не было, определяем по текущему положению
            else:
                trend_direction = 'up' if fast_ema > slow_ema else 'down'
            
            # Сила тренда
            trend_strength = 'strong' if adx > config["trend_strength_threshold"] else 'weak'
            
            # Режим рынка
            if adx < config["flat_threshold"]:
                regime = 'FLAT'
            elif trend_strength == 'strong':
                regime = 'TREND'
            else:
                regime = 'WEAK_TREND'
                
            return {
                'timeframe': timeframe,
                'regime': regime,
                'trend_direction': trend_direction,
                'trend_strength': trend_strength,
                'adx': adx,
                'atr': atr,
                'rsi': rsi,
                'fast_ema': fast_ema,
                'slow_ema': slow_ema,
                'current_price': Decimal(str(last_row['close']))
            }

        except Exception as e:
            log_error(user_id, f"Ошибка анализа таймфрейма {timeframe}: {e}", module_name=__name__)
            return None

    @staticmethod
    async def _combine_timeframe_signals(analyses: Dict[str, Dict]) -> Dict[str, Any]:
        """Сведение сигналов с разных таймфреймов"""
        try:
            # Приоритет таймфреймов: 4h > 1h > 15m
            timeframe_priority = {"4h": 3, "1h": 2, "15m": 1}
            
            # Получаем анализы по приоритету
            sorted_analyses = sorted(
                analyses.items(),
                key=lambda x: timeframe_priority.get(x[0], 0),
                reverse=True
            )
            
            if not sorted_analyses:
                return {'regime': 'UNCERTAIN', 'signal_strength': 0}
                
            # Основной анализ с самого старшего таймфрейма
            primary_tf, primary_analysis = sorted_analyses[0]
            
            # Подтверждение с младших таймфреймов
            confirmations = 0
            total_checks = 0
            
            for tf, analysis in sorted_analyses[1:]:
                total_checks += 1
                
                # Проверяем совпадение направления тренда
                if analysis['trend_direction'] == primary_analysis['trend_direction']:
                    confirmations += 1
                    
                # Проверяем совпадение режима
                if analysis['regime'] == primary_analysis['regime']:
                    confirmations += 0.5
                    
            # Расчет силы сигнала (0-100)
            if total_checks > 0:
                confirmation_ratio = confirmations / total_checks
                base_strength = int(primary_analysis['adx'])
                signal_strength = min(100, int(base_strength * confirmation_ratio))
            else:
                signal_strength = int(primary_analysis['adx'])
                
            # Определение финального режима
            final_regime = primary_analysis['regime']
            
            # Особые случаи
            if signal_strength < 30:
                final_regime = 'UNCERTAIN'
            elif primary_analysis['regime'] == 'FLAT' and signal_strength > 60:
                final_regime = 'STRONG_FLAT'  # Хороший флэт для сеточных стратегий
            elif primary_analysis['regime'] == 'TREND' and signal_strength > 70:
                final_regime = 'STRONG_TREND'  # Сильный тренд для трендовых стратегий
                
            return {
                'regime': final_regime,
                'trend_direction': primary_analysis['trend_direction'],
                'signal_strength': signal_strength,
                'primary_timeframe': primary_tf,
                'confirmations': confirmations,
                'total_checks': total_checks,
                'atr': primary_analysis['atr'],
                'current_price': primary_analysis['current_price'],
                'analyses_detail': analyses
            }
            
        except Exception as e:
            log_error(0, f"Ошибка сведения сигналов: {e}", module_name=__name__)
            return {'regime': 'UNCERTAIN', 'signal_strength': 0}

    @staticmethod
    async def _get_current_market_data(user_id: int, symbol: str) -> Dict[str, Decimal]:
        """Получение текущих рыночных данных"""
        try:
            # Получаем последние данные 1m для актуальной цены
            klines = await get_klines(user_id, symbol, "1m", limit=1)
            if klines:
                current_price = Decimal(str(klines[0][4]))  # close price
                return {'current_price': current_price}
            return {}
        except Exception as e:
            log_error(user_id, f"Ошибка получения текущих данных: {e}", module_name=__name__)
            return {}

    @staticmethod
    def _create_dataframe(klines: List) -> pd.DataFrame:
        """Создание DataFrame из данных свечей"""
        df = pd.DataFrame(klines, columns=['timestamp', 'open', 'high', 'low', 'close', 'volume', 'turnover'])
        
        # Конвертация в числовые типы
        for col in ['open', 'high', 'low', 'close', 'volume']:
            df[col] = pd.to_numeric(df[col], errors='coerce')
            
        # Удаление некорректных данных
        df.dropna(inplace=True)
        
        return df


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
