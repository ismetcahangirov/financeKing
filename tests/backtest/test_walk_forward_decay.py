"""A known half-life produces a negative slope; a stationary edge produces none.

Both synthetic strategies are constructed rather than measured, so the expected answer is
known before the code runs. That is the only way this test can fail usefully: a decay
estimator that returns a plausible negative number for everything passes a test written
against real data and fails the stationary case here.

The stationary edge carries a deterministic sign-alternating perturbation rather than a
seeded random one. A perturbation drawn from an RNG makes the tolerance a statement about
that RNG's particular draw, and the first time somebody changes the seed the assertion is
either vacuous or flaky (`docs/rules/testing-rules.md`). The alternation does not
cancel exactly -- the schedule has an even number of segments per fold, so the +/- pattern
correlates weakly with distance -- and the residual it induces is the noise floor the
tolerance is set from, computed in the comment on that test rather than tuned until green.
"""

from __future__ import annotations

from collections.abc import Callable, Sequence
from datetime import timedelta
from decimal import Decimal

import pytest

from fking.backtest._errors import BacktestError
from fking.backtest.walkforward import (
    DecayReportError,
    Fold,
    OutOfSampleObservation,
    WalkForwardScheme,
    build_schedule,
    decay_report,
    fit_decay,
    segment_windows,
)
from tests.backtest.walk_forward_support import plan_for

pytestmark = pytest.mark.unit

SEGMENT_SPAN = timedelta(days=5)
SEGMENTS_PER_FOLD = 6  # a 30-day test window holds six whole five-day sub-windows

#: Sharpe at zero distance from the fit window, before any decay is applied.
INITIAL_SHARPE = Decimal("1.60")

#: The synthetic strategy's half-life. `BACKTEST_ENGINE.md` section 6.1 works in 30-day
#: units, so a 30-day half-life makes the expected slope readable by hand: half of the
#: initial Sharpe is lost over the first 30 days, so the slope is about -0.80.
HALF_LIFE_DAYS = Decimal("30")

_SECONDS_PER_DAY = Decimal("86400")


def _half_life_sharpe(distance_days: Decimal) -> Decimal:
    """A Sharpe that halves every `HALF_LIFE_DAYS` of distance from the fit.

    Repeated exact halving between integer half-lives plus a linear interpolation inside
    one, rather than `2 ** -x`: the exponential is a float operation and the process
    decimal context traps those on purpose.
    """
    whole_half_lives = int(distance_days / HALF_LIFE_DAYS)
    remainder = distance_days - whole_half_lives * HALF_LIFE_DAYS
    level = INITIAL_SHARPE / Decimal(2) ** whole_half_lives
    return level - (level / 2) * (remainder / HALF_LIFE_DAYS)


def _days_between_span(span: timedelta) -> Decimal:
    return Decimal(span.days) + Decimal(span.seconds) / _SECONDS_PER_DAY


def _observations(
    folds: Sequence[Fold],
    sharpe_of_distance: Callable[[Decimal], Decimal],
    *,
    alternating_noise: Decimal = Decimal(0),
) -> tuple[OutOfSampleObservation, ...]:
    """Score every five-day sub-window of every fold with the supplied Sharpe curve."""
    observations: list[OutOfSampleObservation] = []
    parity = 0
    for fold in folds:
        for opened_at, closed_at in segment_windows(fold, SEGMENT_SPAN):
            midpoint = opened_at + (closed_at - opened_at) / 2
            distance_days = _days_between_span(midpoint - fold.train_end_utc)
            perturbation = alternating_noise if parity % 2 == 0 else -alternating_noise
            observations.append(
                OutOfSampleObservation(
                    fold_index=fold.fold_index,
                    observed_from_utc=opened_at,
                    observed_to_utc=closed_at,
                    sharpe_ratio=sharpe_of_distance(distance_days) + perturbation,
                )
            )
            parity += 1
    return tuple(observations)


def test_a_thirty_day_half_life_produces_a_clearly_negative_decay_slope() -> None:
    schedule = build_schedule(plan_for(WalkForwardScheme.ROLLING))

    report = decay_report(schedule.folds, _observations(schedule.folds, _half_life_sharpe))

    assert report.sharpe_per_30_days < Decimal("-0.50")
    # Half the initial Sharpe is lost over the first 30 days, so a slope steeper than the
    # whole initial level per 30 days would mean the estimator has overshot.
    assert report.sharpe_per_30_days > -INITIAL_SHARPE
    assert report.folds_scored == schedule.fold_total
    assert report.observations_scored == SEGMENTS_PER_FOLD * schedule.fold_total


def test_a_stationary_edge_produces_a_slope_within_noise_of_zero() -> None:
    """The tolerance is derived, not tuned.

    Distances within a fold are 4.5, 9.5, 14.5, 19.5, 24.5 and 29.5 days, so they are
    symmetric about 17 and the +/- pattern contributes
    `0.05 * (-12.5 + 7.5 - 2.5 - 2.5 + 7.5 - 12.5) = -0.75` of covariance per fold
    against `437.5` of variance -- a slope of about -0.051 per 30 days. That is the floor
    a +/-0.05 perturbation can produce, and 0.10 is comfortably above it and far below
    the -0.80 the decaying strategy reports.
    """
    schedule = build_schedule(plan_for(WalkForwardScheme.ROLLING))

    report = decay_report(
        schedule.folds,
        _observations(
            schedule.folds,
            lambda _distance_days: Decimal("0.80"),
            alternating_noise=Decimal("0.05"),
        ),
    )

    assert abs(report.sharpe_per_30_days) < Decimal("0.10")
    assert abs(report.intercept_sharpe - Decimal("0.80")) < Decimal("0.10")


def test_the_decaying_strategy_is_separated_from_the_stationary_one_by_an_order_of_magnitude() -> (
    None
):
    """The contrast is the finding; either slope alone is a number without a scale."""
    schedule = build_schedule(plan_for(WalkForwardScheme.ROLLING))

    decaying = decay_report(schedule.folds, _observations(schedule.folds, _half_life_sharpe))
    stationary = decay_report(
        schedule.folds,
        _observations(
            schedule.folds,
            lambda _distance_days: Decimal("0.80"),
            alternating_noise=Decimal("0.05"),
        ),
    )

    assert abs(stationary.sharpe_per_30_days) * 8 < abs(decaying.sharpe_per_30_days)


def test_the_intercept_is_the_edge_the_fit_window_itself_would_suggest() -> None:
    """A perfectly linear decay recovers both parameters exactly, with no float error."""
    schedule = build_schedule(plan_for(WalkForwardScheme.ROLLING))

    report = decay_report(
        schedule.folds,
        _observations(
            schedule.folds,
            lambda distance_days: Decimal("2.0") - Decimal("0.01") * distance_days,
        ),
    )

    assert report.sharpe_per_30_days == Decimal("-0.30")
    assert report.intercept_sharpe == Decimal("2.0")


def test_scoring_each_fold_as_one_number_is_refused_rather_than_reported_as_flat() -> None:
    """Every fold sits at one identical distance, so a fold-level regression has no x."""
    schedule = build_schedule(plan_for(WalkForwardScheme.ROLLING))
    whole_fold_observations = tuple(
        OutOfSampleObservation(
            fold_index=fold.fold_index,
            observed_from_utc=fold.test_start_utc,
            observed_to_utc=fold.test_end_utc,
            sharpe_ratio=Decimal("1.1"),
        )
        for fold in schedule.folds
    )

    with pytest.raises(DecayReportError, match="no slope exists"):
        decay_report(schedule.folds, whole_fold_observations)


def test_an_observation_reaching_before_its_test_window_is_refused() -> None:
    """In-sample performance reported as out-of-sample flattens the near end of the fit."""
    schedule = build_schedule(plan_for(WalkForwardScheme.ROLLING))
    fold = schedule.folds[1]
    leaking = OutOfSampleObservation(
        fold_index=fold.fold_index,
        observed_from_utc=fold.test_start_utc - timedelta(days=1),
        observed_to_utc=fold.test_start_utc + timedelta(days=4),
        sharpe_ratio=Decimal("3.0"),
    )

    with pytest.raises(DecayReportError, match="in-sample performance"):
        decay_report(schedule.folds, (leaking,))


def test_an_observation_reaching_past_its_test_window_is_refused() -> None:
    schedule = build_schedule(plan_for(WalkForwardScheme.ROLLING))
    fold = schedule.folds[0]
    overrunning = OutOfSampleObservation(
        fold_index=fold.fold_index,
        observed_from_utc=fold.test_end_utc - timedelta(days=1),
        observed_to_utc=fold.test_end_utc + timedelta(days=1),
        sharpe_ratio=Decimal("0.5"),
    )

    with pytest.raises(DecayReportError, match="after the test window"):
        decay_report(schedule.folds, (overrunning,))


def test_a_single_observation_cannot_carry_a_slope() -> None:
    with pytest.raises(DecayReportError, match="at least 2 observations"):
        fit_decay(())


def test_a_float_sharpe_is_refused_at_construction() -> None:
    schedule = build_schedule(plan_for(WalkForwardScheme.ROLLING))
    fold = schedule.folds[0]

    with pytest.raises(BacktestError, match="Decimal"):
        OutOfSampleObservation(
            fold_index=fold.fold_index,
            observed_from_utc=fold.test_start_utc,
            observed_to_utc=fold.test_start_utc + SEGMENT_SPAN,
            sharpe_ratio=1.6,  # type: ignore[arg-type]  # a float Sharpe is the point of the test
        )
