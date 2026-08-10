"""Running the partition: one charge per path, taken before the path is evaluated.

`N=8, k=2` is 28 paths and therefore 28 permanent charges against the global trial
counter. Two properties make that arithmetic hold under the cases that would otherwise
quietly reduce it.

**The charge is registered as each path is reached, not batched at the end.** A run that
charged once at completion is a run that charges nothing when it crashes at path 19, and
the trial ledger becomes an instrument for laundering failed searches -- start a search,
watch the early paths, abandon it if they look poor, and pay for none of it
(`BACKTEST_ENGINE.md` section 6.2, `docs/rules/overfitting-defences.md`).

**A path that fails stays in the denominator.** It is recorded in `paths_failed` with its
reason and it keeps its charge. Dropping it would reduce the path count to the paths that
worked, which is a path count conditioned on success, and every statistic over it
inherits that conditioning. `BACKTEST_ENGINE.md` section 9 states it directly: a CPCV
path that crashes is recorded as a consumed trial with its error, and `path_count` is
never silently reduced.

Only `CpcvPathEvaluationError` is caught, and specifically not `Exception` and not the
package's own base class. A path that fails because its data window could not be served
is a condition this harness models. A `CpcvPartitionError` escaping the evaluator means a
split leaked and the whole partition is suspect; recording that as one bad path would
convert a structural defect into a slightly smaller path count. It propagates and kills
the run.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from datetime import timedelta

from fking.backtest.cpcv._distribution import (
    DistributionRefusedError,
    PathDistribution,
    PathPerformance,
    path_distribution,
)
from fking.backtest.cpcv._errors import (
    CpcvConfigError,
    CpcvPathEvaluationError,
)
from fking.backtest.cpcv._partition import CpcvPartition, CpcvSplit

#: Evaluates one path: fit on its training intervals, score its test intervals.
#: Raising `CpcvPathEvaluationError` marks the path failed; anything else kills the run.
PathEvaluator = Callable[[CpcvSplit], PathPerformance]

#: Charges one trial for one path. Called before the evaluator, once per path, always.
TrialCharge = Callable[[CpcvSplit], None]


@dataclass(frozen=True, slots=True)
class PathFailure:
    """A path that was charged and could not be scored.

    `reason` is the exception's message rather than the exception: this record is written
    down and read months later, and a traceback object that outlives its process is an
    object nobody can read back.
    """

    path_index: int
    reason: str


@dataclass(frozen=True, slots=True)
class CpcvReport:
    """What a CPCV run produced, including what it failed to produce.

    `trials_charged` is stated rather than left for the reader to derive, and the
    invariant that it equals `path_total` is asserted here rather than documented. The two
    numbers drifting apart is precisely the shape of the bug this report exists to make
    impossible: a run that quietly charged for the paths it completed.
    """

    partition: CpcvPartition
    performances: tuple[PathPerformance, ...]
    paths_failed: tuple[PathFailure, ...]
    trials_charged: int

    #: `None` when no distribution could be computed -- too few paths cleared the trade
    #: floor, or none completed. Never zeros standing in for "not measured".
    distribution: PathDistribution | None

    #: Why `distribution` is `None`, when it is. Empty otherwise.
    distribution_refusal: str

    def __post_init__(self) -> None:
        planned = self.path_total
        accounted = len(self.performances) + len(self.paths_failed)
        if accounted != planned:
            raise CpcvConfigError(
                f"{accounted} paths accounted for against {planned} planned; a path that "
                f"neither completed nor failed has left the count"
            )
        if self.trials_charged != planned:
            raise CpcvConfigError(
                f"{self.trials_charged} trials charged against {planned} paths planned; "
                f"every path is a distinct configuration evaluated against a distinct "
                f"context and charges once"
            )

    @property
    def path_total(self) -> int:
        """The path count the partition specified, which failures do not reduce."""
        return self.partition.path_total

    @property
    def paths_completed(self) -> int:
        """How many paths produced a performance record."""
        return len(self.performances)

    @property
    def path_failure_total(self) -> int:
        """How many paths were charged and could not be scored."""
        return len(self.paths_failed)

    @property
    def purge(self) -> timedelta:
        """The purge this run applied, carried so the result schema states it."""
        return self.partition.purge

    @property
    def embargo(self) -> timedelta:
        """The embargo this run applied, carried so the result schema states it."""
        return self.partition.embargo


def run_cpcv(
    partition: CpcvPartition,
    *,
    evaluate: PathEvaluator,
    charge: TrialCharge,
) -> CpcvReport:
    """Charge and evaluate every path in order, then summarise the distribution.

    `charge` and `evaluate` are injected rather than constructed. The ledger write is a
    database effect and the evaluation is a full backtest; neither belongs inside a
    function whose job is the accounting between them, and injecting both is what lets the
    accounting be tested without either.
    """
    performances: list[PathPerformance] = []
    failures: list[PathFailure] = []

    for split in partition.splits:
        # Before the evaluator, unconditionally. A charge taken afterwards is a charge a
        # crash avoids, and a crash is the case this ordering exists for.
        charge(split)
        try:
            performance = evaluate(split)
        except CpcvPathEvaluationError as failure:
            failures.append(PathFailure(path_index=split.path_index, reason=str(failure)))
            continue
        if performance.path_index != split.path_index:
            # Attribution, not pedantry. A performance recorded against the wrong path
            # pairs a Sharpe with somebody else's training set, and every later question
            # about which groups produced it is answered wrongly and confidently.
            raise CpcvConfigError(
                f"evaluator returned a performance for path {performance.path_index} "
                f"while evaluating path {split.path_index}"
            )
        performances.append(performance)

    distribution: PathDistribution | None = None
    refusal = ""
    try:
        distribution = path_distribution(performances)
    except DistributionRefusedError as refused:
        refusal = str(refused)

    return CpcvReport(
        partition=partition,
        performances=tuple(performances),
        paths_failed=tuple(failures),
        trials_charged=len(partition.splits),
        distribution=distribution,
        distribution_refusal=refusal,
    )
