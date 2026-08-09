"""The system beat: every recurring job in this system, on one clock.

Mechanism, not policy. This package knows about fire times, run state and overlap; it
knows nothing about bars, gaps, venues or strategies, and it must not learn -- a function
here named `ingest_hour` or `reconcile` would be trading policy in the platform layer
(`docs/rules/module-boundaries.md`).

Four things it owns:

- **A declared misfire policy per job, with no default.** A missed run means three
  different things to three different jobs and there is no correct global answer, so
  `JobSpec` refuses to be constructed without one (`._policy`).
- **An idempotent claim per fire time.** `(job_id, scheduled_fire_utc)` is a primary key,
  so a restart cannot re-run a window that already ran (`._store`).
- **Overlap refused, never queued.** One task per job. A job whose previous run is still
  in flight is refused and counted; queueing it would fire it at a time nobody scheduled,
  against a window that has since moved (`._beat`).
- **A correlation scope and a span per execution**, so a scheduled run is reconstructable
  from the audit trail like any other flow (`ARCHITECTURE.md` section 11).

**The catalogue ships empty.** Ingestion, gap detection, reconciliation (#66) and audit
chain verification (#95) register their own jobs, in the pull requests that own those
functions. Declaring their cadences here would freeze schedules whose owners are
unwritten -- the same reason the event registry ships with no events.

Why this is not APScheduler, which `ARCHITECTURE.md` section 12 originally named:
ADR-0019. The short version is that `coalesce` plus `misfire_grace_time` cannot express
"replay each missed window in ascending order", its persistent job stores are
synchronous SQLAlchemy and would mean a second PostgreSQL driver, and persisting job
*definitions* is a liability when the schedule is code.

Everything not in `__all__` is private and may change without notice.
"""

from fking.platform.scheduler._beat import DEFAULT_TICK_INTERVAL_SECONDS, SchedulerBeat
from fking.platform.scheduler._catalogue import JobCatalogue
from fking.platform.scheduler._errors import (
    CatchUpBacklogTooLargeError,
    JobOutcome,
    JobRegistrationError,
    SchedulerAlreadyRunningError,
    SchedulerError,
)
from fking.platform.scheduler._policy import DueRuns, MisfirePolicy, due_fire_times
from fking.platform.scheduler._schedule import IntervalSchedule
from fking.platform.scheduler._spec import (
    JobFire,
    JobRunner,
    JobRunReport,
    JobSpec,
    TickReport,
)
from fking.platform.scheduler._store import SCHEDULER_ADVISORY_LOCK_KEY, JobRunLedger

__all__ = [
    "DEFAULT_TICK_INTERVAL_SECONDS",
    "SCHEDULER_ADVISORY_LOCK_KEY",
    "CatchUpBacklogTooLargeError",
    "DueRuns",
    "IntervalSchedule",
    "JobCatalogue",
    "JobFire",
    "JobOutcome",
    "JobRegistrationError",
    "JobRunLedger",
    "JobRunReport",
    "JobRunner",
    "JobSpec",
    "MisfirePolicy",
    "SchedulerAlreadyRunningError",
    "SchedulerBeat",
    "SchedulerError",
    "TickReport",
    "due_fire_times",
]
