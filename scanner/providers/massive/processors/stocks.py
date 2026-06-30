from datetime import datetime
from typing import TYPE_CHECKING, Optional

from rich import box
from rich.table import Table

from .base import AssetProcessor, _no_lookahead_ref
from ....core.models import ScanResult
from ....display.utils import colored, fmt_price, fmt_large, fmt_vol

if TYPE_CHECKING:
    from ..daily_store import DailyStore
    from ..premarket_store import PremarketStore


class StocksProcessor(AssetProcessor):
    def __init__(
        self,
        daily_store: Optional["DailyStore"] = None,
        premarket_store: Optional["PremarketStore"] = None,
    ) -> None:
        self._store = daily_store
        self._premarket_store = premarket_store

    # ── Init hooks ────────────────────────────────────────────────────────────

    def _initial_prev_close(self, msg: object, open_price: float, sym: str) -> float:
        if self._store:
            entry = self._store.get(sym)
            if entry and entry.close != 0:
                return entry.close
        return _no_lookahead_ref(open_price, getattr(msg, "low", None))

    def _initial_premarket_volume(self, sym: str) -> int:
        if self._premarket_store:
            return self._premarket_store.get(sym)
        return 0

    def _initial_prev_day_volume(self, sym: str) -> float:
        if self._store:
            entry = self._store.get(sym)
            if entry:
                return entry.volume
        return 0.0

    def _initial_day_high(self, sym: str, bar_high: float) -> float:
        if self._store:
            hl = self._store.get_today_hl(sym)
            if hl and hl[0] > 0:
                return max(hl[0], bar_high)
        return bar_high

    def _initial_day_low(self, sym: str, bar_low: float) -> float:
        if self._store:
            hl = self._store.get_today_hl(sym)
            if hl and hl[1] > 0:
                return min(hl[1], bar_low)
        return bar_low

    def _initial_regular_volume(self, sym: str) -> int:
        if self._store:
            snap = self._store.get_today_snapshot(sym)
            if snap and snap.volume > 0:
                return int(snap.volume)
        return 0

    def _initial_regular_open(self, sym: str) -> float:
        if self._store:
            snap = self._store.get_today_snapshot(sym)
            if snap:
                return 0.0  # snapshot doesn't have today's open; set 0 = "unknown"
        return 0.0

    def _initial_market_cap(self, sym: str) -> float:
        if self._store:
            entry = self._store.get(sym)
            if entry and entry.market_cap > 0:
                return entry.market_cap
        return 0.0

    def _initial_float_shares(self, sym: str) -> float:
        if self._store:
            entry = self._store.get(sym)
            if entry and entry.float_shares > 0:
                return entry.float_shares
        return 0.0

    def _initial_regular_close(self, sym: str) -> float:
        """Approximate regular close from snapshot (may include AH if fetched after 4pm)."""
        if self._store:
            snap = self._store.get_today_snapshot(sym)
            if snap and snap.close > 0:
                return snap.close
        return 0.0

    def _initial_ah_high(self, sym: str, bar_high: float) -> float:
        if self._store:
            snap = self._store.get_ah_snapshot(sym)
            if snap and snap[0] > 0:
                return max(snap[0], bar_high)
        return bar_high

    def _initial_ah_low(self, sym: str, bar_low: float) -> float:
        if self._store:
            snap = self._store.get_ah_snapshot(sym)
            if snap and snap[1] > 0:
                return min(snap[1], bar_low)
        return bar_low

    def _initial_ah_open(self, sym: str, bar_open: float) -> float:
        if self._store:
            snap = self._store.get_ah_snapshot(sym)
            if snap and snap[2] > 0:
                return snap[2]  # 16:00 bar open from history trumps current-bar open
        return bar_open

    # ── Regular-session table ─────────────────────────────────────────────────

    def build_table(self, results: list[ScanResult], title: str) -> Table:
        ts = datetime.now().strftime("%H:%M:%S")
        t = Table(
            title=f"{title}  [{ts}]  {len(results)} match{'es' if len(results) != 1 else ''}",
            box=box.SIMPLE_HEAD, header_style="bold cyan",
            title_style="bold white", show_lines=False, pad_edge=False,
        )
        t.add_column("Symbol",      style="bold white", min_width=8)
        t.add_column("Price",       justify="right",    min_width=8)
        t.add_column("Chg %",       justify="right",    min_width=8)
        t.add_column("Gap %",       justify="right",    min_width=8)
        t.add_column("Return %",    justify="right",    min_width=9)
        t.add_column("Prev Close",  justify="right",    min_width=10)
        t.add_column("Pre-mkt Vol", justify="right",    min_width=11)
        t.add_column("Open",        justify="right",    min_width=8)
        t.add_column("High",        justify="right",    min_width=8)
        t.add_column("Low",         justify="right",    min_width=8)
        t.add_column("Volume",      justify="right",    min_width=8)
        t.add_column("RVOL",        justify="right",    min_width=6)
        t.add_column("Mkt Cap",     justify="right",    min_width=8)
        t.add_column("Float",       justify="right",    min_width=8)

        if not results:
            t.add_row(*["—"] * 14)
            return t

        for r in results:
            q = r.quote
            mkt = q.market_open
            rvol_str = f"{q.rvol:.2f}x" if mkt and q.rvol > 0 else "—"
            t.add_row(
                q.symbol,
                fmt_price(q.last),
                colored(q.change_pct, ".2f", "%"),
                colored(q.gap_pct, ".2f", "%") if mkt else "—",
                colored(q.return_pct, ".2f", "%") if mkt else "—",
                fmt_price(q.prev_close),
                fmt_vol(q.premarket_volume) if q.premarket_volume > 0 else "—",
                fmt_price(q.open) if mkt else "—",
                fmt_price(q.high) if mkt else "—",
                fmt_price(q.low) if mkt else "—",
                fmt_vol(q.regular_volume) if mkt else "—",
                rvol_str,
                fmt_large(q.market_cap),
                fmt_large(q.float_shares),
            )
        return t

    # ── After-hours table ─────────────────────────────────────────────────────

    def build_afterhours_table(self, results: list[ScanResult], title: str) -> Table:
        ts = datetime.now().strftime("%H:%M:%S")
        ah_results = [r for r in results if r.quote.after_market]
        t = Table(
            title=f"{title}  [{ts}]  {len(ah_results)} match{'es' if len(ah_results) != 1 else ''}",
            box=box.SIMPLE_HEAD, header_style="bold magenta",
            title_style="bold white", show_lines=False, pad_edge=False,
        )
        t.add_column("Symbol",      style="bold white", min_width=8)
        t.add_column("Price",       justify="right",    min_width=8)
        t.add_column("Range %",     justify="right",    min_width=8)
        t.add_column("Return %",    justify="right",    min_width=9)
        t.add_column("Reg Close",   justify="right",    min_width=10)
        t.add_column("AH Open",     justify="right",    min_width=8)
        t.add_column("AH High",     justify="right",    min_width=8)
        t.add_column("AH Low",      justify="right",    min_width=8)
        t.add_column("AH Vol",      justify="right",    min_width=8)
        t.add_column("Mkt Cap",     justify="right",    min_width=8)
        t.add_column("Float",       justify="right",    min_width=8)

        if not ah_results:
            t.add_row(*["—"] * 11)
            return t

        for r in ah_results:
            q = r.quote
            has_reg_close = q.regular_close > 0
            has_ah_open = q.afterhours_open > 0
            t.add_row(
                q.symbol,
                fmt_price(q.last),
                colored(q.afterhours_range_pct, ".2f", "%") if has_ah_open and q.afterhours_high > 0 else "—",
                colored(q.afterhours_return_pct, ".2f", "%") if has_ah_open else "—",
                fmt_price(q.regular_close) if has_reg_close else "—",
                fmt_price(q.afterhours_open) if has_ah_open else "—",
                fmt_price(q.afterhours_high) if q.afterhours_high > 0 else "—",
                fmt_price(q.afterhours_low) if q.afterhours_low > 0 else "—",
                fmt_vol(q.afterhours_volume) if q.afterhours_volume > 0 else "—",
                fmt_large(q.market_cap),
                fmt_large(q.float_shares),
            )
        return t
