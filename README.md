# Real-Time Multi-Asset Scanner

Terminal-based live scanner that streams aggregate bars from the **Massive WebSocket API** and displays ranked results in a Rich table. Supports stocks, crypto, forex, indices, options, and futures simultaneously, each with its own table and processor.

---

## Table of Contents

1. [Architecture Overview](#architecture-overview)
2. [Project Structure](#project-structure)
3. [Core Concepts](#core-concepts)
   - [Data Flow](#data-flow)
   - [Quote Model](#quote-model)
   - [Filters](#filters)
   - [Scanner](#scanner)
   - [Processors](#processors)
4. [Persistent Stores](#persistent-stores)
   - [DailyStore — Previous-Day OHLC](#dailystore--previous-day-ohlc)
   - [PremarketStore — Pre-Market Volume](#premarketstore--pre-market-volume)
5. [Stocks Table — Column Reference](#stocks-table--column-reference)
6. [Display](#display)
7. [Signal Engine & CandleCache](#signal-engine--candlecache)
8. [Order Execution](#order-execution)
   - [OrderProvider interface](#orderprovider-interface)
   - [BracketRef](#bracketref)
   - [IBKROrderProvider](#ibkrorderprovider)
   - [MockOrderProvider](#mockorderprovider)
   - [OrderExecutor](#orderexecutor)
9. [Data Providers](#data-providers)
   - [MassiveProvider](#massiveprovider)
   - [IBKRProvider](#ibkrprovider)
10. [Configuration](#configuration)
11. [Running the Scanner](#running-the-scanner)
    - [Local (Python)](#local-python)
    - [Docker Compose](#docker-compose)
12. [CLI Reference](#cli-reference)
13. [Adding a New Asset Class](#adding-a-new-asset-class)

---

## Architecture Overview

```
Massive WebSocket
       │
       ▼
 MassiveProvider          ← one instance per asset class
  ├── DailyStore          ← prev-day OHLC (stocks only)
  ├── PremarketStore      ← 4am–9:29 volume (stocks only)
  └── AssetProcessor
        └── parse(msg)    ← converts raw bar → Quote
               │
               ▼
         provider._cache  ← dict[symbol, Quote]
               │
               ▼
           Scanner         ← polls cache every N seconds
            ├── apply filters
            ├── sort by column
            └── slice top-N
                   │
                   ▼
            LiveDisplay    ← Rich Live, one table per asset class
```

Multiple asset classes run as independent daemon threads under a single `LiveDisplay`.

---

## Project Structure

```
scanner/
├── main.py                          # Entry point, CLI args, thread orchestration
├── pyproject.toml
├── Dockerfile
├── docker-compose.yml
├── scripts/
│   └── refresh_daily_store.py       # Called by Ofelia cron at 3am
└── scanner/
    ├── config/
    │   └── settings.py              # Settings dataclass, reads from .env
    ├── core/
    │   ├── models.py                # Quote, Bar, ScanResult dataclasses
    │   ├── filters.py               # Filter base class + concrete filters
    │   └── scanner.py               # Scanner: poll → filter → sort → callback
    ├── display/
    │   ├── console.py               # LiveDisplay: Rich Live with stacked tables
    │   └── utils.py                 # colored(), fmt_price(), fmt_vol(), TableBuilder
    └── providers/
        ├── base.py                  # DataProvider ABC
        ├── mock/                    # Mock provider for testing
        └── massive/
            ├── asset_classes.py     # CONFIGS: market name + subscription prefixes
            ├── provider.py          # MassiveProvider: WebSocket + cache
            ├── daily_store.py       # DailyStore: prev-day OHLC persistence
            ├── premarket_store.py   # PremarketStore: 4am–9:29 volume persistence
            └── processors/
                ├── base.py          # AssetProcessor ABC: parse() + build_table()
                ├── stocks.py        # StocksProcessor
                ├── crypto.py        # CryptoProcessor
                ├── forex.py         # ForexProcessor
                └── indices.py       # IndicesProcessor
```

---

## Core Concepts

### Data Flow

1. **MassiveProvider** opens a WebSocket connection per asset class and subscribes to aggregate bars (`A.*` for stocks per-second, `AM.*` for per-minute, etc.).
2. Each incoming message is routed to `AssetProcessor.parse(msg, existing_quote)`. The processor converts the raw `EquityAgg` object into a typed `Quote`.
3. The `Quote` is stored in `provider._cache[symbol]`.
4. **Scanner** polls `get_quotes()` every `SCANNER_INTERVAL` seconds, applies all configured filters, sorts, slices to top-N, and fires the `on_result` callback.
5. **LiveDisplay** receives the results and re-renders the Rich table.

### Quote Model

`scanner/core/models.py`

| Field | Type | Description |
|---|---|---|
| `symbol` | str | Ticker |
| `last` | float | Current price (bar close) |
| `open` | float | Official 9:30 open (`0.0` during pre-market) |
| `high` | float | Regular-session high (`0.0` during pre-market) |
| `low` | float | Regular-session low (`0.0` during pre-market) |
| `prev_close` | float | Previous day close (from DailyStore, or fallback) |
| `volume` | int | Per-bar volume |
| `accumulated_volume` | int | Total daily volume (includes pre-market) |
| `premarket_volume` | int | Volume 4:00am–9:29am |
| `regular_volume` | int | Volume 9:30am+ (= accumulated − premarket) |
| `prev_day_volume` | float | Previous day regular volume (for RVOL) |
| `market_open` | bool | `True` once the 9:30 bar has been seen |

**Computed properties:**

| Property | Formula |
|---|---|
| `change_pct` | `(last − prev_close) / prev_close × 100` |
| `gap_pct` | `(open − prev_close) / prev_close × 100` (0 if pre-market) |
| `return_pct` | `(last − open) / open × 100` (0 if pre-market) |
| `rvol` | `regular_volume / prev_day_volume` (0 if either unknown) |

**Pre-market vs regular sessions:**
`official_open_price` in the Massive `EquityAgg` message is non-zero only after the 9:30 ET open. The processor uses this flag to separate the two sessions: before 9:30 `Quote.open`, `high`, and `low` are `0.0`, and `gap_pct`/`return_pct` return `0.0`.

### Filters

`scanner/core/filters.py` — composable with `&`, `|`, `~`.

| Class | Purpose |
|---|---|
| `PriceFilter(min, max)` | `min ≤ last ≤ max` |
| `VolumeFilter(min)` | accumulated volume ≥ min |
| `ChangePctFilter(min, max)` | change% within range |
| `GapUpFilter(min)` | gap% ≥ min |
| `GapDownFilter(max)` | gap% ≤ max |
| `AboveOpenFilter` | last > open |
| `BelowOpenFilter` | last < open |

Per-asset-class filters are defined in `_filters_for()` in `main.py`. Stocks currently run with **no filters** (all symbols pass) so nothing hides top gappers.

### Scanner

`scanner/core/scanner.py`

Polls `provider.get_quotes(symbols)` every `interval` seconds. Sorts results by a configurable column and optionally slices to top-N.

**Sort columns** (`--sort-by`):

| Key | Quote field |
|---|---|
| `gap` *(default)* | `gap_pct` (pre-market: `change_pct`) |
| `change` | `change_pct` |
| `return` | `return_pct` |
| `price` | `last` |
| `volume` | `regular_volume` |
| `pmkt_vol` | `premarket_volume` |
| `acc_vol` | `accumulated_volume` |
| `rvol` | `rvol` |

### Processors

Each asset class has an `AssetProcessor` subclass in `scanner/providers/massive/processors/`. It handles two responsibilities:

1. **`parse(msg, existing)`** — converts an `EquityAgg` WebSocket message into a `Quote`. Maintains running HOD/LOD across bars. Tracks the pre-market → regular session transition. Falls back to `min(official_open, bar_low)` as `prev_close` when the DailyStore has no entry for that symbol (no lookahead bias — only current bar data is used).

2. **`build_table(results, title)`** — renders a `rich.Table` from a list of `ScanResult` objects.

**OTC and special-character filtering** is applied at the provider level before any message reaches the processor:
```python
if getattr(msg, "otc", None):      return   # skip OTC stocks
if not sym.isalpha():              return   # skip BRK.A, warrants, etc.
```

---

## Persistent Stores

### DailyStore — Previous-Day OHLC

`scanner/providers/massive/daily_store.py`

Cache file: `.cache/prev_day_stocks.json`

```json
{
  "date": "2026-06-29",
  "symbols": {
    "AAPL": { "open": 275.0, "high": 285.95, "low": 274.21, "close": 283.78, "volume": 52341000 },
    "TSLA": { "open": 370.15, "high": 387.8,  "low": 368.6,  "close": 379.71, "volume": 31200000 }
  }
}
```

**Load strategy:**
- If file exists and is ≤ 3 days old → load from disk.
- Otherwise → fetch from Massive REST snapshot API:
  ```
  GET /v2/snapshot/locale/us/markets/stocks/tickers?apiKey=KEY[&tickers=A,B,C]
  ```
  When `SCANNER_SYMBOLS_STOCKS=*` the `&tickers=` parameter is omitted, returning the full market.
- Merge policy: `merged = {**existing, **fresh}` — symbols missing from today's API response keep their last known value.
- Background 3am scheduler refreshes the file daily (also triggered by Ofelia in Docker).

**Used for:**
- `prev_close` (drives `change_pct` and `gap_pct`)
- `prev_day_volume` (drives `rvol`)

---

### PremarketStore — Pre-Market Volume

`scanner/providers/massive/premarket_store.py`

Cache file: `.cache/premarket_volume.json`

```json
{
  "date": "2026-06-29",
  "symbols": {
    "UPC":  5890123,
    "AAPL": 1234567
  }
}
```

**Live path (scanner running from 4am):**
- Every incoming pre-market bar calls `update(sym, accumulated_volume)`.
- In-memory dict is flushed to disk every 60 seconds.
- At 9:30 ET the data is frozen (final flush, updates stop).

**Restart path (scanner stopped and restarted):**
1. `load()` reads today's cache instantly — Pre-mkt Vol column is populated immediately.
2. Background recalculation starts using **15-minute REST bars** (4am–9:30am):
   ```
   GET /v2/aggs/ticker/{sym}/range/15/minute/{from_ms}/{to_ms}?apiKey=KEY
   ```
3. Recalculation runs in **two priority batches**:
   - **Batch 1 — Top gappers first**: symbols sorted by `|gap_pct|` descending (top 50). These appear corrected in the table within seconds of restart.
   - **Batch 2 — Remaining symbols**: processed after batch 1 completes.
4. Each symbol that finishes calls `_apply_premarket_volume()` on the provider, which uses `dataclasses.replace()` to patch the cached `Quote` immediately (no waiting for full recalculation).

**Reset:** at 4:00am ET daily the cache is cleared for the new trading day.

---

## Stocks Table — Column Reference

| Column | Pre-market | Regular (9:30+) |
|---|---|---|
| **Symbol** | always | always |
| **Price** | current price | current price |
| **Chg %** | `(last − prev_close) / prev_close` | same |
| **Gap %** | `—` | `(open_9:30 − prev_close) / prev_close` |
| **Return %** | `—` | `(last − open_9:30) / open_9:30` |
| **Prev Close** | from DailyStore | from DailyStore |
| **Pre-mkt Vol** | accumulating live | frozen at 9:29 value |
| **Open** | `—` | official 9:30 open |
| **High** | `—` | running HOD (regular session) |
| **Low** | `—` | running LOD (regular session) |
| **Volume** | `—` | regular session volume only |
| **RVOL** | `—` | `regular_vol / prev_day_vol` |

**`prev_close` fallback** (when symbol is not in DailyStore):
```
prev_close = min(official_open_price, bar_low)
```
Both values are available at bar time — no lookahead bias. `gap_pct` and `return_pct` remain meaningful even for symbols missing from yesterday's snapshot.

---

## Display

`scanner/display/console.py` — `LiveDisplay` wraps a single `rich.Live` session.

- One `rich.Table` is rendered per active asset class.
- All tables are stacked vertically using `rich.console.Group`.
- Refreshes at 2 Hz.
- Color coding: `bright_green` for positive values, `bright_red` for negative, `white` for zero/missing.

---

## Signal Engine & CandleCache

`scanner/core/signal_engine.py` + `scanner/core/candle_cache.py`

The **CandleCache** listens to raw 5-minute bars from the data provider and maintains per-symbol OHLCV DataFrames enriched with SMA-9, SMA-200, and VWAP. It feeds the **SignalEngine** which evaluates strategy instances defined in `scanner/core/strategies.json`.

```
MassiveProvider.on_raw_bar(sym, O, H, L, C, V, end_ms)
       │
       ▼
 CandleCache
  ├── 5m + 15m DataFrames per symbol
  ├── SMA-9, SMA-200, VWAP computed incrementally
  └── SignalEngine.evaluate() called on every closed bar
              │
              ▼
        list[Signal]   ← stored in cache, read by OrderExecutor
```

**Signal fields:**

| Field | Description |
|---|---|
| `symbol` | Ticker |
| `strategy` | Instance name from strategies.json |
| `timeframe` | `"5m"` or `"15m"` |
| `type` | `"long"` or `"short"` |
| `entry_est` | Expected entry price (≈ open of next bar) |
| `sl` | Stop-loss price |
| `tp` | Take-profit price |
| `status` | `"pending"` (live bar) or `"launched"` (bar closed) |
| `trade_status` | `"open"` → `"tp"` / `"sl"` / `"eod"` |
| `exit_price` | Filled by OrderExecutor on close |
| `pnl_pct` | Realized P&L % filled by OrderExecutor on close |
| `max_hold_hours` | Force-close after N hours (0 = no limit) |

---

## Order Execution

Data and order providers are **fully independent** — any combination is valid:

```
DataProvider          OrderProvider
IBKRProvider    ←──── IBKROrderProvider   (same or different broker)
MassiveProvider ←──── IBKROrderProvider
MassiveProvider ←──── MockOrderProvider   (paper / testing)
```

### OrderProvider interface

`scanner/providers/order_base.py`

```python
class OrderProvider(ABC):
    def connect(self) -> None
    def disconnect(self) -> None
    def is_connected(self) -> bool

    def place_bracket(
        self,
        symbol:      str,
        action:      str,        # "BUY" | "SELL"
        quantity:    float,
        entry_type:  str,        # "MKT" | "LMT"
        entry_price: float,      # used when entry_type="LMT"
        sl:          float,
        tp:          float,
    ) -> BracketRef

    def refresh(self, ref: BracketRef, current_price: float = 0.0) -> BracketRef
    def cancel(self, ref: BracketRef) -> None
```

### BracketRef

`scanner/providers/order_base.py`

Represents a placed bracket order. Opaque to the executor — provider-specific state lives in `_internal`.

| Field | Description |
|---|---|
| `symbol` | Ticker |
| `action` | `"BUY"` or `"SELL"` |
| `quantity` | Shares |
| `entry_type` | `"MKT"` or `"LMT"` |
| `entry_price` | Requested entry |
| `sl` / `tp` | Stop and target prices |
| `fill_price` | Actual entry fill |
| `exit_price` | Actual exit fill |
| `status` | `pending` → `open` → `tp` / `sl` / `cancelled` |

### IBKROrderProvider

`scanner/providers/ibkr/order_provider.py`

Connects to IB Gateway / TWS with a dedicated `clientId` (default `2`, separate from the data provider). Uses native IB bracket orders: entry order (MKT or LMT) with attached TP (LimitOrder) and SL (StopOrder). IB handles the exit automatically; `refresh()` polls trade status to detect fills.

On `cancel()`: unfilled child orders are cancelled; if the entry was already filled, a MKT close order is placed to flatten the position.

```python
from scanner.providers.ibkr.order_provider import IBKROrderProvider

op = IBKROrderProvider(host="127.0.0.1", port=4002, client_id=2)
op.connect()
ref = op.place_bracket("NVDA", "SELL", 10, "MKT", entry_price=130.0, sl=133.0, tp=124.0)
```

### MockOrderProvider

`scanner/providers/mock/order_provider.py`

No broker connection required. Entry fills instantly at `entry_price`. TP/SL are evaluated on each `refresh(ref, current_price)` call by comparing `current_price` against the bracket levels. Used for testing and simulation.

```python
from scanner.providers.mock.order_provider import MockOrderProvider

op = MockOrderProvider()
op.connect()
ref = op.place_bracket("NVDA", "SELL", 10, "MKT", 130.0, sl=133.0, tp=124.0)
ref = op.refresh(ref, current_price=123.5)  # ref.status → "tp"
```

### OrderExecutor

`scanner/core/order_executor.py`

Polls `signal_source.get_signals()` every `interval` seconds and places bracket orders for new `"launched"` signals. Tracks open positions, enforces `max_hold_hours`, and updates `Signal.trade_status` / `exit_price` / `pnl_pct` on close.

**Simultaneity rule:** key is `(strategy_instance, timeframe, symbol)`. The same strategy instance + timeframe cannot re-enter a symbol while a position is open. Different strategies (or different timeframes of the same strategy) can hold simultaneous positions on the same symbol.

```python
from scanner.core.order_executor import OrderExecutor
from scanner.providers.ibkr.order_provider import IBKROrderProvider

order_provider = IBKROrderProvider(host="127.0.0.1", port=4002, client_id=2)
order_provider.connect()

executor = OrderExecutor(
    order_provider=order_provider,
    signal_source=candle_cache,    # get_signals() → list[Signal]
    quote_source=massive_provider, # get_quote(symbol) → Quote (for refresh + timeout price)
    quantity=10,
    entry_type="MKT",              # "MKT" | "LMT"
    interval=2.0,
    on_fill=None,                  # optional callback(Signal, BracketRef) on close
)
executor.start()   # non-blocking daemon thread
# ...
executor.stop()
```

**Constructor parameters:**

| Parameter | Default | Description |
|---|---|---|
| `order_provider` | — | Any `OrderProvider` implementation |
| `signal_source` | — | Object with `get_signals() → list[Signal]` |
| `quote_source` | — | Any `DataProvider` (for live prices) |
| `quantity` | — | Shares per trade (sizing passed from outside) |
| `entry_type` | `"MKT"` | `"MKT"` or `"LMT"` (uses `signal.entry_est` for LMT) |
| `interval` | `2.0` | Seconds between poll cycles |
| `on_fill` | `None` | Optional `(Signal, BracketRef) → None` callback on close |

---

## Data Providers

### MassiveProvider

`scanner/providers/massive/provider.py`

Streams aggregate bars via Massive WebSocket. Supports wildcard subscription (`*`) for stocks. See [Architecture Overview](#architecture-overview) for full data flow.

### IBKRProvider

`scanner/providers/ibkr/provider.py`

Streams live market data from IB Gateway or TWS via **ib_async**. Requires `pip install ib_async`.

- Runs an asyncio event loop in a background daemon thread.
- Calls `reqMktData()` for each symbol — IB pushes tick updates (bid, ask, last, volume, open, high, low, prev_close).
- Cache is refreshed by a background poll coroutine every 0.5 s.
- On-demand subscription: symbols not listed at construction time are subscribed automatically on first `get_quote()` call.
- `get_bars(symbol, limit)` fetches historical 5-minute bars via `reqHistoricalData`.

**Does not support wildcard `*`** — explicit symbol list required.

**Limitations vs MassiveProvider:**

| Field | MassiveProvider | IBKRProvider |
|---|---|---|
| `premarket_volume` | Computed via REST API | Approximated (volume before open tick) |
| `prev_day_volume` (RVOL) | From DailyStore | Not available |
| `market_cap` / `float` | From reference API | Not available |
| Wildcard subscription | Yes (`*`) | No |

**Port reference:**

| Application | Paper | Live |
| --- | --- | --- |
| IB Gateway | `4002` | `4001` |
| TWS | `7497` | `7496` |

```python
from scanner.providers.ibkr.provider import IBKRProvider

dp = IBKRProvider(host="127.0.0.1", port=4002, client_id=1, symbols=["AAPL", "NVDA"])
dp.connect()
quote = dp.get_quote("AAPL")
bars  = dp.get_bars("AAPL", limit=50)
dp.disconnect()
```

**`.env` for IBKR data provider:**

```env
SCANNER_PROVIDER=ibkr
IBKR_HOST=127.0.0.1
IBKR_PORT=4002
IBKR_CLIENT_ID=1
SCANNER_SYMBOLS_STOCKS=AAPL,NVDA,TSLA
```

---

## Configuration

All settings are read from `.env` (loaded via `python-dotenv`).

```env
# Provider
SCANNER_PROVIDER=massive

# Massive credentials
MASSIVE_API_KEY=your_key_here
MASSIVE_FEED=delayed          # delayed | realtime
MASSIVE_TIMEFRAME=seconds     # seconds | minutes

# Asset classes to scan (comma-separated)
MASSIVE_ASSET_CLASSES=stocks,crypto

# Symbol lists (* = full market for stocks)
SCANNER_SYMBOLS_STOCKS=*
SCANNER_SYMBOLS_CRYPTO=BTC-USD,ETH-USD,SOL-USD,XRP-USD
SCANNER_SYMBOLS_FOREX=EUR-USD,GBP-USD,USD-JPY
SCANNER_SYMBOLS_INDICES=I:SPX,I:DJI,I:NDX,I:VIX

# Scan polling interval (seconds)
SCANNER_INTERVAL=5

# Logging level
LOG_LEVEL=INFO
```

---

## Running the Scanner

### Local (Python)

**Install:**
```bash
pip install -e ".[massive]"
```

**Basic run (stocks + crypto, all market, sorted by gap):**
```bash
python main.py --provider massive
```

**Top 20 gappers:**
```bash
python main.py --provider massive --top 20
```

**Top 5 biggest losers:**
```bash
python main.py --provider massive --top 5 --sort asc
```

**Sorted by RVOL, top 30:**
```bash
python main.py --provider massive --top 30 --sort-by rvol
```

**Sorted by pre-market volume:**
```bash
python main.py --provider massive --sort-by pmkt_vol
```

**Only stocks, per-minute bars:**
```bash
python main.py --provider massive --asset-class stocks --timeframe minutes
```

**Stocks + indices simultaneously:**
```bash
python main.py --provider massive --asset-class stocks indices
```

**Manually refresh the previous-day OHLC cache:**
```bash
python scripts/refresh_daily_store.py
```

---

### Docker Compose

**Build and start:**
```bash
docker compose up --build
```

**Start in background:**
```bash
docker compose up -d --build
```

**Attach to the live scanner terminal:**
```bash
docker compose attach scanner
```

**Tail logs:**
```bash
docker compose logs -f scanner
docker compose logs -f ofelia      # cron job logs
```

**Stop:**
```bash
docker compose down
```

The `ofelia` service runs the daily 3am OHLC refresh automatically. The shared `cache_data` Docker volume persists `.cache/` across restarts so the scanner always starts warm.

**Cron schedule:**
```
0 0 3 * * 1-5   →   3:00 AM ET, Monday–Friday
```

---

## CLI Reference

```
python main.py [OPTIONS]

Options:
  --provider {mock,massive,moomoo,ibkr,webull}
      Data provider. Overrides SCANNER_PROVIDER env var.

  --asset-class {stocks,crypto,forex,indices,options,futures} [...]
      One or more asset classes to scan simultaneously.
      Overrides MASSIVE_ASSET_CLASSES env var.

  --timeframe {seconds,minutes}
      Aggregate bar timeframe. Overrides MASSIVE_TIMEFRAME env var.

  --interval FLOAT
      Scan polling interval in seconds. Overrides SCANNER_INTERVAL env var.

  --top N
      Show only the top N results per asset class table.

  --sort {asc,desc}
      Sort direction. Default: desc (highest first).

  --sort-by {gap,change,return,price,volume,pmkt_vol,acc_vol,rvol}
      Column to sort by. Default: gap.
      During pre-market, "gap" automatically falls back to "change".
```

---

## Adding a New Asset Class

1. **Add config** in `scanner/providers/massive/asset_classes.py`:
   ```python
   "my_class": AssetClassConfig(
       market="MyMarket",
       seconds_prefix="XYZ", seconds_event="XYZ",
       minutes_prefix="XYZM", minutes_event="XYZM",
   ),
   ```

2. **Create a processor** in `scanner/providers/massive/processors/my_class.py` that inherits `AssetProcessor` and implements `build_table()`.

3. **Register it** in `scanner/providers/massive/processors/__init__.py`:
   ```python
   from .my_class import MyClassProcessor
   _REGISTRY["my_class"] = MyClassProcessor
   ```

4. **Add default symbols** in `scanner/config/settings.py` (`_DEFAULT_SYMBOLS`) and the `symbols_my_class` field + env var handling in `from_env()`.

5. **Optionally define filters** for the new class in `_filters_for()` in `main.py`.
