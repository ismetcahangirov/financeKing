"""Construction-time refusals on the kill-switch journal types.

Every field of every journal row is validated where it is constructed, because these
rows are also *read back* -- from an append-only table, months later, by a boot sequence
that decides whether the process may trade. A row written malformed is a row that will
be re-read malformed, and the boot sequence has no better information than the
constructor did.

The wrong-type cases go through `cast` rather than a bare literal: mypy refuses the call
outright, which is the first line of defence, and these assertions cover the second --
a value arriving from a database row or a JSON decode where no annotation was checked.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta, timezone
from decimal import Decimal
from typing import cast
from uuid import UUID, uuid4

import pytest

from fking.domain import DomainError, Portfolio
from fking.risk import (
    MIN_ROOT_CAUSE_LENGTH,
    ArmEvent,
    BookSnapshot,
    KillSwitchState,
    KillSwitchStatus,
    ResumeEvent,
    ResumePreconditions,
    TripEvent,
    TripTrigger,
    resume_refusals,
)

pytestmark = pytest.mark.unit

MOMENT = datetime(2026, 8, 1, 3, 14, tzinfo=UTC)
OPERATOR = "human:ismet"
GOOD_ROOT_CAUSE = "x" * MIN_ROOT_CAUSE_LENGTH


def _trigger(**overrides: object) -> TripTrigger:
    fields: dict[str, object] = {
        "trigger_id": "drawdown.daily",
        "unit": "fraction",
        "observed_value": Decimal("0.061"),
        "threshold_value": Decimal("0.05"),
        "detail": "equity fell past the daily limit",
    }
    fields.update(overrides)
    return TripTrigger(**fields)  # type: ignore[arg-type]  # overrides are deliberately wrong


def _snapshot() -> BookSnapshot:
    return BookSnapshot(
        portfolio=Portfolio(as_of_utc=MOMENT, positions=(), cash_balances={}),
        open_client_order_ids=(),
        protective_client_order_ids=(),
        reconciled_at_utc=MOMENT,
        reconciliation_is_clean=True,
    )


@pytest.mark.parametrize(
    ("field_name", "bad_value", "expected"),
    [
        ("trigger_id", cast("str", 7), "must be a string"),
        ("trigger_id", "   ", "must not be blank"),
        ("unit", "", "must not be blank"),
        ("detail", "", "must not be blank"),
        ("observed_value", cast("Decimal", "0.061"), "must be a Decimal"),
        ("threshold_value", Decimal("NaN"), "must be finite"),
    ],
)
def test_a_malformed_trigger_field_is_refused(
    field_name: str, bad_value: object, expected: str
) -> None:
    with pytest.raises(DomainError, match=expected):
        _trigger(**{field_name: bad_value})


@pytest.mark.parametrize(
    ("bad_moment", "expected"),
    [
        (cast("datetime", "2026-08-01"), "must be a datetime"),
        (datetime(2026, 8, 1, 3, 14), "must be timezone-aware"),  # noqa: DTZ001
        (datetime(2026, 8, 1, 7, 14, tzinfo=timezone(timedelta(hours=4))), "must be UTC"),
    ],
)
def test_a_trip_timestamp_that_is_not_aware_utc_is_refused(
    bad_moment: datetime, expected: str
) -> None:
    with pytest.raises(DomainError, match=expected):
        TripEvent(
            event_id=uuid4(),
            incident_id=uuid4(),
            correlation_id=uuid4(),
            occurred_at_utc=bad_moment,
            actor="risk.drawdown_monitor",
            trigger=_trigger(),
            snapshot=_snapshot(),
        )


def test_a_trip_identifier_that_is_not_a_uuid_is_refused() -> None:
    with pytest.raises(DomainError, match="event_id must be a UUID"):
        TripEvent(
            event_id=cast(UUID, "not-a-uuid"),
            incident_id=uuid4(),
            correlation_id=uuid4(),
            occurred_at_utc=MOMENT,
            actor="risk.drawdown_monitor",
            trigger=_trigger(),
            snapshot=_snapshot(),
        )


def test_a_trip_with_no_actor_is_refused() -> None:
    """The actor is who or what tripped it. A blank one produces an audit row that
    answers "something halted us" and nothing further."""
    with pytest.raises(DomainError, match="actor must not be blank"):
        TripEvent(
            event_id=uuid4(),
            incident_id=uuid4(),
            correlation_id=uuid4(),
            occurred_at_utc=MOMENT,
            actor="  ",
            trigger=_trigger(),
            snapshot=_snapshot(),
        )


def test_an_unreconciled_snapshot_is_accepted_when_it_claims_nothing() -> None:
    """A trip during a data outage genuinely has no clean reconciliation. The refusal is
    only for a snapshot that claims one and cannot say when."""
    snapshot = BookSnapshot(
        portfolio=Portfolio(as_of_utc=MOMENT, positions=(), cash_balances={}),
        open_client_order_ids=("fk-a",),
        protective_client_order_ids=(),
        reconciled_at_utc=None,
        reconciliation_is_clean=False,
    )
    assert snapshot.reconciled_at_utc is None


def test_a_blank_client_order_id_in_the_snapshot_is_refused() -> None:
    with pytest.raises(DomainError, match="client_order_id must not be blank"):
        BookSnapshot(
            portfolio=Portfolio(as_of_utc=MOMENT, positions=(), cash_balances={}),
            open_client_order_ids=("",),
            protective_client_order_ids=(),
            reconciled_at_utc=MOMENT,
            reconciliation_is_clean=True,
        )


@pytest.mark.parametrize("root_cause", ["", "   ", "y" * (MIN_ROOT_CAUSE_LENGTH - 1)])
def test_a_resume_row_cannot_be_written_without_a_real_root_cause(root_cause: str) -> None:
    """Belt and braces with `resume_refusals`: the refusal is checked before the row is
    built, and the row refuses to exist even if a future caller skips that check."""
    with pytest.raises(DomainError):
        ResumeEvent(
            event_id=uuid4(),
            incident_id=uuid4(),
            correlation_id=uuid4(),
            occurred_at_utc=MOMENT,
            operator_id=OPERATOR,
            root_cause=root_cause,
        )


def test_an_arm_timestamped_naively_is_refused() -> None:
    with pytest.raises(DomainError, match="timezone-aware"):
        ArmEvent(
            event_id=uuid4(),
            incident_id=uuid4(),
            correlation_id=uuid4(),
            occurred_at_utc=datetime(2026, 8, 1, 3, 14),  # noqa: DTZ001 - the point
            operator_id=OPERATOR,
        )


def test_an_arm_expires_two_minutes_after_it_was_recorded() -> None:
    armed = ArmEvent(
        event_id=uuid4(),
        incident_id=uuid4(),
        correlation_id=uuid4(),
        occurred_at_utc=MOMENT,
        operator_id=OPERATOR,
    )
    assert armed.expires_at_utc == MOMENT + timedelta(seconds=120)


def test_a_halted_state_must_say_why() -> None:
    with pytest.raises(DomainError, match="must state why it is halted"):
        KillSwitchState(
            status=KillSwitchStatus.HALTED,
            incident_id=None,
            tripped_at_utc=None,
            trigger=None,
            halted_reason=None,
        )


@pytest.mark.parametrize(
    "overrides",
    [
        {"halted_reason": "left over from the last incident"},
        {"incident_id": uuid4()},
        {"tripped_at_utc": MOMENT},
    ],
)
def test_a_trading_state_carrying_incident_residue_is_refused(
    overrides: dict[str, object],
) -> None:
    """Half-cleared state is worse than either state: a reader sees TRADING and an
    incident id, and trusts whichever one matches what they expected."""
    fields: dict[str, object] = {
        "status": KillSwitchStatus.TRADING,
        "incident_id": None,
        "tripped_at_utc": None,
        "trigger": None,
        "halted_reason": None,
    }
    fields.update(overrides)
    with pytest.raises(DomainError, match="carries no incident"):
        KillSwitchState(**fields)  # type: ignore[arg-type]  # deliberately inconsistent


def test_a_halted_state_with_a_naive_trip_time_is_refused() -> None:
    with pytest.raises(DomainError, match="timezone-aware"):
        KillSwitchState(
            status=KillSwitchStatus.HALTED,
            incident_id=uuid4(),
            tripped_at_utc=datetime(2026, 8, 1, 3, 14),  # noqa: DTZ001 - the point
            trigger=_trigger(),
            halted_reason="tripped",
        )


def test_an_anonymous_resume_is_refused_even_with_everything_else_satisfied() -> None:
    """`resume_refusals` takes the operator id as a raw string, so the blank case must be
    refused there and not only by `ArmEvent`'s constructor."""
    refusals = resume_refusals(
        state=KillSwitchState(
            status=KillSwitchStatus.HALTED,
            incident_id=uuid4(),
            tripped_at_utc=MOMENT,
            trigger=_trigger(),
            halted_reason="tripped by drawdown.daily",
        ),
        armed_by=None,
        operator_id="   ",
        root_cause=GOOD_ROOT_CAUSE,
        preconditions=ResumePreconditions(
            reconciliation_is_clean=True,
            reconciliation_completed_at_utc=MOMENT,
            trigger_condition_still_true=False,
            recovery_step_completed=7,
        ),
        now_utc=MOMENT,
    )
    assert "no operator identity was recorded" in refusals
