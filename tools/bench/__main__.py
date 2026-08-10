"""`make bench`: run the pinned workload, print the three numbers, optionally gate on them.

    python -m tools.bench                       # measure and print
    python -m tools.bench --check               # ... and fail over the committed budget
    python -m tools.bench --profile docs/perf/<date>-cpcv-reference-profile.md
    python -m tools.bench --workers 4 --memory-limit-bytes 4294967296

Exit codes are the interface: 0 within budget, 1 over it, 2 for a malformed invocation.
A benchmark that printed a breach and exited 0 would be a green build with a regression
in it, which is worse than having no benchmark.
"""

from __future__ import annotations

import argparse
import cProfile
import pstats
import sys
from datetime import UTC, datetime
from io import StringIO
from pathlib import Path

from fking.backtest import WorkerMemoryBudget, container_memory_limit_bytes
from tools.bench._budget import REFERENCE_BUDGET, assess_wall_clock
from tools.bench._measure import Measurement, measured
from tools.bench._workload import (
    GROUP_TOTAL,
    TEST_GROUP_SIZE,
    WINDOW_END_UTC,
    WINDOW_START_UTC,
    dispatched_event_total,
    reference_bars,
    reference_partition,
    run_reference_workload,
)

#: How many call sites the profile report keeps. Ten, per the issue: a hundred-line
#: profile is a file nobody reads twice, and the tail of a cumulative-time listing is
#: noise by construction.
_PROFILE_ROWS = 10


def _parse_arguments(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        prog="python -m tools.bench",
        description="Measure the pinned backtest reference workload.",
    )
    parser.add_argument(
        "--check",
        action="store_true",
        help="fail with exit 1 when the wall clock exceeds the committed budget",
    )
    parser.add_argument(
        "--workers",
        type=int,
        default=1,
        help="fold workers; above 1 needs a memory limit, explicit or from the cgroup",
    )
    parser.add_argument(
        "--memory-limit-bytes",
        type=int,
        default=None,
        help="the container memory limit to bound the worker pool against",
    )
    parser.add_argument(
        "--per-worker-peak-rss-bytes",
        type=int,
        default=None,
        help="measured peak RSS of one worker; defaults to the committed budget's figure",
    )
    parser.add_argument(
        "--profile",
        type=Path,
        default=None,
        help="run under cProfile and write the top-ten cumulative call sites to this file",
    )
    return parser.parse_args(argv)


def _memory_budget(arguments: argparse.Namespace) -> WorkerMemoryBudget | None:
    """The pool's bound, or `None` when the run is single-process and needs none."""
    if arguments.workers == 1 and arguments.memory_limit_bytes is None:
        return None
    memory_limit_bytes = arguments.memory_limit_bytes
    if memory_limit_bytes is None:
        memory_limit_bytes = container_memory_limit_bytes()
    if memory_limit_bytes is None:
        raise SystemExit(
            "--workers above 1 needs a memory limit and this process is not in a "
            "memory-limited cgroup; pass --memory-limit-bytes explicitly. Sizing a pool "
            "against an unknown limit is how a run swaps instead of refusing"
        )
    per_worker_peak_rss_bytes = (
        arguments.per_worker_peak_rss_bytes
        if arguments.per_worker_peak_rss_bytes is not None
        else REFERENCE_BUDGET.peak_rss_bytes
    )
    return WorkerMemoryBudget(
        memory_limit_bytes=memory_limit_bytes,
        per_worker_peak_rss_bytes=per_worker_peak_rss_bytes,
    )


def _measure(arguments: argparse.Namespace) -> Measurement:
    partition = reference_partition()
    # Outside the timed region on purpose: generating the pinned series stands in for the
    # Parquet read, and this benchmark's budget is the engine's cost. A worker pool pays
    # it inside its own processes, which is why a parallel wall clock is not comparable to
    # the single-process budget and is not what CI asserts.
    reference_bars()
    memory_budget = _memory_budget(arguments)
    event_total = dispatched_event_total(partition)
    with measured(lambda: event_total) as sink:
        run_reference_workload(
            worker_total=arguments.workers,
            memory_budget=memory_budget,
        )
    return sink[0]


def _profile(destination: Path, arguments: argparse.Namespace) -> Measurement:
    profiler = cProfile.Profile()
    profiler.enable()
    measurement = _measure(arguments)
    profiler.disable()

    rendered = StringIO()
    pstats.Stats(profiler, stream=rendered).sort_stats("cumulative").print_stats(_PROFILE_ROWS)
    destination.parent.mkdir(parents=True, exist_ok=True)
    destination.write_text(
        _profile_document(rendered.getvalue(), measurement=measurement),
        encoding="utf-8",
    )
    print(f"profile written to {destination}")
    return measurement


def _profile_document(listing: str, *, measurement: Measurement) -> str:
    """The committed profiling record, so the next investigation starts from evidence."""
    # Absolute paths are stripped to repository-relative, forward-slashed ones. Left in,
    # the record would differ between two machines that measured the same thing, which
    # makes two profiles taken a year apart undiffable -- and diffing them is the reason
    # to commit one.
    repository_root = Path(__file__).resolve().parents[2]
    relative = listing.replace(f"{repository_root}\\", "").replace(f"{repository_root}/", "")
    listing = relative.replace("\\", "/")
    return (
        f"# Backtest reference workload profile\n\n"
        f"Generated by `python -m tools.bench --profile`. Timings here are inflated by "
        f"cProfile's per-call overhead and are useful for *ranking* call sites, not as a "
        f"budget -- the budget is the unprofiled wall clock in `PERFORMANCE_GUIDE.md`.\n\n"
        f"- Recorded at: {datetime.now(UTC).isoformat()}\n"
        f"- Workload: CPCV N={GROUP_TOTAL}, k={TEST_GROUP_SIZE} over "
        f"{WINDOW_START_UTC.date().isoformat()}..{WINDOW_END_UTC.date().isoformat()} "
        f"BTCUSDT 1m\n"
        f"- Dispatched events: {measurement.dispatched_event_total}\n"
        f"- Wall clock under the profiler: {measurement.wall_clock_seconds:.2f}s\n"
        f"- Peak RSS: {measurement.peak_rss_bytes / 1_000_000:.1f} MB\n\n"
        f"## Top {_PROFILE_ROWS} call sites by cumulative time\n\n"
        f"```\n{listing}```\n"
    )


def main(argv: list[str] | None = None) -> int:
    """Measure, print, and gate. Returns the process exit code."""
    arguments = _parse_arguments(sys.argv[1:] if argv is None else argv)
    if arguments.workers < 1:
        print("--workers must be at least 1", file=sys.stderr)
        return 2

    measurement = (
        _profile(arguments.profile, arguments)
        if arguments.profile is not None
        else _measure(arguments)
    )

    print(f"wall clock:        {measurement.wall_clock_seconds:.2f} s")
    print(f"peak RSS:          {measurement.peak_rss_bytes / 1_000_000:.1f} MB")
    print(f"dispatched events: {measurement.dispatched_event_total}")
    print(f"events/second:     {measurement.events_per_second:.0f}")
    print(f"fold workers:      {arguments.workers}")

    if not arguments.check:
        return 0
    if arguments.workers != 1:
        print(
            "--check compares against a single-process budget; re-run without --workers",
            file=sys.stderr,
        )
        return 2
    verdict = assess_wall_clock(REFERENCE_BUDGET, wall_clock_seconds=measurement.wall_clock_seconds)
    print(verdict.message, file=sys.stderr if not verdict.within_budget else sys.stdout)
    return 0 if verdict.within_budget else 1


if __name__ == "__main__":
    raise SystemExit(main())
