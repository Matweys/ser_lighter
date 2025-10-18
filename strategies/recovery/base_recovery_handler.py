# strategies/recovery/base_recovery_handler.py
"""
Базовый класс для обработчиков восстановления стратегий.
Определяет интерфейс и общую логику восстановления после перезагрузки сервера.
"""

from typing import Dict, Any, Optional, TYPE_CHECKING
from decimal import Decimal
from abc import ABC, abstractmethod
from core.logger import log_info, log_error, log_warning

if TYPE_CHECKING:
    from strategies.base_strategy import BaseStrategy


class BaseRecoveryHandler(ABC):
    """
    Базовый класс для обработчиков восстановления стратегий.

    Каждая стратегия должна иметь свой recovery handler, который знает как:
    - Восстановить состояние стратегии из сохранённых данных
    - Синхронизировать состояние с биржей
    - Синхронизировать состояние с базой данных
    - Восстановить подписки на события
    - Восстановить мониторинг позиций
    """

    def __init__(self, strategy: 'BaseStrategy'):
        """
        Инициализация recovery handler.

        Args:
            strategy: Экземпляр стратегии для восстановления
        """
        self.strategy = strategy
        self.user_id = strategy.user_id
        self.symbol = strategy.symbol
        self.api = strategy.api
        self.event_bus = strategy.event_bus
        self.bot = strategy.bot

    @abstractmethod
    async def recover(self, additional_data: Dict[str, Any]) -> bool:
        """
        Основной метод восстановления стратегии.

        Args:
            additional_data: Дополнительные данные для восстановления

        Returns:
            bool: True если восстановление прошло успешно
        """
        pass

    @abstractmethod
    async def sync_with_exchange(self) -> bool:
        """
        Синхронизация состояния стратегии с биржей.
        Проверяет активные позиции, ордера и обновляет состояние стратегии.

        Returns:
            bool: True если синхронизация прошла успешно
        """
        pass

    async def sync_with_database(self) -> bool:
        """
        Синхронизация состояния стратегии с базой данных.
        Проверяет соответствие активных сделок в памяти и БД.

        Returns:
            bool: True если синхронизация прошла успешно
        """
        try:
            # Если есть активная связь с БД, проверяем что запись существует
            if hasattr(self.strategy, 'active_trade_db_id') and self.strategy.active_trade_db_id:
                from database.db_trades import db_manager

                # Проверяем, что сделка действительно существует в БД
                trade_exists = await db_manager.trade_exists(self.strategy.active_trade_db_id)

                if trade_exists:
                    log_info(
                        self.user_id,
                        f"✅ Связь с БД подтверждена: trade_id={self.strategy.active_trade_db_id}",
                        self.__class__.__name__
                    )
                    return True
                else:
                    log_warning(
                        self.user_id,
                        f"⚠️ Сделка {self.strategy.active_trade_db_id} не найдена в БД, сбрасываю связь",
                        self.__class__.__name__
                    )
                    delattr(self.strategy, 'active_trade_db_id')
                    return False

            return True

        except Exception as e:
            log_error(self.user_id, f"Ошибка синхронизации с БД: {e}", self.__class__.__name__)
            return False

    async def restore_event_subscriptions(self) -> bool:
        """
        Восстанавливает подписки на события EventBus.

        Returns:
            bool: True если подписки восстановлены успешно
        """
        try:
            log_info(
                self.user_id,
                f"Восстановление подписок на события для {self.symbol}...",
                self.__class__.__name__
            )
            # Базовая реализация - переопределяется в дочерних классах
            return True

        except Exception as e:
            log_error(
                self.user_id,
                f"Ошибка восстановления подписок на события: {e}",
                self.__class__.__name__
            )
            return False

    async def notify_user(self, message: str):
        """
        Отправляет уведомление пользователю о ходе восстановления.

        Args:
            message: Текст уведомления (поддерживает HTML)
        """
        try:
            if self.bot:
                await self.bot.send_message(self.user_id, message, parse_mode="HTML")
        except Exception as e:
            log_error(
                self.user_id,
                f"Ошибка отправки уведомления пользователю: {e}",
                self.__class__.__name__
            )