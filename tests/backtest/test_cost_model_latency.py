"""The three latency stages, the fee tier, funding's sign, and the partial-fill terms.

Four small suites in one file, because each is a handful of assertions about one term and
splitting them further would produce files whose docstrings outweigh their content.

The load-bearing ones:

**Latency is three stages, not one number.** `decision_to_send` is the stage usually
omitted and the one this system cannot omit: it computes features, may consult an LLM
agent whose latency is seconds against a free-tier quota, applies risk sizing, and only
then sends (`BACKTEST_ENGINE.md` section 4.4).

**Funding's sign is real.** A short with a positive rate is *paid* to hold, so the term is
a credit. A model that clamped it at zero would delete the entire P&L of a carry strategy.

**Fees default to VIP-0.** Assuming a better tier than the account holds is a way to
manufacture edge, so the worst tier is the default and a better one is stated on purpose.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta, timezone
from decimal import Decimal

import pytest
from pydantic import ValidationError

from fking.backtest.costs import (
    DEFAULT_MAKER_FEE_BPS,
    DEFAULT_PASSIVE_MARKOUT_BPS,
    DEFAULT_TAKER_FEE_BPS,
    ExecutionLeg,
    FeeSchedule,
    FundingExposure,
    LatencyProfile,
    SpreadQuantile,
    charge_leg,
    charge_round_trip,
)
from fking.domain import Direction
from tests.backtest.test_cost_fixtures import SYMBOL, cost_model, execution_leg, round_trip

pytestmark = pytest.mark.unit


def test_the_latency_term_is_the_drift_over_all_three_stages() -> None:
    profile = LatencyProfile(
        decision_to_send=timedelta(milliseconds=250),
        send_to_ack=timedelta(milliseconds=180),
        ack_to_fill=timedelta(milliseconds=95),
        adverse_drift_bps_per_second=Decimal("0.5"),
    )
    assert profile.total_latency == timedelta(milliseconds=525)
    assert profile.total_latency_seconds == Decimal("0.525")
    assert profile.latency_bps() == Decimal("0.2625")


def test_omitting_the_decision_stage_understates_the_latency_cost() -> None:
    """The stage that is usually left out is the one an agent call lives in."""
    with_agent = LatencyProfile(
        decision_to_send=timedelta(seconds=3),
        send_to_ack=timedelta(milliseconds=180),
        ack_to_fill=timedelta(milliseconds=95),
        adverse_drift_bps_per_second=Decimal("0.5"),
    )
    network_only = LatencyProfile(
        decision_to_send=timedelta(0),
        send_to_ack=timedelta(milliseconds=180),
        ack_to_fill=timedelta(milliseconds=95),
        adverse_drift_bps_per_second=Decimal("0.5"),
    )
    assert with_agent.latency_bps() > network_only.latency_bps() * Decimal("10")


def test_a_negative_latency_stage_is_refused() -> None:
    with pytest.raises(ValidationError, match="must not be negative"):
        LatencyProfile(
            decision_to_send=timedelta(milliseconds=-1),
            send_to_ack=timedelta(0),
            ack_to_fill=timedelta(0),
            adverse_drift_bps_per_second=Decimal("0.5"),
        )


def test_fees_default_to_the_worst_tier() -> None:
    schedule = FeeSchedule()
    assert schedule.maker_fee_bps == DEFAULT_MAKER_FEE_BPS
    assert schedule.taker_fee_bps == DEFAULT_TAKER_FEE_BPS
    assert schedule.fee_bps_for(is_passive=True) == DEFAULT_MAKER_FEE_BPS
    assert schedule.fee_bps_for(is_passive=False) == DEFAULT_TAKER_FEE_BPS


def test_a_float_fee_is_refused_rather_than_coerced() -> None:
    """`Decimal(0.1)` is already rounded before this code runs; strict mode stops it."""
    with pytest.raises(ValidationError):
        FeeSchedule(maker_fee_bps=2.0, taker_fee_bps=5.0)  # type: ignore[arg-type]  # the point of the test is that the annotation is violated


def test_a_long_pays_funding_and_a_short_is_paid() -> None:
    rate = Decimal("0.0001")
    long_side = FundingExposure(direction=Direction.LONG, funding_rate=rate, settlement_count=3)
    short_side = FundingExposure(direction=Direction.SHORT, funding_rate=rate, settlement_count=3)

    assert long_side.funding_bps() == Decimal("3")
    assert short_side.funding_bps() == Decimal("-3")
    assert FundingExposure.none_held().funding_bps() == Decimal("0")


def test_funding_is_charged_per_settlement_held_through() -> None:
    """A strategy that flattens before settlement pays nothing; one that holds pays."""
    rate = Decimal("0.0001")
    flattened = FundingExposure(direction=Direction.LONG, funding_rate=rate, settlement_count=0)
    held = FundingExposure(direction=Direction.LONG, funding_rate=rate, settlement_count=2)
    assert flattened.funding_bps() == Decimal("0")
    assert held.funding_bps() == Decimal("2")


def test_a_negative_settlement_count_is_refused() -> None:
    with pytest.raises(ValidationError, match="must not be negative"):
        FundingExposure(direction=Direction.LONG, funding_rate=Decimal("0"), settlement_count=-1)


def test_a_walked_order_pays_the_requote_cost_and_a_touch_order_does_not() -> None:
    model = cost_model()
    within_touch = charge_leg(model, execution_leg(base_quantity=Decimal("1")), SpreadQuantile.P50)
    walked = charge_leg(model, execution_leg(base_quantity=Decimal("6")), SpreadQuantile.P50)

    assert within_touch.partial_fill_bps == Decimal("0")
    assert walked.partial_fill_bps == model.partial_fills.requote_cost_bps_per_extra_fill


def test_a_passive_leg_pays_the_measured_adverse_selection_markout() -> None:
    model = cost_model()
    passive = charge_leg(
        model, execution_leg(base_quantity=Decimal("1"), is_passive=True), SpreadQuantile.P50
    )
    assert passive.partial_fill_bps == DEFAULT_PASSIVE_MARKOUT_BPS
    assert passive.fees_bps == DEFAULT_MAKER_FEE_BPS


def test_a_leg_is_charged_no_funding_because_funding_belongs_to_the_round_trip() -> None:
    charged = charge_leg(cost_model(), execution_leg(), SpreadQuantile.P50)
    assert charged.funding_bps == Decimal("0")


def test_a_round_trip_charges_both_legs_and_the_funding_once() -> None:
    model = cost_model()
    leg = charge_leg(model, execution_leg(hour_utc=12), SpreadQuantile.P50)
    funding = FundingExposure(
        direction=Direction.LONG, funding_rate=Decimal("0.0002"), settlement_count=1
    )
    charged = charge_round_trip(model, round_trip(hour_utc=12, funding=funding), SpreadQuantile.P50)

    assert charged.is_filled
    assert charged.breakdown is not None
    assert charged.breakdown.fees_bps == leg.fees_bps * Decimal("2")
    assert charged.breakdown.spread_bps == leg.spread_bps * Decimal("2")
    assert charged.breakdown.latency_bps == leg.latency_bps * Decimal("2")
    assert charged.breakdown.funding_bps == funding.funding_bps()


def test_a_leg_stamped_outside_utc_is_refused_rather_than_converted() -> None:
    """The hour of the decision selects the spread quantile, so an offset silently
    reprices the trade into a different hour -- often a funding hour."""
    baku = timezone(timedelta(hours=4))
    with pytest.raises(ValidationError, match="must be UTC"):
        ExecutionLeg(
            symbol=SYMBOL,
            base_quantity=Decimal("1"),
            decided_at_utc=datetime(2026, 5, 14, 12, 0, tzinfo=baku),
            is_passive=False,
        )


def test_a_naive_leg_timestamp_is_refused() -> None:
    """A naive datetime is a wall-clock reading with no record of which wall."""
    naive = datetime(2026, 5, 14, 12, 0, tzinfo=UTC).replace(tzinfo=None)
    with pytest.raises(ValidationError, match="timezone-aware"):
        ExecutionLeg(
            symbol=SYMBOL,
            base_quantity=Decimal("1"),
            decided_at_utc=naive,
            is_passive=False,
        )
