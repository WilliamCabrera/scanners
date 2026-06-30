"""
DailyStore — persists previous-day OHLC for stocks.

On load:
  - Reads from cache file if it exists and is fresh (≤ 3 days old).
  - Fetches from REST snapshot API otherwise.

Background scheduler:
  - Refreshes the file every day at 3 AM.
"""
import json
import logging
import threading
import time
import urllib.request
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Optional
from zoneinfo import ZoneInfo

log = logging.getLogger(__name__)

_SNAPSHOT_URL   = "https://api.massive.com/v2/snapshot/locale/us/markets/stocks/tickers"
_REFERENCE_URL  = "https://api.massive.com/v3/reference/tickers/{ticker}?apiKey={key}"
_AH_AGGS_URL    = "https://api.massive.com/v2/aggs/ticker/{sym}/range/1/hour/{frm}/{to}"
_DEFAULT_CACHE = Path(__file__).parent.parent.parent.parent / ".cache" / "prev_day_stocks.json"
_MAX_CACHE_AGE_DAYS = 0
_ET = ZoneInfo("America/New_York")


@dataclass(frozen=True)
class PrevDay:
    open: float
    high: float
    low: float
    close: float
    volume: float = 0.0       # regular market volume (for RVOL)
    market_cap: float = 0.0   # from /v3/reference/tickers, refreshed daily
    float_shares: float = 0.0 # share_class_shares_outstanding


@dataclass(frozen=True)
class TodaySnapshot:
    """Intraday snapshot from the REST API. Not persisted to disk."""
    high: float
    low: float
    close: float   # latest price (may include AH if fetched after 4pm)
    volume: float  # accumulated volume so far today


class DailyStore:
    def __init__(
        self,
        api_key: str,
        symbols: Optional[list[str]] = None,
        cache_path: Optional[Path] = None,
        on_today_hl_ready: Optional[callable] = None,
        on_ah_hl_ready: Optional[callable] = None,
    ) -> None:
        self._api_key = api_key
        self._symbols = [s for s in (symbols or []) if s != "*"]
        self._cache_path = cache_path or _DEFAULT_CACHE
        self._data: dict[str, PrevDay] = {}
        # Today's intraday snapshot — in-memory only, never persisted.
        self._today: dict[str, TodaySnapshot] = {}
        # After-hours H/L/open/vol from 1h aggs — populated in background when starting mid-AH.
        # Each value is (ah_high, ah_low, ah_open, ah_vol).
        self._ah: dict[str, tuple[float, float, float, int]] = {}
        self._lock = threading.Lock()
        self._on_today_hl_ready = on_today_hl_ready
        # Called after AH 1h bars are fetched so provider can patch cached AH quotes.
        self._on_ah_hl_ready = on_ah_hl_ready

    # ── Public ────────────────────────────────────────────────────────────────

    def refresh(self) -> None:
        """Merge existing cache with a fresh REST fetch. Safe to call from cron."""
        self._cache_path.parent.mkdir(parents=True, exist_ok=True)
        if self._cache_path.exists():
            try:
                self._read_file()
            except Exception:
                pass
        self._fetch_and_save()

    def load(self) -> None:
        """Load cache; fetch from API if missing or stale. Starts 3AM scheduler."""
        self._cache_path.parent.mkdir(parents=True, exist_ok=True)

        if self._cache_path.exists() and self._is_fresh():
            try:
                self._read_file()
                log.info("DailyStore: %d symbols loaded from cache", len(self._data))
                # Fetch today's intraday H/L synchronously so it's ready before the
                # WebSocket stream starts (avoids race where first bar sees empty _today_hl).
                self._fetch_today_hl()
            except Exception as exc:
                log.warning("DailyStore: cache read failed (%s) — refetching", exc)
                self._fetch_and_save()
        else:
            self._fetch_and_save()

        # If starting mid-AH, fetch AH H/L from 1h bars in background so the
        # dashboard can correct afterhours_high/low once the fetch completes.
        if self._is_afterhours():
            self._fetch_ah_background()

        threading.Thread(
            target=self._scheduler_loop,
            daemon=True,
            name="daily-store-3am",
        ).start()

    def get(self, symbol: str) -> Optional[PrevDay]:
        with self._lock:
            return self._data.get(symbol)

    def get_today_hl(self, symbol: str) -> Optional[tuple[float, float]]:
        """Return today's (high, low) or None if not available."""
        with self._lock:
            snap = self._today.get(symbol)
            return (snap.high, snap.low) if snap else None

    def get_today_snapshot(self, symbol: str) -> Optional[TodaySnapshot]:
        """Return full today snapshot or None if not available."""
        with self._lock:
            return self._today.get(symbol)

    def get_ah_snapshot(self, symbol: str) -> Optional[tuple[float, float, float, int]]:
        """Return (ah_high, ah_low, ah_open, ah_vol) from the 1h AH fetch, or None."""
        with self._lock:
            return self._ah.get(symbol)

    # ── File I/O ──────────────────────────────────────────────────────────────

    def _is_fresh(self) -> bool:
        try:
            with open(self._cache_path) as f:
                stored = json.load(f)
            cached = date.fromisoformat(stored.get("date", "2000-01-01"))
            return (date.today() - cached).days <= _MAX_CACHE_AGE_DAYS
        except Exception:
            return False

    def _read_file(self) -> None:
        with open(self._cache_path) as f:
            stored = json.load(f)
        data = {
            sym: PrevDay(
                open=float(v.get("open", 0)),
                high=float(v.get("high", 0)),
                low=float(v.get("low", 0)),
                close=float(v.get("close", 0)),
                volume=float(v.get("volume", 0)),
                market_cap=float(v.get("market_cap", 0)),
                float_shares=float(v.get("float_shares", 0)),
            )
            for sym, v in stored.get("symbols", {}).items()
        }
        with self._lock:
            self._data = data

    def _write_file(self, data: dict[str, PrevDay]) -> None:
        payload = {
            "date": date.today().isoformat(),
            "symbols": {
                sym: {
                    "open": v.open, "high": v.high, "low": v.low,
                    "close": v.close, "volume": v.volume,
                    "market_cap": v.market_cap, "float_shares": v.float_shares,
                }
                for sym, v in data.items()
            },
        }
        with open(self._cache_path, "w") as f:
            json.dump(payload, f, indent=2)
        log.info("DailyStore: %d symbols saved to %s", len(data), self._cache_path)

    # ── REST fetch ────────────────────────────────────────────────────────────

    def _fetch_and_save(self) -> None:
        log.info("DailyStore: fetching prev-day OHLC from REST …")
        url = f"{_SNAPSHOT_URL}?apiKey={self._api_key}"
        if self._symbols:
            url += "&tickers=" + ",".join(self._symbols)

        try:
            with urllib.request.urlopen(url, timeout=15) as resp:
                payload = json.loads(resp.read().decode())
        except Exception as exc:
            log.error("DailyStore: REST fetch failed: %s", exc)
            return

        fresh: dict[str, PrevDay] = {}
        today: dict[str, TodaySnapshot] = {}
        for entry in payload.get("tickers", []):
            sym = entry.get("ticker", "")
            prev = entry.get("prevDay", {})
            if sym and prev and prev.get("c"):
                fresh[sym] = PrevDay(
                    open=float(prev.get("o", 0)),
                    high=float(prev.get("h", 0)),
                    low=float(prev.get("l", 0)),
                    close=float(prev.get("c", 0)),
                    volume=float(prev.get("v", 0)),
                )
            day = entry.get("day", {})
            h = float(day.get("h", 0) or 0)
            l = float(day.get("l", 0) or 0)
            c = float(day.get("c", 0) or 0)
            v = float(day.get("v", 0) or 0)
            if sym and h > 0 and l > 0:
                today[sym] = TodaySnapshot(high=h, low=l, close=c, volume=v)

        # Fetch market cap + float from reference API and merge into PrevDay entries
        if fresh:
            ref = self._fetch_reference_data(list(fresh.keys()))
            fresh = {
                sym: PrevDay(
                    open=e.open, high=e.high, low=e.low, close=e.close, volume=e.volume,
                    market_cap=ref.get(sym, (0.0, 0.0))[0],
                    float_shares=ref.get(sym, (0.0, 0.0))[1],
                )
                for sym, e in fresh.items()
            }

        # Merge: keep last known OHLC for any symbol not in today's response
        with self._lock:
            merged = {**self._data, **fresh}
            self._data = merged
            self._today.update(today)

        self._write_file(merged)
        log.info(
            "DailyStore: %d total symbols (%d updated), today snapshot for %d symbols",
            len(merged), len(fresh), len(today),
        )
        if self._on_today_hl_ready and today:
            self._on_today_hl_ready({sym: (s.high, s.low) for sym, s in today.items()})

    def _fetch_today_hl(self) -> None:
        """Fetch today's intraday H/L from the snapshot API (no disk write)."""
        log.info("DailyStore: fetching today's intraday H/L …")
        url = f"{_SNAPSHOT_URL}?apiKey={self._api_key}"
        if self._symbols:
            url += "&tickers=" + ",".join(self._symbols)

        try:
            with urllib.request.urlopen(url, timeout=15) as resp:
                payload = json.loads(resp.read().decode())
        except Exception as exc:
            log.warning("DailyStore: today H/L fetch failed: %s", exc)
            return

        today: dict[str, TodaySnapshot] = {}
        for entry in payload.get("tickers", []):
            sym = entry.get("ticker", "")
            day = entry.get("day", {})
            h = float(day.get("h", 0) or 0)
            l = float(day.get("l", 0) or 0)
            c = float(day.get("c", 0) or 0)
            v = float(day.get("v", 0) or 0)
            if sym and h > 0 and l > 0:
                today[sym] = TodaySnapshot(high=h, low=l, close=c, volume=v)

        with self._lock:
            self._today.update(today)
        log.info("DailyStore: today snapshot available for %d symbols", len(today))
        if self._on_today_hl_ready and today:
            self._on_today_hl_ready({sym: (s.high, s.low) for sym, s in today.items()})

    # ── Reference data (market cap + float) ──────────────────────────────────

    def _fetch_reference_data(
        self, symbols: list[str]
    ) -> dict[str, tuple[float, float]]:
        """Fetch (market_cap, float_shares) for each symbol from /v3/reference/tickers.
        Returns a dict {sym: (market_cap, float_shares)}; missing symbols are omitted."""

        def _fetch_one(sym: str) -> tuple[str, Optional[tuple[float, float]]]:
            url = _REFERENCE_URL.format(ticker=sym, key=self._api_key)
            try:
                with urllib.request.urlopen(url, timeout=10) as resp:
                    data = json.loads(resp.read().decode())
                r = data.get("results", {})
                mcap   = float(r.get("market_cap", 0) or 0)
                fshare = float(r.get("share_class_shares_outstanding", 0) or 0)
                return sym, (mcap, fshare) if (mcap > 0 or fshare > 0) else None
            except Exception as exc:
                log.debug("Reference fetch failed for %s: %s", sym, exc)
                return sym, None

        log.info("DailyStore: fetching reference data (market cap / float) for %d symbols …", len(symbols))
        results: dict[str, tuple[float, float]] = {}
        with ThreadPoolExecutor(max_workers=20) as pool:
            for sym, val in pool.map(_fetch_one, symbols):
                if val:
                    results[sym] = val
        log.info("DailyStore: reference data ready for %d / %d symbols", len(results), len(symbols))
        return results

    # ── After-hours 1h fetch ──────────────────────────────────────────────────

    @staticmethod
    def _is_afterhours() -> bool:
        return datetime.now(tz=_ET).hour >= 16

    def _fetch_ah_background(self) -> None:
        threading.Thread(
            target=self._fetch_ah_1h,
            daemon=True,
            name="daily-store-ah-hl",
        ).start()

    def _fetch_ah_1h(self) -> None:
        """Fetch 1h aggregate bars for the after-hours period (16:00 → 20:00 ET).
        Uses millisecond timestamps as from/to — same pattern as premarket_store.
        Fires on_ah_hl_ready so the provider can patch cached AH quotes."""
        today = date.today()
        frm_ms = int(datetime(today.year, today.month, today.day, 16, 0, 0, tzinfo=_ET).timestamp() * 1000)
        to_ms  = int(datetime(today.year, today.month, today.day, 20, 0, 0, tzinfo=_ET).timestamp() * 1000)

        with self._lock:
            symbols = list(self._symbols) if self._symbols else list(self._data.keys())
        if not symbols:
            return

        results: dict[str, tuple[float, float, float, int]] = {}

        def _fetch_one(sym: str) -> tuple[str, Optional[tuple[float, float, float, int]]]:
            url = (
                _AH_AGGS_URL.format(sym=sym, frm=frm_ms, to=to_ms)
                + f"?adjusted=false&sort=asc&apiKey={self._api_key}"
            )
            try:
                with urllib.request.urlopen(url, timeout=10) as resp:
                    data = json.loads(resp.read().decode())
                bars = data.get("results", [])
                if not bars:
                    return sym, None
                high  = max(float(b["h"]) for b in bars)
                low   = min(float(b["l"]) for b in bars)
                open_ = float(bars[0].get("o", 0))
                vol   = int(sum(b.get("v", 0) for b in bars))
                return sym, (high, low, open_, vol)
            except Exception as exc:
                log.debug("AH 1h fetch failed for %s: %s", sym, exc)
                return sym, None

        log.info("DailyStore: fetching AH 1h bars for %d symbols …", len(symbols))
        with ThreadPoolExecutor(max_workers=10) as pool:
            for sym, val in pool.map(_fetch_one, symbols):
                if val:
                    results[sym] = val

        with self._lock:
            self._ah.update(results)

        log.info("DailyStore: AH 1h snapshot ready for %d / %d symbols", len(results), len(symbols))
        if self._on_ah_hl_ready and results:
            self._on_ah_hl_ready(results)

    # ── 3 AM scheduler ───────────────────────────────────────────────────────

    def _scheduler_loop(self) -> None:
        while True:
            delay = self._seconds_until_3am()
            log.debug("DailyStore: next refresh in %.0f s (3AM)", delay)
            time.sleep(delay)
            self._fetch_and_save()

    @staticmethod
    def _seconds_until_3am() -> float:
        now = datetime.now()
        target = now.replace(hour=3, minute=0, second=0, microsecond=0)
        if now >= target:
            target += timedelta(days=1)
        return (target - now).total_seconds()
