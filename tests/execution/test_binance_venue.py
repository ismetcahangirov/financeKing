"""The adapter, driven through the same Protocol the demo runtime uses.

`RecordedExchange` implements `GuardedExchange`, so these tests exercise the production
call path -- parameter construction, the `parse_venue_payload` boundary, the
error-envelope check, model validation -- and differ from a live run only in where the
response text came from.

What is *not* asserted here, and why it is stated rather than hidden: the success
payloads of the authenticated endpoints. Recording those needs a testnet key pair, which
this pull request has no way to obtain, so the corpus carries the venue's real rejection
envelopes instead. Those are what the error path is tested against, and they are the
reason `response["orderId"]` is a bug -- the envelope has no `orderId` at all. The
success recordings arrive with the user-data streams in #62/#63.
"""

from __future__ import annotations

from dataclasses import replace
from datetime import UTC, datetime
from decimal import Decimal
from uuid import UUID

import pytest

from fking.domain import Instrument, Order, OrderType, Side, TimeInForce, Venue
from fking.execution import (
    VENUE_PROFILES,
    BinanceVenue,
    ExchangeError,
    ExecutionVenue,
    PermanentExchangeError,
    VenueExchangeInfo,
    VenueProfileError,
)
from tests.execution.conftest import RecordedExchange, recorded_venues

pytestmark = pytest.mark.unit

_CORRELATION_ID = UUID("00000000-0000-4000-8000-00000000c0de")
_ORDER_ID = UUID("00000000-0000-4000-8000-0000000000a1")


def _venue(recorded_exchange: RecordedExchange) -> BinanceVenue:
    return BinanceVenue(recorded_exchange, VENUE_PROFILES[Venue(recorded_exchange.venue_id)])


def _order(venue: Venue, *, order_type: OrderType = OrderType.LIMIT) -> Order:
    instrument = Instrument(
        venue=venue,
        symbol="BTCUSDT",
        base_asset="BTC",
        quote_asset="USDT",
        tick_size=Decimal("0.01"),
        lot_step=Decimal("0.00001"),
        min_notional_quote=Decimal("5"),
    )
    return Order(
        order_id=_ORDER_ID,
        client_order_id="fk-0123456789abcdef",
        correlation_id=_CORRELATION_ID,
        instrument=instrument,
        side=Side.BUY,
        order_type=order_type,
        time_in_force=TimeInForce.GTC,
        base_quantity=Decimal("0.00100"),
        limit_quote_price=Decimal("64000.00") if order_type is OrderType.LIMIT else None,
        created_at_utc=datetime(2026, 8, 5, 12, 0, tzinfo=UTC),
    )


def test_the_adapter_satisfies_the_execution_venue_protocol(
    recorded_exchange: RecordedExchange,
) -> None:
    """The seam is a Protocol so backtest and live differ by which object is passed.

    The annotation is the stronger half: `mypy --strict` checks structural conformance
    at type-check time, so a method that drifts from the interface fails the build rather
    than this assertion.
    """
    venue: ExecutionVenue = _venue(recorded_exchange)
    assert isinstance(venue, ExecutionVenue)


@pytest.mark.asyncio
async def test_exchange_info_parses_a_recorded_response(
    recorded_exchange: RecordedExchange,
) -> None:
    info = await _venue(recorded_exchange).exchange_info()

    assert isinstance(info, VenueExchangeInfo)
    assert info.symbol("BTCUSDT").is_trading
    assert info.server_time_utc.tzinfo is not None
    assert recorded_exchange.request_count == 1


@pytest.mark.asyncio
async def test_exchange_info_filters_reach_the_lattice_an_order_is_snapped_to(
    recorded_exchange: RecordedExchange,
) -> None:
    filters = (await _venue(recorded_exchange).exchange_info()).symbol("BTCUSDT").order_filters()

    assert filters.tick_size > 0
    assert filters.step_size > 0
    assert isinstance(filters.tick_size, Decimal)


@pytest.mark.asyncio
async def test_a_spot_venue_reports_no_positions_without_asking_the_venue() -> None:
    """Not an unimplemented method. A spot account has no position concept, so the
    truthful answer is an empty tuple -- and answering it locally lets a reconciler treat
    both markets identically instead of branching on the venue."""
    exchange = RecordedExchange(Venue.BINANCE_SPOT_TESTNET)
    venue = BinanceVenue(exchange, VENUE_PROFILES[Venue.BINANCE_SPOT_TESTNET])

    assert await venue.fetch_positions() == ()
    assert exchange.request_count == 0


@pytest.mark.asyncio
@pytest.mark.parametrize("venue_id", recorded_venues(), ids=str)
async def test_every_read_method_surfaces_a_recorded_rejection_as_a_classified_failure(
    venue_id: Venue,
) -> None:
    """The corpus holds the venue's real answer to an unsigned request for each endpoint.

    Asserting one classified failure per method is what proves no method indexes into a
    payload before checking whether it is an envelope.
    """
    exchange = RecordedExchange(venue_id)
    venue = BinanceVenue(exchange, VENUE_PROFILES[venue_id])

    with pytest.raises(ExchangeError):
        await venue.fetch_balances()
    with pytest.raises(ExchangeError):
        await venue.fetch_open_orders()
    with pytest.raises(ExchangeError):
        await venue.fetch_my_trades(symbol="BTCUSDT")
    if venue_id is Venue.BINANCE_FUTURES_TESTNET:
        with pytest.raises(ExchangeError):
            await venue.fetch_positions()


@pytest.mark.asyncio
@pytest.mark.parametrize("venue_id", recorded_venues(), ids=str)
async def test_every_write_method_surfaces_a_recorded_rejection_as_a_classified_failure(
    venue_id: Venue,
) -> None:
    exchange = RecordedExchange(venue_id)
    venue = BinanceVenue(exchange, VENUE_PROFILES[venue_id])
    order = _order(venue_id)

    with pytest.raises(ExchangeError):
        await venue.submit(order)
    with pytest.raises(ExchangeError):
        await venue.cancel(symbol="BTCUSDT", client_order_id=order.client_order_id)
    with pytest.raises(ExchangeError):
        await venue.cancel_replace(replacement=order, cancel_client_order_id="fk-previous")


@pytest.mark.asyncio
@pytest.mark.parametrize("venue_id", recorded_venues(), ids=str)
async def test_a_submitted_limit_order_carries_its_client_id_price_and_time_in_force(
    venue_id: Venue,
) -> None:
    """The outbound parameters are the audit record's `clientOrderId` and the venue's
    idempotency key, so what is sent matters as much as what comes back."""
    exchange = RecordedExchange(venue_id)
    venue = BinanceVenue(exchange, VENUE_PROFILES[venue_id])
    order = _order(venue_id)

    with pytest.raises(ExchangeError):
        await venue.submit(order)

    _endpoint, params = exchange.calls[-1]
    assert params["symbol"] == "BTCUSDT"
    assert params["side"] == "BUY"
    assert params["type"] == "LIMIT"
    assert params["newClientOrderId"] == order.client_order_id
    assert params["timeInForce"] == "GTC"
    # Never scientific notation: `str(Decimal("1E-8"))` is "1E-8", which Binance rejects
    # as a malformed quantity on a value that is perfectly correct.
    assert params["quantity"] == "0.00100"
    assert "E" not in params["quantity"]
    assert params["price"] == "64000.00"
    assert params["recvWindow"] == str(venue.profile.recv_window_ms)


@pytest.mark.asyncio
@pytest.mark.parametrize("venue_id", recorded_venues(), ids=str)
async def test_a_market_order_carries_no_time_in_force(venue_id: Venue) -> None:
    """Binance rejects a market order that carries one, and the domain already refuses a
    market order carrying a price."""
    exchange = RecordedExchange(venue_id)
    venue = BinanceVenue(exchange, VENUE_PROFILES[venue_id])

    with pytest.raises(ExchangeError):
        await venue.submit(_order(venue_id, order_type=OrderType.MARKET))

    _endpoint, params = exchange.calls[-1]
    assert params["type"] == "MARKET"
    assert "timeInForce" not in params
    assert "price" not in params


@pytest.mark.asyncio
@pytest.mark.parametrize("venue_id", recorded_venues(), ids=str)
async def test_a_cancel_addresses_the_order_by_the_id_this_system_derived(
    venue_id: Venue,
) -> None:
    """Never by the venue's id: that one is only knowable from a response we may never
    have received, which is exactly the case a cancel after a timeout has to handle."""
    exchange = RecordedExchange(venue_id)
    venue = BinanceVenue(exchange, VENUE_PROFILES[venue_id])

    with pytest.raises(ExchangeError):
        await venue.cancel(symbol="BTCUSDT", client_order_id="fk-abc123")

    _endpoint, params = exchange.calls[-1]
    assert params["origClientOrderId"] == "fk-abc123"
    assert "orderId" not in params


@pytest.mark.asyncio
async def test_a_spot_cancel_replace_is_atomic_at_the_venue() -> None:
    """A cancel followed by a place leaves a window in which the book is not what the
    risk engine believes it is. Spot has one endpoint that does both."""
    exchange = RecordedExchange(Venue.BINANCE_SPOT_TESTNET)
    venue = BinanceVenue(exchange, VENUE_PROFILES[Venue.BINANCE_SPOT_TESTNET])

    with pytest.raises(ExchangeError):
        await venue.cancel_replace(
            replacement=_order(Venue.BINANCE_SPOT_TESTNET), cancel_client_order_id="fk-old"
        )

    endpoint, params = exchange.calls[-1]
    assert endpoint == "privatePostOrderCancelReplace"
    assert params["cancelOrigClientOrderId"] == "fk-old"
    assert params["cancelReplaceMode"] == "STOP_ON_FAILURE"


@pytest.mark.asyncio
async def test_a_futures_cancel_replace_amends_the_live_order_in_place() -> None:
    """Futures has no cancelReplace. `PUT /fapi/v1/order` amends, so the request carries
    the *existing* client id rather than minting a replacement one."""
    exchange = RecordedExchange(Venue.BINANCE_FUTURES_TESTNET)
    venue = BinanceVenue(exchange, VENUE_PROFILES[Venue.BINANCE_FUTURES_TESTNET])

    with pytest.raises(ExchangeError):
        await venue.cancel_replace(
            replacement=_order(Venue.BINANCE_FUTURES_TESTNET), cancel_client_order_id="fk-old"
        )

    endpoint, params = exchange.calls[-1]
    assert endpoint == "fapiPrivatePutOrder"
    assert params["origClientOrderId"] == "fk-old"
    assert "newClientOrderId" not in params


@pytest.mark.asyncio
async def test_a_client_order_id_the_venue_would_reject_fails_before_the_request(
    recorded_exchange: RecordedExchange,
) -> None:
    """A locally-detected rejection costs a stack trace; the same rejection from the
    venue costs a round trip in the order path and names a parameter rather than the id."""
    venue = _venue(recorded_exchange)
    unacceptable = replace(_order(Venue(recorded_exchange.venue_id)), client_order_id="x" * 64)

    with pytest.raises(VenueProfileError, match="accepts 36"):
        await venue.submit(unacceptable)
    assert recorded_exchange.request_count == 0


@pytest.mark.asyncio
async def test_a_payload_of_the_wrong_shape_is_refused_rather_than_indexed(
    recorded_exchange: RecordedExchange,
) -> None:
    """An array where an object was expected is a contract change, not a value to guess at."""

    class _ArrayForEverything(RecordedExchange):
        async def call(self, _endpoint: str, _params: object) -> str:  # type: ignore[override]
            return "[]"

    venue = BinanceVenue(
        _ArrayForEverything(Venue(recorded_exchange.venue_id)),
        VENUE_PROFILES[Venue(recorded_exchange.venue_id)],
    )
    if venue.profile.market == "spot":
        with pytest.raises(PermanentExchangeError, match="returned no 'balances' array"):
            await venue.fetch_balances()
    else:
        assert await venue.fetch_balances() == ()


@pytest.mark.asyncio
async def test_closing_the_venue_releases_the_transport(
    recorded_exchange: RecordedExchange,
) -> None:
    await _venue(recorded_exchange).aclose()
    assert recorded_exchange.closed is True
