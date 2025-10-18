# core/meta_strategist.py
"""
Профессиональный анализатор рынка и стратег для многопользовательской системы
Реализует многотаймфреймовый анализ с динамическими настройками из Redis
"""
from decimal import Decimal, getcontext
from typing import Dict, Any, Optional
from datetime import datetime, timedelta
from core.logger import log_info, log_error
from core.events import EventType, UserSettingsChangedEvent, EventBus
from cache.redis_manager import redis_manager, ConfigType

# Настройка точности для Decimal
getcontext().prec = 28


class MetaStrategist:
    """
    Персональный стратег для пользователя
    Анализирует рынок и принимает решения о запуске стратегий на основе событий
    """

    def __init__(self, user_id: int, analyzer: 'MarketAnalyzer', event_bus: EventBus):
        self.user_id = user_id
        self.analyzer = analyzer
        self.event_bus = event_bus
        self.running = False

        # Кэш для предотвращения спама анализа
        self.last_analysis_time: Dict[str, datetime] = {}
        self.analysis_cooldown = timedelta(minutes=5)  # Минимальный интервал между анализами

        # Настройки пользователя
        self.user_config: Optional[Dict] = None

    async def start(self):
        """Запуск MetaStrategist"""
        if self.running:
            return
        log_info(self.user_id, "Запуск MetaStrategist...", module_name=__name__)
        try:
            await self._load_user_config()

            # Пользовательское событие - с user_id
            await self.event_bus.subscribe(EventType.USER_SETTINGS_CHANGED, self._handle_settings_changed, user_id=self.user_id)

            self.running = True
            log_info(self.user_id, "MetaStrategist запущен", module_name=__name__)
        except Exception as e:
            log_error(self.user_id, f"Ошибка запуска MetaStrategist: {e}", module_name=__name__)
            raise

    async def stop(self):
        """Остановка MetaStrategist"""
        if not self.running:
            return

        # Отписка от событий
        await self.event_bus.unsubscribe(self._handle_settings_changed)

        self.running = False
        log_info(self.user_id, "MetaStrategist остановлен", module_name=__name__)

    async def on_settings_changed(self, event: UserSettingsChangedEvent):
        """
        Публичный метод для обработки события изменения настроек.
        Вызывает внутренний защищенный обработчик.
        """
        await self._handle_settings_changed(event)

    async def _handle_settings_changed(self, event: UserSettingsChangedEvent):
        """Обработчик изменения настроек пользователя"""
        if event.user_id != self.user_id:
            return

        log_info(self.user_id, "Перезагрузка конфигурации после изменения настроек", module_name=__name__)
        await self._load_user_config()

    async def _load_user_config(self):
        """Загрузка конфигурации пользователя"""
        try:
            self.user_config = await redis_manager.get_config(self.user_id, ConfigType.GLOBAL)
            if not self.user_config:
                log_error(self.user_id, "Конфигурация пользователя не найдена", module_name=__name__)
                raise ValueError("Конфигурация пользователя не найдена")
        except Exception as e:
            log_error(self.user_id, f"Ошибка загрузки конфигурации: {e}", module_name=__name__)
            raise
