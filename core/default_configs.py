# core/default_configs.py

from typing import Dict, Any
from decimal import Decimal

class DefaultConfigs:
    @staticmethod
    def get_global_config() -> Dict[str, Any]:
        """Глобальная конфигурация пользователя."""
        return {
            "max_simultaneous_trades": 3,
            "max_daily_loss_usdt": 10.0,
            "watchlist_symbols": ["BTCUSDT", "ETHUSDT", "SOLUSDT"],
            "enable_notifications": True,
            "notify_on_trade_open": True,
            "notify_on_trade_close": True,
            "config_version": "2.1.0" # Обновляем версию
        }


    @staticmethod
    async def create_default_user_config(user_id: int):
        """Создание конфигураций по умолчанию для пользователя."""
        # Сохраняем глобальную конфигурацию
        await redis_manager.save_config(user_id, ConfigType.GLOBAL, all_configs["global_config"])

        # Сохраняем конфигурации стратегий
        await redis_manager.save_config(user_id, ConfigType.STRATEGY_IMPULSE_TRAILING,
                                        all_configs["strategy_configs"]["impulse_trailing"])
        await redis_manager.save_config(user_id, ConfigType.STRATEGY_SIGNAL_SCALPER,
                                        all_configs["strategy_configs"]["signal_scalper"])


    @staticmethod
    def get_impulse_trailing_config() -> Dict[str, Any]:
        """Конфигурация для асимметричной стратегии 'Риск-Реверсал'."""
        return {
            # --- Общие настройки, видимые пользователю ---
            "is_enabled": True,
            "leverage": 2,
            "order_amount": 50.0,
            "analysis_timeframe": "5m",

            # --- Параметры для ЛОНГА (консолидация + пробой) ---
            "long_ema_len": 100,
            "long_bb_len": 20,
            "long_bb_width_thresh": 0.02,
            "long_consol_bars": 10,
            "long_breakout_buffer": 0.0005,  # Снижено для более раннего входа

            # --- НОВЫЕ ПАРАМЕТРЫ ДЛЯ РАННЕГО ВХОДА ---
            "momentum_threshold": 1.0,  # Снижено с 1.5 для более чувствительного входа
            "support_distance_percent": 0.008,  # Увеличено с 0.005 для более широкого захвата отскоков

            # --- Параметры для ШОРТА (паническая свеча) ---
            "short_ret_lookback": 50,
            "short_panic_sigma_k": 2.5,
            "short_vol_ma": 20,
            "short_vol_ratio_min": 1.2,

            # --- Риск-менеджмент (на основе ATR) ---
            "risk_atr_len": 14,
            "long_sl_atr": 1.2,      # SL для лонга = цена - (ATR * N)
            "long_tp_atr": 1.8,      # TP для лонга = цена + (ATR * N)
            "short_sl_atr": 0.8,     # SL для шорта = цена + (ATR * N)
            "short_tp_atr": 0.8,     # TP для шорта = цена - (ATR * N)
            "trailing_sl_atr": 1.5,   # Трейлинг SL = пик - (ATR * N)

            # --- НОВЫЕ ПАРАМЕТРЫ АГРЕССИВНОГО ТРЕЙЛИНГА (В USDT) ---
            # 1. Начальный стоп-лосс (в USDT)
            "initial_sl_usdt": 20.91,  # SL при убытке в 1.5 USDT от суммы ордера

            # 2. Минимальная прибыль для активации трейлинга (в USDT)
            "min_profit_activation_usdt": 5.0,  # Активация трейлинга при прибыли в +5.0 USDT

            # 3. Расстояние трейлинг-стопа от пика (в USDT)
            "trailing_distance_usdt": 11.77,  # Стоп будет отставать от пика

            # 4. Порог отката от пика для закрытия (в USDT)
            "pullback_close_usdt": 5.87,  # Закрытие, если цена откатила от пика
        }

    @staticmethod
    def get_signal_scalper_config() -> Dict[str, Any]:
        """Конфигурация для Signal Scalper Strategy (бывший Grid Scalping)."""
        return {
            # --- Основные ---
            "is_enabled": True,
            "order_amount": 100.0,
            "leverage": 2,
            "analysis_timeframe": "5m",

            # --- Управление выходом ---
            "min_profit_usd": 1.5,
            "trailing_pullback_usd": 1.0,  # Откат от пика прибыли для закрытия
            "max_loss_usd": 15.0,  # Максимальный убыток в долларах (стоп-лосс)

            # --- НОВАЯ СИСТЕМА УСРЕДНЕНИЯ ---
            "enable_averaging": True,          # Включить усреднение позиции
            "averaging_trigger_percent": 1.0,  # При каком проценте убытка усредняться (1%)
            "max_averaging_count": 3,          # Максимальное количество усреднений
            "averaging_multiplier": 1.0,       # Множитель для размера усредняющего ордера (1.0 = тот же размер)

            # --- ПРОГРЕССИВНОЕ УСРЕДНЕНИЕ ---
            "averaging_mode": "progressive",            # "fixed" или "progressive"
            "progressive_multiplier": 1.5,             # Множитель роста (1.5 = +50% каждый раз)

            # --- ТЕХНИЧЕСКИЕ ФИЛЬТРЫ ДЛЯ УСРЕДНЕНИЯ ---
            "averaging_rsi_filter": True,              # Использовать RSI фильтр
            "averaging_rsi_oversold": 36,              # RSI для LONG усреднения (перепроданность)
            "averaging_rsi_overbought": 59,            # RSI для SHORT усреднения (перекупленность)

            # --- Параметры ТА ---
            "EMA_SHORT": 21,
            "EMA_LONG": 50,
            "RSI_PERIOD": 14,
            "RSI_NEUTRAL_MIN": 30,      # Минимум нейтральной зоны RSI (расширено)
            "RSI_NEUTRAL_MAX": 70       # Максимум нейтральной зоны RSI (расширено)
        }


    @staticmethod
    def get_all_default_configs() -> Dict[str, Dict[str, Any]]:
        """Получение всех конфигураций по умолчанию."""
        return {
            "global_config": DefaultConfigs.get_global_config(),
            "strategy_configs": {
                "impulse_trailing": DefaultConfigs.get_impulse_trailing_config(),
                "signal_scalper": DefaultConfigs.get_signal_scalper_config()
            }
        }
