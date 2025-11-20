# core/default_configs.py

from typing import Dict, Any

class DefaultConfigs:
    @staticmethod
    def get_global_config() -> Dict[str, Any]:
        """Глобальная конфигурация пользователя."""
        return {
            "max_concurrent_trades": 5,
            "watchlist_symbols": ["BTCUSDT"],
            "enable_notifications": True,
            "notify_on_trade_open": True,
            "notify_on_trade_close": True
        }


    @staticmethod
    async def create_default_user_config(user_id: int):
        """Создание конфигураций по умолчанию для пользователя."""
        from cache.redis_manager import redis_manager
        from core.enums import ConfigType

        # Получаем все конфигурации
        all_configs = DefaultConfigs.get_all_default_configs()

        # Сохраняем глобальную конфигурацию
        await redis_manager.save_config(user_id, ConfigType.GLOBAL, all_configs["global_config"])

        # Сохраняем конфигурации стратегий
        await redis_manager.save_config(user_id, ConfigType.STRATEGY_SIGNAL_SCALPER,
                                        all_configs["strategy_configs"]["signal_scalper"])


    @staticmethod
    def get_signal_scalper_config() -> Dict[str, Any]:
        """Конфигурация для Signal Scalper Strategy (бывший Grid Scalping)."""
        return {
            # --- Основные ---
            "is_enabled": True,
            "order_amount": 200.0,
            "leverage": 2,
            "analysis_timeframe": "5m",

            # --- КООРДИНАТОР (Multi-Account) ---
            "stuck_threshold_percent": 8.0,              # Порог застревания: если PnL < -8% от маржи → активация Бота 2

            # --- STOP LOSS ---
            "enable_stop_loss": True,                    # Включить/выключить стоп-лосс
            "averaging_stop_loss_percent": 55.0,         # Программный SL: % от маржи (работает только если enable_stop_loss=True)

            # --- УСРЕДНЕНИЕ #1 (Детектор застрявшей цены) ---
            "enable_stagnation_detector": True,          # Включить детектор застрявшей цены (первое усреднение)
            "stagnation_trigger_min_percent": 15.0,      # Триггер детектора: убыток от 15% маржи
            "stagnation_trigger_max_percent": 20.0,      # Триггер детектора: убыток до 20% маржи
            "stagnation_check_interval_seconds": 180,     # Время наблюдения за застрявшей ценой (секунды)
            "stagnation_averaging_multiplier": 1.0,      # Множитель усреднения (x1 = равная сумма основному ордеру)
            "stagnation_averaging_leverage": 1,          # Плечо для усреднения (x1 = без дополнительного плеча)

            # --- УСРЕДНЕНИЕ #2 (Основное усреднение) ---
            "enable_averaging": True,                    # Включить основное усреднение позиции (второе усреднение)
            "averaging_trigger_loss_percent": 15.0,      # Триггер основного усреднения: убыток % от маржи
            "averaging_multiplier": 1.0,                 # Множитель суммы при усреднении (БЕЗ ПЛЕЧА!)
            "max_averaging_count": 1,                    # Только ОДНО основное усреднение



            # --- Параметры ТА ---
            "EMA_SHORT": 21,
            "EMA_LONG": 50,
            "RSI_PERIOD": 14,
            "RSI_NEUTRAL_MIN": 30,      # Минимум нейтральной зоны RSI (расширено) 30
            "RSI_NEUTRAL_MAX": 70       # Максимум нейтральной зоны RSI (расширено) 70
        }



    @staticmethod
    def get_all_default_configs() -> Dict[str, Dict[str, Any]]:
        """Получение всех конфигураций по умолчанию."""
        return {
            "global_config": DefaultConfigs.get_global_config(),
            "strategy_configs": {
                "signal_scalper": DefaultConfigs.get_signal_scalper_config()
            }
        }
