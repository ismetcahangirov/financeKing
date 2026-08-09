"""When a job is due: an anchored interval, computed as integer arithmetic.

A schedule here is `anchor_utc + k * period` for integer `k`, and nothing else. That is
deliberately less than cron can express, and it is exactly what every recurring job this
system has needs -- hourly ingestion, gap detection every few minutes, reconciliation on
a timer, a daily evaluation cycle at a fixed hour, which is `period=1 day` with the
anchor at that hour. A cron parser with no caller would be a dependency adopted for a
syntax nobody had asked for (`CLAUDE.md` section 3).

**The anchor is what makes a fire time an identity rather than a moment.** A schedule
expressed as "every hour from whenever the process started" produces a different set of
fire times on every restart, so the same window is ingested twice under two different
names and neither run can be recognised as a duplicate of the other. Anchored, the fire
times of an hourly job are the same instants in every process that has ever run it,
which is what lets `(job_id, fire_time_utc)` be a durable idempotency key
(`docs/rules/idempotency.md`).

The arithmetic is `timedelta // timedelta`, which is exact integer floor division, rather
than `timedelta / timedelta`, which is a float. Over a multi-year anchor offset at
microsecond resolution a float quotient loses the low bits, and the symptom is a fire
time landing a microsecond either side of the boundary -- which is a *different* primary
key, so the run that already happened is not recognised and the window is processed
twice.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

from fking.platform.scheduler._errors import JobRegistrationError

__all__ = ["IntervalSchedule"]


@dataclass(frozen=True, slots=True, kw_only=True)
class IntervalSchedule:
    """Fires at `anchor_utc + k * period` for every integer `k`.

    Keyword-only and fully required. There is no default period -- a default would be a
    cadence somebody forgot to choose, and the one they would forget is the one that runs
    the most often.
    """

    period: timedelta
    anchor_utc: datetime

    def __post_init__(self) -> None:
        if self.period <= timedelta(0):
            raise JobRegistrationError(
                f"period must be positive; got {self.period}. A non-positive period has no "
                f"next fire time, so the job would either never run or run without bound"
            )
        if self.anchor_utc.tzinfo is None or self.anchor_utc.utcoffset() is None:
            raise JobRegistrationError(
                f"anchor_utc must be timezone-aware; got naive {self.anchor_utc!r}"
            )
        if self.anchor_utc.utcoffset() != UTC.utcoffset(None):
            raise JobRegistrationError(
                f"anchor_utc must be UTC; got offset {self.anchor_utc.utcoffset()!r}. An "
                f"anchor in a local zone reintroduces DST into a market that has no session "
                f"boundary to make the error obvious"
            )

    def fire_time_for_index(self, index: int) -> datetime:
        """The `index`-th fire time, counting from the anchor. Negative indices precede it."""
        return self.anchor_utc + index * self.period

    def index_at_or_before(self, moment_utc: datetime) -> int:
        """The largest `k` whose fire time is at or before `moment_utc`.

        Negative when `moment_utc` precedes the anchor, which is a legitimate state: a
        schedule anchored at the next round hour has not fired yet.
        """
        return (moment_utc - self.anchor_utc) // self.period

    def pending_fire_count(self, *, after_utc: datetime, through_utc: datetime) -> int:
        """How many fire times fall in `(after_utc, through_utc]`.

        Separate from `fire_times_in` so a caller can refuse a backlog before
        materialising it. A one-minute job that was down for a month has 43,200 pending
        fire times, and building that tuple in order to measure it is the shape of
        failure this method exists to avoid.
        """
        if through_utc <= after_utc:
            return 0
        first_index = self.index_at_or_before(after_utc) + 1
        last_index = self.index_at_or_before(through_utc)
        return max(0, last_index - first_index + 1)

    def fire_times_in(self, *, after_utc: datetime, through_utc: datetime) -> tuple[datetime, ...]:
        """Every fire time in `(after_utc, through_utc]`, ascending.

        Half-open on the left and closed on the right, which is the interval that makes
        "the last fire time we recorded" a usable cursor: passing it as `after_utc`
        cannot re-emit it, and a fire time landing exactly on `now` is due now rather
        than one tick later.
        """
        if through_utc <= after_utc:
            return ()
        first_index = self.index_at_or_before(after_utc) + 1
        last_index = self.index_at_or_before(through_utc)
        return tuple(
            self.fire_time_for_index(index) for index in range(first_index, last_index + 1)
        )
