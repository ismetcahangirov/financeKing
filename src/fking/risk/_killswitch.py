"""The kill switch: the trip sequence, boot-halted derivation, and human-only resume.

Everything in this module is a pure function of its arguments plus one deliberately
mutable holder, `KillSwitchGate`. The split is the whole design:

- **Deciding** what the state is, what the trip sequence must be, and whether a resume
  is permitted are pure. They take the clock as a parameter and perform no I/O, so a
  trip is replayable from its journal rows months later (`.claude/rules/time-and-timezones.md`).
- **Holding** the current state so the order path can read it in one attribute access is
  infrastructure, and infrastructure is allowed to be mutable in the same narrow way
  `FrozenClock` is.

`KillSwitchGate.ensure_trading()` is synchronous and contains no `await`. That is not a
performance choice. `RiskEngine.decide()` calls it as its first statement, and because
neither the check nor the order construction that follows it yields to the event loop,
there is no interleaving in which the switch is tripped between the two. Zero windows is
a stronger claim than any millisecond budget, and it is only available while this
function stays synchronous -- adding an `await` here reintroduces exactly the gap an
event-bus subscriber would have had (issue #53).

What is *not* here, and why: the journal's PostgreSQL adapter and the flatten itself.
`fking.risk` performs no I/O, so the adapter is an `execution`-layer implementation of
`read_journal`-shaped calls that hands this module a `JournalReadOutcome`; and the
flatten needs an `ExecutionVenue` that reads position quantities from the venue rather
than from local records, per ADR 0014. `trip_sequence()` below already emits the flatten
step in its correct position, so wiring it is an implementation of a declared step
rather than a change to the sequence.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta
from enum import StrEnum, auto
from typing import Final
from uuid import UUID

from fking.platform.errors import FkingError
from fking.risk._state import (
    MIN_ROOT_CAUSE_LENGTH,
    ArmEvent,
    BookSnapshot,
    JournalRead,
    JournalReadOutcome,
    JournalUnreadable,
    KillSwitchEvent,
    KillSwitchState,
    KillSwitchStatus,
    ResumeEvent,
    TripEvent,
    TripTrigger,
)

# How stale a clean reconciliation may be at the moment of resume. Five minutes, from
# FAILSAFE.md section 2.6: long enough that the operator does not have to re-run it
# while typing, short enough that it describes the venue they are about to trade on.
RECONCILIATION_FRESHNESS: Final = timedelta(minutes=5)

# Recovery is a seven-step procedure (FAILSAFE.md section 3). Resume refuses below it
# rather than warning, because a partially recovered system resumes into the same
# condition that tripped it.
REQUIRED_RECOVERY_STEP: Final = 7

_BOOT_UNREADABLE_REASON: Final = (
    "the kill-switch journal could not be read, so the system cannot prove it is not "
    "halted; unknown is tripped"
)


class KillSwitchError(FkingError):
    """Base for the refusals this module raises."""


class KillSwitchTrippedError(KillSwitchError):
    """The order path was entered while the switch was closed.

    Raised, not returned, so that a caller cannot proceed by forgetting to check a
    boolean. It is caught in exactly one place -- the order path -- where it becomes an
    audited rejection.
    """

    def __init__(self, state: KillSwitchState) -> None:
        super().__init__(f"kill switch is halted: {state.halted_reason}")
        self.state = state


class ResumeRefusedError(KillSwitchError):
    """A resume did not satisfy the procedure. Every failing condition is named."""

    def __init__(self, refusals: tuple[str, ...]) -> None:
        super().__init__("resume refused: " + "; ".join(refusals))
        self.refusals = refusals


class TripStep(StrEnum):
    """The ordered stages of a trip, as values so the order is data a test can assert.

    A sequence expressed only as statements in a function body is a sequence that a
    later edit can reorder without any test noticing, and the ordering here is the
    safety property: block first so nothing new is admitted, record second so a crash
    leaves evidence, remediate third.
    """

    BLOCK_ORDER_ENTRY = auto()
    RECORD_TRIP_EVENT = auto()
    CANCEL_RESTING_ORDERS = auto()
    FLATTEN_FROM_VENUE_STATE = auto()
    RAISE_FLATTEN_BLOCKED = auto()


@dataclass(frozen=True, slots=True)
class TripPolicy:
    """The configurable part of a trip. Nothing here can widen what a trip blocks.

    `on_trip_flatten` defaults to true per ADR 0014, which supersedes FAILSAFE.md
    section 2.4's cancel-only default: the supervisor already flattens on an unhandled
    exception, and a system whose response to maximum uncertainty depends on which code
    path noticed it is not a safety design. Setting it false does not reopen order
    entry, does not skip cancellation, and does not skip the audit row -- it removes one
    remediation step and nothing else, which is why it is safe for it to be
    configurable at all (`.claude/rules/safety-kernel.md` on flags that bypass gates).
    """

    on_trip_flatten: bool = True
    cancel_protective_orders: bool = False


@dataclass(frozen=True, slots=True)
class ResumePreconditions:
    """The world as the resume procedure needs to observe it.

    Every field is supplied by the caller rather than measured here, because measuring
    would make the authorisation a function of when it ran. The caller that gathers
    these is auditable; this function is replayable.
    """

    reconciliation_is_clean: bool
    reconciliation_completed_at_utc: datetime | None
    trigger_condition_still_true: bool
    recovery_step_completed: int


def derive_state(outcome: JournalReadOutcome) -> KillSwitchState:
    """The current state, from the journal alone.

    Three behaviours, in decreasing order of how often they are got wrong:

    1. An unreadable journal is `HALTED`. Not an exception, not an empty list, not a
       default of "open so startup does not break".
    2. A `TRIP` with no later `RESUME` is `HALTED` regardless of elapsed time. A restart
       is not a reset, and neither is thirty days.
    3. An `ARM` grants nothing. It appears in the journal so the two-step is auditable,
       and it moves no state.
    """
    if isinstance(outcome, JournalUnreadable):
        return KillSwitchState(
            status=KillSwitchStatus.HALTED,
            incident_id=None,
            tripped_at_utc=None,
            trigger=None,
            halted_reason=f"{_BOOT_UNREADABLE_REASON} ({outcome.reason})",
        )

    # Per incident, not a single "current trip". Correlated conditions trip together --
    # a testnet wipe presents as a reconciliation divergence and a balance collapse at
    # once -- so two incidents are routinely open, and a RESUME clears exactly the one it
    # names. Tracking one trip and clearing it on any resume reports TRADING while a
    # second incident is still open, which a Hypothesis counterexample found before this
    # ever ran against a venue.
    open_trips: dict[UUID, TripEvent] = {}
    for event in _in_journal_order(outcome.events):
        if isinstance(event, TripEvent):
            # The earliest trip of an incident owns it: that is the one whose snapshot
            # precedes the remediation, so it is the one an investigation needs.
            open_trips.setdefault(event.incident_id, event)
        elif isinstance(event, ResumeEvent):
            open_trips.pop(event.incident_id, None)

    open_trip = min(
        open_trips.values(),
        key=lambda candidate: (candidate.occurred_at_utc, candidate.event_id),
        default=None,
    )
    if open_trip is None:
        return KillSwitchState(
            status=KillSwitchStatus.TRADING,
            incident_id=None,
            tripped_at_utc=None,
            trigger=None,
            halted_reason=None,
        )
    return KillSwitchState(
        status=KillSwitchStatus.HALTED,
        incident_id=open_trip.incident_id,
        tripped_at_utc=open_trip.occurred_at_utc,
        trigger=open_trip.trigger,
        halted_reason=(
            f"tripped by {open_trip.trigger.trigger_id}: "
            f"{open_trip.trigger.observed_value} {open_trip.trigger.unit} against a "
            f"threshold of {open_trip.trigger.threshold_value} "
            f"({open_trip.trigger.detail})"
        ),
    )


def trip_sequence(policy: TripPolicy, *, venue_state_is_readable: bool) -> tuple[TripStep, ...]:
    """The stages a trip runs, in order.

    Order entry is blocked and the trip row is recorded unconditionally; cancellation is
    unconditional; the flatten is last and is skipped when the venue cannot be read.
    ADR 0014 is explicit that the flatten's quantities come from the venue and never
    from local position records -- a reconciliation-divergence trip is precisely the
    case where the local record is the thing under suspicion, and closing from it can
    open a position rather than close one. When the venue cannot be read the sequence
    ends in `RAISE_FLATTEN_BLOCKED`: halted, positions open, paged at CRITICAL.
    """
    steps: list[TripStep] = [
        TripStep.BLOCK_ORDER_ENTRY,
        TripStep.RECORD_TRIP_EVENT,
        TripStep.CANCEL_RESTING_ORDERS,
    ]
    if policy.on_trip_flatten:
        steps.append(
            TripStep.FLATTEN_FROM_VENUE_STATE
            if venue_state_is_readable
            else TripStep.RAISE_FLATTEN_BLOCKED
        )
    return tuple(steps)


def orders_to_cancel(snapshot: BookSnapshot, policy: TripPolicy) -> tuple[str, ...]:
    """Which resting orders the cancellation stage submits cancels for.

    Protective orders resting at the venue at invalidation levels are exempt under the
    default. They are the one category of resting order whose presence reduces exposure,
    and cancelling them converts a bounded position into an unbounded one at the moment
    the system has decided it cannot supervise itself. That exemption is the operational
    payoff for requiring an invalidation level on every signal in the first place.
    """
    if policy.cancel_protective_orders:
        return snapshot.open_client_order_ids
    protective = frozenset(snapshot.protective_client_order_ids)
    return tuple(
        order_id for order_id in snapshot.open_client_order_ids if order_id not in protective
    )


def arm(
    *,
    event_id: UUID,
    incident_id: UUID,
    correlation_id: UUID,
    operator_id: str,
    now_utc: datetime,
) -> ArmEvent:
    """Step one of two. Records an intent; authorises nothing by itself."""
    return ArmEvent(
        event_id=event_id,
        incident_id=incident_id,
        correlation_id=correlation_id,
        occurred_at_utc=now_utc,
        operator_id=operator_id,
    )


def resume_refusals(  # noqa: PLR0913, PLR0912 - one parameter per pre-registered
    # condition and one branch per refusal. Collapsing them into a settings object would
    # let a caller omit a condition by omitting a field, and collapsing the branches
    # would report the first refusal instead of all of them.
    *,
    state: KillSwitchState,
    armed_by: ArmEvent | None,
    operator_id: str,
    root_cause: str,
    preconditions: ResumePreconditions,
    now_utc: datetime,
) -> tuple[str, ...]:
    """Every reason this resume is refused, or an empty tuple.

    All refusals rather than the first, because a resume attempt is a stop-the-world
    window and three attempts costs three of them.

    The root-cause clause is the one that matters. The rest are mechanical checks a
    script could satisfy; a written explanation is the point at which a person has to
    have understood the incident, and it is the only condition here that cannot be
    automated by someone in a hurry.
    """
    refusals: list[str] = []

    if not state.is_halted:
        refusals.append("the kill switch is not halted, so there is nothing to resume")
    if state.incident_id is None:
        refusals.append("no incident is open")

    if armed_by is None:
        refusals.append("no arm precedes this resume; arm then resume, within 120 seconds")
    else:
        if state.incident_id is not None and armed_by.incident_id != state.incident_id:
            refusals.append(
                f"the arm names incident {armed_by.incident_id}, the open incident is "
                f"{state.incident_id}"
            )
        if now_utc >= armed_by.expires_at_utc:
            refusals.append(
                f"the arm expired at {armed_by.expires_at_utc.isoformat()}; it is now "
                f"{now_utc.isoformat()}"
            )
        if now_utc < armed_by.occurred_at_utc:
            refusals.append("the resume is timestamped before its arm")
        if armed_by.operator_id != operator_id:
            refusals.append(
                f"the arm was recorded by {armed_by.operator_id} and the resume by "
                f"{operator_id}; both steps are one person's"
            )

    if not operator_id.strip():
        refusals.append("no operator identity was recorded")

    stripped_root_cause = root_cause.strip()
    if not stripped_root_cause:
        refusals.append("the root cause is empty")
    elif len(stripped_root_cause) < MIN_ROOT_CAUSE_LENGTH:
        refusals.append(
            f"the root cause is {len(stripped_root_cause)} characters; at least "
            f"{MIN_ROOT_CAUSE_LENGTH} are required"
        )

    if not preconditions.reconciliation_is_clean:
        refusals.append("the most recent reconciliation was not clean")
    elif preconditions.reconciliation_completed_at_utc is None:
        refusals.append("no reconciliation completion time was recorded")
    elif now_utc - preconditions.reconciliation_completed_at_utc > RECONCILIATION_FRESHNESS:
        refusals.append(
            f"the clean reconciliation completed at "
            f"{preconditions.reconciliation_completed_at_utc.isoformat()}, more than "
            f"{RECONCILIATION_FRESHNESS} ago"
        )

    if preconditions.trigger_condition_still_true:
        refusals.append("the condition that tripped the switch still evaluates true")

    if preconditions.recovery_step_completed < REQUIRED_RECOVERY_STEP:
        refusals.append(
            f"recovery reached step {preconditions.recovery_step_completed} of "
            f"{REQUIRED_RECOVERY_STEP}"
        )

    return tuple(refusals)


def resume(  # noqa: PLR0913 - see resume_refusals; the parameters are the procedure
    *,
    event_id: UUID,
    correlation_id: UUID,
    state: KillSwitchState,
    armed_by: ArmEvent | None,
    operator_id: str,
    root_cause: str,
    preconditions: ResumePreconditions,
    now_utc: datetime,
) -> ResumeEvent:
    """Step two of two. Raises `ResumeRefusedError` unless every condition holds.

    There is no `force`, no `skip_checks`, and no settings field that reaches this
    function. Adding one would be adding a configuration flag that bypasses a gate,
    which `CLAUDE.md` section 11 names as an anti-pattern precisely because gates exist
    for the person who is in a hurry later.
    """
    refusals = resume_refusals(
        state=state,
        armed_by=armed_by,
        operator_id=operator_id,
        root_cause=root_cause,
        preconditions=preconditions,
        now_utc=now_utc,
    )
    if refusals:
        raise ResumeRefusedError(refusals)
    if state.incident_id is None:  # pragma: no cover - refused above; narrows for mypy
        raise ResumeRefusedError(("no incident is open",))
    return ResumeEvent(
        event_id=event_id,
        incident_id=state.incident_id,
        correlation_id=correlation_id,
        occurred_at_utc=now_utc,
        operator_id=operator_id,
        root_cause=root_cause,
    )


def trip(  # noqa: PLR0913 - every field is required for the audit row to be
    # reconstructable months later without application memory (ARCHITECTURE.md 11)
    *,
    event_id: UUID,
    incident_id: UUID,
    correlation_id: UUID,
    actor: str,
    trigger: TripTrigger,
    snapshot: BookSnapshot,
    now_utc: datetime,
) -> TripEvent:
    """The row that is appended before any remediation runs."""
    return TripEvent(
        event_id=event_id,
        incident_id=incident_id,
        correlation_id=correlation_id,
        occurred_at_utc=now_utc,
        actor=actor,
        trigger=trigger,
        snapshot=snapshot,
    )


class KillSwitchGate:
    """The in-process holder the order path reads.

    Mutable, and deliberately so: it is infrastructure, not a domain object. The state
    it holds is immutable, so a caller that reads it holds a value nothing can change
    underneath it.

    It starts halted. A gate constructed before the journal has been read must not admit
    an order, and a default of `TRADING` makes the window between construction and the
    first `adopt()` an open one -- which is the boot-halted bug wearing a different hat.
    """

    __slots__ = ("_state",)

    def __init__(self, state: KillSwitchState | None = None) -> None:
        self._state = state if state is not None else boot_halted_state()

    @property
    def state(self) -> KillSwitchState:
        """The current state. A frozen value; safe to hold across an await."""
        return self._state

    def adopt(self, state: KillSwitchState) -> None:
        """Replace the held state, e.g. after a journal read or a trip."""
        self._state = state

    def ensure_trading(self) -> None:
        """Raise `KillSwitchTrippedError` if the order path is closed.

        Synchronous by contract. See the module docstring: an `await` anywhere between
        this read and the construction of an `Order` reintroduces the interleaving this
        design exists to remove.
        """
        current = self._state
        if current.status is KillSwitchStatus.HALTED:
            raise KillSwitchTrippedError(current)


def boot_halted_state(reason: str | None = None) -> KillSwitchState:
    """The state a process holds before it has proved it may trade."""
    return KillSwitchState(
        status=KillSwitchStatus.HALTED,
        incident_id=None,
        tripped_at_utc=None,
        trigger=None,
        halted_reason=reason or "the kill-switch journal has not been read yet",
    )


def _in_journal_order(events: tuple[KillSwitchEvent, ...]) -> tuple[KillSwitchEvent, ...]:
    """Journal rows sorted by occurrence, with the event id breaking ties.

    Sorting here rather than trusting the reader's ordering: two rows can share a
    timestamp, and a fold over TRIP/RESUME whose result depends on which of two
    simultaneous rows arrives first is a fold that can decide to trade.
    """
    return tuple(sorted(events, key=lambda event: (event.occurred_at_utc, event.event_id)))


def derive_state_from(events: tuple[KillSwitchEvent, ...]) -> KillSwitchState:
    """`derive_state` for a caller that already holds the rows."""
    return derive_state(JournalRead(events=events))
