"""Latency is scheduled through the loop, not deducted from a price.

The assertion that matters is the last one: an order whose limit the market moved through
during the latency window does not fill at a worse price, it does not fill at all. A
basis-point penalty model cannot produce that outcome, and the missing trades are what
kill latency-sensitive strategies.
"""

from __future__ import annotations

from datetime import timedelta
from decimal import Decimal

import pytest

from fking.backtest import FillEvent, OrderAckEvent, RejectEvent
from fking.domain import OrderType, Side, TimeInForce
from tests.backtest.venue_support import EPOCH, make_bar, make_order, make_venue

pytestmark = pytest.mark.unit


def test_the_three_latency_stages_are_applied_in_order() -> None:
    venue = make_venue()
    schedule = venue.schedule_for(EPOCH)

    # 250 ms decide-to-send, 180 ms send-to-ack, 95 ms ack-to-fill.
    assert schedule.arrives_at_utc == EPOCH + timedelta(milliseconds=250)
    assert schedule.acknowledged_at_utc == EPOCH + timedelta(milliseconds=430)
    assert schedule.earliest_fill_at_utc == EPOCH + timedelta(milliseconds=525)


def test_an_accepted_order_is_acknowledged_after_the_send_and_ack_stages() -> None:
    venue = make_venue()
    venue.observe(make_bar())

    answer = venue.submit(make_order(), decided_at_utc=EPOCH + timedelta(minutes=1))

    assert isinstance(answer, OrderAckEvent)
    assert answer.occurs_at_utc == EPOCH + timedelta(minutes=1, milliseconds=430)


def test_a_rejection_lands_at_the_instant_an_ack_would_have() -> None:
    """A refusal costs the same round trip an acceptance does.

    Reporting it at the decision instant would let a strategy retry inside a window that
    does not exist, which is how a backtest discovers a repricing loop that a real venue
    would rate-limit.
    """
    venue = make_venue()
    venue.observe(make_bar())

    answer = venue.submit(
        make_order(base_quantity="0.00001", limit_quote_price="64000.00"),
        decided_at_utc=EPOCH + timedelta(minutes=1),
    )

    assert isinstance(answer, RejectEvent)
    assert answer.occurs_at_utc == EPOCH + timedelta(minutes=1, milliseconds=430)


def test_a_fill_is_priced_against_the_book_at_the_ack_not_at_the_decision() -> None:
    """The market moves during the latency window, and the fill pays the new price."""
    venue = make_venue(spread_bps=Decimal("0"))
    venue.observe(make_bar())
    ack = venue.submit(
        make_order(order_type=OrderType.MARKET, limit_quote_price=None),
        decided_at_utc=EPOCH + timedelta(minutes=1),
    )
    assert isinstance(ack, OrderAckEvent)

    moved = make_bar(
        open_time_utc=EPOCH + timedelta(minutes=1),
        open_quote_price="64200.00",
        high_quote_price="65000.00",
        low_quote_price="64100.00",
        close_quote_price="64900.00",
    )
    venue.observe(moved)
    events = venue.resolve_ack(ack)

    assert [type(event).__name__ for event in events] == ["FillEvent"]
    filled = events[0]
    assert isinstance(filled, FillEvent)
    assert filled.fill.quote_price == Decimal("64900.00")


def test_a_limit_the_market_ran_away_from_during_the_window_does_not_fill_at_all() -> None:
    """The case a basis-point penalty cannot model: a missing trade, not a worse price."""
    venue = make_venue(spread_bps=Decimal("0"))
    venue.observe(make_bar())
    ack = venue.submit(
        make_order(side=Side.BUY, limit_quote_price="64200.00", time_in_force=TimeInForce.IOC),
        decided_at_utc=EPOCH + timedelta(minutes=1),
    )
    assert isinstance(ack, OrderAckEvent)

    # By the time the ack comes back the touch is 64900, well above the 64200 limit.
    venue.observe(
        make_bar(
            open_time_utc=EPOCH + timedelta(minutes=1),
            open_quote_price="64200.00",
            high_quote_price="65000.00",
            low_quote_price="64100.00",
            close_quote_price="64900.00",
        )
    )
    events = venue.resolve_ack(ack)

    assert len(events) == 1
    refused = events[0]
    assert isinstance(refused, RejectEvent)
    assert "did not cross" in refused.reason
    assert venue.report.fill_count == 0
