"""The kill-switch journal against real PostgreSQL: what a boot derives, and what it cannot.

These are the tests #53 could not write, because the derivation it built had no adapter
and therefore nothing to run against. Every one of them is about the same question asked
from a different direction: *when the journal cannot be read, does the system come back
halted?* The failure being closed off is a `try/except` that returns an empty tuple, which
reports "no trip has ever happened" for a revoked grant, a database that is not there and a
migration mid-flight alike.

Never a mock. A mocked database would agree with itself about grants it does not enforce,
constraints it does not have, and a trigger it has never run. The three unreadable cases
here are produced by actually revoking the grant, actually pointing at a database that
does not exist, and actually leaving the schema one revision short.
"""

from __future__ import annotations

import asyncio
import json
import socket
import uuid
from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Final
from uuid import UUID

import pytest
import pytest_asyncio
import sqlalchemy as sa
from alembic import command
from sqlalchemy.exc import DBAPIError, IntegrityError
from sqlalchemy.ext.asyncio import AsyncEngine, create_async_engine

from fking.domain import Balance, Portfolio, Position, Side
from fking.execution import KillSwitchJournal, restore_kill_switch
from fking.risk import (
    ArmEvent,
    BookSnapshot,
    JournalRead,
    JournalUnreadable,
    KillSwitchGate,
    KillSwitchStatus,
    ResumeEvent,
    TripEvent,
    TripTrigger,
    derive_state,
)
from tests.conftest import alembic_config, dsn_for
from tests.support.domain_factory import BTCUSDT, make_fill

pytestmark = [pytest.mark.integration, pytest.mark.slow]

TRIPPED_AT: Final = datetime(2026, 8, 1, 3, 14, 15, 926535, tzinfo=UTC)
OPERATOR: Final = "human:ismet"
ROOT_CAUSE: Final = "stale funding feed drove the drawdown estimator negative for 40 minutes"
# The last revision before 0019 added the columns the journal reads. Applying only this
# far is a deployment whose code is ahead of its schema -- the real shape of "migration
# mid-flight", rather than a mutation invented to stand in for one.
REVISION_BEFORE_THE_COLUMNS: Final = "0018_trial_ledger_search_context"


def _trigger() -> TripTrigger:
    return TripTrigger(
        trigger_id="drawdown.daily",
        unit="fraction",
        # 18 decimal places, the column's full scale. A value that survives the round trip
        # exactly is the only evidence that nothing on the path went through a float.
        observed_value=Decimal("0.061000000000000001"),
        threshold_value=Decimal("0.05"),
        detail="equity fell 6.1% against a 5% daily limit",
    )


def _snapshot() -> BookSnapshot:
    """A book with a position and a balance in it, not an empty one.

    The empty snapshot round-trips through any encoder. What has to survive is the nested
    `Decimal` inside a `Position` inside a `Portfolio` inside one JSONB column.
    """
    position: Position = (
        Position.flat(BTCUSDT)
        .with_fill(make_fill(side=Side.BUY, quote_price="64000.01", base_quantity="0.01"))
        .after
    )
    return BookSnapshot(
        portfolio=Portfolio(
            as_of_utc=TRIPPED_AT,
            positions=(position,),
            cash_balances={
                "USDT": Balance(
                    asset="USDT",
                    free_quantity=Decimal("9123.456789012345678"),
                    locked_quantity=Decimal("0"),
                )
            },
        ),
        open_client_order_ids=("fk-a", "fk-b"),
        protective_client_order_ids=("fk-b",),
        reconciled_at_utc=TRIPPED_AT - timedelta(minutes=1),
        reconciliation_is_clean=True,
    )


def _trip(incident_id: UUID, *, occurred_at_utc: datetime = TRIPPED_AT) -> TripEvent:
    return TripEvent(
        event_id=uuid.uuid4(),
        incident_id=incident_id,
        correlation_id=uuid.uuid4(),
        occurred_at_utc=occurred_at_utc,
        actor="risk.drawdown_monitor",
        trigger=_trigger(),
        snapshot=_snapshot(),
    )


def _resume(incident_id: UUID, *, occurred_at_utc: datetime) -> ResumeEvent:
    return ResumeEvent(
        event_id=uuid.uuid4(),
        incident_id=incident_id,
        correlation_id=uuid.uuid4(),
        occurred_at_utc=occurred_at_utc,
        operator_id=OPERATOR,
        root_cause=ROOT_CAUSE,
    )


@pytest_asyncio.fixture
async def journal(app_engine: AsyncEngine) -> AsyncIterator[KillSwitchJournal]:
    """The journal as the application holds it: `fking_app`, with INSERT and SELECT.

    Not the migrator. The grants are half of what these tests are about, and a journal
    running as the owner of the table would pass the revoked-grant case by holding rights
    the running system does not have.
    """
    yield KillSwitchJournal(app_engine)


# --------------------------------------------------------------------------- round trip


@pytest.mark.asyncio
async def test_a_trip_row_survives_the_round_trip_exactly(journal: KillSwitchJournal) -> None:
    """Every field, including the nested decimals inside the snapshot."""
    written = _trip(uuid.uuid4())

    await journal.append(written)
    outcome = await journal.read()

    assert isinstance(outcome, JournalRead)
    assert outcome.events == (written,)


@pytest.mark.asyncio
async def test_all_three_event_kinds_round_trip_in_journal_order(
    journal: KillSwitchJournal,
) -> None:
    incident_id = uuid.uuid4()
    tripped = _trip(incident_id)
    armed = ArmEvent(
        event_id=uuid.uuid4(),
        incident_id=incident_id,
        correlation_id=uuid.uuid4(),
        occurred_at_utc=TRIPPED_AT + timedelta(hours=1),
        operator_id=OPERATOR,
    )
    resumed = _resume(incident_id, occurred_at_utc=TRIPPED_AT + timedelta(hours=1, seconds=30))

    for event in (tripped, armed, resumed):
        await journal.append(event)
    outcome = await journal.read()

    assert isinstance(outcome, JournalRead)
    assert outcome.events == (tripped, armed, resumed)
    assert derive_state(outcome).status is KillSwitchStatus.TRADING


@pytest.mark.asyncio
async def test_the_rows_carry_the_tables_own_vocabulary(
    journal: KillSwitchJournal, engine: AsyncEngine
) -> None:
    """`cleared`, not `resumed`: the table keeps the spelling 0005 wrote.

    Asserted against the database rather than against the adapter's constant, because the
    thing that would break is the `CHECK`, and the `CHECK` is what an insert meets.
    """
    incident_id = uuid.uuid4()
    await journal.append(_trip(incident_id))
    await journal.append(_resume(incident_id, occurred_at_utc=TRIPPED_AT + timedelta(minutes=5)))

    async with engine.connect() as connection:
        types = (
            await connection.scalars(
                sa.text("SELECT event_type FROM kill_switch_event ORDER BY occurred_at_utc")
            )
        ).all()

    assert list(types) == ["tripped", "cleared"]


# --------------------------------------------------------------------------- rehydration


@pytest.mark.asyncio
async def test_a_trip_with_no_resume_boots_halted_after_a_thirty_day_gap(
    journal: KillSwitchJournal,
) -> None:
    """A restart is not a reset, and neither is a month of them."""
    incident_id = uuid.uuid4()
    await journal.append(_trip(incident_id, occurred_at_utc=TRIPPED_AT - timedelta(days=30)))

    gate = KillSwitchGate()
    outcome = await restore_kill_switch(journal.read, gate=gate)

    assert isinstance(outcome, JournalRead)
    assert gate.state.is_halted
    assert gate.state.incident_id == incident_id


@pytest.mark.asyncio
async def test_resuming_one_of_two_open_incidents_leaves_the_boot_halted(
    journal: KillSwitchJournal,
) -> None:
    """The named regression from #53, now against rows rather than in-memory events.

    Hypothesis found a fold that tracked a single open trip and cleared it on any RESUME:
    with two incidents open the system reported TRADING while one was still live. Two
    simultaneous incidents is the normal case rather than an exotic one -- a testnet wipe
    presents as a reconciliation divergence and a balance collapse at the same time -- so
    the rehydration path must not reintroduce the collapse by folding rows on its way out
    of the database.
    """
    first, second = uuid.uuid4(), uuid.uuid4()
    await journal.append(_trip(first))
    await journal.append(_trip(second, occurred_at_utc=TRIPPED_AT + timedelta(seconds=2)))
    await journal.append(_resume(first, occurred_at_utc=TRIPPED_AT + timedelta(minutes=10)))

    gate = KillSwitchGate()
    await restore_kill_switch(journal.read, gate=gate)

    assert gate.state.status is KillSwitchStatus.HALTED
    assert gate.state.incident_id == second

    await journal.append(_resume(second, occurred_at_utc=TRIPPED_AT + timedelta(minutes=11)))
    reopened = await restore_kill_switch(journal.read, gate=gate)

    assert isinstance(reopened, JournalRead)
    assert not gate.state.is_halted


@pytest.mark.asyncio
async def test_an_empty_journal_opens_the_gate(journal: KillSwitchJournal) -> None:
    """The one case that may open it, and the gate is closed until this call returns."""
    gate = KillSwitchGate()
    assert gate.state.is_halted

    await restore_kill_switch(journal.read, gate=gate)

    assert gate.state.status is KillSwitchStatus.TRADING


# --------------------------------------------------------------- the three unreadable cases


@pytest.mark.asyncio
async def test_a_revoked_select_grant_boots_halted(
    journal: KillSwitchJournal, engine: AsyncEngine
) -> None:
    """The case the issue calls the one that matters.

    A trip is in the journal and the application can no longer read it. The wrong answer
    is an empty tuple, which says "no trip has ever happened" and opens the gate on the
    exact deployment that has lost the ability to see its own kill switch.
    """
    await journal.append(_trip(uuid.uuid4()))
    async with engine.begin() as connection:
        await connection.execute(sa.text("REVOKE SELECT ON kill_switch_event FROM fking_app"))

    gate = KillSwitchGate()
    outcome = await restore_kill_switch(journal.read, gate=gate)

    assert isinstance(outcome, JournalUnreadable)
    assert "permission denied" in outcome.reason
    assert gate.state.is_halted
    assert "unknown is tripped" in (gate.state.halted_reason or "")


@pytest.mark.asyncio
async def test_a_database_that_is_not_there_boots_halted(postgres_server: str) -> None:
    """A catalog that does not exist: the connection fails before any statement runs."""
    absent = create_async_engine(dsn_for(postgres_server, f"fking_absent_{uuid.uuid4().hex[:8]}"))
    try:
        gate = KillSwitchGate()
        outcome = await restore_kill_switch(KillSwitchJournal(absent).read, gate=gate)
    finally:
        await absent.dispose()

    assert isinstance(outcome, JournalUnreadable)
    assert gate.state.is_halted


@pytest.mark.asyncio
async def test_a_server_that_refuses_the_connection_boots_halted(postgres_server: str) -> None:
    """Nothing listening on the port. The socket-level half of "the database is down".

    Separate from the absent-catalog case above because they fail at different layers --
    one is a driver error, one is an `OSError` the driver never wraps -- and a `read()`
    that caught only the first would return a `JournalUnreadable` for a stopped container
    and raise for a stopped process.
    """
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        closed_port = int(probe.getsockname()[1])

    url = sa.engine.make_url(postgres_server).set(host="127.0.0.1", port=closed_port)
    unreachable = create_async_engine(url, connect_args={"timeout": 5})
    try:
        gate = KillSwitchGate()
        outcome = await restore_kill_switch(KillSwitchJournal(unreachable).read, gate=gate)
    finally:
        await unreachable.dispose()

    assert isinstance(outcome, JournalUnreadable)
    assert gate.state.is_halted


def test_a_migration_mid_flight_boots_halted(scratch_dsn: str) -> None:
    """The schema one revision short of the columns the journal reads.

    Synchronous, because `migrations/env.py` calls `asyncio.run` and cannot be reached
    from inside a running event loop -- an `async def` test driving Alembic fails with a
    never-awaited coroutine rather than with anything about migrations.
    """
    command.upgrade(alembic_config(scratch_dsn), REVISION_BEFORE_THE_COLUMNS)

    async def _read() -> tuple[object, KillSwitchGate]:
        engine = create_async_engine(scratch_dsn)
        gate = KillSwitchGate()
        try:
            outcome = await restore_kill_switch(KillSwitchJournal(engine).read, gate=gate)
        finally:
            await engine.dispose()
        return outcome, gate

    outcome, gate = asyncio.run(_read())

    assert isinstance(outcome, JournalUnreadable)
    assert "incident_id" in outcome.reason
    assert gate.state.is_halted


@pytest.mark.asyncio
async def test_a_row_that_will_not_parse_makes_the_whole_journal_unreadable(
    journal: KillSwitchJournal, engine: AsyncEngine
) -> None:
    """Not skipped. Skipping the row that failed to parse is how an open TRIP disappears.

    The row inserted here is the shape a `tripped` row written before 0019 has: the
    columns exist and are `NULL`, because the completeness constraints are `NOT VALID` and
    therefore say nothing about rows that predate them. Dropping the constraint first is
    what makes that row insertable now; it is not a mutation invented for the test.
    """
    await journal.append(_trip(uuid.uuid4()))
    async with engine.begin() as connection:
        await connection.execute(
            sa.text(
                "ALTER TABLE kill_switch_event "
                "DROP CONSTRAINT ck_kill_switch_event_trip_row_is_complete"
            )
        )
        await connection.execute(
            sa.text(
                """
                INSERT INTO kill_switch_event
                    (event_id, event_type, reason, actor, correlation_id, occurred_at_utc)
                VALUES
                    (gen_random_uuid(), 'tripped', 'a row from before 0019', 'risk',
                     gen_random_uuid(), :occurred_at_utc)
                """
            ),
            {"occurred_at_utc": TRIPPED_AT},
        )

    gate = KillSwitchGate()
    outcome = await restore_kill_switch(journal.read, gate=gate)

    assert isinstance(outcome, JournalUnreadable)
    assert "does not parse" in outcome.reason
    assert gate.state.is_halted


# The column values a complete `tripped` row carries. Each case below replaces exactly one
# of them with something the parser must refuse, and every one of them is a shape a real
# database can hold: the completeness constraints are NOT VALID, so they say nothing about
# a row inserted after they have been dropped -- which is what a migration that widens the
# table later, or a hand-written row during an incident, actually looks like.
_COMPLETE_TRIP_COLUMNS: Final[dict[str, object]] = {
    "event_type": "tripped",
    "incident_id": str(uuid.uuid4()),
    "trigger_id": "drawdown.daily",
    "trigger_unit": "fraction",
    "trigger_observed_value": "0.061",
    "trigger_threshold_value": "0.05",
    "trigger_detail": "equity fell 6.1% against a 5% daily limit",
    "book_snapshot": json.dumps(
        {
            "portfolio": {
                "as_of_utc": TRIPPED_AT.isoformat(),
                "positions": [],
                "cash_balances": {},
            },
            "open_client_order_ids": [],
            "protective_client_order_ids": [],
            "reconciled_at_utc": None,
            "reconciliation_is_clean": False,
        }
    ),
}


async def _insert_doctored_row(engine: AsyncEngine, overrides: dict[str, object]) -> None:
    """Insert one row with the completeness constraints removed first."""
    columns = _COMPLETE_TRIP_COLUMNS | overrides
    async with engine.begin() as connection:
        for constraint in (
            "ck_kill_switch_event_trip_row_is_complete",
            "ck_kill_switch_event_event_type_is_known",
        ):
            await connection.execute(
                sa.text(f"ALTER TABLE kill_switch_event DROP CONSTRAINT {constraint}")
            )
        await connection.execute(
            sa.text(
                """
                INSERT INTO kill_switch_event
                    (event_id, event_type, reason, actor, correlation_id, occurred_at_utc,
                     incident_id, trigger_id, trigger_unit, trigger_observed_value,
                     trigger_threshold_value, trigger_detail, book_snapshot)
                VALUES
                    (gen_random_uuid(), :event_type, 'doctored', 'risk', gen_random_uuid(),
                     :occurred_at_utc, cast(:incident_id as uuid), :trigger_id, :trigger_unit,
                     cast(:trigger_observed_value as numeric),
                     cast(:trigger_threshold_value as numeric), :trigger_detail,
                     cast(:book_snapshot as jsonb))
                """
            ),
            {**columns, "occurred_at_utc": TRIPPED_AT},
        )


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "overrides",
    [
        pytest.param({"event_type": "quarantined"}, id="an-event-type-this-code-cannot-classify"),
        pytest.param({"trigger_detail": None}, id="a-missing-text-field"),
        pytest.param({"trigger_observed_value": None}, id="a-missing-measured-value"),
        pytest.param({"book_snapshot": None}, id="a-missing-snapshot"),
        pytest.param({"book_snapshot": "[1, 2]"}, id="a-snapshot-that-is-not-an-object"),
        pytest.param(
            {"book_snapshot": '{"portfolio": 1.5}'},
            # Every decimal an encoded snapshot carries is a JSON *string*; a JSON number
            # means the payload was written by something that bypassed the codec, and a
            # float that reached a position quantity is not repairable afterwards.
            id="a-snapshot-carrying-a-json-number",
        ),
    ],
)
async def test_no_doctored_row_can_make_the_system_come_back_trading(
    journal: KillSwitchJournal, engine: AsyncEngine, overrides: dict[str, object]
) -> None:
    """One assertion, six shapes: the journal is unreadable and the gate stays shut.

    The interesting half is that the journal is otherwise *empty* here. An adapter that
    dropped the row it could not parse would find no trip, derive `TRADING`, and open the
    order path on a database it has just failed to understand.
    """
    await _insert_doctored_row(engine, overrides)

    gate = KillSwitchGate()
    outcome = await restore_kill_switch(journal.read, gate=gate)

    assert isinstance(outcome, JournalUnreadable)
    assert gate.state.is_halted


# --------------------------------------------------------------------------- append-only


@pytest.mark.asyncio
async def test_updating_a_journal_row_raises(
    journal: KillSwitchJournal, engine: AsyncEngine
) -> None:
    """The trigger, which holds regardless of who holds which grant."""
    await journal.append(_trip(uuid.uuid4()))

    async with engine.begin() as connection:
        with pytest.raises(DBAPIError, match="append-only"):
            await connection.execute(sa.text("UPDATE kill_switch_event SET reason = 'rewritten'"))


@pytest.mark.asyncio
async def test_deleting_a_journal_row_raises(
    journal: KillSwitchJournal, engine: AsyncEngine
) -> None:
    """A deleted TRIP row is a kill switch that resumes itself."""
    await journal.append(_trip(uuid.uuid4()))

    async with engine.begin() as connection:
        with pytest.raises(DBAPIError, match="append-only"):
            await connection.execute(sa.text("DELETE FROM kill_switch_event"))


@pytest.mark.asyncio
async def test_the_application_role_may_only_insert_and_select(engine: AsyncEngine) -> None:
    """The grant, which is the primary control -- no trigger can intercept a TRUNCATE."""
    async with engine.connect() as connection:
        row = (
            await connection.execute(
                sa.text(
                    """
                    SELECT has_table_privilege('fking_app', 'kill_switch_event', 'UPDATE')
                               AS may_update,
                           has_table_privilege('fking_app', 'kill_switch_event', 'DELETE')
                               AS may_delete,
                           has_table_privilege('fking_app', 'kill_switch_event', 'TRUNCATE')
                               AS may_truncate,
                           has_table_privilege('fking_app', 'kill_switch_event', 'INSERT')
                               AS may_insert,
                           has_table_privilege('fking_app', 'kill_switch_event', 'SELECT')
                               AS may_select
                    """
                )
            )
        ).one()

    assert (row.may_update, row.may_delete, row.may_truncate) == (False, False, False)
    assert (row.may_insert, row.may_select) == (True, True)


# ------------------------------------------------------- what the schema refuses outright


async def _insert_resume_row(engine: AsyncEngine, *, operator_id: str, root_cause: str) -> None:
    async with engine.begin() as connection:
        await connection.execute(
            sa.text(
                """
                INSERT INTO kill_switch_event
                    (event_id, event_type, reason, actor, correlation_id, occurred_at_utc,
                     incident_id, operator_id, root_cause)
                VALUES
                    (gen_random_uuid(), 'cleared', :reason, :operator_id, gen_random_uuid(),
                     :occurred_at_utc, gen_random_uuid(), :operator_id, :root_cause)
                """
            ),
            {
                "reason": root_cause or "(blank)",
                "operator_id": operator_id,
                "root_cause": root_cause,
                "occurred_at_utc": TRIPPED_AT,
            },
        )


@pytest.mark.asyncio
async def test_the_database_refuses_a_root_cause_a_script_could_type(engine: AsyncEngine) -> None:
    """Nineteen characters, the boundary. Checked in Python twice and here a third time.

    The third check is the one that still holds for a future writer that inserts a row
    without ever constructing a `ResumeEvent`, which is exactly the writer a dashboard in
    a hurry produces.
    """
    with pytest.raises(IntegrityError, match="root_cause_is_explained"):
        await _insert_resume_row(engine, operator_id=OPERATOR, root_cause="x" * 19)


@pytest.mark.asyncio
async def test_the_database_refuses_a_resume_that_names_no_person(engine: AsyncEngine) -> None:
    with pytest.raises(IntegrityError, match="operator_id_names_a_person"):
        await _insert_resume_row(engine, operator_id="agent:executor", root_cause=ROOT_CAUSE)


@pytest.mark.asyncio
async def test_a_trip_row_without_its_trigger_is_refused(engine: AsyncEngine) -> None:
    """The completeness constraint, from the migration forward.

    `NOT VALID` says nothing about rows written before 0019 and everything about rows
    written after it, which is `NOT NULL` without rewriting a single historical row.
    """
    async with engine.begin() as connection:
        with pytest.raises(IntegrityError, match="trip_row_is_complete"):
            await connection.execute(
                sa.text(
                    """
                    INSERT INTO kill_switch_event
                        (event_id, event_type, reason, actor, correlation_id, occurred_at_utc,
                         incident_id)
                    VALUES
                        (gen_random_uuid(), 'tripped', 'no trigger', 'risk', gen_random_uuid(),
                         :occurred_at_utc, gen_random_uuid())
                    """
                ),
                {"occurred_at_utc": TRIPPED_AT},
            )


@pytest.mark.asyncio
async def test_the_snapshot_is_stored_as_queryable_json(
    journal: KillSwitchJournal, engine: AsyncEngine
) -> None:
    """JSONB rather than text, so an investigation can query the book without the adapter.

    The snapshot is the artefact that replaces freeze-and-inspect (ADR 0014): the flatten
    closes the book before anyone looks at it, so this column is where "what was open at
    the moment of the trip" survives.
    """
    await journal.append(_trip(uuid.uuid4()))

    async with engine.connect() as connection:
        stored = await connection.scalar(
            sa.text(
                "SELECT book_snapshot -> 'open_client_order_ids' FROM kill_switch_event LIMIT 1"
            )
        )

    order_ids = stored if isinstance(stored, list) else json.loads(str(stored))
    assert order_ids == ["fk-a", "fk-b"]


@pytest.mark.asyncio
async def test_appending_a_duplicate_event_id_raises_rather_than_being_absorbed(
    journal: KillSwitchJournal,
) -> None:
    """`append` is loud on failure, and the asymmetry with `read` is the design.

    An unreadable journal is a state the system can hold safely, because holding it means
    halted. A trip that was decided and not recorded is not: the next boot would come back
    trading.
    """
    written = _trip(uuid.uuid4())
    await journal.append(written)

    with pytest.raises(IntegrityError):
        await journal.append(written)


@pytest.mark.asyncio
async def test_a_gate_that_was_open_is_closed_by_a_later_journal_read(
    journal: KillSwitchJournal,
) -> None:
    """Boot is not the only caller: the same path re-derives after a trip lands.

    The gate is a holder, so a state read out of it earlier is a value that does not move.
    Re-reading the journal is what moves the holder.
    """
    gate = KillSwitchGate()
    await restore_kill_switch(journal.read, gate=gate)
    assert gate.state.status is KillSwitchStatus.TRADING

    await journal.append(_trip(uuid.uuid4()))
    await restore_kill_switch(journal.read, gate=gate)

    assert gate.state.is_halted
