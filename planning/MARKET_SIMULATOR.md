# Market Simulator

This document covers the approach and code structure for the simulated price feed. FinAlly uses it whenever `MASSIVE_API_KEY` is not set, which is the default for most users. It implements the `MarketDataSource` contract from `MARKET_INTERFACE.md`, so the rest of the app cannot tell it apart from real data.

**Status:** implemented in `backend/app/market/simulator.py` and `seed_prices.py`. This doc describes that code, the reasons behind it, and recommended tuning (§8).

---

## 1. Goals

| Goal | How it is met |
|---|---|
| Looks alive: prices tick about twice a second, with green/red flashes | 500 ms update loop; per-tick moves of about 1–3 cents |
| Looks plausible: no negative prices; volatile names move more | Geometric Brownian Motion (GBM) with per-ticker σ |
| Stocks move together the way real ones do | Correlated draws via a Cholesky factor of a sector correlation matrix |
| Some drama for the demo | Rare random 2–5% "news" shocks |
| No setup | Pure Python plus numpy, runs in-process, no network, no API key |
| Any ticker the user types works | Unknown tickers get a random seed price and default parameters |

Non-goals: order book or bid/ask, market hours, overnight gaps, historical backfill, and reproducing real prices.

---

## 2. Model: Geometric Brownian Motion

Each step updates every price multiplicatively:

```
S(t+Δt) = S(t) · exp( (μ − σ²/2)·Δt + σ·√Δt·Z )
```

| Symbol | Meaning | Value |
|---|---|---|
| `S` | price | seeded per ticker (§4) |
| `μ` | annual drift | 0.03–0.08 |
| `σ` | annual volatility | 0.17 (V) … 0.50 (TSLA) |
| `Z` | standard normal, **correlated across tickers** | from Cholesky (§3) |
| `Δt` | step as a fraction of a trading year | `0.5 / (252 · 6.5 · 3600) ≈ 8.48e-8` |

Why GBM:
- `exp(·) > 0`, so prices **never go negative**, however long the session runs.
- Returns are normally distributed and prices lognormal. This is the textbook (Black–Scholes) model.
- Each step costs a few floating-point operations per ticker.

**Clock:** one real second equals one *trading* second. A full simulated trading day takes 6.5 real hours, so diffusion alone moves prices slowly:

| σ | 1-tick 1σ move | 1-hour 1σ move (diffusion only) | Example |
|---|---|---|---|
| 0.17 (V) | 0.0050% | 0.42% | $280 → ±1.4¢ / tick |
| 0.22 (AAPL) | 0.0064% | 0.54% | $190 → ±1.2¢ / tick |
| 0.50 (TSLA) | 0.0146% | 1.24% | $250 → ±3.6¢ / tick |

Prices are rounded to cents in the output. After rounding, roughly 1 tick in 3 on a low-σ name shows as "flat". Internal state keeps full precision, so rounding never accumulates.

---

## 3. Correlated moves

Real stocks share a market and sector factor. To reproduce that, draw independent normals `z ~ N(0, I)` and multiply them by the lower-triangular Cholesky factor `L` of a correlation matrix `C`, where `C = L·Lᵀ`:

```
z_corr = L @ z        →   Cov(z_corr) = C
```

Pairwise correlation rule (`_pairwise_correlation`), checked in this order:

| Pair | ρ |
|---|---|
| either ticker is TSLA | 0.3 ("does its own thing") |
| both in **tech** {AAPL, GOOGL, MSFT, AMZN, META, NVDA, NFLX} | 0.6 |
| both in **finance** {JPM, V} | 0.5 |
| anything else, including unknown tickers | 0.3 |

Every pair shares at least 0.3, which acts as a market factor, and the sector blocks add to it. This block structure is always positive-definite, so `np.linalg.cholesky` succeeds for any mix of known and unknown tickers.

`L` is rebuilt only when the ticker set changes. The cost is O(n²) to build and O(n³) to factor, which is negligible for n < 50. Each step then costs one O(n²) mat-vec.

---

## 4. Seed prices and per-ticker parameters (`seed_prices.py`)

Constants only, with no logic:

```python
SEED_PRICES = {"AAPL": 190.00, "GOOGL": 175.00, "MSFT": 420.00, "AMZN": 185.00, "TSLA": 250.00,
               "NVDA": 800.00, "META": 500.00, "JPM": 195.00, "V": 280.00, "NFLX": 600.00}

TICKER_PARAMS = {
    "AAPL": {"sigma": 0.22, "mu": 0.05}, "GOOGL": {"sigma": 0.25, "mu": 0.05},
    "MSFT": {"sigma": 0.20, "mu": 0.05}, "AMZN":  {"sigma": 0.28, "mu": 0.05},
    "TSLA": {"sigma": 0.50, "mu": 0.03}, "NVDA":  {"sigma": 0.40, "mu": 0.08},
    "META": {"sigma": 0.30, "mu": 0.05}, "JPM":   {"sigma": 0.18, "mu": 0.04},
    "V":    {"sigma": 0.17, "mu": 0.04}, "NFLX":  {"sigma": 0.35, "mu": 0.05},
}
DEFAULT_PARAMS = {"sigma": 0.25, "mu": 0.05}           # any other ticker

CORRELATION_GROUPS = {"tech": {...}, "finance": {"JPM", "V"}}
INTRA_TECH_CORR, INTRA_FINANCE_CORR, CROSS_GROUP_CORR, TSLA_CORR = 0.6, 0.5, 0.3, 0.3
```

An unknown ticker, for example one the user adds such as `PLTR`, starts at `uniform(50, 300)` and uses `DEFAULT_PARAMS` and cross-group correlation. Seed prices are only illustrative. The simulator does not try to match the real market.

---

## 5. Random events ("news shocks")

After each GBM step, every ticker independently has probability `p = 0.001` of a jump:

```python
if random.random() < event_probability:
    price *= 1 + random.uniform(0.02, 0.05) * random.choice([-1, 1])
```

At 2 ticks per second, that is one event per ticker every ~500 s. With 10 tickers there is an event somewhere about every 50 s. A jump stays in the price, which continues from its new level. That gives the visible "something happened" moment the UI is designed to show.

Shocks are **not** correlated across tickers, and they are symmetric up or down, so they add no drift on average.

---

## 6. Code structure

```
backend/app/market/
  seed_prices.py    # constants (§4)
  simulator.py      # GBMSimulator (pure math, sync) + SimulatorDataSource (async adapter)
```

### 6.1 `GBMSimulator`: pure, synchronous, easy to test

```python
class GBMSimulator:
    TRADING_SECONDS_PER_YEAR = 252 * 6.5 * 3600
    DEFAULT_DT = 0.5 / TRADING_SECONDS_PER_YEAR

    def __init__(self, tickers: list[str], dt: float = DEFAULT_DT,
                 event_probability: float = 0.001) -> None
    def step(self) -> dict[str, float]          # advance one Δt; {ticker: price rounded to 2dp}
    def add_ticker(self, ticker: str) -> None   # seed price + params, rebuild Cholesky
    def remove_ticker(self, ticker: str) -> None
    def get_price(self, ticker: str) -> float | None
    def get_tickers(self) -> list[str]

    # internals
    _tickers: list[str]                          # order == row order of the Cholesky matrix
    _prices: dict[str, float]                    # full-precision state
    _params: dict[str, dict[str, float]]
    _cholesky: np.ndarray | None                 # None when n <= 1
    def _add_ticker_internal(self, ticker) -> None   # batch init without rebuilding
    def _rebuild_cholesky(self) -> None
    @staticmethod
    def _pairwise_correlation(t1, t2) -> float
```

The `step()` hot path:

```python
z = np.random.standard_normal(n)
if self._cholesky is not None:
    z = self._cholesky @ z
for i, t in enumerate(self._tickers):
    mu, sigma = self._params[t]["mu"], self._params[t]["sigma"]
    self._prices[t] *= math.exp((mu - 0.5 * sigma**2) * self._dt + sigma * math.sqrt(self._dt) * z[i])
    if random.random() < self._event_prob:
        self._prices[t] *= 1 + random.uniform(0.02, 0.05) * random.choice([-1, 1])
    out[t] = round(self._prices[t], 2)
```

It knows nothing about asyncio, the cache or time, so tests can call `step()` thousands of times synchronously.

### 6.2 `SimulatorDataSource`: async adapter implementing `MarketDataSource`

```python
class SimulatorDataSource(MarketDataSource):
    def __init__(self, price_cache: PriceCache, update_interval: float = 0.5,
                 event_probability: float = 0.001) -> None

    async def start(self, tickers):   # build GBMSimulator, seed cache with initial prices, spawn loop
    async def stop(self):             # cancel + await task; idempotent
    async def add_ticker(self, t):    # sim.add_ticker + immediate cache seed (tradeable at once)
    async def remove_ticker(self, t): # sim.remove_ticker + cache.remove
    def get_tickers(self):

    async def _run_loop(self):
        while True:
            try:
                for t, p in self._sim.step().items():
                    self._cache.update(ticker=t, price=p)
            except Exception:
                logger.exception("Simulator step failed")   # never let the task die
            await asyncio.sleep(self._interval)
```

Data flow: `GBMSimulator.step()` → `{ticker: price}` → `PriceCache.update()` → `cache.version++` → the SSE stream sends the change at its next 500 ms check.

---

## 7. Properties and tests

| Property | Test |
|---|---|
| Prices stay > 0 | 10k steps with high σ; `min(price) > 0` |
| Output rounded to cents | `round(p, 2) == p` |
| Volatility scales with σ | the std of log-returns of TSLA is greater than that of V over many steps (events disabled: `event_probability=0`) |
| Correlation works | tech pair sample correlation ≈ 0.6 ± tolerance over 5k steps (events disabled) |
| Cholesky succeeds for the full default set and for 50 tickers | construct; `_cholesky.shape == (n, n)` |
| add/remove are idempotent and rebuild the matrix | add twice → one entry; remove unknown → no error |
| Unknown ticker gets a price within [50, 300] | |
| Source seeds the cache on `start()` and `add_ticker()` | the cache holds the ticker before the first tick |
| `stop()` is idempotent; no writes after stop | |

Statistical tests should fix the seed (§8.2) so they cannot fail by chance.

---

## 8. Recommended refinements

These are small, backwards-compatible changes. None of them affects the interface.

### 8.1 Rebalance shocks against diffusion

With the current numbers, **jumps make up most of the movement.** Per ticker per hour there are about 7.2 events. Their combined 1σ effect is about **9.7%**, while GBM diffusion contributes only about **0.5%**. Over a long session prices therefore drift a long way, by tens of percent, almost entirely through jumps. Between jumps they look calm.

Options, from smallest to largest change:
1. Lower `event_probability` to `0.0002`: one event per ticker every ~40 min and somewhere about every 4 min across 10 tickers. Jump variance then falls to about 4.3% per hour.
2. Add a time-compression factor, for example `dt = speed · 0.5 / TRADING_SECONDS_PER_YEAR` with `speed ≈ 10–50`. Diffusion then becomes visible next to the jumps, and the chart looks like a real intraday trace.
3. Pull prices gently back toward their seed with a mean-reversion (Ornstein–Uhlenbeck) term on log-price. This keeps multi-hour demos inside a plausible range.

Expose `event_probability` and `speed` as constructor arguments, which `event_probability` already is, and optionally as env vars (`SIM_EVENT_PROB`, `SIM_SPEED`) for demos.

### 8.2 Deterministic seeding

Replace the global `np.random` and `random` calls with one `np.random.Generator`:

```python
def __init__(..., seed: int | None = None):
    self._rng = np.random.default_rng(seed)
# step():  z = self._rng.standard_normal(n); self._rng.random() < p; self._rng.uniform(0.02, 0.05)
```

Tests become reproducible, and a fixed `SIM_SEED` gives the same demo run every time.

### 8.3 Ticker normalisation ✅ done

`SimulatorDataSource.start`, `add_ticker` and `remove_ticker` now uppercase and strip tickers, as the Massive source does (see `MARKET_INTERFACE.md` §9, item 7).

### 8.4 Optional extras
- **`previous_close`:** use the seed price as a "day open" reference, which would support a "% today" column if `PriceUpdate` gains it.
- **Sector inference for unknown tickers:** a small static map, for example `PLTR → tech` and `BAC → finance`, would give user-added names realistic co-movement. Low priority.
