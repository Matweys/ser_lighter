# core/logger.py
"""
Простая и эффективная система логирования для многопользовательской торговой системы
Логирует все события в единый файл в удобном для чтения формате.
"""
import logging
import logging.handlers
import sys
from datetime import datetime
from pathlib import Path
from typing import Dict, Any, Optional


class SimpleLogFormatter(logging.Formatter):
    """
    Простой форматтер для вывода логов в одну строку.
    Формат: [TIMESTAMP] | LEVEL | User:USER_ID | MODULE | MESSAGE
    """

    def format(self, record):
        # Добавляем user_id и module_name в запись, если их нет
        user_id = getattr(record, 'user_id', 0)
        module_name = getattr(record, 'module_name', 'Unknown')

        # Используем выравнивание для красивого вывода
        log_format = (
            f"{datetime.fromtimestamp(record.created).isoformat()} | "
            f"{record.levelname:<8} | "
            f"User:{str(user_id):<10} | "
            f"{module_name:<20} | "
            f"{record.getMessage()}"
        )
        return log_format


class TradingLogger:
    """
    Единая система логирования, которая направляет все сообщения в один файл.
    """

    def __init__(self, log_dir: str = "logs", log_file: str = "trading_bot.log"):
        self.log_dir = Path(log_dir)
        self.log_dir.mkdir(exist_ok=True)
        self.log_file = log_file
        self.logger: Optional[logging.Logger] = None
        self._setup_logger()
        # Запускаем очистку старых логов при каждой инициализации
        self.cleanup_old_logs(days=60)

    def _setup_logger(self):
        """Настройка единого логгера."""

        logger = logging.getLogger("TradingBot")
        logger.setLevel(logging.INFO)  # Устанавливаем общий уровень INFO
        logger.propagate = False

        # Очищаем существующие обработчики, чтобы избежать дублирования
        if logger.hasHandlers():
            logger.handlers.clear()

        # Обработчик для записи в единый файл с ротацией
        file_handler = logging.handlers.RotatingFileHandler(
            filename=self.log_dir / self.log_file,
            maxBytes=20 * 1024 * 1024,  # 20MB
            backupCount=5,
            encoding='utf-8'
        )
        file_handler.setFormatter(SimpleLogFormatter())
        logger.addHandler(file_handler)

        # Обработчик для вывода в консоль
        console_handler = logging.StreamHandler(sys.stdout)
        console_handler.setFormatter(SimpleLogFormatter())
        logger.addHandler(console_handler)

        self.logger = logger

    def log(
            self,
            level: str,
            user_id: int,
            message: str,
            module_name: str = "Unknown",
            extra_data: Optional[Dict[str, Any]] = None
    ):
        """Универсальный метод логирования."""

        extra = {
            'user_id': user_id,
            'module_name': module_name
        }

        # Добавляем extra_data в сообщение, если оно есть, для наглядности
        if extra_data:
            message += f" | data: {extra_data}"

        log_level = getattr(logging, level.upper(), logging.INFO)

        self.logger.log(log_level, message, extra=extra)

    def cleanup_old_logs(self, days: int):
        """Удаляет лог-файлы из директории логов, которые старше указанного количества дней."""
        try:
            cutoff = time.time() - (days * 86400)  # 86400 секунд в дне
            files_deleted_count = 0

            for file_path in self.log_dir.glob(f"{self.log_file}*"):
                if file_path.is_file():
                    if file_path.stat().st_mtime < cutoff:
                        file_path.unlink()
                        files_deleted_count += 1

            if files_deleted_count > 0:
                self.log("INFO", 0, f"Удалено {files_deleted_count} старых лог-файлов (старше {days} дней).",
                         "LogManager")

        except Exception as e:
            self.log("ERROR", 0, f"Ошибка при очистке старых логов: {e}", "LogManager")


# Глобальный экземпляр логгера
trading_logger = TradingLogger()


# =============================================================================
# ПУБЛИЧНЫЕ ФУНКЦИИ ЛОГИРОВАНИЯ (теперь все они пишут в один логгер)
# =============================================================================

def log_info(user_id: int, message: str, module_name: str = "Unknown", extra_data: Optional[Dict[str, Any]] = None):
    trading_logger.log("INFO", user_id, message, module_name, extra_data)


def log_error(user_id: int, message: str, module_name: str = "Unknown", extra_data: Optional[Dict[str, Any]] = None):
    trading_logger.log("ERROR", user_id, message, module_name, extra_data)


def log_warning(user_id: int, message: str, module_name: str = "Unknown", extra_data: Optional[Dict[str, Any]] = None):
    trading_logger.log("WARNING", user_id, message, module_name, extra_data)


def log_debug(user_id: int, message: str, module_name: str = "Unknown", extra_data: Optional[Dict[str, Any]] = None):
    # Для debug можно временно понизить уровень логгера
    # trading_logger.logger.setLevel(logging.DEBUG)
    trading_logger.log("DEBUG", user_id, message, module_name, extra_data)


def log_critical(user_id: int, message: str, module_name: str = "Unknown", extra_data: Optional[Dict[str, Any]] = None):
    trading_logger.log("CRITICAL", user_id, message, module_name, extra_data)