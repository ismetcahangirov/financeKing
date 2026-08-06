"""The six terms are reported separately and they sum to `round_trip_cost_bp`.

The property that carries this file is not "the total equals the sum" -- computing the
total *as* the sum makes that half tautological. It is the pair:

1. `as_terms()` is keyed by exactly the members of `CostTerm`, so a seventh term cannot
   be added to the breakdown and left out of the report; and
2. the total equals the sum of the six **named attributes**, read individually, so a
   mapping that pairs `CostTerm.FEES` with `spread_bps` fails here rather than producing
   a tearsheet whose waterfall adds up and attributes the cost to the wrong cause.

Attribution is the whole reason the terms stay apart. A strategy paying 40 bp of which 32
is funding is a carry position with an execution problem it does not have; the same 40 bp
of which 32 is depth slippage is a capacity problem, and only the decomposition tells the
two apart.
"""

from __future__ import annotations

from decimal import Decimal

import pytest
from hypothesis import given
from hypothesis import strategies as st

from fking.backtest.costs import (
    CostBreakdown,
    CostModelConfigError,
    CostTerm,
    FundingExposure,
    RoundTrip,
    SpreadQuantile,
    assess_run,
    charge_round_trip,
)
from fking.domain import Direction
from tests.backtest.test_cost_fixtures import BAND_BASE, cost_model, round_trip

pytestmark = [pytest.mark.unit, pytest.mark.property]

MODEL = cost_model()

# Bounded well inside the +-1% band so that most generated fills are fillable; the
# rejection path has its own suite, and a property test that mostly rejects asserts
# nothing about the decomposition.
fillable_quantities = st.decimals(
    min_value=Decimal("0.001"), max_value=BAND_BASE, places=6, allow_nan=False, allow_infinity=False
)
funding_rates = st.decimals(
    min_value=Decimal("-0.01"),
    max_value=Decimal("0.01"),
    places=6,
    allow_nan=False,
    allow_infinity=False,
)


@st.composite
def round_trips(draw: st.DrawFn) -> RoundTrip:
    return round_trip(
        hour_utc=draw(st.integers(min_value=0, max_value=23)),
        base_quantity=draw(fillable_quantities),
        is_passive=draw(st.booleans()),
        funding=FundingExposure(
            direction=draw(st.sampled_from(Direction)),
            funding_rate=draw(funding_rates),
            settlement_count=draw(st.integers(min_value=0, max_value=9)),
        ),
    )


def _sum_of_named_attributes(breakdown: CostBreakdown) -> Decimal:
    """The total, read off the six fields by name rather than through `as_terms()`."""
    return (
        breakdown.fees_bps
        + breakdown.spread_bps
        + breakdown.depth_slippage_bps
        + breakdown.latency_bps
        + breakdown.partial_fill_bps
        + breakdown.funding_bps
    )


@given(trade=round_trips(), quantile=st.sampled_from(SpreadQuantile))
def test_every_charged_round_trip_decomposes_into_the_declared_terms(
    trade: RoundTrip, quantile: SpreadQuantile
) -> None:
    charged = charge_round_trip(MODEL, trade, quantile)
    assert charged.breakdown is not None

    terms = charged.breakdown.as_terms()
    assert frozenset(terms) == frozenset(CostTerm)
    assert charged.breakdown.round_trip_cost_bp == _sum_of_named_attributes(charged.breakdown)


@given(
    trades=st.lists(round_trips(), min_size=1, max_size=8),
    quantile=st.sampled_from(SpreadQuantile),
)
def test_a_run_report_totals_the_same_six_terms(
    trades: list[RoundTrip], quantile: SpreadQuantile
) -> None:
    """The per-trade mean must decompose exactly as one trade's charge does."""
    report = assess_run(MODEL, trades, gross_edge_per_trade_bp=Decimal("50"), quantile=quantile)
    assert report.filled_trade_count == len(trades)
    assert report.round_trip_cost_bp == _sum_of_named_attributes(report.breakdown)
    assert frozenset(report.breakdown.as_terms()) == frozenset(CostTerm)


@given(trade=round_trips(), quantile=st.sampled_from(SpreadQuantile))
def test_only_funding_may_be_a_credit(trade: RoundTrip, quantile: SpreadQuantile) -> None:
    """Every other term is money paid; a negative one would be a sign error."""
    charged = charge_round_trip(MODEL, trade, quantile)
    assert charged.breakdown is not None
    credited_terms = {
        term: bps for term, bps in charged.breakdown.as_terms().items() if bps < Decimal("0")
    }
    assert set(credited_terms) <= {CostTerm.FUNDING}


def test_the_zero_breakdown_costs_nothing_and_still_names_every_term() -> None:
    zero = CostBreakdown.zero()
    assert zero.round_trip_cost_bp == Decimal("0")
    assert frozenset(zero.as_terms()) == frozenset(CostTerm)


def test_breakdowns_add_and_average_term_by_term() -> None:
    one = CostBreakdown(
        fees_bps=Decimal("1"),
        spread_bps=Decimal("2"),
        depth_slippage_bps=Decimal("3"),
        latency_bps=Decimal("4"),
        partial_fill_bps=Decimal("5"),
        funding_bps=Decimal("-6"),
    )
    doubled = one.plus(one)
    assert doubled.round_trip_cost_bp == one.round_trip_cost_bp * Decimal("2")
    assert doubled.divided_by(2) == one


def test_averaging_over_no_trades_is_refused_rather_than_returning_zero() -> None:
    """A zero mean would report a free run where nothing was charged at all."""
    with pytest.raises(CostModelConfigError, match="over 0 trades"):
        CostBreakdown.zero().divided_by(0)
