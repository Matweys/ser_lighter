# analysis/market_analyzer.py

import pandas as pd
import pandas_ta as ta
from decimal import Decimal, getcontext
from typing import Dict, Optional
from dataclasses import dataclass

from core.logger import log_info, log_error
from core.functions import to_decimal
from api.bybit_api import BybitAPI
from cache.redis_manager import redis_manager
from core.default_configs import DefaultConfigs

getcontext().prec = 28

@dataclass
class MarketAnalysis:
    """Результат анализа рынка для асимметричной стратегии"""
    symbol: str
    current_price: Decimal
    ema_trend: str  # "UP" или "DOWN"
    is_consolidating_now: bool
    consolidation_high: Decimal
    is_panic_bar: bool
    atr: Decimal
    friction_level: str  # "HIGH", "LOW", "NEUTRAL"
    friction_value: Decimal

    def to_dict(self) -> Dict:
        """Преобразование в словарь для передачи через события."""
        return {
            'symbol': self.symbol,
            'current_price': str(self.current_price),
            'ema_trend': self.ema_trend,
            'is_consolidating_now': self.is_consolidating_now,
            'consolidation_high': str(self.consolidation_high),
            'is_panic_bar': self.is_panic_bar,
            'atr': str(self.atr),
            'friction_level': self.friction_level,
            'friction_value': str(self.friction_value)
        }

class MarketAnalyzer:
    """
    Анализатор рынка, адаптированный для асимметричной стратегии.
    Рассчитывает необходимые индикаторы и паттерны на заданном таймфрейме.
    """
    def __init__(self, user_id: int, bybit_api: BybitAPI):
        self.user_id = user_id
        self.api = bybit_api
        self.candle_limit = 400  # Увеличено для более точных расчетов
        log_info(user_id, f"MarketAnalyzer инициализирован для пользователя {user_id}", "market_analyzer")

    async def get_market_analysis(self, symbol: str, timeframe: str) -> Optional[MarketAnalysis]:
        """
        Получает и анализирует рыночные данные для одного символа и таймфрейма.
        """
        try:
            candles = await self._get_candles_cached(symbol, timeframe)
            if candles is None or len(candles) < 200: # Проверка на достаточность данных
                log_error(self.user_id, f"Недостаточно данных для анализа {symbol} на {timeframe}", "market_analyzer")
                return None

            # --- УПРОЩЕНИЕ: Анализируем и сразу возвращаем результат ---
            analysis_dict = self._analyze_timeframe(candles, symbol)
            if analysis_dict:
                return MarketAnalysis(**analysis_dict)
            return None
        except Exception as e:
            log_error(self.user_id, f"Критическая ошибка анализа рынка для {symbol}: {e}", "market_analyzer")
            return None

    def _calculate_friction(self, candles: pd.DataFrame) -> tuple[str, Decimal]:
        """Расчет фрикции рынка за последние 100 свечей."""
        try:
            window = 100
            if len(candles) < window:
                return "NEUTRAL", Decimal('1000')

            recent_candles = candles.tail(window)
            price_move = abs(recent_candles['close'].iloc[-1] - recent_candles['close'].iloc[0])
            volume = recent_candles['volume'].sum()

            # Избегаем деления на ноль
            friction = volume / (price_move + Decimal('1e-9'))

            if friction > 5000:
                return "HIGH", friction
            elif friction < 500:
                return "LOW", friction
            else:
                return "NEUTRAL", friction

        except Exception as e:
            log_error(self.user_id, f"Ошибка расчета фрикции: {e}", "market_analyzer")
            return "NEUTRAL", Decimal('1000')

    async def _get_candles_cached(self, symbol: str, timeframe: str) -> Optional[pd.DataFrame]:
        """Получение свечей с кэшированием."""
        try:
            # Попытка получить из кэша
            cache_key = f"candles:{symbol}:{timeframe}"
            cached_data = await redis_manager.get_cached_data(cache_key)

            if cached_data:
                # Преобразование из кэша в DataFrame
                return pd.DataFrame(cached_data)

            # Получение свежих данных через API
            candles = await self.api.get_klines(
                symbol=symbol,
                interval=timeframe,
                limit=self.candle_limit
            )

            if candles and len(candles) > 0:
                df = pd.DataFrame(candles)
                # Кэширование на 1 минуту
                await redis_manager.cache_data(cache_key, df.to_dict('records'), ttl=60)
                return df

            return None
        except Exception as e:
            log_error(self.user_id, f"Ошибка получения свечей для {symbol}: {e}", "market_analyzer")
            return None

    def _analyze_timeframe(self, candles: pd.DataFrame, symbol: str) -> Optional[Dict]:
        """Анализ одного таймфрейма по новой, асимметричной логике."""
        try:
            config = DefaultConfigs.get_impulse_trailing_config()

            # Приведение типов данных к float для pandas_ta
            candles_float = candles.copy()
            for col in ['open', 'high', 'low', 'close', 'volume']:
                candles_float[col] = candles_float[col].astype(float)

            # 1. Расчет EMA
            candles_float['ema'] = ta.ema(candles_float['close'], length=config['long_ema_len'])

            # 2. Расчет полос Боллинджера
            bbands = ta.bbands(candles_float['close'], length=config['long_bb_len'])
            if bbands is not None and not bbands.empty:
                candles_float['bb_width'] = (bbands[f'BBU_{config["long_bb_len"]}_2.0'] - bbands[f'BBL_{config["long_bb_len"]}_2.0']) / bbands[f'BBM_{config["long_bb_len"]}_2.0']
            else:
                candles_float['bb_width'] = 0.0

            # 3. Определение консолидации
            candles_float['is_narrow'] = candles_float['bb_width'] < config['long_bb_width_thresh']
            candles_float['consolidation'] = candles_float['is_narrow'].rolling(config['long_consol_bars']).apply(all, raw=False).fillna(0)

            # 4. Расчет панической свечи
            candles_float['ret'] = candles_float['close'].pct_change()
            candles_float['ret_std'] = candles_float['ret'].rolling(config['short_ret_lookback']).std()
            candles_float['vol_ma'] = candles_float['volume'].rolling(config['short_vol_ma']).mean()
            candles_float['vol_ratio'] = candles_float['volume'] / candles_float['vol_ma']

            cond_ret = candles_float['ret'] < (-config['short_panic_sigma_k'] * candles_float['ret_std'])
            cond_vol = candles_float['vol_ratio'] > config['short_vol_ratio_min']
            candles_float['is_panic'] = cond_ret & cond_vol

            # 5. Расчет ATR
            candles_float['atr'] = ta.atr(candles_float['high'], candles_float['low'], candles_float['close'], length=config['risk_atr_len'])

            # 6. Расчет фрикции рынка
            friction_level, friction_value = self._calculate_friction(candles)

            # 7. Формируем результат
            last = candles_float.iloc[-1]
            prev = candles_float.iloc[-2]

            return {
                'symbol': symbol,
                'current_price': to_decimal(last['close']),
                'ema_trend': "UP" if last['close'] > last['ema'] else "DOWN",
                'is_consolidating_now': bool(prev['consolidation']),
                'consolidation_high': to_decimal(candles_float['high'].iloc[-config['long_consol_bars']:-1].max()),
                'is_panic_bar': bool(last['is_panic']),
                'atr': to_decimal(last['atr']),
                'friction_level': friction_level,
                'friction_value': to_decimal(friction_value)
            }
        except Exception as e:
            log_error(self.user_id, f"Ошибка при расчете индикаторов для {symbol}: {e}", "market_analyzer")
            return None
