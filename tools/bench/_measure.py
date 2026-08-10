"""Wall clock and peak resident set, measured the same way on both platforms.

**Wall clock, not CPU.** `time.process_time()` would be steadier and would be measuring
the wrong thing: a change that halves CPU and triples Parquet I/O is not an improvement,
and against a CPU budget it would read as a 50% win. `perf_counter` is monotonic and
includes every second the work actually took.

**Peak RSS, not current RSS.** The number that decides how many fold workers fit in a
container is the high-water mark, because that is the moment the OOM killer or the swap
file gets involved. Current RSS at the end of a run is whatever the allocator happened not
to have returned, which is a different number and a smaller one.

There is no portable stdlib call for either half of peak RSS, so this module has one
implementation per platform. `resource.getrusage` is the Unix answer; `GetProcessMemoryInfo`
through `ctypes` is the Windows one. Both report the same quantity, and the alternative --
`tracemalloc` -- reports the Python heap, which on a `Decimal`-heavy workload understates
the process by the whole interpreter and every arena it has not released.
"""

from __future__ import annotations

import sys
import time
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from typing import Final

#: `ru_maxrss` is kilobytes on Linux and bytes on macOS. Only Linux matters here -- CI is
#: ubuntu-latest and the container is Debian -- and the constant is named so that a macOS
#: reading is a visible discrepancy rather than a silent factor of 1024.
_LINUX_MAXRSS_UNIT_BYTES: Final = 1024


# The platform split is at module scope rather than inside the function because
# `ctypes.wintypes` raises on import on Linux and `resource` does not exist on Windows.
# One import guarded by `sys.platform` is also the form mypy narrows, so each branch is
# type-checked on the platform that runs it.
if sys.platform == "win32":
    import ctypes
    import ctypes.wintypes

    class _ProcessMemoryCounters(ctypes.Structure):
        """`PROCESS_MEMORY_COUNTERS`, as psapi.h declares it.

        Every field is present even though only `PeakWorkingSetSize` is read: the struct
        is passed by size, and a short one makes `GetProcessMemoryInfo` fail or write past
        the end of the allocation, depending on the build.
        """

        # Not a mutable class attribute in the RUF012 sense: ctypes reads it once at
        # class creation and the descriptor set it produces is what the fields become.
        _fields_ = [
            ("cb", ctypes.wintypes.DWORD),
            ("PageFaultCount", ctypes.wintypes.DWORD),
            ("PeakWorkingSetSize", ctypes.c_size_t),
            ("WorkingSetSize", ctypes.c_size_t),
            ("QuotaPeakPagedPoolUsage", ctypes.c_size_t),
            ("QuotaPagedPoolUsage", ctypes.c_size_t),
            ("QuotaPeakNonPagedPoolUsage", ctypes.c_size_t),
            ("QuotaNonPagedPoolUsage", ctypes.c_size_t),
            ("PagefileUsage", ctypes.c_size_t),
            ("PeakPagefileUsage", ctypes.c_size_t),
        ]

    # The prototypes are declared rather than left to ctypes' defaults, and this is not
    # tidiness: without a restype of HANDLE, `GetCurrentProcess` comes back through a
    # 32-bit `int` and the (HANDLE)-1 pseudo-handle arrives at the callee as 0xFFFFFFFF
    # instead of 0xFFFFFFFFFFFFFFFF. The call then fails on every 64-bit build, and it
    # fails by returning zero rather than by raising.
    _kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    _psapi = ctypes.WinDLL("psapi", use_last_error=True)
    _kernel32.GetCurrentProcess.restype = ctypes.wintypes.HANDLE
    _kernel32.GetCurrentProcess.argtypes = []
    _psapi.GetProcessMemoryInfo.restype = ctypes.wintypes.BOOL
    _psapi.GetProcessMemoryInfo.argtypes = [
        ctypes.wintypes.HANDLE,
        ctypes.POINTER(_ProcessMemoryCounters),
        ctypes.wintypes.DWORD,
    ]

    def peak_rss_bytes() -> int:
        """This process's high-water working set, in bytes."""
        counters = _ProcessMemoryCounters()
        counters.cb = ctypes.sizeof(_ProcessMemoryCounters)
        measured_ok = _psapi.GetProcessMemoryInfo(
            _kernel32.GetCurrentProcess(), ctypes.byref(counters), counters.cb
        )
        if not measured_ok:
            raise OSError(ctypes.get_last_error(), "GetProcessMemoryInfo failed")
        return int(counters.PeakWorkingSetSize)

else:
    import resource

    def peak_rss_bytes() -> int:
        """This process's high-water resident set, in bytes.

        Self only, never the children. A pool's per-worker footprint is measured by
        running one worker's work in this process, which is the number
        `WorkerMemoryBudget` wants; summing children would report the pool's total and
        invite it to be divided by a worker total the scheduler never ran concurrently.
        """
        return resource.getrusage(resource.RUSAGE_SELF).ru_maxrss * _LINUX_MAXRSS_UNIT_BYTES


@dataclass(frozen=True, slots=True)
class Measurement:
    """What one benchmark run cost.

    `events_per_second` is derived rather than stored so it cannot disagree with the two
    numbers it comes from.
    """

    wall_clock_seconds: float
    peak_rss_bytes: int
    dispatched_event_total: int

    @property
    def events_per_second(self) -> float:
        """Dispatched events divided by wall clock.

        Zero when the run took no measurable time, which only happens for an empty
        workload -- and reporting zero there is better than dividing by zero or reporting
        an infinity that formats as `inf` in a budget file.
        """
        if self.wall_clock_seconds <= 0.0:
            return 0.0
        return self.dispatched_event_total / self.wall_clock_seconds


@contextmanager
def measured(dispatched_event_total: Callable[[], int]) -> Iterator[list[Measurement]]:
    """Time the block and record its peak RSS, appending one `Measurement` on exit.

    The event total arrives as a callable because it is not known until the block has run,
    and a mutable holder the caller writes into would be a second way to get it wrong.
    """
    sink: list[Measurement] = []
    started_at = time.perf_counter()
    yield sink
    elapsed_seconds = time.perf_counter() - started_at
    sink.append(
        Measurement(
            wall_clock_seconds=elapsed_seconds,
            peak_rss_bytes=peak_rss_bytes(),
            dispatched_event_total=dispatched_event_total(),
        )
    )
