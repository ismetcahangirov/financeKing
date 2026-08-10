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

import pickle
from collections.abc import Callable
from concurrent.futures import ProcessPoolExecutor
from dataclasses import dataclass
from datetime import timedelta

from fking.backtest._workers import WorkerMemoryBudget, resolve_worker_total
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


def _attribute(performance: PathPerformance, split: CpcvSplit) -> PathPerformance:
    """Check that a returned performance belongs to the path it was asked for.

    Attribution, not pedantry. A performance recorded against the wrong path pairs a
    Sharpe with somebody else's training set, and every later question about which groups
    produced it is answered wrongly and confidently. It matters more under a worker pool
    than in-process, because there the results arrive from another address space and
    nothing else ties one to its request.
    """
    if performance.path_index != split.path_index:
        raise CpcvConfigError(
            f"evaluator returned a performance for path {performance.path_index} "
            f"while evaluating path {split.path_index}"
        )
    return performance


def _run_sequentially(
    partition: CpcvPartition,
    *,
    evaluate: PathEvaluator,
    charge: TrialCharge,
) -> tuple[list[PathPerformance], list[PathFailure]]:
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
        performances.append(_attribute(performance, split))
    return performances, failures


def _run_in_parallel(
    partition: CpcvPartition,
    *,
    evaluate: PathEvaluator,
    charge: TrialCharge,
    worker_total: int,
) -> tuple[list[PathPerformance], list[PathFailure]]:
    """Evaluate the paths across a bounded process pool, collecting in path order.

    Processes rather than threads. The event loop is CPython bytecode over `Decimal`, so
    threads would serialise on the GIL and buy nothing but a second source of ordering.

    Every path is charged before the first process starts, which is stricter than the
    sequential path rather than weaker: a charge cannot be skipped by a crash that happens
    while another path is still in flight. `charge` stays in the parent because it is a
    database write against the one connection this run owns.

    Results are collected in `partition.splits` order regardless of completion order, so
    two runs of one partition produce byte-identical reports whatever the scheduler did.
    """
    try:
        pickle.dumps(evaluate)
    except (AttributeError, TypeError, pickle.PicklingError) as unpicklable:
        # Checked here rather than left to the pool. `ProcessPoolExecutor` reports the
        # same defect as a `BrokenProcessPool` several frames away from the closure that
        # caused it, and the usual cause -- an evaluator defined inside the caller's
        # function -- is invisible in that traceback.
        raise CpcvConfigError(
            f"the path evaluator cannot be sent to a worker process: {unpicklable}. A "
            f"parallel CPCV run needs an evaluator defined at module level, holding only "
            f"picklable state; run with worker_total=1 to evaluate in this process"
        ) from unpicklable

    for split in partition.splits:
        charge(split)

    performances: list[PathPerformance] = []
    failures: list[PathFailure] = []
    with ProcessPoolExecutor(max_workers=worker_total) as pool:
        submitted = [(split, pool.submit(evaluate, split)) for split in partition.splits]
        for split, pending in submitted:
            try:
                performance = pending.result()
            except CpcvPathEvaluationError as failure:
                failures.append(PathFailure(path_index=split.path_index, reason=str(failure)))
                continue
            performances.append(_attribute(performance, split))
    return performances, failures


def run_cpcv(
    partition: CpcvPartition,
    *,
    evaluate: PathEvaluator,
    charge: TrialCharge,
    worker_total: int = 1,
    memory_budget: WorkerMemoryBudget | None = None,
) -> CpcvReport:
    """Charge and evaluate every path, then summarise the distribution.

    `charge` and `evaluate` are injected rather than constructed. The ledger write is a
    database effect and the evaluation is a full backtest; neither belongs inside a
    function whose job is the accounting between them, and injecting both is what lets the
    accounting be tested without either.

    `worker_total` defaults to 1 -- one process, this one -- because the parallel path
    costs an evaluator that survives pickling, and a default that quietly required that
    would break every existing caller at runtime rather than at the call site.

    Above 1 it needs `memory_budget`, and the budget refuses an oversubscribed pool rather
    than shrinking it (`fking.backtest._workers`). Passing a budget with `worker_total=1`
    is meaningful too: it asserts that even one worker fits.
    """
    if memory_budget is not None:
        resolve_worker_total(worker_total, memory_budget)
    elif worker_total > 1:
        raise CpcvConfigError(
            f"worker_total={worker_total} was requested with no memory_budget. Fold "
            f"parallelism is bounded by memory rather than by cores, and a pool sized "
            f"against nothing is a pool that swaps on the container it was not measured on"
        )

    if worker_total == 1:
        performances, failures = _run_sequentially(partition, evaluate=evaluate, charge=charge)
    else:
        performances, failures = _run_in_parallel(
            partition, evaluate=evaluate, charge=charge, worker_total=worker_total
        )

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
