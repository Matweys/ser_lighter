# core/settings_config.py

"""
Профессиональная система конфигураций для многопользовательского торгового бота.
Загружает настройки из .env и предоставляет структурированный доступ к ним.
"""
import os
from environs import Env
from dataclasses import dataclass, field
from typing import List, Dict, Any, Optional
from decimal import Decimal
from pathlib import Path

from core.logger import log_info, log_error
from core.enums import ExchangeType, AccountType

# --- КОНСТАНТЫ, КОТОРЫЕ ИСПОЛЬЗУЮТСЯ ИЛИ БУДУТ ИСПОЛЬЗОВАТЬСЯ ---

# Константы системы
DEFAULT_SYMBOLS = [
    'BTCUSDT', 'ETHUSDT', 'ADAUSDT', 'SOLUSDT', 'ASTERUSDT',
    'XRPUSDT', 'LINKUSDT', 'MYXUSDT', 'ATOMUSDT',
    'NEARUSDT', 'ICPUSDT'
]
# Комиссии бирж для внутреннего расчета PnL
EXCHANGE_FEES = {
    ExchangeType.BYBIT: {
        'maker': Decimal('0.036'),    # 0.036% (Maker)
        'taker': Decimal('0.1'),      # 0.1% (Taker)
    },
    # Можно добавить другие биржи в будущем
}

# --- ОСНОВНЫЕ ДАТА-КЛАССЫ КОНФИГУРАЦИИ ---

@dataclass
class DatabaseConfig:
    """Конфигурация базы данных"""
    url: str
    pool_size: int = 20
    max_overflow: int = 30
    pool_timeout: int = 30
    pool_recycle: int = 3600
    echo: bool = False

@dataclass
class RedisConfig:
    """Конфигурация Redis"""
    url: str
    db: int = 0
    max_connections: int = 50
    socket_timeout: int = 5
    socket_connect_timeout: int = 10  
    retry_on_timeout: bool = True  
    health_check_interval: int = 30

@dataclass
class ExchangeConfig:
    """Конфигурация биржи"""
    exchange_type: ExchangeType
    api_key: str
    secret_key: str
    demo: bool = False
    base_url: Optional[str] = None
    default_leverage: int = 5

@dataclass
class TelegramConfig:
    """Конфигурация Telegram бота"""
    token: str
    admin_ids: List[int] = field(default_factory=list)
    allowed_updates: List[str] = field(default_factory=lambda: ["message", "callback_query"])
    max_connections: int = 40

@dataclass
class SystemConfig:
    """Главная, корневая конфигурация системы"""
    database: DatabaseConfig
    redis: RedisConfig
    telegram: TelegramConfig
    exchanges: Dict[str, ExchangeConfig] = field(default_factory=dict)
    environment: str = "production"
    encryption_key: str = "" # Ключ для шифрования API ключей в БД

    def get_exchange_config(self, name: str) -> Optional[ExchangeConfig]:
        """Получить конфигурацию конкретной биржи."""
        return self.exchanges.get(name.lower())

    def add_exchange_config(self, name: str, config: ExchangeConfig):
        """Добавить конфигурацию биржи."""
        self.exchanges[name.lower()] = config

    def is_production(self) -> bool:
        """Проверка, является ли окружение production."""
        return self.environment.lower() == "production"

# --- КЛАСС ДЛЯ ЗАГРУЗКИ КОНФИГУРАЦИИ ИЗ .ENV ---

class ConfigLoader:
    """Загрузчик конфигураций из файла .env."""

    def __init__(self, env_file: str = ".env"):
        self.env = Env()
        if Path(env_file).exists():
            self.env.read_env(env_file)
        else:
            log_info(0, f"Файл .env не найден. Используются переменные окружения системы.", 'system_config')

    def load_config(self) -> SystemConfig:
        """Загрузка и валидация полной конфигурации системы."""
        try:
            # Загружаем основные компоненты
            db_config = self._load_database_config()
            redis_config = self._load_redis_config()
            telegram_config = self._load_telegram_config()

            # Создаем главный объект конфигурации
            system_config_obj = SystemConfig(
                database=db_config,
                redis=redis_config,
                telegram=telegram_config,
                environment=self.env.str("ENVIRONMENT", "production"),
                encryption_key=self.env.str("ENCRYPTION_KEY")
            )

            # Загружаем конфигурации для каждой биржи, указанной в .env
            self._load_exchange_configs(system_config_obj)

            # Валидация
            self._validate_config(system_config_obj)

            log_info(0, "Конфигурация системы успешно загружена и валидирована.", 'system_config')
            return system_config_obj

        except Exception as err:
            log_error(0, f"Критическая ошибка загрузки конфигурации: {err}", 'system_config')
            raise

    def _load_database_config(self) -> DatabaseConfig:
        return DatabaseConfig(url=self.env.str("DATABASE_URL"))

    def _load_redis_config(self) -> RedisConfig:
        return RedisConfig(url=self.env.str("REDIS_URL"))

    def _load_telegram_config(self) -> TelegramConfig:
        admin_ids_str = self.env.str("ADMIN_IDS", "")
        admin_ids = [int(uid.strip()) for uid in admin_ids_str.split(',') if uid.strip().isdigit()]
        return TelegramConfig(token=self.env.str("TELEGRAM_TOKEN"), admin_ids=admin_ids)

    def _load_exchange_configs(self, config: SystemConfig):
        # Bybit
        if self.env.str("BYBIT_API_KEY", None):
            bybit_config = ExchangeConfig(
                exchange_type=ExchangeType.BYBIT,
                api_key=self.env.str("BYBIT_API_KEY"),
                secret_key=self.env.str("BYBIT_SECRET_KEY"),
                demo=self.env.bool("BYBIT_DEMO", False),
                base_url=self.env.str("BYBIT_BASE_URL", "https://api.bybit.com" ),
                default_leverage=self.env.int("BYBIT_DEFAULT_LEVERAGE", 5)
            )
            config.add_exchange_config("bybit", bybit_config)
        # Можно добавить другие биржи по аналогии
    @staticmethod
    def _validate_config(config: SystemConfig):
        """Простая валидация ключевых полей."""
        if not config.database.url: raise ValueError("DATABASE_URL не задан в .env")
        if not config.redis.url: raise ValueError("REDIS_URL не задан в .env")
        if not config.telegram.token: raise ValueError("TELEGRAM_TOKEN не задан в .env")
        if not config.encryption_key: raise ValueError("ENCRYPTION_KEY не задан в .env")
        if not config.exchanges: raise ValueError("Не настроена ни одна биржа (например, BYBIT_API_KEY).")

# --- ГЛОБАЛЬНЫЙ ЭКЗЕМПЛЯР КОНФИГУРАЦИИ ---

def load_system_config(env_file: str = ".env") -> SystemConfig:
    """Фабричная функция для загрузки конфигурации."""
    loader = ConfigLoader(env_file)
    return loader.load_config()

# Загружаем конфигурацию при импорте модуля
try:
    system_config = load_system_config()
except Exception as e:
    log_error(0, f"Не удалось загрузить конфигурацию. Убедитесь, что файл .env существует и настроен правильно. Ошибка: {e}", 'system_config')
    # В случае ошибки создаем пустой конфиг, чтобы избежать падения при импорте
    system_config = SystemConfig(
        database=DatabaseConfig(url=""),
        redis=RedisConfig(url=""),
        telegram=TelegramConfig(token="")
    )
