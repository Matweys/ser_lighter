"""
API адаптер для Lighter биржи
Обертка над lighter_client.py для совместимости с архитектурой стратегий
"""
import asyncio
from typing import Dict, Any, Optional, List
from decimal import Decimal, getcontext
from datetime import datetime

from lighter_client import LighterClient, MARKET_ID_SOL
from lighter_md import LighterMDClient
from core.logger import log_info, log_error, log_warning, log_debug

getcontext().prec = 28


class LighterAPI:
    """
    API клиент для Lighter биржи
    Адаптирован для работы со стратегиями
    """
    
    def __init__(self, user_id: int, api_priv: str, account_index: int = 24844, 
                 api_key_index: int = 0, market_id: int = MARKET_ID_SOL):
        self.user_id = user_id
        self.api_priv = api_priv
        self.account_index = account_index
        self.api_key_index = api_key_index
        self.market_id = market_id
        
        # Клиенты
        self.client: Optional[LighterClient] = None
        self.md_client: Optional[LighterMDClient] = None
        
        log_info(user_id, f"LighterAPI инициализирован для account_index={account_index}", 
                 module_name="lighter_api")
    
    async def __aenter__(self):
        """Async context manager entry"""
        await self.connect()
        return self
    
    async def __aexit__(self, exc_type, exc_val, exc_tb):
        """Async context manager exit"""
        await self.close()
    
    async def connect(self):
        """Подключение к Lighter"""
        try:
            self.client = LighterClient(
                account_index=self.account_index,
                api_key_index=self.api_key_index,
                api_priv=self.api_priv
            )
            await self.client.__aenter__()
            
            self.md_client = LighterMDClient()
            
            log_info(self.user_id, "✅ Подключение к Lighter установлено", module_name="lighter_api")
        except Exception as e:
            log_error(self.user_id, f"Ошибка подключения к Lighter: {e}", module_name="lighter_api")
            raise
    
    async def close(self):
        """Закрытие соединений"""
        try:
            if self.client:
                await self.client.__aexit__(None, None, None)
            if self.md_client:
                await self.md_client.aclose()
        except Exception as e:
            log_error(self.user_id, f"Ошибка закрытия соединений: {e}", module_name="lighter_api")
    
    async def get_klines(self, symbol: str, interval: str = "5m", limit: int = 100) -> List[Dict[str, Any]]:
        """
        Получение исторических свечей
        
        Args:
            symbol: Символ (например, "SOL")
            interval: Таймфрейм ("5m", "1m", etc)
            limit: Количество свечей
            
        Returns:
            Список свечей в формате [{"timestamp", "open", "high", "low", "close", "volume"}, ...]
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
            
            log_debug(self.user_id, f"Получено {len(result)} свечей для {symbol}", module_name="lighter_api")
            return result
            
        except Exception as e:
            log_error(self.user_id, f"Ошибка получения свечей: {e}", module_name="lighter_api")
            return []
    
    async def get_current_price(self, symbol: str) -> Optional[Decimal]:
        """Получение текущей цены (mid price)"""
        try:
            if not self.md_client:
                await self.connect()
            
            ask, bid = await self.md_client.top_of_book(market_id=self.market_id)
            
            if ask and bid:
                mid = Decimal(str((ask + bid) / 2))
                return mid
            return None
            
        except Exception as e:
            log_error(self.user_id, f"Ошибка получения цены: {e}", module_name="lighter_api")
            return None
    
    async def place_market_order(self, side: str, notional_usd: float) -> Optional[Dict[str, Any]]:
        """
        Размещение рыночного ордера
        
        Args:
            side: "Buy" или "Sell"
            notional_usd: Сумма в USDT
            
        Returns:
            Информация об ордере или None
        """
        try:
            if not self.client:
                await self.connect()
            
            is_buy = (side.upper() == "BUY")
            
            result = await self.client.open_order_market_ioc(
                market_id=self.market_id,
                notional_usd=notional_usd,
                is_buy=is_buy
            )
            
            log_info(self.user_id, 
                    f"✅ Рыночный ордер размещен: {side} {notional_usd} USDT, tx_hash={result.get('tx_hash')}",
                    module_name="lighter_api")
            
            return result
            
        except Exception as e:
            log_error(self.user_id, f"Ошибка размещения ордера: {e}", module_name="lighter_api")
            return None
    
    async def place_take_profit(self, is_long: bool, target_price_usd: float, 
                               position_size_base: float) -> Optional[Dict[str, Any]]:
        """
        Размещение Take Profit ордера
        
        Args:
            is_long: True для LONG позиции
            target_price_usd: Целевая цена в USDT
            position_size_base: Размер позиции в базовом активе
            
        Returns:
            Информация об ордере или None
        """
        try:
            if not self.client:
                await self.connect()
            
            # Конвертируем размер в integer (с учетом decimals)
            size_decimals = self.client.size_decimals
            base_amount_int = max(1, int(round(position_size_base * (10 ** size_decimals))))
            
            # Конвертируем цену в integer
            price_decimals = self.client.price_decimals
            price_int = max(1, int(round(target_price_usd * (10 ** price_decimals))))
            
            # Для LONG позиции TP = SELL, для SHORT = BUY
            is_sell_tp = is_long
            
            result = await self.client.place_reduce_only_limit(
                market_id=self.market_id,
                is_sell=is_sell_tp,
                base_amount_int=base_amount_int,
                price_int=price_int
            )
            
            log_info(self.user_id,
                    f"✅ Take Profit размещен: {'SELL' if is_sell_tp else 'BUY'} {position_size_base} @ ${target_price_usd:.4f}",
                    module_name="lighter_api")
            
            return result
            
        except Exception as e:
            log_error(self.user_id, f"Ошибка размещения TP: {e}", module_name="lighter_api")
            return None
    
    async def get_positions(self, symbol: str = "SOL") -> List[Dict[str, Any]]:
        """
        Получение текущих позиций
        
        Returns:
            Список позиций
        """
        try:
            if not self.client:
                await self.connect()
            
            pnl_data = await self.client.get_positions_pnl()
            positions = pnl_data.get("positions", [])
            
            # Фильтруем по символу
            result = []
            for pos in positions:
                if pos.get("symbol", "").upper() == symbol.upper():
                    size = float(pos.get("size", 0))
                    if abs(size) > 1e-12:  # Только активные позиции
                        result.append({
                            "symbol": pos.get("symbol"),
                            "size": Decimal(str(abs(size))),
                            "side": "Buy" if size > 0 else "Sell",
                            "avgPrice": Decimal(str(pos.get("avg_entry_price", 0))),
                            "unrealizedPnl": Decimal(str(pos.get("unrealized_pnl", 0)))
                        })
            
            return result
            
        except Exception as e:
            log_error(self.user_id, f"Ошибка получения позиций: {e}", module_name="lighter_api")
            return []
    
    async def calculate_quantity_from_usdt(self, symbol: str, usdt_amount: float, 
                                          leverage: float = 1.0, price: Optional[Decimal] = None) -> Decimal:
        """
        Расчет количества базового актива от суммы в USDT
        
        Args:
            symbol: Символ
            usdt_amount: Сумма в USDT
            leverage: Плечо
            price: Текущая цена (если None, получаем автоматически)
            
        Returns:
            Количество базового актива
        """
        try:
            if price is None:
                price = await self.get_current_price(symbol)
                if not price:
                    return Decimal('0')
            
            # Для Lighter: количество = сумма / цена (без учета плеча в расчете, т.к. плечо на бирже)
            quantity = Decimal(str(usdt_amount)) / price
            
            return quantity
            
        except Exception as e:
            log_error(self.user_id, f"Ошибка расчета количества: {e}", module_name="lighter_api")
            return Decimal('0')
    
    async def stream_price_updates(self, symbol: str, poll_interval: float = 1.0):
        """
        Генератор обновлений цены
        
        Yields:
            {"ts": timestamp_ms, "price": Decimal, "ask": float, "bid": float}
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
            log_error(self.user_id, f"Ошибка стрима цен: {e}", module_name="lighter_api")

