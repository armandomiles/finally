"""Tests for MassiveDataSource (mocked)."""

from datetime import date
from unittest.mock import MagicMock, patch

import pytest
from massive.exceptions import BadResponse
from massive.rest.models import GroupedDailyAgg, PreviousCloseAgg, TickerSnapshot

from app.market.cache import PriceCache
from app.market.massive_client import MassiveDataSource, MassiveMode, snapshot_price


def _make_snapshot(ticker: str, price: float, timestamp_ns: int) -> TickerSnapshot:
    """Build a real snapshot model from API-shaped JSON (lastTrade.t is nanoseconds)."""
    return TickerSnapshot.from_dict(
        {"ticker": ticker, "lastTrade": {"p": price, "s": 100, "t": timestamp_ns, "x": 4}}
    )


class TestSnapshotPrice:
    """snapshot_price() against real model objects and each fallback level."""

    def test_last_trade_nanoseconds_to_seconds(self):
        snap = _make_snapshot("AAPL", 190.5, 1_707_580_800_123_456_789)
        price, ts = snapshot_price(snap)
        assert price == 190.5
        assert ts == pytest.approx(1_707_580_800.123456789)

    def test_falls_back_to_minute_bar(self):
        snap = TickerSnapshot.from_dict(
            {"ticker": "AAPL", "min": {"c": 189.0, "t": 1_707_580_800_000}, "day": {"c": 188.0}}
        )
        assert snapshot_price(snap) == (189.0, 1_707_580_800.0)

    def test_falls_back_to_day_bar(self):
        snap = TickerSnapshot.from_dict(
            {"ticker": "AAPL", "day": {"c": 188.0}, "updated": 1_707_580_800_000_000_000}
        )
        assert snapshot_price(snap) == (188.0, 1_707_580_800.0)

    def test_falls_back_to_previous_day(self):
        snap = TickerSnapshot.from_dict(
            {"ticker": "AAPL", "day": {"c": 0}, "prevDay": {"c": 187.0}}
        )
        price, _ = snapshot_price(snap)
        assert price == 187.0

    def test_no_price_returns_none(self):
        assert snapshot_price(TickerSnapshot.from_dict({"ticker": "AAPL"})) is None


@pytest.mark.asyncio
class TestMassiveDataSource:
    """Unit tests for MassiveDataSource with mocked API."""

    async def test_poll_updates_cache(self):
        """Test that polling updates the cache."""
        cache = PriceCache()
        source = MassiveDataSource(
            api_key="test-key",
            price_cache=cache,
            poll_interval=60.0,  # Long interval so the loop doesn't auto-poll
        )
        source._tickers = ["AAPL", "GOOGL"]
        source._client = MagicMock()  # Satisfy the _poll_once guard

        mock_snapshots = [
            _make_snapshot("AAPL", 190.50, 1707580800000000000),
            _make_snapshot("GOOGL", 175.25, 1707580800000000000),
        ]

        with patch.object(source, "_fetch_snapshots", return_value=mock_snapshots):
            await source._poll_once()

        assert cache.get_price("AAPL") == 190.50
        assert cache.get_price("GOOGL") == 175.25

    async def test_malformed_snapshot_skipped(self):
        """Test that malformed snapshots are skipped gracefully."""
        cache = PriceCache()
        source = MassiveDataSource(
            api_key="test-key",
            price_cache=cache,
            poll_interval=60.0,
        )
        source._tickers = ["AAPL", "BAD"]
        source._client = MagicMock()  # Satisfy the _poll_once guard

        good_snap = _make_snapshot("AAPL", 190.50, 1707580800000000000)
        bad_snap = TickerSnapshot.from_dict({"ticker": "BAD"})  # no price data at all

        with patch.object(source, "_fetch_snapshots", return_value=[good_snap, bad_snap]):
            await source._poll_once()

        # Good ticker processed, bad one skipped
        assert cache.get_price("AAPL") == 190.50
        assert cache.get_price("BAD") is None

    async def test_api_error_does_not_crash(self):
        """Test that API errors don't crash the poller."""
        cache = PriceCache()
        source = MassiveDataSource(
            api_key="test-key",
            price_cache=cache,
            poll_interval=60.0,
        )
        source._tickers = ["AAPL"]
        source._client = MagicMock()  # Satisfy the _poll_once guard

        with patch.object(source, "_fetch_snapshots", side_effect=Exception("network error")):
            await source._poll_once()  # Should not raise

        assert cache.get_price("AAPL") is None  # No update happened

    async def test_timestamp_conversion(self):
        """Test that last-trade timestamps are converted from nanoseconds to seconds."""
        cache = PriceCache()
        source = MassiveDataSource(
            api_key="test-key",
            price_cache=cache,
            poll_interval=60.0,
        )
        source._tickers = ["AAPL"]
        source._client = MagicMock()  # Satisfy the _poll_once guard

        mock_snapshots = [_make_snapshot("AAPL", 190.50, 1707580800000000000)]

        with patch.object(source, "_fetch_snapshots", return_value=mock_snapshots):
            await source._poll_once()

        update = cache.get("AAPL")
        assert update is not None
        assert update.timestamp == 1707580800.0  # Converted to seconds

    async def test_add_ticker(self):
        """Test adding a ticker."""
        cache = PriceCache()
        source = MassiveDataSource(api_key="test-key", price_cache=cache)

        await source.add_ticker("AAPL")
        assert "AAPL" in source.get_tickers()

    async def test_add_ticker_uppercase_normalization(self):
        """Test that tickers are normalized to uppercase."""
        cache = PriceCache()
        source = MassiveDataSource(api_key="test-key", price_cache=cache)

        await source.add_ticker("aapl")
        assert "AAPL" in source.get_tickers()

    async def test_add_ticker_strips_whitespace(self):
        """Test that ticker whitespace is stripped."""
        cache = PriceCache()
        source = MassiveDataSource(api_key="test-key", price_cache=cache)

        await source.add_ticker("  AAPL  ")
        assert "AAPL" in source.get_tickers()

    async def test_remove_ticker(self):
        """Test removing a ticker."""
        cache = PriceCache()
        source = MassiveDataSource(api_key="test-key", price_cache=cache)
        source._tickers = ["AAPL", "GOOGL"]
        cache.update("AAPL", 190.00)

        await source.remove_ticker("AAPL")
        assert "AAPL" not in source.get_tickers()
        assert cache.get("AAPL") is None

    async def test_get_tickers(self):
        """Test getting the list of active tickers."""
        cache = PriceCache()
        source = MassiveDataSource(api_key="test-key", price_cache=cache)
        source._tickers = ["AAPL", "GOOGL"]

        tickers = source.get_tickers()
        assert tickers == ["AAPL", "GOOGL"]

    async def test_empty_tickers_skips_poll(self):
        """Test that polling is skipped when there are no tickers."""
        cache = PriceCache()
        source = MassiveDataSource(api_key="test-key", price_cache=cache)
        source._tickers = []

        # Should not call _fetch_snapshots
        with patch.object(source, "_fetch_snapshots") as mock_fetch:
            await source._poll_once()
            mock_fetch.assert_not_called()

    async def test_stop_is_idempotent(self):
        """Test that stop() can be called multiple times."""
        cache = PriceCache()
        source = MassiveDataSource(api_key="test-key", price_cache=cache)

        await source.stop()
        await source.stop()  # Should not raise

    async def test_stop_cancels_task(self):
        """Test that stop() cancels the polling task."""
        cache = PriceCache()
        source = MassiveDataSource(api_key="test-key", price_cache=cache, poll_interval=10.0)

        # Mock the client and start
        with patch("app.market.massive_client.RESTClient"):
            with patch.object(source, "_fetch_snapshots", return_value=[]):
                await source.start(["AAPL"])

        # Verify task is running
        assert source._task is not None
        assert not source._task.done()

        # Stop and verify task is cancelled
        await source.stop()
        assert source._task is None

    async def test_start_immediate_poll(self):
        """Test that start() does an immediate poll before starting the loop."""
        cache = PriceCache()
        source = MassiveDataSource(api_key="test-key", price_cache=cache, poll_interval=60.0)

        mock_snapshots = [_make_snapshot("AAPL", 190.50, 1707580800000000000)]

        with patch("app.market.massive_client.RESTClient"):
            with patch.object(source, "_fetch_snapshots", return_value=mock_snapshots):
                await source.start(["AAPL"])

        # Cache should have data immediately from the first poll
        assert cache.get_price("AAPL") == 190.50

        await source.stop()


NOT_AUTHORIZED = BadResponse(
    '{"status":"NOT_AUTHORIZED","message":"You are not entitled to this data."}'
)


def _grouped(ticker: str, close: float, timestamp_ms: int = 1_707_580_800_000) -> GroupedDailyAgg:
    return GroupedDailyAgg.from_dict({"T": ticker, "c": close, "t": timestamp_ms})


def _source(tickers: list[str]) -> tuple[MassiveDataSource, PriceCache]:
    cache = PriceCache()
    source = MassiveDataSource(api_key="test-key", price_cache=cache, poll_interval=15.0)
    source._tickers = list(tickers)
    source._client = MagicMock()
    return source, cache


@pytest.mark.asyncio
class TestEodMode:
    """Free Basic keys get NOT_AUTHORIZED on snapshots → end-of-day grouped daily bars."""

    async def test_not_authorized_switches_to_eod(self):
        source, cache = _source(["AAPL", "MSFT"])
        bars = [_grouped("AAPL", 190.0), _grouped("MSFT", 420.0), _grouped("IBM", 150.0)]

        with (
            patch.object(source, "_fetch_snapshots", side_effect=NOT_AUTHORIZED),
            patch.object(source, "_fetch_grouped_daily", return_value=bars) as grouped,
        ):
            ok = await source._poll_once()

        assert ok
        assert source.mode is MassiveMode.EOD
        assert grouped.call_count == 1
        assert cache.get_price("AAPL") == 190.0
        assert cache.get_price("MSFT") == 420.0
        assert cache.get_price("IBM") is None  # not watched
        assert cache.get("AAPL").timestamp == 1_707_580_800.0

    async def test_eod_mode_skips_snapshot_endpoint(self):
        source, _ = _source(["AAPL"])
        source.mode = MassiveMode.EOD

        with (
            patch.object(source, "_fetch_snapshots") as snaps,
            patch.object(source, "_fetch_grouped_daily", return_value=[_grouped("AAPL", 1.0)]),
        ):
            await source._poll_once()

        snaps.assert_not_called()

    async def test_other_bad_response_stays_live_and_fails(self):
        source, cache = _source(["AAPL"])
        bad_key = BadResponse('{"status":"ERROR","error":"Unknown API Key"}')

        with (
            patch.object(source, "_fetch_snapshots", side_effect=bad_key),
            patch.object(source, "_fetch_grouped_daily") as grouped,
        ):
            ok = await source._poll_once()

        assert not ok
        assert source.mode is MassiveMode.LIVE
        grouped.assert_not_called()
        assert cache.get_price("AAPL") is None

    async def test_steps_back_over_holidays(self):
        source, cache = _source(["AAPL"])
        source.mode = MassiveMode.EOD
        days: list[str] = []

        def fetch(day: str):
            days.append(day)
            return [] if len(days) < 3 else [_grouped("AAPL", 188.0)]

        with patch.object(source, "_fetch_grouped_daily", side_effect=fetch):
            assert await source._poll_once()

        assert len(days) == 3
        assert len(set(days)) == 3
        assert all(date.fromisoformat(d).weekday() < 5 for d in days)
        assert days == sorted(days, reverse=True)
        assert cache.get_price("AAPL") == 188.0

    async def test_gives_up_after_lookback(self):
        source, cache = _source(["AAPL"])
        source.mode = MassiveMode.EOD

        with patch.object(source, "_fetch_grouped_daily", return_value=[]) as grouped:
            assert await source._poll_once()

        assert grouped.call_count == MassiveDataSource.EOD_LOOKBACK_DAYS
        assert cache.get_price("AAPL") is None

    async def test_add_ticker_in_eod_fetches_previous_close(self):
        source, cache = _source(["AAPL"])
        source.mode = MassiveMode.EOD
        source._client.get_previous_close_agg.return_value = [
            PreviousCloseAgg.from_dict({"T": "PLTR", "c": 25.5, "t": 1_707_580_800_000})
        ]

        await source.add_ticker("pltr")

        source._client.get_previous_close_agg.assert_called_once_with("PLTR")
        assert cache.get_price("PLTR") == 25.5

    async def test_add_ticker_in_live_mode_waits_for_poll(self):
        source, cache = _source(["AAPL"])

        await source.add_ticker("PLTR")

        source._client.get_previous_close_agg.assert_not_called()
        assert "PLTR" in source.get_tickers()

    async def test_previous_close_failure_is_swallowed(self):
        source, cache = _source([])
        source.mode = MassiveMode.EOD
        source._client.get_previous_close_agg.side_effect = Exception("429")

        await source.add_ticker("PLTR")  # Should not raise

        assert "PLTR" in source.get_tickers()
        assert cache.get_price("PLTR") is None


class TestBackoff:
    def test_success_uses_live_interval(self):
        source, _ = _source([])
        assert source._next_delay(True, 240.0) == 15.0

    def test_failures_double_up_to_cap(self):
        source, _ = _source([])
        delay = 15.0
        seen = []
        for _ in range(7):
            delay = source._next_delay(False, delay)
            seen.append(delay)
        assert seen == [30.0, 60.0, 120.0, 240.0, 300.0, 300.0, 300.0]

    def test_eod_uses_eod_interval(self):
        source, _ = _source([])
        source.mode = MassiveMode.EOD
        assert source._next_delay(True, 15.0) == MassiveDataSource.EOD_INTERVAL
        assert (
            source._next_delay(False, MassiveDataSource.EOD_INTERVAL)
            == MassiveDataSource.MAX_BACKOFF
        )


@pytest.mark.asyncio
class TestStart:
    async def test_client_retries_disabled(self):
        source = MassiveDataSource(api_key="test-key", price_cache=PriceCache())

        with patch("app.market.massive_client.RESTClient") as client_cls:
            with patch.object(source, "_fetch_snapshots", return_value=[]):
                await source.start(["AAPL"])
        await source.stop()

        client_cls.assert_called_once_with(api_key="test-key", retries=0)

    async def test_start_normalizes_and_dedupes(self):
        source = MassiveDataSource(api_key="test-key", price_cache=PriceCache())

        with patch("app.market.massive_client.RESTClient"):
            with patch.object(source, "_fetch_snapshots", return_value=[]):
                await source.start(["aapl", " AAPL ", "msft"])
        await source.stop()

        assert source.get_tickers() == ["AAPL", "MSFT"]
