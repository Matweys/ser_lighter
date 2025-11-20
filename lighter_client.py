# lighter_client.py
import asyncio
import time
from typing import Any, Dict, List, Optional, Tuple, Union

import lighter
from lighter.configuration import Configuration
from lighter.signer_client import SignerClient


# ====== Константы окружения (подставь при необходимости) ======
BASE_URL = "https://mainnet.zklighter.elliot.ai"

ACCOUNT_INDEX = 24844
API_KEY_INDEX = 0

# Рынок SOL/USDT
MARKET_ID_SOL = 2

# Десятичность цены/размера (укажи под свой рынок)
PRICE_DECIMALS = 3
SIZE_DECIMALS = 3

# Максимальный допуск по цене для рыночного IOC (на всякий случай)
MAX_SLIPPAGE = 0.01  # 1%

# Приватный ключ Lighter API (как у тебя)
API_PRIV = "9c0e33110d77faca346ce7ff8b2fe6c051d5d152850cd6e74ff38dfc7ecadd83f0dc4cdf8cfadf07"


def _to_dict_safe(obj: Any) -> Any:
    if obj is None or isinstance(obj, (str, int, float, bool)):
        return obj
    if isinstance(obj, dict):
        return {k: _to_dict_safe(v) for k, v in obj.items()}
    if isinstance(obj, (list, tuple, set)):
        return [_to_dict_safe(x) for x in obj]
    td = getattr(obj, "to_dict", None)
    if callable(td):
        try:
            return td()
        except Exception:
            pass
    return {k: _to_dict_safe(v) for k, v in obj.__dict__.items() if not k.startswith("_")}


def _extract_cursor(obj: Any) -> Optional[str]:
    d = _to_dict_safe(obj) or {}
    for k in ("next_cursor", "cursor", "next", "nextCursor"):
        v = d.get(k)
        if isinstance(v, str) and v:
            return v
    return None


def _flt(x: Any) -> Optional[float]:
    if x is None:
        return None
    if isinstance(x, (int, float)):
        return float(x)
    if isinstance(x, str):
        try:
            return float(x)
        except ValueError:
            return None
    return None


def _int_from_units(s: Union[str, float, int], decimals: int) -> Optional[int]:
    if isinstance(s, int):
        return s
    if isinstance(s, float):
        return max(1, int(round(s * (10 ** decimals))))
    if isinstance(s, str):
        f = _flt(s)
        if f is None:
            return None
        return max(1, int(round(f * (10 ** decimals))))
    return None


class LighterClient:
    def __init__(
        self,
        base_url: str = BASE_URL,
        account_index: int = ACCOUNT_INDEX,
        api_key_index: int = API_KEY_INDEX,
        api_priv: str = API_PRIV,
        price_decimals: int = PRICE_DECIMALS,
        size_decimals: int = SIZE_DECIMALS,
        max_slippage: float = MAX_SLIPPAGE,
    ) -> None:
        self.base_url = base_url
        self.account_index = account_index
        self.api_key_index = api_key_index
        self.api_priv = api_priv

        self.price_decimals = price_decimals
        self.size_decimals = size_decimals
        self.max_slippage = max_slippage

        self._cfg = Configuration(host=self.base_url)
        self._api_client = lighter.ApiClient(configuration=self._cfg)
        self._signer = SignerClient(
            url=self.base_url,
            private_key=self.api_priv,
            api_key_index=self.api_key_index,
            account_index=self.account_index,
        )
        self._authed = False

    # ---------- lifecycle ----------
    async def __aenter__(self):
        await self._ensure_auth()
        return self

    async def __aexit__(self, exc_type, exc, tb):
        await self.close()

    async def _ensure_auth(self) -> None:
        if self._authed:
            return
        auth, err = self._signer.create_auth_token_with_expiry()
        if err:
            raise RuntimeError(f"Auth token error: {err}")
        self._api_client.default_headers["Authorization"] = auth
        self._authed = True

    async def close(self) -> None:
        try:
            await self._signer.close()
        finally:
            await self._api_client.close()

    # ---------- low-level ----------
    def _order_api(self) -> lighter.OrderApi:
        return lighter.OrderApi(self._api_client)

    def _account_api(self) -> lighter.AccountApi:
        return lighter.AccountApi(self._api_client)

    def _tx_api(self) -> lighter.TransactionApi:
        return lighter.TransactionApi(self._api_client)

    async def next_nonce(self) -> int:
        nn = await self._tx_api().next_nonce(
            account_index=self.account_index,
            api_key_index=self.api_key_index,
        )
        return nn.nonce

    async def account_overview(self) -> Any:
        accs = await self._account_api().account(by="index", value=str(self.account_index))
        return accs.accounts[0]

    async def top_of_book(self, market_id: int, limit: int = 1) -> Tuple[Optional[int], Optional[int]]:
        """(best_ask_int, best_bid_int) в integer-цене."""
        ob = await self._order_api().order_book_orders(market_id=market_id, limit=limit)
        best_ask = None
        best_bid = None
        if getattr(ob, "asks", None):
            best_ask = int(ob.asks[0].price.replace(".", ""))
        if getattr(ob, "bids", None):
            best_bid = int(ob.bids[0].price.replace(".", ""))
        return best_ask, best_bid

    # ---------- positions ----------
    async def get_position_size_base(self, symbol: str = "SOL") -> float:
        """Возвращает текущий размер позиции (в базовом активе), + для long, - для short."""
        pnl = await self.get_positions_pnl()
        for p in pnl.get("positions", []):
            if (p.get("symbol") or "").upper() == symbol.upper():
                sz = _flt(p.get("size")) or 0.0
                return float(sz)
        return 0.0

    async def get_positions_pnl(self) -> Dict[str, Any]:
        await self._ensure_auth()
        acc = await self.account_overview()
        positions = getattr(acc, "positions", None) or []
        out: List[Dict[str, Any]] = []
        total_unreal = 0.0
        total_real = 0.0
        for p in positions:
            pd = _to_dict_safe(p)
            sym = pd.get("symbol")
            size = pd.get("position", pd.get("size"))
            entry = pd.get("avg_entry_price")
            u = float(pd.get("unrealized_pnl") or 0)
            r = float(pd.get("realized_pnl") or 0)
            total_unreal += u
            total_real += r
            out.append(
                {
                    "symbol": sym,
                    "size": size,
                    "avg_entry_price": entry,
                    "unrealized_pnl": u,
                    "realized_pnl": r,
                }
            )
        return {
            "account_index": getattr(acc, "index", self.account_index),
            "available_balance": getattr(acc, "available_balance", None),
            "positions": out,
            "unrealized_pnl_total": total_unreal,
            "realized_pnl_total": total_real,
        }

    # ---------- orders ----------
    async def open_order_market_ioc(
        self,
        market_id: int = MARKET_ID_SOL,
        notional_usd: float = 10.0,
        is_buy: bool = True,
    ) -> Dict[str, Any]:
        """Рыночный (IOC) вход на заданный нотионал USDT."""
        await self._ensure_auth()

        best_ask_int, best_bid_int = await self.top_of_book(market_id)
        if is_buy and not best_ask_int:
            raise RuntimeError("Стакан пуст: нет ask.")
        if not is_buy and not best_bid_int:
            raise RuntimeError("Стакан пуст: нет bid.")

        px_int = best_ask_int if is_buy else best_bid_int
        price_usd = px_int / (10 ** self.price_decimals)
        size_base = max(1e-12, notional_usd / price_usd)
        base_amount_int = max(1, int(round(size_base * (10 ** self.size_decimals))))

        acceptable_price_int = (
            round(px_int * (1 + self.max_slippage)) if is_buy else max(1, round(px_int * (1 - self.max_slippage)))
        )

        nonce = await self.next_nonce()
        _, tx_hash, err = await self._signer.create_order(
            market_index=market_id,
            client_order_index=0,
            base_amount=base_amount_int,
            price=acceptable_price_int,
            is_ask=not is_buy,
            order_type=SignerClient.ORDER_TYPE_MARKET,
            time_in_force=SignerClient.ORDER_TIME_IN_FORCE_IMMEDIATE_OR_CANCEL,
            order_expiry=SignerClient.DEFAULT_IOC_EXPIRY,
            reduce_only=False,
            api_key_index=self.api_key_index,
            nonce=nonce,
        )
        if err:
            raise RuntimeError(f"create_order MARKET IOC failed: {err}")

        return {
            "tx_hash": getattr(tx_hash, "tx_hash", tx_hash),
            "requested_notional_usd": notional_usd,
            "base_amount_int": base_amount_int,
            "acceptable_price_int": acceptable_price_int,
            "side": "BUY" if is_buy else "SELL",
        }

    async def place_reduce_only_limit(
            self,
            *,
            market_id: int = MARKET_ID_SOL,
            is_sell: bool,
            base_amount_int: int,
            price_int: int,
    ) -> Dict[str, Any]:
        """Выставить reduce-only LIMIT ордер (обычно TP)."""
        await self._ensure_auth()

        # ✅ правильная константа TIF и ЯВНАЯ передача
        tif = SignerClient.ORDER_TIME_IN_FORCE_GOOD_TILL_TIME
        # 28-дневный срок из SDK (сам Signer подставит корректную дату)
        expiry = getattr(SignerClient, "DEFAULT_28_DAY_ORDER_EXPIRY", -1)

        nonce = await self.next_nonce()
        _, tx_hash, err = await self._signer.create_order(
            market_index=market_id,
            client_order_index=0,
            base_amount=max(1, int(base_amount_int)),
            price=max(1, int(price_int)),
            is_ask=bool(is_sell),
            order_type=SignerClient.ORDER_TYPE_LIMIT,  # явно LIMIT
            time_in_force=tif,  # ✅ обязательно
            order_expiry=expiry,  # ✅ долгий срок
            reduce_only=True,
            api_key_index=self.api_key_index,
            nonce=nonce,
        )
        if err:
            raise RuntimeError(f"create_order LIMIT reduce_only failed: {err}")

        return {
            "tx_hash": getattr(tx_hash, "tx_hash", tx_hash),
            "is_sell": is_sell,
            "base_amount_int": int(base_amount_int),
            "price_int": int(price_int),
        }

    # Высокоуровневый хелпер: открыть рынок и сразу поставить TP
    async def open_and_place_tp(
        self,
        *,
        market_id: int = MARKET_ID_SOL,
        is_long: bool,
        notional_usd: float,
        target_price_usd: float,
        fill_wait_s: float = 20.0,
        poll_interval_s: float = 1.0,
        symbol_for_pos: str = "SOL",
    ) -> Dict[str, Any]:
        """Вход MARKET IOC и немедленный LIMIT TP reduce_only на target_price."""
        open_info = await self.open_order_market_ioc(
            market_id=market_id, notional_usd=notional_usd, is_buy=is_long
        )

        # Ждём, пока позиция появится
        size_base = 0.0
        t0 = time.time()
        while time.time() - t0 < fill_wait_s:
            sz = await self.get_position_size_base(symbol_for_pos)
            if abs(sz) > 1e-12:
                size_base = abs(sz)
                break
            await asyncio.sleep(poll_interval_s)

        # Фолбэк: если позиция ещё не отразилась, ставим TP на запрошенный размер
        if size_base <= 0.0:
            size_base = (open_info["base_amount_int"] / (10 ** self.size_decimals))

        base_amount_int = max(1, int(round(size_base * (10 ** self.size_decimals))))
        price_int = max(1, int(round(target_price_usd * (10 ** self.price_decimals))))
        is_sell_tp = True if is_long else False  # long -> sell TP, short -> buy TP

        tp_info = await self.place_reduce_only_limit(
            market_id=market_id,
            is_sell=is_sell_tp,
            base_amount_int=base_amount_int,
            price_int=price_int,
        )

        return {
            "entry": open_info,
            "tp": tp_info,
        }
