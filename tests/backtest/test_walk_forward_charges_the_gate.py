"""The fold count a walk-forward charges is the number the overfitting gate deflates by.

`WalkForwardReport.trials_charged` and `SharpeEvidence.trials_at_time_of_run` are the two
halves of one claim, written in two packages, and nothing in either package proves they
meet. This file is that proof: the same observed Sharpe passes the gate when it came from
a search of one configuration and fails when the walk-forward that produced it charged
sixty re-fits.

That is the property the charge exists for. A walk-forward that charged once for a
twelve-configuration search would understate `SR*` by the square root of a logarithm --
a correction that grows so slowly it never looks urgent, applies to every strategy in the
population at once, and never comes back down.

The evidence's trial count is the *global* ledger figure, not this run's contribution, and
the tests below construct it as such. They pass `report.trials_charged` as the whole count
only in the case where the ledger holds nothing else, which is stated in each test rather
than assumed -- conflating "what this run added" with "what has ever been searched" is the
same understatement one level up.
"""

from __future__ import annotations

from collections.abc import Sequence
from datetime import timedelta
from decimal import Decimal

import pytest

from fking.backtest.validation import (
    PathSplit,
    SharpeEvidence,
    ValidationRefusal,
    assess_validation,
    expected_max_sharpe,
)
from fking.backtest.walkforward import (
    Fold,
    FoldEvaluationError,
    OutOfSampleObservation,
    WalkForwardReport,
    WalkForwardScheme,
    build_schedule,
    run_walk_forward,
    segment_windows,
)
from tests.backtest.walk_forward_support import plan_for

pytestmark = pytest.mark.unit

SEGMENT_SPAN = timedelta(days=5)

#: A Sharpe that clears the gate when nothing was searched to find it, and still clears
#: it after twelve re-fits. Chosen at the boundary on purpose: a figure high enough to
#: survive any search would make the test pass for a reason unrelated to the fold count.
OBSERVED_SHARPE = Decimal("1.2")

#: Dispersion of Sharpe across the configurations searched. Drives `SR*` directly.
SHARPE_VARIANCE_ACROSS_TRIALS = Decimal("0.25")

#: The issue's acceptance criterion: a run with twelve re-fits charges twelve trials.
REFIT_TOTAL = 12

#: A search wide enough that the selection benchmark alone sinks a 1.9 Sharpe.
WIDE_REFIT_TOTAL = 60

#: Every even-indexed fold crashes, so half of a REFIT_TOTAL run survives.
SURVIVING_FOLDS = REFIT_TOTAL // 2


def _observations(fold: Fold) -> Sequence[OutOfSampleObservation]:
    return tuple(
        OutOfSampleObservation(
            fold_index=fold.fold_index,
            observed_from_utc=opened_at,
            observed_to_utc=closed_at,
            sharpe_ratio=OBSERVED_SHARPE - Decimal("0.01") * index,
        )
        for index, (opened_at, closed_at) in enumerate(segment_windows(fold, SEGMENT_SPAN))
    )


def _walk_forward(fold_target: int) -> WalkForwardReport:
    schedule = build_schedule(plan_for(WalkForwardScheme.ANCHORED, fold_target=fold_target))
    return run_walk_forward(schedule, evaluate=_observations, charge=lambda _fold: None)


def _evidence(trial_count: int) -> SharpeEvidence:
    return SharpeEvidence(
        observed_sharpe=OBSERVED_SHARPE,
        trials_at_time_of_run=trial_count,
        independent_episode_count=60,
        skewness=Decimal("0"),
        kurtosis=Decimal("3"),
        sharpe_variance_across_trials=SHARPE_VARIANCE_ACROSS_TRIALS,
    )


def _honest_splits() -> tuple[PathSplit, ...]:
    """Path splits whose in-sample winner also wins out of sample, so PBO is not the
    binding constraint and the deflated Sharpe is what moves."""
    return tuple(
        PathSplit(
            in_sample_sharpe=(Decimal("1.9"), Decimal("0.4"), Decimal("0.1")),
            out_of_sample_sharpe=(Decimal("1.7"), Decimal("0.3"), Decimal("0.0")),
        )
        for _ in range(8)
    )


def test_the_walk_forward_charge_is_the_count_the_gate_deflates_by() -> None:
    """Twelve re-fits are twelve trials at the gate, not one run."""
    report = _walk_forward(fold_target=REFIT_TOTAL)

    assert report.trials_charged == REFIT_TOTAL
    # The ledger holds nothing but this run, so its contribution is the whole count.
    evidence = _evidence(report.trials_charged)
    assert evidence.trials_at_time_of_run == report.trials_charged


def test_a_wider_search_raises_the_benchmark_the_same_sharpe_must_beat() -> None:
    narrow = _walk_forward(fold_target=2)
    wide = _walk_forward(fold_target=WIDE_REFIT_TOTAL)

    narrow_benchmark = expected_max_sharpe(narrow.trials_charged, SHARPE_VARIANCE_ACROSS_TRIALS)
    wide_benchmark = expected_max_sharpe(wide.trials_charged, SHARPE_VARIANCE_ACROSS_TRIALS)

    assert wide.trials_charged == WIDE_REFIT_TOTAL
    assert wide_benchmark > narrow_benchmark


def test_the_same_sharpe_survives_twelve_refits_and_fails_sixty() -> None:
    """The verdict flips on the fold count alone: same returns, same splits, same Sharpe.

    Three points rather than two, because the interesting claim is not "a big search
    fails" -- it is that the gate is sensitive across the range a real walk-forward
    occupies. Twelve re-fits is a quarterly re-fit over three years and still clears the
    floor; sixty is a monthly re-fit over five and does not.

    This is the assertion that would silently stop meaning anything if `trials_charged`
    ever became the count of folds that *completed* -- the failing folds would drop out,
    the benchmark would fall, and a searched result would report as an unsearched one.
    """
    splits = _honest_splits()

    unsearched = assess_validation(_evidence(1), splits)
    modest = assess_validation(
        _evidence(_walk_forward(fold_target=REFIT_TOTAL).trials_charged), splits
    )
    wide = assess_validation(
        _evidence(_walk_forward(fold_target=WIDE_REFIT_TOTAL).trials_charged), splits
    )

    assert unsearched.is_evidence
    assert modest.is_evidence
    assert not wide.is_evidence
    assert wide.refusals == (ValidationRefusal.DEFLATED_SHARPE_BELOW_FLOOR,)
    assert (
        wide.selection_benchmark_sharpe
        > modest.selection_benchmark_sharpe
        > unsearched.selection_benchmark_sharpe
    )


def test_failed_folds_keep_their_charge_at_the_gate() -> None:
    """A crashed fold still widened the search, so it still raises the benchmark."""
    schedule = build_schedule(plan_for(WalkForwardScheme.ANCHORED, fold_target=REFIT_TOTAL))

    def half_of_them_crash(fold: Fold) -> Sequence[OutOfSampleObservation]:
        if fold.fold_index % 2 == 0:
            raise FoldEvaluationError(f"fold {fold.fold_index}: the archive gapped")
        return _observations(fold)

    report = run_walk_forward(schedule, evaluate=half_of_them_crash, charge=lambda _fold: None)

    assert report.folds_completed == SURVIVING_FOLDS
    assert report.fold_failure_total == SURVIVING_FOLDS
    assert report.trials_charged == REFIT_TOTAL
    charged_benchmark = expected_max_sharpe(report.trials_charged, SHARPE_VARIANCE_ACROSS_TRIALS)
    completed_benchmark = expected_max_sharpe(report.folds_completed, SHARPE_VARIANCE_ACROSS_TRIALS)
    assert charged_benchmark > completed_benchmark
