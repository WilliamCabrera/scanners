from typing import Protocol

from rich.table import Table
from rich.text import Text

from ..core.models import ScanResult


class TableBuilder(Protocol):
    """Any object that can render a list of ScanResults into a Rich Table."""

    def build_table(self, results: list[ScanResult], title: str) -> Table: ...


# ── Formatting helpers ────────────────────────────────────────────────────────

def colored(value: float, fmt: str, suffix: str = "") -> Text:
    if value == 0:
        return Text("—", style="white")
    sign = "+" if value > 0 else ""
    color = "bright_green" if value > 0 else "bright_red"
    return Text(f"{sign}{value:{fmt}}{suffix}", style=color)


def fmt_price(value: float, decimals: int = 2) -> str:
    return f"{value:.{decimals}f}" if value != 0 else "—"


def fmt_vol(volume: int) -> str:
    if volume == 0:
        return "—"
    if volume >= 1_000_000:
        return f"{volume / 1_000_000:.1f}M"
    if volume >= 1_000:
        return f"{volume / 1_000:.0f}K"
    return str(volume)


def fmt_large(value: float) -> str:
    """Format large numbers (market cap, float shares) with T/B/M suffix."""
    if not value:
        return "—"
    if value >= 1e12:
        return f"{value / 1e12:.2f}T"
    if value >= 1e9:
        return f"{value / 1e9:.2f}B"
    if value >= 1e6:
        return f"{value / 1e6:.0f}M"
    return str(int(value))
