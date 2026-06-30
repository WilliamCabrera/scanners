from datetime import datetime

from rich import box
from rich.console import Console, Group
from rich.live import Live
from rich.table import Table

from ..core.models import ScanResult
from .utils import TableBuilder, colored, fmt_vol

_console = Console()


def _generic_table(results: list[ScanResult], title: str) -> Table:
    """Fallback table used when no processor is registered for an asset class."""
    ts = datetime.now().strftime("%H:%M:%S")
    t = Table(
        title=f"{title}  [{ts}]  {len(results)} match{'es' if len(results) != 1 else ''}",
        box=box.SIMPLE_HEAD, header_style="bold cyan",
        title_style="bold white", show_lines=False, pad_edge=False,
    )
    t.add_column("Symbol",  style="bold white", min_width=10)
    t.add_column("Chg",     justify="right",    min_width=8)
    t.add_column("Chg %",   justify="right",    min_width=8)
    t.add_column("Volume",  justify="right",    min_width=8)
    t.add_column("Acc.Vol", justify="right",    min_width=8)

    if not results:
        t.add_row(*["—"] * 5)
        return t

    for r in results:
        q = r.quote
        t.add_row(
            q.symbol,
            colored(q.change, ".2f"),
            colored(q.change_pct, ".2f", "%"),
            fmt_vol(q.volume),
            fmt_vol(q.accumulated_volume),
        )
    return t


class LiveDisplay:
    """Live-updating console display with one table per asset class."""

    def __init__(
        self,
        title: str = "Scanner",
        asset_classes: list[str] | None = None,
        processors: dict[str, TableBuilder] | None = None,
    ) -> None:
        self._base_title = title
        self._asset_classes = asset_classes or ["default"]
        self._processors: dict[str, TableBuilder] = processors or {}
        self._results: dict[str, list[ScanResult]] = {ac: [] for ac in self._asset_classes}
        self._live = Live(
            self._render(),
            console=_console,
            refresh_per_second=2,
            screen=False,
        )

    def update(self, asset_class: str, results: list[ScanResult]) -> None:
        self._results[asset_class] = results
        self._live.update(self._render())

    def _render(self) -> Group:
        tables = []
        for ac, results in self._results.items():
            table_title = f"{self._base_title}  [{ac.upper()}]"
            proc = self._processors.get(ac)
            tables.append(
                proc.build_table(results, table_title)
                if proc
                else _generic_table(results, table_title)
            )
        return Group(*tables)

    def __enter__(self) -> "LiveDisplay":
        self._live.start()
        return self

    def __exit__(self, *_: object) -> None:
        self._live.stop()
