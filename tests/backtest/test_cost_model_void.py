"""An `edge_to_cost_ratio` that is infinite or negative voids the result.

`BACKTEST_ENGINE.md` section 9 lists this under failure handling with the diagnosis
attached: **the cost model did not run**. There are four ways to arrive there and all
four are voids rather than weak results.

- **Zero cost.** The ratio is infinite. A model that charged nothing did not charge.
- **Negative cost.** A funding credit exceeded every other term, so the ratio's sign is
  inverted and its magnitude reads as a quality score.
- **Negative gross edge.** A loss divided by a cost. `-4.1` and `4.1` sit in the same
  column on a tearsheet and are skimmed as the same number.
- **No fillable trade.** Nothing was charged because nothing happened; the run measured
  the venue's depth, not the strategy.

`edge_to_cost_ratio` is `None` under every one of them, which is the structural half: the
type system makes every reader handle the case rather than formatting a number that means
nothing.
"""

from __future__ import annotations

from decimal import Decimal

import pytest

from fking.backtest.costs import (
    CostVerdict,
    SpreadQuantile,
    assess_run,
)
from tests.backtest.test_cost_fixtures import (
    BAND_BASE,
    cost_model,
    costless_model,
    default_round_trips,
    funding_credit,
    round_trip,
)

pytestmark = pytest.mark.unit

# An entry and an exit, so a size the book cannot absorb is refused twice.
LEGS_PER_ROUND_TRIP = 2


def test_a_zero_cost_run_is_void_rather_than_infinitely_profitable() -> None:
    report = assess_run(
        costless_model(),
        [round_trip(hour_utc=12)],
        gross_edge_per_trade_bp=Decimal("50"),
        quantile=SpreadQuantile.P50,
    )

    assert report.round_trip_cost_bp == Decimal("0")
    assert report.edge_to_cost_ratio is None
    assert report.verdict is CostVerdict.VOID
    assert not report.is_evidence
    assert report.void_reason is not None
    assert "infinite" in report.void_reason


def test_a_negative_cost_run_is_void_rather_than_reported() -> None:
    """A funding credit that exceeds every other term inverts the ratio's sign."""
    report = assess_run(
        costless_model(),
        [round_trip(hour_utc=12, funding=funding_credit())],
        gross_edge_per_trade_bp=Decimal("50"),
        quantile=SpreadQuantile.P50,
    )

    assert report.breakdown.funding_bps < Decimal("0")
    assert report.round_trip_cost_bp < Decimal("0")
    assert report.edge_to_cost_ratio is None
    assert report.verdict is CostVerdict.VOID
    assert report.void_reason is not None
    assert "credit" in report.void_reason


def test_a_negative_gross_edge_is_void_rather_than_a_negative_ratio() -> None:
    report = assess_run(
        cost_model(),
        default_round_trips(),
        gross_edge_per_trade_bp=Decimal("-25"),
        quantile=SpreadQuantile.P50,
    )

    assert report.round_trip_cost_bp > Decimal("0")
    assert report.net_edge_per_trade_bp < Decimal("0")
    assert report.edge_to_cost_ratio is None
    assert report.verdict is CostVerdict.VOID
    assert report.void_reason is not None
    assert "negative" in report.void_reason


def test_a_run_with_no_fillable_trade_is_void() -> None:
    report = assess_run(
        cost_model(),
        [round_trip(base_quantity=BAND_BASE * 5)],
        gross_edge_per_trade_bp=Decimal("50"),
        quantile=SpreadQuantile.P50,
    )

    assert report.filled_trade_count == 0
    assert report.edge_to_cost_ratio is None
    assert report.verdict is CostVerdict.VOID
    assert report.void_reason is not None
    assert "no round trip was fillable" in report.void_reason
    # The finding is still reported: the capacity limit is the result.
    assert sum(report.rejections_by_reason.values()) == LEGS_PER_ROUND_TRIP


def test_an_empty_run_is_void_rather_than_free() -> None:
    report = assess_run(
        cost_model(), [], gross_edge_per_trade_bp=Decimal("50"), quantile=SpreadQuantile.P50
    )
    assert report.verdict is CostVerdict.VOID
    assert report.edge_to_cost_ratio is None
    assert report.rejections_by_reason == {}


def test_a_void_run_reports_no_ratio_at_all() -> None:
    """The field is `None`, not a sentinel number that could be formatted and read."""
    voided = [
        assess_run(
            costless_model(),
            [round_trip(hour_utc=12)],
            gross_edge_per_trade_bp=Decimal("50"),
            quantile=SpreadQuantile.P50,
        ),
        assess_run(
            cost_model(),
            default_round_trips(),
            gross_edge_per_trade_bp=Decimal("-1"),
            quantile=SpreadQuantile.P50,
        ),
    ]
    assert all(report.edge_to_cost_ratio is None for report in voided)
    assert all(report.verdict is CostVerdict.VOID for report in voided)
