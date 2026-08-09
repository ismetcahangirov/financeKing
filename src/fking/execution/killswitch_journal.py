"""The PostgreSQL side of the kill switch's journal, and the boot step that reads it.

#53 built the decision and could not build this: `fking.risk` performs no I/O, and the
`strategy and risk are pure and never reach a database` contract enforces it, so the code
that maps a failed `SELECT` on `kill_switch_event` onto `JournalUnreadable` has to sit one
layer up. It sits here rather than in `fking.platform` because `platform` may not import
another `fking` module and this module's whole job is to translate rows into `fking.risk`
values -- `docs/rules/module-boundaries.md`: the translation belongs to the module that
owns the concept, and the layer above `risk` that owns the boot path is `execution`.

Three properties are the reason this file exists at all.

**A failed read is a value, not an exception.** `read()` returns `JournalUnreadable` for a
revoked grant, a database that is not there and a migration mid-flight alike, and
`derive_state` turns every one of them into `HALTED`. The shape being closed off is the
natural one: a `try/except` returning `()` so that startup does not break, which reports
"no trip has ever happened" for all three. A safety mechanism whose unavailability means
"no restriction" is not a safety mechanism, and a restart is not a reset.

**A row that will not parse makes the whole journal unreadable.** Not skipped, not
dropped, not logged-and-continued. Skipping the one row that failed to parse is exactly
how an open `TRIP` disappears and a boot reports `TRADING`; the failure the parser cannot
distinguish is "this row is malformed" from "this row is the one that matters".

**Rehydration keys on `incident_id`, because `derive_state` does.** A fold tracking a
single open trip clears it on any `RESUME`, so with two incidents open the system reports
`TRADING` while one is still live -- a defect Hypothesis found during #53. Two incidents
open at once is the normal case rather than an exotic one: a testnet wipe presents as a
reconciliation divergence and a balance collapse at the same time. This module hands
`derive_state` every row it read and never folds rows itself, so the collapse cannot be
reintroduced here by an adapter that thinks it is being helpful.

Writing is deliberately narrow. There is one `append`, no `update`, no `delete` and no
`correct`; the database would refuse the last three anyway, since `fking_app` holds only
`SELECT` and `INSERT` and a `BEFORE UPDATE OR DELETE` trigger raises regardless of who
holds what (`docs/rules/append-only-audit.md`).
"""

from __future__ import annotations

import json
from collections.abc import Awaitable, Callable, Mapping
from datetime import UTC, datetime
from decimal import Decimal
from typing import Final
from uuid import UUID

import sqlalchemy as sa
from asyncpg import InterfaceError, PostgresError
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncEngine

from fking.domain import DomainError, JsonValue, decode, encode
from fking.platform.logging import get_logger
from fking.risk import (
    ArmEvent,
    BookSnapshot,
    JournalRead,
    JournalReadOutcome,
    JournalUnreadable,
    KillSwitchEvent,
    KillSwitchGate,
    ResumeEvent,
    TripEvent,
    TripTrigger,
    derive_state,
)

__all__ = ["KillSwitchJournal", "restore_kill_switch"]

_LOG: Final = get_logger(__name__)

# The row spellings `0005_execution_and_risk` chose and `0019` extended. `cleared` is the
# resume row: the table is append-only, so a historical row could never be migrated onto a
# `resumed` synonym, and two permanent spellings of one state is worse than one spelling
# that disagrees with the domain's vocabulary in a single documented place.
_TRIPPED: Final[str] = "tripped"
_ARMED: Final[str] = "armed"
_CLEARED: Final[str] = "cleared"

# A failed read's reason reaches a log line and a `KillSwitchState.halted_reason`, both of
# which an operator reads at 03:00. Driver messages carry the failing statement and can run
# to kilobytes, so the reason is capped -- long enough to name the condition, short enough
# that the line stays legible.
_MAX_REASON_LENGTH: Final[int] = 300

# Everything except the timestamp is selected as text, and that is the whole parsing
# strategy rather than a detail. Whether a driver hands back a `dict` or a `str` for JSONB,
# and a `UUID` or a `str` for `uuid`, depends on codecs registered at connection time; a
# numeric that ever arrives as a float is not repairable by widening the type afterwards.
# Text is the one representation every driver agrees on, it is the representation the value
# was written from, and it leaves this module with one parse path per column instead of one
# per driver. The timestamp stays typed because `TIMESTAMPTZ` renders in the session's zone
# and the driver's conversion is the one that respects it.
_SELECT_JOURNAL: Final = sa.text(
    """
    SELECT event_id::text      AS event_id,
           event_type,
           incident_id::text   AS incident_id,
           correlation_id::text AS correlation_id,
           occurred_at_utc,
           actor,
           operator_id,
           root_cause,
           trigger_id,
           trigger_unit,
           trigger_observed_value::text  AS trigger_observed_value,
           trigger_threshold_value::text AS trigger_threshold_value,
           trigger_detail,
           book_snapshot::text           AS book_snapshot
      FROM kill_switch_event
     ORDER BY occurred_at_utc, event_id
    """
)

_INSERT_EVENT: Final = sa.text(
    """
    INSERT INTO kill_switch_event (
        event_id, event_type, reason, actor, correlation_id, occurred_at_utc,
        incident_id, operator_id, root_cause, trigger_id, trigger_unit,
        trigger_observed_value, trigger_threshold_value, trigger_detail, book_snapshot
    ) VALUES (
        :event_id, :event_type, :reason, :actor, :correlation_id, :occurred_at_utc,
        :incident_id, :operator_id, :root_cause, :trigger_id, :trigger_unit,
        :trigger_observed_value, :trigger_threshold_value, :trigger_detail,
        cast(:book_snapshot as jsonb)
    )
    """
)


class KillSwitchJournal:
    """The append-only journal on PostgreSQL, read at boot and appended to on every trip."""

    __slots__ = ("_engine",)

    def __init__(self, engine: AsyncEngine) -> None:
        self._engine = engine

    async def read(self) -> JournalReadOutcome:
        """Every journal row, or the stated reason they could not be read.

        Never raises. That is not politeness: the caller must be unable to express "I
        could not tell, so assume nothing is wrong", and an exception is the shape that
        invites a `try/except` around the boot sequence which does exactly that.
        """
        try:
            async with self._engine.connect() as connection:
                rows = (await connection.execute(_SELECT_JOURNAL)).mappings().all()
        except (SQLAlchemyError, PostgresError, InterfaceError, OSError) as unreadable:
            # Exactly the classes that mean "the read did not happen", and there are four
            # rather than one for a reason worth stating: SQLAlchemy wraps what the driver
            # raises *while executing a statement* -- a revoked grant, a column a
            # mid-flight migration has not added yet -- but not what it raises while
            # *connecting*. A database that does not exist surfaces as a raw
            # `asyncpg.InvalidCatalogNameError`, straight past the wrapping, and a
            # `except SQLAlchemyError` alone would let it propagate and take a boot down
            # with a traceback instead of halting it with a reason. `OSError` is the
            # socket layer underneath both: nothing listening on the port at all.
            #
            # Nothing wider. A `MemoryError` here is not a journal condition, and catching
            # it as one would report "the kill switch cannot be read" for a process that
            # is failing for an entirely different reason.
            reason = _condensed(f"{type(unreadable).__name__}: {unreadable}")
            _LOG.critical("killswitch.journal_unreadable", reason=reason)
            return JournalUnreadable(reason=reason)

        try:
            events = tuple(_event_from_row(row) for row in rows)
        except DomainError as malformed:
            # One unparseable row makes the whole journal unreadable. Dropping it would
            # be dropping whichever row it happened to be, and the row that matters is
            # the TRIP -- so the system would come back trading on a journal it failed to
            # read. Halting is the recoverable direction.
            reason = _condensed(f"a journal row does not parse: {malformed}")
            _LOG.critical("killswitch.journal_unreadable", reason=reason)
            return JournalUnreadable(reason=reason)
        return JournalRead(events=events)

    async def append(self, event: KillSwitchEvent) -> None:
        """Append one journal row.

        Raises on failure, and the asymmetry with `read()` is deliberate. An unreadable
        journal is a state the system can hold safely -- halted. A trip that was decided
        and not recorded is not: the next boot would come back trading, so the write must
        be loud rather than absorbed into a value.
        """
        async with self._engine.begin() as connection:
            await connection.execute(_INSERT_EVENT, _row_for(event))


async def restore_kill_switch(
    read_journal: Callable[[], Awaitable[JournalReadOutcome]], *, gate: KillSwitchGate
) -> JournalReadOutcome:
    """Read the journal, adopt what it implies into `gate`, and hand back what was read.

    The boot step. `gate` starts halted by construction, so the window between process
    start and this call admits no order; this call is the only thing that can open it, and
    it opens it only for a journal that was read and holds no open incident.

    The derived state goes into the gate rather than being returned, and the *outcome* is
    returned instead. That is deliberate: the state is what the order path must read, and
    a caller holding it as a local would be holding a copy that a later trip does not
    move. The outcome is what a caller needs to tell "the journal says we are halted" from
    "the journal could not be read", which are the same `HALTED` state and different
    operational situations.

    Takes the read as a callable rather than a `KillSwitchJournal`, so the pre-flight
    checklist that calls it stays testable without a database and so a caller that already
    holds rows -- a replay of a past boot from the audit log -- can pass its own reader.
    """
    outcome = await read_journal()
    state = derive_state(outcome)
    gate.adopt(state)
    events_read = len(outcome.events) if isinstance(outcome, JournalRead) else 0
    if state.is_halted:
        _LOG.critical(
            "killswitch.boot_halted",
            halted_reason=state.halted_reason,
            incident_id=str(state.incident_id) if state.incident_id is not None else "",
            journal_events=events_read,
        )
    else:
        _LOG.info("killswitch.boot_trading", journal_events=events_read)
    return outcome


def _condensed(reason: str) -> str:
    """One line, capped. Multi-line driver messages make a log record unreadable."""
    single_line = " ".join(reason.split())
    if len(single_line) <= _MAX_REASON_LENGTH:
        return single_line
    return f"{single_line[:_MAX_REASON_LENGTH]}..."


def _row_for(event: KillSwitchEvent) -> Mapping[str, object]:
    """The insert parameters for one journal event.

    `reason` and `actor` are derived here and never read back. They are the columns 0005
    created for a log line, and keeping them populated means an operator running `psql`
    sees a legible table; keeping them out of the parse path means rewording one cannot
    change what a boot derives.
    """
    common: dict[str, object] = {
        "event_id": event.event_id,
        "incident_id": event.incident_id,
        "correlation_id": event.correlation_id,
        "occurred_at_utc": event.occurred_at_utc,
        "operator_id": None,
        "root_cause": None,
        "trigger_id": None,
        "trigger_unit": None,
        "trigger_observed_value": None,
        "trigger_threshold_value": None,
        "trigger_detail": None,
        "book_snapshot": None,
    }
    match event:
        case TripEvent():
            return common | {
                "event_type": _TRIPPED,
                "actor": event.actor,
                "reason": (
                    f"{event.trigger.trigger_id}: {event.trigger.observed_value} "
                    f"{event.trigger.unit} against a threshold of "
                    f"{event.trigger.threshold_value} ({event.trigger.detail})"
                ),
                "trigger_id": event.trigger.trigger_id,
                "trigger_unit": event.trigger.unit,
                "trigger_observed_value": event.trigger.observed_value,
                "trigger_threshold_value": event.trigger.threshold_value,
                "trigger_detail": event.trigger.detail,
                "book_snapshot": json.dumps(encode(event.snapshot), sort_keys=True),
            }
        case ArmEvent():
            return common | {
                "event_type": _ARMED,
                # The operator is the actor for both resume steps. An arm written by an
                # automated caller therefore carries a person's name in an append-only
                # row, which is the whole of what `human:<handle>` buys.
                "actor": event.operator_id,
                "reason": f"armed to resume incident {event.incident_id}",
                "operator_id": event.operator_id,
            }
        # `case _` rather than `case ResumeEvent()`, for the last member only. The wildcard
        # narrows to the one type left in the union, so mypy still proves the match
        # exhaustive and still fails on `.root_cause` the day a fourth event type is added
        # -- and there is no unreachable fall-through arm for a coverage report to point at
        # forever.
        case _:
            return common | {
                "event_type": _CLEARED,
                "actor": event.operator_id,
                "reason": event.root_cause,
                "operator_id": event.operator_id,
                "root_cause": event.root_cause,
            }


def _event_from_row(row: sa.RowMapping) -> KillSwitchEvent:
    """One row as the journal event it records, with every invariant re-checked.

    Every failure here is a `DomainError`, which `read()` turns into `JournalUnreadable`.
    A row is not trusted for being ours: it survived a migration, possibly several, and
    the domain constructors are where the invariants live.
    """
    event_type = _as_text(row, "event_type")
    match event_type:
        case "tripped":
            return TripEvent(
                event_id=_as_uuid(row, "event_id"),
                incident_id=_as_uuid(row, "incident_id"),
                correlation_id=_as_uuid(row, "correlation_id"),
                occurred_at_utc=_as_utc(row, "occurred_at_utc"),
                actor=_as_text(row, "actor"),
                trigger=TripTrigger(
                    trigger_id=_as_text(row, "trigger_id"),
                    unit=_as_text(row, "trigger_unit"),
                    observed_value=_as_decimal(row, "trigger_observed_value"),
                    threshold_value=_as_decimal(row, "trigger_threshold_value"),
                    detail=_as_text(row, "trigger_detail"),
                ),
                snapshot=decode(BookSnapshot, _as_json_object(row, "book_snapshot")),
            )
        case "armed":
            return ArmEvent(
                event_id=_as_uuid(row, "event_id"),
                incident_id=_as_uuid(row, "incident_id"),
                correlation_id=_as_uuid(row, "correlation_id"),
                occurred_at_utc=_as_utc(row, "occurred_at_utc"),
                operator_id=_as_text(row, "operator_id"),
            )
        case "cleared":
            return ResumeEvent(
                event_id=_as_uuid(row, "event_id"),
                incident_id=_as_uuid(row, "incident_id"),
                correlation_id=_as_uuid(row, "correlation_id"),
                occurred_at_utc=_as_utc(row, "occurred_at_utc"),
                operator_id=_as_text(row, "operator_id"),
                root_cause=_as_text(row, "root_cause"),
            )
        case _:
            # Reachable only from a row written after the CHECK vocabulary was widened by
            # a migration this code predates -- which is a deployment mid-flight, and is
            # the case that must halt rather than ignore the row it cannot classify.
            raise DomainError(f"unknown kill_switch_event.event_type {event_type!r}")


def _as_text(row: sa.RowMapping, column: str) -> str:
    candidate: object = row[column]
    if not isinstance(candidate, str):
        raise DomainError(f"{column} came back as {type(candidate).__name__} {candidate!r}")
    return candidate


def _as_uuid(row: sa.RowMapping, column: str) -> UUID:
    candidate: object = row[column]
    if not isinstance(candidate, str):
        raise DomainError(f"{column} came back as {type(candidate).__name__} {candidate!r}")
    try:
        return UUID(candidate)
    except ValueError as invalid:  # pragma: no cover - a uuid column renders nothing else
        raise DomainError(f"{column} is not a UUID: {candidate!r}") from invalid


def _as_utc(row: sa.RowMapping, column: str) -> datetime:
    candidate: object = row[column]
    if not isinstance(candidate, datetime):  # pragma: no cover - TIMESTAMPTZ always yields one
        raise DomainError(f"{column} came back as {type(candidate).__name__} {candidate!r}")
    if candidate.tzinfo is None or candidate.utcoffset() is None:  # pragma: no cover - as above
        raise DomainError(f"{column} came back naive: {candidate!r}")
    # TIMESTAMPTZ has no zone of its own; the driver renders it in the session's, which is
    # not guaranteed to be UTC. Normalising here is what keeps the domain's UTC invariant
    # true for a row read on a machine whose Postgres session says Europe/Istanbul.
    return candidate.astimezone(UTC)


def _as_decimal(row: sa.RowMapping, column: str) -> Decimal:
    candidate: object = row[column]
    if not isinstance(candidate, str):
        # Including a float, which is the failure this shape exists to make impossible:
        # the SELECT casts both numerics to text, so a float here would mean the cast was
        # removed -- and a value that has been through a float is not repairable by
        # widening the type afterwards.
        raise DomainError(f"{column} came back as {type(candidate).__name__} {candidate!r}")
    try:
        return Decimal(candidate)
    except ArithmeticError as invalid:  # pragma: no cover - NUMERIC renders nothing else
        raise DomainError(f"{column} is not a decimal: {candidate!r}") from invalid


def _as_json_object(row: sa.RowMapping, column: str) -> JsonValue:
    candidate: object = row[column]
    if not isinstance(candidate, str):
        raise DomainError(f"{column} came back as {type(candidate).__name__} {candidate!r}")
    try:
        parsed: object = json.loads(candidate)
    except json.JSONDecodeError as invalid:  # pragma: no cover - JSONB renders valid JSON
        raise DomainError(f"{column} is not JSON: {invalid}") from invalid
    if not isinstance(parsed, dict):
        raise DomainError(f"{column} is a {type(parsed).__name__}, not a JSON object")
    # The keys of a JSONB object are strings by construction, so the narrowing below is
    # about satisfying the type rather than about a case that can occur.
    return {str(key): _as_json_value(item) for key, item in parsed.items()}


def _as_json_value(candidate: object) -> JsonValue:
    if candidate is None or isinstance(candidate, str | bool | int):
        return candidate
    if isinstance(candidate, list):
        return [_as_json_value(item) for item in candidate]
    if isinstance(candidate, dict):
        return {str(key): _as_json_value(item) for key, item in candidate.items()}
    # `json.loads` produces floats for JSON numbers with a fractional part. Every Decimal
    # in a snapshot was encoded as a *string* by `fking.domain.encode`, so a float here
    # means the payload was written by something that bypassed the codec.
    raise DomainError(
        f"a snapshot field came back as {type(candidate).__name__} {candidate!r}; "
        f"every decimal in an encoded snapshot is a JSON string"
    )
