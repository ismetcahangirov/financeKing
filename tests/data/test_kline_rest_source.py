"""The guarded kline endpoint: where it may point, how it pages, and when it stops.

The transport is swapped for `httpx.MockTransport` on the client the safety kernel built,
which is the pattern `tests/data/test_archive_fetcher.py` already uses. It matters that the
client is the real one: the host validation runs as an event hook on the built request, so
a stub client would prove that the stub does not call `api.binance.com`.

Every page served here is assembled from checksum-verified recorded archive bytes
(`tests/support/rest_klines`). Nothing about the wire encoding is authored.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from uuid import uuid4

import httpx
import pytest

from fking.data.backfill.rest import MAX_KLINES_PER_PAGE, GuardedKlineRest
from fking.platform.correlation import correlation_scope
from fking.platform.errors import DataUnavailableError
from fking.platform.safety import SafetyViolation
from tests.support import rest_klines

pytestmark = [pytest.mark.unit, pytest.mark.asyncio]

TESTNET_BASE = "https://testnet.binancefuture.com"
KLINES_PATH = "/fapi/v1/klines"
SYMBOL = "BTCUSDT"
BAR_INTERVAL = "1m"
MINUTE = timedelta(minutes=1)

NOW_UTC = datetime(2026, 8, 4, 12, 0, tzinfo=UTC)
WINDOW_START = datetime(2026, 8, 4, 10, 0, tzinfo=UTC)

TWO_PAGES = 2


def _source(handler: httpx.MockTransport, *, max_pages: int = 8) -> GuardedKlineRest:
    source = GuardedKlineRest(
        base_url=TESTNET_BASE,
        klines_path=KLINES_PATH,
        bar_interval=MINUTE,
        max_pages=max_pages,
    )
    # The house pattern from tests/data/test_archive_fetcher.py: keep the client the
    # kernel constructed -- with its host hook, trust_env=False and follow_redirects=False
    # -- and replace only the layer that would open a socket.
    source._client._transport = handler
    return source


def _page_for(request: httpx.Request, *, available: int) -> httpx.Response:
    """Serve the recorded bars whose open time falls inside the requested window."""
    start_ms = int(str(request.url.params["startTime"]))
    end_ms = int(str(request.url.params["endTime"]))
    limit = int(str(request.url.params["limit"]))
    rows = rest_klines.shift_to(
        rest_klines.recorded_rows()[:available], first_open_utc=WINDOW_START
    )
    inside = [row for row in rows if start_ms <= int(str(row[0])) <= end_ms][:limit]
    return httpx.Response(httpx.codes.OK, text=rest_klines.page(inside))


async def test_a_production_host_is_refused_at_construction() -> None:
    """The endpoint is public and unauthenticated, which is exactly the argument that
    gets a production host added. The allowlist does not distinguish intent."""
    with pytest.raises(SafetyViolation):
        GuardedKlineRest(
            base_url="https://fapi.binance.com",
            klines_path=KLINES_PATH,
            bar_interval=MINUTE,
        )


async def test_a_max_page_count_below_one_is_refused() -> None:
    """Zero pages is a fetcher that returns nothing and reports success, which the caller
    would read as "the venue has nothing" and use to leave a gap unrepaired."""
    with pytest.raises(ValueError, match="at least one"):
        GuardedKlineRest(
            base_url=TESTNET_BASE, klines_path=KLINES_PATH, bar_interval=MINUTE, max_pages=0
        )


async def test_a_short_page_ends_the_walk() -> None:
    """Fewer bars than the page limit means the venue has no more in this window. Asking
    again returns the same short page forever."""
    available = 30
    source = _source(httpx.MockTransport(lambda request: _page_for(request, available=available)))

    async with source:
        records = await source.klines(
            symbol=SYMBOL,
            interval=BAR_INTERVAL,
            from_utc=WINDOW_START,
            until_utc=WINDOW_START + MINUTE * 500,
            now_utc=NOW_UTC,
        )

    assert len(records) == available
    assert source.request_count == 1


async def test_a_full_page_advances_the_cursor_and_the_walk_continues() -> None:
    """A full page is not evidence the window is covered, so the cursor moves past the
    last bar received and the next page starts there."""
    available = MAX_KLINES_PER_PAGE + 200
    source = _source(httpx.MockTransport(lambda request: _page_for(request, available=available)))

    async with source:
        records = await source.klines(
            symbol=SYMBOL,
            interval=BAR_INTERVAL,
            from_utc=WINDOW_START,
            until_utc=WINDOW_START + MINUTE * available,
            now_utc=NOW_UTC,
        )

    assert len(records) == available
    assert source.request_count == TWO_PAGES
    assert [record.open_time_utc for record in records] == [
        WINDOW_START + MINUTE * index for index in range(available)
    ]


async def test_the_page_cap_stops_the_walk_rather_than_downloading_history() -> None:
    """A gap can be arbitrarily old, and REST is for repair rather than for bulk history
    (DATA_PIPELINE.md section 2). Hitting the cap narrows the gap by what arrived and
    leaves the rest recorded as missing."""
    source = _source(
        httpx.MockTransport(lambda request: _page_for(request, available=1440)), max_pages=1
    )

    # The cap emits a warning, and every log record in this system carries a correlation
    # id. In production the scope is opened by `KlineGapBackfiller.run`; here it has to be
    # opened by hand, or a suite that happens to have configured strict logging first
    # fails on the log call rather than on the behaviour under test.
    with correlation_scope(uuid4()):
        async with source:
            records = await source.klines(
                symbol=SYMBOL,
                interval=BAR_INTERVAL,
                from_utc=WINDOW_START,
                until_utc=WINDOW_START + MINUTE * 1440,
                now_utc=NOW_UTC,
            )

    assert len(records) == MAX_KLINES_PER_PAGE
    assert source.request_count == 1


async def test_bars_outside_the_requested_window_are_dropped() -> None:
    """The venue's `endTime` is inclusive and ours is half-open, so the boundary minute
    is the one that would silently arrive twice across two adjacent repairs."""
    source = _source(httpx.MockTransport(lambda request: _page_for(request, available=30)))

    async with source:
        records = await source.klines(
            symbol=SYMBOL,
            interval=BAR_INTERVAL,
            from_utc=WINDOW_START,
            until_utc=WINDOW_START + MINUTE * 5,
            now_utc=NOW_UTC,
        )

    assert [record.open_time_utc for record in records] == [
        WINDOW_START + MINUTE * index for index in range(5)
    ]


async def test_a_non_200_leaves_the_gap_open_rather_than_reporting_no_data() -> None:
    """An error is not "the venue has nothing". Returning an empty tuple here would let
    the caller record a range as recovered that it never asked about successfully."""
    source = _source(
        httpx.MockTransport(lambda _request: httpx.Response(httpx.codes.TOO_MANY_REQUESTS))
    )

    async with source:
        with pytest.raises(DataUnavailableError, match="returned HTTP 429"):
            await source.klines(
                symbol=SYMBOL,
                interval=BAR_INTERVAL,
                from_utc=WINDOW_START,
                until_utc=WINDOW_START + MINUTE * 5,
                now_utc=NOW_UTC,
            )
