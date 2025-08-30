import asyncio
import json
import math
from decimal import Decimal, ROUND_HALF_UP, getcontext
from typing import Dict, Any, Optional, List, Union, Tuple
import pandas as pd
import numpy as np
from datetime import datetime, timedelta, timezone
import hashlib
import hmac
import base64
from urllib.parse import urlencode
import re
from core.enums import MarketCondition, IndicatorType as TechnicalIndicator

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
        # Ловим только ожидаемые ошибки преобразования
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

def calculate_percentage(current: Union[int, float, Decimal], 
                        previous: Union[int, float, Decimal]) -> Decimal:
    """Расчет процентного изменения"""
    current_decimal = to_decimal(current)
    previous_decimal = to_decimal(previous)
    
    if previous_decimal == 0:
        return Decimal('0')
    
    return ((current_decimal - previous_decimal) / previous_decimal) * Decimal('100')

def safe_divide(numerator: Union[int, float, Decimal], 
                denominator: Union[int, float, Decimal]) -> Decimal:
    """Безопасное деление с проверкой на ноль"""
    numerator_decimal = to_decimal(numerator)
    denominator_decimal = to_decimal(denominator)
    
    if denominator_decimal == 0:
        return Decimal('0')
    
    return numerator_decimal / denominator_decimal

def round_to_precision(value: Union[int, float, Decimal], precision: int) -> Decimal:
    """Округление до заданной точности"""
    decimal_value = to_decimal(value)
    quantizer = Decimal('0.1') ** precision
    return decimal_value.quantize(quantizer, rounding=ROUND_HALF_UP)

def round_to_tick_size(value: Union[int, float, Decimal], tick_size: Union[int, float, Decimal]) -> Decimal:
    """Округление до размера тика"""
    decimal_value = to_decimal(value)
    tick_decimal = to_decimal(tick_size)
    
    if tick_decimal == 0:
        return decimal_value
    
    return (decimal_value / tick_decimal).quantize(Decimal('1'), rounding=ROUND_HALF_UP) * tick_decimal

def calculate_position_size(balance: Union[int, float, Decimal],
                          risk_percent: Union[int, float, Decimal],
                          entry_price: Union[int, float, Decimal],
                          stop_price: Union[int, float, Decimal]) -> Decimal:
    """Расчет размера позиции на основе риска"""
    balance_decimal = to_decimal(balance)
    risk_decimal = to_decimal(risk_percent) / Decimal('100')
    entry_decimal = to_decimal(entry_price)
    stop_decimal = to_decimal(stop_price)
    
    if entry_decimal == 0 or stop_decimal == 0:
        return Decimal('0')
    
    risk_amount = balance_decimal * risk_decimal
    price_diff = abs(entry_decimal - stop_decimal)
    
    if price_diff == 0:
        return Decimal('0')
    
    position_size = risk_amount / price_diff
    return position_size

def calculate_pnl(entry_price: Union[int, float, Decimal],
                  current_price: Union[int, float, Decimal],
                  quantity: Union[int, float, Decimal],
                  side: str) -> Decimal:
    """Расчет PnL позиции"""
    entry_decimal = to_decimal(entry_price)
    current_decimal = to_decimal(current_price)
    quantity_decimal = to_decimal(quantity)
    
    if side.upper() == "BUY":
        pnl = (current_decimal - entry_decimal) * quantity_decimal
    else:  # SELL
        pnl = (entry_decimal - current_decimal) * quantity_decimal
    
    return pnl

def calculate_atr(high: List[Union[int, float, Decimal]], 
                  low: List[Union[int, float, Decimal]], 
                  close: List[Union[int, float, Decimal]], 
                  period: int = 14) -> Decimal:
    """Расчет Average True Range"""
    if len(high) < period or len(low) < period or len(close) < period:
        return Decimal('0')
    
    tr_list = []
    for i in range(1, len(high)):
        high_decimal = to_decimal(high[i])
        low_decimal = to_decimal(low[i])
        close_prev_decimal = to_decimal(close[i-1])
        
        tr1 = high_decimal - low_decimal
        tr2 = abs(high_decimal - close_prev_decimal)
        tr3 = abs(low_decimal - close_prev_decimal)
        tr_list.append(max(tr1, tr2, tr3))
    
    if len(tr_list) < period:
        return Decimal('0')
    
    return sum(tr_list[-period:]) / Decimal(str(period))

def calculate_rsi(prices: List[Union[int, float, Decimal]], period: int = 14) -> Decimal:
    """Расчет RSI"""
    if len(prices) < period + 1:
        return Decimal('50')
    
    gains = []
    losses = []
    
    for i in range(1, len(prices)):
        current_price = to_decimal(prices[i])
        prev_price = to_decimal(prices[i-1])
        change = current_price - prev_price
        
        if change > 0:
            gains.append(change)
            losses.append(Decimal('0'))
        else:
            gains.append(Decimal('0'))
            losses.append(abs(change))
    
    if len(gains) < period:
        return Decimal('50')
    
    avg_gain = sum(gains[-period:]) / Decimal(str(period))
    avg_loss = sum(losses[-period:]) / Decimal(str(period))
    
    if avg_loss == 0:
        return Decimal('100')
    
    rs = avg_gain / avg_loss
    rsi = Decimal('100') - (Decimal('100') / (Decimal('1') + rs))
    
    return rsi

def calculate_ema(prices: List[Union[int, float, Decimal]], period: int) -> Decimal:
    """Расчет экспоненциальной скользящей средней"""
    if len(prices) < period:
        return Decimal('0')
    
    decimal_prices = [to_decimal(p) for p in prices]
    multiplier = Decimal('2') / (Decimal(str(period)) + Decimal('1'))
    
    # Начинаем с простой скользящей средней
    ema = sum(decimal_prices[:period]) / Decimal(str(period))
    
    # Применяем формулу EMA для остальных значений
    for price in decimal_prices[period:]:
        ema = (price * multiplier) + (ema * (Decimal('1') - multiplier))
    
    return ema

def calculate_bollinger_bands(prices: List[Union[int, float, Decimal]], 
                             period: int = 20, 
                             std_dev: int = 2) -> Tuple[Decimal, Decimal, Decimal]:
    """Расчет полос Боллинджера"""
    if len(prices) < period:
        return Decimal('0'), Decimal('0'), Decimal('0')
    
    decimal_prices = [to_decimal(p) for p in prices[-period:]]
    
    # Средняя линия (SMA)
    middle = sum(decimal_prices) / Decimal(str(period))
    
    # Стандартное отклонение
    variance = sum((price - middle) ** 2 for price in decimal_prices) / Decimal(str(period))
    std = variance.sqrt()
    
    # Верхняя и нижняя полосы
    upper = middle + (std * Decimal(str(std_dev)))
    lower = middle - (std * Decimal(str(std_dev)))
    
    return upper, middle, lower

def detect_market_condition(prices: List[Union[int, float, Decimal]], volumes: List[Union[int, float, Decimal]] = None, period: int = 20) -> MarketCondition:
    """Определение рыночных условий с учетом объема"""
    if len(prices) < period:
        return MarketCondition.UNKNOWN

    decimal_prices = [to_decimal(p) for p in prices[-period:]]

    # Расчет трендовости через линейную регрессию
    x = list(range(len(decimal_prices)))
    n = len(decimal_prices)

    sum_x = sum(x)
    sum_y = sum(decimal_prices)
    sum_xy = sum(Decimal(str(x[i])) * decimal_prices[i] for i in range(n))
    sum_x2 = sum(Decimal(str(x[i])) ** 2 for i in range(n))

    denominator_slope = (Decimal(str(n)) * sum_x2 - Decimal(str(sum_x)) ** 2)
    if denominator_slope == 0:
        return MarketCondition.FLAT

    slope = (Decimal(str(n)) * sum_xy - Decimal(str(sum_x)) * sum_y) / denominator_slope

    mean_x = Decimal(str(sum_x)) / Decimal(str(n))
    mean_y = sum_y / Decimal(str(n))

    numerator_corr = sum((Decimal(str(x[i])) - mean_x) * (decimal_prices[i] - mean_y) for i in range(n))
    denominator_x_corr = sum((Decimal(str(x[i])) - mean_x) ** 2 for i in range(n))
    denominator_y_corr = sum((decimal_prices[i] - mean_y) ** 2 for i in range(n))

    if denominator_x_corr == 0 or denominator_y_corr == 0:
        return MarketCondition.FLAT

    correlation = numerator_corr / (denominator_x_corr * denominator_y_corr).sqrt()

    # Анализ объема для подтверждения силы тренда
    volume_confirms_trend = False
    if volumes and len(volumes) >= period:
        decimal_volumes = [to_decimal(v) for v in volumes[-period:]]
        avg_volume = sum(decimal_volumes) / len(decimal_volumes)
        # Считаем тренд подтвержденным, если последний объем выше среднего
        if decimal_volumes[-1] > avg_volume * Decimal('1.2'): # Порог в 1.2 для надежности
            volume_confirms_trend = True

    # Определение условий с учетом объема
    if abs(correlation) > Decimal('0.7'):
        if volume_confirms_trend:
            return MarketCondition.STRONG_TREND
        else:
            return MarketCondition.TREND
    elif abs(correlation) > Decimal('0.4'):
        return MarketCondition.WEAK_TREND
    else:
        return MarketCondition.FLAT if abs(slope) < Decimal('0.0005') else MarketCondition.STRONG_FLAT


def calculate_volatility(prices: List[Union[int, float, Decimal]], period: int = 20) -> Decimal:
    """Расчет волатильности"""
    if len(prices) < period:
        return Decimal('0')
    
    decimal_prices = [to_decimal(p) for p in prices[-period:]]
    returns = []
    
    for i in range(1, len(decimal_prices)):
        ret = (decimal_prices[i] - decimal_prices[i-1]) / decimal_prices[i-1]
        returns.append(ret)
    
    if not returns:
        return Decimal('0')
    
    mean_return = sum(returns) / Decimal(str(len(returns)))
    variance = sum((ret - mean_return) ** 2 for ret in returns) / Decimal(str(len(returns)))
    
    return variance.sqrt()

def generate_signature(secret: str, params: Dict[str, Any]) -> str:
    """Генерация подписи для API запросов"""
    query_string = urlencode(sorted(params.items()))
    return hmac.new(
        secret.encode('utf-8'),
        query_string.encode('utf-8'),
        hashlib.sha256
    ).hexdigest()

def validate_symbol(symbol: str) -> bool:
    """Валидация символа торговой пары"""
    if not symbol or len(symbol) < 3:
        return False
    
    # Проверяем формат символа (например, BTCUSDT, ETHUSDT)
    pattern = r'^[A-Z0-9]+USDT?$|^[A-Z0-9]+BTC$|^[A-Z0-9]+ETH$'
    return bool(re.match(pattern, symbol.upper()))

def validate_order_params(symbol: str, side: str, order_type: str, 
                         quantity: Union[int, float, Decimal],
                         price: Union[int, float, Decimal] = None) -> bool:
    """Валидация параметров ордера"""
    if not validate_symbol(symbol):
        return False
    
    if side.upper() not in ['BUY', 'SELL']:
        return False
    
    if order_type.upper() not in ['MARKET', 'LIMIT', 'STOP', 'STOP_LIMIT']:
        return False
    
    quantity_decimal = to_decimal(quantity)
    if quantity_decimal <= 0:
        return False
    
    if order_type.upper() in ['LIMIT', 'STOP_LIMIT'] and price is not None:
        price_decimal = to_decimal(price)
        if price_decimal <= 0:
            return False
    
    return True

def format_datetime(dt: datetime) -> str:
    """Форматирование даты и времени"""
    return dt.strftime("%Y-%m-%d %H:%M:%S")

def parse_timeframe(timeframe: str) -> int:
    """Парсинг таймфрейма в секунды"""
    timeframe_map = {
        '1m': 60,
        '3m': 180,
        '5m': 300,
        '15m': 900,
        '30m': 1800,
        '1h': 3600,
        '2h': 7200,
        '4h': 14400,
        '6h': 21600,
        '8h': 28800,
        '12h': 43200,
        '1d': 86400,
        '3d': 259200,
        '1w': 604800,
        '1M': 2592000
    }
    
    return timeframe_map.get(timeframe, 3600)  # По умолчанию 1 час

def get_timeframe_minutes(timeframe: str) -> int:
    """Получение количества минут в таймфрейме"""
    return parse_timeframe(timeframe) // 60

def calculate_drawdown(equity_curve: List[Union[int, float, Decimal]]) -> Tuple[Decimal, Decimal]:
    """Расчет максимальной и текущей просадки"""
    if not equity_curve:
        return Decimal('0'), Decimal('0')
    
    decimal_curve = [to_decimal(eq) for eq in equity_curve]
    peak = decimal_curve[0]
    max_drawdown = Decimal('0')
    current_drawdown = Decimal('0')
    
    for equity in decimal_curve:
        if equity > peak:
            peak = equity
        
        drawdown = (peak - equity) / peak * Decimal('100') if peak > 0 else Decimal('0')
        current_drawdown = drawdown
        
        if drawdown > max_drawdown:
            max_drawdown = drawdown
    
    return max_drawdown, current_drawdown

def calculate_sharpe_ratio(returns: List[Union[int, float, Decimal]], 
                          risk_free_rate: Union[int, float, Decimal] = 0) -> Decimal:
    """Расчет коэффициента Шарпа"""
    if not returns:
        return Decimal('0')
    
    decimal_returns = [to_decimal(ret) for ret in returns]
    risk_free_decimal = to_decimal(risk_free_rate)
    
    excess_returns = [ret - risk_free_decimal for ret in decimal_returns]
    
    if not excess_returns:
        return Decimal('0')
    
    mean_excess_return = sum(excess_returns) / Decimal(str(len(excess_returns)))
    
    if len(excess_returns) < 2:
        return Decimal('0')
    
    variance = sum((ret - mean_excess_return) ** 2 for ret in excess_returns) / Decimal(str(len(excess_returns) - 1))
    std_dev = variance.sqrt()
    
    if std_dev == 0:
        return Decimal('0')
    
    return mean_excess_return / std_dev

def sanitize_user_input(text: str, max_length: int = 100) -> str:
    """Очистка пользовательского ввода"""
    if not text:
        return ""
    
    # Удаляем опасные символы
    sanitized = re.sub(r'[<>"\']', '', text)
    
    # Ограничиваем длину
    return sanitized[:max_length].strip()

def is_market_hours() -> bool:
    """Проверка торговых часов (крипто торгуется 24/7)"""
    return True

def get_next_candle_time(timeframe: str, current_time: datetime = None) -> datetime:
    """Получение времени следующей свечи"""
    if current_time is None:
        current_time = datetime.now(timezone.utc)
    
    timeframe_seconds = parse_timeframe(timeframe)
    
    # Округляем до начала текущего периода
    timestamp = int(current_time.timestamp())
    period_start = (timestamp // timeframe_seconds) * timeframe_seconds
    
    # Добавляем один период для получения следующей свечи
    next_period = period_start + timeframe_seconds
    
    return datetime.fromtimestamp(next_period)

def format_trade_summary(trades: List[Dict[str, Any]]) -> str:
    """Форматирование сводки по сделкам"""
    if not trades:
        return "Нет сделок"
    
    total_trades = len(trades)
    winning_trades = sum(1 for trade in trades if to_decimal(trade.get('pnl', 0)) > 0)
    losing_trades = total_trades - winning_trades
    
    total_pnl = sum(to_decimal(trade.get('pnl', 0)) for trade in trades)
    win_rate = (Decimal(str(winning_trades)) / Decimal(str(total_trades))) * Decimal('100') if total_trades > 0 else Decimal('0')
    
    return f"""📊 Сводка по сделкам:
Всего сделок: {total_trades}
Прибыльных: {winning_trades}
Убыточных: {losing_trades}
Win Rate: {format_percentage(win_rate)}
Общий PnL: {format_currency(total_pnl)}"""

# Функции для работы с Bybit API (совместимость с существующим кодом)
def calculate_quantity_from_usdt(usdt_amount: Union[int, float, Decimal], 
                                price: Union[int, float, Decimal], 
                                leverage: int = 1) -> Decimal:
    """Расчет количества из суммы в USDT"""
    usdt_decimal = to_decimal(usdt_amount)
    price_decimal = to_decimal(price)
    leverage_decimal = to_decimal(leverage)
    
    if price_decimal == 0:
        return Decimal('0')
    
    return (usdt_decimal * leverage_decimal) / price_decimal

def calculate_take_profit_price(entry_price: Union[int, float, Decimal], 
                               profit_percent: Union[int, float, Decimal], 
                               side: str) -> Decimal:
    """Расчет цены тейк-профита"""
    entry_decimal = to_decimal(entry_price)
    profit_decimal = to_decimal(profit_percent)
    
    multiplier = Decimal('1') + (profit_decimal / Decimal('100'))
    
    if side.lower() == "buy":
        return entry_decimal * multiplier
    else:  # sell
        return entry_decimal / multiplier

def calculate_stop_loss_price(entry_price: Union[int, float, Decimal], 
                             loss_percent: Union[int, float, Decimal], 
                             side: str) -> Decimal:
    """Расчет цены стоп-лосса"""
    entry_decimal = to_decimal(entry_price)
    loss_decimal = to_decimal(loss_percent)
    
    multiplier = Decimal('1') - (loss_decimal / Decimal('100'))
    
    if side.lower() == "buy":
        return entry_decimal * multiplier
    else:  # sell
        return entry_decimal / multiplier

