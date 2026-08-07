"""Twelve re-fits charge twelve trials, and a fold that crashes stays in the denominator.

The two failures being tested for are both arithmetic that flatters when it is wrong: a
run that charges once for a twelve-configuration search understates the selection pool by
an order of magnitude and deflates nothing, and a run that drops its crashed folds reports
a fold count conditioned on success.

The charger here records fold indices in a list rather than writing to Postgres, so these
are unit tests of the accounting: `run_walk_forward` invokes the charge exactly once per
planned fold, before the evaluator, whatever the evaluator does.

The binding to the real `trial_ledger` -- twelve folds leaving twelve rows that no crash
can refund -- is `tests/backtest/test_walk_forward_ledger_charges.py`, against real
Postgres. Both files are needed and neither subsumes the other: a list can show the call
count without showing durability, and the database test is too slow to carry the dozen
edge cases below.
"""

from __future__ import annotations

from collections.abc import Sequence
from datetime import timedelta
from decimal import Decimal

import pytest

from fking.backtest.walkforward import (
    Fold,
    FoldEvaluationError,
    OutOfSampleObservation,
    WalkForwardConfigError,
    WalkForwardReport,
    WalkForwardScheme,
    build_schedule,
    run_walk_forward,
    segment_windows,
)
from tests.backtest.walk_forward_support import plan_for

pytestmark = pytest.mark.unit

SEGMENT_SPAN = timedelta(days=5)

#: The issue's acceptance criterion: twelve re-fits leave twelve ledger charges.
REFIT_TOTAL = 12
CRASHING_FOLDS = frozenset({3, 7})
SURVIVING_FOLDS = REFIT_TOTAL - len(CRASHING_FOLDS)


class RecordingLedger:
    """A stand-in for the trial ledger that remembers what it was asked to charge."""

    def __init__(self) -> None:
        self.charged: list[int] = []

    def __call__(self, fold: Fold) -> None:
        self.charged.append(fold.fold_index)


def _flat_observations(fold: Fold) -> tuple[OutOfSampleObservation, ...]:
    """A stationary edge that decays slightly, so every fold contributes real points."""
    return tuple(
        OutOfSampleObservation(
            fold_index=fold.fold_index,
            observed_from_utc=opened_at,
            observed_to_utc=closed_at,
            sharpe_ratio=Decimal("1.0") - Decimal("0.01") * index,
        )
        for index, (opened_at, closed_at) in enumerate(segment_windows(fold, SEGMENT_SPAN))
    )


def _run(
    *,
    fold_target: int = REFIT_TOTAL,
    failing_indices: frozenset[int] = frozenset(),
) -> tuple[WalkForwardReport, RecordingLedger]:
    schedule = build_schedule(plan_for(WalkForwardScheme.ANCHORED, fold_target=fold_target))
    ledger = RecordingLedger()

    def evaluate(fold: Fold) -> Sequence[OutOfSampleObservation]:
        if fold.fold_index in failing_indices:
            raise FoldEvaluationError(
                f"fold {fold.fold_index}: the archive could not serve the training window"
            )
        return _flat_observations(fold)

    return run_walk_forward(schedule, evaluate=evaluate, charge=ledger), ledger


def test_twelve_refits_leave_twelve_charges_in_fold_order() -> None:
    report, ledger = _run(fold_target=REFIT_TOTAL)

    assert report.folds_planned == REFIT_TOTAL
    assert report.trials_charged == REFIT_TOTAL
    assert ledger.charged == list(range(REFIT_TOTAL))


def test_a_crashed_fold_is_charged_and_surfaces_as_folds_failed() -> None:
    report, ledger = _run(fold_target=REFIT_TOTAL, failing_indices=CRASHING_FOLDS)

    assert report.folds_planned == REFIT_TOTAL
    assert report.folds_completed == SURVIVING_FOLDS
    assert report.fold_failure_total == len(CRASHING_FOLDS)
    assert [failure.fold_index for failure in report.folds_failed] == sorted(CRASHING_FOLDS)
    assert "could not serve the training window" in report.folds_failed[0].reason
    # The charge is taken before the evaluator, so a crash cannot avoid it.
    assert ledger.charged == list(range(REFIT_TOTAL))
    assert report.trials_charged == report.folds_planned


def test_a_crashed_fold_never_reduces_the_planned_fold_count() -> None:
    """A fold count conditioned on success is a fold count designed to flatter."""
    every_fold_fails = frozenset(range(REFIT_TOTAL))

    report, ledger = _run(fold_target=REFIT_TOTAL, failing_indices=every_fold_fails)

    assert report.folds_planned == REFIT_TOTAL
    assert report.folds_completed == 0
    assert report.fold_failure_total == REFIT_TOTAL
    assert report.trials_charged == REFIT_TOTAL
    assert ledger.charged == list(range(REFIT_TOTAL))


def test_a_run_whose_folds_all_failed_reports_no_decay_rather_than_a_flat_one() -> None:
    report, _ledger = _run(fold_target=REFIT_TOTAL, failing_indices=frozenset(range(REFIT_TOTAL)))

    assert report.decay is None
    assert "at least 2 observations" in report.decay_refusal


def test_a_completed_run_reports_a_decay_slope_over_every_surviving_fold() -> None:
    report, _ledger = _run(fold_target=REFIT_TOTAL)

    assert report.decay is not None
    assert report.decay.folds_scored == REFIT_TOTAL
    assert report.decay.sharpe_per_30_days < Decimal(0)


def test_the_surviving_folds_still_carry_their_decay_when_some_folds_crash() -> None:
    report, _ledger = _run(fold_target=REFIT_TOTAL, failing_indices=frozenset({0, REFIT_TOTAL - 1}))

    assert report.decay is not None
    assert report.decay.folds_scored == SURVIVING_FOLDS
    assert {point.fold_index for point in report.decay.points} == set(range(1, REFIT_TOTAL - 1))


def test_an_evaluator_failure_outside_the_taxonomy_kills_the_run() -> None:
    """A defect in the code under test must not be recorded as a validation result.

    `AttributeError` here stands for the whole class of failure this harness cannot
    reason about. Recording it would turn a visible crash into a walk-forward report with
    a slightly smaller fold count, which is exactly the conversion `CLAUDE.md` section 11
    forbids.
    """
    schedule = build_schedule(plan_for(WalkForwardScheme.ANCHORED, fold_target=3))
    ledger = RecordingLedger()

    def evaluate(fold: Fold) -> Sequence[OutOfSampleObservation]:
        if fold.fold_index == 1:
            raise AttributeError("'NoneType' object has no attribute 'close_quote_price'")
        return _flat_observations(fold)

    with pytest.raises(AttributeError):
        run_walk_forward(schedule, evaluate=evaluate, charge=ledger)

    # The fold that crashed was still charged before it ran; the ledger is monotone and
    # nothing here refunds it.
    assert ledger.charged == [0, 1]


def test_a_report_whose_folds_do_not_account_for_the_plan_is_refused() -> None:
    """The invariant is asserted by the report, not left to the reader to check."""
    schedule = build_schedule(plan_for(WalkForwardScheme.ANCHORED, fold_target=4))

    with pytest.raises(WalkForwardConfigError, match="folds accounted for"):
        WalkForwardReport(
            schedule=schedule,
            folds_completed=3,
            folds_failed=(),
            trials_charged=4,
            observations=(),
            decay=None,
            decay_refusal="",
        )


def test_a_report_charging_fewer_trials_than_folds_is_refused() -> None:
    schedule = build_schedule(plan_for(WalkForwardScheme.ANCHORED, fold_target=4))

    with pytest.raises(WalkForwardConfigError, match="trials charged"):
        WalkForwardReport(
            schedule=schedule,
            folds_completed=4,
            folds_failed=(),
            trials_charged=1,
            observations=(),
            decay=None,
            decay_refusal="",
        )
