"""Over generated schedules: no fold ever fits on data at or after its own test window.

Example-based tests confirm the boundaries somebody thought to write down. The boundary
that breaks is the one nobody wrote down -- a gap arithmetic that is correct for a
90-day training window and off by one step for a 91-day one, or a rolling window whose
start walks backwards past the plan's own origin once the training span exceeds the
distance to the first test.

The invariant asserted here is the one whose violation does not fail: a training window
that reaches into its test period produces a better Sharpe, a cleaner equity curve and no
error anywhere (`.claude/rules/no-lookahead.md`).
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from itertools import pairwise

import pytest
from hypothesis import assume, given
from hypothesis import strategies as st

from fking.backtest.walkforward import (
    ScheduleRefusedError,
    WalkForwardDeclaration,
    WalkForwardPlan,
    WalkForwardSchedule,
    WalkForwardScheme,
    build_schedule,
)

pytestmark = [pytest.mark.property, pytest.mark.unit]

ORIGIN = datetime(2024, 1, 1, tzinfo=UTC)

positive_hours = st.integers(min_value=1, max_value=2_400).map(lambda h: timedelta(hours=h))
non_negative_hours = st.integers(min_value=0, max_value=240).map(lambda h: timedelta(hours=h))

declarations = st.builds(
    WalkForwardDeclaration,
    label_horizon=positive_hours,
    availability_lag=non_negative_hours,
    max_feature_lookback=non_negative_hours,
    max_holding_horizon=positive_hours,
)


@st.composite
def plans(draw: st.DrawFn) -> WalkForwardPlan:
    """Plans whose window is often long enough for a schedule to be admitted.

    `step` is drawn from the range bounded by `test_span` rather than filtered against
    it: a wider step is refused at construction, and a strategy that mostly generates
    rejected configurations would test the constructor rather than the scheduler.
    """
    test_days = draw(st.integers(min_value=1, max_value=120))
    return WalkForwardPlan(
        scheme=draw(st.sampled_from(WalkForwardScheme)),
        start_utc=ORIGIN,
        end_utc=ORIGIN + timedelta(days=draw(st.integers(min_value=1, max_value=2_000))),
        train_span=timedelta(days=draw(st.integers(min_value=1, max_value=400))),
        test_span=timedelta(days=test_days),
        step=timedelta(days=draw(st.integers(min_value=1, max_value=test_days))),
        declaration=draw(declarations),
    )


def _schedule_or_discard(plan: WalkForwardPlan) -> WalkForwardSchedule:
    """A window too short for two folds is a refusal by design, not a counterexample.

    `assume(False)` discards the example rather than failing it, and never returns -- so
    every caller below can treat the schedule as present without a `None` branch that
    would be dead code in the type checker's eyes as well as at runtime.
    """
    try:
        return build_schedule(plan)
    except ScheduleRefusedError:
        assume(False)


@given(plan=plans())
def test_every_fold_fits_strictly_before_its_test_window_with_the_embargo_applied(
    plan: WalkForwardPlan,
) -> None:
    schedule = _schedule_or_discard(plan)

    declaration = plan.declaration
    assert schedule.purge == declaration.purge
    assert schedule.embargo == declaration.embargo

    for fold in schedule.folds:
        assert fold.train_start_utc < fold.train_end_utc
        assert fold.train_end_utc < fold.test_start_utc
        assert fold.test_start_utc < fold.test_end_utc

        # The separation is exactly purge + embargo, and never merely "some gap": an
        # implementation that applied the purge alone would still satisfy a `<` check.
        assert fold.gap == declaration.purge + declaration.embargo
        assert fold.gap >= declaration.embargo


@given(plan=plans())
def test_folds_are_ordered_stepwise_and_stay_inside_the_declared_window(
    plan: WalkForwardPlan,
) -> None:
    schedule = _schedule_or_discard(plan)

    for earlier, later in pairwise(schedule.folds):
        assert later.fold_index == earlier.fold_index + 1
        assert later.test_start_utc - earlier.test_start_utc == plan.step

    for fold in schedule.folds:
        assert fold.train_start_utc >= plan.start_utc
        assert fold.test_end_utc <= plan.end_utc


@given(plan=plans())
def test_the_scheme_moves_the_fit_origin_and_nothing_else(plan: WalkForwardPlan) -> None:
    """The two schemes are one scheduler, which is a claim only a property can carry."""
    schedule = _schedule_or_discard(plan)

    for fold in schedule.folds:
        if plan.scheme is WalkForwardScheme.ANCHORED:
            assert fold.train_start_utc == plan.start_utc
        else:
            assert fold.train_end_utc - fold.train_start_utc == plan.train_span
