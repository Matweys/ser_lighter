# core/logger.py
"""
Профессиональная система логирования для многопользовательской торговой системы
Поддерживает структурированное логирование, ротацию файлов и различные уровни детализации
"""
import logging
import logging.handlers
import sys
import json
import traceback
from datetime import datetime
from pathlib import Path
from typing import Dict, Any, Optional
from enum import Enum

from core.enums import LogLevel


class LogFormatter(logging.Formatter):
    """Кастомный форматтер для структурированного логирования"""
    
    def __init__(self):
        super().__init__()

    def format(self, record):
        # Базовая информация
        log_entry = {
            "timestamp": datetime.fromtimestamp(record.created).isoformat(),
            "level": record.levelname,
            "logger": record.name,
            "message": record.getMessage(),
            "module": getattr(record, 'module_name', 'Unknown'),
            "user_id": getattr(record, 'user_id', 0),
            "thread": record.thread,
            "process": record.process
        }
        
        # Дополнительные поля
        if hasattr(record, 'extra_data'):
            log_entry.update(record.extra_data)
            
        # Информация об исключении
        if record.exc_info:
            log_entry["exception"] = {
                "type": record.exc_info[0].__name__,
                "message": str(record.exc_info[1]),
                "traceback": traceback.format_exception(record.exc_info[1])
            }
            
        # Форматирование для консоли (читаемый вид)
        if hasattr(record, 'console_format') and record.console_format:
            return f"{log_entry['timestamp']} | {log_entry['level']:8} | User:{log_entry['user_id']:6} | {log_entry['module']:20} | {log_entry['message']}"
        
        # JSON формат для файлов
        return json.dumps(log_entry, ensure_ascii=False, separators=(',', ':'))


class TradingLogger:
    """
    Профессиональная система логирования для торговой системы
    
    Особенности:
    - Структурированное JSON логирование
    - Ротация файлов по размеру и времени
    - Различные уровни детализации
    - Контекстное логирование с метаданными
    - Отдельные логи для разных компонентов
    """
    
    def __init__(self, log_dir: str = "logs"):
        self.log_dir = Path(log_dir)
        self.log_dir.mkdir(exist_ok=True)
        
        # Основные логгеры
        self.loggers: Dict[str, logging.Logger] = {}
        
        # Настройка логгеров
        self._setup_loggers()
        
    def _setup_loggers(self):
        """Настройка различных логгеров"""
        
        # Основной логгер системы
        self._create_logger(
            name="system",
            filename="system.log",
            level=logging.INFO,
            console=True
        )
        
        # Логгер торговых операций
        self._create_logger(
            name="trading",
            filename="trading.log",
            level=logging.INFO,
            console=False
        )
        
        # Логгер API операций
        self._create_logger(
            name="api",
            filename="api.log",
            level=logging.DEBUG,
            console=False
        )
        
        # Логгер стратегий
        self._create_logger(
            name="strategies",
            filename="strategies.log",
            level=logging.INFO,
            console=False
        )
        
        # Логгер риск-менеджмента
        self._create_logger(
            name="risk",
            filename="risk.log",
            level=logging.WARNING,
            console=True
        )
        
        # Логгер ошибок
        self._create_logger(
            name="errors",
            filename="errors.log",
            level=logging.ERROR,
            console=True
        )
        
        # Логгер WebSocket
        self._create_logger(
            name="websocket",
            filename="websocket.log",
            level=logging.INFO,
            console=False
        )
        
        # Логгер событий
        self._create_logger(
            name="events",
            filename="events.log",
            level=logging.DEBUG,
            console=False
        )
        
    def _create_logger(
        self, 
        name: str, 
        filename: str, 
        level: int = logging.INFO,
        console: bool = False
    ):
        """Создание логгера с настройками"""
        
        logger = logging.getLogger(f"TradingBot.{name}")
        logger.setLevel(level)
        logger.propagate = False
        
        # Очистка существующих обработчиков
        logger.handlers.clear()
        
        # Файловый обработчик с ротацией
        file_handler = logging.handlers.RotatingFileHandler(
            filename=self.log_dir / filename,
            maxBytes=10 * 1024 * 1024,  # 10MB
            backupCount=5,
            encoding='utf-8'
        )
        file_handler.setFormatter(LogFormatter())
        logger.addHandler(file_handler)
        
        # Консольный обработчик (если нужен)
        if console:
            console_handler = logging.StreamHandler(sys.stdout)
            console_formatter = LogFormatter()
            console_handler.setFormatter(console_formatter)
            logger.addHandler(console_handler)
            
        self.loggers[name] = logger
        
    def get_logger(self, name: str) -> logging.Logger:
        """Получение логгера по имени"""
        return self.loggers.get(name, self.loggers["system"])
        
    def log(
        self,
        logger_name: str,
        level: str,
        user_id: int,
        message: str,
        module_name: str = "Unknown",
        extra_data: Optional[Dict[str, Any]] = None,
        console_format: bool = False
    ):
        """Универсальный метод логирования"""
        
        logger = self.get_logger(logger_name)
        
        # Создание записи с дополнительными данными
        extra = {
            'user_id': user_id,
            'module_name': module_name,
            'console_format': console_format
        }
        
        if extra_data:
            extra['extra_data'] = extra_data
            
        # Определение уровня логирования
        log_level = getattr(logging, level.upper(), logging.INFO)
        
        logger.log(log_level, message, extra=extra)


# Глобальный экземпляр логгера
trading_logger = TradingLogger()


# =============================================================================
# ПУБЛИЧНЫЕ ФУНКЦИИ ЛОГИРОВАНИЯ
# =============================================================================

def log_info(
    user_id: int, 
    message: str, 
    module_name: str = "Unknown",
    extra_data: Optional[Dict[str, Any]] = None,
    logger_name: str = "system"
):
    """Логирование информационных сообщений"""
    trading_logger.log(
        logger_name=logger_name,
        level="INFO",
        user_id=user_id,
        message=message,
        module_name=module_name,
        extra_data=extra_data,
        console_format=True
    )


def log_error(
    user_id: int, 
    message: str, 
    module_name: str = "Unknown",
    extra_data: Optional[Dict[str, Any]] = None,
    logger_name: str = "errors"
):
    """Логирование ошибок"""
    trading_logger.log(
        logger_name=logger_name,
        level="ERROR",
        user_id=user_id,
        message=message,
        module_name=module_name,
        extra_data=extra_data,
        console_format=True
    )


def log_warning(
    user_id: int, 
    message: str, 
    module_name: str = "Unknown",
    extra_data: Optional[Dict[str, Any]] = None,
    logger_name: str = "system"
):
    """Логирование предупреждений"""
    trading_logger.log(
        logger_name=logger_name,
        level="WARNING",
        user_id=user_id,
        message=message,
        module_name=module_name,
        extra_data=extra_data,
        console_format=True
    )


def log_debug(
    user_id: int, 
    message: str, 
    module_name: str = "Unknown",
    extra_data: Optional[Dict[str, Any]] = None,
    logger_name: str = "system"
):
    """Логирование отладочной информации"""
    trading_logger.log(
        logger_name=logger_name,
        level="DEBUG",
        user_id=user_id,
        message=message,
        module_name=module_name,
        extra_data=extra_data,
        console_format=False
    )


def log_critical(
    user_id: int, 
    message: str, 
    module_name: str = "Unknown",
    extra_data: Optional[Dict[str, Any]] = None,
    logger_name: str = "errors"
):
    """Логирование критических ошибок"""
    trading_logger.log(
        logger_name=logger_name,
        level="CRITICAL",
        user_id=user_id,
        message=message,
        module_name=module_name,
        extra_data=extra_data,
        console_format=True
    )


# =============================================================================
# СПЕЦИАЛИЗИРОВАННЫЕ ФУНКЦИИ ЛОГИРОВАНИЯ
# =============================================================================

def log_trade(
    user_id: int,
    action: str,
    symbol: str,
    side: str,
    quantity: float,
    price: float,
    order_id: str = None,
    strategy: str = None,
    extra_data: Optional[Dict[str, Any]] = None
):
    """Логирование торговых операций"""
    
    trade_data = {
        "action": action,
        "symbol": symbol,
        "side": side,
        "quantity": quantity,
        "price": price,
        "order_id": order_id,
        "strategy": strategy
    }
    
    if extra_data:
        trade_data.update(extra_data)
        
    message = f"{action}: {side} {quantity} {symbol} @ {price}"
    if order_id:
        message += f" (Order: {order_id})"
        
    log_info(
        user_id=user_id,
        message=message,
        module_name="Trading",
        extra_data=trade_data,
        logger_name="trading"
    )


def log_strategy(
    user_id: int,
    strategy_name: str,
    action: str,
    message: str,
    extra_data: Optional[Dict[str, Any]] = None
):
    """Логирование событий стратегий"""
    
    strategy_data = {
        "strategy_name": strategy_name,
        "action": action
    }
    
    if extra_data:
        strategy_data.update(extra_data)
        
    formatted_message = f"[{strategy_name}] {action}: {message}"
    
    log_info(
        user_id=user_id,
        message=formatted_message,
        module_name="Strategy",
        extra_data=strategy_data,
        logger_name="strategies"
    )


def log_risk(
    user_id: int,
    risk_type: str,
    message: str,
    risk_level: str = "MEDIUM",
    extra_data: Optional[Dict[str, Any]] = None
):
    """Логирование событий риск-менеджмента"""
    
    risk_data = {
        "risk_type": risk_type,
        "risk_level": risk_level
    }
    
    if extra_data:
        risk_data.update(extra_data)
        
    formatted_message = f"[{risk_type}] {risk_level}: {message}"
    
    # Определение уровня логирования по уровню риска
    if risk_level in ["HIGH", "CRITICAL"]:
        log_level = "ERROR"
    elif risk_level == "MEDIUM":
        log_level = "WARNING"
    else:
        log_level = "INFO"
        
    trading_logger.log(
        logger_name="risk",
        level=log_level,
        user_id=user_id,
        message=formatted_message,
        module_name="RiskManager",
        extra_data=risk_data,
        console_format=True
    )


def log_api(
    user_id: int,
    method: str,
    endpoint: str,
    status_code: int = None,
    response_time: float = None,
    error: str = None,
    extra_data: Optional[Dict[str, Any]] = None
):
    """Логирование API операций"""
    
    api_data = {
        "method": method,
        "endpoint": endpoint,
        "status_code": status_code,
        "response_time": response_time,
        "error": error
    }
    
    if extra_data:
        api_data.update(extra_data)
        
    if error:
        message = f"{method} {endpoint} - ERROR: {error}"
        log_level = "ERROR"
    elif status_code and status_code >= 400:
        message = f"{method} {endpoint} - HTTP {status_code}"
        log_level = "WARNING"
    else:
        message = f"{method} {endpoint}"
        if response_time:
            message += f" ({response_time:.3f}s)"
        log_level = "DEBUG"
        
    trading_logger.log(
        logger_name="api",
        level=log_level,
        user_id=user_id,
        message=message,
        module_name="API",
        extra_data=api_data,
        console_format=False
    )


def log_websocket(
    user_id: int,
    event: str,
    channel: str = None,
    message: str = None,
    extra_data: Optional[Dict[str, Any]] = None
):
    """Логирование WebSocket событий"""
    
    ws_data = {
        "event": event,
        "channel": channel
    }
    
    if extra_data:
        ws_data.update(extra_data)
        
    formatted_message = f"[WebSocket] {event}"
    if channel:
        formatted_message += f" ({channel})"
    if message:
        formatted_message += f": {message}"
        
    log_debug(
        user_id=user_id,
        message=formatted_message,
        module_name="WebSocket",
        extra_data=ws_data,
        logger_name="websocket"
    )


def log_event(
    user_id: int,
    event_type: str,
    event_data: Dict[str, Any],
    message: str = None
):
    """Логирование системных событий"""
    
    formatted_message = message or f"Event: {event_type}"
    
    log_debug(
        user_id=user_id,
        message=formatted_message,
        module_name="EventBus",
        extra_data={
            "event_type": event_type,
            "event_data": event_data
        },
        logger_name="events"
    )


# =============================================================================
# УТИЛИТЫ ЛОГИРОВАНИЯ
# =============================================================================

def set_log_level(logger_name: str, level: str):
    """Установка уровня логирования для конкретного логгера"""
    logger = trading_logger.get_logger(logger_name)
    log_level = getattr(logging, level.upper(), logging.INFO)
    logger.setLevel(log_level)


def get_log_stats() -> Dict[str, Any]:
    """Получение статистики логирования"""
    stats = {}
    
    for name, logger in trading_logger.loggers.items():
        handler_info = []
        for handler in logger.handlers:
            if isinstance(handler, logging.handlers.RotatingFileHandler):
                handler_info.append({
                    "type": "file",
                    "filename": handler.baseFilename,
                    "level": handler.level,
                    "max_bytes": handler.maxBytes,
                    "backup_count": handler.backupCount
                })
            elif isinstance(handler, logging.StreamHandler):
                handler_info.append({
                    "type": "console",
                    "level": handler.level
                })
                
        stats[name] = {
            "level": logger.level,
            "handlers": handler_info
        }
        
    return stats


def cleanup_old_logs(days: int = 30):
    """Очистка старых лог файлов"""
    import time
    import os
    
    current_time = time.time()
    cutoff_time = current_time - (days * 24 * 60 * 60)
    
    log_dir = trading_logger.log_dir
    deleted_files = []
    
    for log_file in log_dir.glob("*.log*"):
        if log_file.stat().st_mtime < cutoff_time:
            try:
                log_file.unlink()
                deleted_files.append(str(log_file))
            except Exception as e:
                log_error(0, f"Не удалось удалить лог файл {log_file}: {e}", "LogCleaner")
                
    if deleted_files:
        log_info(0, f"Удалено {len(deleted_files)} старых лог файлов", "LogCleaner")
        
    return deleted_files

