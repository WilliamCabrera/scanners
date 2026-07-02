"""
Massive WebSocket provider — aggregate bars per asset class.

pip install massive
"""
import logging
import threading
from typing import Optional

from ..base import DataProvider
from ...core.models import Quote
from .asset_classes import CONFIGS, SUPPORTED
from .processors import AssetProcessor, get_processor

log = logging.getLogger(__name__)


class MassiveProvider(DataProvider):
    def __init__(
        self,
        api_key: str,
        asset_class: str = "stocks",
        symbols: Optional[list[str]] = None,
        feed: str = "delayed",
        timeframe: str = "seconds",
    ) -> None:
        if asset_class not in SUPPORTED:
            raise ValueError(f"asset_class must be one of {sorted(SUPPORTED)}, got {asset_class!r}")
        if timeframe not in ("seconds", "minutes"):
            raise ValueError(f"timeframe must be 'seconds' or 'minutes', got {timeframe!r}")

        cfg = CONFIGS[asset_class]
        self._api_key = api_key
        self._asset_class = asset_class
        self._symbols = symbols or ["*"]
        self._feed_name = feed
        self._timeframe = timeframe
        self._ev = cfg.seconds_event if timeframe == "seconds" else cfg.minutes_event
        self._prefix = cfg.seconds_prefix if timeframe == "seconds" else cfg.minutes_prefix
        self._market_name = cfg.market

        # Stocks: DailyStore (prev-day OHLC) + PremarketStore (4am-9:29 volume)
        self._daily_store = None
        self._premarket_store = None
        if asset_class == "stocks":
            from .daily_store import DailyStore
            from .premarket_store import PremarketStore
            self._daily_store = DailyStore(
                api_key=api_key,
                symbols=self._symbols,
                on_today_hl_ready=self._apply_today_hl,
                on_ah_hl_ready=self._apply_ah_hl,
            )
            self._premarket_store = PremarketStore(
                api_key=api_key,
                symbols=self._symbols,
                priority_fn=self._top_change_symbols,
                all_symbols_fn=self._all_known_symbols,
                on_symbol_updated=self._apply_premarket_volume,
            )
            self._processor: AssetProcessor = get_processor(
                asset_class,
                daily_store=self._daily_store,
                premarket_store=self._premarket_store,
            )
        else:
            self._processor = get_processor(asset_class)

        self._cache: dict[str, Quote] = {}
        self._client = None
        self._thread: Optional[threading.Thread] = None
        self._connected = False
        # Optional callback fired on every accepted bar: (sym, open, high, low, close, volume, end_ms)
        self._on_raw_bar = None

    # ── DataProvider interface ────────────────────────────────────────────────

    def connect(self) -> None:
        from massive import WebSocketClient  # type: ignore[import]
        from massive.websocket.models import Feed, Market  # type: ignore[import]

        if self._daily_store:
            self._daily_store.load()
        if self._premarket_store:
            self._premarket_store.load()

        feed = Feed.RealTime if self._feed_name == "realtime" else Feed.Delayed
        market = getattr(Market, self._market_name)
        self._client = WebSocketClient(
            api_key=self._api_key,
            feed=feed,
            market=market,
        )

        subs = (
            [f"{self._prefix}.*"]
            if "*" in self._symbols
            else [f"{self._prefix}.{s}" for s in self._symbols]
        )
        self._client.subscribe(*subs)

        self._thread = threading.Thread(
            target=self._client.run,
            args=(self._handle_messages,),
            daemon=True,
            name=f"massive-{self._asset_class}",
        )
        self._thread.start()
        self._connected = True
        log.info(
            "Massive connected | asset_class=%s | feed=%s | timeframe=%s | subs=%s",
            self._asset_class, self._feed_name, self._timeframe, subs,
        )

    def disconnect(self) -> None:
        if self._client and hasattr(self._client, "close"):
            try:
                self._client.close()
            except Exception:
                pass
        self._client = None
        self._connected = False

    def is_connected(self) -> bool:
        return self._connected and bool(self._thread and self._thread.is_alive())

    def get_quote(self, symbol: str) -> Quote:
        return self._cache[symbol]

    def set_raw_bar_callback(self, fn) -> None:
        """Register a callback fired for every accepted WebSocket bar.

        Signature: fn(sym, open, high, low, close, volume, end_ms)
        """
        self._on_raw_bar = fn

    def get_quotes(self, symbols: list[str]) -> list[Quote]:
        if "*" in symbols:
            return list(self._cache.values())
        return [self._cache[s] for s in symbols if s in self._cache]

    # ── Pre-market recalculation helpers ─────────────────────────────────────

    def _top_change_symbols(self) -> list[str]:
        """Return symbols sorted by |change_pct| DESC — used as recalculation priority."""
        quotes = list(self._cache.values())
        quotes.sort(key=lambda q: abs(q.change_pct), reverse=True)
        return [q.symbol for q in quotes]

    def _all_known_symbols(self) -> list[str]:
        """All symbols from DailyStore + active stream cache, deduplicated."""
        daily: set[str] = set(self._daily_store._data.keys()) if self._daily_store else set()
        active: set[str] = set(self._cache.keys())
        return list(daily | active)

    def _apply_premarket_volume(self, symbol: str, volume: int) -> None:
        """Patch a cached Quote with a freshly recalculated pre-market volume.
        Called from the PremarketStore background thread for each completed symbol."""
        from dataclasses import replace
        quote = self._cache.get(symbol)
        if quote is None or not quote.market_open:
            return
        regular_vol = max(0, quote.accumulated_volume - volume)
        self._cache[symbol] = replace(
            quote,
            premarket_volume=volume,
            regular_volume=regular_vol,
        )

    def _apply_ah_hl(self, ah_data: dict[str, tuple[float, float, float, int]]) -> None:
        """Patch cached AH quotes with true H/L/open from 1h bars fetched in background."""
        from dataclasses import replace
        patched = 0
        for sym, (ah_high, ah_low, ah_open, ah_vol) in ah_data.items():
            quote = self._cache.get(sym)
            if quote is None or not quote.after_market:
                continue
            new_high = max(quote.afterhours_high, ah_high) if ah_high > 0 else quote.afterhours_high
            new_low  = min(quote.afterhours_low,  ah_low)  if ah_low  > 0 else quote.afterhours_low
            new_open = ah_open if ah_open > 0 else quote.afterhours_open
            new_vol  = max(quote.afterhours_volume, ah_vol)
            self._cache[sym] = replace(
                quote,
                afterhours_high=new_high,
                afterhours_low=new_low,
                afterhours_open=new_open,
                afterhours_volume=new_vol,
            )
            patched += 1
        log.debug("AH 1h H/L applied to %d cached quotes", patched)

    def _apply_today_hl(self, today_hl: dict[str, tuple[float, float]]) -> None:
        """Correct cached quotes whose H/L was seeded before the snapshot arrived.
        Called by DailyStore after _today_hl is populated. Patches any market-open
        quote whose stored high is below the true day high (or low above true day low)."""
        from dataclasses import replace
        for sym, (snap_high, snap_low) in today_hl.items():
            quote = self._cache.get(sym)
            if quote is None or not quote.market_open:
                continue
            new_high = max(quote.high, snap_high) if snap_high > 0 else quote.high
            new_low  = min(quote.low,  snap_low)  if snap_low  > 0 else quote.low
            if new_high != quote.high or new_low != quote.low:
                self._cache[sym] = replace(quote, high=new_high, low=new_low)
        log.debug("DailyStore today H/L applied to %d cached quotes", len(today_hl))

    # ── Message handling ──────────────────────────────────────────────────────

    def _handle_messages(self, msgs: list) -> None:
        for msg in msgs:
            try:
                self._update_cache(msg)
            except Exception as exc:
                log.debug("Skipped message: %s", exc)

    def _update_cache(self, msg: object) -> None:
        if getattr(msg, "event_type", None) != self._ev:
            return
        if getattr(msg, "otc", None):
            return
        sym: str = getattr(msg, "symbol", "") or ""
        if not sym or not sym.isalpha():
            return
        quote = self._processor.parse(msg, self._cache.get(sym))
        if quote:
            self._cache[sym] = quote
            if not quote.market_open and self._premarket_store:
                self._premarket_store.update(sym, quote.accumulated_volume)
            log.debug("%s %s: close=%.4f", self._ev, sym, quote.last)
            if self._on_raw_bar:
                end_ms = getattr(msg, "end_timestamp", None)
                if end_ms:
                    try:
                        self._on_raw_bar(
                            sym,
                            float(getattr(msg, "open",   quote.last)),
                            float(getattr(msg, "high",   quote.last)),
                            float(getattr(msg, "low",    quote.last)),
                            quote.last,
                            quote.volume,
                            int(end_ms),
                        )
                    except Exception as exc:
                        log.debug("raw_bar callback error for %s: %s", sym, exc)
