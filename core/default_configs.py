# core/default_configs.py

from typing import Dict, Any
from decimal import Decimal

class DefaultConfigs:
    @staticmethod
    def get_global_config() -> Dict[str, Any]:
        """Глобальная конфигурация пользователя."""
        return {
            "max_concurrent_trades": 5,
            "max_daily_loss_usdt": 10.0,
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
        await redis_manager.save_config(user_id, ConfigType.STRATEGY_FLASH_DROP_CATCHER,
                                        all_configs["strategy_configs"]["flash_drop_catcher"])


    @staticmethod
    def get_signal_scalper_config() -> Dict[str, Any]:
        """Конфигурация для Signal Scalper Strategy (бывший Grid Scalping)."""
        return {
            # --- Основные ---
            "is_enabled": True,
            "order_amount": 200.0,
            "leverage": 2,
            "analysis_timeframe": "5m",

            # ============================================================
            # ДЕТЕКТОР ЗАСТРЯВШЕЙ ЦЕНЫ (STAGNATION DETECTOR)
            # Легко отключить: установите enable_stagnation_detector = False
            # ============================================================
            "enable_stagnation_detector": True,                      # Включить детектор застрявшей цены
            "stagnation_check_interval_seconds": 30,                 # Время наблюдения
            "stagnation_ranges_percent": [                           # Диапазоны убытков (в % от маржи = order_amount × leverage)
                {"min": 15.0, "max": 20.0}
            ],
            "stagnation_averaging_multiplier": 1.0,                  # Множитель усреднения (x1 = равная сумма основному ордеру)
            "stagnation_averaging_leverage": 1,                      # Плечо для усреднения (x1 = без дополнительного плеча)
            # ============================================================

            # --- ОСНОВНОЕ УСРЕДНЕНИЕ ---
            "enable_averaging": True,                    # Включить основное усреднение позиции
            "averaging_trigger_loss_percent": 15.0,      # Триггер основного усреднения: убыток % от маржи
            "averaging_multiplier": 1.0,                 # УДвоение суммы при усреднении (БЕЗ ПЛЕЧА!)
            "max_averaging_count": 1,                    # Только ОДНО основное усреднение
            "averaging_stop_loss_percent": 45.0,         # Программный SL после усреднения: % от маржи



            # --- Параметры ТА ---
            "EMA_SHORT": 21,
            "EMA_LONG": 50,
            "RSI_PERIOD": 14,
            "RSI_NEUTRAL_MIN": 30,      # Минимум нейтральной зоны RSI (расширено) 30
            "RSI_NEUTRAL_MAX": 70       # Максимум нейтральной зоны RSI (расширено) 70
        }


    @staticmethod
    def get_flash_drop_catcher_config() -> Dict[str, Any]:
        """Конфигурация для Flash Drop Catcher Strategy."""
        return {
            # --- Основные параметры ---
            "is_enabled": False,
            "order_amount": 200.0,  # Сумма ордера в USDT
            "leverage": 2,  # Плечо

            # --- Параметры обнаружения падений ---
            "drop_percent": 7.0,  # Процент падения для сигнала (2% от среднего)
            "candle_history_size": 7,  # Количество свечей для расчета среднего

            # --- Параметры выхода ---
            "hard_stop_loss_usdt": -15.0,  # Жесткий стоп-лосс при -3 USDT
            # Trailing stop использует те же уровни, что и signal_scalper (динамические)
        }

    @staticmethod
    def get_all_default_configs() -> Dict[str, Dict[str, Any]]:
        """Получение всех конфигураций по умолчанию."""
        return {
            "global_config": DefaultConfigs.get_global_config(),
            "strategy_configs": {
                "signal_scalper": DefaultConfigs.get_signal_scalper_config(),
                "flash_drop_catcher": DefaultConfigs.get_flash_drop_catcher_config()
            }
        }
