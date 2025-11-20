# analysis/signal_analyzer.py
import pandas as pd
import numpy as np
from decimal import Decimal
from typing import Optional, Dict
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
    Анализатор, реализующий логику на основе EMA и RSI с использованием pandas.
    """
    
    @staticmethod
    def _calculate_rsi(prices: pd.Series, period: int = 14) -> float:
        """Расчет RSI (Relative Strength Index)"""
        delta = prices.diff()
        gain = (delta.where(delta > 0, 0)).rolling(window=period).mean()
        loss = (-delta.where(delta < 0, 0)).rolling(window=period).mean()
        rs = gain / loss
        rsi = 100 - (100 / (1 + rs))
        return rsi.iloc[-1] if not pd.isna(rsi.iloc[-1]) else 50.0

    def __init__(self, user_id: int, api: BybitAPI, config: Dict):
        self.user_id = user_id
        self.api = api
        self.config = config

        # ПАРАМЕТРЫ СТРАТЕГИИ из конфигурации
        self.EMA_SHORT = config.get("EMA_SHORT", 21)
        self.EMA_LONG = config.get("EMA_LONG", 50)
        self.RSI_PERIOD = config.get("RSI_PERIOD", 14)
        self.RSI_NEUTRAL_MIN = config.get("RSI_NEUTRAL_MIN", 30)
        self.RSI_NEUTRAL_MAX = config.get("RSI_NEUTRAL_MAX", 70)
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

            # 2. Подготовка данных для pandas
            df = pd.DataFrame(candles)
            close_series = df['close'].astype(float)

            if len(close_series) < self.EMA_LONG or len(close_series) < self.RSI_PERIOD:
                return None

            # 3. Расчет индикаторов используя pandas
            ema_short = close_series.ewm(span=self.EMA_SHORT, adjust=False).mean().iloc[-1]
            ema_long = close_series.ewm(span=self.EMA_LONG, adjust=False).mean().iloc[-1]
            rsi = self._calculate_rsi(close_series, period=self.RSI_PERIOD)
            price = Decimal(str(close_series.iloc[-1]))

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