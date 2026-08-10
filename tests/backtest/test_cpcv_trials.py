"""Every path is a trial, charged as the path is reached and never refunded.

`N=8, k=2` is 28 paths and therefore 28 permanent charges. The tests that matter here are
the ones about *when* the charge happens, because a run that batches its charges at the
end charges nothing when it crashes at path 19 -- and a searcher who abandons a run after
watching the early paths has then performed a selection that cost nothing.
"""

from __future__ import annotations

from decimal import Decimal

import pytest

from fking.backtest.cpcv import (
    CpcvConfigError,
    CpcvPathEvaluationError,
    CpcvSplit,
    PathPerformance,
    build_splits,
    run_cpcv,
)
from tests.backtest.cpcv_support import PATH_TOTAL, plan_for

#: Two paths made to fail, and one made to crash. Named rather than inlined so the
#: arithmetic in the assertions reads as arithmetic instead of as three literals.
FAILING_PATHS: tuple[int, ...] = (3, 17)
CRASHING_PATH = 2


class _ChargeLedger:
    """Records charges in call order, so "when" is assertable and not just "how many"."""

    def __init__(self) -> None:
        self.charged_paths: list[int] = []

    def __call__(self, split: CpcvSplit) -> None:
        self.charged_paths.append(split.path_index)


def _performance(split: CpcvSplit, *, trade_count: int = 120) -> PathPerformance:
    return PathPerformance(
        path_index=split.path_index,
        trade_count=trade_count,
        sharpe_ratio=Decimal("0.50"),
    )


def test_eight_groups_choose_two_charges_exactly_twenty_eight_trials() -> None:
    partition = build_splits(plan_for())
    ledger = _ChargeLedger()

    report = run_cpcv(partition, evaluate=_performance, charge=ledger)

    assert report.path_total == PATH_TOTAL
    assert report.trials_charged == PATH_TOTAL
    assert len(ledger.charged_paths) == PATH_TOTAL
    assert ledger.charged_paths == list(range(PATH_TOTAL))


def test_the_charge_is_registered_before_the_path_is_evaluated() -> None:
    """Incrementally, not batched.

    The evaluator asserts the ledger state it observes, so a harness that charged after
    the loop -- or in one call at the end -- fails on the first path rather than producing
    the same final total by a route that a crash would have made free.
    """
    partition = build_splits(plan_for())
    ledger = _ChargeLedger()
    observed: list[tuple[int, int]] = []

    def evaluate(split: CpcvSplit) -> PathPerformance:
        observed.append((split.path_index, len(ledger.charged_paths)))
        return _performance(split)

    run_cpcv(partition, evaluate=evaluate, charge=ledger)

    assert observed == [(index, index + 1) for index in range(PATH_TOTAL)]


def test_a_failed_path_keeps_its_charge_and_stays_in_the_denominator() -> None:
    partition = build_splits(plan_for())
    ledger = _ChargeLedger()

    def evaluate(split: CpcvSplit) -> PathPerformance:
        if split.path_index in set(FAILING_PATHS):
            raise CpcvPathEvaluationError(f"no bars for path {split.path_index}")
        return _performance(split)

    report = run_cpcv(partition, evaluate=evaluate, charge=ledger)

    assert report.trials_charged == PATH_TOTAL
    assert report.path_total == PATH_TOTAL
    assert report.paths_completed == PATH_TOTAL - len(FAILING_PATHS)
    assert report.path_failure_total == len(FAILING_PATHS)
    assert tuple(failure.path_index for failure in report.paths_failed) == FAILING_PATHS
    assert report.paths_failed[0].reason == "no bars for path 3"


def test_a_defect_in_the_evaluator_kills_the_run_rather_than_becoming_a_bad_path() -> None:
    """Only `CpcvPathEvaluationError` is caught, and specifically not the base class.

    A `CpcvPartitionError` escaping the evaluator means a split leaked; recording it as
    one failed path would convert a structural defect into a slightly smaller path count.
    """
    partition = build_splits(plan_for())
    ledger = _ChargeLedger()

    def evaluate(split: CpcvSplit) -> PathPerformance:
        if split.path_index == CRASHING_PATH:
            raise ZeroDivisionError("annualisation divided by an empty window")
        return _performance(split)

    with pytest.raises(ZeroDivisionError):
        run_cpcv(partition, evaluate=evaluate, charge=ledger)

    assert ledger.charged_paths == list(range(CRASHING_PATH + 1))


def test_a_performance_attributed_to_the_wrong_path_is_refused() -> None:
    partition = build_splits(plan_for())

    def evaluate(_split: CpcvSplit) -> PathPerformance:
        return PathPerformance(path_index=0, trade_count=120, sharpe_ratio=Decimal("0.5"))

    with pytest.raises(CpcvConfigError, match="returned a performance for path 0"):
        run_cpcv(partition, evaluate=evaluate, charge=_ChargeLedger())


def test_the_report_refuses_to_exist_with_a_charge_count_that_does_not_match() -> None:
    """The invariant is asserted by the type, not left to whoever reads the two fields."""
    partition = build_splits(plan_for())
    report = run_cpcv(partition, evaluate=_performance, charge=_ChargeLedger())

    with pytest.raises(CpcvConfigError, match="trials charged against"):
        type(report)(
            partition=partition,
            performances=report.performances,
            paths_failed=report.paths_failed,
            trials_charged=1,
            distribution=report.distribution,
            distribution_refusal="",
        )


def test_the_report_refuses_a_path_that_neither_completed_nor_failed() -> None:
    partition = build_splits(plan_for())
    report = run_cpcv(partition, evaluate=_performance, charge=_ChargeLedger())

    with pytest.raises(CpcvConfigError, match="paths accounted for against"):
        type(report)(
            partition=partition,
            performances=report.performances[:-1],
            paths_failed=(),
            trials_charged=PATH_TOTAL,
            distribution=report.distribution,
            distribution_refusal="",
        )
