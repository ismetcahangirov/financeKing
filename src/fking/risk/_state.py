"""The kill switch's vocabulary: its journal events and its derived state.

Two things live here and nothing else does. The **journal** is an append-only sequence
of `TripEvent`, `ArmEvent` and `ResumeEvent` rows and is authoritative. The **state** is
derived from that sequence and is rebuildable from it at any moment, which is what makes
it safe for the state to be lost -- a restart recomputes it -- and unsafe for the journal
to be.

The type that carries the most weight is `JournalUnreadable`. A read of the journal
returns either the rows or a stated reason it could not be read, and there is no third
possibility and no empty-list fallback. The natural shape -- a function returning
`tuple[KillSwitchEvent, ...]` with a `try/except` that logs and returns `()` so startup
does not break -- reports "no trip has ever happened" for a revoked grant, a database
that is down, and a migration mid-flight. A safety mechanism whose unavailability means
"no restriction" is not a safety mechanism, so the unreadable case is a value the caller
must destructure rather than an exception it can swallow (issue #53).

Everything here is frozen and every field is validated at construction, for the reason
in `docs/rules/immutability.md`: these objects are read by the risk engine, the boot
sequence and the audit writer, and a mutable one makes the value each of them sees a
function of scheduling order.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta
from decimal import Decimal
from enum import StrEnum
from typing import Final
from uuid import UUID

from fking.domain import DomainError, JsonValue, Portfolio, encode

# A resume is attributable to a named person, and the namespace is how the row says so.
# This is attribution, not authentication -- an automated caller can spell a string --
# but it is attribution in an append-only table, which means an agent that resumed the
# kill switch has written "human:someone" over its own name and left it there. The
# authenticating front end is the dashboard's job (#102); this is the invariant the
# journal enforces regardless of which front end wrote the row.
HUMAN_OPERATOR_PREFIX: Final = "human:"

# Below this, a root cause is a shrug. Twenty characters does not make an explanation
# good; it makes an *empty* explanation impossible to submit by reflex, which is the
# only part of the resume procedure that cannot be satisfied by a script (issue #53).
MIN_ROOT_CAUSE_LENGTH: Final = 20

# An arm is a statement that the operator is about to resume, not a standing permission.
# FAILSAFE.md section 2.6: two steps, deliberately awkward, and the window is short
# enough that an arm left lying around from an earlier attempt has expired.
ARM_VALIDITY: Final = timedelta(seconds=120)


class KillSwitchStatus(StrEnum):
    """Whether the order path is open.

    Two members, not three. A `DEGRADED` or `UNKNOWN` member would need a rule about
    whether it blocks orders, and every reading of that rule under pressure is a
    reading of whether "unknown" is closer to open or to shut. It is shut.
    """

    TRADING = "trading"
    HALTED = "halted"


@dataclass(frozen=True, slots=True)
class TripTrigger:
    """What tripped the switch, in enough detail to argue with afterwards.

    `observed_value` and `threshold_value` are dimensionless on purpose and `unit`
    states which dimension applies, because the triggers do not share one: a drawdown
    trip is a fraction, a rejection-spike trip is a count, a spread trip is basis
    points. Naming the field `drawdown_fraction` would be honest for one trigger and a
    lie for the other two (`docs/rules/naming.md`).
    """

    trigger_id: str
    unit: str
    observed_value: Decimal
    threshold_value: Decimal
    detail: str

    def __post_init__(self) -> None:
        _require_text(self.trigger_id, "trigger_id")
        _require_text(self.unit, "unit")
        _require_text(self.detail, "detail")
        _require_exact_decimal(self.observed_value, "observed_value")
        _require_exact_decimal(self.threshold_value, "threshold_value")


@dataclass(frozen=True, slots=True)
class BookSnapshot:
    """The book as it stood when the switch tripped, recorded before any remediation.

    ADR 0014 gives up the ability to freeze-and-inspect: the flatten closes the book
    before an investigator sees it. This is the artefact that replaces it, which is why
    it is written inside the same append-only row as the trip and not fetched later.
    """

    portfolio: Portfolio
    open_client_order_ids: tuple[str, ...]
    protective_client_order_ids: tuple[str, ...]
    reconciled_at_utc: datetime | None
    reconciliation_is_clean: bool

    def __post_init__(self) -> None:
        for order_id in (*self.open_client_order_ids, *self.protective_client_order_ids):
            _require_text(order_id, "client_order_id")
        unknown = set(self.protective_client_order_ids) - set(self.open_client_order_ids)
        if unknown:
            raise DomainError(
                f"protective orders {sorted(unknown)} are not among the open orders; "
                f"a protective order the snapshot does not also list as open would be "
                f"exempted from cancellation without any record of having existed"
            )
        if self.reconciled_at_utc is not None:
            _require_utc(self.reconciled_at_utc, "reconciled_at_utc")
        elif self.reconciliation_is_clean:
            raise DomainError("a clean reconciliation must carry the time it completed")

    def as_json(self) -> JsonValue:
        """The snapshot in the shape the audit row stores."""
        return encode(self)


@dataclass(frozen=True, slots=True)
class TripEvent:
    """The switch closed. Appended before any remediation runs.

    A crash between this row and the cancellations leaves the system halted on the next
    boot, which is the recoverable failure. Remediating first and recording afterwards
    leaves a system that can crash into believing it is fine.
    """

    event_id: UUID
    incident_id: UUID
    correlation_id: UUID
    occurred_at_utc: datetime
    actor: str
    trigger: TripTrigger
    snapshot: BookSnapshot

    def __post_init__(self) -> None:
        _require_uuids(self, ("event_id", "incident_id", "correlation_id"))
        _require_utc(self.occurred_at_utc, "occurred_at_utc")
        _require_text(self.actor, "actor")


@dataclass(frozen=True, slots=True)
class ArmEvent:
    """An operator declared an intent to resume. Expires; grants nothing on its own."""

    event_id: UUID
    incident_id: UUID
    correlation_id: UUID
    occurred_at_utc: datetime
    operator_id: str

    def __post_init__(self) -> None:
        _require_uuids(self, ("event_id", "incident_id", "correlation_id"))
        _require_utc(self.occurred_at_utc, "occurred_at_utc")
        _require_human_operator(self.operator_id)

    @property
    def expires_at_utc(self) -> datetime:
        """When this arm stops authorising anything.

        Derived rather than stored: a stored expiry is a second number that can
        disagree with the first, and the disagreement always resolves in favour of the
        longer window.
        """
        return self.occurred_at_utc + ARM_VALIDITY


@dataclass(frozen=True, slots=True)
class ResumeEvent:
    """The switch reopened, with a name and a written explanation against it."""

    event_id: UUID
    incident_id: UUID
    correlation_id: UUID
    occurred_at_utc: datetime
    operator_id: str
    root_cause: str

    def __post_init__(self) -> None:
        _require_uuids(self, ("event_id", "incident_id", "correlation_id"))
        _require_utc(self.occurred_at_utc, "occurred_at_utc")
        _require_human_operator(self.operator_id)
        _require_text(self.root_cause, "root_cause")
        if len(self.root_cause.strip()) < MIN_ROOT_CAUSE_LENGTH:
            raise DomainError(
                f"root_cause must be at least {MIN_ROOT_CAUSE_LENGTH} characters; "
                f"got {len(self.root_cause.strip())}"
            )


type KillSwitchEvent = TripEvent | ArmEvent | ResumeEvent


@dataclass(frozen=True, slots=True)
class JournalRead:
    """The journal was read. `events` may legitimately be empty."""

    events: tuple[KillSwitchEvent, ...]


@dataclass(frozen=True, slots=True)
class JournalUnreadable:
    """The journal could not be read, and the reason is recorded rather than logged.

    Constructing this is the *only* way to report a failed read, so a caller cannot
    express "I could not tell, so assume nothing is wrong".
    """

    reason: str

    def __post_init__(self) -> None:
        _require_text(self.reason, "reason")


type JournalReadOutcome = JournalRead | JournalUnreadable


@dataclass(frozen=True, slots=True)
class KillSwitchState:
    """The derived, rebuildable view of the journal.

    Carries `halted_reason` even when trading, as `None`, rather than a separate type
    per status: the risk engine reads one attribute to decide, and every audit row that
    mentions the state can quote the reason without asking which shape it holds.
    """

    status: KillSwitchStatus
    incident_id: UUID | None
    tripped_at_utc: datetime | None
    trigger: TripTrigger | None
    halted_reason: str | None

    def __post_init__(self) -> None:
        if self.status is KillSwitchStatus.HALTED:
            if not self.halted_reason:
                raise DomainError("a halted kill switch must state why it is halted")
        elif (
            self.halted_reason is not None
            or self.incident_id is not None
            or self.tripped_at_utc is not None
            or self.trigger is not None
        ):
            raise DomainError(
                "a trading kill switch carries no incident: a state that is open for "
                "orders while naming a live incident is the shape a reader trusts "
                "the wrong half of"
            )
        if self.tripped_at_utc is not None:
            _require_utc(self.tripped_at_utc, "tripped_at_utc")

    @property
    def is_halted(self) -> bool:
        """Whether the order path is closed."""
        return self.status is KillSwitchStatus.HALTED


def _require_text(candidate: object, field_name: str) -> str:
    if not isinstance(candidate, str):
        raise DomainError(
            f"{field_name} must be a string, got {type(candidate).__name__} {candidate!r}"
        )
    if not candidate.strip():
        raise DomainError(f"{field_name} must not be blank")
    return candidate


def _require_exact_decimal(candidate: object, field_name: str) -> Decimal:
    if isinstance(candidate, float):
        raise DomainError(
            f"{field_name} must be a Decimal constructed from str, not a float; "
            f"got {candidate!r}, which is already rounded"
        )
    if not isinstance(candidate, Decimal):
        raise DomainError(
            f"{field_name} must be a Decimal, got {type(candidate).__name__} {candidate!r}"
        )
    if not candidate.is_finite():
        raise DomainError(f"{field_name} must be finite; got {candidate}")
    return candidate


def _require_utc(candidate: object, field_name: str) -> datetime:
    if not isinstance(candidate, datetime):
        raise DomainError(
            f"{field_name} must be a datetime, got {type(candidate).__name__} {candidate!r}"
        )
    if candidate.tzinfo is None or candidate.utcoffset() is None:
        raise DomainError(f"{field_name} must be timezone-aware; got naive {candidate!r}")
    if candidate.utcoffset() != timedelta(0):
        raise DomainError(f"{field_name} must be UTC; got offset {candidate.utcoffset()!r}")
    return candidate


def _require_uuids(event: object, field_names: tuple[str, ...]) -> None:
    for field_name in field_names:
        candidate = getattr(event, field_name)
        if not isinstance(candidate, UUID):
            raise DomainError(
                f"{field_name} must be a UUID, got {type(candidate).__name__} {candidate!r}"
            )


def _require_human_operator(candidate: object) -> str:
    operator_id = _require_text(candidate, "operator_id")
    if not operator_id.startswith(HUMAN_OPERATOR_PREFIX):
        raise DomainError(
            f"operator_id must name a person as {HUMAN_OPERATOR_PREFIX}<handle>; got "
            f"{operator_id!r}. A kill switch an automated actor can reset under its own "
            f"identity is not a kill switch"
        )
    if not operator_id[len(HUMAN_OPERATOR_PREFIX) :].strip():
        raise DomainError(f"operator_id {operator_id!r} names nobody after the prefix")
    return operator_id
