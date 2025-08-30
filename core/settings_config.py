"""
Профессиональная система конфигураций для многопользовательского торгового бота
"""
import os
from environs import Env, EnvError
from dataclasses import dataclass, field
from typing import List, Dict, Any, Optional
from decimal import Decimal
from pathlib import Path
import json

from core.logger import log_info, log_error
from core.enums import ExchangeType, AccountType, SystemConstants


# Константы системы
DEFAULT_SYMBOLS = [
    'BTCUSDT', 'ETHUSDT', 'ADAUSDT', 'SOLUSDT', 'DOTUSDT',
    'XRPUSDT', 'LINKUSDT', 'AVAXUSDT', 'ATOMUSDT',
    'NEARUSDT', 'ICPUSDT'
]

# Комиссии бирж (в процентах)
EXCHANGE_FEES = {
    ExchangeType.BYBIT: {
        'maker': Decimal('0.02'),    # 0.02%
        'taker': Decimal('0.055'),   # 0.055%
    },
    ExchangeType.BINANCE: {
        'maker': Decimal('0.02'),    # 0.02%
        'taker': Decimal('0.04'),    # 0.04%
    },
    ExchangeType.OKX: {
        'maker': Decimal('0.08'),    # 0.08%
        'taker': Decimal('0.10'),    # 0.10%
    }
}

# Лимиты API запросов (запросов в секунду)
API_RATE_LIMITS = {
    ExchangeType.BYBIT: {
        'public': 120,      # 120 запросов в секунду
        'private': 100,     # 100 запросов в секунду
        'order': 50,        # 50 ордеров в секунду
    },
    ExchangeType.BINANCE: {
        'public': 1200,     # 1200 запросов в минуту
        'private': 6000,    # 6000 запросов в минуту
        'order': 100,       # 100 ордеров в 10 секунд
    }
}

# Настройки WebSocket
WEBSOCKET_CONFIG = {
    'reconnect_interval': 5,        # секунд
    'max_reconnect_attempts': 10,   # попыток
    'ping_interval': 20,            # секунд
    'ping_timeout': 10,             # секунд
    'message_timeout': 30,          # секунд
}

# Настройки Redis
REDIS_CONFIG = {
    'connection_pool_size': 20,
    'socket_timeout': 5,
    'socket_connect_timeout': 5,
    'retry_on_timeout': True,
    'health_check_interval': 30,
    'max_connections': 50,
}

# Настройки логирования
LOGGING_CONFIG = {
    'max_file_size': 10 * 1024 * 1024,  # 10MB
    'backup_count': 5,
    'log_retention_days': 30,
    'console_level': 'INFO',
    'file_level': 'DEBUG',
}

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
    socket_connect_timeout: int = 5
    retry_on_timeout: bool = True
    health_check_interval: int = 30

@dataclass
class ExchangeConfig:
    """Конфигурация биржи"""
    exchange_type: ExchangeType
    api_key: str
    secret_key: str
    passphrase: Optional[str] = None
    sandbox: bool = False
    base_url: Optional[str] = None

    # Настройки торговли
    default_leverage: int = 1
    max_leverage: int = 20
    account_type: AccountType = AccountType.UNIFIED

    # Лимиты
    min_order_size: Decimal = Decimal('5')      # Минимальный размер ордера в USDT
    max_order_size: Decimal = Decimal('10000')  # Максимальный размер ордера в USDT
    max_position_size: Decimal = Decimal('50000') # Максимальный размер позиции в USDT

@dataclass
class TelegramConfig:
    """Конфигурация Telegram бота"""
    token: str
    admin_ids: List[int] = field(default_factory=list)
    webhook_url: Optional[str] = None
    webhook_port: int = 8443
    max_connections: int = 40
    allowed_updates: List[str] = field(default_factory=lambda: [
        "message", "callback_query", "inline_query"
    ])

@dataclass
class SecurityConfig:
    """Настройки безопасности"""
    encryption_key: str
    jwt_secret: str
    session_timeout: int = 3600  # 1 час
    max_login_attempts: int = 5
    lockout_duration: int = 900  # 15 минут
    password_min_length: int = 8
    require_2fa: bool = False

@dataclass
class MonitoringConfig:
    """Настройки мониторинга"""
    enable_metrics: bool = True
    metrics_port: int = 9090
    health_check_interval: int = 60
    alert_webhook_url: Optional[str] = None
    log_level: str = "INFO"

    # Пороги для алертов
    max_memory_usage: float = 0.8    # 80%
    max_cpu_usage: float = 0.7       # 70%
    max_error_rate: float = 0.05     # 5%

@dataclass
class SystemConfig:
    """Главная конфигурация системы"""
    # Основные компоненты
    database: DatabaseConfig
    redis: RedisConfig
    telegram: TelegramConfig
    security: SecurityConfig
    monitoring: MonitoringConfig

    # Биржи (может быть несколько)
    exchanges: Dict[str, ExchangeConfig] = field(default_factory=dict)

    # Общие настройки
    environment: str = "production"  # production, staging, development
    debug: bool = False
    timezone: str = "UTC"

    # Торговые настройки по умолчанию
    default_symbols: List[str] = field(default_factory=lambda: DEFAULT_SYMBOLS.copy())
    supported_timeframes: List[str] = field(default_factory=lambda: [
        '1m', '3m', '5m', '15m', '30m', '1h', '2h', '4h', '6h', '12h', '1d'
    ])

    # Лимиты системы
    max_users: int = 1000
    max_strategies_per_user: int = 10
    max_active_orders_per_user: int = 50
    max_positions_per_user: int = 20

    # Настройки производительности
    worker_threads: int = 4
    max_concurrent_requests: int = 100
    request_timeout: int = 30

    def get_exchange_config(self, exchange_name: str) -> Optional[ExchangeConfig]:
        """Получить конфигурацию биржи"""
        return self.exchanges.get(exchange_name)

    def add_exchange_config(self, name: str, exchange_config: ExchangeConfig):
        """Добавить конфигурацию биржи"""
        self.exchanges[name] = exchange_config

    @staticmethod
    def get_fee_rate(exchange_type: ExchangeType, order_type: str) -> Decimal:
        """Получить ставку комиссии"""
        fees = EXCHANGE_FEES.get(exchange_type, {})
        return fees.get(order_type.lower(), Decimal('0.1'))  # По умолчанию 0.1%

    @staticmethod
    def get_rate_limit(exchange_type: ExchangeType, endpoint_type: str) -> int:
        """Получить лимит запросов"""
        limits = API_RATE_LIMITS.get(exchange_type, {})
        return limits.get(endpoint_type, 10)  # По умолчанию 10 запросов в секунду

    def is_production(self) -> bool:
        """Проверка production окружения"""
        return self.environment.lower() == "production"

    def is_development(self) -> bool:
        """Проверка development окружения"""
        return self.environment.lower() == "development"

class ConfigLoader:
    """Загрузчик конфигураций"""

    def __init__(self, env_file: str = ".env"):
        self.env_file = env_file
        self.env = Env()

        # Читаем переменные окружения
        if Path(env_file).exists():
            self.env.read_env(env_file)

    def load_config(self) -> SystemConfig:
        """Загрузка полной конфигурации системы"""
        try:
            # Основные компоненты
            database_config = self._load_database_config()
            redis_config = self._load_redis_config()
            telegram_config = self._load_telegram_config()
            security_config = self._load_security_config()
            monitoring_config = self._load_monitoring_config()

            # Создаем главную конфигурацию
            system_config_obj = SystemConfig(
                database=database_config,
                redis=redis_config,
                telegram=telegram_config,
                security=security_config,
                monitoring=monitoring_config,
                environment=self.env.str("ENVIRONMENT", "production"),
                debug=False,
                timezone="Europe/Moscow",
                max_users=self.env.int("MAX_USERS", 1000),
                worker_threads=self.env.int("WORKER_THREADS", 4),
            )

            # Загружаем конфигурации бирж
            self._load_exchange_configs(system_config_obj)

            log_info(0, "Конфигурация системы успешно загружена", module_name='system_config')
            return system_config_obj

        except Exception as err:
            log_error(0, f"Ошибка загрузки конфигурации: {err}", module_name='system_config')
            raise ValueError(f"Критическая ошибка загрузки конфигурации: {err}")

    def _load_database_config(self) -> DatabaseConfig:
        """Загрузка конфигурации базы данных"""
        return DatabaseConfig(
            url=self.env.str("DATABASE_URL"),
            pool_size=self.env.int("DB_POOL_SIZE", 20),
            max_overflow=self.env.int("DB_MAX_OVERFLOW", 30),
            pool_timeout=self.env.int("DB_POOL_TIMEOUT", 30),
            pool_recycle=self.env.int("DB_POOL_RECYCLE", 3600),
            echo=self.env.bool("DB_ECHO", False),
        )

    def _load_redis_config(self) -> RedisConfig:
        """Загрузка конфигурации Redis"""
        return RedisConfig(
            url=self.env.str("REDIS_URL"),
            db=self.env.int("REDIS_DB", 0),
            max_connections=self.env.int("REDIS_MAX_CONNECTIONS", 50),
            socket_timeout=self.env.int("REDIS_SOCKET_TIMEOUT", 5),
            socket_connect_timeout=self.env.int("REDIS_CONNECT_TIMEOUT", 5),
            retry_on_timeout=self.env.bool("REDIS_RETRY_ON_TIMEOUT", True),
            health_check_interval=self.env.int("REDIS_HEALTH_CHECK_INTERVAL", 30),
        )

    def _load_telegram_config(self) -> TelegramConfig:
        """Загрузка конфигурации Telegram"""
        admin_ids_str = self.env.str("ADMIN_IDS", "")
        admin_ids = [int(admin_id_str.strip()) for admin_id_str in admin_ids_str.split(",") if admin_id_str.strip().isdigit()]

        return TelegramConfig(
            token=self.env.str("TELEGRAM_TOKEN"),
            admin_ids=admin_ids,
            webhook_url=self.env.str("WEBHOOK_URL", None),
            webhook_port=self.env.int("WEBHOOK_PORT", 8443),
            max_connections=self.env.int("TELEGRAM_MAX_CONNECTIONS", 40),
        )

    def _load_security_config(self) -> SecurityConfig:
        """Загрузка настроек безопасности"""
        return SecurityConfig(
            encryption_key=self.env.str("ENCRYPTION_KEY"),
            jwt_secret=self.env.str("JWT_SECRET"),
            session_timeout=self.env.int("SESSION_TIMEOUT", 3600),
            max_login_attempts=self.env.int("MAX_LOGIN_ATTEMPTS", 5),
            lockout_duration=self.env.int("LOCKOUT_DURATION", 900),
            password_min_length=self.env.int("PASSWORD_MIN_LENGTH", 8),
            require_2fa=self.env.bool("REQUIRE_2FA", False),
        )

    def _load_monitoring_config(self) -> MonitoringConfig:
        """Загрузка настроек мониторинга"""
        return MonitoringConfig(
            enable_metrics=self.env.bool("ENABLE_METRICS", True),
            metrics_port=self.env.int("METRICS_PORT", 9090),
            health_check_interval=self.env.int("HEALTH_CHECK_INTERVAL", 60),
            alert_webhook_url=self.env.str("ALERT_WEBHOOK_URL", None),
            log_level=self.env.str("LOG_LEVEL", "INFO"),
            max_memory_usage=self.env.float("MAX_MEMORY_USAGE", 0.8),
            max_cpu_usage=self.env.float("MAX_CPU_USAGE", 0.7),
            max_error_rate=self.env.float("MAX_ERROR_RATE", 0.05),
        )

    def _load_exchange_configs(self, system_config_obj: SystemConfig):
        """Загрузка конфигураций бирж"""
        # Bybit
        if self.env.str("BYBIT_API_KEY", None) and self.env.str("BYBIT_SECRET_KEY", None):
            bybit_config = ExchangeConfig(
                exchange_type=ExchangeType.BYBIT,
                api_key=self.env.str("BYBIT_API_KEY"),
                secret_key=self.env.str("BYBIT_SECRET_KEY"),
                sandbox=self.env.bool("BYBIT_SANDBOX", False),
                base_url=self.env.str("BYBIT_BASE_URL", "https://api.bybit.com"),
                default_leverage=self.env.int("BYBIT_DEFAULT_LEVERAGE", 1),
                max_leverage=self.env.int("BYBIT_MAX_LEVERAGE", 5),
                min_order_size=Decimal(self.env.str("BYBIT_MIN_ORDER_SIZE", "5")),
                max_order_size=Decimal(self.env.str("BYBIT_MAX_ORDER_SIZE", "10000")),
            )
            system_config_obj.add_exchange_config("bybit", bybit_config)

        # Binance
        if self.env.str("BINANCE_API_KEY", None) and self.env.str("BINANCE_SECRET_KEY", None):
            binance_config = ExchangeConfig(
                exchange_type=ExchangeType.BINANCE,
                api_key=self.env.str("BINANCE_API_KEY"),
                secret_key=self.env.str("BINANCE_SECRET_KEY"),
                sandbox=self.env.bool("BINANCE_SANDBOX", False),
                base_url=self.env.str("BINANCE_BASE_URL", "https://api.binance.com"),
                default_leverage=self.env.int("BINANCE_DEFAULT_LEVERAGE", 1),
                max_leverage=self.env.int("BINANCE_MAX_LEVERAGE", 20),
            )
            config.add_exchange_config("binance", binance_config)

def load_system_config(env_file: str = ".env") -> SystemConfig:
    """Загрузка системной конфигурации"""
    loader = ConfigLoader(env_file)
    return loader.load_config()

def validate_config(config_to_validate: SystemConfig) -> bool:
    """Валидация конфигурации"""
    try:
        # Проверяем обязательные поля
        if not config_to_validate.database.url:
            raise ValueError("DATABASE_URL не задан")

        if not config_to_validate.redis.url:
            raise ValueError("REDIS_URL не задан")

        if not config_to_validate.telegram.token:
            raise ValueError("TELEGRAM_TOKEN не задан")

        if not config_to_validate.security.encryption_key:
            raise ValueError("ENCRYPTION_KEY не задан")

        # Проверяем наличие хотя бы одной биржи
        if not config_to_validate.exchanges:
            raise ValueError("Не настроена ни одна биржа")

        # Проверяем конфигурации бирж
        for name, exchange_config in config_to_validate.exchanges.items():
            if not exchange_config.api_key or not exchange_config.secret_key:
                raise ValueError(f"API ключи для биржи {name} не заданы")

        log_info(0, "Валидация конфигурации прошла успешно", module_name="system_config")
        return True

    except Exception as err:
        log_error(0, f"Ошибка валидации конфигурации: {err}", module_name="system_config")
        return False

# Глобальная конфигурация (загружается при импорте)
try:
    system_config = load_system_config()
    if not validate_config(system_config):
        raise ValueError("Конфигурация не прошла валидацию")
except Exception as e:
    log_error(0, f"Критическая ошибка инициализации конфигурации: {e}", module_name='system_config')
    raise

# Экспортируем для обратной совместимости
config = system_config

