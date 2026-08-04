"""What a job must declare before the beat will run it, and what a tick reports back.

Every field on `JobSpec` is required and keyword-only. That is the mechanism behind this
issue's first acceptance criterion rather than a style preference: omitting
`misfire_policy` is a `TypeError` naming the parameter, at import, before the process
does anything -- and a default would be a decision somebody skipped, silently, on the one
question that has three defensible answers (`fking.platform.scheduler._policy`).

`JobFire.fire_time_utc` is what the job is *for*, never the instant it happened to start.
An hourly ingestion replaying the 04:00 window must ingest 04:00, not "the last hour",
and a job that reads a clock instead of its fire time cannot be replayed at all
(`.claude/rules/time-and-timezones.md`).
"""

from __future__ import annotations

import re
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import datetime
from typing import Final
from uuid import UUID

from fking.platform.scheduler._errors import JobOutcome, JobRegistrationError
from fking.platform.scheduler._policy import MisfirePolicy
from fking.platform.scheduler._schedule import IntervalSchedule

__all__ = ["JobFire", "JobRunReport", "JobRunner", "JobSpec", "TickReport"]

# `subsystem.operation`, lower snake case. The same shape as a span name, and for the
# same reason: a job id is a Prometheus label and a log field, and both are queried by
# strings written elsewhere. It is also the identity half of the run ledger's primary
# key, so renaming a job orphans its history rather than moving it.
_JOB_ID: Final[re.Pattern[str]] = re.compile(r"\A[a-z][a-z0-9_]*(\.[a-z][a-z0-9_]*)*\Z")

_SINGLE_RUN_POLICIES: Final[frozenset[MisfirePolicy]] = frozenset(
    {MisfirePolicy.SKIP_TO_LATEST, MisfirePolicy.RUN_NOW}
)


@dataclass(frozen=True, slots=True)
class JobFire:
    """One scheduled execution, handed to the job callable.

    `is_catch_up` is passed rather than inferred by the job from a clock comparison. A
    job that behaves differently when it is behind -- skipping a notification, widening a
    query window -- must be able to say so, and the beat is the only component that knows
    how many fires elapsed.
    """

    job_id: str
    fire_time_utc: datetime
    correlation_id: UUID
    is_catch_up: bool


JobRunner = Callable[[JobFire], Awaitable[None]]


@dataclass(frozen=True, slots=True, kw_only=True)
class JobSpec:
    """A registrable recurring job."""

    job_id: str
    schedule: IntervalSchedule
    misfire_policy: MisfirePolicy
    max_catch_up_runs: int
    run: JobRunner

    def __post_init__(self) -> None:
        if _JOB_ID.fullmatch(self.job_id) is None:
            raise JobRegistrationError(
                f"job_id {self.job_id!r} is not subsystem.operation in lower snake case; a "
                f"job id is a metric label, a log field and half of the run ledger's "
                f"primary key, so it is frozen once the job has run"
            )
        if self.max_catch_up_runs < 1:
            raise JobRegistrationError(
                f"{self.job_id} declares max_catch_up_runs={self.max_catch_up_runs}; a job "
                f"that may replay no fire times can never recover from an outage"
            )
        if self.misfire_policy in _SINGLE_RUN_POLICIES and self.max_catch_up_runs != 1:
            raise JobRegistrationError(
                f"{self.job_id} declares misfire_policy={self.misfire_policy} with "
                f"max_catch_up_runs={self.max_catch_up_runs}. That policy produces exactly "
                f"one run however many fires were missed, so any other bound states a "
                f"tolerance that can never be reached and reads as if it could"
            )


@dataclass(frozen=True, slots=True)
class JobRunReport:
    """What one execution did. Returned rather than only logged, so a test asserts on a
    value instead of scraping a log line."""

    job_id: str
    fire_time_utc: datetime
    outcome: JobOutcome
    correlation_id: UUID


@dataclass(frozen=True, slots=True)
class TickReport:
    """What one tick of the beat admitted, and what it turned away.

    A tick reports on *admission*, not on outcomes: it claims fire times and starts one
    task per job, and the outcomes arrive later through `SchedulerBeat.drain`. Reporting
    outcomes here would mean awaiting every job inside the tick, which is the behaviour
    that makes a seventy-minute job stall everything else.

    `refused_overlap` and `already_claimed` are separate fields because they are separate
    conditions with separate responses. An overlap means the job is slower than its
    cadence, and either the cadence or the job has to change. An already-claimed fire
    time means another process or an earlier boot got there first, which is the
    idempotency machinery working as designed.
    """

    claimed: tuple[JobFire, ...]
    refused_overlap: tuple[str, ...]
    already_claimed: tuple[str, ...]
