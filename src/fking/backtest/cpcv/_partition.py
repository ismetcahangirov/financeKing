"""The groups, the combinations, and the two gaps that keep them apart.

`N` contiguous groups, every combination of `k` of them as a test set, and the rest as
training -- minus a purge before each test block and an embargo after it. That is the
whole construction, and every part of it that is not arithmetic is a refusal.

**The embargo is stated and checked, not derived.** `CpcvPlan.embargo` is a required
field with no default, and a value below `max_feature_lookback + max_holding_horizon` is
rejected at construction rather than clamped up to the floor. Deriving it silently would
be defensible and is the wrong trade here: CPCV is where this number is quoted into a
result schema and a log line (`BACKTEST_ENGINE.md` section 6.2 asks for three places),
and a value that exists only as a property of something else has one authority and no
point at which a reviewer sees a disagreement. Clamping is worse still -- it produces a
run whose gap nobody chose, reported next to the shorter number that was asked for.

The purge *is* derived, from `WalkForwardDeclaration.purge`, because it is arithmetic on
declared horizons with no floor to violate: a label resolving `label_horizon +
availability_lag` after its decision overlaps the test window or it does not.

**Why the embargo is not symmetric with the purge.** Purging removes training samples
whose label resolves inside the test window -- training on them is training on the
answer. The embargo removes training samples that *begin* after the test window closes,
which sounds unnecessary until you notice that serial correlation leaks backwards: a
sample at `t` is computed from features whose lookback reaches to `t -
max_feature_lookback`, so every sample within one lookback of the test window's close was
computed partly from test-period bars. Without the embargo the model trains on the test
period filtered through a feature lookback, and nothing about the resulting fold table
looks wrong.

`purged_train_intervals` takes the two spans as arguments rather than reading them off a
plan, which is what lets the leakage test call it with a zero embargo and measure what
the floor buys. The floor lives on the plan; this function is the mechanism.
"""

from __future__ import annotations

import math
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime, timedelta
from itertools import combinations
from typing import Final

from fking.backtest.cpcv._errors import CpcvConfigError, CpcvPartitionError
from fking.backtest.walkforward import WalkForwardDeclaration

_UTC_OFFSET: Final = timedelta(0)

_ONE_MICROSECOND: Final = timedelta(microseconds=1)

# Two groups with `k=1` is a train/test split performed twice, with the same single
# boundary seen from both sides. Three is the smallest `N` at which a training set is
# ever assembled from more than one contiguous block, which is the property that makes
# the design combinatorial rather than a fold table.
#
# It also carries the floor `MINIMUM_FOLDS` carries one package over -- one path is one
# draw with one boundary, and a distribution over it is a number wearing a percentile's
# name -- without a second check: the smallest `C(N, k)` admitted by `N >= 3` and
# `1 <= k < N` is `C(3, 1) = 3`. A separate path-count guard would be a branch that can
# never be taken, which is worse than no guard, because it reads as one that can.
MINIMUM_GROUPS: Final = 3


def _require_utc(candidate: object, field_name: str) -> datetime:
    """A timezone-aware datetime whose offset is exactly UTC.

    Rejects rather than converts. A boundary stamped in `Europe/Baku` orders fine,
    compares fine, and moves every group four hours -- straight through a purge measured
    in hours. `astimezone(UTC)` here would launder the wrong offset into a confident
    value with no record that anybody guessed.
    """
    if not isinstance(candidate, datetime):
        raise CpcvConfigError(
            f"{field_name} must be a datetime, got {type(candidate).__name__} {candidate!r}"
        )
    if candidate.tzinfo is None or candidate.utcoffset() is None:
        raise CpcvConfigError(f"{field_name} must be timezone-aware; got naive {candidate!r}")
    if candidate.utcoffset() != _UTC_OFFSET:
        raise CpcvConfigError(f"{field_name} must be UTC; got offset {candidate.utcoffset()!r}")
    return candidate


def _require_non_negative_span(candidate: object, field_name: str) -> timedelta:
    """A `timedelta` at or above zero, and never a bare number of seconds.

    Zero is legal for the two gaps at this level -- `purged_train_intervals` must be
    callable with a zero embargo, because that is the configuration the leakage test
    exists to price. What is not legal is a plan carrying it; that check is on `CpcvPlan`.
    """
    if not isinstance(candidate, timedelta):
        raise CpcvConfigError(
            f"{field_name} must be a timedelta, got {type(candidate).__name__} {candidate!r}"
        )
    if candidate < timedelta(0):
        raise CpcvConfigError(f"{field_name} must not be negative; got {candidate}")
    return candidate


def _require_index(candidate: object, field_name: str) -> int:
    """A non-negative `int`, and specifically not a `bool`.

    `bool` is an `int` in Python, so `path_index=True` type-checks, indexes, formats and
    compares -- and reports every observation against path 1.
    """
    if not isinstance(candidate, int) or isinstance(candidate, bool):
        raise CpcvConfigError(
            f"{field_name} must be an int, got {type(candidate).__name__} {candidate!r}"
        )
    if candidate < 0:
        raise CpcvConfigError(f"{field_name} must not be negative; got {candidate}")
    return candidate


@dataclass(frozen=True, slots=True)
class TimeInterval:
    """A half-open span `[start_utc, end_utc)`, the unit both sets are expressed in.

    Half-open on purpose and stated here rather than left to the reader: the closed
    spelling makes the instant where a training block meets a test block belong to both,
    and one shared bar at that boundary is enough for a label computed from it to have
    been fitted on.
    """

    start_utc: datetime
    end_utc: datetime

    def __post_init__(self) -> None:
        _require_utc(self.start_utc, "start_utc")
        _require_utc(self.end_utc, "end_utc")
        if self.end_utc <= self.start_utc:
            raise CpcvConfigError(
                f"interval is empty ({self.start_utc.isoformat()} .. {self.end_utc.isoformat()})"
            )

    @property
    def span(self) -> timedelta:
        """How long the interval lasts."""
        return self.end_utc - self.start_utc

    def contains(self, moment: datetime) -> bool:
        """Whether `moment` falls inside the half-open span."""
        _require_utc(moment, "moment")
        return self.start_utc <= moment < self.end_utc

    def overlaps(self, other: TimeInterval) -> bool:
        """Whether the two half-open spans share any instant."""
        return self.start_utc < other.end_utc and other.start_utc < self.end_utc


@dataclass(frozen=True, slots=True)
class Group:
    """One of the `N` contiguous blocks the series is cut into.

    The index is carried rather than inferred from position so that a split can name the
    groups it tested on in a form that survives being written to a record and read back.
    """

    group_index: int
    interval: TimeInterval

    def __post_init__(self) -> None:
        _require_index(self.group_index, "group_index")
        if not isinstance(self.interval, TimeInterval):
            raise CpcvConfigError(f"interval must be a TimeInterval, got {self.interval!r}")


@dataclass(frozen=True, slots=True)
class CpcvPlan:
    """A combinatorial purged cross-validation, fully specified before any data is read.

    `group_total` is `N` and `test_group_size` is `k`; the path count is `C(N, k)` and
    every one of those paths is a permanent charge against the global trial counter.
    `N=8, k=2` is 28 paths and therefore 28 trials, which is worth knowing before the run
    rather than after.
    """

    start_utc: datetime
    end_utc: datetime

    #: `N`: how many contiguous groups the window is cut into.
    group_total: int

    #: `k`: how many groups each path tests on.
    test_group_size: int

    declaration: WalkForwardDeclaration

    #: Stated, not derived, and checked against the floor. `declaration.embargo` is the
    #: value to pass unless there is a reason to be more conservative; there is never a
    #: reason to be less, which is what the check enforces.
    embargo: timedelta

    def __post_init__(self) -> None:
        _require_utc(self.start_utc, "start_utc")
        _require_utc(self.end_utc, "end_utc")
        if self.end_utc <= self.start_utc:
            raise CpcvConfigError(
                f"end_utc {self.end_utc.isoformat()} must follow "
                f"start_utc {self.start_utc.isoformat()}"
            )
        if not isinstance(self.declaration, WalkForwardDeclaration):
            raise CpcvConfigError(
                f"declaration must be a WalkForwardDeclaration, got {self.declaration!r}"
            )
        _require_index(self.group_total, "group_total")
        _require_index(self.test_group_size, "test_group_size")
        if self.group_total < MINIMUM_GROUPS:
            raise CpcvConfigError(
                f"group_total must be at least {MINIMUM_GROUPS}; got {self.group_total}. "
                f"Below that the training set is a single contiguous block on every path "
                f"and the design is a fold table, not a combinatorial partition"
            )
        if self.test_group_size < 1 or self.test_group_size >= self.group_total:
            raise CpcvConfigError(
                f"test_group_size must be at least 1 and below group_total "
                f"{self.group_total}; got {self.test_group_size}"
            )
        _require_non_negative_span(self.embargo, "embargo")
        floor = self.embargo_floor
        if self.embargo < floor:
            raise CpcvConfigError(
                f"embargo {self.embargo} is below the floor {floor} implied by "
                f"max_feature_lookback {self.declaration.max_feature_lookback} + "
                f"max_holding_horizon {self.declaration.max_holding_horizon}; it is refused "
                f"rather than clamped, because a run whose gap nobody chose is reported "
                f"next to the shorter number that was asked for"
            )
        if (self.end_utc - self.start_utc) // _ONE_MICROSECOND < self.group_total:
            raise CpcvConfigError(
                f"window {self.start_utc.isoformat()} .. {self.end_utc.isoformat()} is too "
                f"short to cut into {self.group_total} non-empty groups"
            )

    @property
    def path_total(self) -> int:
        """`C(N, k)`: the number of paths, and therefore the number of trials charged."""
        return math.comb(self.group_total, self.test_group_size)

    @property
    def purge(self) -> timedelta:
        """Derived from the declaration: how far a label reaches past its decision."""
        return self.declaration.purge

    @property
    def embargo_floor(self) -> timedelta:
        """`max_feature_lookback + max_holding_horizon`, stated by the issue as a floor.

        A strategy with a four-hour feature lookback and a six-hour maximum hold needs at
        least ten hours. Using one bar because "it is crypto, it is fast" reintroduces
        exactly the leak the embargo exists to close.
        """
        return self.declaration.max_feature_lookback + self.declaration.max_holding_horizon

    @property
    def window(self) -> TimeInterval:
        """The whole series, as the interval the training set is carved out of."""
        return TimeInterval(start_utc=self.start_utc, end_utc=self.end_utc)


@dataclass(frozen=True, slots=True)
class CpcvSplit:
    """One path: the groups it tests on, and what is left to train on after the gaps.

    The overlap check is in `__post_init__` rather than in the builder, so that a split
    assembled by hand -- in a test, in a notebook, by a future second builder -- cannot
    be the one that leaks. `BACKTEST_ENGINE.md` calls an overlapping train and test range
    a hard failure; a check that only the sanctioned constructor performs is a check the
    unsanctioned path skips.
    """

    path_index: int
    test_group_indices: tuple[int, ...]
    test_intervals: tuple[TimeInterval, ...]
    train_intervals: tuple[TimeInterval, ...]
    purge: timedelta
    embargo: timedelta

    def __post_init__(self) -> None:
        _require_index(self.path_index, "path_index")
        _require_non_negative_span(self.purge, "purge")
        _require_non_negative_span(self.embargo, "embargo")
        if not self.test_group_indices:
            raise CpcvPartitionError(f"path {self.path_index}: no test groups")
        if not self.test_intervals:
            raise CpcvPartitionError(f"path {self.path_index}: no test intervals")
        if not self.train_intervals:
            # Not an empty tuple quietly returned. A path with nothing left to train on
            # after the gaps is a path whose out-of-sample number would be produced by an
            # unfitted model, and that number is not a worse estimate -- it is a
            # different quantity entirely.
            raise CpcvPartitionError(
                f"path {self.path_index}: purge {self.purge} and embargo {self.embargo} "
                f"consume every training interval; the window is too short or the groups "
                f"are too few for these horizons"
            )
        for test_interval in self.test_intervals:
            forbidden = TimeInterval(
                start_utc=test_interval.start_utc - self.purge,
                end_utc=test_interval.end_utc + self.embargo,
            )
            for train_interval in self.train_intervals:
                if train_interval.overlaps(forbidden):
                    raise CpcvPartitionError(
                        f"path {self.path_index}: training interval "
                        f"{train_interval.start_utc.isoformat()} .. "
                        f"{train_interval.end_utc.isoformat()} overlaps the test interval "
                        f"{test_interval.start_utc.isoformat()} .. "
                        f"{test_interval.end_utc.isoformat()} widened by purge "
                        f"{self.purge} and embargo {self.embargo}"
                    )

    @property
    def train_span(self) -> timedelta:
        """Total training time left after purging and embargoing."""
        return sum((interval.span for interval in self.train_intervals), timedelta(0))

    @property
    def test_span(self) -> timedelta:
        """Total out-of-sample time this path is scored on."""
        return sum((interval.span for interval in self.test_intervals), timedelta(0))


@dataclass(frozen=True, slots=True)
class CpcvPartition:
    """The plan, its groups, and every split it admits.

    The two gaps are carried alongside the splits rather than recomputed by whoever reads
    them, because this object is the output schema `BACKTEST_ENGINE.md` section 6.2 names
    as one of the three places purge and embargo must appear. A partition that reported
    only its boundaries would make the number that is silently wrong most often the one
    number a reader has to derive.
    """

    plan: CpcvPlan
    groups: tuple[Group, ...]
    splits: tuple[CpcvSplit, ...]
    purge: timedelta
    embargo: timedelta

    def __post_init__(self) -> None:
        if len(self.splits) != self.plan.path_total:
            raise CpcvConfigError(
                f"{len(self.splits)} split(s) against C({self.plan.group_total}, "
                f"{self.plan.test_group_size}) = {self.plan.path_total} paths; a partition "
                f"missing a path is a search reported at less than it cost"
            )

    @property
    def path_total(self) -> int:
        """How many paths this partition specifies, and so how many trials it charges."""
        return len(self.splits)


def build_groups(plan: CpcvPlan) -> tuple[Group, ...]:
    """Cut the window into `N` contiguous, non-overlapping, gap-free groups.

    Boundaries are computed in whole microseconds by integer arithmetic rather than by
    repeatedly adding a `timedelta` quotient. Accumulated division would leave the last
    group short or long by a rounding remainder, and a group boundary that depends on how
    the span divides is a boundary two runs of the same plan can disagree about.
    """
    total_microseconds = (plan.end_utc - plan.start_utc) // _ONE_MICROSECOND
    boundaries = [
        plan.start_utc + timedelta(microseconds=total_microseconds * index // plan.group_total)
        for index in range(plan.group_total + 1)
    ]
    return tuple(
        Group(
            group_index=index,
            interval=TimeInterval(start_utc=boundaries[index], end_utc=boundaries[index + 1]),
        )
        for index in range(plan.group_total)
    )


def merge_adjacent(intervals: Sequence[TimeInterval]) -> tuple[TimeInterval, ...]:
    """Fuse touching or overlapping intervals into the fewest that cover the same span.

    Two adjacent test groups are one test *block*, and the distinction matters: purging
    and embargoing them separately would carve a gap out of the middle of a contiguous
    test period and count the hole as training data.
    """
    if not intervals:
        return ()
    ordered = sorted(intervals, key=lambda interval: (interval.start_utc, interval.end_utc))
    merged: list[TimeInterval] = [ordered[0]]
    for interval in ordered[1:]:
        last = merged[-1]
        if interval.start_utc <= last.end_utc:
            merged[-1] = TimeInterval(
                start_utc=last.start_utc,
                end_utc=max(last.end_utc, interval.end_utc),
            )
        else:
            merged.append(interval)
    return tuple(merged)


def purged_train_intervals(
    window: TimeInterval,
    test_intervals: Sequence[TimeInterval],
    *,
    purge: timedelta,
    embargo: timedelta,
) -> tuple[TimeInterval, ...]:
    """What is left of `window` once every test block and both its gaps are removed.

    Each test block `[t0, t1)` removes `[t0 - purge, t1 + embargo)`. The left gap is the
    purge: a training sample at `s` carries a label that resolves at `s + purge`, so it
    overlaps the test window exactly when `s > t0 - purge`. The right gap is the embargo:
    a training sample at `s` is computed from features reaching back to
    `s - max_feature_lookback`, so it still sees the test period when
    `s < t1 + max_feature_lookback`, and the position it opens can still be held into it
    for `max_holding_horizon` beyond that.

    The spans are arguments rather than plan attributes on purpose. `CpcvPlan` refuses an
    embargo below its floor; this function will happily run with zero, which is what lets
    a leakage test measure the difference as a number instead of asserting it as a claim.
    """
    _require_non_negative_span(purge, "purge")
    _require_non_negative_span(embargo, "embargo")
    forbidden = merge_adjacent(
        [
            TimeInterval(
                start_utc=interval.start_utc - purge,
                end_utc=interval.end_utc + embargo,
            )
            for interval in test_intervals
        ]
    )
    remaining: list[TimeInterval] = []
    cursor = window.start_utc
    for block in forbidden:
        if block.start_utc > cursor:
            remaining.append(
                TimeInterval(start_utc=cursor, end_utc=min(block.start_utc, window.end_utc))
            )
        cursor = max(cursor, block.end_utc)
        if cursor >= window.end_utc:
            break
    if cursor < window.end_utc:
        remaining.append(TimeInterval(start_utc=cursor, end_utc=window.end_utc))
    return tuple(remaining)


def build_splits(plan: CpcvPlan) -> CpcvPartition:
    """Every combination of `k` groups, with its purged and embargoed training set.

    Paths are emitted in `itertools.combinations` order -- lexicographic by group index --
    so `path_index` is reproducible from the plan alone. A path identified by its position
    in a set iteration would be a path that cannot be looked up in last month's report.
    """
    groups = build_groups(plan)
    purge = plan.purge
    embargo = plan.embargo
    window = plan.window

    choices = combinations(range(plan.group_total), plan.test_group_size)
    splits: list[CpcvSplit] = []
    for path_index, chosen in enumerate(choices):
        test_intervals = merge_adjacent([groups[index].interval for index in chosen])
        splits.append(
            CpcvSplit(
                path_index=path_index,
                test_group_indices=tuple(chosen),
                test_intervals=test_intervals,
                train_intervals=purged_train_intervals(
                    window, test_intervals, purge=purge, embargo=embargo
                ),
                purge=purge,
                embargo=embargo,
            )
        )

    return CpcvPartition(
        plan=plan,
        groups=groups,
        splits=tuple(splits),
        purge=purge,
        embargo=embargo,
    )
