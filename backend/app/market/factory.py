"""Factory for creating market data sources."""

from __future__ import annotations

import logging
import math
import os

from .cache import PriceCache
from .interface import MarketDataSource
from .massive_client import MassiveDataSource
from .simulator import SimulatorDataSource

logger = logging.getLogger(__name__)

DEFAULT_MASSIVE_POLL_INTERVAL = 15.0


def _massive_poll_interval() -> float:
    """MASSIVE_POLL_INTERVAL in seconds; falls back to the default if unset or invalid."""
    raw = os.environ.get("MASSIVE_POLL_INTERVAL", "").strip()
    if not raw:
        return DEFAULT_MASSIVE_POLL_INTERVAL
    try:
        value = float(raw)
    except ValueError:
        value = 0.0
    if not math.isfinite(value) or value <= 0:
        logger.warning(
            "Ignoring invalid MASSIVE_POLL_INTERVAL=%r; using %.0fs",
            raw,
            DEFAULT_MASSIVE_POLL_INTERVAL,
        )
        return DEFAULT_MASSIVE_POLL_INTERVAL
    return value


def create_market_data_source(price_cache: PriceCache) -> MarketDataSource:
    """Create the appropriate market data source based on environment variables.

    - MASSIVE_API_KEY set and non-empty → MassiveDataSource (real market data)
    - Otherwise → SimulatorDataSource (GBM simulation)

    MASSIVE_POLL_INTERVAL (seconds, default 15) sets the Massive poll interval.

    Returns an unstarted source. Caller must await source.start(tickers).
    """
    api_key = os.environ.get("MASSIVE_API_KEY", "").strip()

    if api_key:
        logger.info("Market data source: Massive API (real data)")
        return MassiveDataSource(
            api_key=api_key,
            price_cache=price_cache,
            poll_interval=_massive_poll_interval(),
        )
    else:
        logger.info("Market data source: GBM Simulator")
        return SimulatorDataSource(price_cache=price_cache)
