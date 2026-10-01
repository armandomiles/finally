"""Massive (Polygon.io) API client for real market data."""

from __future__ import annotations

import asyncio
import logging
import time
from datetime import date, timedelta
from enum import Enum

from massive import RESTClient
from massive.exceptions import BadResponse
from massive.rest.models import SnapshotMarketType, TickerSnapshot

from .cache import PriceCache
from .interface import MarketDataSource

logger = logging.getLogger(__name__)


def snapshot_price(snap: TickerSnapshot) -> tuple[float, float] | None:
    """Best available (price, unix_seconds) from a snapshot, or None if it has no price.

    Falls back last trade → minute bar → day bar → previous day, since lastTrade
    is absent on plans without trade data and day/min are empty before the open.
    Units differ by field: last_trade.sip_timestamp and updated are nanoseconds,
    min.timestamp is milliseconds.
    """
    if snap.last_trade and snap.last_trade.price:
        ts = snap.last_trade.sip_timestamp
        return snap.last_trade.price, (ts / 1e9 if ts else time.time())
    if snap.min and snap.min.close:
        ts = snap.min.timestamp
        return snap.min.close, (ts / 1e3 if ts else time.time())
    if snap.day and snap.day.close:
        return snap.day.close, (snap.updated / 1e9 if snap.updated else time.time())
    if snap.prev_day and snap.prev_day.close:
        return snap.prev_day.close, time.time()
    return None


class MassiveMode(str, Enum):
    """Which data the API key is entitled to."""

    LIVE = "live"  # snapshots (Starter+): delayed or real-time
    EOD = "eod"  # Basic/free plan: end-of-day grouped daily bars only


def _is_not_authorized(error: Exception) -> bool:
    """True if a BadResponse means the plan doesn't include the endpoint."""
    msg = str(error)
    return "NOT_AUTHORIZED" in msg or "not entitled" in msg.lower()


def _prev_weekday(d: date) -> date:
    d -= timedelta(days=1)
    while d.weekday() >= 5:
        d -= timedelta(days=1)
    return d


class MassiveDataSource(MarketDataSource):
    """MarketDataSource backed by the Massive (Polygon.io) REST API.

    LIVE mode polls GET /v2/snapshot/locale/us/markets/stocks/tickers for all
    watched tickers in a single API call. If the key's plan has no snapshot
    access (free Basic plan → NOT_AUTHORIZED), it switches to EOD mode and
    polls the grouped daily bars (one call covers the whole market).

    Failed polls back off exponentially up to MAX_BACKOFF. Client-side retries
    are disabled so they can't burn the free tier's 5 calls/min.
    """

    EOD_INTERVAL = 15 * 60.0
    MAX_BACKOFF = 300.0
    EOD_LOOKBACK_DAYS = 5  # step back over weekends/holidays

    def __init__(
        self,
        api_key: str,
        price_cache: PriceCache,
        poll_interval: float = 15.0,
    ) -> None:
        self._api_key = api_key
        self._cache = price_cache
        self._interval = poll_interval
        self._tickers: list[str] = []
        self._task: asyncio.Task | None = None
        self._client: RESTClient | None = None
        self.mode = MassiveMode.LIVE

    async def start(self, tickers: list[str]) -> None:
        self._client = RESTClient(api_key=self._api_key, retries=0)
        self._tickers = list(dict.fromkeys(t.upper().strip() for t in tickers))

        # Do an immediate first poll so the cache has data right away
        # (this also detects EOD mode on a free key)
        await self._poll_once()

        self._task = asyncio.create_task(self._poll_loop(), name="massive-poller")
        logger.info(
            "Massive poller started: %d tickers, %s mode, %.1fs interval",
            len(self._tickers),
            self.mode.value,
            self._base_interval(),
        )

    async def stop(self) -> None:
        if self._task and not self._task.done():
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
        self._task = None
        self._client = None
        logger.info("Massive poller stopped")

    async def add_ticker(self, ticker: str) -> None:
        ticker = ticker.upper().strip()
        if ticker in self._tickers:
            return
        self._tickers.append(ticker)
        if self.mode is MassiveMode.EOD and self._client:
            # Next EOD poll may be 15 min away — fetch a price now
            await self._fetch_prev_close(ticker)
        logger.info("Massive: added ticker %s", ticker)

    async def remove_ticker(self, ticker: str) -> None:
        ticker = ticker.upper().strip()
        self._tickers = [t for t in self._tickers if t != ticker]
        self._cache.remove(ticker)
        logger.info("Massive: removed ticker %s", ticker)

    def get_tickers(self) -> list[str]:
        return list(self._tickers)

    # --- Internal ---

    def _base_interval(self) -> float:
        return self._interval if self.mode is MassiveMode.LIVE else self.EOD_INTERVAL

    def _next_delay(self, ok: bool, current: float) -> float:
        """Base interval after success; double (capped) after failure."""
        if ok:
            return self._base_interval()
        return min(max(current, self._base_interval()) * 2, self.MAX_BACKOFF)

    async def _poll_loop(self) -> None:
        """Poll on interval. First poll already happened in start()."""
        delay = self._base_interval()
        while True:
            await asyncio.sleep(delay)
            ok = await self._poll_once()
            delay = self._next_delay(ok, delay)

    async def _poll_once(self) -> bool:
        """Execute one poll cycle. Returns False if the poll failed."""
        if not self._tickers or not self._client:
            return True

        try:
            if self.mode is MassiveMode.LIVE:
                try:
                    await self._poll_snapshots()
                    return True
                except BadResponse as e:
                    if not _is_not_authorized(e):
                        raise
                    logger.warning(
                        "Massive key has no snapshot access (Basic plan) — "
                        "switching to end-of-day prices"
                    )
                    self.mode = MassiveMode.EOD
            await self._poll_grouped_daily()
            return True
        except Exception as e:
            # Don't re-raise — the loop backs off and retries.
            # Common failures: 401 (bad key), 429 (rate limit), network errors.
            logger.error("Massive poll failed: %s", e)
            return False

    async def _poll_snapshots(self) -> None:
        # The Massive RESTClient is synchronous — run in a thread to
        # avoid blocking the event loop.
        snapshots = await asyncio.to_thread(self._fetch_snapshots)
        processed = 0
        for snap in snapshots:
            try:
                extracted = snapshot_price(snap)
            except (AttributeError, TypeError) as e:
                extracted = None
                logger.warning(
                    "Skipping snapshot for %s: %s",
                    getattr(snap, "ticker", "???"),
                    e,
                )
            if extracted is None:
                continue
            price, timestamp = extracted
            self._cache.update(ticker=snap.ticker, price=price, timestamp=timestamp)
            processed += 1
        logger.debug("Massive poll: updated %d/%d tickers", processed, len(self._tickers))

    async def _poll_grouped_daily(self) -> None:
        """Write the latest available daily close for each watched ticker."""
        wanted = set(self._tickers)
        day = date.today()
        for _ in range(self.EOD_LOOKBACK_DAYS):
            day = _prev_weekday(day)
            bars = await asyncio.to_thread(self._fetch_grouped_daily, day.isoformat())
            if not bars:
                continue  # holiday — step back another day
            for bar in bars:
                if bar.ticker in wanted and bar.close:
                    ts = bar.timestamp / 1e3 if bar.timestamp else time.time()
                    self._cache.update(ticker=bar.ticker, price=bar.close, timestamp=ts)
            return
        logger.warning(
            "Massive: no grouped daily data in the last %d weekdays", self.EOD_LOOKBACK_DAYS
        )

    async def _fetch_prev_close(self, ticker: str) -> None:
        try:
            results = await asyncio.to_thread(self._client.get_previous_close_agg, ticker)
        except Exception as e:
            logger.warning("Massive: previous close for %s failed: %s", ticker, e)
            return
        if results and results[0].close:
            bar = results[0]
            ts = bar.timestamp / 1e3 if bar.timestamp else time.time()
            self._cache.update(ticker=ticker, price=bar.close, timestamp=ts)

    def _fetch_snapshots(self) -> list:
        """Synchronous call to the Massive REST API. Runs in a thread."""
        return self._client.get_snapshot_all(
            market_type=SnapshotMarketType.STOCKS,
            tickers=self._tickers,
        )

    def _fetch_grouped_daily(self, day: str) -> list:
        """Synchronous call to the Massive REST API. Runs in a thread."""
        return self._client.get_grouped_daily_aggs(day, adjusted=True)
