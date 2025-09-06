"""
Профессиональный анализатор рынка для многотаймфреймового анализа.
Выделен из MetaStrategist для четкого разделения ответственности.
"""

import asyncio
import pandas as pd
import pandas_ta as ta
from decimal import Decimal, getcontext
from typing import Dict, List, Optional, Tuple
from datetime import datetime, timedelta
from dataclasses import dataclass

from core.enums import MarketCondition, TimeFrame
from core.logger import log_info, log_error, log_warning, log_debug
from core.functions import to_decimal, calculate_atr, calculate_rsi, calculate_ema
from api.bybit_api import BybitAPI

# Настройка точности Decimal
getcontext().prec = 28



@dataclass
class MarketAnalysis:
    """Результат анализа рынка"""
    regime: MarketCondition
    strength: int  # 0-100
    confidence: int  # 0-100
    atr: Decimal
    rsi: Decimal
    trend_direction: str  # "UP", "DOWN", "SIDEWAYS"
    volatility_level: str  # "LOW", "MEDIUM", "HIGH"
    recommendation: str  # Рекомендация по стратегии
    trend_confirmation: bool
    current_price: Decimal

    def to_dict(self) -> Dict:
        """Преобразование в словарь для передачи через события"""
        return {
            'regime': self.regime.value,
            'strength': self.strength,
            'confidence': self.confidence,
            'atr': str(self.atr),
            'rsi': str(self.rsi),
            'trend_direction': self.trend_direction,
            'volatility_level': self.volatility_level,
            'recommendation': self.recommendation,
            'trend_confirmation': self.trend_confirmation,
            'current_price': str(self.current_price)
        }

class MarketAnalyzer:
    """
    Профессиональный анализатор рынка с многотаймфреймовым подходом.
    
    Возможности:
    - Анализ трех таймфреймов (15m, 1h, 4h)
    - Расчет технических индикаторов (EMA, RSI, ATR, ADX)
    - Определение рыночного режима и силы тренда
    - Рекомендации по выбору стратегий
    """
    
    def __init__(self, user_id: int, bybit_api: BybitAPI):
        self.user_id = user_id
        self.api = bybit_api
        log_info(user_id, f"MarketAnalyzer инициализирован для пользователя {user_id}", module_name="market_analyzer")
        
        # Настройки анализа
        #self.timeframes = [TimeFrame.M15, TimeFrame.H1, TimeFrame.H4]
        self.candle_limit = 200
        self.cache_duration = timedelta(minutes=5)
        
        log_info(user_id, f"MarketAnalyzer инициализирован для пользователя {user_id}", module_name="market_analyzer")

    async def get_market_analysis(self, symbol: str, timeframes: List[str]) -> MarketAnalysis:
        """
        Получить полный анализ рынка для символа.
        ...
        """
        try:
            # Получаем данные по всем таймфреймам
            timeframe_data = {}
            for tf_value in timeframes:
                candles = await self._get_candles_cached(symbol, tf_value)
                if candles is not None and len(candles) >= 50:
                    timeframe_data[tf_value] = candles
                else:
                    log_warning(self.user_id, f"Недостаточно данных для {symbol} на {tf_value}", module_name="market_analyzer")
            
            if not timeframe_data:
                log_error(self.user_id, f"Нет данных для анализа {symbol}", module_name="market_analyzer")
                return self._create_default_analysis()
            
            # Анализируем каждый таймфрейм
            tf_analyses = {}
            for tf, candles in timeframe_data.items():
                analysis = await self._analyze_timeframe(candles, tf)
                tf_analyses[tf] = analysis
                #log_debug(self.user_id, f"Анализ {tf}: {analysis}", module_name="market_analyzer")
            
            # Объединяем результаты
            final_analysis = self._combine_timeframe_analyses(tf_analyses)
            
            #log_info(self.user_id, f"Анализ {symbol} завершен: {final_analysis.regime.value}, сила: {final_analysis.strength}", module_name="market_analyzer")
            return final_analysis
            
        except Exception as e:
            log_error(self.user_id, f"Ошибка анализа рынка для {symbol}: {e}", module_name="market_analyzer")
            return self._create_default_analysis()

    async def _get_candles_cached(self, symbol: str, timeframe: str) -> Optional[pd.DataFrame]:
        """Получить данные свечей, используя глобальный кэш в Redis."""
        try:
            # 1. Генерируем ключ для кэша в Redis
            redis_cache_key = f"klines:{symbol}:{timeframe}"

            # 2. Пытаемся получить данные из Redis
            cached_data = await redis_manager.get_cached_data(redis_cache_key)

            if cached_data:
                # Если данные в кэше есть, преобразуем их из JSON в DataFrame
                df = pd.read_json(cached_data, orient='records')
                df['timestamp'] = pd.to_datetime(df['start_time'], unit='ms')
                df.set_index('timestamp', inplace=True)
                for col in ['open', 'high', 'low', 'close', 'volume']:
                    df[col] = df[col].apply(to_decimal)
                return df

            # 3. Если в кэше данных нет (cache miss), запрашиваем их у API
            candles_data = await self.api.get_klines(
                symbol=symbol,
                interval=timeframe,
                limit=self.candle_limit
            )

            if not candles_data:
                return None

            # 4. Преобразуем в DataFrame
            df = pd.DataFrame(candles_data)
            # Сохраняем start_time как обычную колонку для сериализации в JSON
            df['timestamp'] = pd.to_datetime(df['start_time'], unit='ms')
            df.set_index('timestamp', inplace=True)

            for col in ['open', 'high', 'low', 'close', 'volume']:
                df[col] = df[col].apply(to_decimal)

            # 5. Сериализуем DataFrame в JSON и сохраняем в Redis
            # Используем to_json с 'records' для корректного сохранения/чтения
            # Сбрасываем индекс, чтобы 'start_time' сохранился как колонка
            df_for_json = df.reset_index(drop=True)
            json_data = df_for_json.to_json(orient='records')

            # Кэшируем на 60 секунд. Этого достаточно, т.к. минутная свеча - самый частый триггер.
            await redis_manager.cache_data(redis_cache_key, json_data, ttl=60)

            return df

        except Exception as e:
            log_error(self.user_id, f"Ошибка получения свечей {symbol} {timeframe}: {e}", module_name="market_analyzer")
            return None
    
    async def _analyze_timeframe(self, candles: pd.DataFrame, timeframe: str) -> Dict:
        """Анализ одного таймфрейма"""
        try:
            # Рассчитываем индикаторы
            close_prices = [float(price) for price in candles['close'].tolist()]
            high_prices = [float(price) for price in candles['high'].tolist()]
            low_prices = [float(price) for price in candles['low'].tolist()]
            
            # EMA
            ema_20 = calculate_ema(close_prices, 20)
            ema_50 = calculate_ema(close_prices, 50)
            
            # RSI
            rsi = calculate_rsi(close_prices, 14)
            
            # ATR
            atr = calculate_atr(high_prices, low_prices, close_prices, 14)

            current_price = to_decimal(close_prices[-1])
            current_ema_20 = ema_20 if ema_20 > 0 else current_price
            current_ema_50 = ema_50 if ema_50 > 0 else current_price
            current_rsi = rsi
            current_atr = atr
            
            # Определяем тренд
            trend_direction = "SIDEWAYS"
            trend_strength = 0
            
            if current_ema_20 > current_ema_50:
                trend_direction = "UP"
                trend_strength = min(100, int((current_ema_20 / current_ema_50 - 1) * 10000))
            elif current_ema_20 < current_ema_50:
                trend_direction = "DOWN"
                trend_strength = min(100, int((current_ema_50 / current_ema_20 - 1) * 10000))

            # Определяем режим рынка
            if trend_strength > 50:
                if current_rsi > 70 or current_rsi < 30:
                    regime = MarketCondition.STRONG_TREND
                else:
                    regime = MarketCondition.TREND
            elif trend_strength > 20:
                regime = MarketCondition.WEAK_TREND
            else:
                if current_rsi > 60 or current_rsi < 40:
                    regime = MarketCondition.STRONG_FLAT
                else:
                    regime = MarketCondition.FLAT
            
            # Определяем волатильность
            volatility_level = "LOW"
            if current_atr > current_price * Decimal('0.02'):
                volatility_level = "HIGH"
            elif current_atr > current_price * Decimal('0.01'):
                volatility_level = "MEDIUM"
            
            return {
                'regime': regime,
                'strength': trend_strength,
                'trend_direction': trend_direction,
                'volatility_level': volatility_level,
                'rsi': current_rsi,
                'atr': current_atr,
                'ema_20': current_ema_20,
                'ema_50': current_ema_50,
                'price': current_price
            }
            
        except Exception as e:
            log_error(self.user_id, f"Ошибка анализа таймфрейма {timeframe}: {e}", module_name="market_analyzer")
            return {
                'regime': MarketCondition.FLAT,
                'strength': 0,
                'trend_direction': "SIDEWAYS",
                'volatility_level': "LOW",
                'rsi': Decimal('50'),
                'atr': Decimal('0'),
                'ema_20': Decimal('0'),
                'ema_50': Decimal('0'),
                'price': Decimal('0')
            }
    
    def _combine_timeframe_analyses(self, tf_analyses: Dict) -> MarketAnalysis:
        """Объединение анализов разных таймфреймов"""
        try:
            # Веса для разных таймфреймов
            weights = {
                TimeFrame.M15.value: 0.2,
                TimeFrame.H1.value: 0.3,
                TimeFrame.H4.value: 0.5
            }
            
            # Подсчитываем взвешенные значения
            total_strength = 0
            total_weight = 0
            regime_votes = {}
            trend_votes = {}
            volatility_votes = {}
            
            avg_rsi = Decimal('0')
            avg_atr = Decimal('0')

            # Список для подсчета направлений
            trend_directions_list = []

            for tf, analysis in tf_analyses.items():
                weight = weights.get(tf, 0.33)
                total_weight += weight
                
                # Сила тренда
                total_strength += analysis['strength'] * weight
                
                # Голосование по режиму
                regime = analysis['regime']
                regime_votes[regime] = regime_votes.get(regime, 0) + weight
                
                # Голосование по направлению тренда
                direction = analysis['trend_direction']
                trend_votes[direction] = trend_votes.get(direction, 0) + weight
                if direction != "SIDEWAYS":  # Собираем только явные тренды
                    trend_directions_list.append(direction)

                # Голосование по волатильности
                volatility = analysis['volatility_level']
                volatility_votes[volatility] = volatility_votes.get(volatility, 0) + weight
                
                # Средние значения индикаторов
                avg_rsi += analysis['rsi'] * Decimal(str(weight))
                avg_atr += analysis['atr'] * Decimal(str(weight))
            
            # Нормализуем
            if total_weight > 0:
                final_strength = int(total_strength / total_weight)
                avg_rsi = avg_rsi / Decimal(str(total_weight))
                avg_atr = avg_atr / Decimal(str(total_weight))
            else:
                final_strength = 0
                avg_rsi = Decimal('50')
                avg_atr = Decimal('0')
            
            # Определяем финальные значения по голосованию
            final_regime = max(regime_votes, key=regime_votes.get) if regime_votes else MarketCondition.FLAT
            final_trend = max(trend_votes, key=trend_votes.get) if trend_votes else "SIDEWAYS"
            final_volatility = max(volatility_votes, key=volatility_votes.get) if volatility_votes else "LOW"

            # Получаем текущую цену из наиболее релевантного (короткого) таймфрейма
            # Ищем самый короткий таймфрейм из доступных (например, '5m' < '15m')
            shortest_tf = sorted(tf_analyses.keys(), key=lambda x: int(x.replace('m', '').replace('h', '60').replace('d', '1440')))[0]
            current_price = tf_analyses[shortest_tf]['price'] if shortest_tf in tf_analyses else Decimal('0')

            # Рассчитываем уверенность
            confidence = min(100, int(max(regime_votes.values()) / total_weight * 100)) if total_weight > 0 else 50

            # Логика подтверждения тренда: большинство ТФ должны показывать одно направление
            min_confirming_timeframes = 2  # Требуем, чтобы хотя бы 2 ТФ совпадали
            trend_confirmation = False
            if final_trend != "SIDEWAYS":
                confirmation_count = trend_directions_list.count(final_trend)
                if confirmation_count >= min_confirming_timeframes:
                    trend_confirmation = True

            # Рекомендация по стратегии
            recommendation = self._get_strategy_recommendation(final_regime, final_strength, final_volatility)
            
            return MarketAnalysis(
                regime=final_regime,
                strength=final_strength,
                confidence=confidence,
                atr=avg_atr,
                rsi=avg_rsi,
                trend_direction=final_trend,
                volatility_level=final_volatility,
                recommendation=recommendation,
                trend_confirmation=trend_confirmation,
                current_price=current_price
            )
            
        except Exception as e:
            log_error(self.user_id, f"Ошибка объединения анализов: {e}", module_name="market_analyzer")
            return self._create_default_analysis()

    @staticmethod
    def _get_strategy_recommendation(regime: MarketCondition, strength: int, volatility: str) -> str:
        """Рекомендация стратегии на основе анализа"""
        if regime in [MarketCondition.STRONG_TREND, MarketCondition.TREND] and strength > 60:
            return "impulse_trailing"
        elif regime == MarketCondition.WEAK_TREND and volatility == "HIGH":
            return "grid_scalping"


    @staticmethod
    def _create_default_analysis() -> MarketAnalysis:
        """Создание анализа по умолчанию при ошибках"""
        return MarketAnalysis(
            regime=MarketCondition.FLAT,
            strength=0,
            confidence=0,
            atr=Decimal('0'),
            rsi=Decimal('50'),
            trend_direction="SIDEWAYS",
            volatility_level="LOW",
            recommendation="grid_scalping",
            trend_confirmation=False,
            current_price=Decimal('0')
        )
    
    def clear_cache(self):
        """Очистка кэша данных"""
        self.candles_cache.clear()
        self.cache_expiry.clear()
        log_info(self.user_id, "Кэш анализатора очищен", module_name="market_analyzer")

