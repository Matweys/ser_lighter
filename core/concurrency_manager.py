"""
Concurrency Manager - Централизованная система управления многопользовательскими блокировками

Обеспечивает потокобезопасность для неограниченного количества пользователей.
Предотвращает race conditions, deadlocks и data corruption.

Архитектура:
- Каждый пользователь имеет свою изолированную блокировку
- Каждый координатор имеет свою изолированную блокировку
- Автоматическая очистка неиспользуемых блокировок
- Поддержка декораторов для упрощения использования
"""
import asyncio
import time
from typing import Dict, Optional, Callable, Any
from functools import wraps
from core.logger import log_debug, log_warning


class ConcurrencyManager:
    """
    Глобальный менеджер блокировок для многопользовательской системы.

    Принципы:
    - Thread-safe для неограниченного количества пользователей
    - Автоматическое создание и удаление блокировок
    - Изоляция между пользователями
    - Низкие накладные расходы (lazy initialization)
    """

    def __init__(self):
        # Блокировки на уровне пользователя (user_id -> Lock)
        self._user_locks: Dict[int, asyncio.Lock] = {}

        # Блокировки координаторов (user_id:symbol -> Lock)
        self._coordinator_locks: Dict[str, asyncio.Lock] = {}

        # Мастер-блокировка для создания/удаления блокировок (bootstrap lock)
        self._master_lock = asyncio.Lock()

        # Метрики использования (для автоочистки)
        self._user_lock_last_used: Dict[int, float] = {}
        self._coordinator_lock_last_used: Dict[str, float] = {}

        # Настройки автоочистки
        self.CLEANUP_INTERVAL = 3600  # Очистка каждый час
        self.LOCK_TTL = 7200  # Удалять блокировки неактивные 2 часа

    async def get_user_lock(self, user_id: int) -> asyncio.Lock:
        """
        Получает или создаёт блокировку для пользователя.

        Thread-safe: Использует мастер-блокировку для создания новых блокировок.
        """
        # Быстрый путь - блокировка уже существует
        if user_id in self._user_locks:
            self._user_lock_last_used[user_id] = time.time()
            return self._user_locks[user_id]

        # Медленный путь - создаём новую блокировку
        async with self._master_lock:
            # Double-check (другой поток мог создать пока мы ждали)
            if user_id in self._user_locks:
                self._user_lock_last_used[user_id] = time.time()
                return self._user_locks[user_id]

            # Создаём новую блокировку
            lock = asyncio.Lock()
            self._user_locks[user_id] = lock
            self._user_lock_last_used[user_id] = time.time()

            log_debug(0, f"[ConcurrencyManager] Создана блокировка для user {user_id}", "ConcurrencyManager")
            return lock

    async def get_coordinator_lock(self, user_id: int, symbol: str) -> asyncio.Lock:
        """
        Получает или создаёт блокировку для координатора.

        Thread-safe: Использует мастер-блокировку для создания новых блокировок.
        """
        lock_key = f"{user_id}:{symbol}"

        # Быстрый путь
        if lock_key in self._coordinator_locks:
            self._coordinator_lock_last_used[lock_key] = time.time()
            return self._coordinator_locks[lock_key]

        # Медленный путь
        async with self._master_lock:
            # Double-check
            if lock_key in self._coordinator_locks:
                self._coordinator_lock_last_used[lock_key] = time.time()
                return self._coordinator_locks[lock_key]

            # Создаём новую блокировку
            lock = asyncio.Lock()
            self._coordinator_locks[lock_key] = lock
            self._coordinator_lock_last_used[lock_key] = time.time()

            log_debug(0, f"[ConcurrencyManager] Создана блокировка для coordinator {lock_key}", "ConcurrencyManager")
            return lock

    async def cleanup_unused_locks(self):
        """
        Очищает неиспользуемые блокировки для экономии памяти.

        Вызывается периодически в фоновом режиме.
        """
        async with self._master_lock:
            current_time = time.time()

            # Очистка пользовательских блокировок
            users_to_remove = []
            for user_id, last_used in self._user_lock_last_used.items():
                if current_time - last_used > self.LOCK_TTL:
                    # Блокировка не использовалась долго - можно удалить
                    # НО ТОЛЬКО если она не занята!
                    lock = self._user_locks.get(user_id)
                    if lock and not lock.locked():
                        users_to_remove.append(user_id)

            for user_id in users_to_remove:
                del self._user_locks[user_id]
                del self._user_lock_last_used[user_id]
                log_debug(0, f"[ConcurrencyManager] Удалена неиспользуемая блокировка user {user_id}", "ConcurrencyManager")

            # Очистка блокировок координаторов
            coordinators_to_remove = []
            for lock_key, last_used in self._coordinator_lock_last_used.items():
                if current_time - last_used > self.LOCK_TTL:
                    lock = self._coordinator_locks.get(lock_key)
                    if lock and not lock.locked():
                        coordinators_to_remove.append(lock_key)

            for lock_key in coordinators_to_remove:
                del self._coordinator_locks[lock_key]
                del self._coordinator_lock_last_used[lock_key]
                log_debug(0, f"[ConcurrencyManager] Удалена неиспользуемая блокировка coordinator {lock_key}", "ConcurrencyManager")

            if users_to_remove or coordinators_to_remove:
                log_debug(0,
                         f"[ConcurrencyManager] Очистка: удалено {len(users_to_remove)} user locks, {len(coordinators_to_remove)} coordinator locks",
                         "ConcurrencyManager")

    async def release_user_lock(self, user_id: int):
        """
        Принудительно удаляет блокировку пользователя.

        Используется при остановке сессии пользователя.
        """
        async with self._master_lock:
            if user_id in self._user_locks:
                lock = self._user_locks[user_id]
                if not lock.locked():
                    del self._user_locks[user_id]
                    del self._user_lock_last_used[user_id]
                    log_debug(0, f"[ConcurrencyManager] Освобождена блокировка user {user_id}", "ConcurrencyManager")
                else:
                    log_warning(0, f"[ConcurrencyManager] Блокировка user {user_id} занята, пропускаю удаление", "ConcurrencyManager")

    async def release_coordinator_lock(self, user_id: int, symbol: str):
        """
        Принудительно удаляет блокировку координатора.

        Используется при остановке координатора.
        """
        lock_key = f"{user_id}:{symbol}"
        async with self._master_lock:
            if lock_key in self._coordinator_locks:
                lock = self._coordinator_locks[lock_key]
                if not lock.locked():
                    del self._coordinator_locks[lock_key]
                    del self._coordinator_lock_last_used[lock_key]
                    log_debug(0, f"[ConcurrencyManager] Освобождена блокировка coordinator {lock_key}", "ConcurrencyManager")
                else:
                    log_warning(0, f"[ConcurrencyManager] Блокировка coordinator {lock_key} занята, пропускаю удаление", "ConcurrencyManager")

    def get_stats(self) -> Dict[str, Any]:
        """Возвращает статистику использования блокировок."""
        return {
            "total_user_locks": len(self._user_locks),
            "total_coordinator_locks": len(self._coordinator_locks),
            "active_user_locks": sum(1 for lock in self._user_locks.values() if lock.locked()),
            "active_coordinator_locks": sum(1 for lock in self._coordinator_locks.values() if lock.locked()),
        }


# Глобальный экземпляр менеджера
concurrency_manager = ConcurrencyManager()


def user_locked(func: Callable) -> Callable:
    """
    Декоратор для автоматической защиты методов UserSession блокировкой.

    Использование:
        @user_locked
        async def start_strategy(self, ...):
            ...
    """
    @wraps(func)
    async def wrapper(self, *args, **kwargs):
        # Получаем блокировку для пользователя
        lock = await concurrency_manager.get_user_lock(self.user_id)

        # Выполняем метод под блокировкой
        async with lock:
            return await func(self, *args, **kwargs)

    return wrapper


def coordinator_locked(func: Callable) -> Callable:
    """
    Декоратор для автоматической защиты методов MultiAccountCoordinator блокировкой.

    Использование:
        @coordinator_locked
        async def _activate_bot(self, priority: int):
            ...
    """
    @wraps(func)
    async def wrapper(self, *args, **kwargs):
        # Получаем блокировку для координатора
        lock = await concurrency_manager.get_coordinator_lock(self.user_id, self.symbol)

        # Выполняем метод под блокировкой
        async with lock:
            return await func(self, *args, **kwargs)

    return wrapper


def strategy_locked(func: Callable) -> Callable:
    """
    Декоратор для автоматической защиты методов Strategy блокировкой.

    Использование:
        @strategy_locked
        async def _handle_new_candle(self, event):
            ...

    ВАЖНО: Использует user_id + symbol для уникальной блокировки каждой стратегии.
    """
    @wraps(func)
    async def wrapper(self, *args, **kwargs):
        # Получаем блокировку для стратегии (используем user_id + symbol как ключ)
        lock = await concurrency_manager.get_coordinator_lock(self.user_id, self.symbol)

        # Выполняем метод под блокировкой
        async with lock:
            return await func(self, *args, **kwargs)

    return wrapper


async def start_cleanup_task():
    """
    Запускает фоновую задачу очистки неиспользуемых блокировок.

    Вызывается при старте BotApplication.
    """
    while True:
        await asyncio.sleep(concurrency_manager.CLEANUP_INTERVAL)
        try:
            await concurrency_manager.cleanup_unused_locks()
        except Exception as e:
            log_warning(0, f"[ConcurrencyManager] Ошибка при очистке блокировок: {e}", "ConcurrencyManager")