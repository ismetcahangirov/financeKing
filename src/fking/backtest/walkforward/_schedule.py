"""The fold boundaries, and the purge and embargo that separate them.

Two schemes from one field. **Anchored** grows the training window from a fixed origin;
**rolling** slides a fixed-length one. They are not two harnesses -- the test windows,
the purge, the embargo and the fold count are identical, and the only difference is
where each training window starts. Writing them as one function is what makes that
claim checkable rather than asserted.

The scheme is chosen from the strategy's **re-fit story**, not from habit
(`BACKTEST_ENGINE.md` section 6.1). `step` is the re-fit cadence, and it must be the one
production would use: a harness that re-fits monthly against a strategy that would re-fit
quarterly has validated a strategy that will never exist. That is why `step` is required
and has no default -- the default anybody would reach for is "same as the test window",
which is a statement about the harness rather than about the strategy.

**Purge and embargo are derived, never chosen.** They come from
`WalkForwardDeclaration`, which carries the numbers the feature and the strategy already
declared: `FeatureSpec.label_horizon`, `FeatureSpec.availability_lag`,
`FeatureSpec.lookback` and the strategy's maximum holding horizon. A harness that took
purge and embargo as free parameters would have made them tunable, and the direction
they would be tuned is shorter -- a shorter embargo lets adjacent folds share
information, which raises every fold's Sharpe and looks like an improvement.

They appear in three places, which is the count `BACKTEST_ENGINE.md` section 6.2 asks
for and the reason `WalkForwardSchedule` carries them alongside the folds rather than
discarding them once the boundaries are computed: the plan, the emitted fold table, and
the log line the caller writes from `schedule_table`.
"""

from __future__ import annotations

import enum
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime, timedelta
from types import MappingProxyType
from typing import Final

from fking.backtest.walkforward._errors import (
    ScheduleRefusedError,
    WalkForwardConfigError,
)
from fking.backtest.walkforward._guards import require_utc

# A walk-forward reporting one fold is a single train/test split wearing the name of a
# validation methodology. Two is the smallest number that produces a boundary the author
# did not choose, and the smallest at which the fold table says anything the mean of one
# number does not.
MINIMUM_FOLDS: Final = 2


class WalkForwardScheme(enum.Enum):
    """Which end of the training window moves.

    An enum rather than a `bool` named `is_anchored`: the emitted fold table names the
    scheme, and a table row reading `scheme=False` is a row nobody can read back.
    """

    #: The training window grows from a fixed origin. For strategies that accumulate
    #: history and whose production re-fit uses everything available.
    ANCHORED = "anchored"

    #: A fixed-length training window slides forward. For strategies with a bounded
    #: memory, where an old regime in the training set is noise rather than evidence.
    ROLLING = "rolling"


def _require_positive_span(candidate: object, field_name: str) -> timedelta:
    """A strictly positive `timedelta`.

    Zero is rejected rather than treated as "no window": a zero training span produces
    folds that fit on nothing and test on data, which yields a performance number with
    no fit behind it and no error anywhere.
    """
    if not isinstance(candidate, timedelta):
        raise WalkForwardConfigError(
            f"{field_name} must be a timedelta, got {type(candidate).__name__} {candidate!r}"
        )
    if candidate <= timedelta(0):
        raise WalkForwardConfigError(f"{field_name} must be positive; got {candidate}")
    return candidate


def _require_non_negative_span(candidate: object, field_name: str) -> timedelta:
    """A `timedelta` at or above zero.

    Zero is legal here and is the honest declaration for a feature computed only from a
    bar that has already closed. It is a lie for anything a venue publishes after the
    instant it stamps, which is why the declaration is required rather than defaulted.
    """
    if not isinstance(candidate, timedelta):
        raise WalkForwardConfigError(
            f"{field_name} must be a timedelta, got {type(candidate).__name__} {candidate!r}"
        )
    if candidate < timedelta(0):
        raise WalkForwardConfigError(f"{field_name} must not be negative; got {candidate}")
    return candidate


@dataclass(frozen=True, slots=True)
class WalkForwardDeclaration:
    """The four declared horizons that size purge and embargo.

    Every field is required and none has a default, for the reason `FeatureSpec` gives:
    a default is a value somebody forgets to override, and the value they would forget is
    zero -- which is the permissive answer in all four cases and shortens both gaps.

    None of these is verifiable from inside the validation layer. An understated
    `max_feature_lookback` shortens the embargo, lets adjacent folds share information,
    and makes the harness report a stable edge that is partly the same data seen twice
    (`DATA_PIPELINE.md` section 7). The declaration is taken on trust; what this layer
    guarantees is that it is taken *at all*, and that nothing downstream can override it.
    """

    #: `FeatureSpec.label_horizon`: how long after a decision its label resolves.
    label_horizon: timedelta

    #: `FeatureSpec.availability_lag`: `available_at_utc` minus `event_time_utc`.
    availability_lag: timedelta

    #: `FeatureSpec.lookback`, maximised over every feature the strategy consumes.
    max_feature_lookback: timedelta

    #: The longest a position may stay open under this strategy.
    max_holding_horizon: timedelta

    def __post_init__(self) -> None:
        _require_positive_span(self.label_horizon, "label_horizon")
        _require_non_negative_span(self.availability_lag, "availability_lag")
        _require_non_negative_span(self.max_feature_lookback, "max_feature_lookback")
        _require_positive_span(self.max_holding_horizon, "max_holding_horizon")

    @property
    def purge(self) -> timedelta:
        """Training samples whose label resolves inside the test window.

        A label that resolves after the test window opens was partly determined by
        test-period information, and training on it is training on the answer. The
        availability lag is added rather than ignored: a label the system could not have
        read until `availability_lag` after it resolved is a label whose usable instant
        is that much later (`docs/rules/no-lookahead.md`).
        """
        return self.label_horizon + self.availability_lag

    @property
    def embargo(self) -> timedelta:
        """The floor from `BACKTEST_ENGINE.md` section 6.2, never below the purge.

        `max_feature_lookback + max_holding_horizon` is stated there as a floor and not
        as a suggestion: a strategy with a four-hour feature lookback and a six-hour
        maximum hold needs at least ten hours, and using one bar because "it is crypto,
        it is fast" reintroduces exactly the leak the embargo exists to close.

        Taking the maximum against the purge rather than the sum keeps the two gaps from
        double-counting the same horizon while making it impossible for the embargo to be
        the shorter of the two -- which is the direction that would matter.
        """
        floor = self.max_feature_lookback + self.max_holding_horizon
        return max(floor, self.purge)

    @property
    def gap(self) -> timedelta:
        """The distance between the end of a training window and the start of its test.

        Purge plus embargo, applied at the trailing edge of every training window.

        The embargo term is carried here rather than only in cross-validation because
        walk-forward training windows *contain* earlier test windows: with a contiguous
        step, fold `i + 1` trains on everything up to `gap` before its own test window,
        which includes the tail of fold `i`'s test period. Without the embargo the model
        that is about to be scored on fold `i + 1` was fitted on samples whose feature
        lookbacks still reach into the fold whose result has already been reported, and
        the two folds stop being independent draws.
        """
        return self.purge + self.embargo


@dataclass(frozen=True, slots=True)
class WalkForwardPlan:
    """A walk-forward, fully specified before any data is read.

    `train_span` means the same thing in both schemes and is not the same window: under
    `ROLLING` it is the length of every training window, and under `ANCHORED` it is the
    length of the *first* one, after which the window grows. That is why the first fold
    of an anchored plan and the first fold of the otherwise-identical rolling plan are
    the same fold -- a property worth asserting, because it is the one that breaks first
    if the two schemes ever drift into being two implementations.
    """

    scheme: WalkForwardScheme
    start_utc: datetime
    end_utc: datetime
    train_span: timedelta
    test_span: timedelta

    #: The re-fit cadence, mirroring what production would do. See the module docstring.
    step: timedelta

    declaration: WalkForwardDeclaration

    def __post_init__(self) -> None:
        if not isinstance(self.scheme, WalkForwardScheme):
            raise WalkForwardConfigError(f"scheme must be a WalkForwardScheme, got {self.scheme!r}")
        require_utc(self.start_utc, "start_utc", WalkForwardConfigError)
        require_utc(self.end_utc, "end_utc", WalkForwardConfigError)
        _require_positive_span(self.train_span, "train_span")
        _require_positive_span(self.test_span, "test_span")
        _require_positive_span(self.step, "step")
        if not isinstance(self.declaration, WalkForwardDeclaration):
            raise WalkForwardConfigError(
                f"declaration must be a WalkForwardDeclaration, got {self.declaration!r}"
            )
        if self.end_utc <= self.start_utc:
            raise WalkForwardConfigError(
                f"end_utc {self.end_utc.isoformat()} must follow "
                f"start_utc {self.start_utc.isoformat()}"
            )
        if self.step > self.test_span:
            # A step wider than the test window leaves out-of-sample periods that no
            # fold ever scores. The gaps are invisible in the fold table -- every fold
            # looks well-formed -- and the reported coverage silently excludes whatever
            # happened between them, which is the interval the strategy was live in
            # production under the fit it had just made.
            raise WalkForwardConfigError(
                f"step {self.step} exceeds test_span {self.test_span}, which leaves "
                f"out-of-sample periods no fold scores; production re-fits at least as "
                f"often as it is measured"
            )


@dataclass(frozen=True, slots=True)
class Fold:
    """One re-fit and the out-of-sample window it is scored on.

    A distinct configuration evaluated against a distinct context, so a distinct trial:
    twelve re-fits are twelve charges against the ledger, not one
    (`docs/rules/overfitting-defences.md`).
    """

    fold_index: int
    train_start_utc: datetime
    train_end_utc: datetime
    test_start_utc: datetime
    test_end_utc: datetime

    def __post_init__(self) -> None:
        require_utc(self.train_start_utc, "train_start_utc", WalkForwardConfigError)
        require_utc(self.train_end_utc, "train_end_utc", WalkForwardConfigError)
        require_utc(self.test_start_utc, "test_start_utc", WalkForwardConfigError)
        require_utc(self.test_end_utc, "test_end_utc", WalkForwardConfigError)
        if self.train_end_utc <= self.train_start_utc:
            raise WalkForwardConfigError(
                f"fold {self.fold_index}: train window is empty "
                f"({self.train_start_utc.isoformat()} .. {self.train_end_utc.isoformat()})"
            )
        if self.test_end_utc <= self.test_start_utc:
            raise WalkForwardConfigError(
                f"fold {self.fold_index}: test window is empty "
                f"({self.test_start_utc.isoformat()} .. {self.test_end_utc.isoformat()})"
            )
        if self.train_end_utc >= self.test_start_utc:
            # The check is `>=`, not `>`. A training window ending exactly at the test
            # window's first instant shares that instant's bar, and one shared bar at the
            # boundary is enough for a label computed from it to have been fitted on.
            raise WalkForwardConfigError(
                f"fold {self.fold_index}: train window ends at "
                f"{self.train_end_utc.isoformat()}, at or after the test window opens at "
                f"{self.test_start_utc.isoformat()}"
            )

    @property
    def gap(self) -> timedelta:
        """The realised separation between fit and test for this fold."""
        return self.test_start_utc - self.train_end_utc


@dataclass(frozen=True, slots=True)
class WalkForwardSchedule:
    """The folds, with the plan and the two gaps that produced them.

    The gaps are carried rather than recomputed because this object is the output schema
    `BACKTEST_ENGINE.md` section 6.2 names as one of the three places purge and embargo
    must appear. A schedule that reported only its boundaries would make the number that
    is silently wrong most often the one number a reader has to derive.
    """

    plan: WalkForwardPlan
    folds: tuple[Fold, ...]
    purge: timedelta
    embargo: timedelta

    def __post_init__(self) -> None:
        if len(self.folds) < MINIMUM_FOLDS:
            raise ScheduleRefusedError(
                f"a schedule needs at least {MINIMUM_FOLDS} folds; got {len(self.folds)}"
            )

    @property
    def fold_total(self) -> int:
        """How many re-fits this schedule specifies, and so how many trials it charges."""
        return len(self.folds)


def build_schedule(plan: WalkForwardPlan) -> WalkForwardSchedule:
    """Every fold the plan admits inside its window, in chronological order.

    One function for both schemes. The test windows, the fold count, the purge and the
    embargo do not depend on `plan.scheme`; only `train_start_utc` does.
    """
    declaration = plan.declaration
    gap = declaration.gap

    # The first test window cannot open before a full training window plus the gap has
    # elapsed. Under ANCHORED the window grows after this, but it starts at the same
    # length -- a fold fitted on less than `train_span` is a fold fitted on a different
    # strategy configuration than the one being validated.
    first_test_start = plan.start_utc + plan.train_span + gap

    folds: list[Fold] = []
    fold_index = 0
    while True:
        test_start = first_test_start + fold_index * plan.step
        test_end = test_start + plan.test_span
        if test_end > plan.end_utc:
            break
        train_end = test_start - gap
        train_start = (
            plan.start_utc
            if plan.scheme is WalkForwardScheme.ANCHORED
            else train_end - plan.train_span
        )
        folds.append(
            Fold(
                fold_index=fold_index,
                train_start_utc=train_start,
                train_end_utc=train_end,
                test_start_utc=test_start,
                test_end_utc=test_end,
            )
        )
        fold_index += 1

    if len(folds) < MINIMUM_FOLDS:
        raise ScheduleRefusedError(
            f"{plan.scheme.value} plan over "
            f"{plan.start_utc.isoformat()} .. {plan.end_utc.isoformat()} admits "
            f"{len(folds)} fold(s) at train_span={plan.train_span}, "
            f"test_span={plan.test_span}, step={plan.step}, gap={gap}; "
            f"{MINIMUM_FOLDS} is the minimum, because one fold is a single train/test "
            f"split and a single split is not evidence"
        )

    return WalkForwardSchedule(
        plan=plan,
        folds=tuple(folds),
        purge=declaration.purge,
        embargo=declaration.embargo,
    )


def schedule_table(schedule: WalkForwardSchedule) -> tuple[Mapping[str, str], ...]:
    """The fold-boundary table, as rows a fixture and a log line can both hold.

    Every field is a string: this table is compared against a committed fixture and
    pasted into a log, and a `datetime` that renders differently under two serialisers
    would make a fixture mismatch look like a scheduling change. `isoformat` on an aware
    UTC instant is the one rendering both sides agree on.

    Purge and embargo appear on every row rather than once in a header, because a row is
    what gets quoted into an incident note, and a row that has been separated from its
    header has lost the two numbers most worth checking.
    """
    return tuple(
        MappingProxyType(
            {
                "scheme": schedule.plan.scheme.value,
                "fold_index": str(fold.fold_index),
                "train_start_utc": fold.train_start_utc.isoformat(),
                "train_end_utc": fold.train_end_utc.isoformat(),
                "test_start_utc": fold.test_start_utc.isoformat(),
                "test_end_utc": fold.test_end_utc.isoformat(),
                "purge": str(schedule.purge),
                "embargo": str(schedule.embargo),
            }
        )
        for fold in schedule.folds
    )


def segment_windows(fold: Fold, segment_span: timedelta) -> tuple[tuple[datetime, datetime], ...]:
    """Partition a fold's test window into contiguous sub-windows.

    Out-of-sample decay is a slope against distance from the fit window, and a fold
    scored as one number sits at exactly one distance -- so a harness that reports one
    Sharpe per fold produces a regression whose every point shares an x, which has no
    slope at all. The decay measurement needs sub-fold resolution, and this is where it
    comes from.

    A trailing remainder shorter than `segment_span` is dropped rather than kept: a
    partial segment carries a Sharpe estimated from fewer observations than its
    neighbours, and it always sits at the far end of the distance axis, which is the end
    that moves the slope most.
    """
    _require_positive_span(segment_span, "segment_span")
    windows: list[tuple[datetime, datetime]] = []
    cursor = fold.test_start_utc
    while cursor + segment_span <= fold.test_end_utc:
        windows.append((cursor, cursor + segment_span))
        cursor += segment_span
    return tuple(windows)


def fold_by_index(folds: Sequence[Fold], fold_index: int) -> Fold:
    """The fold with this index, or a refusal naming what was asked for.

    Not `folds[fold_index]`: the folds are ordered by index today, and an observation
    that names a fold no schedule produced must not silently be attributed to whichever
    fold happens to occupy that position.
    """
    for fold in folds:
        if fold.fold_index == fold_index:
            return fold
    raise WalkForwardConfigError(
        f"no fold with index {fold_index}; the schedule holds "
        f"{sorted(fold.fold_index for fold in folds)}"
    )
