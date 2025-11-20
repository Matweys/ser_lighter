# lighter_md.py
import asyncio
import time
from typing import AsyncIterator, Optional, Tuple, List, Dict, Any

import lighter
from lighter.configuration import Configuration

BASE_URL = "https://mainnet.zklighter.elliot.ai"
MARKET_ID_SOL = 2  # SOL/USDT


def _to_dict_safe(obj: Any) -> Dict[str, Any]:
    if obj is None:
        return {}
    if isinstance(obj, dict):
        return obj
    td = getattr(obj, "to_dict", None)
    if callable(td):
        try:
            return td()
        except Exception:
            pass
    out = {}
    for k, v in getattr(obj, "__dict__", {}).items():
        if not k.startswith("_"):
            out[k] = v
    return out


def _tf_to_ms(resolution: str) -> int:
    r = resolution.strip().lower()
    if r.endswith("ms"):
        return int(r[:-2])
    if r.endswith("s"):
        return int(r[:-1]) * 1000
    if r.endswith("m"):
        return int(r[:-1]) * 60_000
    if r.endswith("h"):
        return int(r[:-1]) * 3_600_000
    if r.endswith("d"):
        return int(r[:-1]) * 86_400_000
    if r.isdigit():  # «голые» секунды
        return int(r) * 1000
    raise ValueError(f"Unsupported resolution: {resolution}")


class LighterMDClient:
    """
    MD-клиент к Lighter:
      • fetch_ohlcv: история свечей (поддерживает разные версии сигнатуры candlesticks)
      • stream_midprice: опрос лучшего bid/ask -> mid
    """
    def __init__(self, base_url: str = BASE_URL):
        self._cfg = Configuration(host=base_url)
        self._api_client = lighter.ApiClient(configuration=self._cfg)
        self._order_api = lighter.OrderApi(self._api_client)
        self._candle_api = lighter.CandlestickApi(self._api_client)

    async def aclose(self):
        await self._api_client.close()

    async def fetch_ohlcv(
        self,
        market_id: int = MARKET_ID_SOL,
        resolution: str = "5m",
        count_back: int = 300,
        end_timestamp_ms: Optional[int] = None,
    ) -> List[Dict[str, float]]:
        """
        Возвращает список свечей вида:
        [{'timestamp': ms, 'open':..., 'high':..., 'low':..., 'close':..., 'volume':...}, ...]
        """
        if end_timestamp_ms is None:
            end_timestamp_ms = int(time.time() * 1000)

        tf_ms = _tf_to_ms(resolution)
        start_ms = end_timestamp_ms - (count_back + 2) * tf_ms  # небольшой запас

        # разные варианты resolution для совместимости SDK
        res_variants = []
        r = resolution.strip().lower()
        res_variants.append(r)  # как есть (например "5m")
        if r.endswith("m"):
            res_variants.append(r[:-1])              # "5"
            res_variants.append(f"{int(r[:-1])*60}s")  # "300s"
            res_variants.append(f"{int(r[:-1])*60_000}ms")  # "300000ms"
        elif r.endswith("s"):
            res_variants.append(f"{int(r[:-1])//60}m")

        # market param name иногда "market_index"
        market_param_options = [
            {"market_index": market_id},
            {"market_id": market_id},
        ]

        # Комбинации параметров: требуем явно start_timestamp/end_timestamp (+/- count_back)
        attempts: List[Dict[str, Any]] = []
        for mp in market_param_options:
            for rv in res_variants:
                attempts.append({**mp, "resolution": rv,
                                 "start_timestamp": start_ms, "end_timestamp": end_timestamp_ms})
                attempts.append({**mp, "resolution": rv,
                                 "start_timestamp": start_ms, "end_timestamp": end_timestamp_ms,
                                 "count_back": count_back})

        last_exc: Optional[Exception] = None
        resp = None
        for params in attempts:
            try:
                resp = await self._candle_api.candlesticks(**params)
                break
            except Exception as e:
                last_exc = e
                continue

        if resp is None:
            raise RuntimeError(f"candlesticks() failed; last error: {last_exc}")

        items = getattr(resp, "candlesticks", None) or getattr(resp, "items", None) or []
        out: List[Dict[str, float]] = []
        for it in items:
            d = _to_dict_safe(it)
            ts = (d.get("start_timestamp") or d.get("timestamp") or d.get("ts") or d.get("start") or 0)
            o = float(d.get("open") or d.get("o") or 0)
            h = float(d.get("high") or d.get("h") or 0)
            l = float(d.get("low") or d.get("l") or 0)
            c = float(d.get("close") or d.get("c") or 0)
            v = float(d.get("volume") or d.get("v") or 0)
            out.append({"timestamp": int(ts), "open": o, "high": h, "low": l, "close": c, "volume": v})

        out.sort(key=lambda x: x["timestamp"])
        if len(out) > count_back:
            out = out[-count_back:]
        return out

    async def top_of_book(self, market_id: int = MARKET_ID_SOL) -> Tuple[Optional[float], Optional[float]]:
        ob = await self._order_api.order_book_orders(market_id=market_id, limit=1)
        best_ask = float(ob.asks[0].price) if getattr(ob, "asks", None) else None
        best_bid = float(ob.bids[0].price) if getattr(ob, "bids", None) else None
        return best_ask, best_bid

    async def stream_midprice(
        self,
        market_id: int = MARKET_ID_SOL,
        poll_interval: float = 1.0
    ) -> AsyncIterator[dict]:
        try:
            while True:
                ask, bid = await self.top_of_book(market_id=market_id)
                mid = 0.5 * (ask + bid) if (ask is not None and bid is not None) else None
                yield {"ts": int(time.time() * 1000), "ask": ask, "bid": bid, "mid": mid}
                await asyncio.sleep(poll_interval)
        finally:
            await self.aclose()
