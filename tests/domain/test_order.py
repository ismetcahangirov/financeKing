"""Order and Fill invariants."""

from __future__ import annotations

from decimal import Decimal
from uuid import UUID

import pytest

from fking.domain import DomainError, Fill, Order, OrderType, Side, TimeInForce
from tests.support.domain_factory import BTCUSDT, EPOCH, make_fill, make_order

pytestmark = pytest.mark.unit


def test_a_limit_order_without_a_price_is_refused() -> None:
    with pytest.raises(DomainError, match="carries no limit_quote_price"):
        make_order(order_type=OrderType.LIMIT, limit_quote_price=None)


def test_a_market_order_carrying_a_price_is_refused() -> None:
    """The venue ignores the field and fills at market.

    Accepting it means the order that executes is not the order the risk engine
    priced, and nothing in the response says so.
    """
    with pytest.raises(DomainError, match="fills at market"):
        make_order(order_type=OrderType.MARKET, limit_quote_price="64000.00")


def test_a_market_order_without_a_price_constructs() -> None:
    order = make_order(order_type=OrderType.MARKET, limit_quote_price=None)
    assert order.limit_quote_price is None


def test_client_order_id_may_not_be_blank() -> None:
    """It is the idempotency key. Blank means a retry places a second order."""
    with pytest.raises(DomainError, match="client_order_id must not be blank"):
        Order(
            order_id=UUID(int=7),
            client_order_id="",
            correlation_id=UUID(int=8),
            instrument=BTCUSDT,
            side=Side.BUY,
            order_type=OrderType.MARKET,
            time_in_force=TimeInForce.IOC,
            base_quantity=Decimal("0.01"),
            limit_quote_price=None,
            created_at_utc=EPOCH,
        )


def test_identifiers_must_be_uuids() -> None:
    with pytest.raises(DomainError, match="order_id must be a UUID"):
        Fill(
            fill_id=UUID(int=1),
            order_id="7",  # type: ignore[arg-type]  # the wrong type is the test
            venue_trade_id="t-1",
            instrument=BTCUSDT,
            side=Side.BUY,
            event_time_utc=EPOCH,
            quote_price=Decimal("64000"),
            base_quantity=Decimal("0.01"),
            fee_quote=Decimal("0"),
        )


@pytest.mark.parametrize(
    ("side", "expected"),
    [(Side.BUY, "0.01"), (Side.SELL, "-0.01")],
)
def test_signed_quantity_follows_the_side(side: Side, expected: str) -> None:
    assert make_order(side=side).signed_base_quantity == Decimal(expected)
    assert make_fill(side=side, quote_price="64000", base_quantity="0.01").signed_base_quantity == (
        Decimal(expected)
    )


def test_a_fill_reports_its_notional_gross_of_fee() -> None:
    fill = make_fill(side=Side.BUY, quote_price="64000", base_quantity="0.01", fee_quote="0.64")
    assert fill.notional_quote == Decimal("640.00")


def test_a_negative_fee_is_refused() -> None:
    """A rebate is a separate event, not a negative charge on this fill."""
    with pytest.raises(DomainError, match="fee_quote must not be negative"):
        make_fill(side=Side.BUY, quote_price="64000", base_quantity="0.01", fee_quote="-0.01")


def test_a_zero_quantity_fill_is_refused() -> None:
    with pytest.raises(DomainError, match="base_quantity must be positive"):
        make_fill(side=Side.BUY, quote_price="64000", base_quantity="0")
