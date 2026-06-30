from datetime import datetime

from rich import box
from rich.table import Table

from .base import AssetProcessor
from ....core.models import ScanResult
from ....display.utils import colored, fmt_price, fmt_vol


class IndicesProcessor(AssetProcessor):
    def build_table(self, results: list[ScanResult], title: str) -> Table:
        ts = datetime.now().strftime("%H:%M:%S")
        t = Table(
            title=f"{title}  [{ts}]  {len(results)} match{'es' if len(results) != 1 else ''}",
            box=box.SIMPLE_HEAD, header_style="bold magenta",
            title_style="bold white", show_lines=False, pad_edge=False,
        )
        t.add_column("Index",   style="bold magenta", min_width=8)
        t.add_column("Value",   justify="right",      min_width=10)
        t.add_column("Chg",     justify="right",      min_width=8)
        t.add_column("Chg %",   justify="right",      min_width=8)
        t.add_column("Volume",  justify="right",      min_width=8)

        if not results:
            t.add_row(*["—"] * 5)
            return t

        for r in results:
            q = r.quote
            t.add_row(
                q.symbol,
                fmt_price(q.last, decimals=2),
                colored(q.change, ".2f"),
                colored(q.change_pct, ".2f", "%"),
                fmt_vol(q.accumulated_volume),
            )
        return t
