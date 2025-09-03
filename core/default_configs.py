# core/default_configs.py
"""
Конфигурации по умолчанию для многопользовательской торговой системы
Создает базовые настройки для новых пользователей
"""
from typing import Dict, Any
from decimal import Decimal


class DefaultConfigs:
    """
    Класс для управления конфигурациями по умолчанию
    
    Содержит:
    - Глобальные настройки пользователя
    - Настройки для каждого типа стратегии
    - Настройки риск-менеджмента
    - Настройки анализа рынка
    """
    
    @staticmethod
    def get_global_config() -> Dict[str, Any]:
        """
        Глобальная конфигурация пользователя
        
        Returns:
            Dict: Глобальные настройки
        """
        return {
            # Основные настройки
            "leverage": 2, # Значение по умолчанию для плеча
            "order_amount": 10.0,  # USDT на одну сделку
            "max_simultaneous_trades": 3,
            
            # Риск-менеджмент
            "risk_per_trade_percent": 1.0,  # 2% риска на сделку
            "max_daily_loss_usdt": 10.0,  # Максимальный суточный убыток в USDT
            "global_daily_drawdown_percent": 2.0,  # 10% максимальная дневная просадка
            "max_portfolio_exposure_percent": 50.0,  # 50% максимальная экспозиция
            "stop_loss_percent": 1.0,  # 3% стоп-лосс
            "take_profit_percent": 6.0,  # 6% тейк-профит (R:R = 1:2)
            
            # Watchlist символов для анализа
            "watchlist_symbols": [
                "BTCUSDT",
                "ETHUSDT",
                "SOLUSDT"
            ],

            # Настройки анализа
            "analysis_timeframes": ["15m", "1h", "4h"],
            "analysis_cooldown_minutes": 5,  # Минимальный интервал между анализами
            "min_signal_strength": 70,  # Минимальная сила сигнала для запуска стратегии
            
            # Настройки уведомлений
            "enable_notifications": True,
            "notify_on_trade_open": True,
            "notify_on_trade_close": True,
            "notify_on_risk_warning": True,
            
            # Дополнительные настройки
            "close_positions_on_stop": False,  # Закрывать ли позиции при остановке
            "auto_restart_strategies": True,   # Автоматический перезапуск стратегий
            "save_trade_history": True,        # Сохранять историю сделок
            
            # Временные настройки
            "session_timeout_hours": 24,      # Таймаут сессии в часах
            "config_cache_minutes": 5,        # Кэширование конфигурации
            
            # Версия конфигурации (для миграций)
            "config_version": "1.0.0"
        }
    
    @staticmethod
    def get_bidirectional_grid_config() -> Dict[str, Any]:
        """
        Конфигурация стратегии двунаправленной сетки
        
        Returns:
            Dict: Настройки стратегии
        """
        return {
            # Основные параметры сетки
            "grid_levels": 5,                    # Количество уровней сетки в каждую сторону
            "grid_spacing_percent": 1.0,         # Расстояние между уровнями в %
            "profit_percent": 0.5,               # Прибыль с каждого исполнения в %
            
            # Адаптивные настройки
            "adaptive_spacing": True,            # Адаптивное расстояние на основе ATR
            "min_spacing_percent": 0.2,          # Минимальное расстояние
            "max_spacing_percent": 2.0,          # Максимальное расстояние
            "atr_multiplier": 0.5,               # Множитель ATR для расчета spacing
            
            # Управление сеткой
            "auto_rebuild_grid": True,           # Автоматическая перестройка сетки
            "rebuild_threshold_percent": 80,     # Порог для перестройки (% от границ)
            "min_orders_for_rebuild": 3,         # Минимум ордеров для перестройки
            
            # Размеры позиций
            "position_size_percent": 10,         # % от order_amount на всю сетку
            "max_grid_exposure": 100.0,          # Максимальная экспозиция сетки в USDT
            
            # Условия запуска
            "required_market_conditions": ["STRONG_FLAT", "FLAT"],
            "min_signal_strength": 60,
            "max_volatility_threshold": 5.0,     # Максимальная волатильность для запуска
            
            # Дополнительные настройки
            "enable_profit_orders": True,        # Размещать ордера прибыли
            "grid_timeout_hours": 24,            # Таймаут работы сетки
            "save_grid_stats": True              # Сохранять статистику сетки
        }
    
    @staticmethod
    def get_impulse_trailing_config() -> Dict[str, Any]:
        """
        Конфигурация стратегии импульсного трейлинга
        
        Returns:
            Dict: Настройки стратегии
        """
        return {
            # Основные параметры
            "initial_stop_percent": 1.0,         # Начальный стоп-лосс в %
            "trailing_step_percent": 0.5,        # Шаг трейлинга в %
            "min_profit_for_trailing": 1.0,      # Минимальная прибыль для начала трейлинга
            
            # Импульсные настройки
            "impulse_threshold_percent": 3.0,    # Порог импульса в %
            "impulse_timeframe": "15m",          # Таймфрейм для определения импульса
            "volume_confirmation": True,         # Подтверждение объемом
            "min_volume_ratio": 1.5,             # Минимальное отношение объема к среднему
            
            # Управление позицией
            "position_size_percent": 50,         # % от order_amount
            "max_position_size": 50.0,           # Максимальный размер позиции в USDT
            "partial_close_enabled": True,       # Частичное закрытие позиций
            "partial_close_percent": 50,         # % позиции для частичного закрытия
            
            # Условия запуска
            "required_market_conditions": ["STRONG_TREND", "TREND"],
            "min_signal_strength": 75,
            "trend_confirmation_required": True,
            
            # Дополнительные фильтры
            "rsi_filter_enabled": True,          # Фильтр по RSI
            "rsi_oversold_threshold": 30,        # Порог перепроданности
            "rsi_overbought_threshold": 70,      # Порог перекупленности
            
            # Таймауты и лимиты
            "max_holding_time_hours": 12,        # Максимальное время удержания позиции
            "strategy_timeout_hours": 6,         # Таймаут работы стратегии
            "save_impulse_stats": True           # Сохранять статистику импульсов
        }
    
    @staticmethod
    def get_grid_scalping_config() -> Dict[str, Any]:
        """
        Конфигурация стратегии грид-скальпинга
        
        Returns:
            Dict: Настройки стратегии
        """
        return {
            # Основные параметры скальпинга
            "scalp_levels": 5,                   # Количество уровней скальпинга
            "scalp_spacing_percent": 0.5,        # Расстояние между уровнями в %
            "quick_profit_percent": 0.5,         # Быстрая прибыль в %
            
            # Адаптивные настройки
            "adaptive_to_spread": True,          # Адаптация к спреду
            "min_spread_multiplier": 2.0,        # Минимальный множитель спреда
            "volatility_adjustment": True,       # Корректировка по волатильности
            
            # Управление позициями
            "position_size_percent": 30,         # % от order_amount
            "max_scalp_exposure": 30.0,          # Максимальная экспозиция в USDT
            "fast_execution_mode": True,         # Режим быстрого исполнения
            
            # Условия запуска
            "required_market_conditions": ["WEAK_TREND", "FLAT"],
            "min_signal_strength": 65,
            "max_spread_threshold": 0.1,         # Максимальный спред для работы
            
            # Фильтры качества
            "liquidity_filter": True,            # Фильтр по ликвидности
            "min_volume_24h": 1000000,           # Минимальный объем за 24ч
            "order_book_depth_check": True,      # Проверка глубины стакана
            
            # Таймауты
            "order_timeout_seconds": 30,         # Таймаут ордера
            "strategy_timeout_hours": 2,         # Таймаут работы стратегии
            "save_scalp_stats": True             # Сохранять статистику скальпинга
        }
    
    @staticmethod
    def get_meta_strategist_config() -> Dict[str, Any]:
        """
        Конфигурация MetaStrategist
        
        Returns:
            Dict: Настройки анализатора
        """
        return {
            # Параметры анализа
            "analysis_timeframes": ["15m", "1h", "4h"],
            "analysis_interval_minutes": 5,
            "cooldown_between_analysis_minutes": 3,
            
            # Индикаторы
            "ema_periods": [9, 21, 50],
            "rsi_period": 14,
            "adx_period": 14,
            "atr_period": 14,
            "bollinger_period": 20,
            "bollinger_std": 2.0,
            
            # Пороги для определения трендов
            "strong_trend_threshold": 80,
            "trend_threshold": 60,
            "flat_threshold": 40,
            "adx_trend_threshold": 25,
            
            # Подтверждения сигналов
            "require_timeframe_confirmation": True,
            "min_confirming_timeframes": 2,
            "signal_strength_weights": {
                "15m": 0.3,
                "1h": 0.4,
                "4h": 0.3
            },
            
            # Фильтры
            "volume_filter_enabled": True,
            "min_volume_ratio": 1.2,
            "volatility_filter_enabled": True,
            "max_volatility_threshold": 10.0,
            
            # Настройки кэширования
            "cache_analysis_minutes": 2,
            "save_analysis_history": True,
            "max_history_records": 1000
        }
    
    @staticmethod
    def get_risk_manager_config() -> Dict[str, Any]:
        """
        Конфигурация RiskManager
        
        Returns:
            Dict: Настройки риск-менеджмента
        """
        return {
            # Основные лимиты
            "max_simultaneous_trades": 3,
            "max_daily_trades": 20,
            "max_portfolio_exposure_percent": 50.0,
            "global_daily_drawdown_percent": 10.0,
            
            # Расчет размера позиции
            "risk_per_trade_percent": 2.0,
            "use_atr_for_position_sizing": True,
            "atr_multiplier": 2.0,
            "min_position_size_usdt": 5.0,
            "max_position_size_percent": 10.0,
            
            # Мониторинг рисков
            "drawdown_warning_threshold": 7.0,   # Предупреждение при 7% просадке
            "risk_check_interval_minutes": 1,
            "emergency_stop_enabled": True,
            
            # Корреляционные риски
            "max_correlated_positions": 2,
            "correlation_threshold": 0.7,
            "correlation_timeframe": "1h",
            
            # Временные ограничения
            "trading_hours_enabled": False,
            "trading_start_hour": 0,
            "trading_end_hour": 24,
            "weekend_trading_enabled": True,
            
            # Настройки уведомлений
            "notify_on_risk_warning": True,
            "notify_on_limit_exceeded": True,
            "save_risk_events": True
        }
    
    @staticmethod
    def get_all_default_configs() -> Dict[str, Dict[str, Any]]:
        """
        Получение всех конфигураций по умолчанию
        
        Returns:
            Dict: Все конфигурации
        """
        return {
            "global_config": DefaultConfigs.get_global_config(),
            "strategy_configs": {
                "bidirectional_grid": DefaultConfigs.get_bidirectional_grid_config(),
                "impulse_trailing": DefaultConfigs.get_impulse_trailing_config(),
                "grid_scalping": DefaultConfigs.get_grid_scalping_config()
            },
            "component_configs": {
                "meta_strategist": DefaultConfigs.get_meta_strategist_config(),
                "risk_manager": DefaultConfigs.get_risk_manager_config()
            }
        }
    
    @staticmethod
    def validate_config(config: Dict[str, Any], config_type: str) -> bool:
        """
        Валидация конфигурации
        
        Args:
            config: Конфигурация для проверки
            config_type: Тип конфигурации
            
        Returns:
            bool: True если конфигурация валидна
        """
        try:
            if config_type == "global_config":
                required_fields = [
                    "leverage", "order_amount", "max_simultaneous_trades",
                    "risk_per_trade_percent", "global_daily_drawdown_percent",
                    "watchlist_symbols"
                ]
            elif config_type == "bidirectional_grid":
                required_fields = [
                    "grid_levels", "grid_spacing_percent", "profit_percent"
                ]
            elif config_type == "impulse_trailing":
                required_fields = [
                    "initial_stop_percent", "trailing_step_percent", "impulse_threshold_percent"
                ]
            elif config_type == "grid_scalping":
                required_fields = [
                    "scalp_levels", "scalp_spacing_percent", "quick_profit_percent"
                ]
            else:
                return True  # Неизвестный тип - пропускаем валидацию
                
            # Проверка наличия обязательных полей
            for field in required_fields:
                if field not in config:
                    return False
                    
            # Дополнительные проверки
            if config_type == "global_config":
                if not config.get("watchlist_symbols"):
                    return False
                if config.get("leverage", 0) <= 0:
                    return False
                if config.get("order_amount", 0) <= 0:
                    return False
                    
            return True
            
        except Exception:
            return False

