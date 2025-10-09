"""
📌 Детектор резких движений (Spike Detector)

Отслеживает мгновенные всплески волатильности на 1-минутных свечах.
Используется для определения оптимальной точки входа в сделку.

Логика:
- Хранит последние N свечей (1 минута)
- Детектирует всплески больше порога (например, 0.1%)
- Предоставляет анализ последних всплесков для принятия решений
"""

import asyncio
from decimal import Decimal
from collections import deque
from typing import Optional, List, Dict
from datetime import datetime

from core.logger import log_info, log_debug, log_error


class SpikeDetector:
    """
    Детектор всплесков цены для одного символа.
    Каждый экземпляр работает НЕЗАВИСИМО для своего символа.
    """

    def __init__(self, user_id: int, symbol: str, lookback: int = 50, threshold: float = 0.001):
        """
        Args:
            user_id: ID пользователя (для логирования)
            symbol: Символ для отслеживания (например, "BTCUSDT")
            lookback: Количество свечей для хранения в истории
            threshold: Порог для детекции всплеска (0.001 = 0.1%)
        """
        self.user_id = user_id
        self.symbol = symbol
        self.threshold = Decimal(str(threshold))

        # История цен закрытия (1-минутные свечи)
        self.price_history: deque = deque(maxlen=lookback)

        # История обнаруженных всплесков {timestamp, direction, magnitude, price}
        self.spike_history: deque = deque(maxlen=20)  # Последние 20 всплесков

        log_info(user_id,
                f"📡 SpikeDetector инициализирован для {symbol}: порог={threshold*100:.2f}%, lookback={lookback}",
                "SpikeDetector")

    def add_candle(self, close_price: Decimal, timestamp: Optional[float] = None):
        """
        Добавляет новую закрытую 1-минутную свечу в историю и проверяет всплеск.

        Args:
            close_price: Цена закрытия свечи
            timestamp: Временная метка (если None, используется текущее время)
        """
        if timestamp is None:
            timestamp = datetime.now().timestamp()

        # Добавляем цену в историю
        self.price_history.append(close_price)

        # Проверяем всплеск (нужно минимум 2 свечи)
        if len(self.price_history) >= 2:
            self._detect_spike(timestamp)

    def _detect_spike(self, timestamp: float):
        """
        Анализирует последнее изменение цены на предмет всплеска.

        Args:
            timestamp: Временная метка текущей свечи
        """
        # Берем последние 2 цены
        prev_price = self.price_history[-2]
        curr_price = self.price_history[-1]

        # Рассчитываем процентное изменение
        if prev_price == 0:
            return

        pct_change = (curr_price - prev_price) / prev_price

        # Проверяем превышение порога
        if abs(pct_change) > self.threshold:
            direction = "UP" if pct_change > 0 else "DOWN"
            magnitude = abs(pct_change)

            # Сохраняем всплеск
            spike = {
                "timestamp": timestamp,
                "direction": direction,
                "magnitude": float(magnitude),
                "price": float(curr_price),
                "pct_change": float(pct_change)
            }
            self.spike_history.append(spike)

            emoji = "📈" if direction == "UP" else "📉"
            log_debug(self.user_id,
                     f"{emoji} Всплеск {direction} {pct_change*100:.2f}% на {self.symbol} (цена: {curr_price})",
                     "SpikeDetector")

    def get_recent_spikes(self, seconds: int = 300) -> List[Dict]:
        """
        Возвращает всплески за последние N секунд.

        Args:
            seconds: Временное окно в секундах (по умолчанию 5 минут = 300 сек)

        Returns:
            Список всплесков: [{"timestamp", "direction", "magnitude", "price"}, ...]
        """
        current_time = datetime.now().timestamp()
        cutoff_time = current_time - seconds

        return [
            spike for spike in self.spike_history
            if spike["timestamp"] >= cutoff_time
        ]

    def analyze_momentum(self, seconds: int = 300) -> Dict:
        """
        Анализирует импульс (momentum) за последние N секунд.

        Args:
            seconds: Временное окно (по умолчанию 5 минут)

        Returns:
            {
                "up_spikes": количество всплесков вверх,
                "down_spikes": количество всплесков вниз,
                "last_3_direction": направление последних 3 всплесков ("UP", "DOWN", "MIXED", None),
                "consecutive_up": есть ли 2+ всплеска ВВЕРХ подряд,
                "consecutive_down": есть ли 2+ всплеска ВНИЗ подряд,
                "momentum": "BULLISH", "BEARISH", "NEUTRAL"
            }
        """
        recent_spikes = self.get_recent_spikes(seconds)

        if not recent_spikes:
            return {
                "up_spikes": 0,
                "down_spikes": 0,
                "last_3_direction": None,
                "consecutive_up": False,
                "consecutive_down": False,
                "momentum": "NEUTRAL"
            }

        # Считаем всплески по направлениям
        up_spikes = sum(1 for s in recent_spikes if s["direction"] == "UP")
        down_spikes = sum(1 for s in recent_spikes if s["direction"] == "DOWN")

        # Анализируем последние 3 всплеска
        last_3 = recent_spikes[-3:] if len(recent_spikes) >= 3 else recent_spikes
        last_3_directions = [s["direction"] for s in last_3]

        if all(d == "UP" for d in last_3_directions):
            last_3_direction = "UP"
        elif all(d == "DOWN" for d in last_3_directions):
            last_3_direction = "DOWN"
        else:
            last_3_direction = "MIXED"

        # Проверяем последовательные всплески (минимум 2 подряд)
        consecutive_up = len(last_3_directions) >= 2 and last_3_directions[-1] == "UP" and last_3_directions[-2] == "UP"
        consecutive_down = len(last_3_directions) >= 2 and last_3_directions[-1] == "DOWN" and last_3_directions[-2] == "DOWN"

        # Определяем общий импульс
        if up_spikes >= down_spikes * 1.5:  # В 1.5 раза больше всплесков вверх
            momentum = "BULLISH"
        elif down_spikes >= up_spikes * 1.5:  # В 1.5 раза больше всплесков вниз
            momentum = "BEARISH"
        else:
            momentum = "NEUTRAL"

        return {
            "up_spikes": up_spikes,
            "down_spikes": down_spikes,
            "last_3_direction": last_3_direction,
            "consecutive_up": consecutive_up,
            "consecutive_down": consecutive_down,
            "momentum": momentum
        }

    def should_enter_on_pullback(self, main_signal: str) -> tuple[bool, str]:
        """
        🎯 КЛЮЧЕВОЙ МЕТОД: Определяет, стоит ли входить в позицию.

        Логика "Адаптивного гибрида":
        - Если основной сигнал LONG и идет откат ВНИЗ (2-3 всплеска) → ВХОДИМ
        - Если основной сигнал LONG и резкое ускорение ВВЕРХ (3+ всплеска) → НЕ ВХОДИМ (ждем отката)
        - Аналогично для SHORT

        Args:
            main_signal: Основной сигнал ("LONG", "SHORT", "HOLD")

        Returns:
            (should_enter: bool, reason: str)
        """
        if main_signal == "HOLD":
            return False, "Main signal is HOLD"

        # ПРОВЕРКА МИНИМАЛЬНОГО НАКОПЛЕНИЯ ДАННЫХ (защита от холодного старта)
        recent_spikes = self.get_recent_spikes(seconds=300)
        if len(recent_spikes) < 3:
            log_info(self.user_id,
                    f"⏸️ SpikeDetector ({self.symbol}): Недостаточно данных ({len(recent_spikes)}/3 всплесков), накапливаю историю...",
                    "SpikeDetector")
            return False, f"⏸️ Недостаточно данных для анализа ({len(recent_spikes)}/3 всплесков)"

        # Анализируем импульс за последние 5 минут
        momentum_data = self.analyze_momentum(seconds=300)

        up_spikes = momentum_data["up_spikes"]
        down_spikes = momentum_data["down_spikes"]
        consecutive_up = momentum_data["consecutive_up"]
        consecutive_down = momentum_data["consecutive_down"]
        last_3_direction = momentum_data["last_3_direction"]
        momentum = momentum_data["momentum"]

        # Логируем анализ
        log_debug(self.user_id,
                 f"📊 Momentum {self.symbol}: {momentum} (⬆️{up_spikes} vs ⬇️{down_spikes}), "
                 f"последние 3: {last_3_direction}, consecutive_up={consecutive_up}, consecutive_down={consecutive_down}",
                 "SpikeDetector")

        # ========== ЛОГИКА ДЛЯ LONG ==========
        if main_signal == "LONG":
            # Сценарий 1: Откат в восходящем тренде (ЛУЧШИЙ вход для LONG)
            if consecutive_down and up_spikes > down_spikes:
                return True, f"✅ Откат в восходящем тренде ({down_spikes} всплесков ВНИЗ, но общий импульс {momentum})"

            # Сценарий 2: Подтверждение тренда (сильный вход)
            if momentum == "BULLISH":
                return True, f"✅ Бычий импульс подтверждает LONG ({up_spikes} всплесков ВВЕРХ)"

            # Сценарий 3: Резкое ускорение ВВЕРХ (цена убежала, ждем отката)
            if consecutive_up and last_3_direction == "UP" and len(self.get_recent_spikes(180)) >= 3:
                return False, f"⏸️ Цена резко ускорилась ВВЕРХ ({up_spikes} всплесков), ждем отката"

            # Сценарий 4: Сильный медвежий импульс против LONG
            if momentum == "BEARISH":
                return False, f"⏸️ Медвежий импульс против LONG ({down_spikes} всплесков ВНИЗ)"

            # Нейтральный случай - входим
            return True, f"✅ LONG без сильных противоречий (импульс: {momentum})"

        # ========== ЛОГИКА ДЛЯ SHORT ==========
        elif main_signal == "SHORT":
            # Сценарий 1: Откат в нисходящем тренде (ЛУЧШИЙ вход для SHORT)
            if consecutive_up and down_spikes > up_spikes:
                return True, f"✅ Откат в нисходящем тренде ({up_spikes} всплесков ВВЕРХ, но общий импульс {momentum})"

            # Сценарий 2: Подтверждение тренда (сильный вход)
            if momentum == "BEARISH":
                return True, f"✅ Медвежий импульс подтверждает SHORT ({down_spikes} всплесков ВНИЗ)"

            # Сценарий 3: Резкое ускорение ВНИЗ (цена убежала, ждем отката)
            if consecutive_down and last_3_direction == "DOWN" and len(self.get_recent_spikes(180)) >= 3:
                return False, f"⏸️ Цена резко ускорилась ВНИЗ ({down_spikes} всплесков), ждем отката"

            # Сценарий 4: Сильный бычий импульс против SHORT
            if momentum == "BULLISH":
                return False, f"⏸️ Бычий импульс против SHORT ({up_spikes} всплесков ВВЕРХ)"

            # Нейтральный случай - входим
            return True, f"✅ SHORT без сильных противоречий (импульс: {momentum})"

        return False, "Unknown signal"

    def get_last_price(self) -> Optional[Decimal]:
        """Возвращает последнюю цену из истории."""
        return self.price_history[-1] if self.price_history else None

    def reset(self):
        """Сбрасывает историю (используется при перезапуске стратегии)."""
        self.price_history.clear()
        self.spike_history.clear()
        log_info(self.user_id, f"🔄 SpikeDetector для {self.symbol} сброшен", "SpikeDetector")