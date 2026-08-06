"""The depth conservatism rule: quoted size is all the liquidity there is.

Free full-depth L2 order-book history does not exist (`SOURCES.md` section 2, VF-017), so
`depth_at_touch_base` is the `bookTicker` quantity and nothing behind it is observable.
The three regimes and the rejection are the whole content of this file.

The rejection is the part that matters. An order larger than the +-1% band notional is
refused rather than filled at an invented price, and it appears in `rejections_by_reason`.
A backtest full of size rejections has discovered a capacity limit, which is a genuine
finding; filling arbitrary size at the touch is how a strategy with no capacity gets
promoted.
"""

from __future__ import annotations

from decimal import Decimal

import pytest
from pydantic import ValidationError

from fking.backtest.costs import (
    BAND_WIDTH_BPS,
    CostModelConfigError,
    DepthProfile,
    RejectionReason,
    SpreadQuantile,
    assess_run,
    charge_round_trip,
    walk_depth,
)
from tests.backtest.test_cost_fixtures import (
    BAND_BASE,
    TOUCH_BASE,
    cost_model,
    depth_profile,
    round_trip,
)

pytestmark = pytest.mark.unit

GROSS_EDGE_PER_TRADE_BP = Decimal("50")

# A walk into the band arrives as the touch print plus the band print.
FILLS_WALKING_THE_BAND = 2
# An entry and an exit, so a size the book cannot absorb is refused twice.
LEGS_PER_ROUND_TRIP = 2


def test_an_order_inside_the_touch_pays_no_depth_slippage() -> None:
    walk = walk_depth(depth_profile(), TOUCH_BASE)
    assert walk.is_filled
    assert walk.depth_slippage_bps == Decimal("0")
    assert walk.fill_count == 1
    assert walk.filled_base_quantity == TOUCH_BASE


def test_an_order_beyond_the_touch_walks_the_band_at_an_interpolated_price() -> None:
    """Half the band, so the marginal displacement is 50 bp and the average is 25 bp.

    Weighted by the walked share of the order: 4 of 6 units walked, so
    25 bp * 4/6 = 16.666... bp.
    """
    walk = walk_depth(depth_profile(), Decimal("6"))
    assert walk.is_filled
    assert walk.fill_count == FILLS_WALKING_THE_BAND
    fraction_into_band = (Decimal("6") - TOUCH_BASE) / (BAND_BASE - TOUCH_BASE)
    expected = BAND_WIDTH_BPS * fraction_into_band / Decimal("2") * Decimal("4") / Decimal("6")
    assert walk.depth_slippage_bps == expected


def test_slippage_rises_with_size_all_the_way_to_the_band_edge() -> None:
    walked = [walk_depth(depth_profile(), Decimal(quantity)) for quantity in (3, 5, 7, 9, 10)]
    slippage = [walk.depth_slippage_bps for walk in walked]
    assert all(walk.is_filled for walk in walked)
    assert slippage == sorted(slippage)
    assert slippage[0] < slippage[-1]


def test_an_order_beyond_the_band_is_rejected_as_unfillable() -> None:
    walk = walk_depth(depth_profile(), BAND_BASE + Decimal("0.000001"))
    assert not walk.is_filled
    assert walk.rejection is RejectionReason.UNFILLABLE_DEPTH
    assert walk.filled_base_quantity == Decimal("0")
    assert walk.fill_count == 0


def test_a_rejected_leg_leaves_the_round_trip_uncosted_and_counted() -> None:
    charged = charge_round_trip(
        cost_model(),
        round_trip(base_quantity=BAND_BASE * Decimal("3")),
        SpreadQuantile.P50,
    )
    assert charged.breakdown is None
    assert not charged.is_filled
    # Both legs of the round trip were refused, so the count is two.
    assert charged.rejections_by_reason == {RejectionReason.UNFILLABLE_DEPTH: 2}


def test_rejections_appear_in_the_run_report_and_do_not_contribute_a_cost() -> None:
    report = assess_run(
        cost_model(),
        [round_trip(base_quantity=Decimal("1")), round_trip(base_quantity=BAND_BASE * 3)],
        gross_edge_per_trade_bp=GROSS_EDGE_PER_TRADE_BP,
        quantile=SpreadQuantile.P50,
    )
    assert report.filled_trade_count == 1
    assert report.rejections_by_reason[RejectionReason.UNFILLABLE_DEPTH] == LEGS_PER_ROUND_TRIP
    # The report describes the one trade that happened, not the two that were asked for.
    assert report.gross_return_bps == GROSS_EDGE_PER_TRADE_BP


def test_size_is_magnitude_so_a_short_consumes_the_same_book() -> None:
    assert walk_depth(depth_profile(), Decimal("-6")) == walk_depth(depth_profile(), Decimal("6"))


def test_a_zero_quantity_is_a_caller_error_rather_than_a_market_outcome() -> None:
    """Reporting it as UNFILLABLE_DEPTH would put a modelling bug into the capacity
    report as a finding about the book."""
    with pytest.raises(CostModelConfigError, match="must be non-zero"):
        walk_depth(depth_profile(), Decimal("0"))


def test_a_band_narrower_than_the_touch_is_refused() -> None:
    with pytest.raises(ValidationError, match="is cumulative"):
        DepthProfile(depth_at_touch_base=Decimal("10"), band_1pct_base=Decimal("2"))


def test_a_symbol_with_no_calibrated_depth_is_refused_rather_than_defaulted() -> None:
    with pytest.raises(CostModelConfigError, match="no calibrated depth profile"):
        cost_model().depth_profile_for("ETHUSDT")


def test_a_passive_leg_pays_the_markout_instead_of_the_walk() -> None:
    """A resting order does not cross the book, so it does not walk it -- but it is
    disproportionately the order that filled because the market came to it."""
    model = cost_model()
    passive = charge_round_trip(
        model, round_trip(base_quantity=Decimal("6"), is_passive=True), SpreadQuantile.P50
    )
    marketable = charge_round_trip(
        model, round_trip(base_quantity=Decimal("6"), is_passive=False), SpreadQuantile.P50
    )
    assert passive.breakdown is not None
    assert marketable.breakdown is not None
    assert passive.breakdown.depth_slippage_bps == Decimal("0")
    assert passive.breakdown.spread_bps == Decimal("0")
    assert passive.breakdown.partial_fill_bps > Decimal("0")
    assert marketable.breakdown.depth_slippage_bps > Decimal("0")
