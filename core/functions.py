import json
from decimal import Decimal, getcontext
from typing import Union

# Устанавливаем точность для Decimal
getcontext().prec = 28

class DecimalEncoder(json.JSONEncoder):
    """JSON encoder для Decimal"""
    def default(self, obj):
        if isinstance(obj, Decimal):
            return float(obj)
        return super().default(obj)

def to_decimal(value: Union[int, float, str, Decimal]) -> Decimal:
    """Безопасное преобразование в Decimal"""
    if isinstance(value, Decimal):
        return value
    if isinstance(value, (int, float)):
        return Decimal(str(value))
    if isinstance(value, str):
        try:
            return Decimal(value)
        except Exception:
            return Decimal('0')
    return Decimal('0')

def format_number(value: Union[int, float, Decimal], precision: int = 8) -> str:
    """Форматирование числа с заданной точностью"""
    decimal_value = to_decimal(value)

    # Убираем лишние нули
    formatted = f"{decimal_value:.{precision}f}".rstrip('0').rstrip('.')
    return formatted if formatted else "0"

def format_currency(value: Union[int, float, Decimal], currency: str = "USDT", precision: int = 2) -> str:
    """Форматирование валютного значения"""
    decimal_value = to_decimal(value)
    formatted = f"{decimal_value:.{precision}f}"
    return f"{formatted} {currency}"

def format_percentage(value: Union[int, float, Decimal], precision: int = 2) -> str:
    """Форматирование процентного значения"""
    decimal_value = to_decimal(value)
    formatted = f"{decimal_value:.{precision}f}"
    return f"{formatted}%"


# === Функции для работы со временем ===

from datetime import datetime, timezone, timedelta
from typing import Optional, Dict, Any

def get_moscow_tz() -> timezone:
    """
    Возвращает объект timezone для московского времени (UTC+3).

    Returns:
        timezone: Московская временная зона (UTC+3)
    """
    return timezone(timedelta(hours=3))


def get_moscow_time() -> datetime:
    """
    Возвращает текущее время в московской временной зоне.

    Returns:
        datetime: Текущее время по МСК
    """
    return datetime.now(get_moscow_tz())


def convert_to_moscow_time(dt: Optional[datetime]) -> Optional[datetime]:
    """
    Конвертирует datetime в московское время.

    Args:
        dt: Время для конвертации (может быть naive или aware)

    Returns:
        Optional[datetime]: Время в МСК или None если входное время None
    """
    if dt is None:
        return None

    moscow_tz = get_moscow_tz()

    if dt.tzinfo is None:
        # Если время naive, считаем его UTC и конвертируем в московское
        return dt.replace(tzinfo=timezone.utc).astimezone(moscow_tz)
    else:
        # Если время уже aware, просто конвертируем
        return dt.astimezone(moscow_tz)


# === Функции для работы с БД ===

def parse_order_metadata(order_dict: Dict[str, Any]) -> Dict[str, Any]:
    """
    Парсит метаданные ордера из JSON строки в dict.

    Args:
        order_dict: Словарь с данными ордера из БД

    Returns:
        Dict: Словарь с распарсенными метаданными
    """
    if order_dict and order_dict.get('metadata'):
        if isinstance(order_dict['metadata'], str):
            order_dict['metadata'] = json.loads(order_dict['metadata'])
    return order_dict


def process_order_result(result: Optional[Any]) -> Optional[Dict[str, Any]]:
    """
    Обрабатывает результат запроса ордера из БД:
    - Конвертирует в dict
    - Парсит метаданные

    Args:
        result: Результат запроса из asyncpg (Row или None)

    Returns:
        Optional[Dict]: Обработанный словарь с данными ордера или None
    """
    if not result:
        return None

    order_dict = dict(result)
    return parse_order_metadata(order_dict)


def process_order_list(results: list) -> list:
    """
    Обрабатывает список результатов запросов ордеров из БД.

    Args:
        results: Список результатов запросов из asyncpg

    Returns:
        list: Список обработанных словарей с данными ордеров
    """
    if not results:
        return []

    return [process_order_result(row) for row in results]