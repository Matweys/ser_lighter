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

        # ПАРАМЕТРЫ СТРАТЕГИИ из конфигурации
        self.EMA_SHORT = config.get("EMA_SHORT", 21)
        self.EMA_LONG = config.get("EMA_LONG", 50)
        self.RSI_PERIOD = config.get("RSI_PERIOD", 14)
        self.RSI_NEUTRAL_MIN = config.get("RSI_NEUTRAL_MIN", 35)
        self.RSI_NEUTRAL_MAX = config.get("RSI_NEUTRAL_MAX", 65)
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

            if len(close_prices) < self.EMA_LONG or len(close_prices) < self.RSI_PERIOD:
                return None

            # 3. Расчет индикаторов
            ema_short = talib.EMA(close_prices, timeperiod=self.EMA_SHORT)[-1]
            ema_long = talib.EMA(close_prices, timeperiod=self.EMA_LONG)[-1]
            rsi = talib.RSI(close_prices, timeperiod=self.RSI_PERIOD)[-1]
            price = Decimal(str(close_prices[-1]))

            # 4. Логика сигналов: EMA + RSI (без объемного фильтра)
            direction = "HOLD"

            # Проверяем базовые условия EMA
            ema_long_signal = ema_short > ema_long
            ema_short_signal = ema_short < ema_long

            # Проверяем RSI в нейтральной зоне (избегаем экстремумов)
            rsi_neutral = self.RSI_NEUTRAL_MIN < rsi < self.RSI_NEUTRAL_MAX

            # Генерируем сигналы при выполнении условий EMA + RSI
            if ema_long_signal and rsi_neutral:
                direction = "LONG"
            elif ema_short_signal and rsi_neutral:
                direction = "SHORT"

            return SignalAnalysisResult(
                direction=direction,
                price=price,
                indicators={
                    "ema_short": ema_short,
                    "ema_long": ema_long,
                    "rsi": rsi,
                    "rsi_neutral": rsi_neutral
                }
            )

        except Exception as e:
            log_error(self.user_id, f"Ошибка в SignalAnalyzer для {symbol}: {e}", "SignalAnalyzer")
            return None