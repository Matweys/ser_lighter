# Система Recovery Handlers

## Описание

Модуль `strategies/recovery/` содержит обработчики восстановления для каждой стратегии. Это позволяет отделить логику восстановления от основной логики стратегий и упрощает добавление/удаление стратегий.

## Структура

```
strategies/recovery/
├── __init__.py                         # Экспорт всех recovery handlers
├── base_recovery_handler.py            # Базовый класс для всех handlers
├── signal_scalper_recovery.py          # Recovery для SignalScalper
└── README.md                            # Эта документация
```

## Принцип работы

Каждая стратегия имеет свой **Recovery Handler**, который отвечает за:

1. **Восстановление состояния** - загрузка сохранённых данных из Redis
2. **Синхронизацию с биржей** - проверка активных позиций и ордеров
3. **Синхронизацию с БД** - проверка соответствия данных в памяти и БД
4. **Восстановление подписок** - восстановление подписок на события EventBus
5. **Восстановление мониторинга** - запуск задач мониторинга позиций

## Как добавить новую стратегию

### Шаг 1: Создать Recovery Handler

Создайте файл `strategies/recovery/your_strategy_recovery.py`:

```python
from strategies.recovery.base_recovery_handler import BaseRecoveryHandler
from core.logger import log_info, log_error
from typing import Dict, Any

class YourStrategyRecoveryHandler(BaseRecoveryHandler):
    """Recovery handler для YourStrategy."""

    async def recover(self, additional_data: Dict[str, Any]) -> bool:
        """
        Основной метод восстановления.

        Returns:
            bool: True если восстановление прошло успешно
        """
        try:
            log_info(self.user_id, f"Начало восстановления YourStrategy для {self.symbol}...")

            # 1. Восстановите специфичные для стратегии компоненты
            # await self._restore_your_components()

            # 2. Синхронизируйтесь с биржей
            sync_success = await self.sync_with_exchange()

            # 3. Синхронизируйтесь с БД
            await self.sync_with_database()

            log_info(self.user_id, "Восстановление YourStrategy завершено")
            return sync_success

        except Exception as e:
            log_error(self.user_id, f"Ошибка восстановления: {e}")
            return False

    async def sync_with_exchange(self) -> bool:
        """
        Синхронизация с биржей.
        Проверьте активные позиции и восстановите состояние.
        """
        try:
            # Ваша логика синхронизации
            return True
        except Exception as e:
            log_error(self.user_id, f"Ошибка синхронизации с биржей: {e}")
            return False
```

### Шаг 2: Зарегистрировать в `__init__.py`

Добавьте импорт в `strategies/recovery/__init__.py`:

```python
from strategies.recovery.your_strategy_recovery import YourStrategyRecoveryHandler

__all__ = [
    'BaseRecoveryHandler',
    'SignalScalperRecoveryHandler',
    'YourStrategyRecoveryHandler',  # <-- Добавьте это
]
```

### Шаг 3: Использовать в стратегии

В вашей стратегии создайте recovery handler:

```python
from strategies.recovery import YourStrategyRecoveryHandler

class YourStrategy(BaseStrategy):
    def __init__(self, ...):
        super().__init__(...)

        # Создаём recovery handler
        self.recovery_handler = YourStrategyRecoveryHandler(self)

    async def _strategy_specific_recovery(self, additional_data: Dict[str, Any]):
        """Делегируем восстановление recovery handler."""
        return await self.recovery_handler.recover(additional_data)
```

## Базовый класс BaseRecoveryHandler

### Методы для переопределения

```python
async def recover(self, additional_data: Dict[str, Any]) -> bool:
    """Основной метод восстановления (ОБЯЗАТЕЛЬНО)"""
    pass

async def sync_with_exchange(self) -> bool:
    """Синхронизация с биржей (ОБЯЗАТЕЛЬНО)"""
    pass

async def restore_event_subscriptions(self) -> bool:
    """Восстановление подписок на события (опционально)"""
    pass
```

### Доступные методы

```python
# Синхронизация с БД (уже реализована)
await self.sync_with_database()

# Отправка уведомления пользователю
await self.notify_user("Ваше сообщение в HTML формате")
```

### Доступные атрибуты

```python
self.strategy     # Экземпляр стратегии
self.user_id      # ID пользователя
self.symbol       # Торговый символ
self.api          # API биржи
self.event_bus    # EventBus
self.bot          # Telegram bot
```

## Пример: SignalScalperRecoveryHandler

Смотрите `signal_scalper_recovery.py` как пример полной реализации recovery handler для сложной стратегии с:
- Восстановлением активных позиций
- Проверкой усреднения
- Восстановлением стоп-лоссов
- Синхронизацией размеров позиций
- Уведомлениями пользователя

## Удаление стратегии

Чтобы удалить стратегию:

1. Удалите файл recovery handler: `rm strategies/recovery/your_strategy_recovery.py`
2. Удалите импорт из `strategies/recovery/__init__.py`
3. Удалите саму стратегию: `rm strategies/your_strategy.py`

Всё! Код остаётся чистым и модульным.

## Преимущества этой архитектуры

✅ **Разделение ответственности** - восстановление отделено от торговой логики
✅ **Модульность** - легко добавлять/удалять стратегии
✅ **Повторное использование кода** - базовый класс с общей логикой
✅ **Тестируемость** - каждый recovery handler можно тестировать отдельно
✅ **Читаемость** - файлы стратегий остаются компактными
✅ **Масштабируемость** - легко добавлять новые стратегии без засорения кода

## Поддержка

При возникновении проблем:
1. Проверьте логи - каждый recovery handler пишет подробные логи
2. Убедитесь, что методы `recover()` и `sync_with_exchange()` реализованы
3. Проверьте, что recovery handler зарегистрирован в `__init__.py`