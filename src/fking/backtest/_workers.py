"""How many fold workers a machine may run at once, and the refusal when it may not.

CPCV is embarrassingly parallel across paths and *not* parallel inside one: the event
loop is single-threaded because a second thread is a second source of ordering that no
seed reproduces (`_queue.py`). So the only lever is how many paths run at the same time,
and that lever is bounded by memory rather than by cores.

The bound matters because of what happens when it is wrong. A path holds the whole bar
series for its window plus a trace with one entry per dispatched event; eight of those in
eight processes on a 4 GiB container does not fail, it swaps -- and a swapping benchmark
reports a wall clock four times the real cost, which is then written into a budget and
defended. A run that cannot fit is refused here, loudly, before the first process starts.

Two properties are deliberate:

**The permitted total is derived from a measured per-worker peak RSS, never from the core
count.** `os.cpu_count()` on a container reports the host's cores, not the cgroup's quota,
so sizing a pool from it is how a 2-CPU container ends up running sixteen workers.

**An undetectable memory limit is not a licence to guess.** `container_memory_limit_bytes`
returns `None` off cgroups rather than falling back to physical RAM, and the caller then
has to state a limit. A default that silently means "the whole machine" is the value
somebody forgets to override on the one machine where it matters.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Final

from fking.backtest._errors import OversubscribedWorkersError

#: cgroup v2 unified hierarchy. `max` means "no limit set on this cgroup", which is the
#: normal reading on a developer machine and is reported as an absent limit rather than
#: as an enormous one.
_CGROUP_V2_MEMORY_MAX: Final = Path("/sys/fs/cgroup/memory.max")

#: cgroup v1. Docker on kernels before the unified hierarchy still writes here, and an
#: unlimited cgroup reports a sentinel near 2**63 rather than a word -- see
#: `_UNLIMITED_V1_FLOOR`.
_CGROUP_V1_MEMORY_LIMIT: Final = Path("/sys/fs/cgroup/memory/memory.limit_in_bytes")

#: cgroup v1 spells "unlimited" as PAGE_COUNTER_MAX scaled by the page size -- in practice
#: 0x7FFFFFFFFFFFF000 on x86-64, and other very large values elsewhere. Anything at or
#: above a pebibyte is that sentinel and not a real container limit; no machine this
#: project runs on has a pebibyte of RAM.
_UNLIMITED_V1_FLOOR: Final = 1 << 50

#: Held back for the parent process: it keeps the bar series the folds were sliced from,
#: the partition, and every completed `PathPerformance`. The reference workload's whole
#: single-process run peaks at 168 MB (`docs/perf/2026-08-10-cpcv-reference-profile.md`);
#: 512 MiB is that with room for a window several times longer, and rounding up here costs
#: one worker slot on a small container while rounding down costs the run.
DEFAULT_PARENT_RESERVE_BYTES: Final = 512 * 1024 * 1024


@dataclass(frozen=True, slots=True)
class WorkerMemoryBudget:
    """The memory arithmetic that decides how many folds may run at once.

    Every field is bytes and every field is stated by the caller. Nothing here reads the
    machine: a budget that measures its own environment cannot be unit-tested against the
    container it will actually run in, and the interesting cases -- the 2 GiB CI runner,
    the 4 GiB compose limit -- are exactly the ones no developer machine reproduces.
    """

    #: What the container is allowed to use in total.
    memory_limit_bytes: int

    #: Peak resident set of one fold worker, measured rather than assumed. `make bench`
    #: prints it; `PERFORMANCE_GUIDE.md` records the machine it was printed on.
    per_worker_peak_rss_bytes: int

    #: Withheld from the workers for the parent process.
    parent_reserve_bytes: int = DEFAULT_PARENT_RESERVE_BYTES

    def __post_init__(self) -> None:
        if self.memory_limit_bytes <= 0:
            raise OversubscribedWorkersError(
                f"memory_limit_bytes must be positive; got {self.memory_limit_bytes}"
            )
        if self.per_worker_peak_rss_bytes <= 0:
            raise OversubscribedWorkersError(
                f"per_worker_peak_rss_bytes must be positive; got "
                f"{self.per_worker_peak_rss_bytes}. A worker whose footprint is unknown "
                f"cannot be budgeted for, and zero would permit an unbounded pool"
            )
        if self.parent_reserve_bytes < 0:
            raise OversubscribedWorkersError(
                f"parent_reserve_bytes must not be negative; got {self.parent_reserve_bytes}"
            )
        if self.parent_reserve_bytes >= self.memory_limit_bytes:
            raise OversubscribedWorkersError(
                f"parent_reserve_bytes {self.parent_reserve_bytes} leaves nothing of the "
                f"{self.memory_limit_bytes}-byte limit for a worker; the run cannot proceed "
                f"even single-threaded on this container"
            )

    @property
    def permitted_worker_total(self) -> int:
        """How many fold workers fit, floor-divided and never rounded up.

        Can be zero, and zero is returned rather than clamped to one. A container that
        cannot hold a single worker beside its parent is a container the run must refuse
        on, and clamping would turn that into the swap the whole module exists to prevent.
        """
        return (self.memory_limit_bytes - self.parent_reserve_bytes) // (
            self.per_worker_peak_rss_bytes
        )


def container_memory_limit_bytes() -> int | None:
    """The cgroup memory limit in force, or `None` when there is no limit to read.

    `None` means "this process is not memory-limited by a cgroup", which is the ordinary
    answer on a developer machine. It deliberately does not fall back to physical RAM:
    the caller must then state a limit, and stating it is what makes the number visible
    in a review.
    """
    if _CGROUP_V2_MEMORY_MAX.is_file():
        raw = _CGROUP_V2_MEMORY_MAX.read_text(encoding="utf-8").strip()
        if raw != "max":
            return int(raw)
        return None
    if _CGROUP_V1_MEMORY_LIMIT.is_file():
        limit_bytes = int(_CGROUP_V1_MEMORY_LIMIT.read_text(encoding="utf-8").strip())
        if limit_bytes >= _UNLIMITED_V1_FLOOR:
            return None
        return limit_bytes
    return None


def resolve_worker_total(requested_worker_total: int, budget: WorkerMemoryBudget) -> int:
    """Return `requested_worker_total`, or refuse it.

    Refuses rather than silently reducing to what fits. A pool quietly shrunk from eight
    to two produces a wall clock four times the one the caller planned around, reported
    with no indication that the plan was overruled -- and the next person to see the
    number treats it as the cost of eight workers. The caller asked a question with a
    wrong answer and is told so.
    """
    if requested_worker_total < 1:
        raise OversubscribedWorkersError(
            f"requested_worker_total must be at least 1; got {requested_worker_total}"
        )
    permitted = budget.permitted_worker_total
    if requested_worker_total > permitted:
        raise OversubscribedWorkersError(
            f"{requested_worker_total} fold workers were requested and this container "
            f"permits {permitted}: a {budget.memory_limit_bytes}-byte limit, less "
            f"{budget.parent_reserve_bytes} bytes reserved for the parent, divided by a "
            f"measured {budget.per_worker_peak_rss_bytes}-byte peak per worker. Running "
            f"them anyway would swap, and a swapping run reports a wall clock that is an "
            f"artefact of the oversubscription rather than the cost of the work"
        )
    return requested_worker_total
