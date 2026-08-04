"""The guarded `aggTrades` endpoint: where it may point, how it pages, and when it stops.

The transport is swapped for `httpx.MockTransport` on the client the safety kernel built,
which is the pattern `tests/data/test_kline_rest_source.py` already uses. It matters that
the client is the real one: the host validation runs as an event hook on the built request,
so a stub client would prove that the stub does not call `api.binance.com`.

Every page served here is assembled from frames recorded off a live testnet socket
(`tests/support/tape_prints`). Nothing about the wire encoding is authored.

The paging property under test is the one that distinguishes this endpoint from the kline
one: the cursor advances from the **last id the response actually carried**, not from
`from_id + limit`. A short page that was assumed to be a full one would skip every id the
venue did have between the two, and the caller would record them as still missing.
"""

from __future__ import annotations

from datetime import UTC, datetime

import httpx
import pytest

from fking.data.backfill.agg_trades import MAX_AGG_TRADES_PER_PAGE, GuardedAggTradeRest
from fking.platform.errors import DataIntegrityError, DataUnavailableError
from fking.platform.safety import SafetyViolation
from tests.support import tape_prints

pytestmark = [pytest.mark.unit, pytest.mark.asyncio]

TESTNET_BASE = "https://testnet.binance.vision"
AGG_TRADES_PATH = "/api/v3/aggTrades"
SYMBOL = "BTCUSDT"

NOW_UTC = datetime(2026, 8, 4, 12, 0, tzinfo=UTC)
TAPE_START = datetime(2026, 8, 3, 10, 0, tzinfo=UTC)

TWO_PAGES = 2
# Thirteen copies of the ninety-four-frame recording: just over the endpoint's
# thousand-print page maximum, which is the only volume at which the walk pages at all.
TAPE_REPEATS = 13


def _source(handler: httpx.MockTransport, *, max_pages: int = 8) -> GuardedAggTradeRest:
    source = GuardedAggTradeRest(
        base_url=TESTNET_BASE, agg_trades_path=AGG_TRADES_PATH, max_pages=max_pages
    )
    # The house pattern: keep the client the kernel constructed -- with its host hook,
    # trust_env=False and follow_redirects=False -- and replace only the layer that would
    # open a socket.
    source._client._transport = handler
    return source


def _recorded_rows(*, repeats: int = 1) -> tuple[tape_prints.StreamPayload, ...]:
    return tape_prints.rest_rows(
        tape_prints.tiled(
            tape_prints.shift_to(tape_prints.recorded_payloads(), first_event_utc=TAPE_START),
            repeats=repeats,
        )
    )


def _first_recorded_id() -> int:
    return int(str(_recorded_rows()[0]["a"]))


def _serve(*, repeats: int = 1) -> tuple[httpx.MockTransport, list[int]]:
    """The venue: contiguous prints from `fromId`, up to the `limit` it was asked for.

    Exactly the endpoint's own behaviour -- a page shorter than `limit` means it has run
    out -- so the walk's short-page rule is being tested against the semantics it was
    written for rather than against a server that withholds rows it has.
    """
    available = _recorded_rows(repeats=repeats)
    asked_from: list[int] = []

    def handler(request: httpx.Request) -> httpx.Response:
        from_id = int(str(request.url.params["fromId"]))
        page_limit = int(str(request.url.params["limit"]))
        asked_from.append(from_id)
        served = [row for row in available if int(str(row["a"])) >= from_id]
        return httpx.Response(httpx.codes.OK, text=tape_prints.rest_page(served[:page_limit]))

    return httpx.MockTransport(handler), asked_from


async def test_a_production_host_is_refused_at_construction() -> None:
    """The endpoint is public and unauthenticated, which is exactly the argument that gets
    a production host added. The allowlist does not distinguish intent."""
    with pytest.raises(SafetyViolation):
        GuardedAggTradeRest(base_url="https://api.binance.com", agg_trades_path=AGG_TRADES_PATH)


async def test_a_max_page_count_below_one_is_refused() -> None:
    """Zero pages is a fetcher that returns nothing and reports success, which the caller
    would read as "the venue has nothing" and use to leave a gap unrepaired."""
    with pytest.raises(ValueError, match="at least one"):
        GuardedAggTradeRest(base_url=TESTNET_BASE, agg_trades_path=AGG_TRADES_PATH, max_pages=0)


async def test_the_request_asks_by_id_and_never_by_time() -> None:
    """A one-millisecond gap has no time window to ask for. `fromId` is the only parameter
    that can address the prints inside it."""
    captured: list[httpx.URL] = []

    def handler(request: httpx.Request) -> httpx.Response:
        captured.append(request.url)
        return httpx.Response(httpx.codes.OK, text=tape_prints.rest_page(_recorded_rows()[:2]))

    async with _source(httpx.MockTransport(handler)) as source:
        await source.agg_trades(
            symbol=SYMBOL, from_id=_first_recorded_id(), print_count=2, now_utc=NOW_UTC
        )

    assert dict(captured[0].params) == {
        "symbol": SYMBOL,
        "fromId": str(_first_recorded_id()),
        "limit": "2",
    }
    assert "startTime" not in captured[0].params
    assert "endTime" not in captured[0].params


async def test_the_walk_pages_and_the_cursor_comes_from_the_last_id_it_received() -> None:
    """A page boundary loses nothing, because the next request starts one past the last id
    the response actually carried rather than one past what was asked for."""
    wanted = MAX_AGG_TRADES_PER_PAGE + 200
    transport, asked_from = _serve(repeats=TAPE_REPEATS)

    async with _source(transport) as source:
        prints = await source.agg_trades(
            symbol=SYMBOL, from_id=_first_recorded_id(), print_count=wanted, now_utc=NOW_UTC
        )

    assert source.request_count == TWO_PAGES
    assert asked_from == [
        _first_recorded_id(),
        _first_recorded_id() + MAX_AGG_TRADES_PER_PAGE,
    ]
    assert [int(record.venue_trade_id) for record in prints] == [
        _first_recorded_id() + step for step in range(wanted)
    ]


async def test_a_short_page_ends_the_walk_rather_than_being_asked_again() -> None:
    """The venue has nothing further in this range; asking again returns the same short
    page forever."""
    available = len(_recorded_rows())
    transport, _ = _serve()

    async with _source(transport) as source:
        prints = await source.agg_trades(
            symbol=SYMBOL,
            from_id=_first_recorded_id(),
            print_count=available + 50,
            now_utc=NOW_UTC,
        )

    assert source.request_count == 1
    assert len(prints) == available


async def test_the_page_cap_bounds_the_walk_and_returns_what_it_reached() -> None:
    """A repair that silently turned into a full-history download through REST is what
    `DATA_PIPELINE.md` section 2 forbids. Hitting the cap narrows the gap by what arrived
    and leaves the rest recorded."""
    transport, _ = _serve(repeats=TAPE_REPEATS)

    async with _source(transport, max_pages=1) as source:
        prints = await source.agg_trades(
            symbol=SYMBOL,
            from_id=_first_recorded_id(),
            print_count=MAX_AGG_TRADES_PER_PAGE + 200,
            now_utc=NOW_UTC,
        )

    assert source.request_count == 1
    assert len(prints) == MAX_AGG_TRADES_PER_PAGE


async def test_never_more_than_the_prints_that_were_asked_for() -> None:
    """The caller sizes its request from the gap. A page that overshoots would hand back
    prints outside the range the repair reasoned about."""

    overshoot = 10
    wanted = 3

    def handler(request: httpx.Request) -> httpx.Response:
        del request
        return httpx.Response(
            httpx.codes.OK, text=tape_prints.rest_page(_recorded_rows()[:overshoot])
        )

    async with _source(httpx.MockTransport(handler)) as source:
        prints = await source.agg_trades(
            symbol=SYMBOL, from_id=_first_recorded_id(), print_count=wanted, now_utc=NOW_UTC
        )

    assert len(prints) == wanted


async def test_a_non_ok_status_leaves_the_gap_open() -> None:
    """A repair that cannot reach the venue has recovered nothing, and must not be
    mistaken for a venue that had nothing."""

    def handler(request: httpx.Request) -> httpx.Response:
        del request
        return httpx.Response(httpx.codes.TOO_MANY_REQUESTS, text='{"code":-1003}')

    async with _source(httpx.MockTransport(handler)) as source:
        with pytest.raises(DataUnavailableError, match="returned HTTP 429"):
            await source.agg_trades(
                symbol=SYMBOL, from_id=_first_recorded_id(), print_count=2, now_utc=NOW_UTC
            )


async def test_a_negative_starting_id_is_refused_before_a_request_is_made() -> None:
    """Aggregate trade ids are non-negative, so a negative one means the id derived from
    the corpus is not the venue's -- and every print fetched under it would be wrong."""

    def handler(request: httpx.Request) -> httpx.Response:  # pragma: no cover - never called
        del request
        return httpx.Response(httpx.codes.OK, text="[]")

    async with _source(httpx.MockTransport(handler)) as source:
        with pytest.raises(DataIntegrityError, match="non-negative"):
            await source.agg_trades(symbol=SYMBOL, from_id=-1, print_count=2, now_utc=NOW_UTC)
        assert source.request_count == 0
