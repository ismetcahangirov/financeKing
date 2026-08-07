"""Properties of the kill switch that must hold over every journal a venue can produce.

Example-based tests confirm the interleavings someone thought of. The interleavings that
matter here are the ones nobody did: a resume arriving before its trip, two trips at the
same instant, a resume for an incident that never opened, a journal delivered in an order
the database did not promise.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal
from uuid import UUID, uuid4

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st

from fking.domain import Portfolio
from fking.risk import (
    ARM_VALIDITY,
    MIN_ROOT_CAUSE_LENGTH,
    RECONCILIATION_FRESHNESS,
    REQUIRED_RECOVERY_STEP,
    ArmEvent,
    BookSnapshot,
    JournalRead,
    JournalUnreadable,
    KillSwitchEvent,
    KillSwitchGate,
    KillSwitchStatus,
    KillSwitchTrippedError,
    ResumeEvent,
    ResumePreconditions,
    TripEvent,
    TripPolicy,
    TripStep,
    TripTrigger,
    derive_state,
    derive_state_from,
    orders_to_cancel,
    resume_refusals,
    trip_sequence,
)

pytestmark = [pytest.mark.property, pytest.mark.unit]

EPOCH = datetime(2026, 8, 1, tzinfo=UTC)
OPERATOR = "human:ismet"

_incident_ids: list[UUID] = [uuid4() for _ in range(3)]

_offsets = st.integers(min_value=0, max_value=60 * 24 * 40)  # up to forty days


def _trigger() -> TripTrigger:
    return TripTrigger(
        trigger_id="drawdown.daily",
        unit="fraction",
        observed_value=Decimal("0.061"),
        threshold_value=Decimal("0.05"),
        detail="equity fell past the daily limit",
    )


def _snapshot() -> BookSnapshot:
    return BookSnapshot(
        portfolio=Portfolio(as_of_utc=EPOCH, positions=(), cash_balances={}),
        open_client_order_ids=(),
        protective_client_order_ids=(),
        reconciled_at_utc=EPOCH,
        reconciliation_is_clean=True,
    )


trips = st.builds(
    TripEvent,
    event_id=st.uuids(),
    incident_id=st.sampled_from(_incident_ids),
    correlation_id=st.uuids(),
    occurred_at_utc=_offsets.map(lambda minutes: EPOCH + timedelta(minutes=minutes)),
    actor=st.just("risk.drawdown_monitor"),
    trigger=st.builds(_trigger),
    snapshot=st.builds(_snapshot),
)

resumes = st.builds(
    ResumeEvent,
    event_id=st.uuids(),
    incident_id=st.sampled_from(_incident_ids),
    correlation_id=st.uuids(),
    occurred_at_utc=_offsets.map(lambda minutes: EPOCH + timedelta(minutes=minutes)),
    operator_id=st.just(OPERATOR),
    root_cause=st.just("x" * MIN_ROOT_CAUSE_LENGTH),
)

arms = st.builds(
    ArmEvent,
    event_id=st.uuids(),
    incident_id=st.sampled_from(_incident_ids),
    correlation_id=st.uuids(),
    occurred_at_utc=_offsets.map(lambda minutes: EPOCH + timedelta(minutes=minutes)),
    operator_id=st.just(OPERATOR),
)

journals = st.lists(st.one_of(trips, resumes, arms), max_size=10)


def _sorted(events: list[KillSwitchEvent]) -> tuple[KillSwitchEvent, ...]:
    return tuple(sorted(events, key=lambda event: (event.occurred_at_utc, event.event_id)))


@given(events=journals)
@settings(max_examples=400)
def test_the_derived_state_does_not_depend_on_the_order_rows_arrive_in(
    events: list[KillSwitchEvent],
) -> None:
    """The journal is authoritative, not the cursor that read it. A fold whose verdict
    depends on delivery order can decide to trade because a row arrived late."""
    forwards = derive_state_from(tuple(events))
    backwards = derive_state_from(tuple(reversed(events)))
    assert forwards == backwards


@given(events=journals)
@settings(max_examples=400)
def test_an_unresumed_trip_always_halts(events: list[KillSwitchEvent]) -> None:
    ordered = _sorted(events)
    state = derive_state_from(ordered)

    open_incidents: set[UUID] = set()
    for event in ordered:
        if isinstance(event, TripEvent):
            open_incidents.add(event.incident_id)
        elif isinstance(event, ResumeEvent):
            open_incidents.discard(event.incident_id)

    assert state.is_halted == bool(open_incidents)
    # An ARM never moves the state; it is recorded so the two-step is auditable.
    assert (
        state.is_halted
        == derive_state_from(
            tuple(event for event in ordered if not isinstance(event, ArmEvent))
        ).is_halted
    )


@given(events=journals, reason=st.text(min_size=1).filter(lambda text: text.strip() != ""))
@settings(max_examples=200)
def test_an_unreadable_journal_halts_whatever_the_rows_would_have_said(
    events: list[KillSwitchEvent], reason: str
) -> None:
    """Whatever history exists, failing to read it is halted. This is the clause the
    natural implementation gets backwards, so it is asserted against every journal the
    readable path could have produced."""
    assert derive_state(JournalUnreadable(reason=reason)).status is KillSwitchStatus.HALTED
    _ = derive_state(JournalRead(events=tuple(events)))  # the readable path still works


@given(events=journals)
@settings(max_examples=400)
def test_a_gate_that_adopted_a_halted_state_admits_no_order(
    events: list[KillSwitchEvent],
) -> None:
    state = derive_state_from(tuple(events))
    gate = KillSwitchGate(state)
    if state.is_halted:
        with pytest.raises(KillSwitchTrippedError):
            gate.ensure_trading()
    else:
        gate.ensure_trading()


@given(
    on_trip_flatten=st.booleans(),
    venue_state_is_readable=st.booleans(),
    cancel_protective_orders=st.booleans(),
)
def test_every_trip_blocks_and_records_before_it_remediates(
    *, on_trip_flatten: bool, venue_state_is_readable: bool, cancel_protective_orders: bool
) -> None:
    """No policy combination can reorder the first three steps or drop one of them.
    Blocking and recording are not configurable; only the last step is."""
    steps = trip_sequence(
        TripPolicy(
            on_trip_flatten=on_trip_flatten,
            cancel_protective_orders=cancel_protective_orders,
        ),
        venue_state_is_readable=venue_state_is_readable,
    )
    assert steps[:3] == (
        TripStep.BLOCK_ORDER_ENTRY,
        TripStep.RECORD_TRIP_EVENT,
        TripStep.CANCEL_RESTING_ORDERS,
    )
    # The flatten never runs on a venue we could not read: ADR 0014 sources closing
    # quantities from the venue, and a guess can open a position rather than close one.
    assert (TripStep.FLATTEN_FROM_VENUE_STATE in steps) == (
        on_trip_flatten and venue_state_is_readable
    )


order_ids = st.lists(st.text(min_size=1, max_size=8).filter(lambda t: t.strip() != ""), max_size=6)


@given(open_ids=order_ids, protective_indices=st.lists(st.integers(0, 5), max_size=6))
def test_cancellation_never_touches_a_protective_order_under_the_default(
    open_ids: list[str], protective_indices: list[int]
) -> None:
    unique_open = tuple(dict.fromkeys(open_ids))
    protective = tuple(
        dict.fromkeys(
            unique_open[index] for index in protective_indices if index < len(unique_open)
        )
    )
    snapshot = BookSnapshot(
        portfolio=Portfolio(as_of_utc=EPOCH, positions=(), cash_balances={}),
        open_client_order_ids=unique_open,
        protective_client_order_ids=protective,
        reconciled_at_utc=EPOCH,
        reconciliation_is_clean=True,
    )
    cancelled = orders_to_cancel(snapshot, TripPolicy())
    assert set(cancelled).isdisjoint(protective)
    assert set(cancelled) | set(protective) == set(unique_open)
    assert orders_to_cancel(snapshot, TripPolicy(cancel_protective_orders=True)) == unique_open


@given(
    root_cause=st.text(max_size=MIN_ROOT_CAUSE_LENGTH * 2),
    arm_age_seconds=st.integers(min_value=-30, max_value=600),
    reconciliation_age_seconds=st.integers(min_value=0, max_value=3600),
    trigger_condition_still_true=st.booleans(),
    recovery_step_completed=st.integers(min_value=0, max_value=REQUIRED_RECOVERY_STEP),
)
@settings(max_examples=500)
def test_a_resume_is_authorised_only_when_every_condition_holds_at_once(
    root_cause: str,
    arm_age_seconds: int,
    reconciliation_age_seconds: int,
    *,
    trigger_condition_still_true: bool,
    recovery_step_completed: int,
) -> None:
    """The conjunction is the procedure. Any single condition failing must refuse, and
    no combination of the others may compensate for it."""
    now_utc = EPOCH + timedelta(hours=1)
    state = derive_state_from(
        (
            TripEvent(
                event_id=uuid4(),
                incident_id=_incident_ids[0],
                correlation_id=uuid4(),
                occurred_at_utc=EPOCH,
                actor="risk.drawdown_monitor",
                trigger=_trigger(),
                snapshot=_snapshot(),
            ),
        )
    )
    armed = ArmEvent(
        event_id=uuid4(),
        incident_id=_incident_ids[0],
        correlation_id=uuid4(),
        occurred_at_utc=now_utc - timedelta(seconds=arm_age_seconds),
        operator_id=OPERATOR,
    )
    refusals = resume_refusals(
        state=state,
        armed_by=armed,
        operator_id=OPERATOR,
        root_cause=root_cause,
        preconditions=ResumePreconditions(
            reconciliation_is_clean=True,
            reconciliation_completed_at_utc=now_utc - timedelta(seconds=reconciliation_age_seconds),
            trigger_condition_still_true=trigger_condition_still_true,
            recovery_step_completed=recovery_step_completed,
        ),
        now_utc=now_utc,
    )

    expected_ok = (
        len(root_cause.strip()) >= MIN_ROOT_CAUSE_LENGTH
        and 0 <= arm_age_seconds < ARM_VALIDITY.total_seconds()
        and reconciliation_age_seconds <= RECONCILIATION_FRESHNESS.total_seconds()
        and not trigger_condition_still_true
        and recovery_step_completed >= REQUIRED_RECOVERY_STEP
    )
    assert (refusals == ()) is expected_ok
