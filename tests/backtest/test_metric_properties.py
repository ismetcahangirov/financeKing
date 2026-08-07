"""Trade frequency cannot reach a path statistic.

This is issue #38's first acceptance criterion, asserted two ways.

The property test says it for any equity curve: the same daily path reported with three
fills and with thirty produces identical Sharpe, Sortino, Calmar and ulcer index, because
`path_statistics` takes a return series and nothing else.

The example test says it through the accounting: one strategy buys a whole unit once,
another buys a tenth of a unit ten times at the same price and the same instant, and
their daily equity curves are identical by construction. Ten times the trades, the same
four numbers.

The failure this forecloses is not hypothetical arithmetic. Annualising a per-trade
Sharpe by the strategy's own frequency multiplies it by `sqrt(f)`, so the busier strategy
scores `sqrt(10)` -- about 3.16 -- higher on an identical equity curve, and the evolution
engine in P6 then selects for turnover. That is a fee paid to the venue and recorded as
an edge.
"""

from __future__ import annotations

from decimal import Decimal

import pytest
from hypothesis import given
from hypothesis import strategies as st

from fking.backtest.portfolio import (
    ANNUALISATION_DAYS,
    PortfolioState,
    assemble_report,
    build_equity_path,
    path_economics,
    path_statistics,
    risk_profile,
)
from fking.domain import Side
from tests.backtest.portfolio_support import (
    daily_mark,
    grid_day,
    make_fill,
    opening_state,
    path_from_returns,
    state_with_fill_count,
)

pytestmark = [pytest.mark.unit, pytest.mark.property]

daily_return_fractions = st.lists(
    st.decimals(
        min_value=Decimal("-0.05"),
        max_value=Decimal("0.05"),
        places=6,
        allow_nan=False,
        allow_infinity=False,
    ),
    min_size=12,
    max_size=40,
)

# Sixteen daily marks with genuine drawdowns and losing days, so all four ratios have
# denominators and the test is not passing on a degenerate path.
_MARK_SERIES: tuple[Decimal, ...] = tuple(
    Decimal(mark)
    for mark in (
        "40000",
        "40500",
        "39800",
        "41000",
        "40200",
        "41500",
        "40900",
        "42000",
        "41200",
        "42500",
        "41800",
        "43000",
        "42200",
        "43500",
        "42800",
        "44000",
    )
)


@given(return_fractions=daily_return_fractions, extra_fills=st.integers(min_value=1, max_value=500))
def test_trade_count_changes_no_path_statistic(
    return_fractions: list[Decimal], extra_fills: int
) -> None:
    """The same equity path, reported with two different trade counts."""
    path = path_from_returns(return_fractions)
    sparse = assemble_report(path=path, final_state=state_with_fill_count(1))
    busy = assemble_report(path=path, final_state=state_with_fill_count(1 + extra_fills))

    assert busy.credibility.fill_count == 1 + extra_fills
    assert sparse.credibility.fill_count == 1
    assert busy.statistics == sparse.statistics
    assert busy.risk == sparse.risk
    assert busy.economics == sparse.economics


@given(return_fractions=daily_return_fractions, starting_equity=st.integers(1000, 5_000_000))
def test_path_statistics_are_invariant_to_the_capital_they_are_earned_on(
    return_fractions: list[Decimal], starting_equity: int
) -> None:
    """A ratio that moved with account size would not be comparable between strategies."""
    small = path_from_returns(return_fractions, starting_equity_usd=Decimal("1000"))
    large = path_from_returns(return_fractions, starting_equity_usd=Decimal(starting_equity))
    small_risk = risk_profile(small.daily_return_fractions)
    large_risk = risk_profile(large.daily_return_fractions)

    assert small_risk == large_risk


@given(return_fractions=daily_return_fractions)
def test_annualisation_is_the_fixed_grid_and_not_the_strategys_own_frequency(
    return_fractions: list[Decimal],
) -> None:
    """The annualised volatility is the daily figure times `sqrt(365)`, always.

    Nothing about the strategy enters the annualiser -- which is the whole reason the
    grid is fixed rather than derived from what the strategy did.
    """
    path = path_from_returns(return_fractions)
    daily = path.daily_return_fractions
    risk = risk_profile(daily)

    observation_count = len(daily)
    mean = sum(daily, start=Decimal("0")) / Decimal(observation_count)
    variance = sum(
        ((observation - mean) ** 2 for observation in daily), start=Decimal("0")
    ) / Decimal(observation_count - 1)
    expected = variance.sqrt() * Decimal(ANNUALISATION_DAYS).sqrt()

    assert abs(risk.annualised_volatility_fraction - expected) < Decimal("0.0001")


def _holding_state(*, fill_count: int) -> PortfolioState:
    """A portfolio that ends up holding exactly one unit, bought in `fill_count` pieces.

    Every piece fills at the same price and the same instant, so the daily equity curve
    cannot differ between one piece and ten. Fees are zero for the same reason: a fee
    would make the curves genuinely different and the test would be asserting nothing.
    """
    state = opening_state(Decimal("100000"))
    piece = Decimal("1") / Decimal(fill_count)
    for index in range(fill_count):
        state = state.with_fill(
            make_fill(
                label=f"piece-{fill_count}-{index}",
                side=Side.BUY,
                base_quantity=piece,
                quote_price=Decimal("40000"),
                event_time_utc=grid_day(0),
            )
        )
    return state


@pytest.mark.parametrize("busy_fill_count", [10, 40])
def test_splitting_every_trade_leaves_the_four_headline_figures_identical(
    busy_fill_count: int,
) -> None:
    """The accounting route: identical curves, an order of magnitude more trades."""
    sparse_state = _holding_state(fill_count=1)
    busy_state = _holding_state(fill_count=busy_fill_count)
    marks = [daily_mark(offset, mark) for offset, mark in enumerate(_MARK_SERIES)]

    sparse = assemble_report(
        path=build_equity_path([(sparse_state, mark) for mark in marks]),
        final_state=sparse_state,
    )
    busy = assemble_report(
        path=build_equity_path([(busy_state, mark) for mark in marks]),
        final_state=busy_state,
    )

    assert sparse.ledger.fill_count == 1
    assert busy.ledger.fill_count == busy_fill_count
    assert sparse.statistics.sharpe_ratio is not None
    assert sparse.statistics.sortino_ratio is not None
    assert sparse.statistics.calmar_ratio is not None
    assert sparse.risk.max_drawdown_fraction > Decimal("0")

    assert busy.statistics.sharpe_ratio == sparse.statistics.sharpe_ratio
    assert busy.statistics.sortino_ratio == sparse.statistics.sortino_ratio
    assert busy.statistics.calmar_ratio == sparse.statistics.calmar_ratio
    assert busy.risk.ulcer_index_pct == sparse.risk.ulcer_index_pct


def test_a_window_with_no_losing_day_reports_an_undefined_sortino_rather_than_a_number() -> None:
    """`None` says the denominator does not exist. Zero would read as "no edge" and a
    large substitute would read as "excellent"; both are inventions."""
    rising = tuple(Decimal("0.001") for _ in range(20))
    path = path_from_returns(rising)
    daily = path.daily_return_fractions
    risk = risk_profile(daily)
    statistics = path_statistics(daily, risk=risk, economics=path_economics(daily))

    assert risk.downside_deviation_fraction == Decimal("0")
    assert risk.max_drawdown_fraction == Decimal("0")
    assert statistics.sortino_ratio is None
    assert statistics.calmar_ratio is None
