"""What the run says about itself: the distribution, the exclusions, and the two gaps.

Two acceptance criteria live here. Purge and embargo must be present in the result schema
*and* in the emitted log line -- the second and third of the three places
`BACKTEST_ENGINE.md` section 6.2 asks for -- and paths below the trade floor must be
marked `insufficient`, excluded from the statistics, and *counted*, rather than absorbed
into a mean that no longer says what it was computed from.
"""

from __future__ import annotations

from decimal import Decimal

import pytest

from fking.backtest.cpcv import (
    MINIMUM_PATH_TRADES,
    SUSPICIOUS_SHARPE_SPREAD,
    CpcvConfigError,
    CpcvPathEvaluationError,
    CpcvReport,
    CpcvSplit,
    DistributionRefusedError,
    PathPerformance,
    build_splits,
    cpcv_log_line,
    cpcv_path_table,
    path_distribution,
    run_cpcv,
)
from tests.backtest.cpcv_support import DECLARATION, PATH_TOTAL, plan_for

# Twenty-eight Sharpes spanning -0.9 to 3.0: the distribution the issue names, whose mean
# of roughly 1.1 says nothing about the strategy that produced it.
_SPREAD_SHARPES: tuple[str, ...] = (
    "-0.9",
    "-0.4",
    "-0.2",
    "0.0",
    "0.1",
    "0.3",
    "0.4",
    "0.5",
    "0.6",
    "0.7",
    "0.8",
    "0.9",
    "1.0",
    "1.1",
    "1.2",
    "1.3",
    "1.4",
    "1.5",
    "1.6",
    "1.7",
    "1.9",
    "2.0",
    "2.2",
    "2.4",
    "2.5",
    "2.6",
    "2.8",
    "3.0",
)


#: The path made to fail in the table test. Named so the row lookup and the evaluator
#: cannot drift apart.
FAILING_PATH = 7


def _thin_paths() -> frozenset[int]:
    """The paths the fixture makes too thin to count: three trades each."""
    return frozenset({2, 5, 11, 19})


def _run_with_spread() -> CpcvReport:
    partition = build_splits(plan_for())

    def evaluate(split: CpcvSplit) -> PathPerformance:
        thin = split.path_index in _thin_paths()
        return PathPerformance(
            path_index=split.path_index,
            trade_count=3 if thin else 140,
            sharpe_ratio=Decimal(_SPREAD_SHARPES[split.path_index]),
        )

    return run_cpcv(partition, evaluate=evaluate, charge=lambda _split: None)


def test_the_log_line_carries_both_gaps_and_the_floor_they_were_checked_against() -> None:
    """The third of the three places. A gap that appears nowhere a reader looks is a gap
    nobody checks, and the embargo is the number that is silently wrong most often."""
    report = _run_with_spread()
    line = cpcv_log_line(report)

    # purge = 24h label horizon + 0 availability lag; embargo = max(10h floor, 24h purge).
    assert "purge_seconds=86400" in line
    assert "embargo_seconds=86400" in line
    assert "embargo_floor_seconds=36000" in line
    assert f"minimum_path_trades={MINIMUM_PATH_TRADES}" in line


def test_the_log_line_reports_the_spread_rather_than_the_mean_alone() -> None:
    report = _run_with_spread()
    line = cpcv_log_line(report)

    for field_name in (
        "sharpe_mean=",
        "sharpe_p05=",
        "sharpe_p95=",
        "sharpe_spread=",
        "fraction_of_paths_positive=",
        "paths_insufficient=",
        "suspiciously_stable=",
    ):
        assert field_name in line


def test_the_result_schema_carries_both_gaps_without_the_reader_deriving_them() -> None:
    report = _run_with_spread()

    assert report.purge == DECLARATION.purge
    assert report.embargo == DECLARATION.embargo
    assert report.partition.purge == DECLARATION.purge
    assert report.partition.embargo == DECLARATION.embargo


def test_every_table_row_repeats_the_two_gaps() -> None:
    """On every row, not once in a header: a row quoted into an incident note that has
    been separated from its header has lost the two numbers most worth checking."""
    rows = cpcv_path_table(_run_with_spread())

    assert len(rows) == PATH_TOTAL
    assert all(row["purge_seconds"] == "86400" for row in rows)
    assert all(row["embargo_seconds"] == "86400" for row in rows)


def test_thin_paths_are_marked_excluded_and_counted_rather_than_absorbed() -> None:
    report = _run_with_spread()
    distribution = report.distribution
    rows = cpcv_path_table(report)

    assert distribution is not None
    assert distribution.path_total == PATH_TOTAL
    assert distribution.insufficient_path_total == len(_thin_paths())
    assert distribution.included_path_total == PATH_TOTAL - len(_thin_paths())
    assert set(distribution.insufficient_path_indices) == _thin_paths()
    assert {int(row["path_index"]) for row in rows if row["status"] == "insufficient"} == (
        _thin_paths()
    )
    # The excluded Sharpes are absent from the statistics but their trade counts remain
    # visible, which is what makes a twenty-eight-path summary of twenty-four paths
    # readable as one.
    assert len(distribution.trade_counts) == PATH_TOTAL
    assert sorted(distribution.trade_counts)[: len(_thin_paths())] == [3] * len(_thin_paths())


def test_the_mean_is_computed_over_the_included_paths_only() -> None:
    included = [
        Decimal(_SPREAD_SHARPES[index]) for index in range(PATH_TOTAL) if index not in _thin_paths()
    ]
    expected_mean = (sum(included, Decimal(0)) / Decimal(len(included))).quantize(
        Decimal("0.000000000001")
    )

    distribution = _run_with_spread().distribution

    assert distribution is not None
    assert distribution.sharpe_mean == expected_mean


def test_the_percentiles_bracket_the_paths_and_the_spread_is_their_difference() -> None:
    distribution = _run_with_spread().distribution

    assert distribution is not None
    assert distribution.sharpe_p05 < distribution.sharpe_mean < distribution.sharpe_p95
    assert distribution.sharpe_spread == distribution.sharpe_p95 - distribution.sharpe_p05
    assert not distribution.is_suspiciously_stable


def test_a_narrow_spread_is_flagged_as_a_defect_signal_not_a_triumph() -> None:
    """Either the folds are not independent or the same data is in every training set.

    The flag exists because the plain-language reading of the number -- "very consistent"
    -- is the opposite of what it means, and a reader who is not told that will take it.
    """
    performances = [
        PathPerformance(
            path_index=index,
            trade_count=140,
            sharpe_ratio=Decimal("1.10") + Decimal(index) / Decimal("10000"),
        )
        for index in range(PATH_TOTAL)
    ]

    distribution = path_distribution(performances)

    assert distribution.sharpe_spread < SUSPICIOUS_SHARPE_SPREAD
    assert distribution.is_suspiciously_stable


def test_the_fraction_of_positive_paths_is_reported_over_the_included_paths() -> None:
    performances = [
        PathPerformance(
            path_index=index,
            trade_count=140,
            sharpe_ratio=Decimal("1") if index % 4 == 0 else Decimal("-1"),
        )
        for index in range(PATH_TOTAL)
    ]

    distribution = path_distribution(performances)

    assert distribution.fraction_of_paths_positive == Decimal("0.25").quantize(
        Decimal("0.000000000001")
    )


def test_all_thin_paths_refuse_a_distribution_rather_than_returning_zeros() -> None:
    """A `sharpe_p05` of zero reads as "measured, and flat". That is a different
    statement from "not measured", and it is the one a reader will act on."""
    partition = build_splits(plan_for())

    def evaluate(split: CpcvSplit) -> PathPerformance:
        return PathPerformance(
            path_index=split.path_index, trade_count=3, sharpe_ratio=Decimal("2.5")
        )

    report = run_cpcv(partition, evaluate=evaluate, charge=lambda _split: None)

    assert report.distribution is None
    assert "cleared 30 trades" in report.distribution_refusal
    assert "distribution=refused" in cpcv_log_line(report)
    # The trials are charged regardless. A run that produced no distribution still
    # consumed twenty-eight paths' worth of search.
    assert report.trials_charged == PATH_TOTAL


def test_a_failed_path_appears_in_the_table_with_its_reason() -> None:
    partition = build_splits(plan_for())

    def evaluate(split: CpcvSplit) -> PathPerformance:
        if split.path_index == FAILING_PATH:
            raise CpcvPathEvaluationError("archive served 0 bars")
        return PathPerformance(
            path_index=split.path_index,
            trade_count=140,
            sharpe_ratio=Decimal(_SPREAD_SHARPES[split.path_index]),
        )

    report = run_cpcv(partition, evaluate=evaluate, charge=lambda _split: None)
    rows = cpcv_path_table(report)

    assert len(rows) == PATH_TOTAL
    failed = next(row for row in rows if row["path_index"] == str(FAILING_PATH))
    assert failed["status"] == "failed"
    assert failed["failure_reason"] == "archive served 0 bars"
    assert failed["sharpe_ratio"] == ""
    assert report.distribution is not None
    assert report.distribution.path_total == PATH_TOTAL - 1


def test_a_distribution_over_no_paths_is_refused() -> None:
    with pytest.raises(DistributionRefusedError, match="zero paths"):
        path_distribution([])


def test_a_float_sharpe_is_refused_at_the_performance_boundary() -> None:
    """A float fill price is negligent; a float Sharpe here would be a determinism
    hazard, because the percentile it feeds is carried into a promotion decision."""
    with pytest.raises(CpcvConfigError, match="not a float"):
        PathPerformance(path_index=0, trade_count=140, sharpe_ratio=1.1)  # type: ignore[arg-type]


def test_a_non_finite_sharpe_is_refused() -> None:
    with pytest.raises(CpcvConfigError, match="must be finite"):
        PathPerformance(path_index=0, trade_count=140, sharpe_ratio=Decimal("NaN"))
