"""
SignalEngine — evaluates strategy instances from strategies.json against the last 2 closed bars.

Each entry in strategies.json is one independent instance (one row per params set from the
backtester registry).  Signal.strategy stores the instance_name (out_put_name) so trade
blocking operates per-instance: two instances of the same base strategy can have
independent open trades simultaneously.

Indicators available in every row: open, high, low, close, volume, sma_9, sma_200, vwap
"""
from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Optional
from zoneinfo import ZoneInfo

import pandas as pd

log = logging.getLogger(__name__)

_ET = ZoneInfo("America/New_York")
_STRATEGIES_JSON = Path(__file__).parent / "strategies.json"

# Maps registry strategy_name → SignalEngine method name
_STRATEGY_MAP: dict[str, str] = {
    "backside_short_lower_low_fix_stop_iterative": "_backside_lower_low_fix",
    "backside_short_lower_low_hod_stop_iterative": "_backside_lower_low_hod",
    "gap_crap_iterative":                          "_gap_crap",
    "short_push_exhaustion_iterative":             "_push_exhaustion_fix",
    "short_push_exhaustion_hod_stop_iterative":    "_push_exhaustion_hod",
    "push_rejection_iterative":                    "_push_rejection_fix",
    "push_rejection_hod_stop_iterative":           "_push_rejection_hod",
    "sma9_momentum_long_iterative":                "_sma9_momentum_long",
    "sma9_momentum_long_dynrr_iterative":          "_sma9_momentum_dynrr",
    "sma9_momentum_long_bar_sl_iterative":         "_sma9_momentum_long_bar_sl",
}


@dataclass
class Signal:
    symbol:       str
    strategy:     str           # instance_name (out_put_name from registry)
    timeframe:    str
    type:         str           # "long" | "short"
    triggered_at: datetime
    signal_close: float
    entry_est:    float         # expected entry ≈ open of next bar
    sl:           float
    tp:           float
    status:       str = "launched"   # "pending" | "launched"
    trade_status: str = "open"       # "open" | "tp" | "sl" | "eod"
    exit_price:   float = 0.0
    closed_at:    Optional[datetime] = field(default=None)
    pnl_pct:      float = 0.0        # unrealized if open; realized if closed (%)
    max_hold_hours: float = 0.0      # 0 = no time limit; >0 = force-close after N hours

    @property
    def rr(self) -> float:
        if self.tp <= 0 and self.type == "short":
            return 0.0
        risk   = abs(self.entry_est - self.sl)
        reward = abs(self.tp - self.entry_est)
        return round(reward / risk, 2) if risk > 1e-8 else 0.0


class SignalEngine:
    """
    Loads strategy instances from strategies.json and evaluates them bar by bar.
    Each instance runs independently — different param sets of the same base strategy
    can generate simultaneous signals for the same symbol.
    """

    def __init__(self, strategies_path: Path = _STRATEGIES_JSON) -> None:
        instances = json.loads(strategies_path.read_text())
        # list of (method, params_dict, instance_name)
        self._strategies: list[tuple] = []
        for inst in instances:
            method_name = _STRATEGY_MAP.get(inst["strategy_name"])
            if method_name is None:
                log.warning("strategies.json: unknown strategy_name %r — skipped", inst["strategy_name"])
                continue
            self._strategies.append((
                getattr(self, method_name),
                inst["params"],
                inst["instance_name"],
            ))
        log.info("SignalEngine: %d instances loaded from %s", len(self._strategies), strategies_path.name)

    # ── Public API ────────────────────────────────────────────────────────────

    def evaluate(
        self,
        symbol:       str,
        timeframe:    str,
        df:           pd.DataFrame,
        prev_close:   float,
        day_low:      float,
        day_hod:      float,
        float_shares: float = 0.0,
        live:         bool = False,
    ) -> list[Signal]:
        """
        live=False: evaluate the just-CLOSED bar (curr=iloc[-2], prev=iloc[-3]) → "launched"
        live=True:  evaluate the IN-PROGRESS bar  (curr=iloc[-1], prev=iloc[-2]) → "pending"
        Requires at least 3 rows.
        """
        if len(df) < 3:
            return []

        curr = df.iloc[-1] if live else df.iloc[-2]
        prev = df.iloc[-2] if live else df.iloc[-3]

        bar_seconds = 300 if timeframe == "5m" else 900
        no_gap = (int(curr["time"]) - int(prev["time"])) == bar_seconds

        bar_et = datetime.fromtimestamp(int(curr["time"]), tz=_ET)

        # Mirror original backtester: process any bar before 16:00 ET.
        # gap_crap enforces its own 9:25 check internally.
        if bar_et.hour >= 16:
            return []

        ctx = {
            "no_gap":       no_gap,
            "prev_close":   prev_close,
            "day_low":      day_low,
            "day_hod":      day_hod,
            "bar_et":       bar_et,
            "float_shares": float_shares,
        }

        status  = "pending" if live else "launched"
        signals: list[Signal] = []
        for fn, params, instance_name in self._strategies:
            try:
                sig = fn(symbol, timeframe, prev, curr, ctx, params, instance_name)
                if sig:
                    sig.status = status
                    signals.append(sig)
            except Exception:
                pass

        return signals

    # ── Internal helper ───────────────────────────────────────────────────────

    def _sig(
        self,
        sym: str, tf: str, instance_name: str, type_: str,
        curr, entry: float, sl: float, tp: float,
    ) -> Signal:
        return Signal(
            symbol=sym,
            strategy=instance_name,
            timeframe=tf,
            type=type_,
            triggered_at=datetime.fromtimestamp(int(curr["time"]), tz=_ET),
            signal_close=float(curr["close"]),
            entry_est=round(entry, 4),
            sl=round(sl, 4),
            tp=round(tp, 4),
        )

    @staticmethod
    def _short_ok(curr, ctx) -> bool:
        """Price >= $0.60 and float <= 60M shares (0 = unknown, skip check)."""
        if float(curr["close"]) < 0.60:
            return False
        fs = ctx["float_shares"]
        if fs > 0 and fs > 60_000_000:
            return False
        return True

    @staticmethod
    def _long_ok(curr, ctx) -> bool:
        """Price >= $0.10 and float <= 60M shares (0 = unknown, skip check)."""
        if float(curr["close"]) < 0.10:
            return False
        fs = ctx["float_shares"]
        if fs > 0 and fs > 60_000_000:
            return False
        return True

    @staticmethod
    def _has_indicators(*rows) -> bool:
        for row in rows:
            for col in ("sma_9", "sma_200", "vwap"):
                if col not in row.index or pd.isna(row[col]):
                    return False
        return True

    # ── 1 & 2. Backside short — lower low ────────────────────────────────────
    # fix: SL = entry * (1 + stop_pct)
    # hod: SL = day_hod * (1 + stop_pct)

    def _backside_lower_low_fix(self, sym, tf, prev, curr, ctx, params, instance_name) -> Optional[Signal]:
        gap_pct  = params["gap_pct"]
        stop_pct = params["stop_pct"]
        tp_pct   = params["tp_pct"]

        if not self._short_ok(curr, ctx) or not ctx["no_gap"] or not self._has_indicators(prev):
            return None
        if not (
            curr["close"] < curr["open"]
            and prev["close"] > prev["open"]
            and curr["close"] < prev["low"]
            and curr["high"] >= ctx["prev_close"] * (1 + gap_pct)
            and curr["open"] > prev["vwap"]
        ):
            return None
        entry = curr["close"]
        return self._sig(sym, tf, instance_name, "short", curr,
                         entry, entry * (1 + stop_pct), entry * (1 - tp_pct))

    def _backside_lower_low_hod(self, sym, tf, prev, curr, ctx, params, instance_name) -> Optional[Signal]:
        gap_pct  = params["gap_pct"]
        stop_pct = params["stop_pct"]
        tp_pct   = params["tp_pct"]

        if not self._short_ok(curr, ctx) or not ctx["no_gap"] or ctx["day_hod"] <= 0 or not self._has_indicators(prev):
            return None
        if not (
            curr["close"] < curr["open"]
            and prev["close"] > prev["open"]
            and curr["close"] < prev["low"]
            and curr["high"] >= ctx["prev_close"] * (1 + gap_pct)
            and curr["open"] > prev["vwap"]
        ):
            return None
        entry = curr["close"]
        return self._sig(sym, tf, instance_name, "short", curr,
                         entry, ctx["day_hod"] * (1 + stop_pct), entry * (1 - tp_pct))

    # ── 3. Gap crap ───────────────────────────────────────────────────────────

    def _gap_crap(self, sym, tf, prev, curr, ctx, params, instance_name) -> Optional[Signal]:
        if not self._short_ok(curr, ctx):
            return None
        et = ctx["bar_et"]
        if not (et.hour == 9 and et.minute == 25):
            return None
        gap_pct  = params["gap_pct"]
        stop_pct = params["stop_pct"]
        tp_pct   = params["tp_pct"]
        if curr["close"] < ctx["prev_close"] * (1 + gap_pct):
            return None
        entry = curr["close"]
        return self._sig(sym, tf, instance_name, "short", curr,
                         entry, entry * (1 + stop_pct), entry * (1 - tp_pct))

    # ── 4 & 5. Push exhaustion ────────────────────────────────────────────────

    def _push_exhaustion_fix(self, sym, tf, prev, curr, ctx, params, instance_name) -> Optional[Signal]:
        gap_pct  = params["gap_pct"]
        stop_pct = params["stop_pct"]
        tp_pct   = params["tp_pct"]

        if not self._short_ok(curr, ctx) or not ctx["no_gap"] or not self._has_indicators(prev):
            return None
        prev_range  = max(prev["high"] - prev["low"], 1e-8)
        low_to_open = max(curr["open"] - curr["low"], 1e-8)
        if not (
            curr["close"] < curr["open"]
            and (curr["high"] - curr["open"]) >= 1.5 * low_to_open
            and curr["open"] > prev["vwap"]
            and curr["volume"] > prev["volume"]
            and curr["volume"] > 40_000
            and curr["high"] >= ctx["prev_close"] * (1 + gap_pct)
            and (curr["high"] - curr["low"]) >= 0.20 * prev_range
        ):
            return None
        entry = curr["close"]
        return self._sig(sym, tf, instance_name, "short", curr,
                         entry, entry * (1 + stop_pct), entry * (1 - tp_pct))

    def _push_exhaustion_hod(self, sym, tf, prev, curr, ctx, params, instance_name) -> Optional[Signal]:
        gap_pct  = params["gap_pct"]
        stop_pct = params["stop_pct"]
        tp_pct   = params["tp_pct"]

        if not self._short_ok(curr, ctx) or not ctx["no_gap"] or ctx["day_hod"] <= 0 or not self._has_indicators(prev):
            return None
        prev_range  = max(prev["high"] - prev["low"], 1e-8)
        low_to_open = max(curr["open"] - curr["low"], 1e-8)
        if not (
            curr["close"] < curr["open"]
            and (curr["high"] - curr["open"]) >= 1.5 * low_to_open
            and curr["open"] > prev["vwap"]
            and curr["volume"] > prev["volume"]
            and curr["volume"] > 40_000
            and curr["high"] >= ctx["prev_close"] * (1 + gap_pct)
            and (curr["high"] - curr["low"]) >= 0.20 * prev_range
        ):
            return None
        entry = curr["close"]
        return self._sig(sym, tf, instance_name, "short", curr,
                         entry, ctx["day_hod"] * (1 + stop_pct), entry * (1 - tp_pct))

    # ── 6 & 7. Push rejection ─────────────────────────────────────────────────

    def _push_rejection_fix(self, sym, tf, prev, curr, ctx, params, instance_name) -> Optional[Signal]:
        stop_pct = params["stop_pct"]
        tp_pct   = params["tp_pct"]

        if not self._short_ok(curr, ctx) or not ctx["no_gap"] or not self._has_indicators(curr):
            return None
        prev_range = max(prev["high"] - prev["low"], 1e-8)
        if not (
            curr["close"] < curr["open"]
            and curr["open"] > curr["vwap"]
            and curr["close"] < curr["vwap"]
            and (curr["high"] - curr["low"]) >= 0.30 * prev_range
            and prev["close"] > prev["open"]
            and prev_range >= 0.40 * max(prev["open"], 1e-8)
            and (curr["open"] - curr["close"]) > max(curr["close"] - curr["low"], 0)
        ):
            return None
        entry = curr["close"]
        return self._sig(sym, tf, instance_name, "short", curr,
                         entry, entry * (1 + stop_pct), entry * (1 - tp_pct))

    def _push_rejection_hod(self, sym, tf, prev, curr, ctx, params, instance_name) -> Optional[Signal]:
        stop_pct = params["stop_pct"]
        tp_pct   = params["tp_pct"]

        if not self._short_ok(curr, ctx) or not ctx["no_gap"] or ctx["day_hod"] <= 0 or not self._has_indicators(curr):
            return None
        prev_range = max(prev["high"] - prev["low"], 1e-8)
        if not (
            curr["close"] < curr["open"]
            and curr["open"] > curr["vwap"]
            and curr["close"] < curr["vwap"]
            and (curr["high"] - curr["low"]) >= 0.30 * prev_range
            and prev["close"] > prev["open"]
            and prev_range >= 0.40 * max(prev["open"], 1e-8)
            and (curr["open"] - curr["close"]) > max(curr["close"] - curr["low"], 0)
        ):
            return None
        entry = curr["close"]
        return self._sig(sym, tf, instance_name, "short", curr,
                         entry, ctx["day_hod"] * (1 + stop_pct), entry * (1 - tp_pct))

    # ── 8 & 9. SMA9 momentum long ─────────────────────────────────────────────

    def _sma9_momentum_long(self, sym, tf, prev, curr, ctx, params, instance_name) -> Optional[Signal]:
        min_dist_pct = params["min_dist_pct"]
        max_dist_pct = params["max_dist_pct"]
        min_volume   = params["min_volume"]
        target_rr    = params["target_rr"]

        if not self._long_ok(curr, ctx) or not ctx["no_gap"] or ctx["day_low"] <= 0:
            return None
        if not self._has_indicators(curr, prev):
            return None
        day_low  = ctx["day_low"]
        dist_pct = (curr["close"] - day_low) / max(day_low, 1e-8)
        if not (
            curr["close"] > curr["open"]
            and curr["sma_9"] > prev["sma_9"]
            and curr["volume"] >= min_volume
            and min_dist_pct < dist_pct < max_dist_pct
            and (curr["close"] - curr["open"]) / max(curr["high"] - curr["low"], 1e-8) >= 0.10
        ):
            return None
        entry = curr["close"]
        risk  = entry - day_low
        if risk <= 1e-8:
            return None
        return self._sig(sym, tf, instance_name, "long", curr,
                         entry, day_low, entry + target_rr * risk)

    def _sma9_momentum_dynrr(self, sym, tf, prev, curr, ctx, params, instance_name) -> Optional[Signal]:
        min_dist_pct = params["min_dist_pct"]
        max_dist_pct = params["max_dist_pct"]
        min_volume   = params["min_volume"]

        if not self._long_ok(curr, ctx) or not ctx["no_gap"] or ctx["day_low"] <= 0:
            return None
        if not self._has_indicators(curr, prev):
            return None
        day_low  = ctx["day_low"]
        dist_pct = (curr["close"] - day_low) / max(day_low, 1e-8)
        if not (
            curr["close"] > curr["open"]
            and curr["sma_9"] > prev["sma_9"]
            and curr["volume"] >= min_volume
            and min_dist_pct < dist_pct < max_dist_pct
            and (curr["close"] - curr["open"]) / max(curr["high"] - curr["low"], 1e-8) >= 0.10
        ):
            return None
        entry     = curr["close"]
        risk      = entry - day_low
        if risk <= 1e-8:
            return None
        target_rr = _dynamic_target_rr(dist_pct)
        sig = self._sig(sym, tf, instance_name, "long", curr,
                        entry, day_low, entry + target_rr * risk)
        sig.max_hold_hours = float(params.get("max_hold_hours", 0.0))
        return sig


    def _sma9_momentum_long_bar_sl(self, sym, tf, prev, curr, ctx, params, instance_name) -> Optional[Signal]:
        """
        Same entry conditions as sma9_momentum_long_iterative but:
          SL  = low of the signal bar  (not day_low)
          TP  = entry + target_rr * (entry - signal_bar_low)
        The trade stops out immediately if the next bar trades below the signal bar's low.
        """
        min_dist_pct = params["min_dist_pct"]
        max_dist_pct = params["max_dist_pct"]
        min_volume   = params["min_volume"]
        target_rr    = params["target_rr"]

        if not self._long_ok(curr, ctx) or not ctx["no_gap"] or ctx["day_low"] <= 0:
            return None
        if not self._has_indicators(curr, prev):
            return None

        day_low  = ctx["day_low"]
        dist_pct = (curr["close"] - day_low) / max(day_low, 1e-8)
        if not (
            curr["close"] > curr["open"]
            and curr["sma_9"] > prev["sma_9"]
            and curr["volume"] >= min_volume
            and min_dist_pct < dist_pct < max_dist_pct
            and (curr["close"] - curr["open"]) / max(curr["high"] - curr["low"], 1e-8) >= 0.10
        ):
            return None

        entry = float(curr["close"])
        sl    = float(curr["low"])          # signal bar low
        risk  = entry - sl
        if risk <= 1e-8:
            return None

        sig = self._sig(sym, tf, instance_name, "long", curr,
                        entry, sl, entry + target_rr * risk)
        sig.max_hold_hours = float(params.get("max_hold_hours", 0.0))
        return sig


def _dynamic_target_rr(dist_pct: float) -> float:
    if dist_pct < 0.10: return 10.0
    if dist_pct < 0.30: return  4.0
    if dist_pct < 0.40: return  3.0
    if dist_pct < 0.60: return  2.0
    return 1.0
