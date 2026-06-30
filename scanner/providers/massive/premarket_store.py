"""
PremarketStore — persists per-symbol accumulated volume from 4:00am to 9:29am ET.

Live path (scanner running during pre-market):
  - Stream updates call update(sym, acc_vol) on every bar
  - In-memory dict is flushed to disk every 60 s
  - At 9:30 ET: final flush, stop accepting updates (data frozen)

Restart path (scanner stopped and restarted):
  - load() reads today's cache file immediately (warm start)
  - Recalculates in background using 15-minute REST bars (4:00am – 9:29am)
  - Priority batch (top-N gappers from priority_fn) is processed FIRST so the
    most relevant symbols appear in the table as quickly as possible
  - on_symbol_updated is called for each symbol as it completes, allowing the
    provider to patch the cached Quote without waiting for the full recalc

Reset: daily at 4:00am ET the cache is cleared for the new trading day.
"""
import json
import logging
import threading
import time
import urllib.request
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Callable, Optional
from zoneinfo import ZoneInfo

log = logging.getLogger(__name__)

_ET = ZoneInfo("America/New_York")
_DEFAULT_CACHE = Path(__file__).parent.parent.parent.parent / ".cache" / "premarket_volume.json"
_AGG_URL = "https://api.massive.com/v2/aggs/ticker/{sym}/range/15/minute/{frm}/{to}"
_RECALC_WORKERS = 20
_PRIORITY_COUNT = 50
_REMAINING_FLUSH_EVERY = 100   # flush to disk after this many symbols complete


def _et_now() -> datetime:
    return datetime.now(_ET)


def _is_premarket_now() -> bool:
    t = _et_now()
    return t.hour >= 4 and (t.hour, t.minute) < (9, 30)


def _premarket_window_ms(today: date) -> tuple[int, int]:
    """Return (from_ms, to_ms) for 4:00am – 9:29:59 ET as UTC milliseconds.
    to is set to 9:29:59 (one second before open) so the 9:30 regular bar
    is never included regardless of whether the API endpoint is inclusive."""
    frm = datetime(today.year, today.month, today.day, 4, 0, 0, tzinfo=_ET)
    to  = datetime(today.year, today.month, today.day, 9, 29, 59, tzinfo=_ET)
    return int(frm.timestamp() * 1000), int(to.timestamp() * 1000)


class PremarketStore:
    def __init__(
        self,
        api_key: str,
        symbols: Optional[list[str]] = None,
        cache_path: Optional[Path] = None,
        # Returns top symbols sorted by change% — processed first
        priority_fn: Optional[Callable[[], list[str]]] = None,
        # Returns ALL known symbols (DailyStore + active cache) for the remaining batch
        all_symbols_fn: Optional[Callable[[], list[str]]] = None,
        # Called in bulk after each batch completes so the provider can patch its cache
        on_symbol_updated: Optional[Callable[[str, int], None]] = None,
    ) -> None:
        self._api_key = api_key
        self._symbols: list[str] = [s for s in (symbols or []) if s != "*"]
        self._cache_path = cache_path or _DEFAULT_CACHE
        self._priority_fn = priority_fn
        self._all_symbols_fn = all_symbols_fn
        self._on_symbol_updated = on_symbol_updated
        self._data: dict[str, int] = {}
        self._lock = threading.Lock()
        self._accepting = False

    # ── Public ────────────────────────────────────────────────────────────────

    def load(self) -> None:
        """Load today's cache; trigger background recalc if needed. Start housekeeping."""
        self._cache_path.parent.mkdir(parents=True, exist_ok=True)
        loaded = self._try_load_cache()
        self._accepting = _is_premarket_now()

        if not _is_premarket_now() and not loaded:
            # Past 9:30 and no cache for today — wait for stream to warm up, then calculate
            threading.Thread(
                target=self._delayed_recalculate,
                daemon=True,
                name="premarket-recalc",
            ).start()

        if self._accepting:
            threading.Thread(
                target=self._flush_loop, daemon=True, name="premarket-flush"
            ).start()

        threading.Thread(
            target=self._housekeeping_loop, daemon=True, name="premarket-housekeeping"
        ).start()

    def get(self, symbol: str) -> int:
        with self._lock:
            return self._data.get(symbol, 0)

    def update(self, symbol: str, volume: int) -> None:
        """Called from MassiveProvider per pre-market bar. accumulated_volume is
        monotonically increasing so we only store if the value is higher."""
        if not self._accepting:
            return
        with self._lock:
            if volume > self._data.get(symbol, 0):
                self._data[symbol] = volume

    # ── File I/O ──────────────────────────────────────────────────────────────

    def _try_load_cache(self) -> bool:
        if not self._cache_path.exists():
            return False
        try:
            with open(self._cache_path) as f:
                stored = json.load(f)
            if stored.get("date") != date.today().isoformat():
                return False
            with self._lock:
                self._data = {sym: int(v) for sym, v in stored.get("symbols", {}).items()}
            log.info("PremarketStore: %d symbols loaded from cache", len(self._data))
            return True
        except Exception as exc:
            log.warning("PremarketStore: cache load failed (%s)", exc)
            return False

    def _flush_to_disk(self) -> None:
        with self._lock:
            snapshot = dict(self._data)
        payload = {"date": date.today().isoformat(), "symbols": snapshot}
        try:
            with open(self._cache_path, "w") as f:
                json.dump(payload, f)
            log.debug("PremarketStore: flushed %d symbols", len(snapshot))
        except Exception as exc:
            log.warning("PremarketStore: flush failed (%s)", exc)

    # ── Background loops ──────────────────────────────────────────────────────

    def _flush_loop(self) -> None:
        """Flush in-memory data to disk every 60 s during pre-market."""
        while True:
            time.sleep(60)
            if not _is_premarket_now():
                self._accepting = False
                self._flush_to_disk()
                log.info("PremarketStore: market opened, pre-market volume frozen")
                return
            self._flush_to_disk()

    def _housekeeping_loop(self) -> None:
        """Wait until 4am ET daily and reset data for the new trading day."""
        while True:
            delay = self._seconds_until_4am()
            log.debug("PremarketStore: next reset in %.0f s", delay)
            time.sleep(delay)
            with self._lock:
                self._data = {}
            self._flush_to_disk()
            self._accepting = True
            log.info("PremarketStore: reset for new trading day")
            threading.Thread(
                target=self._flush_loop, daemon=True, name="premarket-flush"
            ).start()

    # ── REST recalculation ────────────────────────────────────────────────────

    def _delayed_recalculate(self) -> None:
        """Wait for the stream to warm up before recalculating so priority_fn
        returns a meaningful gapper list."""
        log.info("PremarketStore: waiting 15s for stream to warm up before recalculation…")
        time.sleep(15)
        self._recalculate_all()

    def _recalculate_all(self) -> None:
        """
        Two-pass recalculation using 15-minute REST bars (4am–9:30am).

        Pass 1 — top 50 by change%:
          Fetches, saves to cache, bulk-applies to provider cache so the table
          shows correct Pre-mkt Vol for the most relevant stocks quickly.

        Pass 2 — all remaining symbols (from DailyStore / all_symbols_fn):
          Runs in the background; cache file is updated every
          _REMAINING_FLUSH_EVERY symbols so partial results survive a restart.
        """
        today = date.today()
        frm_ms, to_ms = _premarket_window_ms(today)

        priority: list[str] = (
            self._priority_fn()[:_PRIORITY_COUNT] if self._priority_fn else []
        )

        if not priority:
            log.info("PremarketStore: no symbols in stream cache yet — skipping recalculation")
            return

        # ── Pass 1: top 50 by change% ────────────────────────────────────────
        log.info("PremarketStore: [pass 1] fetching pre-market volume for top %d stocks…", len(priority))
        self._run_batch(priority, frm_ms, to_ms)
        self._flush_to_disk()
        self._bulk_apply()
        log.info("PremarketStore: [pass 1] done — table updated")

        # ── Pass 2: all remaining symbols ─────────────────────────────────────
        all_symbols: list[str] = self._all_symbols_fn() if self._all_symbols_fn else []
        priority_set = set(priority)
        remaining = [s for s in all_symbols if s not in priority_set and s.isalpha()]

        if not remaining:
            return

        log.info("PremarketStore: [pass 2] fetching pre-market volume for %d remaining stocks…", len(remaining))
        self._run_batch(remaining, frm_ms, to_ms, flush_every=_REMAINING_FLUSH_EVERY)
        self._flush_to_disk()
        log.info("PremarketStore: [pass 2] done — %d total symbols in cache", len(self._data))

    def _bulk_apply(self) -> None:
        """Push all computed values to the provider cache via on_symbol_updated."""
        if not self._on_symbol_updated:
            return
        with self._lock:
            snapshot = dict(self._data)
        for sym, vol in snapshot.items():
            self._on_symbol_updated(sym, vol)
        log.info("PremarketStore: applied %d pre-market volumes to provider cache", len(snapshot))

    def _run_batch(
        self,
        symbols: list[str],
        frm_ms: int,
        to_ms: int,
        flush_every: Optional[int] = None,
    ) -> None:
        if not symbols:
            return
        completed = 0
        with ThreadPoolExecutor(max_workers=_RECALC_WORKERS) as pool:
            futures = {
                pool.submit(self._fetch_15m_volume, sym, frm_ms, to_ms): sym
                for sym in symbols
            }
            for fut in as_completed(futures):
                sym = futures[fut]
                try:
                    vol = fut.result()
                    if vol > 0:
                        with self._lock:
                            self._data[sym] = vol
                        completed += 1
                        if flush_every and completed % flush_every == 0:
                            self._flush_to_disk()
                            log.debug("PremarketStore: flushed after %d symbols", completed)
                except Exception as exc:
                    log.debug("PremarketStore: recalc failed %s: %s", sym, exc)

    def _fetch_15m_volume(self, symbol: str, from_ms: int, to_ms: int) -> int:
        url = (
            _AGG_URL.format(sym=symbol, frm=from_ms, to=to_ms)
            + f"?apiKey={self._api_key}&adjusted=false&sort=asc&limit=50"
        )
        with urllib.request.urlopen(url, timeout=10) as resp:
            data = json.loads(resp.read().decode())
        return int(sum(bar.get("v", 0) for bar in data.get("results", [])))

    # ── Helpers ───────────────────────────────────────────────────────────────

    @staticmethod
    def _seconds_until_4am() -> float:
        now = _et_now()
        target = now.replace(hour=4, minute=0, second=0, microsecond=0)
        if now >= target:
            target += timedelta(days=1)
        return (target - now).total_seconds()
