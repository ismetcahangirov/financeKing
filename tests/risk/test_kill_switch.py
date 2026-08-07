"""The kill switch's refusals, stated one behaviour per test.

These are the assertions issue #53 names as load-bearing: unknown boots halted, a trip
with no resume survives an arbitrary gap, and each resume precondition is refused
separately so a failure names which one moved.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import cast
from uuid import UUID, uuid4

import pytest

from fking.domain import DomainError, Portfolio
from fking.risk import (
    ARM_VALIDITY,
    MIN_ROOT_CAUSE_LENGTH,
    REQUIRED_RECOVERY_STEP,
    ArmEvent,
    BookSnapshot,
    JournalRead,
    JournalUnreadable,
    KillSwitchGate,
    KillSwitchState,
    KillSwitchStatus,
    KillSwitchTrippedError,
    ResumeEvent,
    ResumePreconditions,
    ResumeRefusedError,
    TripEvent,
    TripPolicy,
    TripStep,
    TripTrigger,
    arm,
    boot_halted_state,
    derive_state,
    derive_state_from,
    orders_to_cancel,
    resume,
    resume_refusals,
    trip,
    trip_sequence,
)

pytestmark = pytest.mark.unit

TRIPPED_AT = datetime(2026, 8, 1, 3, 14, tzinfo=UTC)
OPERATOR = "human:ismet"
GOOD_ROOT_CAUSE = "stale funding feed drove the drawdown estimator negative"


def _trigger() -> TripTrigger:
    return TripTrigger(
        trigger_id="drawdown.daily",
        unit="fraction",
        observed_value=Decimal("0.061"),
        threshold_value=Decimal("0.05"),
        detail="equity fell 6.1% against a 5% daily limit",
    )


def _snapshot(
    *,
    open_client_order_ids: tuple[str, ...] = ("fk-a", "fk-b"),
    protective_client_order_ids: tuple[str, ...] = ("fk-b",),
) -> BookSnapshot:
    return BookSnapshot(
        portfolio=Portfolio(as_of_utc=TRIPPED_AT, positions=(), cash_balances={}),
        open_client_order_ids=open_client_order_ids,
        protective_client_order_ids=protective_client_order_ids,
        reconciled_at_utc=TRIPPED_AT - timedelta(minutes=1),
        reconciliation_is_clean=True,
    )


def _trip_event(
    *, incident_id: UUID | None = None, occurred_at_utc: datetime = TRIPPED_AT
) -> TripEvent:
    return trip(
        event_id=uuid4(),
        incident_id=incident_id or uuid4(),
        correlation_id=uuid4(),
        actor="risk.drawdown_monitor",
        trigger=_trigger(),
        snapshot=_snapshot(),
        now_utc=occurred_at_utc,
    )


def _preconditions(
    *,
    reconciliation_is_clean: bool = True,
    reconciliation_completed_at_utc: datetime | None = None,
    trigger_condition_still_true: bool = False,
    recovery_step_completed: int = REQUIRED_RECOVERY_STEP,
) -> ResumePreconditions:
    return ResumePreconditions(
        reconciliation_is_clean=reconciliation_is_clean,
        reconciliation_completed_at_utc=reconciliation_completed_at_utc,
        trigger_condition_still_true=trigger_condition_still_true,
        recovery_step_completed=recovery_step_completed,
    )


# --------------------------------------------------------------------------- boot


def test_an_unreadable_journal_boots_halted_rather_than_open() -> None:
    state = derive_state(JournalUnreadable(reason="permission denied for kill_switch_event"))
    assert state.status is KillSwitchStatus.HALTED
    assert "unknown is tripped" in (state.halted_reason or "")
    assert "permission denied" in (state.halted_reason or "")


def test_an_empty_journal_is_trading() -> None:
    assert derive_state(JournalRead(events=())).status is KillSwitchStatus.TRADING


def test_a_trip_with_no_resume_still_halts_after_thirty_days() -> None:
    tripped = _trip_event()
    state = derive_state_from((tripped,))
    assert state.is_halted
    # A restart is not a reset, and neither is elapsed time: nothing in derive_state
    # reads a clock, so there is no duration that could expire the trip.
    assert state.tripped_at_utc == TRIPPED_AT


def test_a_resume_naming_another_incident_does_not_clear_this_one() -> None:
    """Two incidents can be open at once; clearing the wrong one silently is the shape
    a hurried operator produces, so the fold matches on incident id rather than on
    "the most recent resume"."""
    tripped = _trip_event()
    mismatched = ResumeEvent(
        event_id=uuid4(),
        incident_id=uuid4(),
        correlation_id=uuid4(),
        occurred_at_utc=TRIPPED_AT + timedelta(minutes=5),
        operator_id=OPERATOR,
        root_cause=GOOD_ROOT_CAUSE,
    )
    assert derive_state_from((tripped, mismatched)).is_halted


def test_a_gate_admits_nothing_before_the_journal_has_been_read() -> None:
    gate = KillSwitchGate()
    with pytest.raises(KillSwitchTrippedError):
        gate.ensure_trading()
    assert gate.state.halted_reason == boot_halted_state().halted_reason


def test_a_gate_that_adopted_a_trading_state_admits_orders() -> None:
    gate = KillSwitchGate()
    gate.adopt(derive_state(JournalRead(events=())))
    gate.ensure_trading()  # does not raise


# --------------------------------------------------------------------- trip sequence


def test_the_trip_records_before_it_remediates() -> None:
    steps = trip_sequence(TripPolicy(), venue_state_is_readable=True)
    assert steps == (
        TripStep.BLOCK_ORDER_ENTRY,
        TripStep.RECORD_TRIP_EVENT,
        TripStep.CANCEL_RESTING_ORDERS,
        TripStep.FLATTEN_FROM_VENUE_STATE,
    )
    assert steps.index(TripStep.RECORD_TRIP_EVENT) < steps.index(TripStep.CANCEL_RESTING_ORDERS)


def test_an_unreadable_venue_blocks_the_flatten_instead_of_guessing_quantities() -> None:
    steps = trip_sequence(TripPolicy(), venue_state_is_readable=False)
    assert steps[-1] is TripStep.RAISE_FLATTEN_BLOCKED
    assert TripStep.FLATTEN_FROM_VENUE_STATE not in steps


def test_disabling_the_flatten_still_blocks_and_still_records() -> None:
    steps = trip_sequence(TripPolicy(on_trip_flatten=False), venue_state_is_readable=True)
    assert steps == (
        TripStep.BLOCK_ORDER_ENTRY,
        TripStep.RECORD_TRIP_EVENT,
        TripStep.CANCEL_RESTING_ORDERS,
    )


def test_protective_orders_survive_the_cancellation_by_default() -> None:
    assert orders_to_cancel(_snapshot(), TripPolicy()) == ("fk-a",)


def test_protective_orders_are_cancelled_when_that_is_asked_for() -> None:
    assert orders_to_cancel(_snapshot(), TripPolicy(cancel_protective_orders=True)) == (
        "fk-a",
        "fk-b",
    )


def test_a_protective_order_absent_from_the_open_orders_is_refused() -> None:
    with pytest.raises(DomainError, match="not among the open orders"):
        _snapshot(open_client_order_ids=("fk-a",), protective_client_order_ids=("fk-z",))


def test_a_clean_reconciliation_must_carry_its_completion_time() -> None:
    with pytest.raises(DomainError, match="must carry the time it completed"):
        BookSnapshot(
            portfolio=Portfolio(as_of_utc=TRIPPED_AT, positions=(), cash_balances={}),
            open_client_order_ids=(),
            protective_client_order_ids=(),
            reconciled_at_utc=None,
            reconciliation_is_clean=True,
        )


def test_the_snapshot_serialises_for_the_audit_row() -> None:
    encoded = _snapshot().as_json()
    assert isinstance(encoded, dict)
    assert "portfolio" in encoded


# ------------------------------------------------------------------------- resume


def _halted_state() -> KillSwitchState:
    return derive_state_from((_trip_event(),))


def _incident_of(state: KillSwitchState) -> UUID:
    """The open incident id, narrowed. A halted state always carries one."""
    assert state.incident_id is not None
    return state.incident_id


def test_an_empty_root_cause_is_refused() -> None:
    state = _halted_state()
    armed = arm(
        event_id=uuid4(),
        incident_id=_incident_of(state),
        correlation_id=uuid4(),
        operator_id=OPERATOR,
        now_utc=TRIPPED_AT,
    )
    refusals = resume_refusals(
        state=state,
        armed_by=armed,
        operator_id=OPERATOR,
        root_cause="   ",
        preconditions=_preconditions(reconciliation_completed_at_utc=TRIPPED_AT),
        now_utc=TRIPPED_AT + timedelta(seconds=30),
    )
    assert "the root cause is empty" in refusals


def test_a_root_cause_one_character_short_is_refused() -> None:
    state = _halted_state()
    armed = arm(
        event_id=uuid4(),
        incident_id=_incident_of(state),
        correlation_id=uuid4(),
        operator_id=OPERATOR,
        now_utc=TRIPPED_AT,
    )
    nineteen = "x" * (MIN_ROOT_CAUSE_LENGTH - 1)
    refusals = resume_refusals(
        state=state,
        armed_by=armed,
        operator_id=OPERATOR,
        root_cause=nineteen,
        preconditions=_preconditions(reconciliation_completed_at_utc=TRIPPED_AT),
        now_utc=TRIPPED_AT + timedelta(seconds=30),
    )
    assert any("19 characters" in refusal for refusal in refusals)


def test_a_reconciliation_older_than_five_minutes_is_refused() -> None:
    state = _halted_state()
    now_utc = TRIPPED_AT + timedelta(minutes=30)
    armed = arm(
        event_id=uuid4(),
        incident_id=_incident_of(state),
        correlation_id=uuid4(),
        operator_id=OPERATOR,
        now_utc=now_utc,
    )
    refusals = resume_refusals(
        state=state,
        armed_by=armed,
        operator_id=OPERATOR,
        root_cause=GOOD_ROOT_CAUSE,
        preconditions=_preconditions(
            reconciliation_completed_at_utc=now_utc - timedelta(minutes=6)
        ),
        now_utc=now_utc + timedelta(seconds=10),
    )
    assert any("more than" in refusal for refusal in refusals)


def test_a_trigger_condition_that_is_still_true_is_refused() -> None:
    state = _halted_state()
    armed = arm(
        event_id=uuid4(),
        incident_id=_incident_of(state),
        correlation_id=uuid4(),
        operator_id=OPERATOR,
        now_utc=TRIPPED_AT,
    )
    refusals = resume_refusals(
        state=state,
        armed_by=armed,
        operator_id=OPERATOR,
        root_cause=GOOD_ROOT_CAUSE,
        preconditions=_preconditions(
            reconciliation_completed_at_utc=TRIPPED_AT,
            trigger_condition_still_true=True,
        ),
        now_utc=TRIPPED_AT + timedelta(seconds=30),
    )
    assert "the condition that tripped the switch still evaluates true" in refusals


def test_an_expired_arm_is_refused() -> None:
    state = _halted_state()
    armed = arm(
        event_id=uuid4(),
        incident_id=_incident_of(state),
        correlation_id=uuid4(),
        operator_id=OPERATOR,
        now_utc=TRIPPED_AT,
    )
    refusals = resume_refusals(
        state=state,
        armed_by=armed,
        operator_id=OPERATOR,
        root_cause=GOOD_ROOT_CAUSE,
        preconditions=_preconditions(reconciliation_completed_at_utc=TRIPPED_AT + ARM_VALIDITY),
        now_utc=TRIPPED_AT + ARM_VALIDITY,
    )
    assert any("expired" in refusal for refusal in refusals)


def test_a_resume_without_an_arm_is_refused() -> None:
    refusals = resume_refusals(
        state=_halted_state(),
        armed_by=None,
        operator_id=OPERATOR,
        root_cause=GOOD_ROOT_CAUSE,
        preconditions=_preconditions(reconciliation_completed_at_utc=TRIPPED_AT),
        now_utc=TRIPPED_AT + timedelta(seconds=30),
    )
    assert any("no arm precedes this resume" in refusal for refusal in refusals)


def test_a_second_operator_cannot_complete_the_first_operators_arm() -> None:
    state = _halted_state()
    armed = arm(
        event_id=uuid4(),
        incident_id=_incident_of(state),
        correlation_id=uuid4(),
        operator_id=OPERATOR,
        now_utc=TRIPPED_AT,
    )
    refusals = resume_refusals(
        state=state,
        armed_by=armed,
        operator_id="human:someone-else",
        root_cause=GOOD_ROOT_CAUSE,
        preconditions=_preconditions(reconciliation_completed_at_utc=TRIPPED_AT),
        now_utc=TRIPPED_AT + timedelta(seconds=30),
    )
    assert any("both steps are one person's" in refusal for refusal in refusals)


def test_an_arm_for_a_different_incident_is_refused() -> None:
    state = _halted_state()
    armed = arm(
        event_id=uuid4(),
        incident_id=uuid4(),
        correlation_id=uuid4(),
        operator_id=OPERATOR,
        now_utc=TRIPPED_AT,
    )
    refusals = resume_refusals(
        state=state,
        armed_by=armed,
        operator_id=OPERATOR,
        root_cause=GOOD_ROOT_CAUSE,
        preconditions=_preconditions(reconciliation_completed_at_utc=TRIPPED_AT),
        now_utc=TRIPPED_AT + timedelta(seconds=30),
    )
    assert any("the open incident is" in refusal for refusal in refusals)


def test_a_resume_timestamped_before_its_arm_is_refused() -> None:
    state = _halted_state()
    armed = arm(
        event_id=uuid4(),
        incident_id=_incident_of(state),
        correlation_id=uuid4(),
        operator_id=OPERATOR,
        now_utc=TRIPPED_AT,
    )
    refusals = resume_refusals(
        state=state,
        armed_by=armed,
        operator_id=OPERATOR,
        root_cause=GOOD_ROOT_CAUSE,
        preconditions=_preconditions(reconciliation_completed_at_utc=TRIPPED_AT),
        now_utc=TRIPPED_AT - timedelta(seconds=1),
    )
    assert "the resume is timestamped before its arm" in refusals


def test_incomplete_recovery_is_refused() -> None:
    state = _halted_state()
    armed = arm(
        event_id=uuid4(),
        incident_id=_incident_of(state),
        correlation_id=uuid4(),
        operator_id=OPERATOR,
        now_utc=TRIPPED_AT,
    )
    refusals = resume_refusals(
        state=state,
        armed_by=armed,
        operator_id=OPERATOR,
        root_cause=GOOD_ROOT_CAUSE,
        preconditions=_preconditions(
            reconciliation_completed_at_utc=TRIPPED_AT, recovery_step_completed=6
        ),
        now_utc=TRIPPED_AT + timedelta(seconds=30),
    )
    assert any("recovery reached step 6" in refusal for refusal in refusals)


def test_an_unclean_or_untimed_reconciliation_is_refused() -> None:
    state = _halted_state()
    armed = arm(
        event_id=uuid4(),
        incident_id=_incident_of(state),
        correlation_id=uuid4(),
        operator_id=OPERATOR,
        now_utc=TRIPPED_AT,
    )
    unclean = resume_refusals(
        state=state,
        armed_by=armed,
        operator_id=OPERATOR,
        root_cause=GOOD_ROOT_CAUSE,
        preconditions=_preconditions(reconciliation_is_clean=False),
        now_utc=TRIPPED_AT + timedelta(seconds=30),
    )
    assert "the most recent reconciliation was not clean" in unclean

    untimed = resume_refusals(
        state=state,
        armed_by=armed,
        operator_id=OPERATOR,
        root_cause=GOOD_ROOT_CAUSE,
        preconditions=_preconditions(reconciliation_completed_at_utc=None),
        now_utc=TRIPPED_AT + timedelta(seconds=30),
    )
    assert "no reconciliation completion time was recorded" in untimed


def test_resuming_a_trading_switch_is_refused() -> None:
    refusals = resume_refusals(
        state=derive_state(JournalRead(events=())),
        armed_by=None,
        operator_id=OPERATOR,
        root_cause=GOOD_ROOT_CAUSE,
        preconditions=_preconditions(reconciliation_completed_at_utc=TRIPPED_AT),
        now_utc=TRIPPED_AT,
    )
    assert "the kill switch is not halted, so there is nothing to resume" in refusals
    assert "no incident is open" in refusals


def test_a_satisfied_resume_records_the_operator_and_the_root_cause() -> None:
    state = _halted_state()
    now_utc = TRIPPED_AT + timedelta(seconds=45)
    armed = arm(
        event_id=uuid4(),
        incident_id=_incident_of(state),
        correlation_id=uuid4(),
        operator_id=OPERATOR,
        now_utc=TRIPPED_AT,
    )
    resumed = resume(
        event_id=uuid4(),
        correlation_id=uuid4(),
        state=state,
        armed_by=armed,
        operator_id=OPERATOR,
        root_cause=GOOD_ROOT_CAUSE,
        preconditions=_preconditions(reconciliation_completed_at_utc=TRIPPED_AT),
        now_utc=now_utc,
    )
    assert resumed.operator_id == OPERATOR
    assert resumed.root_cause == GOOD_ROOT_CAUSE
    assert resumed.incident_id == _incident_of(state)

    tripped = _trip_event()
    cleared = derive_state_from(
        (
            tripped,
            resume(
                event_id=uuid4(),
                correlation_id=uuid4(),
                state=derive_state_from((tripped,)),
                armed_by=arm(
                    event_id=uuid4(),
                    incident_id=tripped.incident_id,
                    correlation_id=uuid4(),
                    operator_id=OPERATOR,
                    now_utc=TRIPPED_AT,
                ),
                operator_id=OPERATOR,
                root_cause=GOOD_ROOT_CAUSE,
                preconditions=_preconditions(reconciliation_completed_at_utc=TRIPPED_AT),
                now_utc=now_utc,
            ),
        )
    )
    assert cleared.status is KillSwitchStatus.TRADING


def test_resume_raises_rather_than_returning_a_refusal_the_caller_can_ignore() -> None:
    with pytest.raises(ResumeRefusedError, match="root cause is empty"):
        resume(
            event_id=uuid4(),
            correlation_id=uuid4(),
            state=_halted_state(),
            armed_by=None,
            operator_id=OPERATOR,
            root_cause="",
            preconditions=_preconditions(reconciliation_completed_at_utc=TRIPPED_AT),
            now_utc=TRIPPED_AT,
        )


# ------------------------------------------------------------------ human-only resume


@pytest.mark.parametrize(
    "operator_id",
    ["risk.drawdown_monitor", "agent:supervisor", "scheduler", "", "human:"],
)
def test_only_a_named_human_may_arm_or_resume(operator_id: str) -> None:
    with pytest.raises(DomainError):
        ArmEvent(
            event_id=uuid4(),
            incident_id=uuid4(),
            correlation_id=uuid4(),
            occurred_at_utc=TRIPPED_AT,
            operator_id=operator_id,
        )


def test_a_naive_trip_timestamp_is_rejected_at_construction() -> None:
    with pytest.raises(DomainError, match="timezone-aware"):
        trip(
            event_id=uuid4(),
            incident_id=uuid4(),
            correlation_id=uuid4(),
            actor="risk",
            trigger=_trigger(),
            snapshot=_snapshot(),
            now_utc=datetime(2026, 8, 1, 3, 14),  # noqa: DTZ001 - the point of the test
        )


def test_a_float_threshold_is_rejected_at_construction() -> None:
    """mypy refuses this call outright, which is the first line of defence. The runtime
    guard is the second, for a value arriving from a JSON decode or a database row where
    no annotation was checked -- hence `cast`, which passes the float through untouched.
    """
    rounded_before_it_arrived = cast("Decimal", 0.05)
    with pytest.raises(DomainError, match="not a float"):
        TripTrigger(
            trigger_id="drawdown.daily",
            unit="fraction",
            observed_value=Decimal("0.061"),
            threshold_value=rounded_before_it_arrived,
            detail="float thresholds round before the comparison",
        )


def test_resuming_one_of_two_open_incidents_leaves_the_switch_halted() -> None:
    """Found by Hypothesis before this ran against a venue: a fold that tracks a single
    open trip reports TRADING once *any* resume arrives, while the second incident is
    still open. Correlated trips are the normal case -- a testnet wipe presents as a
    reconciliation divergence and a balance collapse at once."""
    first = _trip_event(occurred_at_utc=TRIPPED_AT)
    second = _trip_event(occurred_at_utc=TRIPPED_AT + timedelta(minutes=1))
    cleared_first = ResumeEvent(
        event_id=uuid4(),
        incident_id=first.incident_id,
        correlation_id=uuid4(),
        occurred_at_utc=TRIPPED_AT + timedelta(minutes=2),
        operator_id=OPERATOR,
        root_cause=GOOD_ROOT_CAUSE,
    )
    state = derive_state_from((first, second, cleared_first))
    assert state.is_halted
    assert state.incident_id == second.incident_id
