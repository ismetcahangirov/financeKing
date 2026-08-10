"""Fold parallelism: bounded, ordered, charged in full, and refused when it will not fit.

A real `ProcessPoolExecutor` rather than a stubbed one. The properties under test are
properties of running in another address space -- that the evaluator survives pickling,
that results are reassembled in partition order rather than completion order, that a path
failure raised in a worker still lands as a `PathFailure` -- and a fake executor that runs
everything in this process asserts none of them.

Every helper here is module level because a worker process re-imports the module and
unpickles by qualified name. A closure would fail with a `PicklingError` that names nothing
useful, which is precisely why `_run_in_parallel` checks for it up front.
"""

from __future__ import annotations

from decimal import Decimal

import pytest

from fking.backtest import OversubscribedWorkersError, WorkerMemoryBudget
from fking.backtest.cpcv import (
    CpcvConfigError,
    CpcvPathEvaluationError,
    CpcvSplit,
    PathPerformance,
    build_splits,
    run_cpcv,
)
from tests.backtest.cpcv_support import PATH_TOTAL, plan_for

_GIB = 1024 * 1024 * 1024
_MIB = 1024 * 1024

#: The one path the failing evaluator refuses, named so the expectation and the
#: behaviour cannot drift apart.
FAILING_PATH_INDEX = 7

#: Room for four workers beside the parent reserve, which is more than any test asks for.
GENEROUS_BUDGET = WorkerMemoryBudget(
    memory_limit_bytes=8 * _GIB,
    per_worker_peak_rss_bytes=512 * _MIB,
    parent_reserve_bytes=512 * _MIB,
)


def evaluate_deterministically(split: CpcvSplit) -> PathPerformance:
    """A path evaluator with no state and no I/O, so the pool is the only variable."""
    return PathPerformance(
        path_index=split.path_index,
        trade_count=40 + split.path_index,
        sharpe_ratio=Decimal(split.path_index) / Decimal("10"),
    )


def fail_on_path_seven(split: CpcvSplit) -> PathPerformance:
    """Fails one path the way a real evaluator does: with the modelled exception."""
    if split.path_index == FAILING_PATH_INDEX:
        raise CpcvPathEvaluationError("the data window for path 7 could not be served")
    return evaluate_deterministically(split)


def misattribute(split: CpcvSplit) -> PathPerformance:  # noqa: ARG001 - the evaluator's shape
    """Returns a performance for a path it was not asked about."""
    return PathPerformance(path_index=999, trade_count=40, sharpe_ratio=Decimal("1"))


class ChargeRecorder:
    """Counts charges. Lives in the parent, because that is where `charge` is called."""

    def __init__(self) -> None:
        self.charged_path_indices: list[int] = []

    def __call__(self, split: CpcvSplit) -> None:
        self.charged_path_indices.append(split.path_index)


def test_parallel_and_sequential_runs_agree_exactly() -> None:
    """Two workers must produce the report one worker produces, path for path.

    This is the property that makes the pool safe to use at all. Results arrive in
    completion order and are reassembled in partition order; if they were not, the
    distribution would depend on the scheduler and two runs of one plan would disagree.
    """
    partition = build_splits(plan_for())
    sequential = run_cpcv(partition, evaluate=evaluate_deterministically, charge=ChargeRecorder())
    parallel = run_cpcv(
        partition,
        evaluate=evaluate_deterministically,
        charge=ChargeRecorder(),
        worker_total=2,
        memory_budget=GENEROUS_BUDGET,
    )
    assert parallel.performances == sequential.performances
    assert parallel.distribution == sequential.distribution
    assert parallel.trials_charged == PATH_TOTAL


def test_every_path_is_charged_before_the_pool_starts() -> None:
    partition = build_splits(plan_for())
    recorder = ChargeRecorder()
    run_cpcv(
        partition,
        evaluate=evaluate_deterministically,
        charge=recorder,
        worker_total=2,
        memory_budget=GENEROUS_BUDGET,
    )
    assert recorder.charged_path_indices == [split.path_index for split in partition.splits]


def test_a_failed_path_keeps_its_charge_and_stays_in_the_denominator() -> None:
    partition = build_splits(plan_for())
    recorder = ChargeRecorder()
    report = run_cpcv(
        partition,
        evaluate=fail_on_path_seven,
        charge=recorder,
        worker_total=2,
        memory_budget=GENEROUS_BUDGET,
    )
    assert report.path_failure_total == 1
    assert report.paths_failed[0].path_index == FAILING_PATH_INDEX
    assert report.path_total == PATH_TOTAL
    assert report.trials_charged == PATH_TOTAL
    assert len(recorder.charged_path_indices) == PATH_TOTAL


def test_a_misattributed_performance_kills_the_run() -> None:
    """Attribution is checked on the way out of the pool, not assumed."""
    partition = build_splits(plan_for())
    with pytest.raises(CpcvConfigError, match="performance for path 999"):
        run_cpcv(
            partition,
            evaluate=misattribute,
            charge=ChargeRecorder(),
            worker_total=2,
            memory_budget=GENEROUS_BUDGET,
        )


def test_an_oversubscribed_pool_is_refused_before_any_path_is_charged() -> None:
    """The refusal happens first, so a run that cannot fit costs nothing.

    A pool sized past the container's memory does not raise on its own -- it swaps. The
    charge ordering matters as much as the refusal: charging 28 trials and then failing to
    start would spend the trial budget on a run that produced nothing.
    """
    partition = build_splits(plan_for())
    recorder = ChargeRecorder()
    cramped = WorkerMemoryBudget(
        memory_limit_bytes=2 * _GIB,
        per_worker_peak_rss_bytes=_GIB,
        parent_reserve_bytes=512 * _MIB,
    )
    with pytest.raises(OversubscribedWorkersError, match="permits 1"):
        run_cpcv(
            partition,
            evaluate=evaluate_deterministically,
            charge=recorder,
            worker_total=8,
            memory_budget=cramped,
        )
    assert recorder.charged_path_indices == []


def test_a_pool_without_a_memory_budget_is_refused() -> None:
    partition = build_splits(plan_for())
    with pytest.raises(CpcvConfigError, match="no memory_budget"):
        run_cpcv(
            partition,
            evaluate=evaluate_deterministically,
            charge=ChargeRecorder(),
            worker_total=4,
        )


def test_an_unpicklable_evaluator_is_refused_with_the_reason() -> None:
    """A closure cannot cross a process boundary, and the message says so.

    Left to `ProcessPoolExecutor` this surfaces as a `BrokenProcessPool` several frames
    from the closure that caused it.
    """
    partition = build_splits(plan_for())
    offset = 3

    def closure_evaluator(split: CpcvSplit) -> PathPerformance:
        return PathPerformance(
            path_index=split.path_index,
            trade_count=40 + offset,
            sharpe_ratio=Decimal("1"),
        )

    with pytest.raises(CpcvConfigError, match="cannot be sent to a worker process"):
        run_cpcv(
            partition,
            evaluate=closure_evaluator,
            charge=ChargeRecorder(),
            worker_total=2,
            memory_budget=GENEROUS_BUDGET,
        )


def test_a_single_worker_run_still_honours_a_budget_that_permits_none() -> None:
    """`worker_total=1` with a budget asserts that even one worker would fit."""
    partition = build_splits(plan_for())
    impossible = WorkerMemoryBudget(
        memory_limit_bytes=600 * _MIB,
        per_worker_peak_rss_bytes=512 * _MIB,
        parent_reserve_bytes=256 * _MIB,
    )
    with pytest.raises(OversubscribedWorkersError, match="permits 0"):
        run_cpcv(
            partition,
            evaluate=evaluate_deterministically,
            charge=ChargeRecorder(),
            memory_budget=impossible,
        )
