import hashlib
import hmac
import time
import json
import asyncio
import aiohttp
from typing import Dict, Any, Optional, List
from decimal import Decimal, getcontext
from core.logger import log_info, log_error, log_warning
from core.functions import to_decimal
from urllib.parse import urlencode
from core.functions import format_number
# Настройка точности для Decimal
getcontext().prec = 28


class BybitAPI:
    """
    Профессиональный API клиент для Bybit
    
    Особенности:
    - Точные вычисления через Decimal
    - Автоматическое управление rate limits
    - Retry механизм для надежности
    - Детальное логирование операций
    - Поддержка всех типов ордеров
    """
    
    def __init__(self, api_key: str, api_secret: str, user_id: int, demo: bool = False, event_bus=None):
        self.api_key = api_key
        self.api_secret = api_secret
        self.user_id = user_id
        self.demo = demo
        self.event_bus = event_bus

        if self.api_key:
            log_info(self.user_id, f"BybitAPI инициализирован с ключом: {self.api_key[:4]}...{self.api_key[-4:]}",
                     module_name="bybit_api")

            # URL endpoints
        if demo:
            self.base_url = "https://api-demo.bybit.com"
        else:
            self.base_url = "https://api.bybit.com"

        log_info(self.user_id, f"BybitAPI использует базовый URL: {self.base_url}", module_name="bybit_api")
            
        # Rate limiting
        self.rate_limit_delay = 0.1  # 100ms между запросами
        self.last_request_time = 0
        
        # Retry settings
        self.max_retries = 3
        self.retry_delay = 1.0
        
        # Session для переиспользования соединений
        self.session: Optional[aiohttp.ClientSession] = None
        
        # Кэш для инструментов
        self.instruments_cache: Dict[str, Dict] = {}
        self.cache_expiry = 300  # 5 минут
        self.cache_timestamp = 0
        

    
    async def __aenter__(self):
        """Async context manager entry"""
        await self._ensure_session()
        return self
        
    async def __aexit__(self, exc_type, exc_val, exc_tb):
        """Async context manager exit"""
        await self.close()
        
    async def _ensure_session(self):
        """Обеспечение активной HTTP сессии"""
        if not self.session or self.session.closed:
            # Увеличиваем общий таймаут до 60 секунд
            timeout = aiohttp.ClientTimeout(total=60, connect=20)
            # Убираем глобальный Content-Type, будем добавлять его только для POST запросов
            self.session = aiohttp.ClientSession(
                timeout=timeout,
                headers={
                    "User-Agent": "Manus-Trading-Bot/1.0"
                }
            )
            
    async def close(self):
        """Закрытие HTTP сессии"""
        if self.session and not self.session.closed:
            await self.session.close()
            
    def _generate_signature(self, params: str, timestamp: str) -> str:
        """Генерация подписи для запроса"""
        try:
            # Для V5 API
            param_str = timestamp + self.api_key + "5000" + params
            return hmac.new(
                bytes(self.api_secret, "utf-8"),
                param_str.encode("utf-8"),
                hashlib.sha256
            ).hexdigest()
        except Exception as e:
            log_error(self.user_id, f"Ошибка генерации подписи: {e}", module_name="bybit_api")
            return ""
    
    async def _rate_limit(self):
        """Управление rate limits"""
        current_time = time.time()
        time_since_last = current_time - self.last_request_time
        
        if time_since_last < self.rate_limit_delay:
            await asyncio.sleep(self.rate_limit_delay - time_since_last)
            
        self.last_request_time = time.time()

    async def _make_request(
            self,
            method: str,
            endpoint: str,
            params: Dict[str, Any] = None,
            private: bool = True,
            return_full_response: bool = False
    ) -> Optional[Dict[str, Any]]:
        """Выполнение HTTP запроса к API с retry механизмом"""
        if params is None:
            params = {}

        await self._ensure_session()

        for attempt in range(self.max_retries + 1):
            try:
                await self._rate_limit()

                timestamp = str(int(time.time() * 1000))
                url = f"{self.base_url}{endpoint}"
                headers = {}

                # --- Логика сгруппирована по типу метода ---
                if method == "GET":
                    # Для GET запросов параметры сортируются для подписи, если запрос приватный
                    request_params = params
                    if private:
                        sorted_params_list = sorted(params.items())
                        signature_params = urlencode(sorted_params_list)
                        signature = self._generate_signature(signature_params, timestamp)
                        headers.update({
                            "X-BAPI-API-KEY": self.api_key, "X-BAPI-SIGN": signature,
                            "X-BAPI-SIGN-TYPE": "2", "X-BAPI-TIMESTAMP": timestamp,
                            "X-BAPI-RECV-WINDOW": "5000"
                        })
                        # Используем отсортированный список кортежей для запроса, чтобы гарантировать порядок
                        request_params = sorted_params_list

                    async with self.session.get(url, headers=headers, params=request_params) as response:
                        response_result = await response.json(content_type=None) if response.content else None

                elif method == "POST":
                    # Для POST запросов тело JSON используется для подписи
                    if private:
                        signature_params = json.dumps(params) if params else ""
                        signature = self._generate_signature(signature_params, timestamp)
                        headers.update({
                            "X-BAPI-API-KEY": self.api_key, "X-BAPI-SIGN": signature,
                            "X-BAPI-SIGN-TYPE": "2", "X-BAPI-TIMESTAMP": timestamp,
                            "X-BAPI-RECV-WINDOW": "5000"
                        })
                    headers["Content-Type"] = "application/json"

                    async with self.session.post(url, headers=headers, json=params) as response:
                        response_result = await response.json(content_type=None) if response.content else None
                else:
                    log_error(self.user_id, f"Неподдерживаемый HTTP метод: {method}", module_name="bybit_api")
                    return None

                # --- Общая логика обработки ответа ---
                if return_full_response:
                    return response_result

                ret_code = response_result.get("retCode", -1) if response_result else -1

                # Обработка успешного ответа
                if ret_code == 0:
                    return response_result.get("result", {})

                # Специальная обработка для ошибки "ордер не существует" при отмене
                if endpoint == "/v5/order/cancel" and ret_code == 110001:
                    log_warning(self.user_id,
                                f"Попытка отменить ордер, который уже не существует (код: 110001). Считаем операцию успешной.",
                                module_name="bybit_api")
                    return {"status": "already_cancelled"}  # Возвращаем непустой словарь для успеха

                # Обработка других ошибок
                else:
                    error_msg = response_result.get("retMsg", "получен пустой ответ от сервера") if response_result else "получен пустой ответ от сервера"
                    log_error(self.user_id, f"API ошибка: {error_msg} (код: {ret_code})", module_name="bybit_api")
                    if ret_code in [10003, 10004]:
                        log_error(self.user_id,f"КРИТИЧЕСКАЯ ОШИБКА АУТЕНТИФИКАЦИИ (код: {ret_code}): {error_msg}. Проверьте правильность API ключей и их права доступа!",
                                  module_name="bybit_api")
                        return None  # Для ошибок ключей выходим сразу

                    if attempt < self.max_retries:
                        await asyncio.sleep(self.retry_delay * (attempt + 1))
                        continue
                    else:
                        return {}

            except asyncio.TimeoutError:
                log_error(self.user_id, f"Таймаут запроса (попытка {attempt + 1})", module_name="bybit_api")
                if attempt < self.max_retries:
                    await asyncio.sleep(self.retry_delay * (attempt + 1))
                else:
                    return None
            except Exception as e:
                log_error(self.user_id, f"Ошибка запроса (попытка {attempt + 1}): {e}", module_name="bybit_api")
                if attempt < self.max_retries:
                    await asyncio.sleep(self.retry_delay * (attempt + 1))
                else:
                    return None
        return None
    
    # =============================================================================
    # ПУБЛИЧНЫЕ МЕТОДЫ API
    # =============================================================================
    
    async def get_ticker(self, symbol: str) -> Optional[Dict[str, Any]]:
        """Получение тикера инструмента"""
        try:
            params = {
                "category": "linear",
                "symbol": symbol
            }
            
            result = await self._make_request("GET", "/v5/market/tickers", params, private=False)
            
            if result and "list" in result and result["list"]:
                ticker_data = result["list"][0]
                
                # Конвертация в Decimal для точности
                return {
                    "symbol": ticker_data.get("symbol"),
                    "lastPrice": to_decimal(ticker_data.get("lastPrice", "0")),
                    "bid1Price": to_decimal(ticker_data.get("bid1Price", "0")),
                    "ask1Price": to_decimal(ticker_data.get("ask1Price", "0")),
                    "volume24h": to_decimal(ticker_data.get("volume24h", "0")),
                    "turnover24h": to_decimal(ticker_data.get("turnover24h", "0")),
                    "price24hPcnt": to_decimal(ticker_data.get("price24hPcnt", "0"))
                }
                
        except Exception as e:
            log_error(self.user_id, f"Ошибка получения тикера {symbol}: {e}", module_name="bybit_api")
            
        return None
    
    async def get_order_book(self, symbol: str, limit: int = 25) -> Optional[Dict[str, Any]]:
        """Получение стакана ордеров"""
        try:
            params = {
                "category": "linear",
                "symbol": symbol,
                "limit": limit
            }
            
            result = await self._make_request("GET", "/v5/market/orderbook", params, private=False)
            
            if result:
                # Конвертация в Decimal
                bids = []
                asks = []

                for bid in result.get("b", []):
                    bids.append([to_decimal(bid[0]), to_decimal(bid[1])])

                for ask in result.get("a", []):
                    asks.append([to_decimal(ask[0]), to_decimal(ask[1])])
                
                return {
                    "symbol": symbol,
                    "bids": bids,
                    "asks": asks,
                    "timestamp": result.get("ts")
                }
                
        except Exception as e:
            log_error(self.user_id, f"Ошибка получения стакана {symbol}: {e}", module_name="bybit_api")
            
        return None


    async def get_current_price(self, symbol: str) -> Optional[Decimal]:
        """
        Получает последнюю цену для указанного символа.
        Является удобной оберткой над get_ticker.
        """
        try:
            ticker_data = await self.get_ticker(symbol)
            if ticker_data and "lastPrice" in ticker_data:
                return ticker_data["lastPrice"]
            log_warning(self.user_id, f"Не удалось получить последнюю цену для {symbol} через тикер.", module_name=__name__)
            return None
        except Exception as e:
            log_error(self.user_id, f"Ошибка при получении текущей цены для {symbol}: {e}", module_name=__name__)
            return None

    async def get_order_status(self, order_id: str) -> Optional[Dict[str, Any]]:
        """
        Получает статус конкретного ордера по его ID.
        Использует эндпоинт истории ордеров для получения самых свежих данных.
        """
        try:
            endpoint = "/v5/order/history"
            params = {
                "category": "linear",
                "orderId": order_id,
                "limit": 1
            }

            # ИСПРАВЛЕНИЕ: Используем правильный метод _make_request
            # Запрос к истории ордеров является приватным
            response_data = await self._make_request(method="GET", endpoint=endpoint, params=params, private=True, return_full_response=True)

            if response_data and response_data.get("retCode") == 0 and response_data.get('result', {}).get('list'):
                order_details = response_data['result']['list'][0]
                return {
                    "orderId": order_details.get("orderId"),
                    "orderStatus": order_details.get("orderStatus"),
                    "side": order_details.get("side"),
                    "avgPrice": order_details.get("avgPrice", '0'),
                    "cumExecQty": order_details.get("cumExecQty", '0'),
                    "cumExecFee": order_details.get("cumExecFee", '0')
                }
            else:
                # Это может произойти, если ордер еще не попал в историю. Не считаем это ошибкой.
                log_info(self.user_id, f"Статус для ордера {order_id} пока не найден в истории. Ответ API: {response_data}", module_name=__name__)
                return None

        except Exception as e:
            log_error(self.user_id, f"Критическая ошибка при получении статуса ордера {order_id}: {e}", module_name=__name__)
            return None

    async def get_positions(self, symbol: str = None) -> Optional[List[Dict[str, Any]]]:
        """Получение позиций"""
        try:
            params = {
                "category": "linear"
            }

            if symbol:
                params["symbol"] = symbol

            result = await self._make_request("GET", "/v5/position/list", params)

            if result and "list" in result:
                positions = []
                for position in result["list"]:
                    # Фильтрация только активных позиций
                    size = to_decimal(position.get("size", "0"))
                    if size > 0:
                        positions.append({
                            "symbol": position.get("symbol"),
                            "side": position.get("side"),
                            "size": size,
                            "avgPrice": to_decimal(position.get("avgPrice", "0")),
                            "markPrice": to_decimal(position.get("markPrice", "0")),
                            "unrealisedPnl": to_decimal(position.get("unrealisedPnl", "0")),
                        })

                return positions

        except Exception as e:
            log_error(self.user_id, f"Ошибка получения позиций: {e}", module_name=__name__)

        return None


    async def get_klines(
        self,
        symbol: str,
        interval: str,
        limit: int = 200,
        start_time: Optional[int] = None,
        end_time: Optional[int] = None
    ) -> Optional[List[Dict[str, Any]]]:
        """Получение исторических свечей"""
        try:
            # Конвертер таймфреймов в формат, требуемый Bybit V5 API
            interval_map = {
                "1m": "1", "3m": "3", "5m": "5", "15m": "15", "30m": "30",
                "1h": "60", "2h": "120", "4h": "240", "6h": "360", "12h": "720",
                "1d": "D", "1w": "W", "1M": "M"
            }
            api_interval = interval_map.get(interval, interval) # Если нет в словаре, используем как есть

            params = {
                "category": "linear",
                "symbol": symbol,
                "interval": api_interval,
                "limit": limit
            }
            
            if start_time:
                params["start"] = start_time
            if end_time:
                params["end"] = end_time
                
            result = await self._make_request("GET", "/v5/market/kline", params, private=False)
            
            if result and "list" in result:
                candles = []
                for candle in result["list"]:
                    candles.append({
                        "start_time": int(candle[0]),
                        "open": to_decimal(candle[1]),
                        "high": to_decimal(candle[2]),
                        "low": to_decimal(candle[3]),
                        "close": to_decimal(candle[4]),
                        "volume": to_decimal(candle[5]),
                        "turnover": to_decimal(candle[6])
                    })
                    
                # Сортировка по времени (от старых к новым)
                candles.sort(key=lambda x: x["start_time"])
                return candles
                
        except Exception as e:
            log_error(self.user_id, f"Ошибка получения свечей {symbol}: {e}", module_name="bybit_api")
            
        return None

    async def get_instruments_info(self, symbol: str = None) -> Optional[Dict[str, Any]]:
        """Получение информации об инструментах с кэшированием"""
        try:
            current_time = time.time()

            # Если кэш невалиден, полностью обновляем его
            if not self.instruments_cache or (current_time - self.cache_timestamp > self.cache_expiry):
                log_info(self.user_id, "Кэш инструментов пуст или устарел. Запрашиваю полный список...",
                         module_name="bybit_api")
                params = {"category": "linear"}
                result = await self._make_request("GET", "/v5/market/instruments-info", params, private=False)

                if result and "list" in result:
                    new_cache = {}
                    for instrument in result["list"]:
                        symbol_name = instrument.get("symbol")
                        if symbol_name:
                            new_cache[symbol_name] = {
                                "symbol": symbol_name,
                                "baseCoin": instrument.get("baseCoin"),
                                "quoteCoin": instrument.get("quoteCoin"),
                                "minOrderQty": to_decimal(instrument.get("lotSizeFilter", {}).get("minOrderQty", "0")),
                                "maxOrderQty": to_decimal(instrument.get("lotSizeFilter", {}).get("maxOrderQty", "0")),
                                "qtyStep": to_decimal(instrument.get("lotSizeFilter", {}).get("qtyStep", "0")),
                                "minPrice": to_decimal(instrument.get("priceFilter", {}).get("minPrice", "0")),
                                "maxPrice": to_decimal(instrument.get("priceFilter", {}).get("maxPrice", "0")),
                                "tickSize": to_decimal(instrument.get("priceFilter", {}).get("tickSize", "0")),
                                "status": instrument.get("status")
                            }

                    self.instruments_cache = new_cache
                    self.cache_timestamp = current_time
                    log_info(self.user_id, f"Кэш инструментов обновлен. Загружено {len(new_cache)} символов.",
                             module_name="bybit_api")
                else:
                    log_error(self.user_id, "Не удалось обновить кэш инструментов.", module_name="bybit_api")
                    return None

            # Теперь, когда кэш гарантированно полный, возвращаем нужную часть
            if symbol:
                return self.instruments_cache.get(symbol)
            else:
                return self.instruments_cache

        except Exception as e:
            log_error(self.user_id, f"Ошибка получения информации об инструментах: {e}", module_name="bybit_api")

        return None
    
    # =============================================================================
    # ПРИВАТНЫЕ МЕТОДЫ API (ТОРГОВЛЯ)
    # =============================================================================

    async def get_wallet_balance(self, account_type: str = "UNIFIED") -> Optional[Dict[str, Any]]:
        """Получение баланса кошелька с фолбэком на CONTRACT аккаунт для testnet."""
        try:
            # Первая попытка: запросить указанный тип аккаунта (по умолчанию UNIFIED)
            params = {"accountType": account_type}
            result = await self._make_request("GET", "/v5/account/wallet-balance", params)

            # Если результат пустой и мы в testnet, пробуем тип CONTRACT
            is_empty_result = not (result and result.get("list"))
            if is_empty_result and self.testnet:
                log_warning(self.user_id, f"Баланс для аккаунта {account_type} не найден в testnet, пробую CONTRACT...",
                            module_name="bybit_api")
                params = {"accountType": "CONTRACT"}
                result = await self._make_request("GET", "/v5/account/wallet-balance", params)

            # Парсим результат
            if result and result.get("list"):
                account = result["list"][0]
                coins = {}

                for coin in account.get("coin", []):
                    coin_name = coin.get("coin")
                    coins[coin_name] = {
                        "coin": coin_name,
                        "walletBalance": to_decimal(coin.get("walletBalance", "0")),
                        "availableBalance": to_decimal(coin.get("availableToWithdraw", "0")),
                        "unrealisedPnl": to_decimal(coin.get("unrealisedPnl", "0")),
                        "totalEquity": to_decimal(coin.get("equity", "0"))
                    }

                return {
                    "accountType": account.get("accountType"),
                    "totalWalletBalance": to_decimal(account.get("totalWalletBalance", "0")),
                    "totalAvailableBalance": to_decimal(account.get("totalAvailableBalance", "0")),
                    "totalUnrealisedPnl": to_decimal(account.get("totalUnrealisedPnl", "0")),
                    "totalEquity": to_decimal(account.get("totalEquity", "0")),
                    "coins": coins
                }
        except Exception as e:
            log_error(self.user_id, f"Ошибка получения баланса: {e}", module_name="bybit_api")
        return None

    async def place_order(
        self,
        symbol: str,
        side: str,
        order_type: str,
        qty: Decimal,
        price: Optional[Decimal] = None,
        time_in_force: str = "GTC",
        reduce_only: bool = False,
        close_on_trigger: bool = False,
        stop_loss: Optional[Decimal] = None,
        take_profit: Optional[Decimal] = None
    ) -> Optional[str]:
        """
        Размещение ордера. Теперь этот метод ДОВЕРЯЕТ полученному qty
        и только форматирует его в правильную строку перед отправкой.
        """
        try:
            # Форматируем количество в строку с нужной точностью
            formatted_qty = await self._format_quantity(symbol, qty)

            params = {
                "category": "linear",
                "symbol": symbol,
                "side": side,
                "orderType": order_type,
                "qty": formatted_qty, # <-- Используем точно отформатированную строку
                "timeInForce": time_in_force
            }

            if price is not None:
                params["price"] = str(price)
            if reduce_only:
                params["reduceOnly"] = True
            if close_on_trigger:
                params["closeOnTrigger"] = True
            if stop_loss is not None:
                params["stopLoss"] = str(stop_loss)
            if take_profit is not None:
                params["takeProfit"] = str(take_profit)

            result = await self._make_request("POST", "/v5/order/create", params)

            if result and "orderId" in result and result["orderId"]:
                order_id = result["orderId"]
                log_info(self.user_id,
                         f"Ордер успешно размещен: {side} {formatted_qty} {symbol} по {price if price else 'рынку'} (ID: {order_id})",
                         "bybit_api")
                return order_id
            else:
                log_error(self.user_id, f"Не удалось разместить ордер. Ответ API: {result}", "bybit_api")
                return None

        except Exception as e:
            log_error(self.user_id, f"Исключение при размещении ордера: {e}", "bybit_api")
            return None

    
    async def cancel_order(self, symbol: str, order_id: str) -> bool:
        """Отмена ордера"""
        try:
            params = {
                "category": "linear",
                "symbol": symbol,
                "orderId": order_id
            }
            
            result = await self._make_request("POST", "/v5/order/cancel", params)
            
            if result:
                log_info(self.user_id, f"Ордер отменен: {order_id}", module_name="bybit_api")
                return True
            else:
                log_error(self.user_id, f"Не удалось отменить ордер: {order_id}", module_name="bybit_api")
                
        except Exception as e:
            log_error(self.user_id, f"Ошибка отмены ордера {order_id}: {e}", module_name="bybit_api")
            
        return False
    
    async def get_open_orders(self, symbol: str = None) -> Optional[List[Dict[str, Any]]]:
        """Получение открытых ордеров"""
        try:
            params = {
                "category": "linear"
            }
            
            if symbol:
                params["symbol"] = symbol
                
            result = await self._make_request("GET", "/v5/order/realtime", params)
            
            if result and "list" in result:
                orders = []
                for order in result["list"]:
                    orders.append({
                        "orderId": order.get("orderId"),
                        "symbol": order.get("symbol"),
                        "side": order.get("side"),
                        "orderType": order.get("orderType"),
                        "qty": to_decimal(order.get("qty", "0")),
                        "price": to_decimal(order.get("price", "0")),
                        "leavesQty": to_decimal(order.get("leavesQty", "0")),
                        "cumExecQty": to_decimal(order.get("cumExecQty", "0")),
                        "avgPrice": to_decimal(order.get("avgPrice", "0")),
                        "orderStatus": order.get("orderStatus"),
                        "timeInForce": order.get("timeInForce"),
                        "createdTime": order.get("createdTime"),
                        "updatedTime": order.get("updatedTime")
                    })
                    
                return orders
                
        except Exception as e:
            log_error(self.user_id, f"Ошибка получения открытых ордеров: {e}", module_name="bybit_api")
            
        return None
    

    
    async def set_leverage(self, symbol: str, leverage: int) -> bool:
        """Установка плеча"""
        try:
            params = {
                "category": "linear",
                "symbol": symbol,
                "buyLeverage": str(leverage),
                "sellLeverage": str(leverage)
            }

            response = await self._make_request("POST", "/v5/position/set-leverage", params, return_full_response=True)

            ret_code = response.get("retCode", -1) if response else -1

            if ret_code == 0 or ret_code == 110043:
                if ret_code == 0:
                    log_info(self.user_id, f"Плечо успешно установлено {leverage}x для {symbol}",
                             module_name="bybit_api")
                else:
                    log_info(self.user_id, f"Плечо для {symbol} уже было {leverage}x. Изменения не требуются.",
                             module_name="bybit_api")
                return True
            else:
                error_msg = response.get("retMsg", "Неизвестная ошибка") if response else "Пустой ответ"
                log_error(self.user_id,
                          f"Не удалось установить плечо для {symbol}. Ошибка: {error_msg} (код: {ret_code})",
                          module_name="bybit_api")

        except Exception as e:
            log_error(self.user_id, f"Ошибка установки плеча для {symbol}: {e}", module_name="bybit_api")

        return False
    
    async def set_trading_stop(
        self,
        symbol: str,
        stop_loss: Optional[Decimal] = None,
        take_profit: Optional[Decimal] = None,
        position_idx: int = 0
    ) -> bool:
        """Установка стоп-лосса и тейк-профита"""
        try:
            params = {
                "category": "linear",
                "symbol": symbol,
                "positionIdx": position_idx
            }
            
            if stop_loss is not None:
                params["stopLoss"] = str(stop_loss)
                
            if take_profit is not None:
                params["takeProfit"] = str(take_profit)

            response = await self._make_request("POST", "/v5/position/trading-stop", params, return_full_response=True)

            ret_code = response.get("retCode", -1) if response else -1

            if ret_code == 0:
                log_info(self.user_id,
                         f"Торговые стопы успешно установлены/обновлены для {symbol}: SL={stop_loss}, TP={take_profit}",
                         module_name="bybit_api")
                return True
            # Код 34040 означает "not modified" - это не ошибка, а подтверждение, что нужные стопы уже стоят.
            elif ret_code == 34040:
                log_info(self.user_id, f"Торговые стопы для {symbol} не требуют обновления.", module_name="bybit_api")
                return True
            else:
                error_msg = response.get("retMsg", "Неизвестная ошибка") if response else "Пустой ответ"
                log_error(self.user_id,
                          f"Не удалось установить торговые стопы для {symbol}. Ошибка: {error_msg} (код: {ret_code})",
                          module_name="bybit_api")
                
        except Exception as e:
            log_error(self.user_id, f"Ошибка установки торговых стопов для {symbol}: {e}", module_name="bybit_api")
            
        return False
    
    # =============================================================================
    # ВСПОМОГАТЕЛЬНЫЕ МЕТОДЫ
    # =============================================================================

    # 1. НОВЫЙ ВСПОМОГАТЕЛЬНЫЙ МЕТОД
    async def _format_quantity(self, symbol: str, qty: Decimal) -> str:
        """
        Форматирует количество в строку с точной десятичной точностью,
        требуемой биржей для данного символа, используя округление вниз.
        """
        from decimal import ROUND_DOWN
        try:
            instrument_info = await self.get_instruments_info(symbol)
            if not instrument_info:
                return qty.to_eng_string()

            qty_step_str = str(instrument_info.get("qtyStep", "0.001"))

            if '.' in qty_step_str:
                precision = len(qty_step_str.split('.')[1].rstrip('0'))
            else:
                precision = 0

            quantizer = Decimal('1e-' + str(precision))

            formatted_qty_decimal = qty.quantize(quantizer, rounding=ROUND_DOWN)

            return str(formatted_qty_decimal)

        except Exception as e:
            log_error(self.user_id, f"Ошибка форматирования количества для {symbol}: {e}", "bybit_api")
            return qty.to_eng_string()

    async def calculate_quantity_from_usdt(
            self,
            symbol: str,
            usdt_amount: Decimal,
            leverage: Decimal,  # <-- ДОБАВЛЕН ПАРАМЕТР ПЛЕЧА
            price: Optional[Decimal] = None
    ) -> Decimal:
        """
        Рассчитывает и ОКРУГЛЯЕТ количество, проверяя МИНИМАЛЬНЫЙ РАЗМЕР ордера.
        ВКЛЮЧАЕТ УЧЕТ ПЛЕЧА и ДЕТАЛЬНОЕ ЛОГИРОВАНИЕ ДЛЯ ДИАГНОСТИКИ.
        """
        log_info(self.user_id, f"--- [QTY DEBUG START] ---", "bybit_api")
        log_info(self.user_id, f"[QTY_DEBUG] Расчет для {symbol} | Сумма: {usdt_amount} USDT | Плечо: {leverage}x",
                 "bybit_api")
        try:
            if price is None:
                ticker = await self.get_ticker(symbol)
                if not ticker or ticker["lastPrice"] <= 0:
                    log_error(self.user_id, f"[QTY_DEBUG] ОШИБКА: Не удалось получить актуальную цену для {symbol}",
                              "bybit_api")
                    return Decimal('0')
                price = ticker["lastPrice"]
            log_info(self.user_id, f"[QTY_DEBUG] Цена для расчета: {price}", "bybit_api")

            # ИСПРАВЛЕНИЕ: Рассчитываем номинальную стоимость позиции с учетом плеча
            notional_value = usdt_amount * leverage
            base_qty = notional_value / price
            log_info(self.user_id,
                     f"[QTY_DEBUG] Номинальная стоимость: {notional_value:.4f} USDT | Сырое кол-во: {base_qty}",
                     "bybit_api")

            instrument_info = await self.get_instruments_info(symbol)
            if instrument_info:
                qty_step = instrument_info.get("qtyStep", Decimal("0.001"))
                min_qty = instrument_info.get("minOrderQty", Decimal("0"))
                log_info(self.user_id, f"[QTY_DEBUG] Правила инструмента: qtyStep={qty_step}, minOrderQty={min_qty}",
                         "bybit_api")

                if qty_step > 0:
                    # Округление ВНИЗ до ближайшего шага
                    floored_qty = (base_qty // qty_step) * qty_step
                    log_info(self.user_id, f"[QTY_DEBUG] Кол-во после округления вниз до шага: {floored_qty}",
                             "bybit_api")
                else:
                    floored_qty = base_qty

                if floored_qty < min_qty:
                    log_warning(self.user_id,
                                f"[QTY_DEBUG] ОШИБКА: Рассчитанное кол-во {floored_qty} меньше минимального {min_qty}.",
                                "bybit_api")
                    return Decimal('0')

                log_info(self.user_id, f"--- [QTY DEBUG END] Финальное кол-во (Decimal): {floored_qty} ---",
                         "bybit_api")
                return floored_qty
            else:
                log_error(self.user_id, f"[QTY_DEBUG] ОШИБКА: Не удалось получить instrument_info для {symbol}.",
                          "bybit_api")

        except Exception as e:
            log_error(self.user_id, f"[QTY_DEBUG] КРИТИЧЕСКАЯ ОШИБКА при расчете: {e}", "bybit_api")

        return Decimal('0')
    
    async def round_price(self, symbol: str, price: Decimal) -> Decimal:
        """Округление цены до допустимого шага"""
        try:
            instrument_info = await self.get_instruments_info(symbol)
            if instrument_info:
                tick_size = instrument_info.get("tickSize", Decimal('0.01'))
                
                if tick_size > 0:
                    return (price // tick_size) * tick_size
                    
            return price
            
        except Exception as e:
            log_error(self.user_id, f"Ошибка округления цены для {symbol}: {e}", module_name="bybit_api")
            return price
    
    async def validate_order_params(
        self, 
        symbol: str, 
        qty: Decimal, 
        price: Optional[Decimal] = None
    ) -> bool:
        """Валидация параметров ордера"""
        try:
            instrument_info = await self.get_instruments_info(symbol)
            if not instrument_info:
                return False
                
            # Проверка количества
            min_qty = instrument_info.get("minOrderQty", Decimal('0'))
            max_qty = instrument_info.get("maxOrderQty", Decimal('999999999'))
            
            if qty < min_qty or qty > max_qty:
                log_error(self.user_id, f"Некорректное количество {qty} для {symbol} (мин: {min_qty}, макс: {max_qty})", module_name="bybit_api")
                return False
                
            # Проверка цены (если указана)
            if price is not None:
                min_price = instrument_info.get("minPrice", Decimal('0'))
                max_price = instrument_info.get("maxPrice", Decimal('999999999'))
                
                if price < min_price or price > max_price:
                    log_error(self.user_id, f"Некорректная цена {price} для {symbol} (мин: {min_price}, макс: {max_price})", module_name="bybit_api")
                    return False
                    
            return True
            
        except Exception as e:
            log_error(self.user_id, f"Ошибка валидации параметров ордера: {e}", module_name="bybit_api")
            return False

