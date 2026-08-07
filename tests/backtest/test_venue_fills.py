"""Crossing fills, the price bound they can never escape, and the markout that keeps
passive execution from looking free.

The Hypothesis property is the load-bearing test in this file: no fill this venue prints
may sit outside the traded range of the bar it was priced against, for any generated bar
and any generated order. A price outside that range is a trade with a counterparty that
did not exist, and it is worth exactly the difference between it and the nearest real
print.
"""

from __future__ import annotations

from datetime import timedelta
from decimal import Decimal

import pytest
from hypothesis import given
from hypothesis import strategies as st

from fking.backtest import FillEvent, OrderAckEvent
from fking.backtest.costs import PartialFillProfile
from fking.backtest.venue import BacktestVenue
from fking.domain import OrderType, Side
from tests.backtest.test_cost_fixtures import cost_model, depth_profile, flat_spread_profile
from tests.backtest.venue_support import (
    BTCUSDT,
    EPOCH,
    SYMBOL,
    make_bar,
    make_order,
    make_venue,
)

pytestmark = pytest.mark.unit

# An order beyond the touch prints at the touch and again in the band: two prints.
_WALKED_FILL_COUNT = 2


def _cross(
    venue: BacktestVenue, *, base_quantity: str, side: Side = Side.BUY
) -> tuple[FillEvent, ...]:
    ack = venue.submit(
        make_order(
            order_type=OrderType.MARKET,
            limit_quote_price=None,
            base_quantity=base_quantity,
            side=side,
        ),
        decided_at_utc=EPOCH + timedelta(minutes=1),
    )
    assert isinstance(ack, OrderAckEvent)
    events = venue.resolve_ack(ack)
    return tuple(event for event in events if isinstance(event, FillEvent))


def test_an_order_inside_the_touch_prints_once_at_the_touch() -> None:
    venue = make_venue(spread_bps=Decimal("0"), touch_base=Decimal("2"), band_base=Decimal("10"))
    venue.observe(make_bar())

    fills = _cross(venue, base_quantity="1")

    assert len(fills) == 1
    assert fills[0].fill.quote_price == Decimal("64200.00")


def test_an_order_beyond_the_touch_prints_twice_at_successively_worse_prices() -> None:
    """Each print carries its own instant, its own fee and its own audit identity."""
    venue = make_venue(spread_bps=Decimal("0"), touch_base=Decimal("2"), band_base=Decimal("10"))
    venue.observe(make_bar())

    fills = _cross(venue, base_quantity="5")

    assert len(fills) == _WALKED_FILL_COUNT
    first, second = (event.fill for event in fills)
    assert first.base_quantity == Decimal("2")
    assert second.base_quantity == Decimal("3")
    assert second.quote_price > first.quote_price  # a buy walks up the book
    assert second.event_time_utc > first.event_time_utc
    assert first.fill_id != second.fill_id
    assert first.venue_trade_id != second.venue_trade_id
    assert first.fee_quote > 0
    assert second.fee_quote > 0


def test_a_sell_walking_the_book_prints_at_successively_lower_prices() -> None:
    venue = make_venue(spread_bps=Decimal("0"), touch_base=Decimal("2"), band_base=Decimal("10"))
    venue.observe(make_bar())

    fills = _cross(venue, side=Side.SELL, base_quantity="5")

    assert len(fills) == _WALKED_FILL_COUNT
    assert fills[1].fill.quote_price < fills[0].fill.quote_price


@pytest.mark.property
@given(
    close_offset=st.integers(min_value=0, max_value=700),
    low_offset=st.integers(min_value=0, max_value=200),
    high_offset=st.integers(min_value=0, max_value=500),
    quantity_steps=st.integers(min_value=1, max_value=900),
    side=st.sampled_from(Side),
    spread_bps=st.sampled_from([Decimal("0"), Decimal("2"), Decimal("50"), Decimal("500")]),
)
def test_no_fill_is_ever_priced_outside_the_bar_that_produced_it(  # noqa: PLR0913, PLR0917
    # Six Hypothesis draws, each an independent axis of the generated market. Bundling
    # them into one composite strategy would hide which axis shrank on a failure.
    close_offset: int,
    low_offset: int,
    high_offset: int,
    quantity_steps: int,
    side: Side,
    spread_bps: Decimal,
) -> None:
    low = Decimal("63800") - low_offset
    high = Decimal("64500") + high_offset
    close = low + close_offset
    close = min(close, high)
    bar = make_bar(
        open_quote_price=str(close),
        high_quote_price=str(high),
        low_quote_price=str(low),
        close_quote_price=str(close),
        base_volume="50",
    )
    venue = make_venue(spread_bps=spread_bps, touch_base=Decimal("2"), band_base=Decimal("10"))
    venue.observe(bar)

    fills = _cross(venue, side=side, base_quantity=str(Decimal("0.01") * quantity_steps))

    assert fills, "a crossing order inside the band always prints"
    for event in fills:
        assert bar.low_quote_price <= event.fill.quote_price <= bar.high_quote_price


def test_a_passive_fill_carries_an_adverse_selection_markout() -> None:
    venue = make_venue(spread_bps=Decimal("0"), touch_base=Decimal("2"), band_base=Decimal("10"))
    venue.observe(make_bar())
    ack = venue.submit(
        make_order(limit_quote_price="63900.00", base_quantity="0.01"),
        decided_at_utc=EPOCH + timedelta(minutes=1),
    )
    assert isinstance(ack, OrderAckEvent)
    venue.resolve_ack(ack)
    venue.observe(make_bar(open_time_utc=EPOCH + timedelta(minutes=1)))
    venue.observe(make_bar(open_time_utc=EPOCH + timedelta(minutes=2)))

    report = venue.report
    assert report.passive_fill_count == 1
    assert report.passive_markout_quote > Decimal("0")


def test_dropping_the_markout_term_raises_a_passive_strategys_net_return() -> None:
    """The regression fixture: the term cannot be quietly dropped.

    If this test ever reports an unchanged net return, the markout has stopped being
    charged -- and a passive-only strategy will look free, which is a property of the
    simulator rather than of the market.
    """
    charged = _passive_net_return(markout_bps=Decimal("3.2"))
    uncharged = _passive_net_return(markout_bps=Decimal("0"))

    assert uncharged > charged
    assert uncharged - charged == Decimal("0.20448000")  # 3.2 bp on a 639 USDT print


def _passive_net_return(*, markout_bps: Decimal) -> Decimal:
    model = cost_model(
        spreads={SYMBOL: flat_spread_profile(Decimal("0"))},
        depth={SYMBOL: depth_profile(touch_base=Decimal("2"), band_base=Decimal("10"))},
        partial_fills=PartialFillProfile(
            passive_markout_bps=markout_bps, requote_cost_bps_per_extra_fill=Decimal("1.5")
        ),
    )
    venue = make_venue(model=model)
    venue.observe(make_bar())
    ack = venue.submit(
        make_order(limit_quote_price="63900.00", base_quantity="0.01"),
        decided_at_utc=EPOCH + timedelta(minutes=1),
    )
    assert isinstance(ack, OrderAckEvent)
    venue.resolve_ack(ack)
    venue.observe(make_bar(open_time_utc=EPOCH + timedelta(minutes=1)))
    venue.observe(make_bar(open_time_utc=EPOCH + timedelta(minutes=2)))

    report = venue.report
    assert report.fill_count == 1
    exit_quote_price = Decimal("64200.00")
    net_quote = Decimal("0")
    for record in report.fills:
        gross_quote = (exit_quote_price - record.fill.quote_price) * record.fill.base_quantity
        net_quote += gross_quote - record.fill.fee_quote - record.passive_markout_quote
    return net_quote


def test_fills_carry_the_instrument_lot_lattice() -> None:
    venue = make_venue(spread_bps=Decimal("0"), touch_base=Decimal("2"), band_base=Decimal("10"))
    venue.observe(make_bar())

    fills = _cross(venue, base_quantity="3.5")

    for event in fills:
        assert event.fill.base_quantity % BTCUSDT.lot_step == Decimal("0")
