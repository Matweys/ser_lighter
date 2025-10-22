"""
📌 Детектор резких движений (Spike Detector)

Отслеживает мгновенные всплески волатильности на 1-минутных свечах.
Используется для определения оптимальной точки входа в сделку.

Логика:
- Хранит последние N свечей (1 минута)
- Детектирует всплески больше порога (например, 0.1%)
- Предоставляет анализ последних всплесков для принятия решений
"""

from decimal import Decimal
from collections import deque
from typing import Optional, List, Dict
from datetime import datetime

from core.logger import log_info, log_debug


class SpikeDetector:
    """
    Детектор всплесков цены для одного символа.
    Каждый экземпляр работает НЕЗАВИСИМО для своего символа.
    """

    def __init__(self, user_id: int, symbol: str, lookback: int = 50, threshold: float = 0.0008):
        """
        Args:
            user_id: ID пользователя (для логирования)
            symbol: Символ для отслеживания (например, "BTCUSDT")
            lookback: Количество свечей для хранения в истории
            threshold: Порог для детекции всплеска (0.0005 = 0.05%)
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

    def get_recent_spikes(self, seconds: int = 600) -> List[Dict]:
        """
        Возвращает всплески за последние N секунд.

        Args:
            seconds: Временное окно в секундах (по умолчанию 10 минут = 600 сек)

        Returns:
            Список всплесков: [{"timestamp", "direction", "magnitude", "price"}, ...]
        """
        current_time = datetime.now().timestamp()
        cutoff_time = current_time - seconds

        return [
            spike for spike in self.spike_history
            if spike["timestamp"] >= cutoff_time
        ]

    def analyze_momentum(self, seconds: int = 600) -> Dict:
        """
        Анализирует импульс (momentum) за последние N секунд.

        Args:
            seconds: Временное окно (по умолчанию 10 минут)

        Returns:
            {
                "up_spikes": количество всплесков вверх,
                "down_spikes": количество всплесков вниз,
                "overall_direction": общее направление ВСЕХ всплесков ("UP", "DOWN", "MIXED", None),
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
                "overall_direction": None,
                "consecutive_up": False,
                "consecutive_down": False,
                "momentum": "NEUTRAL"
            }

        # Считаем всплески по направлениям
        up_spikes = sum(1 for s in recent_spikes if s["direction"] == "UP")
        down_spikes = sum(1 for s in recent_spikes if s["direction"] == "DOWN")

        # Получаем направления ВСЕХ всплесков за период
        all_directions = [s["direction"] for s in recent_spikes]

        # Определяем общее направление на основе ВСЕХ всплесков
        if up_spikes >= down_spikes * 1.5:  # В 1.5 раза больше всплесков вверх
            overall_direction = "UP"
        elif down_spikes >= up_spikes * 1.5:  # В 1.5 раза больше всплесков вниз
            overall_direction = "DOWN"
        else:
            overall_direction = "MIXED"

        # Проверяем последовательные всплески (минимум 2 подряд в конце периода)
        consecutive_up = len(all_directions) >= 2 and all_directions[-1] == "UP" and all_directions[-2] == "UP"
        consecutive_down = len(all_directions) >= 2 and all_directions[-1] == "DOWN" and all_directions[-2] == "DOWN"

        # Определяем общий импульс (BULLISH/BEARISH/NEUTRAL)
        if up_spikes >= down_spikes * 1.5:
            momentum = "BULLISH"
        elif down_spikes >= up_spikes * 1.5:
            momentum = "BEARISH"
        else:
            momentum = "NEUTRAL"

        return {
            "up_spikes": up_spikes,
            "down_spikes": down_spikes,
            "overall_direction": overall_direction,
            "consecutive_up": consecutive_up,
            "consecutive_down": consecutive_down,
            "momentum": momentum
        }

    def should_enter_on_pullback(self, main_signal: str) -> tuple[bool, str, str]:
        """
        🎯 КЛЮЧЕВОЙ МЕТОД: Определяет, стоит ли входить в позицию.

        Улучшенная логика с многоуровневым анализом:
        - Анализирует 3 временных окна (3мин, 5мин, 10мин) для точности
        - Требует минимум 6 всплесков для статистически значимого анализа
        - Определяет перевес направления по соотношению UP/DOWN всплесков
        - Ищет сильные противоположные всплески >0.35% для разворота сигнала

        Args:
            main_signal: Основной сигнал ("LONG", "SHORT", "HOLD")

        Returns:
            (should_enter: bool, final_signal: str, reason: str)
        """
        if main_signal == "HOLD":
            return False, "HOLD", "Main signal is HOLD"

        # ПРОВЕРКА МИНИМАЛЬНОГО НАКОПЛЕНИЯ ДАННЫХ (защита от холодного старта)
        # УЛУЧШЕНО: Требуем минимум 6 всплесков для надёжного анализа
        recent_spikes_10min = self.get_recent_spikes(seconds=600)  # 10 минут
        if len(recent_spikes_10min) < 6:
            # УЛУЧШЕНО: Показываем размеры всплесков для диагностики
            spikes_info = ", ".join([f"{s['pct_change']*100:.2f}%" for s in recent_spikes_10min]) if recent_spikes_10min else "нет данных"
            log_info(self.user_id,
                    f"⏸️ SpikeDetector ({self.symbol}): Недостаточно данных ({len(recent_spikes_10min)}/6 всплесков). "
                    f"Текущие всплески: [{spikes_info}]. Накапливаю историю...",
                    "SpikeDetector")
            return False, main_signal, f"⏸️ Недостаточно данных для анализа ({len(recent_spikes_10min)}/6 всплесков)"

        # ========== МНОГОУРОВНЕВЫЙ АНАЛИЗ ИМПУЛЬСА ==========
        # Анализируем 3 временных окна для максимальной точности
        momentum_3min = self.analyze_momentum(seconds=180)   # Краткосрочный (3 мин)
        momentum_5min = self.analyze_momentum(seconds=300)   # Среднесрочный (5 мин)
        momentum_10min = self.analyze_momentum(seconds=600)  # Долгосрочный (10 мин)

        # Используем долгосрочное окно как основное (для совместимости с текущим кодом)
        up_spikes = momentum_10min["up_spikes"]
        down_spikes = momentum_10min["down_spikes"]
        consecutive_up = momentum_10min["consecutive_up"]
        consecutive_down = momentum_10min["consecutive_down"]
        overall_direction = momentum_10min["overall_direction"]
        momentum = momentum_10min["momentum"]

        # НОВАЯ ЛОГИКА: Определяем перевес по соотношению UP/DOWN
        total_spikes = up_spikes + down_spikes
        if total_spikes > 0:
            up_ratio = (up_spikes / total_spikes) * 100  # Процент всплесков ВВЕРХ
            down_ratio = (down_spikes / total_spikes) * 100  # Процент всплесков ВНИЗ
        else:
            up_ratio = down_ratio = 0

        # Определяем СИЛЬНЫЙ перевес (>60% в одну сторону)
        strong_bullish_bias = up_ratio >= 60  # 60%+ всплесков вверх
        strong_bearish_bias = down_ratio >= 60  # 60%+ всплесков вниз

        # Логируем детальный анализ
        log_info(self.user_id,
                f"📊 Spike Detector ({len(recent_spikes_10min)} свечей, {total_spikes} всплесков за 10мин): "
                f"⬆️{up_spikes} ({up_ratio:.0f}%) vs ⬇️{down_spikes} ({down_ratio:.0f}%), "
                f"импульс 3m/5m/10m = {momentum_3min['momentum']}/{momentum_5min['momentum']}/{momentum_10min['momentum']}",
                "SpikeDetector")

        # ========== ПРИОРИТЕТ: ПРОВЕРКА СИЛЬНЫХ ПРОТИВОПОЛОЖНЫХ ВСПЛЕСКОВ ==========
        # Порог для "сильного" всплеска: 0.35% (0.0035)
        strong_spike_threshold = Decimal('0.0035')

        # Ищем сильные противоположные всплески за последние 10 минут
        strong_opposite_spikes = []
        reversed_signal = None

        for spike in recent_spikes_10min:
            spike_magnitude = Decimal(str(spike["magnitude"]))

            # Проверяем силу всплеска
            if spike_magnitude >= strong_spike_threshold:
                # Проверяем, противоположен ли он основному сигналу
                if main_signal == "SHORT" and spike["direction"] == "UP":
                    # Сильный всплеск ВВЕРХ при сигнале SHORT
                    strong_opposite_spikes.append(spike)
                    reversed_signal = "LONG"
                elif main_signal == "LONG" and spike["direction"] == "DOWN":
                    # Сильный всплеск ВНИЗ при сигнале LONG
                    strong_opposite_spikes.append(spike)
                    reversed_signal = "SHORT"

        # Если обнаружено ДВА или более сильных противоположных всплеска - РАЗВОРАЧИВАЕМ сигнал
        if len(strong_opposite_spikes) >= 3 and reversed_signal:
            direction_emoji = "📈" if strong_opposite_spikes[-1]["direction"] == "UP" else "📉"
            last_magnitude_pct = strong_opposite_spikes[-1]["magnitude"] * 100

            log_info(self.user_id,
                    f"🔄 РАЗВОРОТ СИГНАЛА! {direction_emoji} Обнаружено {len(strong_opposite_spikes)} сильных всплесков {strong_opposite_spikes[-1]['direction']} "
                    f"(последний: {last_magnitude_pct:.2f}%, порог >{strong_spike_threshold*100:.2f}%). "
                    f"Меняю {main_signal} → {reversed_signal}!",
                    "SpikeDetector")

            return True, reversed_signal, (f"🔄 РАЗВОРОТ: {direction_emoji} {len(strong_opposite_spikes)} всплеска {strong_opposite_spikes[-1]['direction']} "
                          f"(последний {last_magnitude_pct:.2f}%) развернули {main_signal} → {reversed_signal}")

        # Логируем анализ
        log_debug(self.user_id,
                 f"📊 Momentum {self.symbol}: {momentum} (⬆️{up_spikes} vs ⬇️{down_spikes}), "
                 f"общее направление: {overall_direction}, consecutive_up={consecutive_up}, consecutive_down={consecutive_down}",
                 "SpikeDetector")

        # ========== ПРОДВИНУТАЯ ЛОГИКА ДЛЯ LONG ==========
        if main_signal == "LONG":
            # 🎯 ПРИОРИТЕТ 1: Полное подтверждение на всех таймфреймах (САМЫЙ СИЛЬНЫЙ СИГНАЛ)
            all_bullish = (momentum_3min["momentum"] == "BULLISH" and
                          momentum_5min["momentum"] == "BULLISH" and
                          momentum_10min["momentum"] == "BULLISH")

            if all_bullish:
                return True, "LONG", f"🚀 СИЛЬНЕЙШИЙ LONG: все таймфреймы BULLISH (3m/5m/10m согласны)"

            # 🎯 ПРИОРИТЕТ 2: Идеальный вход на откате в восходящем тренде
            perfect_pullback_entry = (
                momentum_10min["momentum"] == "BULLISH" and           # Долгосрочный тренд вверх
                momentum_3min["down_spikes"] >= 2 and                 # Краткосрочный откат вниз
                momentum_3min["momentum"] != "BEARISH"                # Но НЕ медвежий разворот
            )

            if perfect_pullback_entry:
                return True, "LONG", f"🎯 ИДЕАЛЬНЫЙ ВХОД: откат вниз в восходящем тренде (10m=BULLISH, 3m откат)"

            # Сценарий 3: Обнаружение раннего разворота (БЛОКИРУЕМ ВХОД)
            early_reversal_to_bearish = (
                momentum_3min["momentum"] == "BEARISH" and            # Краткосрочно медвежий
                momentum_10min["momentum"] == "BULLISH"               # Долгосрочно бычий
            )

            if early_reversal_to_bearish:
                return False, "LONG", f"⚠️ Ранний признак разворота вниз (3m=BEARISH, 10m=BULLISH)"

            # Сценарий 4: Стандартное подтверждение тренда
            if momentum == "BULLISH":
                return True, "LONG", f"✅ Бычий импульс подтверждает LONG ({up_spikes} всплесков ВВЕРХ)"

            # Сценарий 5: Резкое ускорение ВВЕРХ (цена убежала, ждем отката)
            if consecutive_up and overall_direction == "UP" and len(self.get_recent_spikes(180)) >= 3:
                return False, "LONG", f"⏸️ Цена резко ускорилась ВВЕРХ ({up_spikes} всплесков), ждем отката"

            # Сценарий 6: Сильный медвежий импульс против LONG
            if momentum == "BEARISH":
                return False, "LONG", f"⏸️ Медвежий импульс против LONG ({down_spikes} всплесков ВНИЗ)"

            # Нейтральный случай - входим
            return True, "LONG", f"✅ LONG без сильных противоречий (импульс: {momentum})"

        # ========== ПРОДВИНУТАЯ ЛОГИКА ДЛЯ SHORT ==========
        elif main_signal == "SHORT":
            # 🎯 ПРИОРИТЕТ 1: Полное подтверждение на всех таймфреймах (САМЫЙ СИЛЬНЫЙ СИГНАЛ)
            all_bearish = (momentum_3min["momentum"] == "BEARISH" and
                          momentum_5min["momentum"] == "BEARISH" and
                          momentum_10min["momentum"] == "BEARISH")

            if all_bearish:
                return True, "SHORT", f"🚀 СИЛЬНЕЙШИЙ SHORT: все таймфреймы BEARISH (3m/5m/10m согласны)"

            # 🎯 ПРИОРИТЕТ 2: Идеальный вход на откате в нисходящем тренде
            perfect_pullback_entry = (
                momentum_10min["momentum"] == "BEARISH" and           # Долгосрочный тренд вниз
                momentum_3min["up_spikes"] >= 2 and                   # Краткосрочный откат вверх
                momentum_3min["momentum"] != "BULLISH"                # Но НЕ бычий разворот
            )

            if perfect_pullback_entry:
                return True, "SHORT", f"🎯 ИДЕАЛЬНЫЙ ВХОД: откат вверх в нисходящем тренде (10m=BEARISH, 3m откат)"

            # Сценарий 3: Обнаружение раннего разворота (БЛОКИРУЕМ ВХОД)
            early_reversal_to_bullish = (
                momentum_3min["momentum"] == "BULLISH" and            # Краткосрочно бычий
                momentum_10min["momentum"] == "BEARISH"               # Долгосрочно медвежий
            )

            if early_reversal_to_bullish:
                return False, "SHORT", f"⚠️ Ранний признак разворота вверх (3m=BULLISH, 10m=BEARISH)"

            # Сценарий 4: Стандартное подтверждение тренда
            if momentum == "BEARISH":
                return True, "SHORT", f"✅ Медвежий импульс подтверждает SHORT ({down_spikes} всплесков ВНИЗ)"

            # Сценарий 5: Резкое ускорение ВНИЗ (цена убежала, ждем отката)
            if consecutive_down and overall_direction == "DOWN" and len(self.get_recent_spikes(180)) >= 3:
                return False, "SHORT", f"⏸️ Цена резко ускорилась ВНИЗ ({down_spikes} всплесков), ждем отката"

            # Сценарий 6: Сильный бычий импульс против SHORT
            if momentum == "BULLISH":
                return False, "SHORT", f"⏸️ Бычий импульс против SHORT ({up_spikes} всплесков ВВЕРХ)"

            # Нейтральный случай - входим
            return True, "SHORT", f"✅ SHORT без сильных противоречий (импульс: {momentum})"

        return False, main_signal, "Unknown signal"

    def get_last_price(self) -> Optional[Decimal]:
        """Возвращает последнюю цену из истории."""
        return self.price_history[-1] if self.price_history else None

    def reset(self):
        """Сбрасывает историю (используется при перезапуске стратегии)."""
        self.price_history.clear()
        self.spike_history.clear()
        log_info(self.user_id, f"🔄 SpikeDetector для {self.symbol} сброшен", "SpikeDetector")