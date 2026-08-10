"""The partition: groups, combinations, and the two gaps that must separate them.

The refusals get as much space as the arithmetic, because every one of them guards a
condition that produces a *better-looking* result rather than a crash. A clamped embargo,
a silently-dropped path, a training block that reaches one bar into the test period --
none of those fail. They all report a cleaner edge.
"""

from __future__ import annotations

import math
from datetime import datetime, timedelta, timezone
from itertools import pairwise

import pytest

from fking.backtest.cpcv import (
    MINIMUM_GROUPS,
    CpcvConfigError,
    CpcvPartitionError,
    CpcvPlan,
    CpcvSplit,
    Group,
    TimeInterval,
    build_groups,
    build_splits,
    merge_adjacent,
    purged_train_intervals,
)
from fking.backtest.walkforward import WalkForwardDeclaration
from tests.backtest.cpcv_support import (
    DECLARATION,
    GROUP_TOTAL,
    PATH_TOTAL,
    TEST_GROUP_SIZE,
    WINDOW_END,
    WINDOW_START,
    plan_for,
)


def test_groups_tile_the_window_without_gap_or_overlap() -> None:
    groups = build_groups(plan_for())

    assert len(groups) == GROUP_TOTAL
    assert groups[0].interval.start_utc == WINDOW_START
    assert groups[-1].interval.end_utc == WINDOW_END
    for earlier, later in pairwise(groups):
        assert earlier.interval.end_utc == later.interval.start_utc
        assert earlier.group_index + 1 == later.group_index


def test_group_boundaries_do_not_drift_on_an_indivisible_window() -> None:
    """A span that does not divide by `N` still tiles exactly, with no lost microsecond.

    Accumulated `timedelta` division would leave the final group short by the remainder,
    and a boundary that depends on how the span divides is a boundary two runs of the same
    plan can disagree about.
    """
    plan = plan_for(
        group_total=7,
        end_utc=WINDOW_START + timedelta(days=100, microseconds=3),
    )

    groups = build_groups(plan)

    assert groups[-1].interval.end_utc == plan.end_utc
    covered = sum((group.interval.span for group in groups), timedelta(0))
    assert covered == plan.end_utc - plan.start_utc


def test_the_worked_example_produces_twenty_eight_paths_in_lexicographic_order() -> None:
    partition = build_splits(plan_for())

    assert partition.path_total == math.comb(GROUP_TOTAL, TEST_GROUP_SIZE) == PATH_TOTAL
    assert [split.path_index for split in partition.splits] == list(range(PATH_TOTAL))
    assert partition.splits[0].test_group_indices == (0, 1)
    assert partition.splits[-1].test_group_indices == (6, 7)
    assert len({split.test_group_indices for split in partition.splits}) == PATH_TOTAL


def test_the_partition_is_reproducible_from_the_plan_alone() -> None:
    assert build_splits(plan_for()) == build_splits(plan_for())


def test_adjacent_test_groups_become_one_block_rather_than_two() -> None:
    """Purging two touching groups separately would carve a hole out of the test period.

    The hole would then be counted as training data sitting inside the test window, which
    is the leak the purge exists to close, introduced by the purge itself.
    """
    partition = build_splits(plan_for())
    adjacent = next(split for split in partition.splits if split.test_group_indices == (3, 4))
    disjoint = next(split for split in partition.splits if split.test_group_indices == (1, 5))

    assert len(adjacent.test_intervals) == 1
    assert len(disjoint.test_intervals) == TEST_GROUP_SIZE


def test_every_training_interval_clears_the_purge_and_the_embargo() -> None:
    partition = build_splits(plan_for())

    for split in partition.splits:
        for test_interval in split.test_intervals:
            for train_interval in split.train_intervals:
                if train_interval.end_utc <= test_interval.start_utc:
                    assert test_interval.start_utc - train_interval.end_utc >= split.purge
                else:
                    assert train_interval.start_utc - test_interval.end_utc >= split.embargo


def test_purged_intervals_are_the_plain_complement_when_both_gaps_are_zero() -> None:
    """The gaps are what removes data; without them nothing but the test block goes."""
    window = TimeInterval(start_utc=WINDOW_START, end_utc=WINDOW_END)
    test_interval = TimeInterval(
        start_utc=WINDOW_START + timedelta(days=30),
        end_utc=WINDOW_START + timedelta(days=60),
    )

    remaining = purged_train_intervals(
        window, [test_interval], purge=timedelta(0), embargo=timedelta(0)
    )

    assert remaining == (
        TimeInterval(start_utc=WINDOW_START, end_utc=test_interval.start_utc),
        TimeInterval(start_utc=test_interval.end_utc, end_utc=WINDOW_END),
    )


def test_a_test_block_at_the_window_edge_leaves_one_training_interval() -> None:
    window = TimeInterval(start_utc=WINDOW_START, end_utc=WINDOW_END)
    leading = TimeInterval(start_utc=WINDOW_START, end_utc=WINDOW_START + timedelta(days=30))

    remaining = purged_train_intervals(
        window, [leading], purge=timedelta(hours=24), embargo=timedelta(hours=24)
    )

    assert remaining == (
        TimeInterval(start_utc=leading.end_utc + timedelta(hours=24), end_utc=WINDOW_END),
    )


def test_merge_adjacent_fuses_touching_intervals_and_keeps_disjoint_ones() -> None:
    first = TimeInterval(start_utc=WINDOW_START, end_utc=WINDOW_START + timedelta(days=1))
    touching = TimeInterval(
        start_utc=WINDOW_START + timedelta(days=1), end_utc=WINDOW_START + timedelta(days=2)
    )
    apart = TimeInterval(
        start_utc=WINDOW_START + timedelta(days=5), end_utc=WINDOW_START + timedelta(days=6)
    )

    assert merge_adjacent([touching, first, apart]) == (
        TimeInterval(start_utc=first.start_utc, end_utc=touching.end_utc),
        apart,
    )
    assert merge_adjacent([]) == ()


def test_an_embargo_below_the_floor_is_refused_not_clamped() -> None:
    """The acceptance criterion, asserted on both halves.

    Refused: the construction raises. Not clamped: no plan exists afterwards carrying the
    floor in place of the value that was asked for, which is what a `max()` would have
    produced -- a run whose gap nobody chose, reported next to a shorter number.
    """
    floor = DECLARATION.max_feature_lookback + DECLARATION.max_holding_horizon
    assert floor == timedelta(hours=10)

    with pytest.raises(CpcvConfigError, match="below the floor"):
        plan_for(embargo=floor - timedelta(seconds=1))


def test_an_embargo_exactly_at_the_floor_is_accepted() -> None:
    floor = timedelta(hours=10)
    declaration = WalkForwardDeclaration(
        label_horizon=timedelta(hours=1),
        availability_lag=timedelta(0),
        max_feature_lookback=timedelta(hours=4),
        max_holding_horizon=timedelta(hours=6),
    )

    plan = plan_for(declaration=declaration, embargo=floor)

    assert plan.embargo == floor
    assert plan.embargo_floor == floor


def test_a_split_whose_training_range_reaches_into_the_purge_is_a_hard_failure() -> None:
    """Hand-assembled, because that is the path a future second builder would take.

    The check lives on the split rather than in `build_splits` for exactly this reason: a
    validation only the sanctioned constructor performs is a validation the unsanctioned
    path skips, and `BACKTEST_ENGINE.md` calls an overlapping train and test range a hard
    failure rather than a warning.
    """
    test_interval = TimeInterval(
        start_utc=WINDOW_START + timedelta(days=30),
        end_utc=WINDOW_START + timedelta(days=60),
    )
    purge = timedelta(hours=24)

    with pytest.raises(CpcvPartitionError, match="overlaps the test interval"):
        CpcvSplit(
            path_index=0,
            test_group_indices=(1,),
            test_intervals=(test_interval,),
            # Ends one second inside the purge: the last training label resolves after the
            # test window has already opened.
            train_intervals=(
                TimeInterval(
                    start_utc=WINDOW_START,
                    end_utc=test_interval.start_utc - purge + timedelta(seconds=1),
                ),
            ),
            purge=purge,
            embargo=timedelta(hours=24),
        )


def test_a_split_whose_training_range_touches_the_purge_boundary_is_accepted() -> None:
    """The half-open boundary, asserted from the legal side of it."""
    test_interval = TimeInterval(
        start_utc=WINDOW_START + timedelta(days=30),
        end_utc=WINDOW_START + timedelta(days=60),
    )
    purge = timedelta(hours=24)

    split = CpcvSplit(
        path_index=0,
        test_group_indices=(1,),
        test_intervals=(test_interval,),
        train_intervals=(
            TimeInterval(start_utc=WINDOW_START, end_utc=test_interval.start_utc - purge),
        ),
        purge=purge,
        embargo=timedelta(hours=24),
    )

    assert split.train_span == timedelta(days=29)
    assert split.test_span == timedelta(days=30)


def test_a_split_with_no_training_data_left_is_refused() -> None:
    with pytest.raises(CpcvPartitionError, match="consume every training interval"):
        CpcvSplit(
            path_index=0,
            test_group_indices=(0,),
            test_intervals=(
                TimeInterval(start_utc=WINDOW_START, end_utc=WINDOW_START + timedelta(days=1)),
            ),
            train_intervals=(),
            purge=timedelta(hours=24),
            embargo=timedelta(hours=24),
        )


@pytest.mark.parametrize(
    ("group_total", "test_group_size", "expected"),
    [
        (2, 1, f"group_total must be at least {MINIMUM_GROUPS}"),
        (8, 0, "test_group_size must be at least 1"),
        (8, 8, "test_group_size must be at least 1"),
    ],
)
def test_degenerate_shapes_are_refused(
    group_total: int, test_group_size: int, expected: str
) -> None:
    with pytest.raises(CpcvConfigError, match=expected):
        plan_for(group_total=group_total, test_group_size=test_group_size)


def test_a_naive_boundary_is_rejected_rather_than_localised() -> None:
    with pytest.raises(CpcvConfigError, match="timezone-aware"):
        plan_for(start_utc=datetime(2024, 1, 1))  # noqa: DTZ001


def test_a_non_utc_boundary_is_rejected_rather_than_converted() -> None:
    """`astimezone` here would move every group by the offset and record nothing."""
    baku = timezone(timedelta(hours=4))

    with pytest.raises(CpcvConfigError, match="must be UTC"):
        plan_for(start_utc=datetime(2024, 1, 1, tzinfo=baku))


def test_a_window_that_ends_before_it_starts_is_refused() -> None:
    with pytest.raises(CpcvConfigError, match="must follow"):
        plan_for(end_utc=WINDOW_START - timedelta(days=1))


def test_the_smallest_legal_shape_still_yields_a_distribution_worth_the_name() -> None:
    """`N >= 3` is what makes a separate path-count floor unnecessary.

    The smallest admitted `C(N, k)` is `C(3, 1) = 3`, so no legal plan can produce the
    one-path case a percentile would launder into a distribution. Asserted here rather
    than guarded at runtime, because a branch that can never be taken reads as one that
    can.
    """
    smallest = build_splits(plan_for(group_total=MINIMUM_GROUPS, test_group_size=1))

    assert smallest.path_total == MINIMUM_GROUPS


@pytest.mark.parametrize(
    ("field_name", "bad_value", "expected"),
    [
        ("group_total", "eight", "group_total must be an int"),
        ("test_group_size", -1, "test_group_size must not be negative"),
        ("embargo", 36000, "embargo must be a timedelta"),
        ("embargo", timedelta(hours=-1), "embargo must not be negative"),
    ],
)
def test_malformed_plan_fields_are_refused_at_construction(
    field_name: str, bad_value: object, expected: str
) -> None:
    """Every one of these type-checks under `mypy --strict` and none of them is safe.

    The values reaching a plan come from a config file and from an agent's output, which
    `docs/rules/llm-output-handling.md` treats as hostile input. A `bool` group_total or a
    bare `36000` embargo would index, compare and format perfectly well, and produce a
    partition whose gaps are three orders of magnitude from what was asked for.
    """
    fields: dict[str, object] = {
        "start_utc": WINDOW_START,
        "end_utc": WINDOW_END,
        "group_total": GROUP_TOTAL,
        "test_group_size": TEST_GROUP_SIZE,
        "declaration": DECLARATION,
        "embargo": DECLARATION.embargo,
    }
    fields[field_name] = bad_value

    with pytest.raises(CpcvConfigError, match=expected):
        CpcvPlan(**fields)  # type: ignore[arg-type]


def test_a_group_holding_something_that_is_not_an_interval_is_refused() -> None:
    with pytest.raises(CpcvConfigError, match="interval must be a TimeInterval"):
        Group(group_index=0, interval=(WINDOW_START, WINDOW_END))  # type: ignore[arg-type]


def test_an_empty_interval_is_refused() -> None:
    with pytest.raises(CpcvConfigError, match="interval is empty"):
        TimeInterval(start_utc=WINDOW_START, end_utc=WINDOW_START)


@pytest.mark.parametrize(
    ("test_group_indices", "test_intervals", "expected"),
    [
        ((), (), "no test groups"),
        ((1,), (), "no test intervals"),
    ],
)
def test_a_split_with_nothing_under_test_is_refused(
    test_group_indices: tuple[int, ...],
    test_intervals: tuple[TimeInterval, ...],
    expected: str,
) -> None:
    with pytest.raises(CpcvPartitionError, match=expected):
        CpcvSplit(
            path_index=0,
            test_group_indices=test_group_indices,
            test_intervals=test_intervals,
            train_intervals=(
                TimeInterval(start_utc=WINDOW_START, end_utc=WINDOW_START + timedelta(days=1)),
            ),
            purge=timedelta(hours=24),
            embargo=timedelta(hours=24),
        )


def test_a_window_too_short_for_its_groups_is_refused() -> None:
    with pytest.raises(CpcvConfigError, match="too short to cut into"):
        plan_for(
            group_total=8,
            end_utc=WINDOW_START + timedelta(microseconds=4),
        )


def test_the_plan_derives_its_purge_from_the_declaration() -> None:
    """Purge is derived; only the embargo is stated. Both reach the partition."""
    plan = plan_for()
    partition = build_splits(plan)

    assert plan.purge == DECLARATION.label_horizon + DECLARATION.availability_lag
    assert partition.purge == plan.purge
    assert partition.embargo == plan.embargo
    assert all(split.purge == plan.purge for split in partition.splits)
    assert all(split.embargo == plan.embargo for split in partition.splits)


def test_a_partition_missing_a_path_is_refused() -> None:
    partition = build_splits(plan_for())

    with pytest.raises(CpcvConfigError, match="a partition missing a path"):
        type(partition)(
            plan=partition.plan,
            groups=partition.groups,
            splits=partition.splits[:-1],
            purge=partition.purge,
            embargo=partition.embargo,
        )


def test_plan_rejects_a_declaration_of_the_wrong_type() -> None:
    with pytest.raises(CpcvConfigError, match="must be a WalkForwardDeclaration"):
        CpcvPlan(
            start_utc=WINDOW_START,
            end_utc=WINDOW_END,
            group_total=GROUP_TOTAL,
            test_group_size=TEST_GROUP_SIZE,
            declaration="24h",  # type: ignore[arg-type]
            embargo=timedelta(hours=24),
        )
