import pandas as pd
import pandas_ta as ta
from decimal import Decimal, getcontext
from typing import Dict, Optional
from dataclasses import dataclass

from core.logger import log_info, log_error, log_warning
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
            if candles is None or len(candles) < 200:  # Проверка на достаточность данных
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
            # Используем turnover (объем в USDT) вместо volume для более точного расчета
            turnover = recent_candles['turnover'].sum()
            friction = turnover / (price_move + Decimal('1e-9'))

            # Увеличиваем пороги в 1000 раз, чтобы сделать фильтр менее строгим
            if friction > 5_000_000:
                return "HIGH", friction
            elif friction < 500_000:
                return "LOW", friction
            else:
                return "NEUTRAL", friction

        except Exception as e:
            log_error(self.user_id, f"Ошибка расчета фрикции: {e}", "market_analyzer")
            return "NEUTRAL", Decimal('1000')

    async def _get_candles_cached(self, symbol: str, timeframe: str) -> Optional[pd.DataFrame]:
        """Получение свечей с кэшированием."""
        try:
            cache_key = f"candles:{symbol}:{timeframe}"
            cached_data = await redis_manager.get_cached_data(cache_key)

            if cached_data:
                df = pd.DataFrame(cached_data)
                # ВАЖНО: Явное преобразование столбцов в числовой тип
                numeric_cols = ['open', 'high', 'low', 'close', 'volume', 'turnover']
                for col in numeric_cols:
                    if col in df.columns:
                        df[col] = pd.to_numeric(df[col], errors='coerce')
                return df

            candles = await self.api.get_klines(
                symbol=symbol,
                interval=timeframe,
                limit=self.candle_limit
            )

            if candles and len(candles) > 0:
                df = pd.DataFrame(candles)
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
            try:
                candles_float['ema'] = ta.ema(candles_float['close'], length=config['long_ema_len'])
                if candles_float['ema'].isna().all():
                    log_error(self.user_id, f"EMA не рассчитана для {symbol}", "market_analyzer")
                    candles_float['ema'] = candles_float['close']  # Fallback
            except Exception as e:
                log_error(self.user_id, f"Ошибка расчета EMA для {symbol}: {e}", "market_analyzer")
                candles_float['ema'] = candles_float['close']

            # 2. Расчет полос Боллинджера
            try:
                bbands = ta.bbands(candles_float['close'], length=config['long_bb_len'])
                if bbands is not None and not bbands.empty:
                    bb_upper = bbands[f'BBU_{config["long_bb_len"]}_2.0']
                    bb_lower = bbands[f'BBL_{config["long_bb_len"]}_2.0']
                    bb_middle = bbands[f'BBM_{config["long_bb_len"]}_2.0']

                    # Проверяем на NaN
                    if bb_upper.isna().all() or bb_lower.isna().all() or bb_middle.isna().all():
                        log_warning(self.user_id, f"Bollinger Bands содержат NaN для {symbol}", "market_analyzer")
                        candles_float['bb_width'] = 0.02  # 2% по умолчанию
                    else:
                        candles_float['bb_width'] = (bb_upper - bb_lower) / bb_middle
                        candles_float['bb_width'] = candles_float['bb_width'].fillna(0.02)
                else:
                    log_warning(self.user_id, f"Bollinger Bands не рассчитаны для {symbol}", "market_analyzer")
                    candles_float['bb_width'] = 0.02
            except Exception as e:
                log_error(self.user_id, f"Ошибка расчета Bollinger Bands для {symbol}: {e}", "market_analyzer")
                candles_float['bb_width'] = 0.02

            # 3. Определение консолидации
            try:
                candles_float['is_narrow'] = candles_float['bb_width'] < config['long_bb_width_thresh']
                candles_float['consolidation'] = candles_float['is_narrow'].rolling(config['long_consol_bars']).apply(
                    all, raw=False).fillna(0)
            except Exception as e:
                log_error(self.user_id, f"Ошибка расчета консолидации для {symbol}: {e}", "market_analyzer")
                candles_float['consolidation'] = 0

            # 4. Расчет панической свечи
            try:
                candles_float['ret'] = candles_float['close'].pct_change()
                candles_float['ret_std'] = candles_float['ret'].rolling(config['short_ret_lookback']).std()
                candles_float['vol_ma'] = candles_float['volume'].rolling(config['short_vol_ma']).mean()
                candles_float['vol_ratio'] = candles_float['volume'] / candles_float['vol_ma']

                # Проверяем на NaN и заменяем
                candles_float['ret_std'] = candles_float['ret_std'].fillna(0.01)
                candles_float['vol_ratio'] = candles_float['vol_ratio'].fillna(1.0)

                cond_ret = candles_float['ret'] < (-config['short_panic_sigma_k'] * candles_float['ret_std'])
                cond_vol = candles_float['vol_ratio'] > config['short_vol_ratio_min']
                candles_float['is_panic'] = cond_ret & cond_vol
            except Exception as e:
                log_error(self.user_id, f"Ошибка расчета панической свечи для {symbol}: {e}", "market_analyzer")
                candles_float['is_panic'] = False

            # 5. Расчет ATR
            try:
                candles_float['atr'] = ta.atr(candles_float['high'], candles_float['low'], candles_float['close'],
                                              length=config['risk_atr_len'])
                if candles_float['atr'].isna().all():
                    log_warning(self.user_id, f"ATR не рассчитан для {symbol}, используем значение по умолчанию",
                                "market_analyzer")
                    # Рассчитываем простой ATR как среднее от high-low
                    candles_float['atr'] = (candles_float['high'] - candles_float['low']).rolling(14).mean()
                candles_float['atr'] = candles_float['atr'].fillna(candles_float['close'] * 0.02)  # 2% от цены
            except Exception as e:
                log_error(self.user_id, f"Ошибка расчета ATR для {symbol}: {e}", "market_analyzer")
                candles_float['atr'] = candles_float['close'] * 0.02

            # 6. Расчет фрикции рынка
            friction_level, friction_value = self._calculate_friction(candles)

            # 7. Формируем результат с проверками
            last = candles_float.iloc[-1]
            prev = candles_float.iloc[-2]

            # Проверяем критические значения
            current_price = last['close']
            atr_value = last['atr']

            if pd.isna(current_price) or current_price <= 0:
                log_error(self.user_id, f"Некорректная цена для {symbol}: {current_price}", "market_analyzer")
                return None

            if pd.isna(atr_value) or atr_value <= 0:
                log_warning(self.user_id, f"Некорректный ATR для {symbol}: {atr_value}, используем 2% от цены",
                            "market_analyzer")
                atr_value = current_price * 0.02

            # Безопасное получение максимума для consolidation_high
            try:
                consol_bars = config['long_consol_bars']
                if len(candles_float) > consol_bars:
                    consolidation_high = candles_float['high'].iloc[-consol_bars:-1].max()
                else:
                    consolidation_high = candles_float['high'].max()

                if pd.isna(consolidation_high):
                    consolidation_high = current_price
            except Exception as e:
                log_warning(self.user_id, f"Ошибка расчета consolidation_high для {symbol}: {e}", "market_analyzer")
                consolidation_high = current_price

            result = {
                'symbol': symbol,
                'current_price': to_decimal(current_price),
                'ema_trend': "UP" if current_price > last['ema'] else "DOWN",
                'is_consolidating_now': bool(prev['consolidation']) if not pd.isna(prev['consolidation']) else False,
                'consolidation_high': to_decimal(consolidation_high),
                'is_panic_bar': bool(last['is_panic']) if not pd.isna(last['is_panic']) else False,
                'atr': to_decimal(atr_value),
                'friction_level': friction_level,
                'friction_value': to_decimal(friction_value)
            }

            log_info(self.user_id,
                     f"Анализ {symbol} завершен успешно: trend={result['ema_trend']}, panic={result['is_panic_bar']}, consol={result['is_consolidating_now']}",
                     "market_analyzer")
            return result

        except Exception as e:
            log_error(self.user_id, f"Критическая ошибка при расчете индикаторов для {symbol}: {e}", "market_analyzer")
            return None