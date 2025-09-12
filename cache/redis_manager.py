# cache/redis_manager.py
"""
Профессиональный менеджер Redis для многопользовательской торговой системы
Поддерживает динамические конфигурации, кэширование и событийную архитектуру
"""
import redis.asyncio as redis
import asyncio
import json
import time
from typing import Optional, Dict, Any, Set, List, Union
from decimal import Decimal, getcontext
from datetime import datetime, timedelta
from core.functions import DecimalEncoder
from core.logger import log_info, log_error, log_debug
from core.enums import ConfigType, SystemConstants

# Настройка точности для Decimal
getcontext().prec = 28


class RedisManager:
    """
    Профессиональный менеджер Redis для торговой системы
    
    Особенности:
    - Асинхронные операции для высокой производительности
    - Типизированные конфигурации с валидацией
    - Кэширование с TTL для оптимизации
    - Поддержка Decimal для точных вычислений
    - Событийная система уведомлений
    - Автоматическая очистка устаревших данных
    """

    def __init__(self, redis_url: str = "redis://localhost:6379/0"):
        self.redis_url = redis_url
        self.redis_client: Optional[redis.Redis] = None
        self.is_connected = False
        
        # Префиксы для организации данных
        self.prefix = "trading_bot"
        
        # Кэш для часто используемых данных
        self._config_cache: Dict[str, Dict] = {}
        self._cache_timestamps: Dict[str, float] = {}
        
        # Подписчики на изменения конфигураций
        self._config_subscribers: Dict[str, List] = {}


    async def init_redis(self):
        """Инициализация подключения к Redis"""
        try:
            self.redis_client = redis.from_url(
                self.redis_url, 
                decode_responses=True,
                socket_connect_timeout=10,
                socket_keepalive=True,
                socket_keepalive_options={},
                health_check_interval=30
            )
            
            # Проверка подключения
            await self.redis_client.ping()
            self.is_connected = True
            
            log_info(0, "Redis подключен успешно", module_name=__name__)
            
            # Инициализация структур данных
            await self._init_data_structures()
            
        except Exception as e:
            log_error(0, f"Ошибка подключения к Redis: {e}", module_name=__name__)
            self.is_connected = False
            raise

    async def close(self):
        """Закрытие подключения к Redis"""
        if self.redis_client:
            await self.redis_client.close()
            self.is_connected = False
            log_info(0, "Redis подключение закрыто", module_name=__name__)

    async def _init_data_structures(self):
        """Инициализация базовых структур данных"""
        try:
            # Создание индексов для быстрого поиска
            await self.redis_client.sadd(f"{self.prefix}:indexes:users", "initialized")
            await self.redis_client.sadd(f"{self.prefix}:indexes:sessions", "initialized")
            await self.redis_client.sadd(f"{self.prefix}:indexes:strategies", "initialized")
            
            log_debug(0, "Структуры данных Redis инициализированы", module_name=__name__)
            
        except Exception as e:
            log_error(0, f"Ошибка инициализации структур данных: {e}", module_name=__name__)

    def _get_key(self, *parts) -> str:
        """Генерация ключа Redis"""
        return f"{self.prefix}:" + ":".join(str(part) for part in parts)

    async def _safe_execute(self, operation, *args, **kwargs):
        """Безопасное выполнение Redis операций"""
        if not self.is_connected or not self.redis_client:
            log_error(0, "Redis не подключен", module_name=__name__)
            return None
            
        try:
            return await operation(*args, **kwargs)
        except Exception as e:
            log_error(0, f"Ошибка Redis операции: {e}", module_name=__name__)
            return None


    # =============================================================================
    # УПРАВЛЕНИЕ ПОЛЬЗОВАТЕЛЬСКИМИ СЕССИЯМИ
    # =============================================================================

    async def create_user_session(self, user_id: int, session_data: Dict[str, Any]) -> bool:
        """Создание пользовательской сессии"""
        try:
            key = self._get_key("user", user_id, "session")
            
            # Добавление метаданных
            session_data.setdefault("created_at", datetime.now().isoformat())
            session_data.setdefault("last_activity", datetime.now().isoformat())
            
            # Сохранение сессии
            serialized_data = {}
            for k, v in session_data.items():
                if isinstance(v, Decimal):
                    serialized_data[k] = str(v)
                elif isinstance(v, (dict, list)):
                    serialized_data[k] = json.dumps(v)
                else:
                    serialized_data[k] = str(v)
            
            result = await self._safe_execute(
                self.redis_client.hset, key, mapping=serialized_data
            )
            
            if result is not None:
                # Добавление в индекс активных сессий
                await self.redis_client.sadd(f"{self.prefix}:active_sessions", str(user_id))
                await self.redis_client.sadd(f"{self.prefix}:indexes:users", str(user_id))
                
                log_info(user_id, "Пользовательская сессия создана", module_name=__name__)
                return True
                
        except Exception as e:
            log_error(user_id, f"Ошибка создания сессии: {e}", module_name=__name__)
            
        return False

    async def get_user_session(self, user_id: int) -> Optional[Dict[str, Any]]:
        """Получение данных пользовательской сессии"""
        try:
            key = self._get_key("user", user_id, "session")
            session_data = await self._safe_execute(self.redis_client.hgetall, key)
            
            if not session_data:
                return None
                
            # Десериализация данных
            result = {}
            for k, v in session_data.items():
                # 1. Пропускаем пустые значения
                if v is None:
                    result[k] = None
                    continue

                # 2. Обработка известных строковых полей
                if k in ["api_key", "secret_key", "status", "start_time", "created_at", "last_activity"]:
                    result[k] = v
                # 3. Обработка булевых значений
                elif v.lower() in ["true", "false"]:
                    result[k] = v.lower() == "true"
                # 4. Обработка JSON-объектов
                elif k.startswith("config_") or k.startswith("settings_") or v.startswith(('[', '{')):
                    try:
                        result[k] = json.loads(v)
                    except json.JSONDecodeError:
                        result[k] = v  # Оставляем как строку, если не JSON
                # 5. Обработка числовых значений
                else:
                    try:
                        # Сначала пытаемся преобразовать в int
                        result[k] = int(v)
                    except ValueError:
                        try:
                            # Если не получилось, пытаемся в Decimal
                            result[k] = Decimal(v)
                        except Exception:
                            # Если и это не удалось, оставляем как есть (строку)
                            result[k] = v
                        
            # Обновление времени последней активности
            await self.update_user_activity(user_id)
            
            return result
            
        except Exception as e:
            log_error(user_id, f"Ошибка получения сессии: {e}", module_name=__name__)
            return None

    async def update_user_activity(self, user_id: int):
        """Обновление времени последней активности пользователя"""
        key = self._get_key("user", user_id, "session")
        await self._safe_execute(
            self.redis_client.hset, key, "last_activity", datetime.now().isoformat()
        )

    async def delete_user_session(self, user_id: int) -> bool:
        """Удаление пользовательской сессии"""
        try:
            key = self._get_key("user", user_id, "session")
            
            # Удаление сессии
            result = await self._safe_execute(self.redis_client.delete, key)
            
            if result:
                # Удаление из индексов
                await self.redis_client.srem(f"{self.prefix}:active_sessions", str(user_id))
                
                log_info(user_id, "Пользовательская сессия удалена", module_name=__name__)
                return True
                
        except Exception as e:
            log_error(user_id, f"Ошибка удаления сессии: {e}", module_name=__name__)
            
        return False

    async def get_active_users(self) -> Set[int]:
        """Получение множества активных пользователей"""
        try:
            active_users = await self._safe_execute(
                self.redis_client.smembers, f"{self.prefix}:active_sessions"
            )
            
            if active_users:
                return {int(user_id) for user_id in active_users}
                
        except Exception as e:
            log_error(0, f"Ошибка получения активных пользователей: {e}", module_name=__name__)
            
        return set()

    # =============================================================================
    # УПРАВЛЕНИЕ КОНФИГУРАЦИЯМИ
    # =============================================================================

    async def save_config(
        self, 
        user_id: int, 
        config_type: ConfigType, 
        config_data: Dict[str, Any],
        ttl: Optional[int] = None
    ) -> bool:
        """Сохранение конфигурации пользователя"""
        try:
            key = self._get_key("user", user_id, "config", config_type.value)
            
            # Добавление метаданных
            config_data.update({
                "updated_at": datetime.now().isoformat(),
                "config_type": config_type.value
            })
            
            # Сериализация данных
            serialized_data = {}
            for k, v in config_data.items():
                if isinstance(v, Decimal):
                    serialized_data[k] = str(v)
                elif isinstance(v, (dict, list)):
                    serialized_data[k] = json.dumps(v)
                elif isinstance(v, bool):
                    serialized_data[k] = "true" if v else "false"
                else:
                    serialized_data[k] = str(v)
            
            # Сохранение конфигурации
            result = await self._safe_execute(
                self.redis_client.hset, key, mapping=serialized_data
            )
            
            if result is not None:
                # Установка TTL если указан
                if ttl:
                    await self.redis_client.expire(key, ttl)
                
                # Очистка кэша
                cache_key = f"{user_id}:{config_type.value}"
                self._config_cache.pop(cache_key, None)
                self._cache_timestamps.pop(cache_key, None)
                
                # Уведомление подписчиков об изменении
                await self._notify_config_change(user_id, config_type, config_data)
                
                log_debug(
                    user_id, 
                    f"Конфигурация {config_type.value} сохранена", 
                    module_name=__name__
                )
                return True
                
        except Exception as e:
            log_error(user_id, f"Ошибка сохранения конфигурации {config_type.value}: {e}", module_name=__name__)
            
        return False

    async def get_config(self, user_id: int, config_type: ConfigType, use_cache: bool = True) -> Optional[Dict[str, Any]]:
        """Получение конфигурации пользователя с улучшенной десериализацией"""
        try:
            cache_key = f"{user_id}:{config_type.value}"

            # Проверка кэша
            if use_cache and cache_key in self._config_cache:
                cache_time = self._cache_timestamps.get(cache_key, 0)
                if time.time() - cache_time < SystemConstants.CONFIG_CACHE_SECONDS:
                    return self._config_cache[cache_key].copy()

            key = self._get_key("user", user_id, "config", config_type.value)
            config_data = await self._safe_execute(self.redis_client.hgetall, key)

            if not config_data:
                return None

            # Десериализация данных
            result = {}
            for k, v in config_data.items():
                if v is None:
                    result[k] = None
                    continue

                # 1. Обработка предопределенных ключей
                if k in ["updated_at", "config_type", "strategy_name", "symbol"]:
                    result[k] = v
                # 2. Обработка булевых значений
                elif v.lower() in ["true", "false"]:
                    result[k] = v.lower() == "true"
                # 3. Обработка JSON-объектов
                elif k.endswith("_config") or k.endswith("_data") or k.endswith("_symbols") or v.startswith(('[', '{')):
                    try:
                        result[k] = json.loads(v)
                    except json.JSONDecodeError:
                        result[k] = v  # Оставляем как строку, если не JSON
                # 4. Обработка числовых значений
                else:
                    try:
                        # Сначала пытаемся преобразовать в int
                        result[k] = int(v)
                    except ValueError:
                        try:
                            # Если не получилось, пытаемся в Decimal
                            result[k] = Decimal(v)
                        except Exception:
                            # Если и это не удалось, оставляем как есть (строку)
                            result[k] = v

            # Кэширование результата
            if use_cache:
                self._config_cache[cache_key] = result.copy()
                self._cache_timestamps[cache_key] = time.time()

            return result

        except Exception as e:
            log_error(user_id, f"Ошибка получения конфигурации {config_type.value}: {e}", module_name=__name__)
            return None

    async def delete_config(self, user_id: int, config_type: ConfigType) -> bool:
        """Удаление конфигурации пользователя"""
        try:
            key = self._get_key("user", user_id, "config", config_type.value)
            result = await self._safe_execute(self.redis_client.delete, key)
            
            if result:
                # Очистка кэша
                cache_key = f"{user_id}:{config_type.value}"
                self._config_cache.pop(cache_key, None)
                self._cache_timestamps.pop(cache_key, None)
                
                log_debug(
                    user_id, 
                    f"Конфигурация {config_type.value} удалена", 
                    module_name=__name__
                )
                return True
                
        except Exception as e:
            log_error(user_id, f"Ошибка удаления конфигурации {config_type.value}: {e}", module_name=__name__)
            
        return False

    async def _notify_config_change(
        self, 
        user_id: int, 
        config_type: ConfigType, 
        config_data: Dict[str, Any]
    ):
        """Уведомление подписчиков об изменении конфигурации"""
        try:
            # Публикация события изменения конфигурации
            channel = f"{self.prefix}:config_changes:{user_id}"
            message = {
                "user_id": user_id,
                "config_type": config_type.value,
                "timestamp": datetime.now().isoformat(),
                "data": config_data
            }

            await self._safe_execute(self.redis_client.publish,channel, json.dumps(message, cls=DecimalEncoder))
        except Exception as e:
            log_error(user_id, f"Ошибка уведомления об изменении конфигурации: {e}", module_name=__name__)

    # =============================================================================
    # УПРАВЛЕНИЕ СОСТОЯНИЯМИ СТРАТЕГИЙ
    # =============================================================================

    async def save_strategy_state(
        self, 
        user_id: int, 
        strategy_name: str, 
        symbol: str, 
        state_data: Dict[str, Any]
    ) -> bool:
        """Сохранение состояния стратегии"""
        try:
            key = self._get_key("user", user_id, "strategy", strategy_name, symbol, "state")
            
            # Добавление метаданных
            state_data.update({
                "updated_at": datetime.now().isoformat(),
                "strategy_name": strategy_name,
                "symbol": symbol
            })
            
            # Сериализация данных
            serialized_data = {}
            for k, v in state_data.items():
                if isinstance(v, Decimal):
                    serialized_data[k] = str(v)
                elif isinstance(v, (dict, list)):
                    serialized_data[k] = json.dumps(v)
                elif v is None:
                    serialized_data[k] = "null"
                else:
                    serialized_data[k] = str(v)
            
            result = await self._safe_execute(
                self.redis_client.hset, key, mapping=serialized_data
            )
            
            if result is not None:
                # Добавление в индекс активных стратегий
                strategy_key = f"{strategy_name}:{symbol}"
                await self.redis_client.sadd(
                    f"{self.prefix}:user:{user_id}:active_strategies", 
                    strategy_key
                )
                
                log_debug(
                    user_id, 
                    f"Состояние стратегии {strategy_name}:{symbol} сохранено", 
                    module_name=__name__
                )
                return True
                
        except Exception as e:
            log_error(
                user_id, 
                f"Ошибка сохранения состояния стратегии {strategy_name}:{symbol}: {e}", 
                module_name=__name__
            )
            
        return False

    async def get_strategy_state(
        self, 
        user_id: int, 
        strategy_name: str, 
        symbol: str
    ) -> Optional[Dict[str, Any]]:
        """Получение состояния стратегии"""
        try:
            key = self._get_key("user", user_id, "strategy", strategy_name, symbol, "state")
            state_data = await self._safe_execute(self.redis_client.hgetall, key)
            
            if not state_data:
                return None
                
            # Десериализация данных
            result = {}
            for k, v in state_data.items():
                if v == "null":
                    result[k] = None
                elif k in ["updated_at", "strategy_name", "symbol"]:
                    result[k] = v
                elif k.endswith("_data") or k.endswith("_orders"):
                    try:
                        result[k] = json.loads(v)
                    except json.JSONDecodeError:
                        result[k] = v
                else:
                    # Попытка конвертации в числовые типы
                    try:
                        if "." in v or "e" in v.lower():
                            result[k] = Decimal(v)
                        elif v.isdigit() or (v.startswith("-") and v[1:].isdigit()):
                            result[k] = int(v)
                        else:
                            result[k] = v
                    except (ValueError, TypeError):
                        result[k] = v
                        
            return result
            
        except Exception as e:
            log_error(
                user_id, 
                f"Ошибка получения состояния стратегии {strategy_name}:{symbol}: {e}", 
                module_name=__name__
            )
            return None

    async def delete_strategy_state(
        self, 
        user_id: int, 
        strategy_name: str, 
        symbol: str
    ) -> bool:
        """Удаление состояния стратегии"""
        try:
            key = self._get_key("user", user_id, "strategy", strategy_name, symbol, "state")
            result = await self._safe_execute(self.redis_client.delete, key)
            
            if result:
                # Удаление из индекса активных стратегий
                strategy_key = f"{strategy_name}:{symbol}"
                await self.redis_client.srem(
                    f"{self.prefix}:user:{user_id}:active_strategies", 
                    strategy_key
                )
                
                log_debug(
                    user_id, 
                    f"Состояние стратегии {strategy_name}:{symbol} удалено", 
                    module_name=__name__
                )
                return True
                
        except Exception as e:
            log_error(
                user_id, 
                f"Ошибка удаления состояния стратегии {strategy_name}:{symbol}: {e}", 
                module_name=__name__
            )
            
        return False

    async def get_user_active_strategies(self, user_id: int) -> List[str]:
        """Получение списка активных стратегий пользователя"""
        try:
            key = f"{self.prefix}:user:{user_id}:active_strategies"
            strategies = await self._safe_execute(self.redis_client.smembers, key)
            
            return list(strategies) if strategies else []
            
        except Exception as e:
            log_error(user_id, f"Ошибка получения активных стратегий: {e}", module_name=__name__)
            return []

    # =============================================================================
    # КЭШИРОВАНИЕ ДАННЫХ
    # =============================================================================

    async def cache_data(self, key: str, data: Any, ttl: Optional[int] = SystemConstants.CACHE_EXPIRY_SECONDS) -> bool:
        """Кэширование данных с TTL (с поддержкой постоянного хранения)"""
        try:
            cache_key = self._get_key("cache", key)

            # Сериализация данных
            if isinstance(data, (dict, list)):
                serialized_data = json.dumps(data, default=str)
            elif isinstance(data, Decimal):
                serialized_data = str(data)
            else:
                serialized_data = str(data)

            # Если ttl=None, используем команду SET для постоянного хранения.
            # Иначе используем SETEX для временного кэша.
            if ttl is None:
                result = await self._safe_execute(
                    self.redis_client.set, cache_key, serialized_data
                )
            else:
                result = await self._safe_execute(
                    self.redis_client.setex, cache_key, ttl, serialized_data
                )

            return result is not None
        except Exception as e:
            log_error(0, f"Ошибка кэширования данных {key}: {e}", module_name=__name__)
            return False

    async def get_cached_data(self, key: str) -> Optional[Any]:
        """Получение кэшированных данных"""
        try:
            cache_key = self._get_key("cache", key)
            data = await self._safe_execute(self.redis_client.get, cache_key)
            
            if data is None:
                return None
                
            # Попытка десериализации JSON
            try:
                # Сначала пытаемся декодировать как JSON
                return json.loads(data)
            except (json.JSONDecodeError, TypeError):
                # Если это не JSON, проверяем, является ли строка числом
                try:
                    # Попытка конвертации в Decimal, если это похоже на число
                    return Decimal(data)
                except Exception:
                    # Если не получилось, возвращаем как есть (например, для lock-строки "CTCUSDT")
                    return data
                    
        except Exception as e:
            log_error(0, f"Ошибка получения кэшированных данных {key}: {e}", module_name=__name__)
            return None

    async def delete_cached_data(self, key: str) -> bool:
        """Удаление кэшированных данных"""
        try:
            cache_key = self._get_key("cache", key)
            result = await self._safe_execute(self.redis_client.delete, cache_key)
            
            return result > 0
            
        except Exception as e:
            log_error(0, f"Ошибка удаления кэшированных данных {key}: {e}", module_name=__name__)
            return False

    async def add_to_list(self, key: str, value: str, max_length: int = 100) -> bool:
        """
        Добавляет значение в начало списка в Redis и обрезает список до max_length.
        """
        try:
            list_key = self._get_key("list", key)

            # Последовательно выполняем две команды. Для этой операции pipeline не обязателен.
            await self.redis_client.lpush(list_key, value)
            await self.redis_client.ltrim(list_key, 0, max_length - 1)

            return True
        except Exception as e:
            log_error(0, f"Ошибка добавления в список {key}: {e}", module_name=__name__)
            return False

    # =============================================================================
    # СТАТИСТИКА И МОНИТОРИНГ
    # =============================================================================

    async def increment_counter(self, counter_name: str, user_id: Optional[int] = None) -> int:
        """Увеличение счетчика"""
        try:
            if user_id:
                key = self._get_key("counter", counter_name, "user", user_id)
            else:
                key = self._get_key("counter", counter_name)
                
            result = await self._safe_execute(self.redis_client.incr, key)
            return result if result is not None else 0
            
        except Exception as e:
            log_error(user_id or 0, f"Ошибка увеличения счетчика {counter_name}: {e}", module_name=__name__)
            return 0

    async def get_counter(self, counter_name: str, user_id: Optional[int] = None) -> int:
        """Получение значения счетчика"""
        try:
            if user_id:
                key = self._get_key("counter", counter_name, "user", user_id)
            else:
                key = self._get_key("counter", counter_name)
                
            result = await self._safe_execute(self.redis_client.get, key)
            return int(result) if result else 0
            
        except Exception as e:
            log_error(user_id or 0, f"Ошибка получения счетчика {counter_name}: {e}", module_name=__name__)
            return 0

    async def reset_counter(self, counter_name: str, user_id: Optional[int] = None) -> bool:
        """Сброс счетчика"""
        try:
            if user_id:
                key = self._get_key("counter", counter_name, "user", user_id)
            else:
                key = self._get_key("counter", counter_name)
                
            result = await self._safe_execute(self.redis_client.delete, key)
            return result > 0
            
        except Exception as e:
            log_error(user_id or 0, f"Ошибка сброса счетчика {counter_name}: {e}", module_name=__name__)
            return False

    # =============================================================================
    # БЛОКИРОВКИ
    # =============================================================================

    async def acquire_lock(
        self, 
        lock_name: str, 
        user_id: int, 
        timeout: int = 30
    ) -> bool:
        """Получение блокировки"""
        try:
            key = self._get_key("lock", lock_name, "user", user_id)
            result = await self._safe_execute(
                self.redis_client.set, key, "locked", nx=True, ex=timeout
            )
            
            if result:
                log_debug(user_id, f"Блокировка {lock_name} получена", module_name=__name__)
                return True
            else:
                log_debug(user_id, f"Блокировка {lock_name} уже занята", module_name=__name__)
                return False
                
        except Exception as e:
            log_error(user_id, f"Ошибка получения блокировки {lock_name}: {e}", module_name=__name__)
            return False

    async def release_lock(self, lock_name: str, user_id: int) -> bool:
        """Освобождение блокировки"""
        try:
            key = self._get_key("lock", lock_name, "user", user_id)
            result = await self._safe_execute(self.redis_client.delete, key)
            
            if result:
                log_debug(user_id, f"Блокировка {lock_name} освобождена", module_name=__name__)
                return True
            else:
                log_debug(user_id, f"Блокировка {lock_name} не найдена", module_name=__name__)
                return False
                
        except Exception as e:
            log_error(user_id, f"Ошибка освобождения блокировки {lock_name}: {e}", module_name=__name__)
            return False

    # =============================================================================
    # ОЧИСТКА ДАННЫХ
    # =============================================================================

    async def cleanup_user_data(self, user_id: int):
        """Полная очистка данных пользователя"""
        try:
            # Получение всех ключей пользователя
            pattern = f"{self.prefix}:user:{user_id}:*"
            keys = await self._safe_execute(self.redis_client.keys, pattern)
            
            if keys:
                # Удаление всех ключей пользователя
                await self._safe_execute(self.redis_client.delete, *keys)
                
                # Удаление из индексов
                await self.redis_client.srem(f"{self.prefix}:active_sessions", str(user_id))
                await self.redis_client.srem(f"{self.prefix}:indexes:users", str(user_id))
                
                # Очистка кэша
                cache_keys_to_remove = [
                    k for k in self._config_cache.keys() 
                    if k.startswith(f"{user_id}:")
                ]
                for cache_key in cache_keys_to_remove:
                    self._config_cache.pop(cache_key, None)
                    self._cache_timestamps.pop(cache_key, None)
                
                log_info(user_id, f"Данные пользователя очищены: {len(keys)} ключей", module_name=__name__)
            else:
                log_info(user_id, "Данные пользователя не найдены", module_name=__name__)
                
        except Exception as e:
            log_error(user_id, f"Ошибка очистки данных пользователя: {e}", module_name=__name__)

    async def cleanup_expired_data(self):
        """Очистка устаревших данных"""
        try:
            # Очистка устаревших блокировок
            pattern = f"{self.prefix}:lock:*"
            keys = await self._safe_execute(self.redis_client.keys, pattern)
            
            if keys:
                expired_locks = []
                for key in keys:
                    ttl = await self.redis_client.ttl(key)
                    if ttl == -1:  # Ключ без TTL
                        expired_locks.append(key)
                
                if expired_locks:
                    await self._safe_execute(self.redis_client.delete, *expired_locks)
                    log_info(0, f"Очищены устаревшие блокировки: {len(expired_locks)}", module_name=__name__)
            
            # Очистка старых кэшированных данных
            cache_pattern = f"{self.prefix}:cache:*"
            cache_keys = await self._safe_execute(self.redis_client.keys, cache_pattern)
            
            if cache_keys:
                expired_cache = []
                for key in cache_keys:
                    ttl = await self.redis_client.ttl(key)
                    if ttl == -1:  # Ключ без TTL
                        expired_cache.append(key)
                
                if expired_cache:
                    await self._safe_execute(self.redis_client.delete, *expired_cache)
                    log_info(0, f"Очищены устаревшие кэшированные данные: {len(expired_cache)}", module_name=__name__)
            
            # Очистка локального кэша
            current_time = time.time()
            expired_local_cache = [
                k for k, timestamp in self._cache_timestamps.items()
                if current_time - timestamp > SystemConstants.CONFIG_CACHE_SECONDS * 2
            ]
            
            for cache_key in expired_local_cache:
                self._config_cache.pop(cache_key, None)
                self._cache_timestamps.pop(cache_key, None)
            
            if expired_local_cache:
                log_info(0, f"Очищен локальный кэш: {len(expired_local_cache)} записей", module_name=__name__)
                
        except Exception as e:
            log_error(0, f"Ошибка очистки устаревших данных: {e}", module_name=__name__)

    async def get_system_stats(self) -> Dict[str, Any]:
        """Получение статистики системы"""
        try:
            stats = {
                "active_users": len(await self.get_active_users()),
                "total_keys": 0,
                "memory_usage": "N/A",
                "cache_size": len(self._config_cache),
                "uptime": "N/A"
            }
            
            # Получение информации о Redis
            info = await self._safe_execute(self.redis_client.info)
            if info:
                stats.update({
                    "memory_usage": info.get("used_memory_human", "N/A"),
                    "uptime": info.get("uptime_in_seconds", 0),
                    "total_keys": info.get("db0", {}).get("keys", 0) if "db0" in info else 0
                })
            
            return stats
            
        except Exception as e:
            log_error(0, f"Ошибка получения статистики системы: {e}", module_name=__name__)
            return {}


# Глобальный экземпляр
redis_manager = RedisManager()

