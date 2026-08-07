"""Every refusal on the portfolio path, asserted rather than reviewed.

These are the branches whose absence produces a number instead of an error: a grid whose
boundary drifted, a day that quietly went missing, a path that reached ruin, a mark that
was not a price. None of them would raise on its own -- each would simply make the
equity curve slightly different from the one the run actually earned.
"""

from __future__ import annotations

from datetime import timedelta
from decimal import Decimal

import pytest

from fking.backtest.portfolio import (
    EquityPath,
    EquityPathError,
    EquityPoint,
    MarkPriceMissingError,
    MetricInputError,
    PortfolioAccountingError,
    PortfolioState,
    effective_sample_or_none,
    path_economics,
    risk_profile,
)
from fking.domain import Instrument, Position
from tests.backtest.portfolio_support import BTCUSDT, grid_day, opening_state

pytestmark = pytest.mark.unit


def _point(offset_days: int, equity_usd: str = "100000") -> EquityPoint:
    return EquityPoint(
        as_of_utc=grid_day(offset_days),
        equity_usd=Decimal(equity_usd),
        is_in_market=True,
        regime="calm",
    )


def test_a_boundary_that_is_not_midnight_utc_is_refused() -> None:
    """A grid whose boundary drifts by hours is not a grid, and nothing else says so."""
    with pytest.raises(EquityPathError, match="not a midnight UTC grid boundary"):
        EquityPoint(
            as_of_utc=grid_day(0) + timedelta(hours=9),
            equity_usd=Decimal("100000"),
            is_in_market=False,
            regime="calm",
        )


def test_a_path_that_reaches_ruin_is_refused_rather_than_divided_by() -> None:
    """Zero equity is a terminal condition, not a denominator."""
    with pytest.raises(EquityPathError, match="reached ruin"):
        EquityPoint(
            as_of_utc=grid_day(0),
            equity_usd=Decimal("0"),
            is_in_market=False,
            regime="calm",
        )


def test_a_single_boundary_yields_no_return_and_is_refused() -> None:
    with pytest.raises(EquityPathError, match="at least two grid boundaries"):
        EquityPath(points=(_point(0),))


def test_a_missing_day_is_refused_rather_than_compounded_into_its_neighbour() -> None:
    """A gap turns one daily return into a two-day one, which raises the Sharpe."""
    with pytest.raises(EquityPathError, match="the grid skips from"):
        EquityPath(points=(_point(0), _point(2)))


def test_asking_for_a_regime_the_path_never_carried_is_refused() -> None:
    path = EquityPath(points=(_point(0), _point(1), _point(2)))
    with pytest.raises(EquityPathError, match="no earning day carries the regime"):
        path.time_in_market_pct_in_regime("stressed")


def test_a_positions_map_whose_key_disagrees_with_its_value_is_refused() -> None:
    """The shape a partial rename leaves behind: every lookup then marks another symbol."""
    other = Instrument(
        venue=BTCUSDT.venue,
        symbol="ETHUSDT",
        base_asset="ETH",
        quote_asset="USDT",
        tick_size=Decimal("0.01"),
        lot_step=Decimal("0.0001"),
        min_notional_quote=Decimal("10.00"),
    )
    with pytest.raises(PortfolioAccountingError, match="but holds a"):
        PortfolioState(
            as_of_utc=grid_day(0),
            quote_cash_usd=Decimal("100000"),
            positions={other: Position.flat(BTCUSDT)},
            applied_fill_ids=frozenset(),
            applied_funding_keys=frozenset(),
            funding_paid_usd=Decimal("0"),
            risk_limit_breach_count=0,
        )


def test_a_negative_breach_counter_is_refused() -> None:
    """A counter that can go down is a counter that can be talked down."""
    with pytest.raises(PortfolioAccountingError, match="must not be negative"):
        PortfolioState(
            as_of_utc=grid_day(0),
            quote_cash_usd=Decimal("100000"),
            positions={},
            applied_fill_ids=frozenset(),
            applied_funding_keys=frozenset(),
            funding_paid_usd=Decimal("0"),
            risk_limit_breach_count=-1,
        )


def test_a_mutable_dedupe_set_is_refused_at_construction() -> None:
    """A `set` here would let a caller retroactively unsee a fill."""
    with pytest.raises(PortfolioAccountingError, match="applied_fill_ids must be a frozenset"):
        PortfolioState(
            as_of_utc=grid_day(0),
            quote_cash_usd=Decimal("100000"),
            positions={},
            applied_fill_ids=set(),  # type: ignore[arg-type]  # the wrong type is the assertion
            applied_funding_keys=frozenset(),
            funding_paid_usd=Decimal("0"),
            risk_limit_breach_count=0,
        )


def test_a_run_cannot_open_at_zero_capital() -> None:
    with pytest.raises(PortfolioAccountingError, match="no denominator for a return"):
        PortfolioState.opening(as_of_utc=grid_day(0), starting_cash_usd=Decimal("0"))


def test_a_non_positive_mark_is_a_feed_fault_and_not_a_price() -> None:
    state = opening_state()
    with pytest.raises(MarkPriceMissingError, match="not a price"):
        state.with_funding(
            instrument=BTCUSDT,
            occurs_at_utc=grid_day(0),
            funding_rate=Decimal("0.0001"),
            mark_quote_price=Decimal("0"),
        )


def test_a_single_day_cannot_support_a_dispersion_estimate() -> None:
    with pytest.raises(MetricInputError, match="cannot support a dispersion"):
        risk_profile((Decimal("0.01"),))


def test_an_empty_window_has_no_economics() -> None:
    with pytest.raises(MetricInputError, match="at least one daily return"):
        path_economics(())


def test_a_short_window_reports_no_effective_sample_rather_than_a_fabricated_one() -> None:
    assert effective_sample_or_none((Decimal("0.01"), Decimal("-0.01"))) is None
