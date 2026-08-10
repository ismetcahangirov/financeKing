"""The third place purge and embargo appear: the emitted log line.

`BACKTEST_ENGINE.md` section 6.2 asks for three, and names them: the validation plan, the
output schema, and the log line. `CpcvPlan` is the first, `CpcvPartition` and `CpcvReport`
are the second, and this module is the third. Three, because this is the number that is
silently wrong most often and one place is one place to overlook -- an understated
`FeatureSpec.lookback` shortens the embargo, adjacent paths share information, and the run
reports a stable edge that is partly the same data seen twice. Nothing about that looks
wrong; the only defence is that the number is written down where somebody will read it
without going looking.

Both renderings are strings, and both are pure. `cpcv_log_line` returns the line rather
than emitting it, so a test can assert its content without a log capture and the caller
keeps ownership of the logger and its bound context. The same trade `schedule_table`
makes one package over.

Spans are rendered as plain decimal seconds rather than `str(timedelta)`, because
`str(timedelta(days=1))` is `'1 day, 0:00:00'` -- a value containing a comma and two
spaces, in a line of `key=value` pairs. A grep for `embargo_seconds=` returns the number;
a grep for `embargo=` would return the first third of it.
"""

from __future__ import annotations

from collections.abc import Mapping
from datetime import timedelta
from decimal import Decimal
from types import MappingProxyType
from typing import Final

from fking.backtest.cpcv._distribution import MINIMUM_PATH_TRADES
from fking.backtest.cpcv._harness import CpcvReport

_MICROSECONDS_PER_SECOND: Final = 6


def _seconds(span: timedelta) -> str:
    """A span as an exact decimal number of seconds, with no trailing noise.

    Exact rather than `total_seconds()`: that returns a float, and a purge of one day
    would be rendered from a value whose last bits depend on the platform. The integer
    microsecond count is what the span actually is.
    """
    microseconds = span // timedelta(microseconds=1)
    return f"{Decimal(microseconds).scaleb(-_MICROSECONDS_PER_SECOND).normalize():f}"


def cpcv_path_table(report: CpcvReport) -> tuple[Mapping[str, str], ...]:
    """One row per path: its groups, its gaps, its trade count and its Sharpe.

    Every field is a string, for the reason `schedule_table` gives: this table is compared
    against a fixture and pasted into a record, and a `Decimal` that renders differently
    under two serialisers would make a fixture mismatch look like a partitioning change.

    Purge and embargo appear on every row rather than once in a header, because a row is
    what gets quoted into an incident note, and a row separated from its header has lost
    the two numbers most worth checking.

    Failed paths appear too, with an empty Sharpe and their reason. A table listing only
    the paths that produced a number is a table whose length is a count of successes.
    """
    by_index = {performance.path_index: performance for performance in report.performances}
    failure_by_index = {failure.path_index: failure for failure in report.paths_failed}

    rows: list[Mapping[str, str]] = []
    for split in report.partition.splits:
        performance = by_index.get(split.path_index)
        failure = failure_by_index.get(split.path_index)
        rows.append(
            MappingProxyType(
                {
                    "path_index": str(split.path_index),
                    "test_groups": ",".join(str(index) for index in split.test_group_indices),
                    "train_span_seconds": _seconds(split.train_span),
                    "test_span_seconds": _seconds(split.test_span),
                    "purge_seconds": _seconds(split.purge),
                    "embargo_seconds": _seconds(split.embargo),
                    "trade_count": "" if performance is None else str(performance.trade_count),
                    "sharpe_ratio": "" if performance is None else f"{performance.sharpe_ratio:f}",
                    "status": _path_status(
                        insufficient=performance is not None and performance.is_insufficient,
                        failed=failure is not None,
                    ),
                    "failure_reason": "" if failure is None else failure.reason,
                }
            )
        )
    return tuple(rows)


def _path_status(*, insufficient: bool, failed: bool) -> str:
    """`failed`, `insufficient` or `included` -- the three states a path can be in.

    `insufficient` is a status rather than an absence: a path below the trade floor is
    excluded from the statistics and must still be visible as a path that ran, or a
    twenty-eight-path summary silently becomes a fourteen-path one.
    """
    if failed:
        return "failed"
    if insufficient:
        return "insufficient"
    return "included"


def cpcv_log_line(report: CpcvReport) -> str:
    """The one-line summary, carrying both gaps and the shape of the distribution.

    The distribution comes first among the statistics and the mean is not alone on the
    line: a mean of 1.1 assembled from paths ranging -0.9 to 3.0 is a strategy with no
    stable edge, and a log line reporting only the mean would be the same omission this
    package exists to prevent, made one layer down.
    """
    partition = report.partition
    plan = partition.plan
    fields: list[str] = [
        "cpcv",
        f"group_total={plan.group_total}",
        f"test_group_size={plan.test_group_size}",
        f"path_total={report.path_total}",
        f"paths_completed={report.paths_completed}",
        f"paths_failed={report.path_failure_total}",
        f"trials_charged={report.trials_charged}",
        f"purge_seconds={_seconds(report.purge)}",
        f"embargo_seconds={_seconds(report.embargo)}",
        f"embargo_floor_seconds={_seconds(plan.embargo_floor)}",
        f"minimum_path_trades={MINIMUM_PATH_TRADES}",
    ]
    distribution = report.distribution
    if distribution is None:
        fields.append("distribution=refused")
        fields.append(f'distribution_refusal="{report.distribution_refusal}"')
        return " ".join(fields)
    fields.extend(
        [
            f"paths_included={distribution.included_path_total}",
            f"paths_insufficient={distribution.insufficient_path_total}",
            f"sharpe_mean={distribution.sharpe_mean:f}",
            f"sharpe_p05={distribution.sharpe_p05:f}",
            f"sharpe_p95={distribution.sharpe_p95:f}",
            f"sharpe_spread={distribution.sharpe_spread:f}",
            f"fraction_of_paths_positive={distribution.fraction_of_paths_positive:f}",
            f"suspiciously_stable={str(distribution.is_suspiciously_stable).lower()}",
        ]
    )
    return " ".join(fields)
