"""The run ledger: which fire times have been claimed, and how each one ended.

One table, `scheduler_job_run`, whose primary key is `(job_id, scheduled_fire_utc)`. That
key is the whole durability story:

**A claim is `INSERT … ON CONFLICT DO NOTHING RETURNING 1`.** No row returned means this
fire time has already been claimed -- by an earlier boot, by a run still in flight, or by
a second process -- and the beat does not run it. "A restart does not re-fire jobs that
already ran" is therefore a property of a unique index rather than of a code path
somebody has to keep correct (`docs/rules/idempotency.md`).

**The cursor is `max(scheduled_fire_utc)` over every claimed run, whatever its outcome.**
A fire time is consumed once. That is a deliberate choice with a cost worth naming: a run
that *failed* is not automatically retried, so the window it covered stays uncovered
until somebody backfills it. The alternative -- a cursor over successful runs only --
retries a permanently failing window on every tick forever, which converts one broken
window into a job that never advances and a log nobody can read. Recording the failure
and stopping is the same trade this system makes everywhere else
(`docs/rules/error-handling.md`).

**Unfinished rows are swept at boot, not on a timeout.** A row with no `finished_at_utc`
means the process holding it died. The sweep is only sound because one beat runs at a
time, and that is enforced rather than assumed: `single_instance()` takes a session-level
advisory lock, so a second beat over the same database refuses to start instead of
marking the first one's live runs abandoned.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from datetime import datetime
from typing import Final
from uuid import UUID

import sqlalchemy as sa
from sqlalchemy.ext.asyncio import AsyncEngine

from fking.platform.scheduler._errors import JobOutcome, SchedulerAlreadyRunningError

__all__ = ["SCHEDULER_ADVISORY_LOCK_KEY", "JobRunLedger"]

# A fixed key reserved for the scheduler's single-instance lock. Arbitrary, but it must
# never collide with another advisory lock in this database -- PostgreSQL's advisory lock
# space is global and untyped, so two subsystems picking the same number would deadlock
# each other for reasons neither one can see. Keys in use: 5510477 (audit chain,
# `docs/rules/append-only-audit.md`), 8812331 (trial ledger).
SCHEDULER_ADVISORY_LOCK_KEY: Final[int] = 7714903

_CLAIM: Final[sa.TextClause] = sa.text(
    """
    INSERT INTO scheduler_job_run (
        job_id, scheduled_fire_utc, correlation_id, is_catch_up
    )
    VALUES (:job_id, :scheduled_fire_utc, :correlation_id, :is_catch_up)
    ON CONFLICT (job_id, scheduled_fire_utc) DO NOTHING
    RETURNING 1
    """
)

# `finished_at_utc IS NULL` is not belt and braces. It is how a second writer learns it
# lost -- by updating zero rows rather than by overwriting a verdict -- and the
# `scheduler_job_run_completion_only` trigger from 0013 refuses the same rewrite from the
# database side, in the order `docs/rules/append-only-audit.md` argues for.
_FINISH: Final[sa.TextClause] = sa.text(
    """
    UPDATE scheduler_job_run
       SET finished_at_utc = clock_timestamp(),
           outcome         = :outcome,
           failure_reason  = :failure_reason
     WHERE job_id = :job_id
       AND scheduled_fire_utc = :scheduled_fire_utc
       AND finished_at_utc IS NULL
    RETURNING 1
    """
)

_ABANDON_UNFINISHED: Final[sa.TextClause] = sa.text(
    """
    UPDATE scheduler_job_run
       SET finished_at_utc = clock_timestamp(),
           outcome         = :outcome,
           failure_reason  = :failure_reason
     WHERE finished_at_utc IS NULL
    RETURNING job_id, scheduled_fire_utc
    """
)

_ABANDONED_REASON: Final[str] = (
    "claimed by a process that is no longer running; swept at scheduler boot"
)


class JobRunLedger:
    """Reads and writes `scheduler_job_run`, one short transaction at a time.

    Holds an engine rather than a connection: a beat runs for the life of the process,
    and a caller handed one connection would either keep a transaction open for that
    whole time -- pinning a snapshot and blocking vacuum -- or commit inside somebody
    else's unit of work.
    """

    __slots__ = ("_engine",)

    def __init__(self, engine: AsyncEngine) -> None:
        self._engine = engine

    @asynccontextmanager
    async def single_instance(self) -> AsyncIterator[None]:
        """Hold the scheduler's advisory lock for the duration of the block.

        Session-level rather than transaction-level, on a connection pinned to
        `AUTOCOMMIT`. Transaction-level would release at the first commit, which is every
        claim; and an ordinary connection left open for the process lifetime sits
        idle-in-transaction, which holds back the vacuum horizon for as long as the
        scheduler runs.

        The lock also releases itself if the process dies without unlocking, because
        PostgreSQL drops session locks when the backend goes away. That is what makes a
        crashed beat recoverable by restarting it rather than by hand.
        """
        async with self._engine.connect() as connection:
            autocommitting = await connection.execution_options(isolation_level="AUTOCOMMIT")
            acquired = (
                await autocommitting.execute(
                    sa.text("SELECT pg_try_advisory_lock(:lock_key)"),
                    {"lock_key": SCHEDULER_ADVISORY_LOCK_KEY},
                )
            ).scalar_one()
            if not acquired:
                raise SchedulerAlreadyRunningError(
                    f"advisory lock {SCHEDULER_ADVISORY_LOCK_KEY} is held by another "
                    f"session, so a scheduler is already running against this database. "
                    f"Two beats would each sweep the other's in-flight runs to abandoned"
                )
            try:
                yield
            finally:
                await autocommitting.execute(
                    sa.text("SELECT pg_advisory_unlock(:lock_key)"),
                    {"lock_key": SCHEDULER_ADVISORY_LOCK_KEY},
                )

    async def last_fire_utc(self, job_id: str) -> datetime | None:
        """The most recent fire time claimed for `job_id`, or `None` if it has never run."""
        async with self._engine.connect() as connection:
            row = (
                await connection.execute(
                    sa.text(
                        "SELECT max(scheduled_fire_utc) AS last_fire_utc "
                        "FROM scheduler_job_run WHERE job_id = :job_id"
                    ),
                    {"job_id": job_id},
                )
            ).one()
        last_fire_utc: datetime | None = row.last_fire_utc
        return last_fire_utc

    async def claim(
        self,
        *,
        job_id: str,
        fire_time_utc: datetime,
        correlation_id: UUID,
        is_catch_up: bool,
    ) -> bool:
        """Reserve one fire time. `False` means somebody else already has it."""
        async with self._engine.begin() as connection:
            claimed = (
                await connection.execute(
                    _CLAIM,
                    {
                        "job_id": job_id,
                        "scheduled_fire_utc": fire_time_utc,
                        "correlation_id": correlation_id,
                        "is_catch_up": is_catch_up,
                    },
                )
            ).first()
        return claimed is not None

    async def finish(
        self,
        *,
        job_id: str,
        fire_time_utc: datetime,
        outcome: JobOutcome,
        failure_reason: str | None = None,
    ) -> bool:
        """Close a claimed run. `False` means it was already closed."""
        async with self._engine.begin() as connection:
            closed = (
                await connection.execute(
                    _FINISH,
                    {
                        "job_id": job_id,
                        "scheduled_fire_utc": fire_time_utc,
                        "outcome": outcome.value,
                        "failure_reason": failure_reason,
                    },
                )
            ).first()
        return closed is not None

    async def abandon_unfinished(self) -> tuple[tuple[str, datetime], ...]:
        """Close every run left open by a previous process, and report what was swept.

        Returned rather than only counted: a beat that abandons runs at every boot is a
        beat that is being restarted mid-job, and knowing *which* job and *which* window
        is the difference between an investigation and a shrug.
        """
        async with self._engine.begin() as connection:
            swept = (
                await connection.execute(
                    _ABANDON_UNFINISHED,
                    {
                        "outcome": JobOutcome.ABANDONED.value,
                        "failure_reason": _ABANDONED_REASON,
                    },
                )
            ).all()
        return tuple((str(row[0]), row[1]) for row in swept)
