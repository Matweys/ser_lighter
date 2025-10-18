# strategies/recovery/signal_scalper_recovery.py
"""
Recovery handler для стратегии SignalScalper.
Отвечает за восстановление состояния стратегии после перезагрузки сервера.
"""

import asyncio
import time
from typing import Dict, Any, Optional, TYPE_CHECKING
from decimal import Decimal
from strategies.recovery.base_recovery_handler import BaseRecoveryHandler
from core.logger import log_info, log_error, log_warning, log_debug
from core.events import EventType

if TYPE_CHECKING:
    from strategies.signal_scalper_strategy import SignalScalperStrategy


class SignalScalperRecoveryHandler(BaseRecoveryHandler):
    """
    Обработчик восстановления для стратегии SignalScalper.

    Отвечает за:
    - Восстановление состояния активных позиций
    - Синхронизацию с биржей
    - Восстановление состояния усреднения
    - Восстановление отслеживания стоп-лоссов
    - Восстановление подписок на события цен
    - Восстановление мониторинга позиций
    """

    async def recover(self, additional_data: Dict[str, Any]) -> bool:
        """
        Полное восстановление стратегии SignalScalper.

        Args:
            additional_data: Дополнительные данные из сохранённого состояния

        Returns:
            bool: True если восстановление прошло успешно
        """
        try:
            log_info(
                self.user_id,
                f"🔧 Начало восстановления SignalScalper для {self.symbol}...",
                "SignalScalperRecovery"
            )

            # Проверяем, была ли активна позиция на момент сохранения
            if hasattr(self.strategy, 'position_active') and self.strategy.position_active:
                log_info(
                    self.user_id,
                    f"🎯 Восстанавливаю активную позицию SignalScalper",
                    "SignalScalperRecovery"
                )

                # Восстанавливаем компоненты стратегии
                await self._restore_active_position_components()

                # Восстанавливаем состояние усреднения
                await self._restore_averaging_state()

                # Восстанавливаем стоп-лосс
                await self._restore_stop_loss()

                # Восстанавливаем мониторинг позиции
                await self._restore_position_monitoring()

                log_info(
                    self.user_id,
                    f"✅ Активная позиция SignalScalper для {self.symbol} полностью восстановлена",
                    "SignalScalperRecovery"
                )

            else:
                log_info(
                    self.user_id,
                    f"ℹ️ Позиция неактивна, восстанавливаю только базовые компоненты",
                    "SignalScalperRecovery"
                )
                # Даже для неактивной позиции нужен анализатор сигналов
                await self._init_signal_analyzer()

            # КРИТИЧЕСКИ ВАЖНО: Принудительная синхронизация с биржей
            sync_success = await self.sync_with_exchange()

            # Проверяем синхронизацию с базой данных
            await self.sync_with_database()

            log_info(
                self.user_id,
                f"✅ Восстановление SignalScalper завершено успешно",
                "SignalScalperRecovery"
            )

            return sync_success

        except Exception as e:
            log_error(
                self.user_id,
                f"❌ Ошибка восстановления SignalScalper: {e}",
                "SignalScalperRecovery"
            )
            return False

    async def _restore_active_position_components(self):
        """Восстанавливает компоненты активной позиции."""
        try:
            # Восстанавливаем подписки на события цен
            if not hasattr(self.strategy, '_price_subscription_restored'):
                await self.restore_event_subscriptions()
                self.strategy._price_subscription_restored = True

            # Проверяем состояние замороженной конфигурации
            if hasattr(self.strategy, 'config_frozen') and self.strategy.config_frozen:
                log_info(
                    self.user_id,
                    f"❄️ Восстановлена заморозка конфигурации активной сделки",
                    "SignalScalperRecovery"
                )

            # Восстанавливаем последние сигналы
            if hasattr(self.strategy, 'last_signal'):
                log_debug(
                    self.user_id,
                    f"📡 Восстановлен последний сигнал: {getattr(self.strategy, 'last_signal', 'None')}",
                    "SignalScalperRecovery"
                )

            # Инициализируем анализатор сигналов
            await self._init_signal_analyzer()

        except Exception as e:
            log_error(
                self.user_id,
                f"Ошибка восстановления компонентов позиции: {e}",
                "SignalScalperRecovery"
            )

    async def _restore_averaging_state(self):
        """Восстанавливает состояние усреднения."""
        try:
            if hasattr(self.strategy, 'averaging_executed') and self.strategy.averaging_executed:
                total_size = getattr(self.strategy, 'total_position_size', 0)
                avg_price = getattr(self.strategy, 'average_entry_price', 0)

                log_info(
                    self.user_id,
                    f"📊 Восстановлено состояние усреднения: "
                    f"executed={self.strategy.averaging_executed}, "
                    f"общий размер: {total_size}, средняя цена: {avg_price}",
                    "SignalScalperRecovery"
                )

        except Exception as e:
            log_error(
                self.user_id,
                f"Ошибка восстановления состояния усреднения: {e}",
                "SignalScalperRecovery"
            )

    async def _restore_stop_loss(self):
        """Восстанавливает отслеживание стоп-лосса."""
        try:
            if hasattr(self.strategy, 'stop_loss_order_id') and self.strategy.stop_loss_order_id:
                log_info(
                    self.user_id,
                    f"🛡️ Восстанавливаю отслеживание стоп-лосса: {self.strategy.stop_loss_order_id}",
                    "SignalScalperRecovery"
                )

                # Пытаемся восстановить стоп-лосс с биржи
                await self._restore_stop_loss_from_exchange()

        except Exception as e:
            log_error(
                self.user_id,
                f"Ошибка восстановления стоп-лосса: {e}",
                "SignalScalperRecovery"
            )

    async def _restore_position_monitoring(self):
        """Восстанавливает мониторинг активной позиции."""
        try:
            # Проверяем, нужно ли запустить монитор позиции
            if not self.strategy._position_monitor_task or self.strategy._position_monitor_task.done():
                if hasattr(self.strategy, 'position_size') and getattr(self.strategy, 'position_size', 0) > 0:
                    self.strategy._position_monitor_task = asyncio.create_task(
                        self.strategy._monitor_active_position()
                    )
                    log_info(
                        self.user_id,
                        f"🔍 Запущен монитор позиции для {self.symbol}",
                        "SignalScalperRecovery"
                    )

        except Exception as e:
            log_error(
                self.user_id,
                f"Ошибка восстановления мониторинга позиции: {e}",
                "SignalScalperRecovery"
            )

    async def _init_signal_analyzer(self):
        """Инициализирует анализатор сигналов."""
        try:
            if not self.strategy.signal_analyzer:
                from analysis.signal_analyzer import SignalAnalyzer
                self.strategy.signal_analyzer = SignalAnalyzer(
                    self.user_id,
                    self.api,
                    self.strategy.config
                )
                log_info(
                    self.user_id,
                    f"📈 Инициализирован анализатор сигналов",
                    "SignalScalperRecovery"
                )

        except Exception as e:
            log_error(
                self.user_id,
                f"Ошибка инициализации анализатора сигналов: {e}",
                "SignalScalperRecovery"
            )

    async def restore_event_subscriptions(self) -> bool:
        """Восстанавливает подписки на события цен."""
        try:
            await self.event_bus.subscribe(
                EventType.PRICE_UPDATE,
                self.strategy.handle_price_update,
                user_id=self.user_id
            )
            log_info(
                self.user_id,
                f"✅ Восстановлена подписка на обновления цен для {self.symbol}",
                "SignalScalperRecovery"
            )
            return True

        except Exception as e:
            log_error(
                self.user_id,
                f"Ошибка восстановления подписок на события: {e}",
                "SignalScalperRecovery"
            )
            return False

    async def sync_with_exchange(self) -> bool:
        """
        КРИТИЧЕСКИ ВАЖНАЯ синхронизация состояния стратегии с биржей.
        Восстанавливает состояние активных позиций и предотвращает дублирование ордеров.

        Returns:
            bool: True если синхронизация прошла успешно
        """
        try:
            log_info(
                self.user_id,
                f"🔄 Принудительная синхронизация с биржей для {self.symbol}...",
                "SignalScalperRecovery"
            )

            # Получаем активные позиции с биржи
            exchange_positions = await self.api.get_positions()
            active_position = None

            for position in exchange_positions:
                if (position.get('symbol') == self.symbol and
                    float(position.get('size', 0)) > 0):
                    active_position = position
                    break

            if active_position:
                # На бирже есть активная позиция
                return await self._handle_active_exchange_position(active_position)
            else:
                # На бирже НЕТ активных позиций
                return await self._handle_no_exchange_position()

        except Exception as e:
            log_error(
                self.user_id,
                f"❌ Критическая ошибка синхронизации с биржей: {e}",
                "SignalScalperRecovery"
            )
            return False

    async def _handle_active_exchange_position(self, position_data: Dict[str, Any]) -> bool:
        """Обработка случая, когда на бирже есть активная позиция."""
        try:
            position_size = Decimal(str(position_data.get('size', 0)))
            position_side = position_data.get('side', '').lower()
            entry_price = Decimal(str(position_data.get('avgPrice', 0)))

            log_warning(
                self.user_id,
                f"🚨 НАЙДЕНА АКТИВНАЯ ПОЗИЦИЯ на бирже: {self.symbol} {position_side.upper()} "
                f"размер={position_size}, вход=${entry_price:.4f}",
                "SignalScalperRecovery"
            )

            # Если стратегия считала позицию неактивной - восстанавливаем
            if not self.strategy.position_active:
                await self._force_restore_position_state(position_size, position_side, entry_price)
                return True

            # Если позиция была активна - проверяем соответствие размеров
            else:
                return await self._verify_position_sizes(position_size)

        except Exception as e:
            log_error(
                self.user_id,
                f"Ошибка обработки активной позиции на бирже: {e}",
                "SignalScalperRecovery"
            )
            return False

    async def _handle_no_exchange_position(self) -> bool:
        """Обработка случая, когда на бирже НЕТ активных позиций."""
        try:
            if self.strategy.position_active:
                log_warning(
                    self.user_id,
                    f"⚠️ Стратегия считала позицию АКТИВНОЙ, но на бирже позиции НЕТ! Сбрасываю состояние...",
                    "SignalScalperRecovery"
                )
                await self._force_reset_position_state()
            else:
                log_info(
                    self.user_id,
                    f"✅ Синхронизация подтверждена: нет активных позиций",
                    "SignalScalperRecovery"
                )

            return True

        except Exception as e:
            log_error(
                self.user_id,
                f"Ошибка обработки отсутствия позиции: {e}",
                "SignalScalperRecovery"
            )
            return False

    async def _force_restore_position_state(
        self,
        position_size: Decimal,
        position_side: str,
        entry_price: Decimal
    ):
        """Принудительно восстанавливает состояние позиции из данных биржи."""
        try:
            log_warning(
                self.user_id,
                f"⚠️ Стратегия считала позицию НЕАКТИВНОЙ, но на бирже есть позиция! "
                f"ВОССТАНАВЛИВАЮ состояние...",
                "SignalScalperRecovery"
            )

            # Восстанавливаем базовое состояние позиции
            self.strategy.position_active = True
            self.strategy.active_direction = "LONG" if position_side == "long" else "SHORT"
            self.strategy.entry_price = entry_price
            self.strategy.position_size = position_size
            self.strategy.peak_profit_usd = Decimal('0')
            self.strategy.hold_signal_counter = 0

            # Восстанавливаем подписку на события цены
            await self.restore_event_subscriptions()

            # Проверяем, было ли усреднение
            expected_base_size = await self._estimate_base_position_size()
            if expected_base_size and position_size > expected_base_size * Decimal('1.1'):
                await self._restore_averaging_from_size(position_size, entry_price, expected_base_size)

            # Попытка восстановить стоп-лосс
            await self._restore_stop_loss_from_exchange()

            log_info(
                self.user_id,
                f"✅ Состояние стратегии ВОССТАНОВЛЕНО: {self.strategy.active_direction} позиция "
                f"размер={position_size}, вход=${entry_price:.4f}",
                "SignalScalperRecovery"
            )

            # Отправляем уведомление пользователю
            await self._send_position_recovery_notification(position_size, entry_price)

        except Exception as e:
            log_error(
                self.user_id,
                f"Ошибка принудительного восстановления состояния позиции: {e}",
                "SignalScalperRecovery"
            )

    async def _restore_averaging_from_size(
        self,
        position_size: Decimal,
        entry_price: Decimal,
        expected_base_size: Decimal
    ):
        """Восстанавливает состояние усреднения на основе размера позиции."""
        try:
            log_info(
                self.user_id,
                f"📊 Обнаружено возможное усреднение: "
                f"биржа={position_size}, ожидаемый_базовый≈{expected_base_size:.0f}",
                "SignalScalperRecovery"
            )

            # Устанавливаем усредненные значения
            self.strategy.total_position_size = position_size
            self.strategy.average_entry_price = entry_price
            self.strategy.averaging_executed = True

            log_info(
                self.user_id,
                f"📊 Восстановлено состояние усреднения: executed={self.strategy.averaging_executed}, "
                f"total_size={self.strategy.total_position_size}, avg_price={self.strategy.average_entry_price:.4f}",
                "SignalScalperRecovery"
            )

        except Exception as e:
            log_error(
                self.user_id,
                f"Ошибка восстановления состояния усреднения: {e}",
                "SignalScalperRecovery"
            )

    async def _verify_position_sizes(self, exchange_size: Decimal) -> bool:
        """Проверяет соответствие размеров позиции между стратегией и биржей."""
        try:
            strategy_total_size = (
                self.strategy.total_position_size
                if self.strategy.total_position_size > 0
                else self.strategy.position_size
            )

            if abs(strategy_total_size - exchange_size) > Decimal('1'):
                log_warning(
                    self.user_id,
                    f"⚠️ НЕСООТВЕТСТВИЕ РАЗМЕРОВ: стратегия={strategy_total_size}, биржа={exchange_size}. "
                    f"Синхронизирую...",
                    "SignalScalperRecovery"
                )

                # Принудительно синхронизируем размеры
                if self.strategy.total_position_size > 0:
                    self.strategy.total_position_size = exchange_size
                else:
                    self.strategy.position_size = exchange_size

            return True

        except Exception as e:
            log_error(
                self.user_id,
                f"Ошибка проверки размеров позиции: {e}",
                "SignalScalperRecovery"
            )
            return False

    async def _estimate_base_position_size(self) -> Optional[Decimal]:
        """Оценивает размер базовой позиции на основе текущих настроек."""
        try:
            order_amount = self.strategy._convert_to_decimal(
                self.strategy.get_config_value("order_amount", 50.0)
            )
            leverage = self.strategy._convert_to_decimal(
                self.strategy.get_config_value("leverage", 1.0)
            )

            current_price = await self._get_current_market_price()
            if current_price:
                estimated_qty = await self.api.calculate_quantity_from_usdt(
                    self.symbol, order_amount, leverage, price=current_price
                )
                return estimated_qty

        except Exception as e:
            log_error(
                self.user_id,
                f"Ошибка оценки базового размера позиции: {e}",
                "SignalScalperRecovery"
            )

        return None

    async def _get_current_market_price(self) -> Optional[Decimal]:
        """Получает текущую рыночную цену символа."""
        try:
            ticker = await self.api.get_ticker(self.symbol)
            if ticker and 'lastPrice' in ticker:
                return Decimal(str(ticker['lastPrice']))
        except Exception as e:
            log_error(
                self.user_id,
                f"Ошибка получения рыночной цены: {e}",
                "SignalScalperRecovery"
            )
        return None

    async def _restore_stop_loss_from_exchange(self):
        """Восстанавливает информацию о стоп-лоссе с биржи."""
        try:
            position_info = await self.api.get_position_info(self.symbol)
            if position_info:
                stop_loss_str = position_info.get('stopLoss', '0')
                if stop_loss_str and stop_loss_str != '0':
                    self.strategy.stop_loss_price = Decimal(str(stop_loss_str))
                    self.strategy.stop_loss_order_id = f"restored_sl_{self.symbol}_{int(time.time())}"
                    log_info(
                        self.user_id,
                        f"🛡️ Восстановлен стоп-лосс: ${self.strategy.stop_loss_price:.4f}",
                        "SignalScalperRecovery"
                    )

        except Exception as e:
            log_error(
                self.user_id,
                f"Ошибка восстановления стоп-лосса с биржи: {e}",
                "SignalScalperRecovery"
            )

    async def _force_reset_position_state(self):
        """Принудительно сбрасывает состояние позиции."""
        try:
            log_info(
                self.user_id,
                "🔄 Принудительный сброс состояния позиции...",
                "SignalScalperRecovery"
            )

            # Сбрасываем все переменные состояния
            self.strategy.position_active = False
            self.strategy.active_direction = None
            self.strategy.entry_price = None
            self.strategy.position_size = None
            self.strategy.peak_profit_usd = Decimal('0')
            self.strategy.hold_signal_counter = 0

            # Сбрасываем переменные усреднения
            self.strategy.averaging_executed = False
            self.strategy.averaging_count = 0
            self.strategy.initial_margin_usd = Decimal('0')
            self.strategy.total_fees_paid = Decimal('0')
            self.strategy.total_position_size = Decimal('0')
            self.strategy.average_entry_price = Decimal('0')

            # Сбрасываем стоп-лосс
            self.strategy.stop_loss_order_id = None
            self.strategy.stop_loss_price = None

            # Отписываемся от событий цены
            await self.event_bus.unsubscribe(self.strategy._handle_price_update)

            log_info(
                self.user_id,
                "✅ Состояние позиции сброшено",
                "SignalScalperRecovery"
            )

        except Exception as e:
            log_error(
                self.user_id,
                f"Ошибка сброса состояния позиции: {e}",
                "SignalScalperRecovery"
            )

    async def _send_position_recovery_notification(
        self,
        position_size: Decimal,
        entry_price: Decimal
    ):
        """Отправляет уведомление пользователю о восстановлении позиции."""
        try:
            averaging_status = "Да" if self.strategy.averaging_executed else "Нет"
            recovery_message = (
                f"🔄 <b>ВОССТАНОВЛЕНИЕ ПОЗИЦИИ</b>\n\n"
                f"📊 <b>Символ:</b> {self.symbol}\n"
                f"📈 <b>Направление:</b> {self.strategy.active_direction}\n"
                f"📏 <b>Размер:</b> {position_size}\n"
                f"💰 <b>Цена входа:</b> {entry_price:.4f} USDT\n"
                f"🔄 <b>Усреднение выполнено:</b> {averaging_status}\n\n"
                f"Стратегия продолжит мониторинг восстановленной позиции."
            )

            await self.notify_user(recovery_message)

        except Exception as e:
            log_error(
                self.user_id,
                f"Ошибка отправки уведомления о восстановлении: {e}",
                "SignalScalperRecovery"
            )