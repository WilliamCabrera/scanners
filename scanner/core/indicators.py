"""Technical indicators — same logic as backtester_api/app/utils/indicators.py."""
from __future__ import annotations

import pandas as pd


def compute_vwap(df: pd.DataFrame) -> pd.Series:
    """Intraday VWAP resetting each ET day. Expects `time` column as Unix seconds UTC."""
    d = df.copy()
    d["_tp"]      = (d["high"] + d["low"] + d["close"]) / 3
    d["_tp_vol"]  = d["_tp"] * d["volume"]
    d["_date_et"] = (
        pd.to_datetime(d["time"], unit="s", utc=True)
        .dt.tz_convert("America/New_York")
        .dt.date
    )
    d["_cum_tp_vol"] = d.groupby("_date_et")["_tp_vol"].cumsum()
    d["_cum_vol"]    = d.groupby("_date_et")["volume"].cumsum()
    return (d["_cum_tp_vol"] / d["_cum_vol"]).rename("vwap")


def compute_sma(df: pd.DataFrame, window: int, column: str = "close") -> pd.Series:
    return df[column].rolling(window, min_periods=1).mean().rename(f"sma_{window}")
