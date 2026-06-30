from datetime import datetime

from rich import box
from rich.table import Table

from .base import AssetProcessor
from ....core.models import ScanResult
from ....display.utils import colored, fmt_vol


class ForexProcessor(AssetProcessor):
    def _initial_prev_close(self, msg: object, open_price: float) -> float:
        # Forex is 24/5 — no official open, use bar open
        return open_price

    def build_table(self, results: list[ScanResult], title: str) -> Table:
        ts = datetime.now().strftime("%H:%M:%S")
        t = Table(
            title=f"{title}  [{ts}]  {len(results)} match{'es' if len(results) != 1 else ''}",
            box=box.SIMPLE_HEAD, header_style="bold blue",
            title_style="bold white", show_lines=False, pad_edge=False,
        )
        t.add_column("Pair",    style="bold blue", min_width=10)
        t.add_column("Rate",    justify="right",   min_width=10)
        t.add_column("Chg",     justify="right",   min_width=10)
        t.add_column("Chg %",   justify="right",   min_width=8)
        t.add_column("Volume",  justify="right",   min_width=8)

        if not results:
            t.add_row(*["—"] * 5)
            return t

        for r in results:
            q = r.quote
            t.add_row(
                q.symbol,
                f"{q.last:.5f}" if q.last != 0 else "—",
                colored(q.change, ".5f"),
                colored(q.change_pct, ".3f", "%"),
                fmt_vol(q.accumulated_volume),
            )
        return t
