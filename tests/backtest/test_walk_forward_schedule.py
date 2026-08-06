"""Fold boundaries are a committed table, and the two schemes differ in one field only.

The fixture is the contract. A fold table is the thing a validation record cites and an
incident note quotes, so a change to any boundary -- including one that looks like a
tidy-up of the gap arithmetic -- must show up as a diff against bytes somebody reviewed,
not as a number that moved.

The fixture is not left to certify itself. `test_first_and_last_fold_boundaries_are_the
_declared_arithmetic` derives the two boundaries that matter from the plan by hand, so a
fixture regenerated from a broken implementation fails here even though it matches there.
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta
from itertools import pairwise
from pathlib import Path
from typing import Final

import pytest

from fking.backtest.walkforward import (
    ScheduleRefusedError,
    WalkForwardConfigError,
    WalkForwardDeclaration,
    WalkForwardPlan,
    WalkForwardScheme,
    build_schedule,
    fold_by_index,
    schedule_table,
    segment_windows,
)
from tests.backtest.walk_forward_support import (
    DECLARATION,
    STEP,
    TEST_SPAN,
    TRAIN_SPAN,
    WINDOW_START,
    plan_for,
)

pytestmark = pytest.mark.unit

FIXTURE: Final = Path(__file__).parent / "fixtures" / "walk_forward_schedule.json"

#: The fixture plan's fold count, and so the index of its last fold.
FOLD_TOTAL: Final = 9
LAST_FOLD_INDEX: Final = FOLD_TOTAL - 1

#: A 30-day test window holds four whole weeks; the two-day tail is dropped.
WEEKLY_SEGMENTS_PER_FOLD: Final = 4


def _committed_table(scheme: WalkForwardScheme) -> list[dict[str, str]]:
    payload: dict[str, list[dict[str, str]]] = json.loads(FIXTURE.read_text(encoding="utf-8"))
    return payload[scheme.value]


@pytest.mark.parametrize("scheme", list(WalkForwardScheme), ids=lambda s: s.value)
def test_emitted_fold_table_matches_the_committed_fixture(scheme: WalkForwardScheme) -> None:
    schedule = build_schedule(plan_for(scheme))
    emitted = [dict(row) for row in schedule_table(schedule)]
    assert emitted == _committed_table(scheme)


def test_first_and_last_fold_boundaries_are_the_declared_arithmetic() -> None:
    """Derived here by hand, so the fixture cannot certify a broken derivation."""
    plan = plan_for(WalkForwardScheme.ROLLING, fold_target=9)
    schedule = build_schedule(plan)

    # gap = purge + embargo = 24h + max(4h + 6h, 24h) = 48h.
    assert schedule.purge == timedelta(hours=24)
    assert schedule.embargo == timedelta(hours=24)

    first = schedule.folds[0]
    assert first.test_start_utc == WINDOW_START + TRAIN_SPAN + timedelta(hours=48)
    assert first.train_end_utc == first.test_start_utc - timedelta(hours=48)
    assert first.train_start_utc == first.train_end_utc - TRAIN_SPAN
    assert first.test_end_utc == first.test_start_utc + TEST_SPAN

    last = schedule.folds[-1]
    assert last.fold_index == LAST_FOLD_INDEX
    assert last.test_start_utc == first.test_start_utc + LAST_FOLD_INDEX * STEP
    assert last.test_end_utc == plan.end_utc


def test_the_two_schemes_share_every_test_window_and_differ_only_in_the_fit_origin() -> None:
    anchored = build_schedule(plan_for(WalkForwardScheme.ANCHORED))
    rolling = build_schedule(plan_for(WalkForwardScheme.ROLLING))

    assert [fold.test_start_utc for fold in anchored.folds] == [
        fold.test_start_utc for fold in rolling.folds
    ]
    assert [fold.train_end_utc for fold in anchored.folds] == [
        fold.train_end_utc for fold in rolling.folds
    ]
    # The first fold is the same fold under either scheme: an anchored window starts at
    # `train_span` and grows from there, so there is nothing to grow from yet.
    assert anchored.folds[0] == rolling.folds[0]


def test_anchored_training_windows_grow_from_a_fixed_origin() -> None:
    schedule = build_schedule(plan_for(WalkForwardScheme.ANCHORED))
    spans = [fold.train_end_utc - fold.train_start_utc for fold in schedule.folds]

    assert {fold.train_start_utc for fold in schedule.folds} == {WINDOW_START}
    assert spans == sorted(spans)
    assert spans[0] == TRAIN_SPAN
    assert spans[-1] == TRAIN_SPAN + LAST_FOLD_INDEX * STEP


def test_rolling_training_windows_keep_a_constant_length() -> None:
    schedule = build_schedule(plan_for(WalkForwardScheme.ROLLING))
    spans = {fold.train_end_utc - fold.train_start_utc for fold in schedule.folds}

    assert spans == {TRAIN_SPAN}


def test_a_window_admitting_one_fold_is_refused_rather_than_reported() -> None:
    """One fold is a single train/test split, and a single split is not evidence."""
    with pytest.raises(ScheduleRefusedError, match="single train/test split"):
        build_schedule(plan_for(WalkForwardScheme.ANCHORED, fold_target=1))


def test_a_window_too_short_for_any_fold_is_refused() -> None:
    plan = WalkForwardPlan(
        scheme=WalkForwardScheme.ROLLING,
        start_utc=WINDOW_START,
        end_utc=WINDOW_START + timedelta(days=30),
        train_span=TRAIN_SPAN,
        test_span=TEST_SPAN,
        step=STEP,
        declaration=DECLARATION,
    )
    with pytest.raises(ScheduleRefusedError, match="admits 0 fold"):
        build_schedule(plan)


def test_a_step_wider_than_the_test_window_is_refused() -> None:
    """Otherwise the gaps between test windows are scored by no fold and reported by none."""
    with pytest.raises(WalkForwardConfigError, match="leaves out-of-sample periods"):
        WalkForwardPlan(
            scheme=WalkForwardScheme.ROLLING,
            start_utc=WINDOW_START,
            end_utc=WINDOW_START + timedelta(days=400),
            train_span=TRAIN_SPAN,
            test_span=timedelta(days=7),
            step=timedelta(days=30),
            declaration=DECLARATION,
        )


def test_the_embargo_floor_binds_when_the_lookback_and_hold_exceed_the_label_horizon() -> None:
    """`BACKTEST_ENGINE.md` section 6.2: the floor is a floor, not a suggestion."""
    declaration = WalkForwardDeclaration(
        label_horizon=timedelta(hours=1),
        availability_lag=timedelta(minutes=30),
        max_feature_lookback=timedelta(hours=4),
        max_holding_horizon=timedelta(hours=6),
    )
    assert declaration.purge == timedelta(hours=1, minutes=30)
    assert declaration.embargo == timedelta(hours=10)
    assert declaration.gap == timedelta(hours=11, minutes=30)


def test_the_embargo_is_never_shorter_than_the_purge() -> None:
    declaration = WalkForwardDeclaration(
        label_horizon=timedelta(days=7),
        availability_lag=timedelta(hours=2),
        max_feature_lookback=timedelta(minutes=5),
        max_holding_horizon=timedelta(minutes=15),
    )
    assert declaration.embargo == declaration.purge
    assert declaration.gap == 2 * declaration.purge


@pytest.mark.parametrize(
    ("field_name", "span"),
    [
        ("label_horizon", timedelta(0)),
        ("max_holding_horizon", timedelta(0)),
        ("availability_lag", timedelta(seconds=-1)),
        ("max_feature_lookback", timedelta(seconds=-1)),
    ],
)
def test_a_declaration_refuses_the_permissive_default_somebody_would_forget(
    field_name: str, span: timedelta
) -> None:
    horizons: dict[str, timedelta] = {
        "label_horizon": timedelta(hours=24),
        "availability_lag": timedelta(0),
        "max_feature_lookback": timedelta(hours=4),
        "max_holding_horizon": timedelta(hours=6),
    }
    horizons[field_name] = span
    with pytest.raises(WalkForwardConfigError, match=field_name):
        WalkForwardDeclaration(**horizons)


def test_a_naive_window_boundary_is_rejected_at_construction() -> None:
    with pytest.raises(WalkForwardConfigError, match="timezone-aware"):
        WalkForwardPlan(
            scheme=WalkForwardScheme.ROLLING,
            start_utc=datetime(2024, 1, 1),  # noqa: DTZ001 - the point of the test
            end_utc=WINDOW_START + timedelta(days=400),
            train_span=TRAIN_SPAN,
            test_span=TEST_SPAN,
            step=STEP,
            declaration=DECLARATION,
        )


def test_segment_windows_tile_the_test_period_and_drop_a_partial_tail() -> None:
    schedule = build_schedule(plan_for(WalkForwardScheme.ROLLING))
    fold = schedule.folds[0]

    windows = segment_windows(fold, timedelta(days=7))

    assert len(windows) == WEEKLY_SEGMENTS_PER_FOLD
    assert windows[0][0] == fold.test_start_utc
    assert all(later[0] == earlier[1] for earlier, later in pairwise(windows))
    assert windows[-1][1] <= fold.test_end_utc


def test_an_observation_naming_an_absent_fold_is_refused_rather_than_positional() -> None:
    schedule = build_schedule(plan_for(WalkForwardScheme.ROLLING))
    with pytest.raises(WalkForwardConfigError, match="no fold with index 99"):
        fold_by_index(schedule.folds, 99)
