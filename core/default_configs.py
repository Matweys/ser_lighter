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
            "config_version": "2.2.1" # Обновлена: исправлен множитель усреднения на 2.0
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
        await redis_manager.save_config(user_id, ConfigType.STRATEGY_IMPULSE_TRAILING,
                                        all_configs["strategy_configs"]["impulse_trailing"])
        await redis_manager.save_config(user_id, ConfigType.STRATEGY_SIGNAL_SCALPER,
                                        all_configs["strategy_configs"]["signal_scalper"])


    @staticmethod
    def get_impulse_trailing_config() -> Dict[str, Any]:
        """Конфигурация для асимметричной стратегии 'Риск-Реверсал'."""
        return {
            "config_version": "2.2.1",  # Версия конфигурации стратегии
            # --- Общие настройки, видимые пользователю ---
            "is_enabled": True,
            "leverage": 2,
            "order_amount": 50.0,
            "analysis_timeframe": "5m",

            # --- Параметры для ЛОНГА (консолидация + пробой) ---
            "long_ema_len": 100,           # Оптимально для 5m: ~8 часов истории
            "long_bb_len": 15,             # УСКОРЕНО: с 20 до 15 для более быстрой реакции на волатильность
            "long_bb_width_thresh": 0.025, # РАСШИРЕНО: с 2% до 2.5% для захвата больше консолидаций
            "long_consol_bars": 6,         # АГРЕССИВНО: с 10 до 6 баров (30 минут вместо 50)
            "long_breakout_buffer": 0.0002,  # АГРЕССИВНО: 0.02% = ранний вход при начале пробоя

            # --- НОВЫЕ ПАРАМЕТРЫ ДЛЯ РАННЕГО ВХОДА ---
            "momentum_threshold": 0.5,  # АГРЕССИВНО: 0.5 = ловим слабые импульсы на раннем этапе
            "support_distance_percent": 0.015,  # РАСШИРЕНО: 1.5% = широкий захват отскоков от поддержки

            # --- Параметры для ШОРТА (паническая свеча) ---
            "short_ret_lookback": 35,      # УСКОРЕНО: с 50 до 35 свечей (~3 часа) для более быстрого обнаружения паники
            "short_panic_sigma_k": 2.0,    # АГРЕССИВНО: с 2.5 до 2.0 сигма для раннего входа в панические движения
            "short_vol_ma": 20,            # Оптимально: стандартный период для MA объёма
            "short_vol_ratio_min": 1.1,    # АГРЕССИВНО: с 1.2 до 1.1 (10% превышение) для раннего обнаружения импульсов

            # --- Риск-менеджмент (на основе ATR) ---
            "risk_atr_len": 14,

            # --- НОВЫЕ ПАРАМЕТРЫ АГРЕССИВНОГО ТРЕЙЛИНГА (В USDT) ---
            # 1. Начальный стоп-лосс (в USDT)
            "initial_sl_usdt": 20.91,  # SL при убытке в 1.5 USDT от суммы ордера

            # 2. Минимальная прибыль для активации трейлинга (в USDT)
            "min_profit_activation_usdt": 3.0,  # Активация трейлинга при прибыли в +5.0 USDT

            # 3. Расстояние трейлинг-стопа от пика (в USDT)
            "trailing_distance_usdt": 11.77,  # Стоп будет отставать от пика

            # 4. Порог отката от пика для закрытия (в USDT)
            "pullback_close_usdt": 3.00,  # Закрытие, если цена откатила от пика
        }

    @staticmethod
    def get_signal_scalper_config() -> Dict[str, Any]:
        """Конфигурация для Signal Scalper Strategy (бывший Grid Scalping)."""
        return {
            "config_version": "2.2.1",  # Версия конфигурации стратегии
            # --- Основные ---
            "is_enabled": True,
            "order_amount": 100.0,
            "leverage": 2,
            "analysis_timeframe": "5m",

            # --- Управление выходом ---
            "min_profit_usd": 1.0,
            "max_loss_usd": 15.0,  # Максимальный убыток в долларах (стоп-лосс)

            # --- НОВАЯ СИСТЕМА УСРЕДНЕНИЯ (ОДИНОЧНОЕ УДВОЕНИЕ) ---
            "enable_averaging": True,                    # Включить усреднение позиции
            "averaging_trigger_loss_percent": 10.0,       # Триггер усреднения: убыток -3% от маржи
            "averaging_multiplier": 2.0,                 # Удвоение суммы при усреднении
            "max_averaging_count": 1,                    # Только ОДНО усреднение
            "averaging_stop_loss_percent": 11.0,          # Программный SL после усреднения: -4% от маржи (на 1% выше триггера)



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
                "impulse_trailing": DefaultConfigs.get_impulse_trailing_config(),
                "signal_scalper": DefaultConfigs.get_signal_scalper_config()
            }
        }
