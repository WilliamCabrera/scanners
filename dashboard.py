"""
Real-time multi-asset scanner — Streamlit dashboard.

Run:
    streamlit run dashboard.py

The scanner runs once in a background thread (shared across all browser sessions).
The dashboard auto-refreshes every REFRESH_INTERVAL seconds via st.fragment.
"""
import json
import logging
import threading
import time
from dataclasses import asdict
from datetime import datetime
from pathlib import Path
from typing import Optional
from zoneinfo import ZoneInfo

import pandas as pd
import streamlit as st

from scanner.config.settings import Settings
from scanner.core.models import Quote, ScanResult
from scanner.core.scanner import Scanner, _SORT_FIELDS, DEFAULT_SORT
from scanner.providers import get_provider

log = logging.getLogger(__name__)

_ET = ZoneInfo("America/New_York")

# ── Session snapshot paths ────────────────────────────────────────────────────

_CACHE_DIR    = Path(__file__).parent / ".cache"
_REGULAR_SNAP = _CACHE_DIR / "snapshot_regular.json"
_AH_SNAP      = _CACHE_DIR / "snapshot_afterhours.json"

# ── Module-level shared state (one scanner, N browser sessions) ───────────────

_state_lock   = threading.Lock()
_results:      dict[str, list[ScanResult]] = {}
_last_update:  dict[str, datetime]         = {}
_started       = threading.Event()

_snap_regular:    list[ScanResult]   = []
_snap_regular_ts: Optional[datetime] = None
_snap_ah:         list[ScanResult]   = []
_snap_ah_ts:      Optional[datetime] = None

REFRESH_INTERVAL = 5


# ── Snapshot serialization ────────────────────────────────────────────────────

def _quote_to_dict(q: Quote) -> dict:
    d = asdict(q)
    d["timestamp"] = q.timestamp.isoformat()
    return d


def _dict_to_quote(d: dict) -> Quote:
    d = dict(d)
    try:
        d["timestamp"] = datetime.fromisoformat(d["timestamp"])
    except Exception:
        d["timestamp"] = datetime.now()
    return Quote(**d)


def _save_snapshot(results: list[ScanResult], path: Path) -> None:
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        payload = {
            "saved_at": datetime.now().isoformat(),
            "quotes": [_quote_to_dict(r.quote) for r in results],
        }
        with open(path, "w") as f:
            json.dump(payload, f)
        log.info("Snapshot saved: %d quotes → %s", len(results), path)
    except Exception as exc:
        log.warning("Failed to save snapshot %s: %s", path, exc)


def _load_snapshot(path: Path) -> tuple[list[ScanResult], Optional[datetime]]:
    try:
        with open(path) as f:
            payload = json.load(f)
        saved_at = datetime.fromisoformat(payload["saved_at"])
        results  = [ScanResult(quote=_dict_to_quote(d)) for d in payload["quotes"]]
        return results, saved_at
    except Exception:
        return [], None


# ── Background snapshot saver (every 60 s) ───────────────────────────────────

def _snapshot_saver_loop() -> None:
    global _snap_regular, _snap_regular_ts, _snap_ah, _snap_ah_ts
    while True:
        time.sleep(60)
        with _state_lock:
            stocks = list(_results.get("stocks", []))
        if not stocks:
            continue

        regular = [r for r in stocks if not r.quote.after_market]
        ah      = [r for r in stocks if r.quote.after_market]

        if regular:
            _save_snapshot(regular, _REGULAR_SNAP)
            _snap_regular    = regular
            _snap_regular_ts = datetime.now()
        if ah:
            _save_snapshot(ah, _AH_SNAP)
            _snap_ah    = ah
            _snap_ah_ts = datetime.now()


# ── Scanner bootstrap ─────────────────────────────────────────────────────────

def _on_result(asset_class: str, results: list[ScanResult]) -> None:
    with _state_lock:
        _results[asset_class] = results
        _last_update[asset_class] = datetime.now()


def _get_live() -> tuple[dict[str, list[ScanResult]], dict[str, datetime]]:
    with _state_lock:
        return (
            {ac: list(r) for ac, r in _results.items()},
            dict(_last_update),
        )


def _start_scanner(settings: Settings) -> None:
    global _snap_regular, _snap_regular_ts, _snap_ah, _snap_ah_ts
    if _started.is_set():
        return
    _started.set()

    _snap_regular, _snap_regular_ts = _load_snapshot(_REGULAR_SNAP)
    _snap_ah,      _snap_ah_ts      = _load_snapshot(_AH_SNAP)

    threading.Thread(target=_snapshot_saver_loop, daemon=True, name="snapshot-saver").start()

    for ac in settings.massive_asset_classes:
        p = get_provider(settings.provider, settings, asset_class=ac)
        p.connect()
        s = Scanner(
            provider=p,
            symbols=settings.symbols_for(ac),
            filters=[],
            interval=settings.scan_interval,
            on_result=lambda results, ac=ac: _on_result(ac, results),
        )
        threading.Thread(target=s.run, daemon=True, name=f"scanner-{ac}").start()


# ── Cell formatters ───────────────────────────────────────────────────────────
# NOTE: Streamlit ignores Styler.format() when rendering via Arrow.  All cell
# values must be pre-formatted as strings here; the Styler is used only for
# colours (which Streamlit DOES honour).

def _p(v: float | None) -> str:
    """Price: 2 dp normally, 4 dp for sub-dollar."""
    if v is None or v == 0:
        return "—"
    return f"{v:.4f}" if 0 < v < 1.0 else f"{v:.2f}"


def _pct(v: float | None) -> str:
    """Percentage with sign and 2 dp."""
    if v is None:
        return "—"
    return f"{v:+.2f}%"


def _vol(v: int | float | None) -> str:
    """Volume in K / M."""
    if not v:
        return "—"
    if v >= 1_000_000:
        return f"{v / 1_000_000:.1f}M"
    if v >= 1_000:
        return f"{v / 1_000:.0f}K"
    return str(int(v))


def _rvol(v: float | None) -> str:
    if not v:
        return "—"
    return f"{v:.2f}x"


def _large(v: float | None) -> str:
    """Market cap / float shares with T/B/M suffix."""
    if not v:
        return "—"
    if v >= 1e12:
        return f"{v / 1e12:.2f}T"
    if v >= 1e9:
        return f"{v / 1e9:.2f}B"
    if v >= 1e6:
        return f"{v / 1e6:.0f}M"
    return str(int(v))


# ── DataFrame builders (all cells are pre-formatted strings) ──────────────────

def _stocks_df(results: list[ScanResult]) -> pd.DataFrame:
    rows = []
    for r in results:
        q   = r.quote
        mkt = q.market_open
        rows.append({
            "Symbol":       q.symbol,
            "Price":        _p(q.last),
            "Chg %":        _pct(q.change_pct)    if q.prev_close              else "—",
            "Gap %":        _pct(q.gap_pct)        if mkt                       else "—",
            "Return %":     _pct(q.return_pct)     if mkt                       else "—",
            "Prev Close":   _p(q.prev_close)       if q.prev_close              else "—",
            "Pre-mkt Vol":  _vol(q.premarket_volume),
            "Open":         _p(q.open)             if mkt                       else "—",
            "High":         _p(q.high)             if mkt                       else "—",
            "Low":          _p(q.low)              if mkt                       else "—",
            "Volume":       _vol(q.regular_volume) if mkt                       else "—",
            "RVOL":         _rvol(q.rvol)          if mkt and q.rvol > 0        else "—",
            "Mkt Cap":      _large(q.market_cap),
            "Float":        _large(q.float_shares),
        })
    return pd.DataFrame(rows) if rows else pd.DataFrame()


def _afterhours_df(results: list[ScanResult]) -> pd.DataFrame:
    rows = []
    for r in results:
        q = r.quote
        if not q.after_market:
            continue
        has_rc  = q.regular_close > 0
        has_aho = q.afterhours_open > 0
        rows.append({
            "Symbol":      q.symbol,
            "Price":       _p(q.last),
            "AH Range %":  _pct(q.afterhours_range_pct)  if has_aho and q.afterhours_high else "—",
            "AH Return %": _pct(q.afterhours_return_pct) if has_aho             else "—",
            "Reg Close":   _p(q.regular_close)           if has_rc              else "—",
            "AH Open":     _p(q.afterhours_open)         if has_aho             else "—",
            "AH High":     _p(q.afterhours_high)         if q.afterhours_high   else "—",
            "AH Low":      _p(q.afterhours_low)          if q.afterhours_low    else "—",
            "AH Vol":      _vol(q.afterhours_volume),
            "Mkt Cap":     _large(q.market_cap),
            "Float":       _large(q.float_shares),
        })
    return pd.DataFrame(rows) if rows else pd.DataFrame()


def _generic_df(results: list[ScanResult]) -> pd.DataFrame:
    rows = []
    for r in results:
        q = r.quote
        rows.append({
            "Symbol": q.symbol,
            "Last":   _p(q.last),
            "Chg %":  _pct(q.change_pct) if q.prev_close else "—",
            "Volume": _vol(q.accumulated_volume),
        })
    return pd.DataFrame(rows) if rows else pd.DataFrame()


# ── Styler: colours only (values already formatted above) ────────────────────

_PCT_COLS    = ["Chg %", "Gap %", "Return %"]
_AH_PCT_COLS = ["AH Range %", "AH Return %"]


def _style_df(df: pd.DataFrame) -> "pd.io.formats.style.Styler":
    """Apply green / red text to percentage columns.  No number formatting here
    — values are already strings pre-formatted in the df-builder functions."""

    def pct_color(val) -> str:
        if not val or val == "—":
            return ""
        try:
            # Strip sign / % from pre-formatted strings like "+93.24%" or "-8.06%"
            f = float(str(val).replace("%", ""))
            return "color: #00cc66" if f > 0 else "color: #ff4455" if f < 0 else "color: #888"
        except (TypeError, ValueError):
            return ""

    all_pct = list(dict.fromkeys(_PCT_COLS + _AH_PCT_COLS))
    present  = [c for c in all_pct if c in df.columns]
    styled   = df.style
    if present:
        styled = styled.map(pct_color, subset=present)
    return styled.hide(axis="index")


# ── Sort ──────────────────────────────────────────────────────────────────────

_SORT_LABEL = {
    "gap":      "Gap %",
    "change":   "Chg %",
    "return":   "Return %",
    "price":    "Price",
    "volume":   "Volume",
    "pmkt_vol": "Pre-mkt Vol",
    "acc_vol":  "Acc. Vol",
    "rvol":     "RVOL",
}

# Inline sort pills for each table — label → sort key used by _apply_sort
_STOCKS_SORT_PILLS: dict[str, str] = {
    "Gap %":       "gap",
    "Chg %":       "change",
    "Return %":    "return",
    "Pre-mkt Vol": "pmkt_vol",
    "Volume":      "volume",
    "RVOL":        "rvol",
}
_AH_SORT_PILLS: dict[str, str] = {
    "Range %":  "ah_range",
    "Return %": "ah_return",
    "AH Vol":   "ah_vol",
}


_AH_SORT_ATTR: dict[str, str] = {
    "ah_range":  "afterhours_range_pct",
    "ah_return": "afterhours_return_pct",
    "ah_vol":    "afterhours_volume",
}


def _apply_sort(
    results: list[ScanResult],
    sort_by: str,
    ascending: bool,
    top_n: int,
) -> list[ScanResult]:
    attr = _SORT_FIELDS.get(sort_by) or _AH_SORT_ATTR.get(sort_by, "gap_pct")

    def key(r: ScanResult) -> float:
        q = r.quote
        if sort_by == "gap" and not q.market_open:
            return q.change_pct
        return float(getattr(q, attr, 0.0))

    out = sorted(results, key=key, reverse=not ascending)
    return out[:top_n] if top_n > 0 else out


# ── Per-asset render ──────────────────────────────────────────────────────────

def _show_table(df: pd.DataFrame, key: str = "") -> None:
    st.dataframe(
        _style_df(df),
        use_container_width=True,
        height=min(40 + len(df) * 35, 600),
        key=key or None,
    )


def _stale_label(saved_at: Optional[datetime]) -> str:
    return saved_at.strftime("Last saved %Y-%m-%d %H:%M ET") if saved_at else ""


def _read_sort_state(
    pills: dict[str, str],
    state_key: str,
    default_label: str,
    ascending: bool,
) -> tuple[str, bool]:
    """Read current sort params from session state without rendering anything."""
    label  = st.session_state.get(state_key, default_label) or default_label
    sort_by = pills.get(label, list(pills.values())[0])
    asc    = st.session_state.get(f"{state_key}_asc", ascending)
    return sort_by, asc


def _render_sort_pills(
    pills: dict[str, str],
    state_key: str,
    default_label: str,
    ascending: bool,
) -> None:
    """Render sort pills UI. Call _read_sort_state first to get the current params."""
    col_pills, col_dir = st.columns([5, 1])
    with col_pills:
        st.pills(
            "Sort",
            options=list(pills),
            default=st.session_state.get(state_key, default_label),
            key=state_key,
            label_visibility="collapsed",
        )
    with col_dir:
        st.toggle(
            "Asc",
            value=st.session_state.get(f"{state_key}_asc", ascending),
            key=f"{state_key}_asc",
        )


def _sort_ah(
    results: list[ScanResult],
    ascending: bool,
    top_n: int,
    sort_by: str = "ah_range",
) -> list[ScanResult]:
    attr = _AH_SORT_ATTR.get(sort_by, "afterhours_range_pct")
    out = sorted(results, key=lambda r: float(getattr(r.quote, attr, 0.0)), reverse=not ascending)
    return out[:top_n] if top_n > 0 else out


def _render_stocks(
    results: list[ScanResult],
    updated: datetime | None,
    sort_by: str,
    ascending: bool,
    top_n: int,
) -> None:
    ts = updated.strftime("%H:%M:%S") if updated else "—"

    regular_all = [r for r in results if not r.quote.after_market]
    ah_all      = [r for r in results if r.quote.after_market]

    # ── Regular-session table ─────────────────────────────────────────────────
    default_reg_label = next((k for k, v in _STOCKS_SORT_PILLS.items() if v == sort_by), "Gap %")
    reg_sort_by, reg_asc = _read_sort_state(
        _STOCKS_SORT_PILLS, "stocks_sort", default_reg_label, ascending,
    )
    sorted_regular = _apply_sort(regular_all, reg_sort_by, reg_asc, top_n)

    _render_sort_pills(_STOCKS_SORT_PILLS, "stocks_sort", default_reg_label, ascending)

    if sorted_regular:
        st.markdown(f"**STOCKS** — {len(sorted_regular)} results — _{ts}_")
        df = _stocks_df(sorted_regular)
        if not df.empty:
            _show_table(df, key="tbl_stocks_regular")
    elif _snap_regular:
        st.markdown("**STOCKS** — last session snapshot")
        st.caption(_stale_label(_snap_regular_ts))
        df = _stocks_df(_apply_sort(_snap_regular, reg_sort_by, reg_asc, top_n))
        if not df.empty:
            _show_table(df, key="tbl_stocks_regular")
    else:
        st.markdown("**STOCKS**")
        st.info("Waiting for data…")

    # ── After-hours table ─────────────────────────────────────────────────────
    ah_sort_by, ah_asc = _read_sort_state(_AH_SORT_PILLS, "ah_sort", "Range %", ascending)
    sorted_ah = _sort_ah(ah_all, ah_asc, top_n, ah_sort_by)

    if sorted_ah or _snap_ah:
        st.markdown("---")
        _render_sort_pills(_AH_SORT_PILLS, "ah_sort", "Range %", ascending)

        if sorted_ah:
            st.markdown(f"**AFTER HOURS** — {len(sorted_ah)} results — _{ts}_")
            df_ah = _afterhours_df(sorted_ah)
            if not df_ah.empty:
                _show_table(df_ah, key="tbl_stocks_ah")
        elif _snap_ah:
            st.markdown("**AFTER HOURS** — last session snapshot")
            st.caption(_stale_label(_snap_ah_ts))
            df_ah = _afterhours_df(_sort_ah(_snap_ah, ah_asc, top_n, ah_sort_by))
            if not df_ah.empty:
                _show_table(df_ah, key="tbl_stocks_ah")


def _render_asset_class(
    ac: str,
    results: list[ScanResult],
    updated: datetime | None,
    sort_by: str,
    ascending: bool,
    top_n: int,
) -> None:
    if ac == "stocks":
        _render_stocks(results, updated, sort_by, ascending, top_n)
        return

    sorted_results = _apply_sort(results, sort_by, ascending, top_n)
    ts = updated.strftime("%H:%M:%S") if updated else "—"
    st.markdown(f"**{ac.upper()}** — {len(sorted_results)} results — _{ts}_")

    if not sorted_results:
        st.info("Waiting for data…")
        return

    df = _generic_df(sorted_results)
    if not df.empty:
        _show_table(df, key=f"tbl_{ac}")


# ── Main app ──────────────────────────────────────────────────────────────────

def main() -> None:
    st.set_page_config(
        page_title="Scanner",
        page_icon="📈",
        layout="wide",
        initial_sidebar_state="expanded",
    )

    settings = Settings.from_env()
    _start_scanner(settings)

    with st.sidebar:
        st.title("📈 Scanner")
        st.caption(f"Provider: **{settings.provider.upper()}**")
        st.divider()

        sort_by = st.selectbox(
            "Sort by",
            options=list(_SORT_LABEL),
            format_func=lambda k: _SORT_LABEL[k],
            index=list(_SORT_LABEL).index(DEFAULT_SORT),
        )
        ascending = st.radio(
            "Direction",
            options=["Descending", "Ascending"],
            horizontal=True,
        ) == "Ascending"

        top_n = st.number_input(
            "Top N (0 = all)",
            min_value=0,
            max_value=500,
            value=50,
            step=10,
        )

        st.divider()
        asset_classes = st.multiselect(
            "Asset classes",
            options=settings.massive_asset_classes,
            default=settings.massive_asset_classes,
        )

        st.divider()
        st.caption(f"Refresh every {REFRESH_INTERVAL}s")
        st.caption(f"Scan interval: {settings.scan_interval}s")

    st.title("Real-Time Scanner")

    @st.fragment(run_every=REFRESH_INTERVAL)
    def live_tables() -> None:
        snapshot, updates = _get_live()

        if not snapshot:
            st.info("⏳ Connecting to stream…")
            return

        for ac in asset_classes:
            results = snapshot.get(ac, [])
            updated = updates.get(ac)
            with st.container(border=True):
                _render_asset_class(ac, results, updated, sort_by, ascending, int(top_n))
            st.divider()

    live_tables()


if __name__ == "__main__":
    main()
