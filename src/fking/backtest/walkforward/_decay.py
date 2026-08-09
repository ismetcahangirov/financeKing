"""Out-of-sample decay: performance as a slope against distance from the fit window.

The mean of the fold Sharpes is the number everybody reports and the one that predicts
least. A strategy at -0.09 Sharpe per 30 days of distance is not *wrong* -- it is
**short-lived**. Its usable life is measured in weeks, and either its re-fit cadence is
set to match or it is not promoted. Read on its mean alone that strategy returns a
perfectly respectable number and no information about the thing that is going to happen
to it (`BACKTEST_ENGINE.md` section 6.1).

So the reported quantity is a slope, in Sharpe per 30 days, estimated by ordinary least
squares of each out-of-sample observation's Sharpe on its distance from the end of the
training window that produced it.

**Distance is measured per observation, not per fold.** Every fold in a constant-step
schedule sits at the same distance from its own fit window -- `gap + test_span / 2`,
identically, for all of them -- so a regression over one Sharpe per fold has zero
variance along its x-axis and no slope exists. The measurement requires sub-fold
resolution, which is what `segment_windows` produces and what this module consumes.

**Arithmetic is `Decimal`, not `float`.** `docs/rules/decimal-and-money.md` permits
float64 for statistical estimates inside `fking.backtest`, and this would qualify on the
sampling-error argument. It is `Decimal` anyway for a different reason: the slope is
carried in a validation record that a promotion gate reads and a digest covers, and a
value whose last bits depend on summation order is a value two runs of the same
`config_hash` can disagree about. A determinism failure outranks everything else on the
queue, and this is a cheap place not to create one.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
from typing import Final

from fking.backtest.walkforward._errors import DecayReportError
from fking.backtest.walkforward._guards import require_finite_decimal, require_utc
from fking.backtest.walkforward._schedule import Fold, fold_by_index

#: The reporting unit. `BACKTEST_ENGINE.md` section 12 names `oos_decay_slope` as Sharpe
#: per 30 days of distance, and the unit is fixed there rather than per strategy so that
#: two strategies' decay figures are comparable without a conversion nobody performs.
DECAY_REPORT_DAYS: Final = Decimal("30")

_SECONDS_PER_DAY: Final = Decimal("86400")

# A line through one point is every line. Two is the arithmetic minimum, not a quality
# bar -- `fit_decay` refuses at one and `decay_points` is what makes two meaningful, by
# requiring the two to sit at different distances.
_MINIMUM_OBSERVATIONS: Final = 2


@dataclass(frozen=True, slots=True)
class OutOfSampleObservation:
    """One Sharpe estimate covering one sub-window of one fold's test period.

    `observed_from_utc` and `observed_to_utc` are the sub-window's bounds rather than a
    single instant, so that "is this observation actually out of sample?" is a question
    this module can answer instead of trust. An observation that starts before its fold's
    test window opens is in-sample data being reported as out-of-sample performance,
    which inflates the near-distance end of the regression and flattens the decay.
    """

    fold_index: int
    observed_from_utc: datetime
    observed_to_utc: datetime
    sharpe_ratio: Decimal

    def __post_init__(self) -> None:
        require_utc(self.observed_from_utc, "observed_from_utc", DecayReportError)
        require_utc(self.observed_to_utc, "observed_to_utc", DecayReportError)
        require_finite_decimal(self.sharpe_ratio, "sharpe_ratio", DecayReportError)
        if not isinstance(self.fold_index, int) or isinstance(self.fold_index, bool):
            raise DecayReportError(f"fold_index must be an int, got {self.fold_index!r}")
        if self.fold_index < 0:
            raise DecayReportError(f"fold_index must not be negative; got {self.fold_index}")
        if self.observed_to_utc <= self.observed_from_utc:
            raise DecayReportError(
                f"fold {self.fold_index}: observation window is empty "
                f"({self.observed_from_utc.isoformat()} .. {self.observed_to_utc.isoformat()})"
            )

    @property
    def midpoint_utc(self) -> datetime:
        """The instant the observation's distance is measured from.

        The midpoint rather than either edge: a sub-window's Sharpe is an average over
        its whole span, so attributing it to the start understates its distance and
        attributing it to the end overstates it, and both biases are systematic in the
        same direction for every observation.
        """
        return self.observed_from_utc + (self.observed_to_utc - self.observed_from_utc) / 2


@dataclass(frozen=True, slots=True)
class DecayPoint:
    """One (distance, Sharpe) pair, with the fold it came from still attached."""

    fold_index: int
    distance_days: Decimal
    sharpe_ratio: Decimal


@dataclass(frozen=True, slots=True)
class DecayReport:
    """The slope, its intercept, and the points it was fitted on.

    The points are retained because the slope alone is exactly the kind of single number
    this project spends most of its engineering refusing to accept: a slope of -0.09 from
    forty tightly-clustered points and a slope of -0.09 from four wild ones are different
    findings, and a reader who cannot see which is which has been handed the mean problem
    one level up.
    """

    #: Negative means the edge decays with distance from the fit. This is the number.
    sharpe_per_30_days: Decimal

    #: Fitted Sharpe at zero distance -- the edge the fit window itself would suggest.
    intercept_sharpe: Decimal

    points: tuple[DecayPoint, ...]

    @property
    def observations_scored(self) -> int:
        """How many out-of-sample observations the slope was fitted on."""
        return len(self.points)

    @property
    def folds_scored(self) -> int:
        """How many distinct folds contributed at least one observation."""
        return len({point.fold_index for point in self.points})


def _days_between(earlier: datetime, later: datetime) -> Decimal:
    """Exact days between two aware instants, as a `Decimal`.

    Via `total_seconds` on the `timedelta` would route through a float. The components
    are integers and the division is exact at the process decimal context's precision,
    so the same pair of instants yields the same digits on every machine.
    """
    span = later - earlier
    seconds = Decimal(span.days) * _SECONDS_PER_DAY + Decimal(span.seconds)
    microseconds = Decimal(span.microseconds) / Decimal("1000000")
    return (seconds + microseconds) / _SECONDS_PER_DAY


def decay_points(
    folds: Sequence[Fold], observations: Sequence[OutOfSampleObservation]
) -> tuple[DecayPoint, ...]:
    """Attach each observation to its fold and measure its distance from that fit.

    Refuses an observation that falls outside the test window of the fold it names.
    That refusal is the only defence against the failure this measurement has: an
    observation mislabelled onto a neighbouring fold lands at a plausible distance with a
    plausible Sharpe, changes the slope, and looks like nothing.
    """
    points: list[DecayPoint] = []
    for observation in observations:
        fold = fold_by_index(folds, observation.fold_index)
        if observation.observed_from_utc < fold.test_start_utc:
            raise DecayReportError(
                f"fold {fold.fold_index}: observation opens at "
                f"{observation.observed_from_utc.isoformat()}, before the test window at "
                f"{fold.test_start_utc.isoformat()}; that is in-sample performance "
                f"reported as out-of-sample"
            )
        if observation.observed_to_utc > fold.test_end_utc:
            raise DecayReportError(
                f"fold {fold.fold_index}: observation closes at "
                f"{observation.observed_to_utc.isoformat()}, after the test window at "
                f"{fold.test_end_utc.isoformat()}"
            )
        points.append(
            DecayPoint(
                fold_index=fold.fold_index,
                distance_days=_days_between(fold.train_end_utc, observation.midpoint_utc),
                sharpe_ratio=observation.sharpe_ratio,
            )
        )
    return tuple(points)


def fit_decay(points: Sequence[DecayPoint]) -> DecayReport:
    """Ordinary least squares of Sharpe on distance, reported per 30 days.

    Refuses rather than returns zero when every point shares a distance. Zero there would
    read as "measured, and flat" -- the finding that a strategy does not decay -- when the
    truth is that the schedule produced no variation to measure against. That confusion
    is the whole reason `segment_windows` exists.
    """
    if len(points) < _MINIMUM_OBSERVATIONS:
        raise DecayReportError(
            f"a decay slope needs at least {_MINIMUM_OBSERVATIONS} observations; got {len(points)}"
        )

    observation_total = Decimal(len(points))
    mean_distance = sum((point.distance_days for point in points), Decimal(0)) / observation_total
    mean_sharpe = sum((point.sharpe_ratio for point in points), Decimal(0)) / observation_total

    covariance = Decimal(0)
    variance = Decimal(0)
    for point in points:
        centred_distance = point.distance_days - mean_distance
        covariance += centred_distance * (point.sharpe_ratio - mean_sharpe)
        variance += centred_distance * centred_distance

    if variance == 0:
        raise DecayReportError(
            f"all {len(points)} observations sit at distance {mean_distance} days, so no "
            f"slope exists; score sub-windows of each test period rather than each fold "
            f"as a whole"
        )

    slope_per_day = covariance / variance
    return DecayReport(
        sharpe_per_30_days=slope_per_day * DECAY_REPORT_DAYS,
        intercept_sharpe=mean_sharpe - slope_per_day * mean_distance,
        points=tuple(points),
    )


def decay_report(
    folds: Sequence[Fold], observations: Sequence[OutOfSampleObservation]
) -> DecayReport:
    """Attach observations to folds, then fit. The two-step form is the tested one."""
    return fit_decay(decay_points(folds, observations))
