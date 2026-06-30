#!/usr/bin/env python3
"""
Refresh previous-day OHLC cache for stocks.
Called by Ofelia at 3:00 AM ET on weekdays.
"""
import logging
import sys

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(levelname)-8s  %(message)s",
    datefmt="%H:%M:%S",
    stream=sys.stdout,
)

from scanner.config.settings import Settings
from scanner.providers.massive.daily_store import DailyStore

settings = Settings.from_env()

store = DailyStore(
    api_key=settings.massive_api_key,
    symbols=settings.symbols_for("stocks"),
)
store.refresh()
