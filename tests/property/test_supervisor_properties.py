"""Properties of the degraded-mode machine that must hold over every ordering of events.

Example-based tests confirm the sequences someone thought of. The ones that matter here
are the sequences nobody did: a database outage that clears while a drawdown trip is still
open, a redelivered `EXCHANGE_UNREACHABLE` notice arriving after the exit, a journal
reading that lands between two degraded transitions. All three are ordinary during an
incident, because an incident is exactly when several subsystems are failing at once and
the bus is redelivering.
"""

from __future__ import annotations

import dataclasses
from datetime import UTC, datetime

import pytest
from hypothesis import given
from hypothesis import strategies as st

from fking.platform.supervisor import (
    TRIPPING_MODES,
    DegradedMode,
    KillSwitchReading,
    SupervisorState,
    apply_reading,
    boot_halted_state,
    enter_degraded,
    exit_degraded,
)

pytestmark = pytest.mark.property

# A described operation rather than a bound callable: a shrunk counterexample prints the
# sequence a reader can retype, which is most of the value of finding it.


@dataclasses.dataclass(frozen=True, slots=True)
class Enter:
    mode: DegradedMode
    now_utc: datetime
    detail: str


@dataclasses.dataclass(frozen=True, slots=True)
class Leave:
    mode: DegradedMode


@dataclasses.dataclass(frozen=True, slots=True)
class Read:
    is_halted: bool


type Operation = Enter | Leave | Read

_MODES = st.sampled_from(list(DegradedMode))
_MOMENTS = st.datetimes(
    min_value=datetime(2020, 1, 1),  # noqa: DTZ001 - Hypothesis requires naive bounds
    max_value=datetime(2030, 1, 1),  # noqa: DTZ001 - whenever `timezones=` is supplied
    timezones=st.just(UTC),
)
_DETAILS = st.text(min_size=1, max_size=40).filter(lambda text: bool(text.strip()))

_OPERATIONS = st.lists(
    st.one_of(
        st.builds(Enter, mode=_MODES, now_utc=_MOMENTS, detail=_DETAILS),
        st.builds(Leave, mode=_MODES),
        st.builds(Read, is_halted=st.booleans()),
    ),
    max_size=12,
)

_HALTED_READING = KillSwitchReading(
    is_halted=True, halted_reason="an open incident was found in the journal"
)
_RESUMED_READING = KillSwitchReading(is_halted=False, halted_reason=None)


def _apply(state: SupervisorState, operation: Operation) -> SupervisorState:
    if isinstance(operation, Enter):
        return enter_degraded(
            state, mode=operation.mode, now_utc=operation.now_utc, detail=operation.detail
        )
    if isinstance(operation, Leave):
        return exit_degraded(state, mode=operation.mode)
    return apply_reading(state, _HALTED_READING if operation.is_halted else _RESUMED_READING)


def _fold(operations: list[Operation]) -> SupervisorState:
    state = boot_halted_state()
    for operation in operations:
        state = _apply(state, operation)
    return state


@given(operations=_OPERATIONS)
def test_signals_are_never_admitted_while_a_tripping_mode_is_active(
    operations: list[Operation],
) -> None:
    """The invariant the whole machine exists for.

    It is stated over arbitrary orderings rather than over the paths a reader can imagine,
    because the dangerous ordering is the one where a resume arrives while a database
    outage is still open -- an operator clearing yesterday's incident during today's.
    """
    state = _fold(operations)
    if state.active_modes & TRIPPING_MODES:
        assert state.signals_admitted is False


@given(operations=_OPERATIONS)
def test_the_degraded_set_stays_unique_and_canonically_ordered(
    operations: list[Operation],
) -> None:
    """Duplicate entries would double-count an outage on the dashboard, and an unordered
    tuple would make two equal states compare unequal."""
    state = _fold(operations)
    modes = [entry.mode for entry in state.degraded]
    assert modes == sorted(set(modes))


@given(operations=_OPERATIONS, mode=_MODES, now_utc=_MOMENTS, detail=_DETAILS)
def test_entering_a_mode_twice_is_the_same_as_entering_it_once(
    operations: list[Operation], mode: DegradedMode, now_utc: datetime, detail: str
) -> None:
    """Redelivery is normal, not exceptional: Redis Streams is at-least-once and the
    consumer restarts that cause redelivery happen during the outage being reported."""
    base = _fold(operations)
    once = enter_degraded(base, mode=mode, now_utc=now_utc, detail=detail)
    twice = enter_degraded(once, mode=mode, now_utc=now_utc, detail=detail)
    assert twice == once
    assert twice is once


@given(operations=_OPERATIONS, mode=_MODES)
def test_exiting_a_mode_never_admits_signals(
    operations: list[Operation], mode: DegradedMode
) -> None:
    """A system that unhalts itself has a kill switch in name only. Waiting is always
    sufficient to clear a trigger condition, so recovery alone must never resume."""
    before = _fold(operations)
    after = exit_degraded(before, mode=mode)
    if not before.signals_admitted:
        assert after.signals_admitted is False


@given(operations=_OPERATIONS, mode=_MODES)
def test_exiting_a_mode_twice_is_the_same_as_exiting_it_once(
    operations: list[Operation], mode: DegradedMode
) -> None:
    before = _fold(operations)
    once = exit_degraded(before, mode=mode)
    assert exit_degraded(once, mode=mode) is once


@given(operations=_OPERATIONS)
def test_a_halted_reading_always_halts_whatever_the_history(
    operations: list[Operation],
) -> None:
    """A restart is not a reset, and neither is any sequence of degraded transitions."""
    state = apply_reading(_fold(operations), _HALTED_READING)
    assert state.signals_admitted is False
    assert state.halted_reason == _HALTED_READING.halted_reason


@given(operations=_OPERATIONS, mode=_MODES, now_utc=_MOMENTS, detail=_DETAILS)
def test_every_transition_leaves_its_input_untouched(
    operations: list[Operation], mode: DegradedMode, now_utc: datetime, detail: str
) -> None:
    """Immutability is what makes a state safe to hold across an await. A transition that
    mutated in place would change a value another coroutine had already read."""
    before = _fold(operations)
    snapshot = dataclasses.replace(before)

    enter_degraded(before, mode=mode, now_utc=now_utc, detail=detail)
    exit_degraded(before, mode=mode)
    apply_reading(before, _HALTED_READING)
    apply_reading(before, _RESUMED_READING)

    assert before == snapshot


@given(operations=_OPERATIONS)
def test_a_resumed_reading_admits_signals_exactly_when_no_tripping_mode_is_active(
    operations: list[Operation],
) -> None:
    """The two halts are independent. A human resume clears the journal's halt and
    nothing else; the database coming back clears the mode and nothing else."""
    before = _fold(operations)
    after = apply_reading(before, _RESUMED_READING)
    assert after.signals_admitted is not bool(before.active_modes & TRIPPING_MODES)


def test_a_state_cannot_be_mutated() -> None:
    """Enforced by the type, so the mistake is a FrozenInstanceError at the moment it is
    made rather than a divergence discovered later."""
    state = boot_halted_state()
    with pytest.raises(dataclasses.FrozenInstanceError):
        state.halted_reason = None  # type: ignore[misc]  # the assignment is the test
