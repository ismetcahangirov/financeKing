"""Portfolio state advances by value, idempotently, and refuses to guess a mark.

The behaviours asserted here are the ones whose failure produces a plausible number
rather than an exception: a replayed fill that doubles a position, a missing mark that
freezes a drawdown, and a state advanced backwards by an event that arrived out of
order.
"""

from __future__ import annotations

import dataclasses
from datetime import timedelta
from decimal import Decimal

import pytest

from fking.backtest.portfolio import (
    EquityPathError,
    MarkPriceMissingError,
    PortfolioAccountingError,
    PortfolioState,
    build_equity_path,
)
from fking.domain import Side
from tests.backtest.portfolio_support import (
    BTCUSDT,
    daily_mark,
    flat_marks,
    grid_day,
    make_fill,
    opening_state,
)

pytestmark = pytest.mark.unit


def test_a_buy_debits_cash_and_the_fee_and_leaves_equity_unchanged_at_the_fill_price() -> None:
    """Buying at the mark converts cash into exposure; only the fee leaves the books."""
    state = opening_state(Decimal("100000"))
    after = state.with_fill(
        make_fill(
            label="buy-1",
            side=Side.BUY,
            base_quantity=Decimal("1"),
            quote_price=Decimal("40000"),
            event_time_utc=grid_day(0) + timedelta(hours=6),
            fee_quote=Decimal("40"),
        )
    )

    assert after.quote_cash_usd == Decimal("59960")
    assert after.equity_usd(flat_marks(Decimal("40000"))) == Decimal("99960")
    assert after.fee_paid_usd == Decimal("40")
    assert after.is_in_market is True


def test_replaying_a_fill_returns_the_same_portfolio_rather_than_doubling_it() -> None:
    """At-least-once delivery is the design constraint, so a repeat must be a no-op."""
    fill = make_fill(
        label="buy-1",
        side=Side.BUY,
        base_quantity=Decimal("1"),
        quote_price=Decimal("40000"),
        event_time_utc=grid_day(0),
    )
    once = opening_state().with_fill(fill)
    twice = once.with_fill(fill)

    assert twice is once
    assert twice.fill_count == 1
    assert twice.open_positions[0].signed_base_quantity == Decimal("1")


def test_advancing_a_state_leaves_the_earlier_one_untouched() -> None:
    """Every transition returns a new object; nothing mutates in place."""
    state = opening_state()
    before = dataclasses.replace(state)
    state.with_fill(
        make_fill(
            label="buy-1",
            side=Side.BUY,
            base_quantity=Decimal("1"),
            quote_price=Decimal("40000"),
            event_time_utc=grid_day(0),
        )
    )

    assert state == before
    assert state.fill_count == 0
    with pytest.raises(dataclasses.FrozenInstanceError):
        state.quote_cash_usd = Decimal("1")  # type: ignore[misc]  # asserting the freeze holds


def test_a_fill_stamped_before_the_portfolio_is_refused_rather_than_reordered() -> None:
    """Books advanced backwards produce an equity number nothing in the run explains."""
    state = opening_state().with_fill(
        make_fill(
            label="buy-1",
            side=Side.BUY,
            base_quantity=Decimal("1"),
            quote_price=Decimal("40000"),
            event_time_utc=grid_day(2),
        )
    )
    with pytest.raises(PortfolioAccountingError, match="cannot be advanced backwards"):
        state.with_fill(
            make_fill(
                label="buy-2",
                side=Side.BUY,
                base_quantity=Decimal("1"),
                quote_price=Decimal("40000"),
                event_time_utc=grid_day(1),
            )
        )


def test_marking_an_open_position_without_a_mark_is_refused() -> None:
    """A defaulted mark hides the drawdown for exactly as long as the feed gap lasts."""
    state = opening_state().with_fill(
        make_fill(
            label="buy-1",
            side=Side.BUY,
            base_quantity=Decimal("1"),
            quote_price=Decimal("40000"),
            event_time_utc=grid_day(0),
        )
    )
    with pytest.raises(MarkPriceMissingError, match="no mark supplied"):
        state.equity_usd({})


def test_funding_charges_the_holding_at_the_settlement_instant_and_is_idempotent() -> None:
    """A long pays when the rate is positive, once, however many times it is replayed."""
    state = opening_state().with_fill(
        make_fill(
            label="buy-1",
            side=Side.BUY,
            base_quantity=Decimal("2"),
            quote_price=Decimal("40000"),
            event_time_utc=grid_day(0),
        )
    )
    settled = state.with_funding(
        instrument=BTCUSDT,
        occurs_at_utc=grid_day(0) + timedelta(hours=8),
        funding_rate=Decimal("0.0001"),
        mark_quote_price=Decimal("40000"),
    )
    replayed = settled.with_funding(
        instrument=BTCUSDT,
        occurs_at_utc=grid_day(0) + timedelta(hours=8),
        funding_rate=Decimal("0.0001"),
        mark_quote_price=Decimal("40000"),
    )

    assert settled.funding_paid_usd == Decimal("8.0000")
    assert replayed is settled
    assert replayed.funding_paid_usd == Decimal("8.0000")


def test_a_flat_portfolio_pays_no_funding_at_a_settlement_it_sat_out() -> None:
    """The discreteness is real and exploitable, so it is modelled rather than accrued."""
    settled = opening_state().with_funding(
        instrument=BTCUSDT,
        occurs_at_utc=grid_day(0) + timedelta(hours=8),
        funding_rate=Decimal("0.01"),
        mark_quote_price=Decimal("40000"),
    )

    assert settled.funding_paid_usd == Decimal("0")
    assert settled.quote_cash_usd == Decimal("100000")


def test_a_round_trip_realises_pnl_net_of_the_fees_actually_charged() -> None:
    """Gross realised PnL and fees stay separate; only the report combines them."""
    state = opening_state()
    state = state.with_fill(
        make_fill(
            label="buy-1",
            side=Side.BUY,
            base_quantity=Decimal("1"),
            quote_price=Decimal("40000"),
            event_time_utc=grid_day(0),
            fee_quote=Decimal("40"),
        )
    )
    state = state.with_fill(
        make_fill(
            label="sell-1",
            side=Side.SELL,
            base_quantity=Decimal("1"),
            quote_price=Decimal("41000"),
            event_time_utc=grid_day(1),
            fee_quote=Decimal("41"),
        )
    )

    assert state.realised_pnl_usd == Decimal("1000")
    assert state.fee_paid_usd == Decimal("81")
    assert state.is_in_market is False
    assert state.equity_usd({}) == Decimal("100919")


def test_the_equity_path_marks_each_day_from_the_state_that_stood_at_its_boundary() -> None:
    """The bridge from accounting to the daily grid, end to end and in Decimal."""
    state = opening_state(Decimal("100000")).with_fill(
        make_fill(
            label="buy-1",
            side=Side.BUY,
            base_quantity=Decimal("1"),
            quote_price=Decimal("40000"),
            event_time_utc=grid_day(0),
        )
    )
    path = build_equity_path(
        [
            (state, daily_mark(0, Decimal("40000"))),
            (state, daily_mark(1, Decimal("41000"))),
            (state, daily_mark(2, Decimal("39000"))),
        ]
    )

    assert [point.equity_usd for point in path.points] == [
        Decimal("100000"),
        Decimal("101000"),
        Decimal("99000"),
    ]
    assert path.time_in_market_pct == Decimal("100")


def test_a_boundary_cannot_observe_state_from_after_it() -> None:
    """Marking a portfolio with state it did not yet hold is look-ahead, directly."""
    state = PortfolioState.opening(as_of_utc=grid_day(3), starting_cash_usd=Decimal("100000"))
    with pytest.raises(EquityPathError, match="cannot observe state from after it"):
        build_equity_path([(state, daily_mark(0, Decimal("40000")))])
