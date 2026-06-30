"""
Mock provider — generates realistic random market data.
No external connection required; use for development and testing.
"""
import random
from datetime import datetime
from typing import Optional

from ..base import DataProvider
from ...core.models import Quote


_UNIVERSE: dict[str, dict] = {
    "AAPL":  {"prev_close": 195.00, "avg_vol": 55_000_000},
    "TSLA":  {"prev_close": 245.00, "avg_vol": 90_000_000},
    "NVDA":  {"prev_close": 875.00, "avg_vol": 45_000_000},
    "AMD":   {"prev_close": 165.00, "avg_vol": 50_000_000},
    "META":  {"prev_close": 510.00, "avg_vol": 15_000_000},
    "AMZN":  {"prev_close": 190.00, "avg_vol": 35_000_000},
    "MSFT":  {"prev_close": 415.00, "avg_vol": 20_000_000},
    "GOOGL": {"prev_close": 175.00, "avg_vol": 25_000_000},
    "SPY":   {"prev_close": 530.00, "avg_vol": 80_000_000},
    "QQQ":   {"prev_close": 460.00, "avg_vol": 40_000_000},
    "MSTR":  {"prev_close":  18.50, "avg_vol":  8_000_000},
    "PLTR":  {"prev_close":  23.00, "avg_vol": 30_000_000},
    "RIVN":  {"prev_close":  12.00, "avg_vol": 22_000_000},
    "SOFI":  {"prev_close":   8.50, "avg_vol": 18_000_000},
    "GME":   {"prev_close":  17.00, "avg_vol": 12_000_000},
}


class MockProvider(DataProvider):
    def __init__(self, seed: Optional[int] = None) -> None:
        self._rng = random.Random(seed)
        self._connected = False
        self._state: dict[str, dict] = {}

    def connect(self) -> None:
        for sym, data in _UNIVERSE.items():
            pc = data["prev_close"]
            self._state[sym] = {
                "prev_close": pc,
                "open": round(pc * self._rng.uniform(0.97, 1.03), 2),
                "last": round(pc * self._rng.uniform(0.95, 1.05), 2),
                "high": 0.0,
                "low": float("inf"),
                "volume": 0,
            }
        self._connected = True

    def disconnect(self) -> None:
        self._connected = False

    def is_connected(self) -> bool:
        return self._connected

    def get_quote(self, symbol: str) -> Quote:
        self._tick(symbol)
        s = self._state[symbol]
        last = s["last"]
        half_spread = last * self._rng.uniform(0.00005, 0.0003)
        return Quote(
            symbol=symbol,
            last=last,
            bid=round(last - half_spread, 4),
            ask=round(last + half_spread, 4),
            volume=s["volume"],
            open=s["open"],
            high=s["high"],
            low=s["low"],
            prev_close=s["prev_close"],
            timestamp=datetime.now(),
        )

    def get_quotes(self, symbols: list[str]) -> list[Quote]:
        return [self.get_quote(s) for s in symbols if s in self._state]

    def _tick(self, symbol: str) -> None:
        s = self._state[symbol]
        move = self._rng.gauss(0, 0.0012)
        s["last"] = round(max(0.01, s["last"] * (1 + move)), 2)
        s["high"] = round(max(s["high"], s["last"]), 2)
        s["low"] = round(min(s["low"], s["last"]), 2)
        s["volume"] += self._rng.randint(500, 80_000)
