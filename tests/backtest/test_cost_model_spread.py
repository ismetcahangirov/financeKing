"""Spread is a distribution with an hour-of-day profile, and p99 is strictly worse.

Three claims, asserted rather than assumed:

**The p99 run is worse than the p50 run.** `BACKTEST_ENGINE.md` section 4.2 makes the p99
re-run the robustness check, and a re-run that is not strictly more expensive is not a
check -- it is a second copy of the same number. This is the one that would silently stop
working if `SpreadQuantile` were ever wired the wrong way round, because both runs would
still produce plausible output.

**The funding hours cost more.** BTCUSDT's spread roughly doubles around 00:00, 08:00 and
16:00 UTC. A strategy that concentrates its entries there against a flat median is being
subsidised by the cost model in exactly the hours it chose, which is a selection effect
rather than a rounding error.

**A profile missing an hour is refused.** Filling the gap from the daily median restores
the subsidy the profile exists to remove, so the gap is a construction failure.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta, timezone
from decimal import Decimal

import pytest
from pydantic import ValidationError

from fking.backtest.costs import (
    HOURS_IN_DAY,
    CostModelConfigError,
    SpreadObservation,
    SpreadQuantile,
    SpreadQuantiles,
    SymbolSpreadProfile,
    assess_run,
    calibrate_spread_profile,
)
from tests.backtest.test_cost_fixtures import (
    BASE_P50_BPS,
    BASE_P99_BPS,
    FUNDING_HOURS,
    cost_model,
    default_round_trips,
    round_trip,
)

pytestmark = pytest.mark.unit

GROSS_EDGE_PER_TRADE_BP = Decimal("50")

# One of the three UTC funding settlements; used as the hour deliberately left unobserved.
SETTLEMENT_HOUR_UTC = 8


def test_the_p99_run_is_strictly_worse_than_the_p50_run() -> None:
    model = cost_model()
    trades = default_round_trips()

    at_p50 = assess_run(
        model,
        trades,
        gross_edge_per_trade_bp=GROSS_EDGE_PER_TRADE_BP,
        quantile=SpreadQuantile.P50,
    )
    at_p99 = assess_run(
        model,
        trades,
        gross_edge_per_trade_bp=GROSS_EDGE_PER_TRADE_BP,
        quantile=SpreadQuantile.P99,
    )

    assert at_p99.round_trip_cost_bp > at_p50.round_trip_cost_bp
    assert at_p99.net_edge_per_trade_bp < at_p50.net_edge_per_trade_bp
    assert at_p99.net_return_bps < at_p50.net_return_bps
    assert at_p99.total_cost_bps > at_p50.total_cost_bps
    # Gross is a property of the strategy, not of the book it traded against.
    assert at_p99.gross_return_bps == at_p50.gross_return_bps
    # Only the spread term moved. If anything else did, the quantile has leaked into a
    # term it does not govern.
    assert at_p99.breakdown.spread_bps > at_p50.breakdown.spread_bps
    assert at_p99.breakdown.fees_bps == at_p50.breakdown.fees_bps
    assert at_p99.breakdown.depth_slippage_bps == at_p50.breakdown.depth_slippage_bps
    assert at_p99.breakdown.latency_bps == at_p50.breakdown.latency_bps


def test_a_funding_hour_entry_costs_more_than_the_same_trade_an_hour_later() -> None:
    model = cost_model()
    at_settlement = assess_run(
        model,
        [round_trip(hour_utc=8)],
        gross_edge_per_trade_bp=GROSS_EDGE_PER_TRADE_BP,
        quantile=SpreadQuantile.P50,
    )
    an_hour_later = assess_run(
        model,
        [round_trip(hour_utc=9)],
        gross_edge_per_trade_bp=GROSS_EDGE_PER_TRADE_BP,
        quantile=SpreadQuantile.P50,
    )
    assert at_settlement.round_trip_cost_bp > an_hour_later.round_trip_cost_bp


@pytest.mark.parametrize("hour_utc", sorted(FUNDING_HOURS))
def test_every_funding_hour_carries_the_wider_spread(hour_utc: int) -> None:
    profile = cost_model().spread_profile_for("BTCUSDT")
    assert profile.spread_bps(hour_utc, SpreadQuantile.P50) == BASE_P50_BPS * Decimal("2")
    assert profile.spread_bps(hour_utc, SpreadQuantile.P99) == BASE_P99_BPS * Decimal("2")


def test_a_marketable_leg_pays_half_the_quoted_spread() -> None:
    profile = cost_model().spread_profile_for("BTCUSDT")
    quoted = profile.spread_bps(12, SpreadQuantile.P50)
    assert profile.half_spread_bps(12, SpreadQuantile.P50) * Decimal("2") == quoted


def test_a_profile_missing_an_hour_is_refused() -> None:
    incomplete = {
        hour: SpreadQuantiles(p50_bps=BASE_P50_BPS, p99_bps=BASE_P99_BPS)
        for hour in range(HOURS_IN_DAY - 1)
    }
    with pytest.raises(ValidationError, match="all 24 UTC hours"):
        SymbolSpreadProfile(hourly=incomplete)


def test_transposed_quantiles_are_refused() -> None:
    """p99 below p50 makes the robustness run the cheaper one, silently."""
    with pytest.raises(ValidationError, match="transposed"):
        SpreadQuantiles(p50_bps=Decimal("2.0"), p99_bps=Decimal("0.5"))


def test_a_symbol_with_no_calibrated_spread_is_refused_rather_than_defaulted() -> None:
    with pytest.raises(CostModelConfigError, match="no calibrated spread profile"):
        cost_model().spread_profile_for("ETHUSDT")


def test_calibration_takes_quantiles_per_hour_by_nearest_rank() -> None:
    """Sixty samples per hour, spread 0.01 .. 0.60 bp.

    Nearest rank puts p50 at element ceil(0.50 * 60) = 30 and p99 at ceil(0.99 * 60) = 60,
    so the p99 is the worst observation rather than an interpolated value between the two
    neighbouring order statistics -- which is what keeps the calibration exact and keeps a
    float out of the field that decides whether a strategy is profitable.
    """
    observations = [
        SpreadObservation(
            observed_at_utc=datetime(2026, 5, 14, hour, minute, tzinfo=UTC),
            spread_bps=Decimal(minute + 1) / Decimal("100"),
        )
        for hour in range(HOURS_IN_DAY)
        for minute in range(60)
    ]
    profile = calibrate_spread_profile(
        observations, calibration_source="binance_um_production_2026-05"
    )
    assert profile.spread_bps(3, SpreadQuantile.P50) == Decimal("0.30")
    assert profile.spread_bps(3, SpreadQuantile.P99) == Decimal("0.60")


def test_calibration_refuses_an_unobserved_hour() -> None:
    observations = [
        SpreadObservation(
            observed_at_utc=datetime(2026, 5, 14, hour, 0, tzinfo=UTC),
            spread_bps=BASE_P50_BPS,
        )
        for hour in range(HOURS_IN_DAY)
        if hour != SETTLEMENT_HOUR_UTC
    ]
    with pytest.raises(CostModelConfigError, match=r"UTC hours \[8\]"):
        calibrate_spread_profile(observations, calibration_source="binance_um_production_2026-05")


def test_a_naive_observation_is_refused() -> None:
    """A naive sample cannot be bucketed: nothing records which wall clock produced it."""
    naive = datetime(2026, 5, 14, 12, 0, tzinfo=UTC).replace(tzinfo=None)
    with pytest.raises(ValidationError, match="timezone-aware"):
        SpreadObservation(observed_at_utc=naive, spread_bps=BASE_P50_BPS)


def test_an_observation_stamped_outside_utc_is_refused() -> None:
    """An offset here shifts every sample into the wrong hour bucket."""
    baku = timezone(timedelta(hours=4))
    with pytest.raises(ValidationError, match="must be UTC"):
        SpreadObservation(
            observed_at_utc=datetime(2026, 5, 14, 12, 0, tzinfo=baku),
            spread_bps=BASE_P50_BPS,
        )
