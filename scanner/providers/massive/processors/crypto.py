from datetime import datetime

from rich import box
from rich.table import Table

from .base import AssetProcessor
from ....core.models import ScanResult
from ....display.utils import colored, fmt_price, fmt_vol


class CryptoProcessor(AssetProcessor):
    def _initial_prev_close(self, msg: object, open_price: float) -> float:
        # Crypto is 24/7 — no official session open
        return open_price

    def build_table(self, results: list[ScanResult], title: str) -> Table:
        ts = datetime.now().strftime("%H:%M:%S")
        t = Table(
            title=f"{title}  [{ts}]  {len(results)} match{'es' if len(results) != 1 else ''}",
            box=box.SIMPLE_HEAD, header_style="bold yellow",
            title_style="bold white", show_lines=False, pad_edge=False,
        )
        t.add_column("Pair",    style="bold yellow", min_width=12)
        t.add_column("Last",    justify="right",     min_width=14)
        t.add_column("Chg %",   justify="right",     min_width=8)
        t.add_column("Volume",  justify="right",     min_width=8)
        t.add_column("Acc.Vol", justify="right",     min_width=8)

        if not results:
            t.add_row(*["—"] * 5)
            return t

        for r in results:
            q = r.quote
            t.add_row(
                q.symbol,
                fmt_price(q.last, decimals=4),
                colored(q.change_pct, ".2f", "%"),
                fmt_vol(q.volume),
                fmt_vol(q.accumulated_volume),
            )
        return t
