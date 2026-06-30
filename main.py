#!/usr/bin/env python3
import argparse
import sys
import threading
import time

from scanner.config.settings import Settings
from scanner.core.filters import ChangePctFilter, PriceFilter, VolumeFilter
from scanner.core.scanner import Scanner
from scanner.providers import get_provider
from scanner.providers.massive.asset_classes import SUPPORTED as MASSIVE_ASSET_CLASSES
from scanner.providers.massive.processors import get_processor
from scanner.core.scanner import _SORT_FIELDS, DEFAULT_SORT
from scanner.utils.logging_config import setup_logging


def _build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        description="Real-time multi-asset scanner",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )
    p.add_argument(
        "--provider",
        choices=["mock", "moomoo", "ibkr", "webull", "massive"],
        help="Data provider (overrides SCANNER_PROVIDER env var)",
    )
    p.add_argument(
        "--interval",
        type=float,
        help="Scan interval in seconds (overrides SCANNER_INTERVAL env var)",
    )
    p.add_argument(
        "--asset-class",
        nargs="+",
        choices=sorted(MASSIVE_ASSET_CLASSES),
        metavar="AC",
        dest="asset_class",
        help="Asset class(es) to scan: stocks crypto forex indices … (overrides MASSIVE_ASSET_CLASSES)",
    )
    p.add_argument(
        "--timeframe",
        choices=["seconds", "minutes"],
        help="Aggregate timeframe for Massive provider (overrides MASSIVE_TIMEFRAME env var)",
    )
    p.add_argument(
        "--top",
        type=int,
        metavar="N",
        help="Show only the top N results per asset class",
    )
    p.add_argument(
        "--sort",
        choices=["asc", "desc"],
        default="desc",
        help="Sort direction (default: desc)",
    )
    p.add_argument(
        "--sort-by",
        choices=list(_SORT_FIELDS),
        default=DEFAULT_SORT,
        dest="sort_by",
        help=f"Column to sort by (default: {DEFAULT_SORT})",
    )
    return p


# ── Per-asset-class filter definitions ───────────────────────────────────────
def _filters_for(asset_class: str) -> list:
    match asset_class:
        case "stocks":
            return []
        case "crypto":
            return [
                VolumeFilter(min_volume=1),
                ChangePctFilter(min_pct=-50.0, max_pct=50.0),
            ]
        case "indices":
            return [
                PriceFilter(min_price=1.0, max_price=100_000.0),
            ]
        case _:
            return [ChangePctFilter(min_pct=-50.0, max_pct=50.0)]
# ─────────────────────────────────────────────────────────────────────────────


def main() -> None:
    args = _build_parser().parse_args()
    settings = Settings.from_env()

    if args.provider:
        settings.provider = args.provider
    if args.interval:
        settings.scan_interval = args.interval
    if args.timeframe:
        settings.massive_timeframe = args.timeframe
    if args.asset_class:
        settings.massive_asset_classes = args.asset_class

    top_n: int | None = args.top
    ascending: bool = args.sort == "asc"
    sort_by: str = args.sort_by

    setup_logging(log_level=settings.log_level)

    asset_classes = settings.massive_asset_classes
    processors = {ac: get_processor(ac) for ac in asset_classes}
    providers = []
    scanners = []

    try:
        for ac in asset_classes:
            p = get_provider(settings.provider, settings, asset_class=ac)
            p.connect()
            providers.append(p)

            s = Scanner(
                provider=p,
                symbols=settings.symbols_for(ac),
                filters=_filters_for(ac),
                interval=settings.scan_interval,
                on_result=lambda results, ac=ac: None,
                limit=top_n,
                ascending=ascending,
                sort_by=sort_by,
            )
            scanners.append(s)

            threading.Thread(
                target=s.run, daemon=True, name=f"scanner-{ac}"
            ).start()

        while True:
            time.sleep(1)

    except KeyboardInterrupt:
        for s in scanners:
            s.stop()

    finally:
        for p in providers:
            p.disconnect()


if __name__ == "__main__":
    main()
