"""The scheduler's own error taxonomy, and the closed set of run outcomes.

Outcomes are a closed enum rather than free text for two reasons that point the same
way. They are a Prometheus label, and an open label string is unbounded cardinality
during exactly the incident that generated the variety. And they are a `CHECK`
constraint on `scheduler_job_run.outcome`, so a value nobody declared is refused by the
database rather than accumulated in it.

`SchedulerError` deliberately does not inherit `FkingError`. The beat catches
`FkingError` around a *job body* -- that is the taxonomy a job raises from -- and
letting a scheduler-internal failure be caught by the same clause would record the
scheduler's own bug as a failed job and keep going.
"""

from __future__ import annotations

from enum import StrEnum


class SchedulerError(RuntimeError):
    """Base for every error this package raises deliberately."""


class JobRegistrationError(SchedulerError):
    """A job declaration is incomplete, malformed, or collides with one already registered."""


class CatchUpBacklogTooLargeError(SchedulerError):
    """More missed fire times are due than the job declared it would replay.

    Raised rather than truncated. Replaying 43,200 windows because a one-minute job was
    down for a month, and silently dropping all but the last, are both worse than
    stopping: the first floods every downstream consumer, and the second leaves holes
    that no later run will notice because "the last run succeeded" is the only state
    most catch-up logic checks.
    """


class SchedulerAlreadyRunningError(SchedulerError):
    """Another process already holds the scheduler's advisory lock.

    Two beats over one database is not a capacity increase. They would race on every
    claim -- the primary key would keep the *executions* correct -- but the boot sweep
    that marks unfinished runs abandoned assumes nothing else is mid-run, and with a
    second process that assumption is false in the direction that loses a record.
    """


class JobOutcome(StrEnum):
    """How a claimed run ended. Written to `scheduler_job_run.outcome`."""

    SUCCEEDED = "succeeded"
    """The job returned."""

    FAILED = "failed"
    """The job raised a member of the `FkingError` taxonomy. Recorded, and the beat
    continues: one broken job must not silence reconciliation. Anything outside that
    taxonomy is not recorded here, because it propagates and stops the process."""

    ABANDONED = "abandoned"
    """Claimed by a previous process that is no longer running. Written by the boot
    sweep, never by a job. Distinct from `failed` on purpose -- a job that failed told us
    something about the world, and a job that was abandoned tells us only that the
    process died, which is a different investigation."""
