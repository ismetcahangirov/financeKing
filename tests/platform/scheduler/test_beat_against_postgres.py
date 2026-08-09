"""The beat against a real database: outage semantics, overlap, restart, and the trigger.

Never a mock. The properties under test here are a primary key, a partial index and a
`BEFORE UPDATE` trigger, and a mocked ledger would prove that the mock refuses a rewrite
(`TESTING.md`, `CLAUDE.md` section 5).
"""

from __future__ import annotations

import asyncio
import contextlib
from datetime import UTC, datetime, timedelta
from uuid import uuid4

import pytest
import sqlalchemy as sa
from sqlalchemy.exc import DBAPIError
from sqlalchemy.ext.asyncio import AsyncEngine

from fking.platform.correlation import current_correlation_id
from fking.platform.errors import DataIntegrityError
from fking.platform.scheduler import (
    IntervalSchedule,
    JobCatalogue,
    JobFire,
    JobOutcome,
    JobRunLedger,
    JobRunner,
    JobSpec,
    MisfirePolicy,
    SchedulerAlreadyRunningError,
    SchedulerBeat,
)

pytestmark = [pytest.mark.integration, pytest.mark.slow]

ANCHOR = datetime(2026, 8, 4, 0, 0, tzinfo=UTC)
HOURLY = IntervalSchedule(period=timedelta(hours=1), anchor_utc=ANCHOR)
LAST_TICK = ANCHOR + timedelta(hours=2)
BACK_UP_AT = ANCHOR + timedelta(hours=8)
MISSED_FIRES = 6
ONE_RUN = 1
TWO_RUNS = 2

JOB_ID = "data.ingest_klines"


class _Recorder:
    """A job that records the fire times it was handed, in the order it was handed them."""

    def __init__(self) -> None:
        self.fires: list[JobFire] = []
        self.correlation_ids: list[str | None] = []

    async def __call__(self, fire: JobFire) -> None:
        self.fires.append(fire)
        self.correlation_ids.append(current_correlation_id())


class _StaleCursorLedger(JobRunLedger):
    """A real ledger whose cursor read is frozen, to reproduce a lost claim race.

    Everything that decides the outcome -- the insert, the conflict, the primary key --
    is the real database. Only the read that a concurrent writer would have invalidated
    is held still, because winning that race deterministically is otherwise a matter of
    scheduling luck.
    """

    def __init__(self, engine: AsyncEngine, *, cursor_utc: datetime) -> None:
        super().__init__(engine)
        self._cursor_utc = cursor_utc

    async def last_fire_utc(self, job_id: str) -> datetime | None:
        del job_id
        return self._cursor_utc


def _spec(
    run: JobRunner,
    *,
    misfire_policy: MisfirePolicy = MisfirePolicy.RUN_EVERY_MISSED,
    max_catch_up_runs: int = 24,
) -> JobSpec:
    return JobSpec(
        job_id=JOB_ID,
        schedule=HOURLY,
        misfire_policy=misfire_policy,
        max_catch_up_runs=max_catch_up_runs,
        run=run,
    )


def _beat(engine: AsyncEngine, spec: JobSpec) -> SchedulerBeat:
    catalogue = JobCatalogue()
    catalogue.register(spec)
    return SchedulerBeat(catalogue=catalogue, ledger=JobRunLedger(engine))


async def _claim_directly(ledger: JobRunLedger, fire_time_utc: datetime) -> bool:
    return await ledger.claim(
        job_id=JOB_ID, fire_time_utc=fire_time_utc, correlation_id=uuid4(), is_catch_up=False
    )


async def _runs(engine: AsyncEngine) -> list[dict[str, object]]:
    async with engine.connect() as connection:
        rows = (
            (
                await connection.execute(
                    sa.text(
                        "SELECT job_id, scheduled_fire_utc, outcome, is_catch_up, "
                        "       failure_reason, correlation_id, finished_at_utc "
                        "  FROM scheduler_job_run ORDER BY job_id, scheduled_fire_utc"
                    )
                )
            )
            .mappings()
            .all()
        )
    return [dict(row) for row in rows]


# ---------------------------------------------------------------------------
# Missed runs, end to end
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_a_six_hour_outage_replays_every_missed_window_in_ascending_order(
    engine: AsyncEngine,
) -> None:
    recorder = _Recorder()
    beat = _beat(engine, _spec(recorder))

    await beat.tick(LAST_TICK)
    await beat.drain()
    report = await beat.tick(BACK_UP_AT)
    await beat.drain()

    assert len(report.claimed) == MISSED_FIRES
    assert [fire.fire_time_utc for fire in recorder.fires[1:]] == [
        ANCHOR + timedelta(hours=hour) for hour in range(3, 9)
    ]
    assert all(fire.is_catch_up for fire in recorder.fires[1:])
    assert len(await _runs(engine)) == MISSED_FIRES + ONE_RUN


@pytest.mark.asyncio
async def test_skip_to_latest_runs_once_over_the_same_outage(engine: AsyncEngine) -> None:
    recorder = _Recorder()
    beat = _beat(
        engine,
        _spec(recorder, misfire_policy=MisfirePolicy.SKIP_TO_LATEST, max_catch_up_runs=1),
    )

    await beat.tick(LAST_TICK)
    await beat.drain()
    await beat.tick(BACK_UP_AT)
    await beat.drain()

    assert [fire.fire_time_utc for fire in recorder.fires] == [LAST_TICK, BACK_UP_AT]


@pytest.mark.asyncio
async def test_run_now_is_stamped_at_the_instant_it_observed(engine: AsyncEngine) -> None:
    recorder = _Recorder()
    beat = _beat(engine, _spec(recorder, misfire_policy=MisfirePolicy.RUN_NOW, max_catch_up_runs=1))
    observed_at = BACK_UP_AT + timedelta(minutes=17)

    await beat.tick(LAST_TICK)
    await beat.drain()
    await beat.tick(observed_at)
    await beat.drain()

    assert recorder.fires[-1].fire_time_utc == observed_at


# ---------------------------------------------------------------------------
# Restart
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_a_restart_does_not_refire_a_window_that_already_ran(
    engine: AsyncEngine,
) -> None:
    """Exercised by building a second beat over the same database rather than by
    asserting on the first one's memory -- process memory is empty exactly when the
    question matters, which is the tick immediately after a restart."""
    before_restart = _Recorder()
    first = _beat(engine, _spec(before_restart))
    await first.tick(LAST_TICK + timedelta(hours=1))
    await first.drain()

    after_restart = _Recorder()
    second = _beat(engine, _spec(after_restart))
    report = await second.tick(LAST_TICK + timedelta(hours=1, minutes=30))
    await second.drain()

    assert len(before_restart.fires) == ONE_RUN
    assert after_restart.fires == []
    assert report.claimed == ()


@pytest.mark.asyncio
async def test_a_claim_for_a_fire_time_another_writer_holds_is_refused(
    engine: AsyncEngine,
) -> None:
    ledger = JobRunLedger(engine)
    assert await _claim_directly(ledger, LAST_TICK)
    assert not await _claim_directly(ledger, LAST_TICK)


@pytest.mark.asyncio
async def test_a_tick_reports_a_fire_time_it_could_not_claim(engine: AsyncEngine) -> None:
    """The lost race: another writer claims the due fire time between our cursor read and
    our insert, so `ON CONFLICT DO NOTHING` returns no row.

    Reproduced by holding the cursor stale rather than by mocking the ledger -- the
    claim, the conflict and the primary key that arbitrates them are all real. It is a
    different condition from nothing being due, which is why the report says which.
    """
    due_at = LAST_TICK + timedelta(hours=1)
    await _claim_directly(JobRunLedger(engine), due_at)

    recorder = _Recorder()
    catalogue = JobCatalogue()
    catalogue.register(_spec(recorder))
    beat = SchedulerBeat(
        catalogue=catalogue, ledger=_StaleCursorLedger(engine, cursor_utc=LAST_TICK)
    )

    report = await beat.tick(due_at)

    assert report.already_claimed == (JOB_ID,)
    assert report.claimed == ()
    assert recorder.fires == []


# ---------------------------------------------------------------------------
# Overlap
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_a_second_run_is_refused_while_the_first_is_still_in_flight(
    engine: AsyncEngine,
) -> None:
    """Refused, not queued. A queued run fires at a time nobody scheduled, against a
    window that has since moved -- and two of them write the same partition."""
    release = asyncio.Event()
    started = asyncio.Event()
    fires: list[JobFire] = []

    async def slow(fire: JobFire) -> None:
        fires.append(fire)
        started.set()
        await release.wait()

    beat = _beat(engine, _spec(slow))

    first = await beat.tick(LAST_TICK)
    await asyncio.wait_for(started.wait(), timeout=5)
    second = await beat.tick(LAST_TICK + timedelta(hours=1))

    assert len(first.claimed) == ONE_RUN
    assert second.refused_overlap == (JOB_ID,)
    assert second.claimed == ()

    release.set()
    await beat.drain()
    assert len(fires) == ONE_RUN

    # The refused fire time was never claimed, so it is still owed and the next tick takes
    # it. A queue would have run it later, against a window that had already moved.
    third = await beat.tick(LAST_TICK + timedelta(hours=1))
    await beat.drain()
    assert [fire.fire_time_utc for fire in third.claimed] == [LAST_TICK + timedelta(hours=1)]


# ---------------------------------------------------------------------------
# Failure handling
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_a_taxonomy_failure_is_recorded_and_the_beat_keeps_going(
    engine: AsyncEngine,
) -> None:
    attempts: list[JobFire] = []

    async def failing(fire: JobFire) -> None:
        attempts.append(fire)
        raise DataIntegrityError("the archive for that hour is malformed")

    beat = _beat(engine, _spec(failing))

    await beat.tick(LAST_TICK)
    reports = await beat.drain()
    assert [report.outcome for report in reports] == [JobOutcome.FAILED]

    # The beat is still usable, and the failed window is not retried: a fire time is
    # consumed once, whatever its outcome.
    await beat.tick(LAST_TICK + timedelta(hours=1))
    await beat.drain()
    assert len(attempts) == TWO_RUNS

    rows = await _runs(engine)
    assert [row["outcome"] for row in rows] == ["failed", "failed"]
    assert all("DataIntegrityError" in str(row["failure_reason"]) for row in rows)


@pytest.mark.asyncio
async def test_an_exception_outside_the_taxonomy_stops_the_beat(engine: AsyncEngine) -> None:
    """Not caught, not recorded as a job failure. An unknown state is not something to
    keep scheduling through (`docs/rules/error-handling.md`)."""

    async def exploding(fire: JobFire) -> None:
        del fire
        raise KeyError("fillId")

    beat = _beat(engine, _spec(exploding))

    await beat.tick(LAST_TICK)
    with pytest.raises(KeyError, match="fillId"):
        await beat.drain()

    assert [row["outcome"] for row in await _runs(engine)] == [None]


# ---------------------------------------------------------------------------
# The boot sweep, the single-instance lock, and the trigger
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_unfinished_runs_from_a_previous_boot_are_swept_to_abandoned(
    engine: AsyncEngine,
) -> None:
    ledger = JobRunLedger(engine)
    await _claim_directly(ledger, LAST_TICK)

    swept = await ledger.abandon_unfinished()

    assert swept == ((JOB_ID, LAST_TICK),)
    rows = await _runs(engine)
    assert rows[0]["outcome"] == JobOutcome.ABANDONED.value
    assert rows[0]["finished_at_utc"] is not None
    assert await ledger.abandon_unfinished() == ()


@pytest.mark.asyncio
async def test_run_forever_sweeps_at_boot_before_it_ticks(engine: AsyncEngine) -> None:
    """The boot sequence, in order: take the lock, close what the last process left open,
    then start scheduling. A tick before the sweep would leave an in-flight-looking row
    that no process is behind."""
    ledger = JobRunLedger(engine)
    await _claim_directly(ledger, LAST_TICK)  # the run the previous process never closed

    recorder = _Recorder()
    catalogue = JobCatalogue()
    catalogue.register(_spec(recorder))
    due_at = LAST_TICK + timedelta(hours=1)
    beat = SchedulerBeat(
        catalogue=catalogue,
        ledger=ledger,
        clock=lambda: due_at,
        tick_interval_seconds=0.01,
    )

    running = asyncio.create_task(beat.run_forever())
    try:
        async with asyncio.timeout(10):
            while not recorder.fires:
                await asyncio.sleep(0.01)
    finally:
        running.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await running

    rows = await _runs(engine)
    assert rows[0]["scheduled_fire_utc"] == LAST_TICK
    assert rows[0]["outcome"] == JobOutcome.ABANDONED.value
    assert [fire.fire_time_utc for fire in recorder.fires] == [due_at]


@pytest.mark.asyncio
async def test_a_second_beat_over_the_same_database_refuses_to_start(
    engine: AsyncEngine,
) -> None:
    """Two beats would each sweep the other's in-flight runs to abandoned, which is the
    one thing the sweep must never do."""
    ledger = JobRunLedger(engine)
    async with ledger.single_instance():
        with pytest.raises(SchedulerAlreadyRunningError, match="already running"):
            async with JobRunLedger(engine).single_instance():
                pass
    # Released on exit, so a restart works without operator intervention.
    async with ledger.single_instance():
        pass


@pytest.mark.asyncio
async def test_a_finished_run_cannot_be_reopened(engine: AsyncEngine) -> None:
    ledger = JobRunLedger(engine)
    await _claim_directly(ledger, LAST_TICK)
    assert await ledger.finish(job_id=JOB_ID, fire_time_utc=LAST_TICK, outcome=JobOutcome.SUCCEEDED)
    # The application's own predicate refuses it first...
    assert not await ledger.finish(
        job_id=JOB_ID, fire_time_utc=LAST_TICK, outcome=JobOutcome.FAILED, failure_reason="x"
    )
    # ...and the trigger refuses it from the other side, for the writer that forgets.
    with pytest.raises(DBAPIError, match="already finished"):
        async with engine.begin() as connection:
            await connection.execute(
                sa.text(
                    "UPDATE scheduler_job_run SET outcome = 'failed', "
                    "finished_at_utc = clock_timestamp() WHERE job_id = :job_id"
                ),
                {"job_id": JOB_ID},
            )


@pytest.mark.asyncio
async def test_a_claim_cannot_be_relabelled(engine: AsyncEngine) -> None:
    """Moving `scheduled_fire_utc` would relabel which window a run covered."""
    await _claim_directly(JobRunLedger(engine), LAST_TICK)
    with pytest.raises(DBAPIError, match="only in its completion"):
        async with engine.begin() as connection:
            await connection.execute(
                sa.text(
                    "UPDATE scheduler_job_run SET scheduled_fire_utc = :moved "
                    " WHERE job_id = :job_id"
                ),
                {"moved": LAST_TICK + timedelta(hours=1), "job_id": JOB_ID},
            )


@pytest.mark.asyncio
async def test_a_succeeded_run_may_not_carry_a_failure_reason(engine: AsyncEngine) -> None:
    """A green run with an explanation attached is exactly the row a later investigation
    would misread, so the database refuses it."""
    await _claim_directly(JobRunLedger(engine), LAST_TICK)
    with pytest.raises(DBAPIError, match="success_carries_no_reason"):
        async with engine.begin() as connection:
            await connection.execute(
                sa.text(
                    "UPDATE scheduler_job_run "
                    "   SET outcome = 'succeeded', finished_at_utc = clock_timestamp(), "
                    "       failure_reason = 'but also this' "
                    " WHERE job_id = :job_id"
                ),
                {"job_id": JOB_ID},
            )


# ---------------------------------------------------------------------------
# Observability
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_every_execution_runs_inside_its_own_correlation_scope(
    engine: AsyncEngine,
) -> None:
    """Per run, not per tick: a tick that fires two jobs has started two flows that have
    nothing to do with each other, and the id is what the run is reconstructed through."""
    recorder = _Recorder()
    beat = _beat(engine, _spec(recorder))

    await beat.tick(BACK_UP_AT)
    await beat.drain()

    assert len(recorder.correlation_ids) == ONE_RUN
    assert recorder.correlation_ids[0] == str(recorder.fires[0].correlation_id)
    assert str((await _runs(engine))[0]["correlation_id"]) == recorder.correlation_ids[0]
