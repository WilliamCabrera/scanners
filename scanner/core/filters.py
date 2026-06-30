from abc import ABC, abstractmethod
from .models import Quote


class Filter(ABC):
    """Base class for all scan conditions. Supports & | ~ composition."""

    @abstractmethod
    def matches(self, quote: Quote) -> bool: ...

    @property
    @abstractmethod
    def name(self) -> str: ...

    def __and__(self, other: "Filter") -> "AndFilter":
        return AndFilter(self, other)

    def __or__(self, other: "Filter") -> "OrFilter":
        return OrFilter(self, other)

    def __invert__(self) -> "NotFilter":
        return NotFilter(self)


class AndFilter(Filter):
    def __init__(self, *filters: Filter):
        self._filters = filters

    @property
    def name(self) -> str:
        return " AND ".join(f.name for f in self._filters)

    def matches(self, quote: Quote) -> bool:
        return all(f.matches(quote) for f in self._filters)


class OrFilter(Filter):
    def __init__(self, *filters: Filter):
        self._filters = filters

    @property
    def name(self) -> str:
        return " OR ".join(f.name for f in self._filters)

    def matches(self, quote: Quote) -> bool:
        return any(f.matches(quote) for f in self._filters)


class NotFilter(Filter):
    def __init__(self, inner: Filter):
        self._inner = inner

    @property
    def name(self) -> str:
        return f"NOT({self._inner.name})"

    def matches(self, quote: Quote) -> bool:
        return not self._inner.matches(quote)


# ── Concrete filters ──────────────────────────────────────────────────────────

class PriceFilter(Filter):
    def __init__(self, min_price: float = 0.0, max_price: float = float("inf")):
        self._min = min_price
        self._max = max_price

    @property
    def name(self) -> str:
        return f"Price[{self._min:.2f}-{self._max:.2f}]"

    def matches(self, quote: Quote) -> bool:
        return self._min <= quote.last <= self._max


class VolumeFilter(Filter):
    def __init__(self, min_volume: int = 0):
        self._min = min_volume

    @property
    def name(self) -> str:
        return f"Vol>={self._min:,}"

    def matches(self, quote: Quote) -> bool:
        vol = quote.accumulated_volume if quote.accumulated_volume > 0 else quote.volume
        return vol >= self._min


class ChangePctFilter(Filter):
    def __init__(
        self,
        min_pct: float = float("-inf"),
        max_pct: float = float("inf"),
    ):
        self._min = min_pct
        self._max = max_pct

    @property
    def name(self) -> str:
        return f"Chg%[{self._min:.1f}%-{self._max:.1f}%]"

    def matches(self, quote: Quote) -> bool:
        return self._min <= quote.change_pct <= self._max


class GapUpFilter(Filter):
    def __init__(self, min_gap_pct: float = 0.0):
        self._min = min_gap_pct

    @property
    def name(self) -> str:
        return f"GapUp>={self._min:.1f}%"

    def matches(self, quote: Quote) -> bool:
        return quote.gap_pct >= self._min


class GapDownFilter(Filter):
    def __init__(self, max_gap_pct: float = 0.0):
        self._max = max_gap_pct

    @property
    def name(self) -> str:
        return f"GapDown<={self._max:.1f}%"

    def matches(self, quote: Quote) -> bool:
        return quote.gap_pct <= self._max


class SpreadFilter(Filter):
    def __init__(self, max_spread_pct: float = float("inf")):
        self._max = max_spread_pct

    @property
    def name(self) -> str:
        return f"Spread<={self._max:.3f}%"

    def matches(self, quote: Quote) -> bool:
        return quote.spread_pct <= self._max


class AboveOpenFilter(Filter):
    @property
    def name(self) -> str:
        return "Last>Open"

    def matches(self, quote: Quote) -> bool:
        return quote.last > quote.open


class BelowOpenFilter(Filter):
    @property
    def name(self) -> str:
        return "Last<Open"

    def matches(self, quote: Quote) -> bool:
        return quote.last < quote.open
