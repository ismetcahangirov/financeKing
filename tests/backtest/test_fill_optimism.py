"""A strategy that fills 100% of its limit orders fails here.

Any backtest whose passive orders all fill is trading against a market that does not
exist. The queue model makes that outcome unreachable rather than merely unlikely, and
this file is the assertion that it stays unreachable -- it is the regression that a later
"improvement" to the fill model has to get past.
"""

from __future__ import annotations

from datetime import timedelta
from decimal import Decimal

import pytest

from fking.backtest import OrderAckEvent
from tests.backtest.venue_support import EPOCH, make_bar, make_order, make_venue

pytestmark = pytest.mark.unit

# One limit order per bar, resting away from the touch -- the shape of every passive
# strategy the evolution engine will produce.
_ORDER_COUNT = 20


def _run_passive_strategy(*, limit_quote_price: str, base_volume: str) -> tuple[int, int]:
    """Rest one order per bar and report `(submitted, filled)`."""
    venue = make_venue(spread_bps=Decimal("0"), touch_base=Decimal("2"), band_base=Decimal("10"))
    venue.observe(make_bar())

    submitted = 0
    for index in range(1, _ORDER_COUNT + 1):
        decided_at_utc = EPOCH + timedelta(minutes=index)
        ack = venue.submit(
            make_order(ordinal=index, limit_quote_price=limit_quote_price, base_quantity="0.01"),
            decided_at_utc=decided_at_utc,
        )
        assert isinstance(ack, OrderAckEvent)
        venue.resolve_ack(ack)
        submitted += 1
        venue.observe(make_bar(open_time_utc=decided_at_utc, base_volume=base_volume))
    return submitted, venue.report.fill_count


def test_a_passive_strategy_cannot_fill_every_order_it_rests() -> None:
    submitted, filled = _run_passive_strategy(limit_quote_price="63900.00", base_volume="12.5")

    assert submitted == _ORDER_COUNT
    assert filled < submitted


def test_thin_bars_fill_nothing_at_all_rather_than_a_little_of_everything() -> None:
    """A bar whose whole volume is smaller than the queue ahead is not a partial fill."""
    submitted, filled = _run_passive_strategy(limit_quote_price="63900.00", base_volume="0.5")

    assert (submitted, filled) == (_ORDER_COUNT, 0)


def test_every_passive_fill_is_earned_by_volume_that_traded_at_its_price() -> None:
    """No order fills on the bar it arrived on: the queue in front of it is still there."""
    venue = make_venue(spread_bps=Decimal("0"), touch_base=Decimal("2"), band_base=Decimal("10"))
    venue.observe(make_bar())
    ack = venue.submit(
        make_order(limit_quote_price="64100.00", base_quantity="0.01"),
        decided_at_utc=EPOCH + timedelta(minutes=1),
    )
    assert isinstance(ack, OrderAckEvent)

    assert venue.resolve_ack(ack) == ()
    assert venue.report.fill_count == 0
