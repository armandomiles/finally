# Market Data Interface

This document defines the single Python API that all FinAlly code uses to get stock prices. If `MASSIVE_API_KEY` is set, prices come from the Massive API (see `MASSIVE_API.md`). Otherwise they come from the built-in simulator (see `MARKET_SIMULATOR.md`). Code outside `app/market/` never knows which one is running.

**Status:** implemented in `backend/app/market/`. This doc is the contract. §9 records the fixes made to bring the Massive implementation in line with the research in `MASSIVE_API.md`; all of them are done.

---

## 1. Design principles

1. **Producers push, consumers read.** One background producer (the simulator or the Massive poller) writes into a shared `PriceCache`. Every consumer (the SSE stream, trade execution, portfolio valuation, the watchlist API and the LLM context) reads from that cache. Nothing downstream calls the producer to get a price.
2. **One interface, two strategies.** `MarketDataSource` is an ABC with two implementations. A factory picks one at startup from the environment.
3. **One output type.** `PriceUpdate` is the only data structure that leaves the market package.
4. **Never block the event loop.** Producers run as asyncio tasks. The synchronous Massive client is called through `asyncio.to_thread`.
5. **Never crash the app.** Producer loops log errors and keep going. A failed poll leaves the last known prices in the cache.

```
                 ┌──────────────── create_market_data_source(cache) ───────────────┐
                 │   MASSIVE_API_KEY set?                                            │
                 ▼ yes                                                    no ▼       │
       MassiveDataSource                                       SimulatorDataSource   │
       (REST poller: LIVE or EOD mode)                          (GBM, 500 ms ticks)   │
                 └──────────────────────┬───────────────────────────────┘           │
                                        ▼ cache.update(ticker, price, ts)
                                   PriceCache  (thread-safe, latest price per ticker, version counter)
                     ┌──────────────┬───┴──────────┬───────────────────┐
                     ▼              ▼              ▼                   ▼
          /api/stream/prices   trade execution   portfolio value   watchlist / chat context
```

---

## 2. Public API (`from app.market import ...`)

| Name | Kind | Purpose |
|---|---|---|
| `PriceUpdate` | frozen dataclass | Immutable price snapshot for one ticker |
| `PriceCache` | class | Thread-safe store of the latest `PriceUpdate` per ticker |
| `MarketDataSource` | ABC | Lifecycle and ticker-set control for a producer |
| `create_market_data_source(cache)` | function | Factory that returns an **unstarted** source |
| `create_stream_router(cache)` | function | FastAPI router for `GET /api/stream/prices` (SSE) |

### 2.1 `PriceUpdate` (`models.py`)

```python
@dataclass(frozen=True, slots=True)
class PriceUpdate:
    ticker: str
    price: float                 # rounded to 2 dp
    previous_price: float        # price at the previous update (first update: == price)
    timestamp: float             # Unix seconds (float)

    @property
    def change(self) -> float: ...           # price - previous_price, 4 dp
    @property
    def change_percent(self) -> float: ...   # vs previous_price, 4 dp; 0.0 if previous_price == 0
    @property
    def direction(self) -> str: ...          # "up" | "down" | "flat"
    def to_dict(self) -> dict: ...           # JSON/SSE payload, includes the derived fields
```

`change` and `direction` are **tick-to-tick**: they compare against the previous update, not the previous close. The frontend uses them for the green/red flash. Day change and P&L are computed elsewhere from `avg_cost`.

### 2.2 `PriceCache` (`cache.py`)

```python
class PriceCache:
    def update(self, ticker: str, price: float, timestamp: float | None = None) -> PriceUpdate
    def get(self, ticker: str) -> PriceUpdate | None
    def get_price(self, ticker: str) -> float | None
    def get_all(self) -> dict[str, PriceUpdate]       # shallow copy, safe to iterate
    def remove(self, ticker: str) -> None
    @property
    def version(self) -> int                          # bumps on every update()
    def __len__(self) -> int
    def __contains__(self, ticker: str) -> bool
```

- A `threading.Lock` makes it safe for asyncio code and for `to_thread` workers.
- It holds only the latest value per ticker, so memory is O(number of tickers). Price **history** (sparklines) builds up in the frontend from the SSE stream.
- `version` lets the SSE generator skip sending when nothing has changed.

### 2.3 `MarketDataSource` (`interface.py`)

```python
class MarketDataSource(ABC):
    async def start(self, tickers: list[str]) -> None     # call once; seeds cache before returning
    async def stop(self) -> None                          # idempotent; no cache writes afterwards
    async def add_ticker(self, ticker: str) -> None       # no-op if present
    async def remove_ticker(self, ticker: str) -> None    # no-op if absent; also cache.remove()
    def get_tickers(self) -> list[str]
```

Every implementation must follow these rules:

| Rule | Why |
|---|---|
| `start()` returns only after the cache holds a price for every known ticker (or the first fetch failed) | The first SSE event and the first trade must not see an empty cache |
| `add_ticker()` gives the ticker a cache price as soon as possible: immediately for the simulator, at the next poll or via a one-off fetch for Massive | A user can buy a ticker right after adding it |
| Tickers are normalised with `ticker.strip().upper()` | The API layer and the LLM may send lowercase symbols |
| Unknown symbols must not break the loop | Massive simply leaves them out. The simulator invents a price for them |
| Producer exceptions are logged and swallowed | A background task must never die silently |

### 2.4 Factory (`factory.py`)

```python
def create_market_data_source(price_cache: PriceCache) -> MarketDataSource:
    api_key = os.environ.get("MASSIVE_API_KEY", "").strip()
    if api_key:
        return MassiveDataSource(api_key=api_key, price_cache=price_cache,
                                 poll_interval=_env_float("MASSIVE_POLL_INTERVAL", 15.0))
    return SimulatorDataSource(price_cache=price_cache)
```

| Env var | Default | Effect |
|---|---|---|
| `MASSIVE_API_KEY` | empty | If non-empty, use Massive. Otherwise use the simulator |
| `MASSIVE_POLL_INTERVAL` *(optional)* | `15` | Seconds between snapshot polls in LIVE mode. Paid tiers can use `2`–`5`. Invalid, zero, negative or non-finite values fall back to 15 with a warning |

---

## 3. How consumers use it

### 3.1 App lifecycle (FastAPI lifespan)

```python
from contextlib import asynccontextmanager
from fastapi import FastAPI
from app.market import PriceCache, create_market_data_source, create_stream_router

price_cache = PriceCache()                         # module level: the router needs it at import time

@asynccontextmanager
async def lifespan(app: FastAPI):
    source = create_market_data_source(price_cache)
    await source.start(load_tracked_tickers())     # watchlist ∪ tickers with open positions
    app.state.price_cache = price_cache
    app.state.market = source
    try:
        yield
    finally:
        await source.stop()

app = FastAPI(lifespan=lifespan)
app.include_router(create_stream_router(price_cache))
```

### 3.2 Reading prices

```python
cache: PriceCache = request.app.state.price_cache

price = cache.get_price("AAPL")                 # float | None
upd   = cache.get("AAPL")                       # PriceUpdate | None
snap  = cache.get_all()                         # dict[str, PriceUpdate]

# Portfolio valuation
value = cash + sum(p.quantity * (cache.get_price(p.ticker) or p.avg_cost) for p in positions)
```

### 3.3 Trade execution

```python
price = cache.get_price(ticker)
if price is None:
    raise HTTPException(409, f"No price available for {ticker} yet")
# fill at `price`
```

### 3.4 Watchlist changes

```python
# POST /api/watchlist
await request.app.state.market.add_ticker(ticker)

# DELETE /api/watchlist/{ticker}
if not user_holds_position(ticker):          # keep pricing held tickers
    await request.app.state.market.remove_ticker(ticker)
```

The **tracked set** is the watchlist **plus** any ticker with an open position. Without that, removing a held ticker from the watchlist would leave its position without a price. The route layer enforces this. The market package only does what it is told.

### 3.5 SSE

`GET /api/stream/prices` sends `data: {"AAPL": {...PriceUpdate.to_dict()}, ...}` about every 500 ms, but only when `cache.version` has changed. It also sends `retry: 1000` so the browser reconnects automatically. In Massive LIVE mode the cache changes every poll interval, so between polls the stream is quiet. That is correct behaviour.

---

## 4. Simulator implementation (summary)

`SimulatorDataSource` wraps `GBMSimulator` and calls `step()` every 0.5 s, writing each price to the cache. `add_ticker` seeds the cache immediately. See `MARKET_SIMULATOR.md` for the full design.

---

## 5. Massive implementation

`MassiveDataSource` has two **modes**. The mode is picked automatically from what the key is entitled to (see `MASSIVE_API.md` §2), as described in §5.1.

| Mode | When | Endpoint | Cycle | Calls/cycle |
|---|---|---|---|---|
| `LIVE` | Snapshot allowed (Starter/Developer/Advanced) | `get_snapshot_all(STOCKS, tickers)` | `poll_interval` (15 s default) | 1 |
| `EOD` | Snapshot returns `NOT_AUTHORIZED` (Basic/free) | `get_grouped_daily_aggs(date)`, filtered to tickers | 15 min | 1, plus 1 per holiday step-back |

### 5.1 Implementation (`backend/app/market/massive_client.py`)

The code is the source of truth. This is how it works:

- **`snapshot_price(snap)`** gets `(price, unix_seconds)` from a `TickerSnapshot`. It tries the last trade first (`sip_timestamp`, in ns), then the minute bar (ms), then the day bar (`updated`, in ns), then the previous day's close, and returns `None` if there's no price.
- **Mode detection costs no extra call.** The source starts in `LIVE`. The first snapshot poll, which `start()` runs immediately, doubles as the probe: if it raises `BadResponse` containing `NOT_AUTHORIZED` or "not entitled", the source switches to `MassiveMode.EOD` and runs the grouped-daily poll in the same cycle. Any other error, such as a bad key, keeps `LIVE` mode and counts as a failed poll. `source.mode` exposes the current mode.
- **EOD poll:** `get_grouped_daily_aggs(day)` for the previous weekday. An empty result (a holiday) steps back one more weekday, up to `EOD_LOOKBACK_DAYS = 5`. Results are filtered to the watched tickers.
- **`add_ticker` in EOD mode** calls `get_previous_close_agg(ticker)` once, so the new ticker has a price before the next poll, which can be 15 minutes away. Failures are logged and ignored.
- **Intervals:** `poll_interval` in LIVE mode (from `MASSIVE_POLL_INTERVAL`, default 15 s) and `EOD_INTERVAL = 900 s` in EOD mode. After a failed poll the delay doubles, up to `MAX_BACKOFF = 300 s`, and resets on the next success (`_next_delay`).
- **`RESTClient(api_key=..., retries=0)`:** the poll loop owns retries.
- **Tickers** are uppercased, stripped and de-duplicated on `start`, `add_ticker` and `remove_ticker`, in both sources.

### 5.2 Behaviour notes
- **EOD mode prices barely move.** They change once a day, so the UI shows static prices with no flashes. That is expected on a free key. Log a startup warning, and expose `source.mode` if the UI should show a "delayed/EOD" badge.
- **Starter/Developer data is 15 minutes delayed.** It still ticks during market hours.
- **Outside market hours** snapshots return the last price, and the cache stays steady. Optionally, call `get_market_status()` and stretch the interval while the market is `closed`.
- The snapshot `updated` and `sip_timestamp` values are **nanoseconds**, and aggregate `t` values are **milliseconds**. Always convert to epoch seconds before calling `cache.update`.

---

## 6. Optional extensions (not required by PLAN.md)

| Extension | Shape | Use |
|---|---|---|
| Day change | add `previous_close: float \| None = None` to `PriceUpdate`. Massive fills it from `prev_day.close`; the simulator uses the seed price | "+1.2% today" column |
| Mode/source label | `MarketDataSource.source_name -> str` (`"simulator"`, `"massive-live"`, `"massive-eod"`) | UI badge, `/api/health` |
| Historical bars | `async def get_history(ticker, days) -> list[Bar]`, via Massive `get_aggs` or a simulator random walk | Detail chart prefill |

Any extension must keep `to_dict()` backwards compatible, adding keys only.

---

## 7. File layout

```
backend/app/market/
  __init__.py        # re-exports the public API (§2)
  models.py          # PriceUpdate
  cache.py           # PriceCache
  interface.py       # MarketDataSource ABC
  factory.py         # create_market_data_source
  simulator.py       # GBMSimulator + SimulatorDataSource
  seed_prices.py     # simulator constants
  massive_client.py  # MassiveDataSource (+ snapshot_price helper, MassiveMode)
  stream.py          # SSE router
```

---

## 8. Testing contract

| Area | Test |
|---|---|
| Factory | key unset or whitespace → simulator; key set → Massive (no network at construction) |
| Cache | first update is flat; direction and change; remove; version increments; thread-safety smoke test |
| Simulator source | `start` seeds cache; `add_ticker` immediately priced; `remove_ticker` evicts; `stop` idempotent |
| Massive LIVE | `snapshot_price` uses **real model objects**: `TickerSnapshot.from_dict(sample_json)`, not `MagicMock`. Covers the ns→s conversion and each fallback level |
| Massive EOD | `BadResponse('{"status":"NOT_AUTHORIZED"...}')` from the probe → EOD mode; an empty grouped-daily result steps back a day |
| Errors | a poll that raises leaves the cache unchanged and the loop alive; back-off grows and then resets |

Build Massive test fixtures with `TickerSnapshot.from_dict(...)` and `GroupedDailyAgg.from_dict(...)`. A `MagicMock` accepts any attribute name, which is how a wrong field name can pass the tests (see §9).

---

## 9. Gap analysis: current code vs this design

| # | Current `massive_client.py` | Problem | Change |
|---|---|---|---|
| 1 ✅ fixed | `snap.last_trade.timestamp / 1000.0` | `LastTrade` has **no** `timestamp` attribute; the real field is `sip_timestamp`, in **nanoseconds**. With the real client, every snapshot raises `AttributeError` and is skipped, so the **cache never fills**. The tests pass only because they use `MagicMock` | Use `snapshot_price()` (§5.1) |
| 2 ✅ fixed | Only `last_trade.price` | `last_trade` is `None` on plans without trades (Starter) | Fall back to `min`, then `day`, then `prev_day` |
| 3 ✅ fixed | Snapshot only | A free Basic key gets `NOT_AUTHORIZED` on every poll, so there are no prices at all | Add EOD mode with grouped daily |
| 4 ✅ fixed | `RESTClient(api_key=...)` with the default `retries=3` | 429 retries burn the free 5/min budget | `retries=0`; our loop owns back-off |
| 5 ✅ fixed | Fixed interval on errors | Hammers the API with a bad key or under rate limiting | Exponential back-off up to 5 min |
| 6 ✅ fixed | Interval hard-coded | — | `MASSIVE_POLL_INTERVAL` env var |
| 7 ✅ fixed | `SimulatorDataSource.add_ticker` does not normalise case | `"tsla"` and `"TSLA"` become separate tickers | Normalise in both sources |
| 8 ✅ fixed | Tests mock snapshots with `MagicMock` | They hid #1 | Build fixtures with `from_dict` |
