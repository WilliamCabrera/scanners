"""
CandleCache — maintains live 5m/15m candle DataFrames with indicators for
stocks with |change_pct| >= 40%.

Life-cycle
──────────
1. Bootstrap: when a symbol first qualifies, fetch today's full history via REST
   and build the initial DataFrames.
2. Stream: every accepted WebSocket bar fires on_raw_bar().  Raw bars (seconds
   or minutes, depending on the provider setting) are aggregated into 5m/15m bars.
   When the current bar period closes (a bar from the next period arrives), the
   completed bar is appended and indicators are recomputed.
3. Signals: after each bar close, the SignalEngine evaluates the last 2 rows and
   emits Signal objects into a bounded deque (today's signal log).

DataFrame columns per timeframe:
    time (UTC seconds), open, high, low, close, volume, sma_9, sma_200, vwap
"""
from __future__ import annotations

import dataclasses
import json
import logging
import threading
import time
import urllib.request
from collections import deque
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Callable, Optional
from zoneinfo import ZoneInfo

import pandas as pd

from .indicators import compute_sma, compute_vwap
from .models import Quote, ScanResult
from .signal_engine import Signal, SignalEngine

log = logging.getLogger(__name__)

# Scanner project root → .cache/ sits alongside dashboard.py
_CACHE_DIR = Path(__file__).parent.parent.parent / ".cache"

_AGGS_URL = (
    "https://api.massive.com/v2/aggs/ticker/{sym}/range/{mult}/{span}/{frm}/{to}"
    "?adjusted=false&sort=asc&limit=50000&apiKey={key}"
)
_ET = ZoneInfo("America/New_York")

CHANGE_THRESHOLD = 40.0  # abs(change_pct) to qualify

# (bar_mult, bar_span, lookback_cal_days, bar_duration_seconds)
_TIMEFRAMES: dict[str, tuple[int, str, int, int]] = {
    "5m":  (5,  "minute", 5,  300),
    "15m": (15, "minute", 14, 900),
}

_OHLCV = ["time", "open", "high", "low", "close", "volume"]


class CandleCache:
    """Thread-safe store of 5m/15m DataFrames with live indicators and signal log."""

    def __init__(
        self,
        api_key: str,
        get_results_fn: Callable[[], dict[str, list[ScanResult]]],
    ) -> None:
        self._api_key     = api_key
        self._get_results = get_results_fn
        self._lock        = threading.Lock()

        # Historical bars (per symbol/tf) — fetched once at bootstrap
        self._hist:          dict[str, dict[str, pd.DataFrame]] = {}
        # Bars closed by the stream today
        self._closed_today:  dict[str, dict[str, pd.DataFrame]] = {}
        # In-progress bar being accumulated from the stream
        self._pending:       dict[str, dict[str, dict]]         = {}
        # Symbols currently being bootstrapped
        self._bootstrapping: set[str]                           = set()
        # Final merged DataFrames with indicators
        self._cache:         dict[str, dict[str, pd.DataFrame]] = {}

        # Intraday running state (updated on every 5m bar close, reset at day boundary)
        self._day_date: dict[str, str]   = {}   # sym → "YYYY-MM-DD"
        self._day_low:  dict[str, float] = {}
        self._day_hod:  dict[str, float] = {}

        # Signal engine
        self._signal_engine = SignalEngine()
        # Confirmed signals (bar closed with condition still met) — most recent first
        self._signal_log: deque[Signal] = deque(maxlen=500)
        # Live mid-bar signals per (sym, tf) — overwritten every tick, cleared on bar close
        self._pending_signals: dict[str, dict[str, list[Signal]]] = {}
        # Dedup keys for launched signals: (symbol, strategy, timeframe, triggered_at)
        self._launched_keys: set[tuple] = set()
        # Open trades keyed by (symbol, strategy, timeframe) — closed when TP/SL/EOD hit
        self._open_trades: dict[tuple, Signal] = {}
        # Parquet path for today's signals (date-scoped so stale files are ignored)
        self._signals_path = _CACHE_DIR / f"signals_{date.today().isoformat()}.parquet"
        self._load_parquet_signals()

        threading.Thread(
            target=self._eod_scheduler, daemon=True, name="eod-scheduler",
        ).start()
        threading.Thread(
            target=self._periodic_save, daemon=True, name="sig-periodic-save",
        ).start()

    # ── Public API ───────────────────────────────────────────────────────────

    def get(self, symbol: str) -> Optional[dict[str, pd.DataFrame]]:
        with self._lock:
            return self._cache.get(symbol)

    def get_all(self) -> dict[str, dict[str, pd.DataFrame]]:
        with self._lock:
            return dict(self._cache)

    def symbols(self) -> list[str]:
        with self._lock:
            return list(self._cache.keys())

    def get_signals(self) -> list[Signal]:
        """Return launched signals as frozen copies so the dashboard snapshot is stable."""
        with self._lock:
            return [dataclasses.replace(s) for s in self._signal_log]

    def get_pending_signals(self) -> list[Signal]:
        """Return pending signals as frozen copies."""
        with self._lock:
            result: list[Signal] = []
            for sym_tfs in self._pending_signals.values():
                for sigs in sym_tfs.values():
                    result.extend(dataclasses.replace(s) for s in sigs)
            return result

    # ── Stream entry point ────────────────────────────────────────────────────

    def on_raw_bar(
        self,
        sym: str,
        open_: float,
        high: float,
        low: float,
        close: float,
        volume: float,
        end_ms: int,
    ) -> None:
        """Accumulate a raw WebSocket bar into 5m/15m candles.

        Called from the provider WebSocket thread — heavy work is offloaded to threads.
        """
        # Get live quote once (outside lock) — used for qualifying check and signal context
        quote = self._find_quote(sym)
        if quote is None or abs(quote.change_pct) < CHANGE_THRESHOLD:
            return

        # Bootstrap on first encounter
        with self._lock:
            needs_bootstrap = sym not in self._hist and sym not in self._bootstrapping
            if needs_bootstrap:
                self._bootstrapping.add(sym)
        if needs_bootstrap:
            threading.Thread(
                target=self._bootstrap, args=(sym,),
                daemon=True, name=f"candle-bootstrap-{sym}",
            ).start()
            return

        end_sec      = end_ms // 1000
        prev_close   = quote.prev_close
        float_shares = quote.float_shares
        new_signals: list[Signal] = []

        for tf, (_, _, _, bar_seconds) in _TIMEFRAMES.items():
            # Subtract 1 before flooring so the bar that ends exactly on the boundary
            # (e.g. 9:05:00) is assigned to the period it CLOSES (9:00), not opens (9:05).
            bar_start   = ((end_sec - 1) // bar_seconds) * bar_seconds
            just_closed = False

            with self._lock:
                sym_pending = self._pending.setdefault(sym, {})
                cur = sym_pending.get(tf)

                if cur is None or cur["time"] != bar_start:
                    if cur is not None:
                        just_closed = True
                        self._close_bar_locked(sym, tf, cur)
                    sym_pending[tf] = {
                        "time": bar_start,
                        "open": open_, "high": high, "low": low,
                        "close": close, "volume": volume,
                    }
                else:
                    cur["high"]   = max(cur["high"], high)
                    cur["low"]    = min(cur["low"], low)
                    cur["close"]  = close
                    cur["volume"] += volume

                self._rebuild_locked(sym, tf)

                df = self._cache.get(sym, {}).get(tf)
                if df is not None and len(df) >= 3:
                    day_low = self._day_low.get(sym, 0.0)
                    day_hod = self._day_hod.get(sym, 0.0)

                    if just_closed:
                        # Bar is now closed — evaluate the closed bar (live=False → "launched")
                        launched = self._signal_engine.evaluate(
                            sym, tf, df,
                            prev_close=prev_close,
                            day_low=day_low, day_hod=day_hod,
                            float_shares=float_shares,
                            live=False,
                        )
                        new_signals.extend(launched)
                        # Reset pending for this tf — new bar just started, no signal yet
                        self._pending_signals.setdefault(sym, {})[tf] = []
                    else:
                        # Bar in progress — evaluate live bar ("pending"); overwrite each tick
                        self._pending_signals.setdefault(sym, {})[tf] = (
                            self._signal_engine.evaluate(
                                sym, tf, df,
                                prev_close=prev_close,
                                day_low=day_low, day_hod=day_hod,
                                float_shares=float_shares,
                                live=True,
                            )
                        )

        if new_signals:
            with self._lock:
                added = self._append_launched_locked(new_signals)
            for sig in added:
                log.info(
                    "SIGNAL LAUNCHED %s %s %s @ %.4f  SL=%.4f  TP=%.4f  R:R=%.1f",
                    sig.symbol, sig.strategy, sig.timeframe,
                    sig.signal_close, sig.sl, sig.tp, sig.rr,
                )
            if added:
                self._save_parquet_signals()

        # Check every open trade for this symbol against the current tick's high/low
        with self._lock:
            trade_closed = self._check_trades_locked(sym, high, low, close, end_ms // 1000)
        if trade_closed:
            self._save_parquet_signals()

    # ── Bootstrap ─────────────────────────────────────────────────────────────

    def _bootstrap(self, sym: str) -> None:
        log.info("CandleCache: bootstrapping %s …", sym)
        today    = date.today()
        now_ms   = int(datetime.now(tz=_ET).timestamp() * 1000)

        hist_frames: dict[str, pd.DataFrame] = {}
        for tf, (mult, span, lookback_days, _) in _TIMEFRAMES.items():
            from_ms = int(
                datetime(
                    (today - timedelta(days=lookback_days)).year,
                    (today - timedelta(days=lookback_days)).month,
                    (today - timedelta(days=lookback_days)).day,
                    tzinfo=_ET,
                ).timestamp() * 1000
            )
            df = self._fetch_ms(sym, mult, span, from_ms, now_ms)
            hist_frames[tf] = df if df is not None else _empty_df()
            log.info("CandleCache: %s %s — %d bars from REST", sym, tf, len(hist_frames[tf]))

        with self._lock:
            self._hist[sym] = hist_frames
            self._closed_today.setdefault(sym, {tf: _empty_df() for tf in _TIMEFRAMES})
            self._bootstrapping.discard(sym)
            for tf in _TIMEFRAMES:
                self._rebuild_locked(sym, tf)
            # Seed day_low / day_hod from today's bars in the bootstrap data
            self._seed_intraday_state_locked(sym)

        # Replay today's closed bars through the signal engine to recover missed signals.
        # Runs outside the lock (pure computation); dedup prevents double-logging.
        self._backfill_signals(sym)

    def _seed_intraday_state_locked(self, sym: str) -> None:
        """Compute day_low / day_hod from today's bars already in the DataFrame."""
        today_str = date.today().isoformat()
        df_5m = self._cache.get(sym, {}).get("5m")
        if df_5m is None or df_5m.empty:
            return
        today_bars = df_5m[
            pd.to_datetime(df_5m["time"], unit="s", utc=True)
            .dt.tz_convert(_ET)
            .dt.date
            == date.today()
        ]
        if not today_bars.empty:
            self._day_date[sym] = today_str
            self._day_low[sym]  = float(today_bars["low"].min())
            self._day_hod[sym]  = float(today_bars["high"].max())

    # ── EOD scheduler ────────────────────────────────────────────────────────

    def _periodic_save(self) -> None:
        """Daemon thread: persist signals to parquet every 60 seconds."""
        while True:
            time.sleep(60)
            self._save_parquet_signals()

    def _eod_scheduler(self) -> None:
        """Daemon thread: waits until 16:00 ET each day, then closes all open trades
        using the last 5m bar before 16:00 from the cache.
        Also runs immediately on startup if it's already past 16:00."""
        # Run immediately on startup if market is already closed
        if datetime.now(tz=_ET).hour >= 16:
            time.sleep(5)  # brief delay so bootstrap/cache has time to populate
            self._close_all_open_eod()

        while True:
            now_et = datetime.now(tz=_ET)
            target = now_et.replace(hour=16, minute=0, second=5, microsecond=0)
            if now_et >= target:
                target += timedelta(days=1)
            time.sleep((target - now_et).total_seconds())
            log.info("EOD scheduler fired — closing all open trades")
            self._close_all_open_eod()

    def _close_all_open_eod(self) -> None:
        """Close every open trade using the last cached 5m bar before 16:00 ET."""
        today_et  = date.today()
        closed_any = False

        with self._lock:
            symbols = list({k[0] for k in self._open_trades})

        for sym in symbols:
            with self._lock:
                df_5m = self._cache.get(sym, {}).get("5m")
            if df_5m is None or df_5m.empty:
                continue

            dates_et   = pd.to_datetime(df_5m["time"], unit="s", utc=True).dt.tz_convert(_ET)
            valid_mask = (dates_et.dt.date == today_et) & (dates_et.dt.hour < 16)
            valid      = df_5m[valid_mask]
            if valid.empty:
                continue

            last_bar   = valid.iloc[-1]
            last_close = float(last_bar["close"])
            last_dt    = datetime.fromtimestamp(int(last_bar["time"]), tz=_ET)

            with self._lock:
                to_close = [
                    (k, t) for k, t in self._open_trades.items()
                    if k[0] == sym and t.triggered_at.date() == today_et
                ]
                for tk, trade in to_close:
                    trade.trade_status = "other"
                    trade.exit_price   = last_close
                    trade.closed_at    = last_dt
                    trade.pnl_pct      = _calc_pnl(trade, last_close)
                    del self._open_trades[tk]
                    log.info("EOD closed %s/%s/%s  exit=%.4f  pnl=%.2f%%",
                             tk[0], tk[1], tk[2], last_close, trade.pnl_pct)

            if to_close:
                closed_any = True

        if closed_any:
            self._save_parquet_signals()

    # ── Parquet persistence ───────────────────────────────────────────────────

    def _load_parquet_signals(self) -> None:
        """Load today's signal parquet into _signal_log on startup (if it exists)."""
        if not self._signals_path.exists():
            return
        try:
            df = pd.read_parquet(self._signals_path)
            sigs: list[Signal] = []
            for _, row in df.iterrows():
                closed_at_raw = row.get("closed_at")
                closed_at = (
                    pd.Timestamp(closed_at_raw).to_pydatetime()
                    if closed_at_raw is not None and pd.notna(closed_at_raw)
                    else None
                )
                sig = Signal(
                    symbol=str(row["symbol"]),
                    strategy=str(row["strategy"]),
                    timeframe=str(row["timeframe"]),
                    type=str(row["type"]),
                    triggered_at=pd.Timestamp(row["triggered_at"]).to_pydatetime(),
                    signal_close=float(row["signal_close"]),
                    entry_est=float(row["entry_est"]),
                    sl=float(row["sl"]),
                    tp=float(row["tp"]),
                    status="launched",
                    trade_status=str(row.get("trade_status", "open")),
                    exit_price=float(row.get("exit_price", 0.0)),
                    closed_at=closed_at,
                    pnl_pct=float(row.get("pnl_pct", 0.0)),
                    max_hold_hours=float(row.get("max_hold_hours", 0.0)),
                )
                key = (sig.symbol, sig.strategy, sig.timeframe, sig.triggered_at)
                if key not in self._launched_keys:
                    self._launched_keys.add(key)
                    sigs.append(sig)
                    if sig.trade_status == "open":
                        self._open_trades[(sig.symbol, sig.strategy, sig.timeframe)] = sig
            sigs.sort(key=lambda s: s.triggered_at, reverse=True)
            self._signal_log.extend(sigs)
            log.info(
                "Loaded %d signals from %s (%d open trades)",
                len(sigs), self._signals_path.name, len(self._open_trades),
            )
        except Exception as exc:
            log.warning("Failed to load signals parquet: %s", exc)

    def _save_parquet_signals(self) -> None:
        """Async snapshot of _signal_log → parquet. Non-blocking."""
        def _write(sigs: list[Signal]) -> None:
            try:
                rows = [
                    {
                        "symbol":       s.symbol,
                        "strategy":     s.strategy,
                        "timeframe":    s.timeframe,
                        "type":         s.type,
                        "triggered_at": s.triggered_at,
                        "signal_close": s.signal_close,
                        "entry_est":    s.entry_est,
                        "sl":           s.sl,
                        "tp":           s.tp,
                        "status":       s.status,
                        "trade_status": s.trade_status,
                        "exit_price":   s.exit_price,
                        "closed_at":    s.closed_at,
                        "pnl_pct":        s.pnl_pct,
                        "max_hold_hours": s.max_hold_hours,
                    }
                    for s in sigs
                ]
                _CACHE_DIR.mkdir(parents=True, exist_ok=True)
                pd.DataFrame(rows).to_parquet(self._signals_path, index=False)
            except Exception as exc:
                log.warning("Failed to save signals parquet: %s", exc)

        with self._lock:
            snapshot = list(self._signal_log)
        threading.Thread(target=_write, args=(snapshot,), daemon=True, name="sig-parquet-save").start()

    def _append_launched_locked(self, sigs: list[Signal]) -> list[Signal]:
        """Append signals to _signal_log with dedup and open-trade blocking.
        Returns only newly added signals. Must be called under self._lock."""
        added: list[Signal] = []
        for sig in sigs:
            trade_key = (sig.symbol, sig.strategy, sig.timeframe)
            key       = (sig.symbol, sig.strategy, sig.timeframe, sig.triggered_at)
            # Block a new OPEN signal if a trade is already open for (sym, strategy, tf)
            if sig.trade_status == "open" and trade_key in self._open_trades:
                log.debug(
                    "Skip signal %s/%s/%s — trade open since %s",
                    sig.symbol, sig.strategy, sig.timeframe,
                    self._open_trades[trade_key].triggered_at.strftime("%H:%M"),
                )
                continue
            if key not in self._launched_keys:
                self._launched_keys.add(key)
                self._signal_log.appendleft(sig)
                if sig.trade_status == "open":
                    self._open_trades[trade_key] = sig
                added.append(sig)
        return added

    # ── Backfill ──────────────────────────────────────────────────────────────

    def _backfill_signals(self, sym: str) -> None:
        """Replay today's bars bar-by-bar to recover missed signals and simulate trade outcomes.

        Runs outside self._lock (pure computation).
        - Blocks duplicate signals: no new signal for (sym, strategy, tf) while a sim trade is open.
        - Simulates TP/SL/EOD outcomes so backfilled trades are not stuck as "open".
        """
        quote        = self._find_quote(sym)
        prev_close   = quote.prev_close   if quote and quote.prev_close   else 0.0
        float_shares = quote.float_shares if quote and quote.float_shares else 0.0
        if prev_close <= 0:
            log.debug("Backfill skipped for %s — no prev_close available", sym)
            return

        today_et  = date.today()
        backfilled: list[Signal] = []

        for tf, (_, _, _, _bar_seconds) in _TIMEFRAMES.items():
            with self._lock:
                df_full = self._cache.get(sym, {}).get(tf)
            if df_full is None or df_full.empty:
                continue

            dates_et = (
                pd.to_datetime(df_full["time"], unit="s", utc=True)
                .dt.tz_convert(_ET).dt.date
            )
            today_df = df_full[dates_et == today_et].reset_index(drop=True)
            if len(today_df) < 3:
                continue

            running_low = float(today_df["low"].iloc[0])
            running_hod = float(today_df["high"].iloc[0])

            # sim_open: (sym, strategy, tf) → Signal  — open trades within this sim run
            sim_open: dict[tuple, Signal] = {}

            for i in range(2, len(today_df)):
                curr_bar = today_df.iloc[i]
                bar_dt   = datetime.fromtimestamp(int(curr_bar["time"]), tz=_ET)
                is_eod   = bar_dt.hour >= 16

                # 1. Check existing sim trades against bar i (the bar after the signal bar)
                for tk, trade in list(sim_open.items()):
                    held_hours = (bar_dt - trade.triggered_at).total_seconds() / 3600
                    max_hold   = trade.max_hold_hours
                    if is_eod:
                        trade.trade_status = "other"
                        trade.exit_price   = float(curr_bar["close"])
                        trade.closed_at    = bar_dt
                        trade.pnl_pct      = _calc_pnl(trade, float(curr_bar["close"]))
                        del sim_open[tk]
                    elif max_hold > 0 and held_hours >= max_hold:
                        trade.trade_status = "timeout"
                        trade.exit_price   = float(curr_bar["close"])
                        trade.closed_at    = bar_dt
                        trade.pnl_pct      = _calc_pnl(trade, float(curr_bar["close"]))
                        del sim_open[tk]
                    elif trade.type == "short":
                        if curr_bar["high"] >= trade.sl:
                            trade.trade_status = "sl"
                            trade.exit_price   = trade.sl
                            trade.closed_at    = bar_dt
                            trade.pnl_pct      = _calc_pnl(trade, trade.sl)
                            del sim_open[tk]
                        elif curr_bar["low"] <= trade.tp:
                            trade.trade_status = "tp"
                            trade.exit_price   = trade.tp
                            trade.closed_at    = bar_dt
                            trade.pnl_pct      = _calc_pnl(trade, trade.tp)
                            del sim_open[tk]
                        else:
                            trade.pnl_pct = _calc_pnl(trade, float(curr_bar["close"]))
                    else:  # long
                        if curr_bar["low"] <= trade.sl:
                            trade.trade_status = "sl"
                            trade.exit_price   = trade.sl
                            trade.closed_at    = bar_dt
                            trade.pnl_pct      = _calc_pnl(trade, trade.sl)
                            del sim_open[tk]
                        elif curr_bar["high"] >= trade.tp:
                            trade.trade_status = "tp"
                            trade.exit_price   = trade.tp
                            trade.closed_at    = bar_dt
                            trade.pnl_pct      = _calc_pnl(trade, trade.tp)
                            del sim_open[tk]
                        else:
                            trade.pnl_pct = _calc_pnl(trade, float(curr_bar["close"]))

                if is_eod:
                    continue

                # 2. Update running stats from the bar that just closed (bar i-1)
                prev_bar    = today_df.iloc[i - 1]
                running_low = min(running_low, float(prev_bar["low"]))
                running_hod = max(running_hod, float(prev_bar["high"]))

                # 3. Evaluate bar i-1 as just-closed signal candidate
                sub_df = today_df.iloc[: i + 1]
                sigs   = self._signal_engine.evaluate(
                    sym, tf, sub_df,
                    prev_close=prev_close,
                    day_low=running_low, day_hod=running_hod,
                    float_shares=float_shares,
                    live=False,
                )
                for sig in sigs:
                    trade_key = (sig.symbol, sig.strategy, sig.timeframe)
                    key       = (sig.symbol, sig.strategy, sig.timeframe, sig.triggered_at)
                    # Skip if sim already has an open trade for this strategy
                    if trade_key in sim_open:
                        continue
                    # Skip if already in the global log (loaded from parquet)
                    with self._lock:
                        already = key in self._launched_keys
                    if already:
                        continue
                    backfilled.append(sig)
                    sim_open[trade_key] = sig

            # Force-close any trades still open after the last bar (EOD)
            # The last bar starts at 15:55 (hour=15) so is_eod never fires in the loop.
            if sim_open:
                last_bar = today_df.iloc[-1]
                last_dt  = datetime.fromtimestamp(int(last_bar["time"]), tz=_ET)
                for trade in sim_open.values():
                    trade.trade_status = "other"
                    trade.exit_price   = float(last_bar["close"])
                    trade.closed_at    = last_dt
                    trade.pnl_pct      = _calc_pnl(trade, float(last_bar["close"]))
                sim_open.clear()

        if not backfilled:
            return

        backfilled.sort(key=lambda s: s.triggered_at)
        with self._lock:
            added = self._append_launched_locked(backfilled)
            if added:
                sorted_sigs = sorted(self._signal_log, key=lambda s: s.triggered_at, reverse=True)
                self._signal_log.clear()
                self._signal_log.extend(sorted_sigs)

        if added:
            log.info("Backfilled %d new signals for %s", len(added), sym)
            self._save_parquet_signals()

    # ── Trade tracking (called under self._lock) ─────────────────────────────

    def _check_trades_locked(
        self, sym: str, high: float, low: float, close: float, end_sec: int
    ) -> bool:
        """Update all open trades for sym against the current raw bar tick.
        SL checked before TP (conservative). Returns True if any trade was closed."""
        bar_dt    = datetime.fromtimestamp(end_sec, tz=_ET)
        is_eod    = bar_dt.hour >= 16
        to_close: list[tuple] = []

        for trade_key, trade in self._open_trades.items():
            if trade_key[0] != sym:
                continue

            held_hours = (bar_dt - trade.triggered_at).total_seconds() / 3600
            max_hold   = trade.max_hold_hours

            if is_eod:
                trade.trade_status = "other"
                trade.exit_price   = close
                trade.closed_at    = bar_dt
                trade.pnl_pct      = _calc_pnl(trade, close)
                to_close.append(trade_key)
            elif max_hold > 0 and held_hours >= max_hold:
                trade.trade_status = "timeout"
                trade.exit_price   = close
                trade.closed_at    = bar_dt
                trade.pnl_pct      = _calc_pnl(trade, close)
                to_close.append(trade_key)

            elif trade.type == "short":
                if high >= trade.sl:
                    trade.trade_status = "sl"
                    trade.exit_price   = trade.sl
                    trade.closed_at    = bar_dt
                    trade.pnl_pct      = _calc_pnl(trade, trade.sl)
                    to_close.append(trade_key)
                elif low <= trade.tp:
                    trade.trade_status = "tp"
                    trade.exit_price   = trade.tp
                    trade.closed_at    = bar_dt
                    trade.pnl_pct      = _calc_pnl(trade, trade.tp)
                    to_close.append(trade_key)
                else:
                    trade.pnl_pct = _calc_pnl(trade, close)

            else:  # long
                if low <= trade.sl:
                    trade.trade_status = "sl"
                    trade.exit_price   = trade.sl
                    trade.closed_at    = bar_dt
                    trade.pnl_pct      = _calc_pnl(trade, trade.sl)
                    to_close.append(trade_key)
                elif high >= trade.tp:
                    trade.trade_status = "tp"
                    trade.exit_price   = trade.tp
                    trade.closed_at    = bar_dt
                    trade.pnl_pct      = _calc_pnl(trade, trade.tp)
                    to_close.append(trade_key)
                else:
                    trade.pnl_pct = _calc_pnl(trade, close)

        for k in to_close:
            log.info(
                "TRADE CLOSED %s/%s/%s  status=%s  exit=%.4f  pnl=%.2f%%",
                k[0], k[1], k[2],
                self._open_trades[k].trade_status,
                self._open_trades[k].exit_price,
                self._open_trades[k].pnl_pct,
            )
            del self._open_trades[k]

        return bool(to_close)

    # ── Internal helpers (called under self._lock) ────────────────────────────

    def _close_bar_locked(self, sym: str, tf: str, bar: dict) -> None:
        new_row  = pd.DataFrame([bar])[_OHLCV]
        existing = self._closed_today.get(sym, {}).get(tf, _empty_df())
        self._closed_today.setdefault(sym, {})[tf] = (
            pd.concat([existing, new_row], ignore_index=True)
            if not existing.empty
            else new_row.reset_index(drop=True)
        )
        # Update intraday running state using the primary (5m) timeframe
        if tf == "5m":
            bar_date = datetime.fromtimestamp(int(bar["time"]), tz=_ET).strftime("%Y-%m-%d")
            if self._day_date.get(sym) != bar_date:
                # New trading day
                self._day_date[sym] = bar_date
                self._day_low[sym]  = float(bar["low"])
                self._day_hod[sym]  = float(bar["high"])
            else:
                self._day_low[sym] = min(self._day_low.get(sym, bar["low"]),  float(bar["low"]))
                self._day_hod[sym] = max(self._day_hod.get(sym, bar["high"]), float(bar["high"]))

        log.debug("CandleCache: closed %s %s bar @ %d close=%.4f", sym, tf, bar["time"], bar["close"])

    def _rebuild_locked(self, sym: str, tf: str) -> None:
        hist   = self._hist.get(sym, {}).get(tf, _empty_df())
        closed = self._closed_today.get(sym, {}).get(tf, _empty_df())
        pend   = self._pending.get(sym, {}).get(tf)

        parts = [p for p in [hist, closed] if not p.empty]
        if pend:
            parts.append(pd.DataFrame([pend])[_OHLCV])
        if not parts:
            return

        full = pd.concat(parts, ignore_index=True)
        full = full.drop_duplicates(subset="time", keep="last").reset_index(drop=True)
        full["sma_9"]   = compute_sma(full, 9)
        full["sma_200"] = compute_sma(full, 200)
        full["vwap"]    = compute_vwap(full)
        self._cache.setdefault(sym, {})[tf] = full

    # ── Helpers ───────────────────────────────────────────────────────────────

    def _find_quote(self, sym: str) -> Optional[Quote]:
        try:
            for r in self._get_results().get("stocks", []):
                if r.quote.symbol == sym:
                    return r.quote
        except Exception:
            pass
        return None

    # ── REST fetch ────────────────────────────────────────────────────────────

    def _fetch_ms(self, sym: str, mult: int, span: str, frm_ms: int, to_ms: int) -> Optional[pd.DataFrame]:
        url = _AGGS_URL.format(sym=sym, mult=mult, span=span, frm=frm_ms, to=to_ms, key=self._api_key)
        try:
            with urllib.request.urlopen(url, timeout=20) as resp:
                data = json.loads(resp.read().decode())
            bars = data.get("results", [])
            if not bars:
                return None
            df = pd.DataFrame(bars)
            df.rename(columns={"o": "open", "c": "close", "h": "high",
                                "l": "low",  "v": "volume", "t": "time"}, inplace=True)
            df["time"] = df["time"] // 1000
            return df[_OHLCV].reset_index(drop=True)
        except Exception as exc:
            log.warning("CandleCache: REST fetch failed %s (%dx%s): %s", sym, mult, span, exc)
            return None


def _calc_pnl(trade: Signal, price: float) -> float:
    if trade.entry_est <= 0:
        return 0.0
    if trade.type == "short":
        return (trade.entry_est - price) / trade.entry_est * 100
    return (price - trade.entry_est) / trade.entry_est * 100


def _empty_df() -> pd.DataFrame:
    return pd.DataFrame(columns=_OHLCV)
