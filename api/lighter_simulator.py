"""
Симулятор API для Lighter биржи
Полностью имитирует торговлю без реальных запросов к бирже
"""
import asyncio
import time
from typing import Dict, Any, Optional, List
from decimal import Decimal, getcontext
from datetime import datetime

from lighter_md import LighterMDClient, MARKET_ID_SOL
from core.logger import log_info, log_error, log_warning, log_debug

getcontext().prec = 28


class LighterSimulator:
    """
    Симулятор торговли для Lighter
    Имитирует все операции без реальных запросов к бирже
    """
    
    def __init__(self, user_id: int, symbol: str = "SOL", market_id: int = MARKET_ID_SOL):
        self.user_id = user_id
        self.symbol = symbol
        self.market_id = market_id
        
        # MD клиент только для получения цен (публичные данные)
        self.md_client: Optional[LighterMDClient] = None
        
        # Симулированное состояние позиции
        self.simulated_position: Optional[Dict[str, Any]] = None
        self.simulated_orders: List[Dict[str, Any]] = []
        
        log_info(user_id, f"LighterSimulator инициализирован для {symbol} (РЕЖИМ СИМУЛЯЦИИ)", 
                 module_name="lighter_simulator")
    
    async def connect(self):
        """Подключение только к публичным данным (цены)"""
        try:
            self.md_client = LighterMDClient()
            log_info(self.user_id, "✅ Подключение к публичным данным Lighter установлено", 
                     module_name="lighter_simulator")
        except Exception as e:
            log_error(self.user_id, f"Ошибка подключения к MD: {e}", module_name="lighter_simulator")
            raise
    
    async def close(self):
        """Закрытие соединений"""
        try:
            if self.md_client:
                await self.md_client.aclose()
        except Exception as e:
            log_error(self.user_id, f"Ошибка закрытия соединений: {e}", module_name="lighter_simulator")
    
    async def get_klines(self, symbol: str, interval: str = "5m", limit: int = 100) -> List[Dict[str, Any]]:
        """
        Получение исторических свечей (публичные данные - реальные)
        """
        try:
            if not self.md_client:
                await self.connect()
            
            candles = await self.md_client.fetch_ohlcv(
                market_id=self.market_id,
                resolution=interval,
                count_back=limit
            )
            
            # Конвертируем в формат, совместимый с SignalAnalyzer
            result = []
            for candle in candles:
                result.append({
                    "timestamp": candle["timestamp"],
                    "open": Decimal(str(candle["open"])),
                    "high": Decimal(str(candle["high"])),
                    "low": Decimal(str(candle["low"])),
                    "close": Decimal(str(candle["close"])),
                    "volume": Decimal(str(candle["volume"]))
                })
            
            log_debug(self.user_id, f"Получено {len(result)} свечей для {symbol}", module_name="lighter_simulator")
            return result
            
        except Exception as e:
            log_error(self.user_id, f"Ошибка получения свечей: {e}", module_name="lighter_simulator")
            return []
    
    async def get_current_price(self, symbol: str) -> Optional[Decimal]:
        """Получение текущей цены (публичные данные - реальные)"""
        try:
            if not self.md_client:
                await self.connect()
            
            ask, bid = await self.md_client.top_of_book(market_id=self.market_id)
            
            if ask and bid:
                mid = Decimal(str((ask + bid) / 2))
                return mid
            return None
            
        except Exception as e:
            log_error(self.user_id, f"Ошибка получения цены: {e}", module_name="lighter_simulator")
            return None
    
    async def place_market_order(self, side: str, notional_usd: float) -> Optional[Dict[str, Any]]:
        """
        СИМУЛЯЦИЯ размещения рыночного ордера
        Не отправляет реальный ордер на биржу!
        """
        try:
            # Получаем текущую цену
            current_price = await self.get_current_price(self.symbol)
            if not current_price:
                log_error(self.user_id, "Не удалось получить цену для симуляции", module_name="lighter_simulator")
                return None
            
            # Симулируем исполнение ордера
            is_buy = (side.upper() == "BUY")
            quantity = Decimal(str(notional_usd)) / current_price
            
            # Создаем симулированный ордер
            order_id = f"SIM_{int(time.time() * 1000)}"
            
            # Симулируем комиссию (0.055% как у Bybit taker)
            commission_rate = Decimal('0.00055')
            commission = Decimal(str(notional_usd)) * commission_rate
            
            order_result = {
                "order_id": order_id,
                "side": side,
                "price": float(current_price),
                "quantity": float(quantity),
                "notional_usd": notional_usd,
                "commission": float(commission),
                "status": "FILLED",
                "simulated": True
            }
            
            # Обновляем симулированную позицию
            if not self.simulated_position:
                self.simulated_position = {
                    "symbol": self.symbol,
                    "side": side,
                    "size": quantity,
                    "avg_price": current_price,
                    "entry_time": datetime.now(),
                    "unrealized_pnl": Decimal('0')
                }
            else:
                # Усреднение или закрытие
                if self.simulated_position["side"] == side:
                    # Усреднение - пересчитываем среднюю цену
                    old_value = self.simulated_position["avg_price"] * self.simulated_position["size"]
                    new_value = current_price * quantity
                    total_size = self.simulated_position["size"] + quantity
                    self.simulated_position["avg_price"] = (old_value + new_value) / total_size
                    self.simulated_position["size"] = total_size
                else:
                    # Закрытие позиции
                    if quantity >= self.simulated_position["size"]:
                        self.simulated_position = None
                    else:
                        self.simulated_position["size"] -= quantity
            
            self.simulated_orders.append(order_result)
            
            log_info(self.user_id, 
                    f"🎮 СИМУЛЯЦИЯ: Ордер {side} {notional_usd} USDT @ ${current_price:.4f} (ID: {order_id})",
                    module_name="lighter_simulator")
            
            return order_result
            
        except Exception as e:
            log_error(self.user_id, f"Ошибка симуляции ордера: {e}", module_name="lighter_simulator")
            return None
    
    async def get_positions(self, symbol: str = "SOL") -> List[Dict[str, Any]]:
        """
        Получение симулированных позиций
        """
        result = []
        
        if self.simulated_position and self.simulated_position["symbol"] == symbol:
            pos = self.simulated_position
            current_price = await self.get_current_price(symbol)
            
            if current_price:
                # Рассчитываем unrealized PnL
                if pos["side"] == "Buy":
                    pnl = (current_price - pos["avg_price"]) * pos["size"]
                else:
                    pnl = (pos["avg_price"] - current_price) * pos["size"]
                
                result.append({
                    "symbol": pos["symbol"],
                    "size": pos["size"],
                    "side": pos["side"],
                    "avgPrice": pos["avg_price"],
                    "unrealizedPnl": pnl
                })
        
        return result
    
    async def calculate_quantity_from_usdt(self, symbol: str, usdt_amount: float, 
                                          leverage: float = 1.0, price: Optional[Decimal] = None) -> Decimal:
        """Расчет количества базового актива"""
        try:
            if price is None:
                price = await self.get_current_price(symbol)
                if not price:
                    return Decimal('0')
            
            quantity = Decimal(str(usdt_amount)) / price
            return quantity
            
        except Exception as e:
            log_error(self.user_id, f"Ошибка расчета количества: {e}", module_name="lighter_simulator")
            return Decimal('0')
    
    async def stream_price_updates(self, symbol: str, poll_interval: float = 1.0):
        """
        Генератор обновлений цены (публичные данные - реальные)
        """
        try:
            if not self.md_client:
                await self.connect()
            
            async for price_data in self.md_client.stream_midprice(
                market_id=self.market_id,
                poll_interval=poll_interval
            ):
                if price_data.get("mid") is not None:
                    yield {
                        "ts": price_data["ts"],
                        "price": Decimal(str(price_data["mid"])),
                        "ask": price_data.get("ask"),
                        "bid": price_data.get("bid")
                    }
                    
        except Exception as e:
            log_error(self.user_id, f"Ошибка стрима цен: {e}", module_name="lighter_simulator")

