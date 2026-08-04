"""The beat: one tick, one admission decision per job, one task per job.

Three properties decide the shape of this file.

**A tick admits, it does not execute.** `tick` resolves what is due, claims it in the run
ledger, and starts one task per job. It does not await the job. If it did, a job that
takes seventy minutes on an hourly cadence would stall every other job for seventy
minutes -- and the overlap this issue is about would be invisible, because there would
never be two runs of anything at once.

**One task per job, not one per fire time.** A `RUN_EVERY_MISSED` catch-up owes six
executions in ascending order, and six concurrent tasks lose the order that policy exists
to preserve. One task iterating its own fire times gives the ordering and the
`max_instances=1` property from the same mechanism: a job with a task in flight is
refused, and refusal is recorded rather than queued. A queued run is a run that fires at
a time nobody scheduled, against a window that has since moved.

**Catch-up and steady state are one code path.** During normal running exactly one fire
time is due per tick and all three misfire policies agree; after an outage they diverge.
Two code paths would mean the outage path is the one never exercised, which is the one
that has to work at 03:00 after a restart.

Error handling follows `.claude/rules/error-handling.md` exactly. A job raising a member
of the `FkingError` taxonomy is a failure this system raises on purpose: it is recorded
against its fire time and the beat continues, because one broken job must not silence
reconciliation. Anything else propagates out of the task, is re-raised by the next
`tick`, and stops the process -- an unknown state is not something to keep scheduling
through. Nothing here catches `Exception`.
"""

from __future__ import annotations

import asyncio
import time
from collections.abc import Callable
from datetime import UTC, datetime
from typing import Final, NoReturn
from uuid import uuid4

from fking.platform.correlation import BOOT, correlation_scope
from fking.platform.errors import FkingError
from fking.platform.logging import get_logger
from fking.platform.scheduler._catalogue import JobCatalogue
from fking.platform.scheduler._errors import JobOutcome
from fking.platform.scheduler._policy import due_fire_times
from fking.platform.scheduler._spec import JobFire, JobRunReport, JobSpec, TickReport
from fking.platform.scheduler._store import JobRunLedger
from fking.platform.telemetry import counter, traced
from fking.platform.telemetry._registry import SCHEDULER_JOB_RUNS, SCHEDULER_OVERLAPS_REFUSED

__all__ = ["DEFAULT_TICK_INTERVAL_SECONDS", "SchedulerBeat"]

_LOG: Final = get_logger(__name__)

# Fifteen seconds. The beat's resolution, not any job's cadence: a job anchored at :00
# fires within one tick of its fire time, and a tick that finds nothing due costs one
# `max(scheduled_fire_utc)` per registered job. Shorter buys precision no job needs;
# longer makes a job's actual fire time drift visibly from its declared one.
DEFAULT_TICK_INTERVAL_SECONDS: Final[float] = 15.0

_RUNS_COUNTER: Final = counter(SCHEDULER_JOB_RUNS)
_OVERLAPS_COUNTER: Final = counter(SCHEDULER_OVERLAPS_REFUSED)


def _system_now_utc() -> datetime:
    return datetime.now(UTC)


class SchedulerBeat:
    """Runs every registered job on its schedule, against one run ledger."""

    __slots__ = (
        "_catalogue",
        "_clock",
        "_completed",
        "_concurrency",
        "_in_flight",
        "_ledger",
        "_tick_interval_seconds",
    )

    def __init__(
        self,
        *,
        catalogue: JobCatalogue,
        ledger: JobRunLedger,
        clock: Callable[[], datetime] = _system_now_utc,
        max_concurrent_jobs: int = 4,
        tick_interval_seconds: float = DEFAULT_TICK_INTERVAL_SECONDS,
    ) -> None:
        self._catalogue = catalogue
        self._ledger = ledger
        # Injected, so a six-hour outage is one call with a different argument rather
        # than a test that waits six hours (`.claude/rules/time-and-timezones.md`).
        self._clock = clock
        self._tick_interval_seconds = tick_interval_seconds
        self._concurrency = asyncio.Semaphore(max_concurrent_jobs)
        self._in_flight: dict[str, asyncio.Task[None]] = {}
        self._completed: list[JobRunReport] = []

    async def run_forever(self) -> NoReturn:
        """Sweep, then tick until something unrecoverable stops the process.

        Returns never. It exits by propagating a failure outside the `FkingError`
        taxonomy, or by cancellation.
        """
        async with self._ledger.single_instance():
            await self._sweep_abandoned()
            while True:
                await self.tick(self._clock())
                await asyncio.sleep(self._tick_interval_seconds)

    async def tick(self, now_utc: datetime) -> TickReport:
        """Admit whatever is due at `now_utc` and start it.

        The tick opens its own correlation scope, and each job run opens a nested one of
        its own. They are separate chains on purpose: a tick that fires ingestion and gap
        detection has started two flows that have nothing to do with each other, and one
        id across both would join them in every query built on it.
        """
        self._reap()
        claimed: list[JobFire] = []
        refused: list[str] = []
        already_claimed: list[str] = []

        with correlation_scope(uuid4()):
            for spec in self._catalogue:
                if spec.job_id in self._in_flight:
                    refused.append(spec.job_id)
                    self._refuse_overlap(spec)
                    continue
                fires = await self._claim_due(spec, now_utc=now_utc)
                if fires is None:
                    continue
                if not fires:
                    already_claimed.append(spec.job_id)
                    continue
                self._in_flight[spec.job_id] = asyncio.create_task(
                    self._run_fires(spec, fires), name=f"fking-scheduler-{spec.job_id}"
                )
                claimed.extend(fires)

        return TickReport(
            claimed=tuple(claimed),
            refused_overlap=tuple(refused),
            already_claimed=tuple(already_claimed),
        )

    async def drain(self) -> tuple[JobRunReport, ...]:
        """Await every in-flight job and return every report gathered so far.

        For tests and for a graceful shutdown. `run_forever` never calls it: a beat that
        drained each tick would serialise every job behind the slowest one, which is the
        behaviour `tick` is shaped to avoid.
        """
        while self._in_flight:
            await asyncio.gather(*self._in_flight.values(), return_exceptions=True)
            self._reap()
        return tuple(self._completed)

    async def _sweep_abandoned(self) -> None:
        """Close runs a previous process claimed and never finished."""
        swept = await self._ledger.abandon_unfinished()
        with correlation_scope(BOOT):
            _LOG.info("scheduler.boot", abandoned_run_count=len(swept))
            for job_id, fire_time_utc in swept:
                # One record per abandoned window rather than a count alone: a count says
                # the process was restarted mid-job, and only the window says which
                # period nothing covered.
                _LOG.warning(
                    "scheduler.run_abandoned",
                    job_id=job_id,
                    fire_time_utc=fire_time_utc.isoformat(),
                )

    def _refuse_overlap(self, spec: JobSpec) -> None:
        """Record that a job is still running and its next run was refused, not queued."""
        _OVERLAPS_COUNTER.increment(job_id=spec.job_id)
        _LOG.warning(
            "scheduler.overlap_refused",
            job_id=spec.job_id,
            misfire_policy=spec.misfire_policy.value,
            reason="a run of this job is still in flight; the cadence is shorter than the job",
        )

    async def _claim_due(self, spec: JobSpec, *, now_utc: datetime) -> tuple[JobFire, ...] | None:
        """Claim every fire time due for `spec`.

        `None` means nothing was due. An empty tuple means something was due and another
        writer already held all of it, which is a different condition and a different
        line in the report.
        """
        last_fire_utc = await self._ledger.last_fire_utc(spec.job_id)
        due = due_fire_times(
            schedule=spec.schedule,
            misfire_policy=spec.misfire_policy,
            last_fire_utc=last_fire_utc,
            now_utc=now_utc,
            max_catch_up_runs=spec.max_catch_up_runs,
        )
        if not due.fire_times:
            return None

        fires: list[JobFire] = []
        for fire_time_utc in due.fire_times:
            fire = JobFire(
                job_id=spec.job_id,
                fire_time_utc=fire_time_utc,
                correlation_id=uuid4(),
                is_catch_up=due.is_catch_up,
            )
            if await self._ledger.claim(
                job_id=fire.job_id,
                fire_time_utc=fire.fire_time_utc,
                correlation_id=fire.correlation_id,
                is_catch_up=fire.is_catch_up,
            ):
                fires.append(fire)
        return tuple(fires)

    async def _run_fires(self, spec: JobSpec, fires: tuple[JobFire, ...]) -> None:
        """Execute one job's due fire times, in the order they were scheduled."""
        async with self._concurrency:
            for fire in fires:
                await self._run_one(spec, fire)

    async def _run_one(self, spec: JobSpec, fire: JobFire) -> None:
        """Execute one fire time and record how it ended.

        `time.monotonic` for the duration, never a wall-clock subtraction: an NTP step
        correction mid-run produces a negative latency, which lands in a histogram as an
        underflow (`.claude/rules/time-and-timezones.md`, the one exception).
        """
        started = time.monotonic()
        with (
            correlation_scope(fire.correlation_id),
            traced(
                "scheduler.job_run",
                job_id=fire.job_id,
                misfire_policy=spec.misfire_policy.value,
                is_catch_up=str(fire.is_catch_up).lower(),
            ),
        ):
            _LOG.info(
                "scheduler.job_started",
                job_id=fire.job_id,
                fire_time_utc=fire.fire_time_utc.isoformat(),
                is_catch_up=fire.is_catch_up,
            )
            try:
                await spec.run(fire)
            except FkingError as failure:
                # The only handler here, and it is not a blind one. A member of the
                # taxonomy is a failure this system raises on purpose; anything else
                # propagates out of this task, is re-raised by the next tick, and stops
                # the process.
                await self._record(
                    fire,
                    outcome=JobOutcome.FAILED,
                    failure_reason=f"{type(failure).__name__}: {failure}",
                )
                _LOG.exception(
                    "scheduler.job_failed",
                    job_id=fire.job_id,
                    fire_time_utc=fire.fire_time_utc.isoformat(),
                    outcome=JobOutcome.FAILED.value,
                    duration_seconds=round(time.monotonic() - started, 3),
                )
                return
            await self._record(fire, outcome=JobOutcome.SUCCEEDED, failure_reason=None)
            _LOG.info(
                "scheduler.job_succeeded",
                job_id=fire.job_id,
                fire_time_utc=fire.fire_time_utc.isoformat(),
                outcome=JobOutcome.SUCCEEDED.value,
                duration_seconds=round(time.monotonic() - started, 3),
            )

    async def _record(
        self, fire: JobFire, *, outcome: JobOutcome, failure_reason: str | None
    ) -> None:
        """Close the run in the ledger, count it, and keep the report for `drain`.

        The duration is logged rather than stored: the ledger holds the two instants it
        was derived from, and a stored duration is a second answer to the same question
        that can disagree with them.
        """
        await self._ledger.finish(
            job_id=fire.job_id,
            fire_time_utc=fire.fire_time_utc,
            outcome=outcome,
            failure_reason=failure_reason,
        )
        _RUNS_COUNTER.increment(job_id=fire.job_id, outcome=outcome.value)
        self._completed.append(
            JobRunReport(
                job_id=fire.job_id,
                fire_time_utc=fire.fire_time_utc,
                outcome=outcome,
                correlation_id=fire.correlation_id,
            )
        )

    def _reap(self) -> None:
        """Retire finished tasks, re-raising whatever ended one outside the taxonomy.

        This is where a job's unexpected exception reaches the beat. Retrieving it here
        rather than leaving it on the task is what turns "Task exception was never
        retrieved" on stderr at interpreter shutdown into a failure that stops the
        process at the next tick.
        """
        for job_id, task in list(self._in_flight.items()):
            if not task.done():
                continue
            del self._in_flight[job_id]
            task.result()
