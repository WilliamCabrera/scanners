"""
IBKR order execution via ib_async.

Uses a dedicated IB connection (separate clientId from the data provider).
Bracket orders are native IB brackets: entry → TP (LMT) + SL (STP).
IB handles the exit automatically; we poll trade status to track fills.

Port conventions:
  4002 — IB Gateway paper  |  4001 — IB Gateway live
  7497 — TWS paper         |  7496 — TWS live
"""
import asyncio
import logging
import threading
from datetime import datetime
from typing import Optional

from ..order_base import BracketRef, OrderProvider

log = logging.getLogger(__name__)

_CONNECT_TIMEOUT = 30
_FILLED = {"Filled"}
_DONE   = {"Filled", "Cancelled", "ApiCancelled", "Inactive"}


class IBKROrderProvider(OrderProvider):

    def __init__(
        self,
        host:      str = "127.0.0.1",
        port:      int = 4002,
        client_id: int = 2,   # keep different from data provider (default 1)
    ) -> None:
        self._host      = host
        self._port      = port
        self._client_id = client_id
        self._ib        = None
        self._loop: Optional[asyncio.AbstractEventLoop] = None
        self._thread: Optional[threading.Thread]        = None
        self._connected = False

    # ── OrderProvider interface ───────────────────────────────────────────────

    def connect(self) -> None:
        self._loop = asyncio.new_event_loop()
        self._thread = threading.Thread(
            target=self._run_loop, daemon=True, name="ibkr-orders-loop"
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

    def place_bracket(
        self,
        symbol:      str,
        action:      str,
        quantity:    float,
        entry_type:  str,
        entry_price: float,
        sl:          float,
        tp:          float,
    ) -> BracketRef:
        future = asyncio.run_coroutine_threadsafe(
            self._async_place_bracket(symbol, action, quantity, entry_type, entry_price, sl, tp),
            self._loop,
        )
        return future.result(timeout=10)

    def refresh(self, ref: BracketRef, current_price: float = 0.0) -> BracketRef:
        """Read live trade statuses — no async needed, just attribute reads."""
        trades = ref._internal
        if not trades:
            return ref

        parent_trade, tp_trade, sl_trade = trades
        parent_status = parent_trade.orderStatus.status

        if parent_status not in _FILLED:
            # Entry not yet filled — stay pending; detect cancellation
            if parent_status in _DONE:
                ref.status = "cancelled"
            return ref

        # Entry filled
        ref.fill_price = float(parent_trade.orderStatus.avgFillPrice or ref.entry_price)
        ref.status = "open"

        tp_status = tp_trade.orderStatus.status
        sl_status = sl_trade.orderStatus.status

        if tp_status in _FILLED:
            ref.status     = "tp"
            ref.exit_price = float(tp_trade.orderStatus.avgFillPrice or tp_trade.order.lmtPrice)
        elif sl_status in _FILLED:
            ref.status     = "sl"
            ref.exit_price = float(sl_trade.orderStatus.avgFillPrice or sl_trade.order.auxPrice)

        return ref

    def cancel(self, ref: BracketRef) -> None:
        """Cancel open child orders; if entry already filled place a MKT close."""
        future = asyncio.run_coroutine_threadsafe(
            self._async_cancel(ref), self._loop
        )
        try:
            future.result(timeout=10)
        except Exception as exc:
            log.warning("IBKR cancel error for %s: %s", ref.symbol, exc)

    # ── Background loop ───────────────────────────────────────────────────────

    def _run_loop(self) -> None:
        asyncio.set_event_loop(self._loop)
        self._loop.run_forever()

    # ── Async helpers ─────────────────────────────────────────────────────────

    async def _async_connect(self) -> None:
        from ib_async import IB  # type: ignore[import]
        self._ib = IB()
        await self._ib.connectAsync(self._host, self._port, clientId=self._client_id)
        self._connected = True
        log.info("IBKROrderProvider connected | %s:%d | clientId=%d",
                 self._host, self._port, self._client_id)

    async def _async_disconnect(self) -> None:
        self._ib.disconnect()

    async def _async_place_bracket(
        self,
        symbol:      str,
        action:      str,
        quantity:    float,
        entry_type:  str,
        entry_price: float,
        sl:          float,
        tp:          float,
    ) -> BracketRef:
        from ib_async import LimitOrder, MarketOrder, Stock, StopOrder  # type: ignore[import]

        contract = Stock(symbol, "SMART", "USD")
        await self._ib.qualifyContractsAsync(contract)

        reverse  = "SELL" if action == "BUY" else "BUY"
        entry_id = self._ib.client.getReqId()
        tp_id    = self._ib.client.getReqId()
        sl_id    = self._ib.client.getReqId()

        if entry_type.upper() == "MKT":
            parent = MarketOrder(action, quantity, orderId=entry_id, transmit=False)
        else:
            parent = LimitOrder(action, quantity, entry_price, orderId=entry_id, transmit=False)

        take_profit = LimitOrder(
            reverse, quantity, tp,
            orderId=tp_id, parentId=entry_id, transmit=False,
        )
        stop_loss = StopOrder(
            reverse, quantity, sl,
            orderId=sl_id, parentId=entry_id, transmit=True,
        )

        parent_trade = self._ib.placeOrder(contract, parent)
        tp_trade     = self._ib.placeOrder(contract, take_profit)
        sl_trade     = self._ib.placeOrder(contract, stop_loss)

        log.info("Bracket placed | %s %s %s | entry=%s entry_price=%.4f sl=%.4f tp=%.4f",
                 action, entry_type, symbol, entry_id, entry_price, sl, tp)

        ref = BracketRef(
            symbol=symbol, action=action, quantity=quantity,
            entry_type=entry_type, entry_price=entry_price,
            sl=sl, tp=tp,
            opened_at=datetime.now(),
        )
        ref._internal = (parent_trade, tp_trade, sl_trade)
        return ref

    async def _async_cancel(self, ref: BracketRef) -> None:
        from ib_async import MarketOrder  # type: ignore[import]

        trades = ref._internal
        if not trades:
            return

        parent_trade, tp_trade, sl_trade = trades
        entry_filled = parent_trade.orderStatus.status in _FILLED

        # Cancel unfilled child orders
        for trade in (tp_trade, sl_trade):
            if trade.orderStatus.status not in _DONE:
                self._ib.cancelOrder(trade.order)

        # If entry was filled, close the position at market
        if entry_filled:
            from ib_async import Stock  # type: ignore[import]
            close_action = "SELL" if ref.action == "BUY" else "BUY"
            contract = Stock(ref.symbol, "SMART", "USD")
            await self._ib.qualifyContractsAsync(contract)
            close_order = MarketOrder(close_action, ref.quantity)
            self._ib.placeOrder(contract, close_order)
            log.info("MKT close placed | %s %s x%s", close_action, ref.symbol, ref.quantity)
