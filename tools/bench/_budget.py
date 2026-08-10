"""The committed budget, and the comparison CI runs against it.

A budget is a measurement plus the machine it was taken on, and there is exactly one
machine here: the GitHub-hosted `ubuntu-latest` runner. That is a deliberate narrowing.
The developer machine this work was done on produced 11.6 s and 43.4 s for the same
workload within one session -- a 3.7x spread from background load and thermal throttling
alone, which is eighteen times the tolerance. A budget asserted there would fail on a busy
afternoon and pass on a genuine 50% regression the following morning, and a gate that does
that gets disabled within a month.

So the laptop gets `make bench` for a number to compare a change against, back to back, in
one sitting -- which is the comparison it can actually support -- and CI gets the gate.
`PERFORMANCE_GUIDE.md` states both, and states which one is load-bearing.

The tolerance is 20% over the recorded number, the issue's figure.

Nothing here decides *whether* a regression is acceptable. It reports the previous number,
the current number and the overshoot, and exits non-zero. Deciding that a change is worth
its cost means editing this file, which is a diff with a reviewer on it.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from typing import Final

#: Over-budget by more than this fraction fails the build. `CLAUDE.md` section 12: the
#: gate must be able to say no, and a gate with no headroom at all says no to scheduler
#: noise instead of to regressions.
TOLERANCE_FRACTION: Final = 0.20


@dataclass(frozen=True, slots=True)
class ReferenceBudget:
    """One machine class's recorded cost for the pinned workload."""

    #: Enough of the machine to reproduce the number: CPU, memory, OS, interpreter.
    machine: str

    #: Wall clock, not CPU. A change that halves CPU and triples I/O has not helped.
    wall_clock_seconds: float

    #: Peak RSS of the single-process run. Recorded rather than asserted -- it is the
    #: input to `WorkerMemoryBudget.per_worker_peak_rss_bytes`, and a memory regression
    #: shows up there as a refused pool rather than as a slower build.
    peak_rss_bytes: int

    #: When the number was taken. A budget with no date is a budget nobody can tell is
    #: five interpreter releases stale.
    measured_on: date

    @property
    def ceiling_seconds(self) -> float:
        """The wall clock at which the build fails."""
        return self.wall_clock_seconds * (1.0 + TOLERANCE_FRACTION)


#: Measured by the `bench` job on pull request #208, over 143,651 events on a
#: GitHub-hosted `ubuntu-latest` runner: **3.60 s** (39,908 events/second, peak RSS
#: 242.0 MB) and **3.46 s** (41,497 events/second, peak RSS 241.4 MB) on two consecutive
#: builds -- a 4% spread.
#:
#: Recorded at 5.0 s rather than at 3.60 s, and the gap is a deliberate trade rather than
#: rounding. Two builds do not bound a shared runner's distribution, whose CPU allocation
#: varies by tens of percent between builds; a budget at the measurement plus 20% would
#: fail builds that changed nothing, and a gate that cries wolf gets marked
#: `continue-on-error` within a month. At 5.0 s the ceiling is 6.0 s, which still fails
#: any regression costing more than 66% extra per event and comfortably fails the doubling
#: this budget exists to catch. Tighten it once a dozen readings exist -- if the spread
#: holds at a few percent, 4.5 s is defensible and 4.0 s is not.
#:
#: Peak RSS is the Linux reading, which is 44% above the same workload's 168 MB on Windows
#: -- glibc's allocator returns less to the OS than Windows' heap does. The larger of the
#: two is the right input to `WorkerMemoryBudget`, which is sizing a pool that will run on
#: Linux.
REFERENCE_BUDGET: Final = ReferenceBudget(
    machine="GitHub-hosted ubuntu-latest runner (4 vCPU, 16 GiB), CPython 3.12",
    wall_clock_seconds=5.0,
    peak_rss_bytes=242 * 1000 * 1000,
    measured_on=date(2026, 8, 10),
)


@dataclass(frozen=True, slots=True)
class BudgetVerdict:
    """Whether a measurement cleared its budget, and the sentence that says so."""

    within_budget: bool
    message: str


def assess_wall_clock(budget: ReferenceBudget, *, wall_clock_seconds: float) -> BudgetVerdict:
    """Compare one measurement against its budget.

    The message carries both numbers in both outcomes. A failure that reported only "too
    slow" would send the reader to run the benchmark themselves to find out by how much,
    and a pass that reported nothing would hide a run sitting at 119% of budget -- which
    is the run before the one that fails.
    """
    ceiling_seconds = budget.ceiling_seconds
    overshoot_fraction = (wall_clock_seconds / budget.wall_clock_seconds) - 1.0
    summary = (
        f"previous {budget.wall_clock_seconds:.2f}s "
        f"(measured {budget.measured_on.isoformat()} on {budget.machine}), "
        f"current {wall_clock_seconds:.2f}s, "
        f"{overshoot_fraction:+.1%} against a {TOLERANCE_FRACTION:.0%} tolerance "
        f"(ceiling {ceiling_seconds:.2f}s)"
    )
    if wall_clock_seconds > ceiling_seconds:
        return BudgetVerdict(
            within_budget=False,
            message=(
                f"the reference workload is over budget -- {summary}. Either the change "
                f"under review made the engine slower, or the budget is genuinely too "
                f"small and tools/bench/_budget.py should be updated with a new "
                f"measurement and the reason. Reducing the workload is not one of the "
                f"two options"
            ),
        )
    return BudgetVerdict(within_budget=True, message=f"within budget -- {summary}")
