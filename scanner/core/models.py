from dataclasses import dataclass, field
from datetime import datetime
from typing import Optional


@dataclass
class Quote:
    symbol: str
    last: float
    bid: float
    ask: float
    volume: int           # per-bar volume
    open: float           # 9:30 official open (0.0 during pre-market)
    high: float           # regular market high (0.0 during pre-market)
    low: float            # regular market low (0.0 during pre-market)
    prev_close: float
    accumulated_volume: int = 0
    premarket_volume: int = 0    # 4am–9:29 accumulated volume
    regular_volume: int = 0      # 9:30–16:00 accumulated volume (frozen at 4pm)
    prev_day_volume: float = 0.0 # previous day regular volume (for RVOL)
    market_cap: float = 0.0      # from reference API, refreshed daily
    float_shares: float = 0.0    # shares float, from reference API
    market_open: bool = False    # True once 9:30 bar arrives
    # ── After-hours fields (4pm+) ─────────────────────────────────────────────
    regular_close: float = 0.0   # last regular-session price (captured at 4pm)
    afterhours_open: float = 0.0 # open of the first 4pm bar
    afterhours_high: float = 0.0 # running high since 4pm
    afterhours_low: float = 0.0  # running low since 4pm
    afterhours_volume: int = 0   # accumulated volume since 4pm
    after_market: bool = False   # True once 4pm+ bar arrives
    timestamp: datetime = field(default_factory=datetime.now)

    @property
    def change(self) -> float:
        return self.last - self.prev_close

    @property
    def change_pct(self) -> float:
        if self.prev_close == 0:
            return 0.0
        return (self.change / self.prev_close) * 100

    @property
    def gap_pct(self) -> float:
        if self.prev_close == 0 or self.open == 0:
            return 0.0
        return ((self.open - self.prev_close) / self.prev_close) * 100

    @property
    def return_pct(self) -> float:
        if self.open == 0:
            return 0.0
        return ((self.last - self.open) / self.open) * 100

    @property
    def rvol(self) -> float:
        if self.prev_day_volume == 0 or self.regular_volume == 0:
            return 0.0
        return self.regular_volume / self.prev_day_volume

    @property
    def afterhours_gap_pct(self) -> float:
        """After-hours open vs regular-session close."""
        if self.regular_close == 0 or self.afterhours_open == 0:
            return 0.0
        return ((self.afterhours_open - self.regular_close) / self.regular_close) * 100

    @property
    def afterhours_range_pct(self) -> float:
        """After-hours high vs after-hours open."""
        if self.afterhours_open == 0 or self.afterhours_high == 0:
            return 0.0
        return ((self.afterhours_high - self.afterhours_open) / self.afterhours_open) * 100

    @property
    def afterhours_return_pct(self) -> float:
        """Current price vs after-hours open."""
        if self.afterhours_open == 0:
            return 0.0
        return ((self.last - self.afterhours_open) / self.afterhours_open) * 100

    @property
    def spread(self) -> float:
        return self.ask - self.bid

    @property
    def spread_pct(self) -> float:
        if self.last == 0:
            return 0.0
        return (self.spread / self.last) * 100


@dataclass
class Bar:
    symbol: str
    open: float
    high: float
    low: float
    close: float
    volume: int
    timestamp: datetime = field(default_factory=datetime.now)


@dataclass
class ScanResult:
    quote: Quote
    matched_filters: list[str] = field(default_factory=list)
    timestamp: datetime = field(default_factory=datetime.now)
