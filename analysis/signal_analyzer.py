# analysis/signal_analyzer.py
import pandas as pd
import numpy as np
import talib
from decimal import Decimal
from typing import Optional, Dict, List
from dataclasses import dataclass

from api.bybit_api import BybitAPI
from core.logger import log_error, log_debug


@dataclass
class SignalAnalysisResult:
    """Результат анализа для SignalScalperStrategy."""
    direction: str  # "LONG", "SHORT", "HOLD"
    price: Decimal
    indicators: Dict[str, float]


class SignalAnalyzer:
    """
    Анализатор, реализующий логику на основе EMA и RSI с использованием TA-Lib.
    """

    def __init__(self, user_id: int, api: BybitAPI, config: Dict):
        self.user_id = user_id
        self.api = api
        self.config = config

        # ПАРАМЕТРЫ СТРАТЕГИИ
        self.EMA_SHORT = 21
        self.EMA_LONG = 50
        self.RSI_PERIOD = 14
        self.RSI_NEUTRAL_MIN = 35  # Минимум нейтральной зоны RSI
        self.RSI_NEUTRAL_MAX = 65  # Максимум нейтральной зоны RSI
        self.VOLUME_MA_PERIOD = 20  # Период для среднего объема
        self.VOLUME_THRESHOLD = 1.3  # Объем должен быть на 30% выше среднего
        self.HISTORY_LIMIT = 100

    async def get_analysis(self, symbol: str) -> Optional[SignalAnalysisResult]:
        """
        Получает исторические данные и рассчитывает сигнал.
        """
        try:
            timeframe = self.config.get("analysis_timeframe", "5m")

            # 1. Получение свечей
            candles = await self.api.get_klines(
                symbol=symbol,
                interval=timeframe,
                limit=self.HISTORY_LIMIT
            )

            if not candles or len(candles) < self.HISTORY_LIMIT:
                log_debug(self.user_id, f"Недостаточно исторических данных для {symbol}, накопление...",
                          "SignalAnalyzer")
                return None

            # 2. Подготовка данных для TA-Lib
            df = pd.DataFrame(candles)
            close_prices = df['close'].to_numpy(dtype=float)
            volumes = df['volume'].to_numpy(dtype=float)

            if len(close_prices) < self.EMA_LONG or len(close_prices) < self.RSI_PERIOD or len(volumes) < self.VOLUME_MA_PERIOD:
                return None

            # 3. Расчет индикаторов
            ema_short = talib.EMA(close_prices, timeperiod=self.EMA_SHORT)[-1]
            ema_long = talib.EMA(close_prices, timeperiod=self.EMA_LONG)[-1]
            rsi = talib.RSI(close_prices, timeperiod=self.RSI_PERIOD)[-1]

            # Объемный анализ
            volume_ma = talib.SMA(volumes, timeperiod=self.VOLUME_MA_PERIOD)[-1]
            current_volume = volumes[-1]
            volume_ratio = current_volume / volume_ma if volume_ma > 0 else 0

            price = Decimal(str(close_prices[-1]))

            # 4. Улучшенная логика сигналов с RSI и объемным фильтром
            direction = "HOLD"

            # Проверяем базовые условия EMA
            ema_long_signal = ema_short > ema_long
            ema_short_signal = ema_short < ema_long

            # Проверяем RSI в нейтральной зоне (избегаем экстремумов)
            rsi_neutral = self.RSI_NEUTRAL_MIN < rsi < self.RSI_NEUTRAL_MAX

            # Проверяем объемный фильтр
            volume_confirmed = volume_ratio >= self.VOLUME_THRESHOLD

            # Генерируем сигналы только при выполнении всех условий
            if ema_long_signal and rsi_neutral and volume_confirmed:
                direction = "LONG"
            elif ema_short_signal and rsi_neutral and volume_confirmed:
                direction = "SHORT"

            return SignalAnalysisResult(
                direction=direction,
                price=price,
                indicators={
                    "ema_short": ema_short,
                    "ema_long": ema_long,
                    "rsi": rsi,
                    "volume_ratio": volume_ratio,
                    "volume_confirmed": volume_confirmed,
                    "rsi_neutral": rsi_neutral
                }
            )

        except Exception as e:
            log_error(self.user_id, f"Ошибка в SignalAnalyzer для {symbol}: {e}", "SignalAnalyzer")
            return None