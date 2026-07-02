"""
Interactive Brokers provider via ib_async.

pip install ib_async
TWS or IB Gateway must be running with API connections enabled.

Port conventions:
  7497 — TWS paper trading
  7496 — TWS live trading
  4002 — IB Gateway paper trading
  4001 — IB Gateway live trading
"""
import asyncio
import logging
import math
import threading
from dataclasses import replace
from datetime import datetime
from typing import Optional

from ..base import DataProvider
from ...core.models import Bar, Quote

log = logging.getLogger(__name__)

_CONNECT_TIMEOUT = 30   # seconds to wait for IB handshake
_POLL_INTERVAL   = 0.5  # seconds between cache refresh from tickers


def _f(val, default: float = 0.0) -> float:
    if val is None:
        return default
    try:
        v = float(val)
        return default if math.isnan(v) or math.isinf(v) else v
    except (TypeError, ValueError):
        return default


def _i(val, default: int = 0) -> int:
    v = _f(val)
    return int(v) if v > 0 else default


class IBKRProvider(DataProvider):
    """
    Streaming OHLCV + order execution via Interactive Brokers.

    Runs an asyncio event loop in a background daemon thread.
    `get_quotes()` reads from an in-memory cache refreshed every
    _POLL_INTERVAL seconds from live ib_async Ticker objects.
    """

    def __init__(
        self,
        host: str = "127.0.0.1",
        port: int = 4002,
        client_id: int = 1,
        symbols: Optional[list[str]] = None,
    ) -> None:
        self._host = host
        self._port = port
        self._client_id = client_id
        self._symbols: list[str] = list(symbols or [])

        self._ib = None
        self._cache: dict[str, Quote] = {}
        self._tickers: dict[str, object] = {}   # sym → ib_async.Ticker

        self._loop: Optional[asyncio.AbstractEventLoop] = None
        self._thread: Optional[threading.Thread] = None
        self._connected = False

    # ── DataProvider interface ────────────────────────────────────────────────

    def connect(self) -> None:
        self._loop = asyncio.new_event_loop()
        self._thread = threading.Thread(
            target=self._run_loop,
            daemon=True,
            name="ibkr-event-loop",
        )
        self._thread.start()
        future = asyncio.run_coroutine_threadsafe(self._async_connect(), self._loop)
        future.result(timeout=_CONNECT_TIMEOUT)

    def disconnect(self) -> None:
        self._connected = False
        if self._loop and self._loop.is_running():
            future = asyncio.run_coroutine_threadsafe(self._async_disconnect(), self._loop)
            try:
                future.result(timeout=5)
            except Exception:
                pass
            self._loop.call_soon_threadsafe(self._loop.stop)
        self._ib = None

    def is_connected(self) -> bool:
        return (
            self._connected
            and self._ib is not None
            and self._ib.isConnected()
            and bool(self._thread and self._thread.is_alive())
        )

    def get_quote(self, symbol: str) -> Quote:
        self._ensure_subscribed(symbol)
        return self._cache[symbol]

    def get_quotes(self, symbols: list[str]) -> list[Quote]:
        if "*" in symbols:
            return list(self._cache.values())
        for sym in symbols:
            self._ensure_subscribed(sym)
        return [self._cache[s] for s in symbols if s in self._cache]

    def get_bars(self, symbol: str, limit: int = 100) -> list[Bar]:
        if not self._loop:
            raise RuntimeError("Not connected")
        future = asyncio.run_coroutine_threadsafe(
            self._fetch_bars(symbol, limit), self._loop
        )
        return future.result(timeout=20)

    # ── Order execution ───────────────────────────────────────────────────────

    def place_order(
        self,
        symbol: str,
        action: str,          # "BUY" | "SELL"
        quantity: float,
        order_type: str = "MKT",   # "MKT" | "LMT" | "STP"
        limit_price: float = 0.0,
        stop_price: float = 0.0,
        tif: str = "DAY",
    ):
        """
        Submit an order. Returns the ib_async Trade object.

        Examples:
            provider.place_order("NVDA", "BUY",  10)
            provider.place_order("NVDA", "SELL", 10, "LMT", limit_price=120.0)
            provider.place_order("NVDA", "SELL", 10, "STP", stop_price=118.0)
        """
        if not self._loop:
            raise RuntimeError("Not connected")
        future = asyncio.run_coroutine_threadsafe(
            self._async_place_order(symbol, action, quantity, order_type, limit_price, stop_price, tif),
            self._loop,
        )
        return future.result(timeout=10)

    def cancel_order(self, trade) -> None:
        """Cancel an open order (pass the Trade object returned by place_order)."""
        if not self._loop:
            raise RuntimeError("Not connected")
        future = asyncio.run_coroutine_threadsafe(
            self._async_cancel_order(trade), self._loop
        )
        future.result(timeout=10)

    # ── Background event loop ─────────────────────────────────────────────────

    def _run_loop(self) -> None:
        asyncio.set_event_loop(self._loop)
        self._loop.run_forever()

    # ── Async helpers ─────────────────────────────────────────────────────────

    async def _async_connect(self) -> None:
        from ib_async import IB  # type: ignore[import]

        self._ib = IB()
        await self._ib.connectAsync(self._host, self._port, clientId=self._client_id)
        self._connected = True
        log.info("IBKR connected | %s:%d | clientId=%d", self._host, self._port, self._client_id)

        for sym in self._symbols:
            await self._subscribe(sym)

        log.info("IBKR subscribed to %d symbol(s)", len(self._symbols))

        # Start background task that copies ticker values into the cache
        self._loop.create_task(self._poll_cache())

    async def _async_disconnect(self) -> None:
        for sym, ticker in list(self._tickers.items()):
            try:
                self._ib.cancelMktData(ticker.contract)
            except Exception:
                pass
        self._ib.disconnect()

    async def _subscribe(self, symbol: str) -> None:
        if symbol in self._tickers:
            return

        from ib_async import Stock  # type: ignore[import]

        contract = Stock(symbol, "SMART", "USD")
        contracts = await self._ib.qualifyContractsAsync(contract)
        if not contracts:
            log.warning("IBKR: could not qualify %s — skipped", symbol)
            return
        contract = contracts[0]

        self._cache[symbol] = Quote(
            symbol=symbol,
            last=0.0, bid=0.0, ask=0.0, volume=0,
            open=0.0, high=0.0, low=0.0, prev_close=0.0,
            timestamp=datetime.now(),
        )

        # No generic ticks — basic OHLCV ticks are always included.
        # Removed 258 (Fundamental Ratios) — requires paid IB subscription.
        ticker = self._ib.reqMktData(contract, genericTickList="", snapshot=False, regulatorySnapshot=False)
        self._tickers[symbol] = ticker
        log.debug("IBKR: subscribed %s (conId=%s)", symbol, contract.conId)

    async def _poll_cache(self) -> None:
        """Copy live ticker values into the Quote cache at _POLL_INTERVAL rate."""
        while self._connected:
            for sym, ticker in list(self._tickers.items()):
                self._refresh_quote(sym, ticker)
            await asyncio.sleep(_POLL_INTERVAL)

    async def _fetch_bars(self, symbol: str, limit: int) -> list[Bar]:
        from ib_async import Stock  # type: ignore[import]

        contract = Stock(symbol, "SMART", "USD")
        days = max(1, limit // 78 + 1)
        ib_bars = await self._ib.reqHistoricalDataAsync(
            contract,
            endDateTime="",
            durationStr=f"{days} D",
            barSizeSetting="5 mins",
            whatToShow="TRADES",
            useRTH=False,
            formatDate=1,
        )
        bars = [
            Bar(
                symbol=symbol,
                open=b.open, high=b.high, low=b.low, close=b.close,
                volume=int(b.volume),
                timestamp=b.date if isinstance(b.date, datetime) else datetime.now(),
            )
            for b in ib_bars
        ]
        return bars[-limit:] if len(bars) > limit else bars

    async def _async_place_order(
        self, symbol, action, quantity, order_type, limit_price, stop_price, tif
    ):
        from ib_async import LimitOrder, MarketOrder, Order, Stock, StopOrder  # type: ignore[import]

        contract = Stock(symbol, "SMART", "USD")
        await self._ib.qualifyContractsAsync(contract)

        match order_type.upper():
            case "LMT":
                order = LimitOrder(action, quantity, limit_price, tif=tif)
            case "STP":
                order = StopOrder(action, quantity, stop_price, tif=tif)
            case _:
                order = MarketOrder(action, quantity, tif=tif)

        trade = self._ib.placeOrder(contract, order)
        log.info("IBKR order placed | %s %s %s x%s", action, order_type, symbol, quantity)
        return trade

    async def _async_cancel_order(self, trade) -> None:
        self._ib.cancelOrder(trade.order)
        log.info("IBKR order cancelled | orderId=%s", trade.order.orderId)

    # ── Cache refresh from ticker ─────────────────────────────────────────────

    def _refresh_quote(self, sym: str, ticker) -> None:
        try:
            last      = _f(ticker.last)
            prev_close = _f(ticker.close)   # tick type 9 = previous day close
            open_     = _f(ticker.open)
            high      = _f(ticker.high)
            low       = _f(ticker.low)
            bid       = _f(ticker.bid)
            ask       = _f(ticker.ask)
            acc_vol   = _i(ticker.volume)   # total day shares
            last_size = _i(ticker.lastSize)

            # Fall back to prev_close until first real trade arrives
            if last <= 0:
                last = prev_close

            # Keep prev_close from cache if IB hasn't sent it yet
            if prev_close <= 0:
                prev_close = self._cache[sym].prev_close

            market_open = open_ > 0
            existing    = self._cache[sym]

            # Snapshot premarket volume on the first bar that carries an open price
            premarket_vol = existing.premarket_volume
            if market_open and premarket_vol == 0 and not existing.market_open:
                premarket_vol = existing.accumulated_volume

            regular_vol = max(0, acc_vol - premarket_vol) if market_open else 0

            self._cache[sym] = replace(
                existing,
                last=last,
                bid=bid,
                ask=ask,
                volume=last_size,
                open=open_,
                high=high,
                low=low,
                prev_close=prev_close,
                accumulated_volume=acc_vol,
                premarket_volume=premarket_vol,
                regular_volume=regular_vol,
                market_open=market_open,
                timestamp=datetime.now(),
            )
        except Exception as exc:
            log.debug("IBKR cache refresh error for %s: %s", sym, exc)

    # ── On-demand subscription ────────────────────────────────────────────────

    def _ensure_subscribed(self, symbol: str) -> None:
        if symbol in self._tickers or not self._loop or not self._loop.is_running():
            return
        future = asyncio.run_coroutine_threadsafe(self._subscribe(symbol), self._loop)
        try:
            future.result(timeout=10)
        except Exception as exc:
            log.warning("IBKR: on-demand subscribe failed for %s: %s", symbol, exc)
