import logging
import time
from typing import Callable, Optional

from .filters import Filter
from .models import Quote, ScanResult
from ..providers.base import DataProvider

logger = logging.getLogger(__name__)


class Scanner:
    """
    Polls the provider for quotes and applies a list of filters.
    All filters must match for a symbol to appear in results.
    Results are delivered via the on_result callback.
    """

_SORT_FIELDS: dict[str, str] = {
    "gap":        "gap_pct",
    "change":     "change_pct",
    "return":     "return_pct",
    "price":      "last",
    "volume":     "regular_volume",
    "pmkt_vol":   "premarket_volume",
    "acc_vol":    "accumulated_volume",
    "rvol":       "rvol",
}

DEFAULT_SORT = "gap"


class Scanner:
    """
    Polls the provider for quotes and applies a list of filters.
    All filters must match for a symbol to appear in results.
    Results are delivered via the on_result callback.
    """

    def __init__(
        self,
        provider: DataProvider,
        symbols: list[str],
        filters: list[Filter],
        interval: float = 5.0,
        on_result: Optional[Callable[[list[ScanResult]], None]] = None,
        limit: Optional[int] = None,
        ascending: bool = False,
        sort_by: str = DEFAULT_SORT,
    ) -> None:
        if sort_by not in _SORT_FIELDS:
            raise ValueError(f"sort_by must be one of {list(_SORT_FIELDS)}, got {sort_by!r}")
        self._provider = provider
        self._symbols = symbols
        self._filters = filters
        self._interval = interval
        self._on_result = on_result
        self._running = False
        self._limit = limit
        self._ascending = ascending
        self._sort_attr = _SORT_FIELDS[sort_by]
        self._sort_by = sort_by

    def scan_once(self) -> list[ScanResult]:
        quotes = self._provider.get_quotes(self._symbols)
        results: list[ScanResult] = []
        for quote in quotes:
            matched = [f.name for f in self._filters if f.matches(quote)]
            if len(matched) == len(self._filters):
                results.append(ScanResult(quote=quote, matched_filters=matched))

        def _key(r: ScanResult) -> float:
            q = r.quote
            # gap sort: fall back to change_pct during pre-market (open not yet set)
            if self._sort_by == "gap" and not q.market_open:
                return q.change_pct
            return float(getattr(q, self._sort_attr, 0.0))

        results.sort(key=_key, reverse=not self._ascending)
        if self._limit is not None:
            results = results[: self._limit]
        return results

    def run(self) -> None:
        self._running = True
        logger.info(
            "Scanner started | provider=%s | symbols=%d | interval=%.1fs",
            self._provider.__class__.__name__,
            len(self._symbols),
            self._interval,
        )
        try:
            while self._running:
                try:
                    results = self.scan_once()
                    if self._on_result:
                        self._on_result(results)
                except Exception:
                    logger.exception("Error during scan cycle — retrying next interval")
                time.sleep(self._interval)
        except KeyboardInterrupt:
            pass
        finally:
            self.stop()

    def stop(self) -> None:
        if self._running:
            self._running = False
            logger.info("Scanner stopped")
