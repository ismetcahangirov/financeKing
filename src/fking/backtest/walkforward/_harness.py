"""Running the schedule: one charge per re-fit, and a crashed fold that stays counted.

Two properties, both of which are about arithmetic that flatters when it is wrong.

**Every fold charges the ledger, before it is evaluated.** A re-fit is a distinct
configuration evaluated against a distinct context, so twelve re-fits are twelve trials
and not one. The charge is taken at specification time -- before the evaluator is
called -- because `.claude/rules/overfitting-defences.md` charges the declared search
rather than the executed one: a fold abandoned after the first three looked good is
still a selection, and charging on completion prices that selection at zero.

**A fold that fails stays in the denominator.** It is recorded in `folds_failed` with its
reason and it keeps its charge. The alternative -- dropping it -- reduces the fold count
to the folds that worked, which is a fold count conditioned on success, and every
statistic computed over it inherits that conditioning. `BACKTEST_ENGINE.md` section 8
says the same thing about `n_trials`: a count that only counts successes is a count
designed to flatter.

Only `WalkForwardError` is caught here, and specifically not `Exception`. A fold that
fails because the data window could not be served is a condition this harness models and
can report. A fold that fails with an `AttributeError` is a defect in the code under
test, and converting that into a recorded row would turn a visible failure into a
validation result with a slightly smaller fold count (`CLAUDE.md` section 11). It
propagates and kills the run.
"""

from __future__ import annotations

from collections.abc import Callable, Sequence
from dataclasses import dataclass

from fking.backtest.walkforward._decay import (
    DecayReport,
    OutOfSampleObservation,
    decay_report,
)
from fking.backtest.walkforward._errors import (
    DecayReportError,
    WalkForwardConfigError,
    WalkForwardError,
)
from fking.backtest.walkforward._schedule import Fold, WalkForwardSchedule

#: Evaluates one fold: fit on its training window, score sub-windows of its test window.
#: Raising `WalkForwardError` marks the fold failed; anything else kills the run.
FoldEvaluator = Callable[[Fold], Sequence[OutOfSampleObservation]]

#: Charges one trial for one fold. Called before the evaluator, once per fold, always.
TrialCharge = Callable[[Fold], None]


@dataclass(frozen=True, slots=True)
class FoldFailure:
    """A fold that was charged and could not be scored.

    `reason` is the exception's message rather than the exception: a report is written to
    a record and read months later, and a traceback object that outlives its process is
    an object nobody can read back.
    """

    fold_index: int
    reason: str


@dataclass(frozen=True, slots=True)
class WalkForwardReport:
    """What a walk-forward produced, including what it failed to produce.

    `trials_charged` is stated rather than derived by the reader, and the invariant that
    it equals `folds_planned` is asserted here rather than documented. The two numbers
    drifting apart is precisely the shape of the bug this report exists to make
    impossible: a run that quietly charged for the folds it completed.
    """

    schedule: WalkForwardSchedule
    folds_completed: int
    folds_failed: tuple[FoldFailure, ...]
    trials_charged: int
    observations: tuple[OutOfSampleObservation, ...]

    #: `None` when decay could not be estimated -- too few observations, or every one at
    #: the same distance. Never a zero standing in for "not measured".
    decay: DecayReport | None

    #: Why `decay` is `None`, when it is. Empty otherwise.
    decay_refusal: str

    def __post_init__(self) -> None:
        planned = self.folds_planned
        accounted = self.folds_completed + len(self.folds_failed)
        if accounted != planned:
            raise WalkForwardConfigError(
                f"{accounted} folds accounted for against {planned} planned; a fold that "
                f"neither completed nor failed has left the count"
            )
        if self.trials_charged != planned:
            raise WalkForwardConfigError(
                f"{self.trials_charged} trials charged against {planned} folds planned; "
                f"every re-fit is a distinct configuration and charges once"
            )

    @property
    def folds_planned(self) -> int:
        """The fold count the schedule specified, which failures do not reduce."""
        return self.schedule.fold_total

    @property
    def fold_failure_total(self) -> int:
        """How many folds were charged and could not be scored."""
        return len(self.folds_failed)


def run_walk_forward(
    schedule: WalkForwardSchedule,
    *,
    evaluate: FoldEvaluator,
    charge: TrialCharge,
) -> WalkForwardReport:
    """Charge and evaluate every fold in order, then fit the decay slope.

    `charge` and `evaluate` are injected rather than constructed. The ledger write is a
    database effect and the evaluation is a full backtest; neither belongs inside a
    function whose job is the accounting between them, and injecting both is what lets
    the accounting be tested without either.
    """
    completed = 0
    failures: list[FoldFailure] = []
    observations: list[OutOfSampleObservation] = []

    for fold in schedule.folds:
        # Before the evaluator, unconditionally. A charge taken afterwards is a charge a
        # crash avoids, and a crash is the case this ordering exists for.
        charge(fold)
        try:
            fold_observations = evaluate(fold)
        except WalkForwardError as failure:
            failures.append(FoldFailure(fold_index=fold.fold_index, reason=str(failure)))
            continue
        observations.extend(fold_observations)
        completed += 1

    decay: DecayReport | None = None
    refusal = ""
    try:
        decay = decay_report(schedule.folds, observations)
    except DecayReportError as refused:
        refusal = str(refused)

    return WalkForwardReport(
        schedule=schedule,
        folds_completed=completed,
        folds_failed=tuple(failures),
        trials_charged=len(schedule.folds),
        observations=tuple(observations),
        decay=decay,
        decay_refusal=refusal,
    )
