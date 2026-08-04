"""The fire-time lattice: what is due, in which half-open interval, at what precision."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta, timezone

import pytest
from hypothesis import given
from hypothesis import strategies as st

from fking.platform.scheduler import IntervalSchedule, JobRegistrationError

pytestmark = pytest.mark.unit

ANCHOR = datetime(2026, 8, 4, 0, 0, tzinfo=UTC)
HOURLY = IntervalSchedule(period=timedelta(hours=1), anchor_utc=ANCHOR)
SIX_FIRES = 6
MINUTES_IN_THIRTY_DAYS = 43_200


def test_a_naive_anchor_is_refused() -> None:
    with pytest.raises(JobRegistrationError, match="timezone-aware"):
        IntervalSchedule(period=timedelta(hours=1), anchor_utc=datetime(2026, 8, 4))


def test_an_aware_but_non_utc_anchor_is_refused_rather_than_converted() -> None:
    baku = timezone(timedelta(hours=4))
    with pytest.raises(JobRegistrationError, match="must be UTC"):
        IntervalSchedule(
            period=timedelta(hours=1), anchor_utc=datetime(2026, 8, 4, 4, 0, tzinfo=baku)
        )


@pytest.mark.parametrize("period", [timedelta(0), timedelta(seconds=-1)])
def test_a_non_positive_period_is_refused(period: timedelta) -> None:
    with pytest.raises(JobRegistrationError, match="must be positive"):
        IntervalSchedule(period=period, anchor_utc=ANCHOR)


def test_the_interval_is_open_on_the_left_and_closed_on_the_right() -> None:
    """The cursor semantics the run ledger depends on.

    Passing the last recorded fire time as `after_utc` must not re-emit it, and a fire
    time landing exactly on `now` is due now rather than one tick later.
    """
    fires = HOURLY.fire_times_in(
        after_utc=ANCHOR + timedelta(hours=1), through_utc=ANCHOR + timedelta(hours=3)
    )
    assert fires == (ANCHOR + timedelta(hours=2), ANCHOR + timedelta(hours=3))


def test_a_schedule_has_fire_times_before_its_anchor() -> None:
    """The anchor sets the phase, not the beginning.

    A job registered with an anchor at the next round hour must still have a well-defined
    most recent fire time, or its first tick would have nothing to compute a cursor from.
    """
    assert HOURLY.index_at_or_before(ANCHOR - timedelta(minutes=30)) == -1
    assert HOURLY.fire_time_for_index(-1) == ANCHOR - timedelta(hours=1)


def test_pending_fire_count_agrees_with_the_materialised_tuple() -> None:
    after_utc = ANCHOR + timedelta(minutes=5)
    through_utc = ANCHOR + timedelta(hours=6, minutes=5)
    assert HOURLY.pending_fire_count(after_utc=after_utc, through_utc=through_utc) == SIX_FIRES
    assert len(HOURLY.fire_times_in(after_utc=after_utc, through_utc=through_utc)) == SIX_FIRES


def test_counting_a_month_of_minutes_does_not_materialise_them() -> None:
    """The reason the count is a separate method: 43,200 fire times are measured, not built."""
    minutely = IntervalSchedule(period=timedelta(minutes=1), anchor_utc=ANCHOR)
    assert (
        minutely.pending_fire_count(after_utc=ANCHOR, through_utc=ANCHOR + timedelta(days=30))
        == MINUTES_IN_THIRTY_DAYS
    )


def test_an_empty_interval_yields_nothing() -> None:
    assert HOURLY.fire_times_in(after_utc=ANCHOR, through_utc=ANCHOR) == ()
    assert HOURLY.pending_fire_count(after_utc=ANCHOR, through_utc=ANCHOR - timedelta(1)) == 0


def test_microsecond_offsets_do_not_shift_a_fire_time() -> None:
    """A fire time is a primary key. A microsecond of drift is a different row, so the
    window would be processed twice and neither run would recognise the other."""
    offset_anchor = ANCHOR + timedelta(microseconds=1)
    schedule = IntervalSchedule(period=timedelta(hours=1), anchor_utc=offset_anchor)
    fires = schedule.fire_times_in(
        after_utc=offset_anchor, through_utc=offset_anchor + timedelta(hours=8760)
    )
    assert fires[-1] == offset_anchor + timedelta(hours=8760)
    assert all(fire.microsecond == 1 for fire in fires[:5])


_MOMENTS = st.datetimes(
    min_value=datetime(2020, 1, 1),
    max_value=datetime(2040, 1, 1),
    timezones=st.just(UTC),
)
_PERIODS = st.sampled_from(
    [
        timedelta(seconds=1),
        timedelta(minutes=1),
        timedelta(minutes=5),
        timedelta(hours=1),
        timedelta(days=1),
    ]
)


@pytest.mark.property
@given(anchor_utc=_MOMENTS, period=_PERIODS, moment_utc=_MOMENTS)
def test_the_index_brackets_its_moment(
    anchor_utc: datetime, period: timedelta, moment_utc: datetime
) -> None:
    """`fire(index) <= moment < fire(index + 1)`, for every anchor and every moment.

    The invariant everything else rests on. An off-by-one here makes a job fire a period
    early or a period late, forever, with nothing that looks wrong.
    """
    schedule = IntervalSchedule(period=period, anchor_utc=anchor_utc)
    index = schedule.index_at_or_before(moment_utc)
    assert schedule.fire_time_for_index(index) <= moment_utc
    assert schedule.fire_time_for_index(index + 1) > moment_utc


@pytest.mark.property
@given(
    anchor_utc=_MOMENTS,
    period=_PERIODS,
    after_utc=_MOMENTS,
    # In periods rather than in wall-clock time. A fixed duration spans 864,000 fire
    # times at a one-second cadence and ten at a daily one, so the test would be
    # measuring the tuple builder rather than the arithmetic -- and would blow the
    # Hypothesis deadline on exactly the periods it covers least interestingly.
    elapsed_periods=st.integers(min_value=0, max_value=50),
)
def test_fire_times_are_strictly_ascending_and_inside_the_interval(
    anchor_utc: datetime, period: timedelta, after_utc: datetime, elapsed_periods: int
) -> None:
    schedule = IntervalSchedule(period=period, anchor_utc=anchor_utc)
    through_utc = after_utc + period * elapsed_periods
    fires = schedule.fire_times_in(after_utc=after_utc, through_utc=through_utc)

    assert list(fires) == sorted(set(fires))
    assert all(after_utc < fire <= through_utc for fire in fires)
    assert len(fires) == schedule.pending_fire_count(after_utc=after_utc, through_utc=through_utc)
