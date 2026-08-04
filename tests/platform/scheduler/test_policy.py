"""Missed-run semantics: the three answers, and why they are not interchangeable.

The six-hour outage below is the case this whole package exists for, and it is a pure
function call rather than a suite that waits six hours or restarts a process.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest
from hypothesis import given
from hypothesis import strategies as st

from fking.platform.scheduler import (
    CatchUpBacklogTooLargeError,
    IntervalSchedule,
    MisfirePolicy,
    due_fire_times,
)

pytestmark = pytest.mark.unit

ANCHOR = datetime(2026, 8, 4, 0, 0, tzinfo=UTC)
HOURLY = IntervalSchedule(period=timedelta(hours=1), anchor_utc=ANCHOR)

# The outage: the beat last ran at 02:00 and comes back at 08:00, so 03:00 through 08:00
# are missed -- six fires.
LAST_FIRE = ANCHOR + timedelta(hours=2)
BACK_UP_AT = ANCHOR + timedelta(hours=8)
MISSED_FIRES = 6

# A one-minute job down for thirty days. The backlog the declared bound exists to refuse.
MINUTES_IN_THIRTY_DAYS = 43_200


@pytest.mark.parametrize(
    ("misfire_policy", "expected_run_count"),
    [
        (MisfirePolicy.SKIP_TO_LATEST, 1),
        (MisfirePolicy.RUN_EVERY_MISSED, 6),
        (MisfirePolicy.RUN_NOW, 1),
    ],
    ids=lambda argument: getattr(argument, "value", str(argument)),
)
def test_a_six_hour_outage_produces_one_six_and_one(
    misfire_policy: MisfirePolicy, expected_run_count: int
) -> None:
    due = due_fire_times(
        schedule=HOURLY,
        misfire_policy=misfire_policy,
        last_fire_utc=LAST_FIRE,
        now_utc=BACK_UP_AT,
        max_catch_up_runs=24,
    )
    assert len(due.fire_times) == expected_run_count
    assert due.missed_fire_count == MISSED_FIRES
    assert due.is_catch_up


def test_run_every_missed_replays_each_window_in_ascending_order() -> None:
    """Skipping five leaves five holes that no later run will notice, because "the last
    run succeeded" is the only state most catch-up logic checks."""
    due = due_fire_times(
        schedule=HOURLY,
        misfire_policy=MisfirePolicy.RUN_EVERY_MISSED,
        last_fire_utc=LAST_FIRE,
        now_utc=BACK_UP_AT,
        max_catch_up_runs=24,
    )
    assert due.fire_times == tuple(ANCHOR + timedelta(hours=hour) for hour in range(3, 9))


def test_skip_to_latest_is_stamped_at_the_most_recent_missed_fire() -> None:
    due = due_fire_times(
        schedule=HOURLY,
        misfire_policy=MisfirePolicy.SKIP_TO_LATEST,
        last_fire_utc=LAST_FIRE,
        now_utc=BACK_UP_AT + timedelta(minutes=17),
        max_catch_up_runs=1,
    )
    assert due.fire_times == (BACK_UP_AT,)


def test_run_now_is_stamped_now_rather_than_at_a_historical_fire() -> None:
    """A reconciliation labelled 03:00 that read the exchange at 08:17 is a record that
    will be misread later, so the stamp is the instant the run actually observes."""
    now_utc = BACK_UP_AT + timedelta(minutes=17)
    due = due_fire_times(
        schedule=HOURLY,
        misfire_policy=MisfirePolicy.RUN_NOW,
        last_fire_utc=LAST_FIRE,
        now_utc=now_utc,
        max_catch_up_runs=1,
    )
    assert due.fire_times == (now_utc,)


def test_nothing_is_due_between_fires() -> None:
    due = due_fire_times(
        schedule=HOURLY,
        misfire_policy=MisfirePolicy.RUN_EVERY_MISSED,
        last_fire_utc=LAST_FIRE,
        now_utc=LAST_FIRE + timedelta(minutes=59),
        max_catch_up_runs=24,
    )
    assert due.fire_times == ()
    assert not due.is_catch_up


def test_a_single_due_fire_is_not_a_catch_up() -> None:
    due = due_fire_times(
        schedule=HOURLY,
        misfire_policy=MisfirePolicy.RUN_EVERY_MISSED,
        last_fire_utc=LAST_FIRE,
        now_utc=LAST_FIRE + timedelta(hours=1),
        max_catch_up_runs=24,
    )
    assert due.missed_fire_count == 1
    assert not due.is_catch_up


def test_a_job_that_has_never_run_starts_at_the_most_recent_fire() -> None:
    """Not at the anchor, and not one period from now: a newly registered job works
    immediately, and replays no history it was never responsible for."""
    due = due_fire_times(
        schedule=HOURLY,
        misfire_policy=MisfirePolicy.RUN_EVERY_MISSED,
        last_fire_utc=None,
        now_utc=ANCHOR + timedelta(hours=500, minutes=20),
        max_catch_up_runs=24,
    )
    assert due.fire_times == (ANCHOR + timedelta(hours=500),)


def test_a_backlog_over_the_declared_bound_refuses_rather_than_truncating() -> None:
    minutely = IntervalSchedule(period=timedelta(minutes=1), anchor_utc=ANCHOR)
    with pytest.raises(CatchUpBacklogTooLargeError, match="43200 missed fire times"):
        due_fire_times(
            schedule=minutely,
            misfire_policy=MisfirePolicy.RUN_EVERY_MISSED,
            last_fire_utc=ANCHOR,
            now_utc=ANCHOR + timedelta(days=30),
            max_catch_up_runs=120,
        )


@pytest.mark.parametrize("misfire_policy", [MisfirePolicy.SKIP_TO_LATEST, MisfirePolicy.RUN_NOW])
def test_the_single_run_policies_ignore_the_backlog_bound(
    misfire_policy: MisfirePolicy,
) -> None:
    """They can only ever produce one run, so a month of missed minutes is not a backlog
    for them -- which is why `JobSpec` refuses to let them declare a bound above one."""
    minutely = IntervalSchedule(period=timedelta(minutes=1), anchor_utc=ANCHOR)
    due = due_fire_times(
        schedule=minutely,
        misfire_policy=misfire_policy,
        last_fire_utc=ANCHOR,
        now_utc=ANCHOR + timedelta(days=30),
        max_catch_up_runs=1,
    )
    assert len(due.fire_times) == 1
    assert due.missed_fire_count == MINUTES_IN_THIRTY_DAYS


@pytest.mark.property
@given(
    elapsed_periods=st.integers(min_value=0, max_value=200),
    offset_seconds=st.integers(min_value=0, max_value=3599),
    misfire_policy=st.sampled_from(MisfirePolicy),
)
def test_due_fire_times_never_leave_the_cursor_window(
    elapsed_periods: int, offset_seconds: int, misfire_policy: MisfirePolicy
) -> None:
    """Whatever the policy: ascending, never at or before the cursor, never after now,
    and never more runs than the declared bound."""
    now_utc = LAST_FIRE + timedelta(hours=elapsed_periods, seconds=offset_seconds)
    max_catch_up_runs = 1 if misfire_policy is not MisfirePolicy.RUN_EVERY_MISSED else 1_000
    due = due_fire_times(
        schedule=HOURLY,
        misfire_policy=misfire_policy,
        last_fire_utc=LAST_FIRE,
        now_utc=now_utc,
        max_catch_up_runs=max_catch_up_runs,
    )
    assert list(due.fire_times) == sorted(set(due.fire_times))
    assert all(LAST_FIRE < fire <= now_utc for fire in due.fire_times)
    assert len(due.fire_times) <= max_catch_up_runs
    assert (due.missed_fire_count == 0) == (due.fire_times == ())
