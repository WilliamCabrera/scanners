"""
OrderExecutor — bridges signals from CandleCache to an OrderProvider.

Rules:
  - One open position per (strategy, timeframe, symbol) — same instance cannot
    re-enter while a trade is live.
  - Different strategies (or different timeframes of the same strategy) can
    have simultaneous positions on the same symbol.
  - Position size (quantity) is passed from outside — no internal sizing logic.
  - Exits: native bracket orders (TP/SL handled by broker); executor also
    enforces max_hold_hours and monitors fill status.
"""
import logging
import threading
import time
from datetime import datetime
from typing import Callable, Optional
from zoneinfo import ZoneInfo

from .signal_engine import Signal
from ..providers.base import DataProvider
from ..providers.order_base import BracketRef, OrderProvider

log = logging.getLogger(__name__)

_ET = ZoneInfo("America/New_York")


class OrderExecutor:
    """
    Polls `signal_source.get_signals()` and places bracket orders via `order_provider`.
    Tracks open positions and updates Signal.trade_status / exit_price / pnl_pct on close.

    Usage:
        executor = OrderExecutor(
            order_provider=IBKROrderProvider(...),
            signal_source=candle_cache,
            quote_source=massive_provider,   # used for current price in refresh + timeout exit
            quantity=10,
            entry_type="MKT",                # "MKT" | "LMT"
        )
        executor.start()   # non-blocking, runs in background thread
        ...
        executor.stop()
    """

    def __init__(
        self,
        order_provider: OrderProvider,
        signal_source,                        # has get_signals() -> list[Signal]
        quote_source:   DataProvider,
        quantity:       float,
        entry_type:     str   = "MKT",        # "MKT" | "LMT"
        interval:       float = 2.0,          # seconds between poll cycles
        on_fill: Optional[Callable[[Signal, BracketRef], None]] = None,
    ) -> None:
        if entry_type.upper() not in ("MKT", "LMT"):
            raise ValueError(f"entry_type must be 'MKT' or 'LMT', got {entry_type!r}")

        self._order_provider = order_provider
        self._signal_source  = signal_source
        self._quote_source   = quote_source
        self._quantity       = quantity
        self._entry_type     = entry_type.upper()
        self._interval       = interval
        self._on_fill        = on_fill

        # key: (strategy_instance, timeframe, symbol) → (Signal, BracketRef)
        self._open: dict[tuple, tuple[Signal, BracketRef]] = {}
        self._lock   = threading.Lock()
        self._running = False
        self._thread: Optional[threading.Thread] = None

    # ── Public API ────────────────────────────────────────────────────────────

    def start(self) -> None:
        """Start the executor in a background daemon thread."""
        if self._running:
            return
        self._running = True
        self._thread = threading.Thread(
            target=self._run, daemon=True, name="order-executor"
        )
        self._thread.start()
        log.info(
            "OrderExecutor started | provider=%s | qty=%s | entry=%s | interval=%.1fs",
            self._order_provider.__class__.__name__,
            self._quantity, self._entry_type, self._interval,
        )

    def stop(self) -> None:
        self._running = False
        if self._thread:
            self._thread.join(timeout=5)

    @property
    def open_positions(self) -> list[tuple[Signal, BracketRef]]:
        with self._lock:
            return list(self._open.values())

    # ── Internal loop ─────────────────────────────────────────────────────────

    def _run(self) -> None:
        while self._running:
            try:
                self._process_new_signals()
                self._check_open_positions()
            except Exception:
                log.exception("OrderExecutor cycle error")
            time.sleep(self._interval)

    def _position_key(self, sig: Signal) -> tuple:
        return (sig.strategy, sig.timeframe, sig.symbol)

    def _current_price(self, symbol: str) -> float:
        try:
            return self._quote_source.get_quote(symbol).last
        except Exception:
            return 0.0

    # ── New signal processing ─────────────────────────────────────────────────

    def _process_new_signals(self) -> None:
        signals: list[Signal] = self._signal_source.get_signals()

        for sig in signals:
            # Only act on confirmed (bar-close) signals that are still open
            if sig.status != "launched" or sig.trade_status != "open":
                continue

            key = self._position_key(sig)
            with self._lock:
                if key in self._open:
                    continue  # same instance already has an open trade on this symbol

            action = "SELL" if sig.type == "short" else "BUY"
            try:
                ref = self._order_provider.place_bracket(
                    symbol=sig.symbol,
                    action=action,
                    quantity=self._quantity,
                    entry_type=self._entry_type,
                    entry_price=sig.entry_est,
                    sl=sig.sl,
                    tp=sig.tp,
                )
            except Exception as exc:
                log.error("Failed to place bracket for %s %s: %s", sig.symbol, sig.strategy, exc)
                continue

            with self._lock:
                self._open[key] = (sig, ref)

            log.info(
                "Order placed | %s %s %s | strategy=%s tf=%s | entry=%.4f sl=%.4f tp=%.4f",
                action, self._entry_type, sig.symbol, sig.strategy, sig.timeframe,
                sig.entry_est, sig.sl, sig.tp,
            )

    # ── Open position tracking ────────────────────────────────────────────────

    def _check_open_positions(self) -> None:
        with self._lock:
            items = list(self._open.items())

        for key, (sig, ref) in items:
            price = self._current_price(sig.symbol)

            # Enforce max_hold_hours timeout
            if sig.max_hold_hours > 0 and ref.status == "open":
                elapsed_h = (datetime.now(_ET) - sig.triggered_at).total_seconds() / 3600
                if elapsed_h >= sig.max_hold_hours:
                    log.info("Max hold reached | %s %s — closing", sig.symbol, sig.strategy)
                    self._order_provider.cancel(ref)
                    self._close(key, sig, ref, trade_status="eod", exit_price=price)
                    continue

            # Ask provider for latest status
            ref = self._order_provider.refresh(ref, current_price=price)

            if ref.status in ("tp", "sl"):
                self._close(key, sig, ref, trade_status=ref.status, exit_price=ref.exit_price)
            elif ref.status == "cancelled":
                # Entry was cancelled (e.g. LMT never filled) — release the slot
                with self._lock:
                    self._open.pop(key, None)
                log.info("Order cancelled (never filled) | %s %s", sig.symbol, sig.strategy)

    def _close(
        self,
        key:          tuple,
        sig:          Signal,
        ref:          BracketRef,
        trade_status: str,
        exit_price:   float,
    ) -> None:
        sig.trade_status = trade_status
        sig.exit_price   = exit_price
        sig.closed_at    = datetime.now(_ET)

        if sig.entry_est > 0 and exit_price > 0:
            if sig.type == "short":
                sig.pnl_pct = (sig.entry_est - exit_price) / sig.entry_est * 100
            else:
                sig.pnl_pct = (exit_price - sig.entry_est) / sig.entry_est * 100

        with self._lock:
            self._open.pop(key, None)

        log.info(
            "Position closed | %s %s | status=%s | exit=%.4f | pnl=%.2f%%",
            sig.symbol, sig.strategy, trade_status, exit_price, sig.pnl_pct,
        )

        if self._on_fill:
            try:
                self._on_fill(sig, ref)
            except Exception:
                log.exception("on_fill callback error")
