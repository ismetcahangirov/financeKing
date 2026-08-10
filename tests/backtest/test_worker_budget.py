"""The fold-worker bound: what fits, what does not, and what happens when it does not.

The load-bearing test here is `test_oversubscribed_pool_is_refused`. An oversubscribed
process pool does not raise on its own -- it swaps, produces every number a healthy run
produces, and reports a wall clock that is an artefact of the swapping. That number then
becomes a performance budget somebody defends.
"""

from __future__ import annotations

import pytest

from fking.backtest import (
    DEFAULT_PARENT_RESERVE_BYTES,
    OversubscribedWorkersError,
    WorkerMemoryBudget,
    container_memory_limit_bytes,
    resolve_worker_total,
)

_GIB = 1024 * 1024 * 1024
_MIB = 1024 * 1024

#: The permitted totals each case works out to, named so the assertion states the
#: arithmetic rather than repeating a number the reader has to re-derive.
SEVEN_WORKERS = 7
TWO_WORKERS = 2
THREE_WORKERS = 3
FOUR_WORKERS = 4


def test_permitted_total_divides_what_is_left_after_the_parent_reserve() -> None:
    budget = WorkerMemoryBudget(
        memory_limit_bytes=4 * _GIB,
        per_worker_peak_rss_bytes=512 * _MIB,
        parent_reserve_bytes=512 * _MIB,
    )
    # (4 GiB - 512 MiB) / 512 MiB = 7
    assert budget.permitted_worker_total == SEVEN_WORKERS


def test_permitted_total_floors_rather_than_rounds() -> None:
    budget = WorkerMemoryBudget(
        memory_limit_bytes=2 * _GIB,
        per_worker_peak_rss_bytes=700 * _MIB,
        parent_reserve_bytes=512 * _MIB,
    )
    # (2048 - 512) / 700 = 2.19: a third worker would not fit, and 2.19 rounded up is the
    # run that swaps.
    assert budget.permitted_worker_total == TWO_WORKERS


def test_a_container_too_small_for_one_worker_permits_none() -> None:
    budget = WorkerMemoryBudget(
        memory_limit_bytes=600 * _MIB,
        per_worker_peak_rss_bytes=512 * _MIB,
        parent_reserve_bytes=256 * _MIB,
    )
    assert budget.permitted_worker_total == 0
    with pytest.raises(OversubscribedWorkersError, match="permits 0"):
        resolve_worker_total(1, budget)


def test_oversubscribed_pool_is_refused() -> None:
    """Eight workers on a container that holds two is refused, not silently reduced."""
    budget = WorkerMemoryBudget(
        memory_limit_bytes=2 * _GIB,
        per_worker_peak_rss_bytes=512 * _MIB,
        parent_reserve_bytes=512 * _MIB,
    )
    assert budget.permitted_worker_total == THREE_WORKERS
    with pytest.raises(OversubscribedWorkersError) as refusal:
        resolve_worker_total(8, budget)
    message = str(refusal.value)
    assert "8 fold workers were requested" in message
    assert "permits 3" in message
    # The refusal states why it is a refusal rather than a reduction.
    assert "swap" in message


def test_a_pool_that_fits_is_returned_unchanged() -> None:
    budget = WorkerMemoryBudget(
        memory_limit_bytes=8 * _GIB,
        per_worker_peak_rss_bytes=512 * _MIB,
    )
    assert resolve_worker_total(FOUR_WORKERS, budget) == FOUR_WORKERS


def test_zero_workers_is_refused() -> None:
    budget = WorkerMemoryBudget(memory_limit_bytes=8 * _GIB, per_worker_peak_rss_bytes=_GIB)
    with pytest.raises(OversubscribedWorkersError, match="at least 1"):
        resolve_worker_total(0, budget)


@pytest.mark.parametrize(
    ("memory_limit_bytes", "per_worker_peak_rss_bytes", "parent_reserve_bytes", "expected"),
    [
        (0, _GIB, 0, "memory_limit_bytes must be positive"),
        (_GIB, 0, 0, "per_worker_peak_rss_bytes must be positive"),
        (_GIB, _GIB, -1, "parent_reserve_bytes must not be negative"),
        (_GIB, _GIB, _GIB, "leaves nothing"),
    ],
)
def test_an_impossible_budget_is_refused_at_construction(
    memory_limit_bytes: int,
    per_worker_peak_rss_bytes: int,
    parent_reserve_bytes: int,
    expected: str,
) -> None:
    with pytest.raises(OversubscribedWorkersError, match=expected):
        WorkerMemoryBudget(
            memory_limit_bytes=memory_limit_bytes,
            per_worker_peak_rss_bytes=per_worker_peak_rss_bytes,
            parent_reserve_bytes=parent_reserve_bytes,
        )


def test_the_default_parent_reserve_is_the_one_the_budget_uses() -> None:
    budget = WorkerMemoryBudget(memory_limit_bytes=8 * _GIB, per_worker_peak_rss_bytes=_GIB)
    assert budget.parent_reserve_bytes == DEFAULT_PARENT_RESERVE_BYTES


def test_the_cgroup_limit_is_an_int_or_an_honest_none() -> None:
    """Never a guess.

    The value depends on where the suite runs -- a cgroup on CI, nothing on a Windows
    laptop -- so the assertion is about the shape of the answer rather than its magnitude.
    What must never happen is a fallback to physical RAM dressed up as a container limit,
    and a `None` is what makes that visible to the caller.
    """
    detected = container_memory_limit_bytes()
    assert detected is None or detected > 0
