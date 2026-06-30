from abc import ABC, abstractmethod
from datetime import datetime, timezone
from typing import Optional
from zoneinfo import ZoneInfo

from rich.table import Table

from ....core.models import Quote, ScanResult

_ET = ZoneInfo("America/New_York")


def _no_lookahead_ref(open_price: float, bar_low: object) -> float:
    """Fallback prev_close: min(official_open, bar_low) — only current bar data."""
    low = float(bar_low) if bar_low is not None else open_price
    return min(open_price, low) if open_price > 0 else low


def _bar_session(end_ms: Optional[int]) -> str:
    """Return 'premarket', 'regular', or 'afterhours' from bar end timestamp."""
    dt = (
        datetime.fromtimestamp(end_ms / 1000, tz=_ET)
        if end_ms
        else datetime.now(tz=_ET)
    )
    h, m = dt.hour, dt.minute
    if h < 9 or (h == 9 and m < 30):
        return "premarket"
    if h < 16:
        return "regular"
    return "afterhours"


class AssetProcessor(ABC):
    """
    1. parse()       — convert a raw WebSocket message into a Quote
    2. build_table() — render a list of ScanResults as a Rich Table
    """

    def parse(self, msg: object, existing: Optional[Quote]) -> Optional[Quote]:
        sym: str = getattr(msg, "symbol", "") or ""
        if not sym:
            return None

        def _f(attr: str, default: float = 0.0) -> float:
            val = getattr(msg, attr, None)
            return float(val) if val is not None else default

        last = _f("close")
        official_open = _f("official_open_price", 0.0)
        bar_vol = int(_f("volume"))
        acc_vol = int(_f("accumulated_volume") or _f("volume"))
        end_ms: Optional[int] = getattr(msg, "end_timestamp", None)
        session = _bar_session(end_ms)

        # ── After-hours fields default (overwritten in AH branches) ──────────
        regular_close = 0.0
        ah_open = 0.0; ah_high = 0.0; ah_low = 0.0; ah_vol = 0
        now_ah = False

        market_cap   = 0.0
        float_shares = 0.0

        if existing:
            market_cap   = existing.market_cap
            float_shares = existing.float_shares
            if session == "premarket":
                premarket_vol = acc_vol
                regular_vol = 0
                open_price = 0.0
                high = 0.0; low = 0.0
                now_open = False

            elif session == "regular":
                if not existing.market_open:
                    # Transition: pre-market → regular session
                    premarket_vol = existing.accumulated_volume
                    regular_vol = max(0, acc_vol - premarket_vol)
                    open_price = official_open if official_open else last
                    high = self._initial_day_high(sym, _f("high", last))
                    low = self._initial_day_low(sym, _f("low", last))
                else:
                    # Continuing regular session
                    premarket_vol = existing.premarket_volume
                    regular_vol = max(0, acc_vol - premarket_vol)
                    open_price = existing.open
                    high = max(existing.high, _f("high")) if existing.high > 0 else _f("high", last)
                    low = min(existing.low, _f("low", last)) if existing.low > 0 else _f("low", last)
                now_open = True

            else:  # afterhours
                # Keep regular-session fields frozen
                premarket_vol = existing.premarket_volume
                open_price = existing.open
                high = existing.high
                low = existing.low
                now_open = True
                now_ah = True

                if not existing.after_market:
                    # Transition: regular → after-hours
                    regular_close = existing.last  # capture the 4pm close
                    regular_vol = existing.regular_volume  # freeze regular vol
                    ah_open = _f("open", last)
                    ah_high = _f("high", last)
                    ah_low = _f("low", last)
                    ah_vol = max(0, acc_vol - premarket_vol - regular_vol)
                else:
                    # Continuing after-hours
                    regular_close = existing.regular_close
                    regular_vol = existing.regular_volume  # still frozen
                    ah_open = existing.afterhours_open
                    ah_high = (
                        max(existing.afterhours_high, _f("high"))
                        if existing.afterhours_high > 0
                        else _f("high", last)
                    )
                    ah_low = (
                        min(existing.afterhours_low, _f("low", last))
                        if existing.afterhours_low > 0
                        else _f("low", last)
                    )
                    ah_vol = max(0, acc_vol - premarket_vol - regular_vol)

            prev_close = existing.prev_close
            prev_day_vol = existing.prev_day_volume

        else:
            # First bar ever for this symbol
            if session == "premarket":
                premarket_vol = acc_vol
                regular_vol = 0; open_price = 0.0
                high = 0.0; low = 0.0
                now_open = False

            elif session == "regular":
                premarket_vol = self._initial_premarket_volume(sym)
                regular_vol = max(0, acc_vol - premarket_vol)
                open_price = official_open if official_open else last
                high = self._initial_day_high(sym, _f("high", last))
                low = self._initial_day_low(sym, _f("low", last))
                now_open = True

            else:  # afterhours, scanner started after 4pm
                premarket_vol = self._initial_premarket_volume(sym)
                regular_vol = self._initial_regular_volume(sym)
                open_price = self._initial_regular_open(sym)
                high = self._initial_day_high(sym, _f("high", last))
                low = self._initial_day_low(sym, _f("low", last))
                regular_close = self._initial_regular_close(sym)
                ah_open = self._initial_ah_open(sym, _f("open", last))
                ah_high = self._initial_ah_high(sym, _f("high", last))
                ah_low  = self._initial_ah_low(sym, _f("low", last))
                ah_vol = max(0, acc_vol - premarket_vol - regular_vol)
                now_open = True
                now_ah = True

            ref_price    = official_open if official_open else last
            prev_close   = self._initial_prev_close(msg, ref_price, sym)
            market_cap   = self._initial_market_cap(sym)
            float_shares = self._initial_float_shares(sym)
            prev_day_vol = self._initial_prev_day_volume(sym)

        ts = (
            datetime.fromtimestamp(end_ms / 1000, tz=timezone.utc).replace(tzinfo=None)
            if end_ms
            else datetime.now()
        )

        return Quote(
            symbol=sym,
            last=last,
            bid=last,
            ask=last,
            volume=bar_vol,
            open=open_price,
            high=high,
            low=low,
            prev_close=prev_close,
            accumulated_volume=acc_vol,
            premarket_volume=premarket_vol,
            regular_volume=regular_vol,
            prev_day_volume=prev_day_vol,
            market_cap=market_cap,
            float_shares=float_shares,
            market_open=now_open,
            regular_close=regular_close,
            afterhours_open=ah_open,
            afterhours_high=ah_high,
            afterhours_low=ah_low,
            afterhours_volume=ah_vol,
            after_market=now_ah,
            timestamp=ts,
        )

    # ── Hooks overridden by subclasses ────────────────────────────────────────

    def _initial_prev_close(self, msg: object, open_price: float, sym: str) -> float:
        return _no_lookahead_ref(open_price, getattr(msg, "low", None))

    def _initial_prev_day_volume(self, sym: str) -> float:
        return 0.0

    def _initial_market_cap(self, sym: str) -> float:
        return 0.0

    def _initial_float_shares(self, sym: str) -> float:
        return 0.0

    def _initial_premarket_volume(self, sym: str) -> int:
        return 0

    def _initial_regular_volume(self, sym: str) -> int:
        return 0

    def _initial_regular_open(self, sym: str) -> float:
        return 0.0

    def _initial_regular_close(self, sym: str) -> float:
        """Regular session close — queried when scanner starts during after-hours."""
        return 0.0

    def _initial_day_high(self, sym: str, bar_high: float) -> float:
        """True day HOD when scanner starts mid-session. Default: current bar only."""
        return bar_high

    def _initial_day_low(self, sym: str, bar_low: float) -> float:
        """True day LOD when scanner starts mid-session. Default: current bar only."""
        return bar_low

    def _initial_ah_high(self, sym: str, bar_high: float) -> float:
        """AH high from historical 1h bars (if available). Default: current bar only."""
        return bar_high

    def _initial_ah_low(self, sym: str, bar_low: float) -> float:
        """AH low from historical 1h bars (if available). Default: current bar only."""
        return bar_low

    def _initial_ah_open(self, sym: str, bar_open: float) -> float:
        """Open of the 16:00 bar from historical data. Default: current bar open."""
        return bar_open

    @abstractmethod
    def build_table(self, results: list[ScanResult], title: str) -> Table: ...
