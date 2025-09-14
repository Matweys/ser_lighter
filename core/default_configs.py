# core/default_configs.py

from typing import Dict, Any
from decimal import Decimal

class DefaultConfigs:
    @staticmethod
    def get_global_config() -> Dict[str, Any]:
        """Глобальная конфигурация пользователя."""
        return {
            "leverage": 3,
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
        await redis_manager.save_config(user_id, ConfigType.STRATEGY_GRID_SCALPING,
                                        all_configs["strategy_configs"]["grid_scalping"])


    @staticmethod
    def get_impulse_trailing_config() -> Dict[str, Any]:
        """Конфигурация для асимметричной стратегии 'Риск-Реверсал'."""
        return {
            # --- Общие настройки, видимые пользователю ---
            "is_enabled": True,
            "order_amount": 50.0,
            "analysis_timeframe": "5m",

            # --- Параметры для ЛОНГА (консолидация + пробой) ---
            "long_ema_len": 100,
            "long_bb_len": 20,
            "long_bb_width_thresh": 0.02,
            "long_consol_bars": 10,
            "long_breakout_buffer": 0.001,

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
            "trailing_sl_atr": 1.5   # Трейлинг SL = пик - (ATR * N)
        }

    @staticmethod
    def get_grid_scalping_config() -> Dict[str, Any]:
        """Конфигурация стратегии грид-скальпинга."""
        return {
            "is_enabled": True,
            "order_amount": 50.0,
            "max_averaging_orders": 3,      # Макс. ордеров на усреднение
            "profit_percent": 0.3,          # Процент для фиксации прибыли
            "stop_loss_percent": 1.0,       # Общий стоп-лосс от цены первого входа в %
        }


    @staticmethod
    def get_all_default_configs() -> Dict[str, Dict[str, Any]]:
        """Получение всех конфигураций по умолчанию."""
        return {
            "global_config": DefaultConfigs.get_global_config(),
            "strategy_configs": {
                "impulse_trailing": DefaultConfigs.get_impulse_trailing_config(),
                "grid_scalping": DefaultConfigs.get_grid_scalping_config()
            }
        }
