"""`edge_to_cost_ratio` below 2.0 rejects, regardless of net return.

The threshold is not a preference. A strategy whose gross edge is 1.5x its costs is one
cost-model revision, one fee-tier change or one volatility regime away from unprofitable,
and it will spend its life oscillating across the line. So a positive `net_return` does
not save it, and the tests below construct exactly that case: gross edge set to 1.5x the
run's measured cost, so net is comfortably positive and the verdict is still a rejection.

Gross, cost and net are asserted as three separate fields, because a net number alone
cannot distinguish a large edge eaten by costs from a small edge that survived, and those
two have completely different futures (`BACKTEST_ENGINE.md` section 4, reporting).
"""

from __future__ import annotations

from decimal import Decimal

import pytest

from fking.backtest.costs import (
    MIN_EDGE_TO_COST_RATIO,
    CostVerdict,
    SpreadQuantile,
    assess_run,
)
from tests.backtest.test_cost_fixtures import cost_model, default_round_trips

pytestmark = pytest.mark.unit


def _measured_cost_bp() -> Decimal:
    """The run's own round-trip cost, so the ratio can be dialled exactly."""
    probe = assess_run(
        cost_model(),
        default_round_trips(),
        gross_edge_per_trade_bp=Decimal("1"),
        quantile=SpreadQuantile.P50,
    )
    return probe.round_trip_cost_bp


def test_a_run_below_the_ratio_is_rejected_even_though_net_return_is_positive() -> None:
    cost_bp = _measured_cost_bp()
    report = assess_run(
        cost_model(),
        default_round_trips(),
        gross_edge_per_trade_bp=cost_bp * Decimal("1.5"),
        quantile=SpreadQuantile.P50,
    )

    assert report.net_edge_per_trade_bp > Decimal("0")
    assert report.net_return_bps > Decimal("0")
    assert report.edge_to_cost_ratio == Decimal("1.5")
    assert report.verdict is CostVerdict.REJECTED_EDGE_TO_COST
    assert not report.is_evidence
    assert report.void_reason is None


def test_the_threshold_is_applied_literally() -> None:
    """A result that fails by a hair failed. There is no 'one more configuration'."""
    cost_bp = _measured_cost_bp()
    just_under = assess_run(
        cost_model(),
        default_round_trips(),
        gross_edge_per_trade_bp=cost_bp * Decimal("1.9999"),
        quantile=SpreadQuantile.P50,
    )
    exactly_at = assess_run(
        cost_model(),
        default_round_trips(),
        gross_edge_per_trade_bp=cost_bp * MIN_EDGE_TO_COST_RATIO,
        quantile=SpreadQuantile.P50,
    )
    assert just_under.verdict is CostVerdict.REJECTED_EDGE_TO_COST
    assert exactly_at.verdict is CostVerdict.ACCEPTED


def test_a_run_clearing_the_ratio_is_accepted_and_reports_all_three_numbers() -> None:
    cost_bp = _measured_cost_bp()
    report = assess_run(
        cost_model(),
        default_round_trips(),
        gross_edge_per_trade_bp=cost_bp * Decimal("3"),
        quantile=SpreadQuantile.P50,
    )

    assert report.verdict is CostVerdict.ACCEPTED
    assert report.is_evidence
    assert report.edge_to_cost_ratio == Decimal("3")
    # Three separate numbers, and the arithmetic between them holds.
    assert report.gross_edge_per_trade_bp - report.round_trip_cost_bp == (
        report.net_edge_per_trade_bp
    )
    trades = Decimal(report.filled_trade_count)
    assert report.gross_return_bps == report.gross_edge_per_trade_bp * trades
    assert report.total_cost_bps == report.round_trip_cost_bp * trades
    assert report.net_return_bps == report.net_edge_per_trade_bp * trades


def test_the_report_carries_the_calibration_source_and_the_quantile_it_ran_at() -> None:
    """`BACKTEST_ENGINE.md` section 7 reports both as credibility fields on every run."""
    report = assess_run(
        cost_model(),
        default_round_trips(),
        gross_edge_per_trade_bp=Decimal("50"),
        quantile=SpreadQuantile.P99,
    )
    assert report.quantile is SpreadQuantile.P99
    assert "production" in report.calibration_source
    assert "testnet" not in report.calibration_source.casefold()
