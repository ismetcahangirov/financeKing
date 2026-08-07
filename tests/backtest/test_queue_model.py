"""A resting order joins the back of the queue and has to earn its fill.

The rule under test: an order fills only once volume traded at its price exceeds the
quantity that was quoted there when it arrived. Queue-position data does not exist for
us, so the back of the queue is the only assumption that cannot flatter a strategy.
"""

from __future__ import annotations

from datetime import timedelta
from decimal import Decimal

import pytest

from fking.backtest import FillEvent, OrderAckEvent
from fking.backtest.venue import BacktestVenue, volume_at_or_beyond
from fking.domain import Side
from tests.backtest.venue_support import EPOCH, make_bar, make_order, make_venue

pytestmark = pytest.mark.unit


def _rest_a_buy(
    venue: BacktestVenue, *, ordinal: int = 1, limit_quote_price: str = "63900.00"
) -> None:
    ack = venue.submit(
        make_order(ordinal=ordinal, limit_quote_price=limit_quote_price, base_quantity="0.01"),
        decided_at_utc=EPOCH + timedelta(minutes=1),
    )
    assert isinstance(ack, OrderAckEvent)
    assert venue.resolve_ack(ack) == ()


def test_a_bar_that_does_not_reach_the_limit_credits_no_volume() -> None:
    bar = make_bar(low_quote_price="63800.00", high_quote_price="64500.00")

    assert volume_at_or_beyond(bar, Side.BUY, Decimal("63000.00")) == Decimal("0")
    assert volume_at_or_beyond(bar, Side.SELL, Decimal("65000.00")) == Decimal("0")


def test_volume_is_credited_by_the_share_of_the_range_beyond_the_limit() -> None:
    """A buy resting at the very low of the bar is credited nothing, by construction."""
    bar = make_bar(low_quote_price="63800.00", high_quote_price="64500.00", base_volume="14")

    assert volume_at_or_beyond(bar, Side.BUY, Decimal("63800.00")) == Decimal("0")
    assert volume_at_or_beyond(bar, Side.BUY, Decimal("64500.00")) == Decimal("14")
    assert volume_at_or_beyond(bar, Side.BUY, Decimal("64150.00")) == Decimal("7")


def test_a_resting_order_does_not_fill_until_traded_volume_exceeds_the_queue_ahead() -> None:
    """The acceptance criterion, stated directly.

    The touch quotes 2 BTC, and the first bar credits 1.785… BTC at the order's price --
    less than the queue in front of it, so nothing fills. The second bar's credit clears
    the remainder of the queue and the order prints.
    """
    venue = make_venue(spread_bps=Decimal("0"), touch_base=Decimal("2"), band_base=Decimal("10"))
    venue.observe(make_bar())
    _rest_a_buy(venue)

    first = venue.observe(make_bar(open_time_utc=EPOCH + timedelta(minutes=1)))
    assert first == ()
    assert venue.report.fill_count == 0

    second = venue.observe(make_bar(open_time_utc=EPOCH + timedelta(minutes=2)))
    assert len(second) == 1
    assert isinstance(second[0], FillEvent)
    assert second[0].fill.base_quantity == Decimal("0.01")


def test_a_bar_that_trades_exactly_the_queue_quantity_fills_nothing() -> None:
    venue = make_venue(spread_bps=Decimal("0"), touch_base=Decimal("2"), band_base=Decimal("10"))
    venue.observe(make_bar())
    _rest_a_buy(venue)

    # Range 63800..64500, limit 64150: exactly half the bar's 4 BTC is credited, which is
    # exactly the 2 BTC quoted at the touch.
    exact = make_bar(open_time_utc=EPOCH + timedelta(minutes=1), base_volume="4")
    assert volume_at_or_beyond(exact, Side.BUY, Decimal("63900.00")) < Decimal("2")

    assert venue.observe(exact) == ()
    assert venue.report.fill_count == 0


def test_a_fill_is_stamped_at_the_close_of_the_bar_that_earned_it() -> None:
    """The first instant the volume that filled it is a fact, and not one moment earlier."""
    venue = make_venue(spread_bps=Decimal("0"), touch_base=Decimal("2"), band_base=Decimal("10"))
    venue.observe(make_bar())
    _rest_a_buy(venue)
    venue.observe(make_bar(open_time_utc=EPOCH + timedelta(minutes=1)))

    earning = make_bar(open_time_utc=EPOCH + timedelta(minutes=2))
    events = venue.observe(earning)

    assert events[0].fill.event_time_utc == earning.close_time_utc


def test_a_resting_buy_never_pays_more_than_its_limit_or_more_than_anyone_traded() -> None:
    venue = make_venue(spread_bps=Decimal("0"), touch_base=Decimal("2"), band_base=Decimal("10"))
    venue.observe(make_bar())
    _rest_a_buy(venue, limit_quote_price="63900.00")
    venue.observe(make_bar(open_time_utc=EPOCH + timedelta(minutes=1)))

    events = venue.observe(make_bar(open_time_utc=EPOCH + timedelta(minutes=2)))

    filled = events[0].fill
    assert filled.quote_price == Decimal("63900.00")
    assert filled.quote_price <= Decimal("64500.00")


def test_a_resting_order_that_never_sees_its_price_stays_working() -> None:
    venue = make_venue(spread_bps=Decimal("0"), touch_base=Decimal("2"), band_base=Decimal("10"))
    venue.observe(make_bar())
    _rest_a_buy(venue, limit_quote_price="60000.00")

    for minute in range(1, 6):
        assert venue.observe(make_bar(open_time_utc=EPOCH + timedelta(minutes=minute))) == ()

    assert len(venue.resting_orders) == 1
    assert venue.resting_orders[0].remaining_base == Decimal("0.01")
