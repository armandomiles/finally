# Massive API Reference (formerly Polygon.io)

Research notes on the Massive REST API for fetching **real-time** and **end-of-day (EOD)** prices for **multiple tickers**, written for FinAlly's market data layer. It is the source material for `MARKET_INTERFACE.md`.

Researched 2026-10-01 against:
- the official docs at `massive.com/docs` (the endpoint pages and `massive.com/llms.txt`),
- the pricing page at `massive.com/pricing`,
- the source of the installed Python client, **`massive` 2.2.0** (pinned in `backend/uv.lock`). All field and method names below were checked against that source, not taken from blog posts.

---

## 1. Overview

| Item | Value |
|---|---|
| Base URL | `https://api.massive.com` (legacy `https://api.polygon.io` still resolves) |
| Python package | `massive` (`uv add massive`); Python ≥ 3.9 |
| Auth | API key. The client reads env var `MASSIVE_API_KEY` by default, or takes `RESTClient(api_key=...)` |
| Get a key | https://massive.com/dashboard/keys |
| Transport | REST (JSON) plus WebSocket streams (Starter and above) |
| Timestamps | Unix **milliseconds** on aggregates; Unix **nanoseconds** on trades, quotes and snapshot `updated` |

Polygon.io rebranded to Massive in 2025. The API paths (`/v2/...`, `/v3/...`) did not change. The Python package changed from `polygon-api-client` (`from polygon import RESTClient`) to `massive` (`from massive import RESTClient`).

---

## 2. Plans: which data a key can actually get

This section matters most for our design. **The free key cannot call snapshot endpoints.**

| Plan (Stocks, individual) | Price | Rate limit | Data timeliness | Snapshots | WebSocket |
|---|---|---|---|---|---|
| **Basic** | Free | **5 calls/min** | **End of day** | ❌ | ❌ |
| **Starter** | $29/mo | Unlimited* | 15-min delayed | ✅ | ✅ |
| **Developer** | $79/mo | Unlimited* | 15-min delayed (+ trades) | ✅ | ✅ |
| **Advanced** | $199/mo | Unlimited* | **Real-time** (+ quotes) | ✅ | ✅ |

\* "Unlimited" is a soft limit. Massive recommends staying under ~100 req/s.

What each plan means for FinAlly:

- **Basic (free):** only aggregate endpoints work: grouped daily, previous close, daily open/close and custom bars. Prices are **EOD**, so polling more than once every few minutes is pointless. A snapshot call returns **HTTP 403** with `"status": "NOT_AUTHORIZED"`.
- **Starter/Developer:** snapshots work, but prices are 15 minutes behind. They still look "live" because they tick during market hours.
- **Advanced:** snapshots carry real-time `lastTrade` and `lastQuote`.

`lastTrade` and `lastQuote` in snapshot responses are only populated "if included in plan". Code must treat them as optional and fall back to `min.close`, then `day.close`, then `prev_day.close`.

---

## 3. Endpoint catalogue for multi-ticker prices

| # | Purpose | Endpoint | Tickers per call | Plans |
|---|---|---|---|---|
| A | **Real-time / delayed snapshot**, many tickers | `GET /v2/snapshot/locale/us/markets/stocks/tickers?tickers=AAPL,MSFT` | any list (whole market if omitted) | Starter+ |
| B | Unified snapshot, many tickers or assets | `GET /v3/snapshot?ticker.any_of=AAPL,MSFT` | ≤ 250 | Starter+ |
| C | Single-ticker snapshot | `GET /v2/snapshot/locale/us/markets/stocks/tickers/{ticker}` | 1 | Starter+ |
| D | **EOD for the whole market** (grouped daily) | `GET /v2/aggs/grouped/locale/us/market/stocks/{date}` | all US stocks | **All (incl. Basic)** |
| E | Previous day bar | `GET /v2/aggs/ticker/{ticker}/prev` | 1 | All |
| F | Daily open/close (with pre-market and after-hours) | `GET /v1/open-close/{ticker}/{date}` | 1 | All |
| G | Custom bars (historical OHLCV) | `GET /v2/aggs/ticker/{ticker}/range/{mult}/{timespan}/{from}/{to}` | 1 | All |
| H | Last trade | `GET /v2/last/trade/{ticker}` | 1 | Developer+ |
| I | Last quote (NBBO) | `GET /v2/last/nbbo/{ticker}` | 1 | Advanced |
| J | Market status | `GET /v1/marketstatus/now` | n/a | All |

**Conclusions for FinAlly:**
- **Live mode (Starter+):** use **A**. One call returns every watched ticker, so the rate budget does not depend on watchlist size.
- **EOD mode (Basic):** use **D**. One call returns about 10–12k tickers for a date, and we filter locally to the watchlist. Calling **E** once per ticker would cost N calls against a budget of 5 per minute, so 10 tickers would take 2 minutes.
- Use **J** to label the UI ("market closed") and to slow polling outside trading hours. It is optional.

---

## 4. Client setup

```python
from massive import RESTClient

client = RESTClient()                       # reads MASSIVE_API_KEY from env
client = RESTClient(api_key="...")          # or explicit

# Constructor defaults in massive 2.2.0:
#   connect_timeout=10.0, read_timeout=10.0, retries=3,
#   base="https://api.massive.com", pagination=True, verbose=False, trace=False
client = RESTClient(api_key="...", retries=0, read_timeout=5.0)
```

Behaviour you need to know about:
- **The client is synchronous** (built on urllib3). In FastAPI, call it through `asyncio.to_thread(...)`.
- **Built-in retries:** urllib3 `Retry(total=retries, backoff_factor=0.1)` on status `413, 429, 499, 500, 502, 503, 504`. On the free tier a 429 retry spends calls from the 5/min budget within about 1 s. **Set `retries=0` for Basic keys** and let our poll loop handle back-off.
- **Errors:**
  - Missing key → `massive.exceptions.AuthError` at construction.
  - Any non-200 response, after retries → `massive.exceptions.BadResponse(body_text)`. The body is JSON such as `{"status":"NOT_AUTHORIZED","message":"You are not entitled to this data..."}`. The HTTP status code is **not** an attribute, so check the message text.
- `raw=True` on any method returns the urllib3 `HTTPResponse` instead of model objects. This helps with debugging.
- `trace=True, verbose=True` logs URLs and headers, with the key redacted.

---

## 5. Real-time / delayed prices for many tickers

### 5.1 Full market snapshot filtered to our tickers (endpoint A)

```python
from massive import RESTClient
from massive.rest.models import SnapshotMarketType

client = RESTClient()
snaps = client.get_snapshot_all(
    market_type=SnapshotMarketType.STOCKS,   # or "stocks"
    tickers=["AAPL", "MSFT", "NVDA"],        # list is joined with ","; case-sensitive
    include_otc=False,
)

for s in snaps:                              # list[TickerSnapshot]
    price = (
        (s.last_trade and s.last_trade.price)    # Developer+/Advanced
        or (s.min and s.min.close)               # latest minute bar
        or (s.day and s.day.close)               # today's bar so far
        or (s.prev_day and s.prev_day.close)     # pre-market / early morning
    )
    print(s.ticker, price, s.todays_change_percent)
```

Raw JSON (one element of `tickers[]`):

```json
{
  "ticker": "AAPL",
  "day":     {"o": 20.64, "h": 20.64, "l": 20.506, "c": 20.506, "v": 37216, "vw": 20.616},
  "prevDay": {"o": 20.79, "h": 21.0,  "l": 20.5,   "c": 20.63,  "v": 292738, "vw": 20.6939},
  "min":     {"o": 20.506, "h": 20.506, "l": 20.506, "c": 20.506, "v": 5000, "vw": 20.5105,
              "t": 1684428600000, "n": 1, "av": 37216},
  "lastTrade": {"p": 20.506, "s": 2416, "t": 1605192894630916600, "x": 4, "c": [14, 41], "i": "71675577320245"},
  "lastQuote": {"p": 20.5, "s": 13, "P": 20.6, "S": 22, "t": 1605192959994246100},
  "todaysChange": -0.124,
  "todaysChangePerc": -0.601,
  "updated": 1605192894630916600
}
```

**How the Python client maps fields** (`massive.rest.models.TickerSnapshot`, checked in source):

| JSON | Python attribute | Type / unit |
|---|---|---|
| `ticker` | `.ticker` | str |
| `day` | `.day` → `Agg(open, high, low, close, volume, vwap, timestamp, transactions)` | |
| `prevDay` | `.prev_day` → `Agg` | `.prev_day.close` = previous close |
| `min` | `.min` → `MinuteSnapshot(open, high, low, close, volume, vwap, accumulated_volume, timestamp)` | ms |
| `lastTrade` | `.last_trade` → `LastTrade(price, size, exchange, conditions, id, sip_timestamp, participant_timestamp, ...)` | **`sip_timestamp` in ns** |
| `lastQuote` | `.last_quote` → `LastQuote(bid_price, bid_size, ask_price, ask_size, sip_timestamp, ...)` | ns |
| `todaysChange` | `.todays_change` | float $ |
| `todaysChangePerc` | `.todays_change_percent` | float % |
| `updated` | `.updated` | **ns** |

> ⚠️ **Pitfalls (the older archived doc gets these wrong):**
> - `LastTrade` has **no `.timestamp` attribute**. It is `.sip_timestamp`, in **nanoseconds**. Convert with `/ 1e9`, not `/ 1000`.
> - `.day` is a plain `Agg`. It has **no** `previous_close` or `change_percent`. Use `.prev_day.close` and `.todays_change_percent`.
> - Snapshot data is cleared at about 3:30 AM ET and repopulates from about 4:00 AM ET. Early in the morning `day` and `min` may be empty or zero, so fall back to `prev_day`.
> - Every nested object can be `None`. Guard each access.

Helper that converts a snapshot to a normalised price and a timestamp in epoch seconds:

```python
import time

def snapshot_price(s) -> tuple[float, float] | None:
    """Best available price and epoch-seconds timestamp from a TickerSnapshot."""
    if s.last_trade and s.last_trade.price:
        ts = s.last_trade.sip_timestamp
        return s.last_trade.price, (ts / 1e9 if ts else time.time())
    if s.min and s.min.close:
        return s.min.close, (s.min.timestamp / 1e3 if s.min.timestamp else time.time())
    if s.day and s.day.close:
        return s.day.close, (s.updated / 1e9 if s.updated else time.time())
    if s.prev_day and s.prev_day.close:
        return s.prev_day.close, time.time()
    return None
```

### 5.2 Unified snapshot (endpoint B)

This endpoint suits mixed asset classes and gives cleaner session fields. It is paginated, so `list_` methods iterate, and it accepts at most 250 tickers per call.

```python
for s in client.list_universal_snapshots(ticker_any_of=["AAPL", "MSFT"], limit=250):
    if s.error:                                  # per-ticker errors, e.g. unknown symbol
        print(s.ticker, s.error, s.message); continue
    sess = s.session                             # UniversalSnapshotSession
    print(s.ticker, s.market_status,
          s.last_trade.price if s.last_trade else sess.close,
          sess.previous_close, sess.change, sess.change_percent)
```

`UniversalSnapshotSession` fields: `price, change, change_percent, open, high, low, close, previous_close, volume, early_trading_change(_percent), regular_trading_change(_percent), late_trading_change(_percent)`.

We prefer **A**. It needs one request with no pagination, and FinAlly is stocks-only.

### 5.3 Single ticker (endpoint C)

```python
s = client.get_snapshot_ticker(SnapshotMarketType.STOCKS, "AAPL")   # TickerSnapshot
```

### 5.4 WebSocket (Starter+)

Push-based streaming is available, but it is **out of scope** for FinAlly. PLAN.md specifies REST polling because it is simpler and has a single code path.

```python
from massive import WebSocketClient
ws = WebSocketClient(api_key="...", subscriptions=["T.AAPL", "AM.MSFT"])  # trades, minute aggs
ws.run(handle_msg=lambda msgs: [print(m) for m in msgs])                  # blocking
```

---

## 6. End-of-day prices for many tickers

### 6.1 Grouped daily: whole market for one date (endpoint D), works on Basic

```python
from datetime import date, timedelta

def last_weekday(d: date) -> date:
    while d.weekday() >= 5:
        d -= timedelta(days=1)
    return d

day = last_weekday(date.today() - timedelta(days=1)).isoformat()
bars = client.get_grouped_daily_aggs(day, adjusted=True)   # list[GroupedDailyAgg]

wanted = {"AAPL", "MSFT", "NVDA"}
eod = {b.ticker: b for b in bars if b.ticker in wanted}
for t, b in eod.items():
    print(t, b.close, b.open, b.high, b.low, b.volume, b.timestamp)  # timestamp: ms
```

JSON:

```json
{"adjusted": true, "queryCount": 3, "resultsCount": 3, "status": "OK",
 "results": [{"T": "AAPL", "o": 34.9, "h": 35.47, "l": 34.21, "c": 34.24,
              "v": 312583, "vw": 34.4736, "n": 4966, "t": 1602705600000}]}
```

Notes:
- On a **holiday**, or for **today before the close**, the call returns `resultsCount: 0` and an empty list. Step back one day and retry, up to about 5 days. Each step costs one call.
- To get the latest close plus the prior close (for a day-change figure), fetch two consecutive trading days. That is 2 calls.
- `GroupedDailyAgg` fields: `ticker, open, high, low, close, volume, vwap, timestamp, transactions, otc`.

### 6.2 Previous close, per ticker (endpoint E)

```python
prev = client.get_previous_close_agg("AAPL")   # list[PreviousCloseAgg]
print(prev[0].close, prev[0].timestamp)
```

This makes one call per ticker. It is fine for a single ticker added mid-session, but too expensive on Basic for the whole watchlist.

### 6.3 Daily open/close with extended hours (endpoint F)

```python
oc = client.get_daily_open_close_agg("AAPL", "2026-09-30")
print(oc.open, oc.close, oc.pre_market, oc.after_hours, oc.volume)
```

### 6.4 Historical bars (endpoint G)

Useful for future detail charts. `list_aggs` iterates through every page; `get_aggs` returns a single list.

```python
bars = client.get_aggs("AAPL", 1, "day", "2026-09-01", "2026-09-30")
for b in bars:
    print(b.timestamp, b.open, b.high, b.low, b.close, b.volume)
```

---

## 7. Market status (endpoint J)

```python
st = client.get_market_status()
print(st.market)            # "open" | "closed" | "extended-hours"
print(st.early_hours, st.after_hours, st.server_time)
```

---

## 8. Detecting the plan at runtime

No endpoint reports the plan, so we probe for it. Call the snapshot endpoint once with a single ticker:

```python
from massive.exceptions import BadResponse

def supports_snapshots(client) -> bool:
    try:
        client.get_snapshot_all("stocks", tickers=["AAPL"])
        return True
    except BadResponse as e:
        msg = str(e)
        if "NOT_AUTHORIZED" in msg or "not entitled" in msg.lower():
            return False
        raise                                   # 401 bad key, 5xx, etc.
```

On Basic this probe costs 1 of the 5 calls per minute. FinAlly avoids the separate call: its first real snapshot poll doubles as the probe and switches to end-of-day mode on `NOT_AUTHORIZED` (see `MARKET_INTERFACE.md` §5.1).

---

## 9. Rate budgeting for FinAlly

| Mode | Endpoint | Calls per cycle | Recommended interval |
|---|---|---|---|
| Live (Starter/Developer/Advanced) | Snapshot A | 1 (any number of tickers) | 2–5 s on paid tiers. 15 s is a conservative default |
| EOD (Basic) | Grouped daily D | 1–2 (more on holidays) | 15 min or longer. Data changes once a day |
| New ticker added in EOD mode | Previous close E | 1 | immediately, once |

---

## 10. Errors

| Situation | What you see |
|---|---|
| Key missing | `AuthError` raised by the `RESTClient()` constructor |
| Key invalid | `BadResponse` with a 401 body (`"status":"ERROR"`, message about the API key) |
| Plan lacks endpoint | `BadResponse` with `"status":"NOT_AUTHORIZED"` (403) |
| Rate limit | 429, retried automatically `retries` times, then `BadResponse` |
| Unknown ticker in snapshot | Ticker simply absent from `tickers[]`. No error |
| Unknown ticker in v3 snapshot | Element with `.error` / `.message` set |
| Holiday or weekend grouped daily | `[]` (empty list) |

---

## 11. Minimal end-to-end example

```python
import asyncio, os
from massive import RESTClient
from massive.rest.models import SnapshotMarketType

async def main():
    client = RESTClient(api_key=os.environ["MASSIVE_API_KEY"], retries=0)
    tickers = ["AAPL", "GOOGL", "MSFT"]
    while True:
        snaps = await asyncio.to_thread(
            client.get_snapshot_all, SnapshotMarketType.STOCKS, tickers
        )
        for s in snaps:
            p = snapshot_price(s)          # helper from §5.1
            if p:
                print(s.ticker, *p)
        await asyncio.sleep(15)

asyncio.run(main())
```

## Sources
- Full Market Snapshot: https://massive.com/docs/rest/stocks/snapshots/full-market-snapshot
- Unified Snapshot: https://massive.com/docs/rest/stocks/snapshots/unified-snapshot
- Single Ticker Snapshot: https://massive.com/docs/rest/stocks/snapshots/single-ticker-snapshot
- Daily Market Summary (grouped daily): https://massive.com/docs/rest/stocks/aggregates/daily-market-summary
- Previous Day Bar: https://massive.com/docs/rest/stocks/aggregates/previous-day-bar
- Market Status: https://massive.com/docs/rest/stocks/market-operations/market-status
- Pricing: https://massive.com/pricing
- Python client: https://github.com/massive-com/client-python (source of `massive` 2.2.0)
