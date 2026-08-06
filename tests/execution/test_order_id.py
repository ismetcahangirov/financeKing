"""The venue-side idempotency key.

The property that matters is not "the function returns a string". It is that the same
decision always produces the same id and two different decisions never share one --
because Binance rejects a duplicate `newClientOrderId` while the original is live, and
that rejection is the only thing standing between a submission retried after a timeout
and a position twice the size the risk engine authorised.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal
from uuid import UUID, uuid4

import pytest
from hypothesis import given
from hypothesis import strategies as st

from fking.domain import Instrument, OrderType, Side, TimeInForce, Venue
from fking.execution import (
    BINANCE_SPOT_TESTNET,
    VENUE_PROFILES,
    VenueProfile,
    VenueProfileError,
    assert_client_order_id_acceptable,
    derive_client_order_id,
)

pytestmark = pytest.mark.unit

_DECIDED_AT = datetime(2026, 8, 5, 12, 0, tzinfo=UTC)
_CORRELATION_ID = UUID("00000000-0000-4000-8000-00000000c0de")

BTCUSDT = Instrument(
    venue=Venue.BINANCE_SPOT_TESTNET,
    symbol="BTCUSDT",
    base_asset="BTC",
    quote_asset="USDT",
    tick_size=Decimal("0.01"),
    lot_step=Decimal("0.00001"),
    min_notional_quote=Decimal("5"),
)


# PLR0913: the parameters are the decision's identity, one for one, and each test below
# varies exactly one of them.
def _derive(  # noqa: PLR0913
    *,
    profile: VenueProfile = BINANCE_SPOT_TESTNET,
    correlation_id: UUID = _CORRELATION_ID,
    strategy_id: str = "breakout-v3",
    base_quantity: Decimal = Decimal("0.001"),
    side: Side = Side.BUY,
    decided_at_utc: datetime = _DECIDED_AT,
) -> str:
    return derive_client_order_id(
        profile=profile,
        correlation_id=correlation_id,
        strategy_id=strategy_id,
        instrument=BTCUSDT,
        side=side,
        base_quantity=base_quantity,
        order_type=OrderType.LIMIT,
        time_in_force=TimeInForce.GTC,
        decided_at_utc=decided_at_utc,
    )


def test_the_same_decision_always_derives_the_same_id() -> None:
    """A `uuid4()` here would be a fresh key per attempt, which is no key at all."""
    assert _derive() == _derive()


@pytest.mark.parametrize(
    ("changed", "why"),
    [
        ({"correlation_id": uuid4()}, "a different flow is a different order"),
        ({"strategy_id": "meanrev-v1"}, "two strategies may want the same trade"),
        ({"side": Side.SELL}, "the opposite trade must never reuse the key"),
        ({"base_quantity": Decimal("0.002")}, "a resize is a new order, not the same one"),
        ({"decided_at_utc": _DECIDED_AT + timedelta(seconds=1)}, "a later decision is new"),
    ],
)
def test_a_different_decision_derives_a_different_id(changed: dict[str, object], why: str) -> None:
    assert _derive() != _derive(**changed), why  # type: ignore[arg-type]


def test_quantities_the_venue_cannot_distinguish_derive_the_same_id() -> None:
    """`Decimal("0.00100")` and `Decimal("0.001")` are one order to Binance.

    Hashing the unquantized value would let two callers that formatted the same quantity
    differently place the same order twice, and the venue's duplicate check would not
    catch it because the ids differ.
    """
    assert _derive(base_quantity=Decimal("0.00100")) == _derive(base_quantity=Decimal("0.001"))


@pytest.mark.parametrize("profile", sorted(VENUE_PROFILES.values(), key=str), ids=str)
def test_a_derived_id_fits_the_venue_it_was_derived_for(profile: VenueProfile) -> None:
    derived = _derive(profile=profile)
    assert derived.startswith(profile.client_order_id_prefix)
    assert len(derived) <= profile.client_order_id_max_len
    assert set(derived) <= set(profile.client_order_id_charset)


@given(
    correlation_ids=st.lists(st.uuids(version=4), min_size=2, max_size=40, unique=True),
)
def test_distinct_flows_never_collide_on_an_id(correlation_ids: list[UUID]) -> None:
    """Collision here is a silently refused order, not a crash: the venue rejects the
    second placement as a duplicate and the position it was meant to open never opens."""
    derived = {_derive(correlation_id=correlation_id) for correlation_id in correlation_ids}
    assert len(derived) == len(correlation_ids)


def test_an_id_longer_than_the_venue_accepts_is_refused_locally() -> None:
    with pytest.raises(VenueProfileError, match="accepts 36"):
        assert_client_order_id_acceptable("f" * 37, profile=BINANCE_SPOT_TESTNET)


def test_an_id_outside_the_venue_charset_is_refused_locally() -> None:
    """A local refusal costs a stack trace; the venue's costs a round trip in the order
    path and names a parameter rather than the id."""
    with pytest.raises(VenueProfileError, match=r"does not accept"):
        assert_client_order_id_acceptable("fk-abc/def", profile=BINANCE_SPOT_TESTNET)


def test_an_acceptable_id_is_returned_unchanged() -> None:
    assert assert_client_order_id_acceptable("fk-abc123", profile=BINANCE_SPOT_TESTNET) == (
        "fk-abc123"
    )
