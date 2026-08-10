"""Properties of the degraded-mode state machine under arbitrary observation sequences.

A hysteresis counter is a small piece of state that is wrong in exactly the situations
nobody writes an example for: a fault arriving between two clears, a redelivery landing
between them, an observation arriving out of order during a reconnect, a mode entered
and cleared and entered again inside one second.

The properties that matter are the ones an example test cannot state. **One effect per
distinct observation**: replaying the whole sequence produces the same state and the same
transitions. **Transitions alternate**: entered, exited, entered -- never two entries in
a row, because the audit row count is what an incident review counts incidents from.

`docs/rules/testing-rules.md` clause 2.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from itertools import pairwise
from typing import Final
from uuid import UUID, uuid5

import pytest
from hypothesis import given
from hypothesis import strategies as st

from fking.risk.degraded import (
    MODE_RULES,
    DegradedMode,
    DegradedModeState,
    ModeDirection,
    ModeObservation,
    ModeTransition,
    blocks_new_orders,
)

pytestmark = [pytest.mark.property, pytest.mark.unit]

_ORIGIN: Final = datetime(2026, 8, 1, 14, 30, tzinfo=UTC)
_CAUSE: Final = UUID("11111111-1111-4111-8111-111111111111")
_NAMESPACE: Final = UUID("44444444-4444-4444-8444-444444444444")

_modes = st.sampled_from(list(DegradedMode))
# A fault flag and whether reconciliation happened to be clean at that moment. The second
# only matters for `EXCHANGE_UNREACHABLE`, which is exactly why it is generated for all
# of them: a rule that reads it when it should not is caught by the difference.
_observation_shapes = st.lists(st.tuples(st.booleans(), st.booleans()), min_size=1, max_size=24)


def _sequence(mode: DegradedMode, shapes: list[tuple[bool, bool]]) -> list[ModeObservation]:
    return [
        ModeObservation(
            observation_id=uuid5(_NAMESPACE, f"{mode}-{index}"),
            correlation_id=_CAUSE,
            mode=mode,
            is_faulted=is_faulted,
            observed_at_utc=_ORIGIN + timedelta(seconds=index),
            reason="generated supervisor observation",
            affected_symbols=("BTCUSDT",),
            reconciliation_is_clean=reconciled,
        )
        for index, (is_faulted, reconciled) in enumerate(shapes)
    ]


def _apply(
    observations: list[ModeObservation],
) -> tuple[DegradedModeState, list[ModeTransition]]:
    state = DegradedModeState()
    transitions: list[ModeTransition] = []
    for observation in observations:
        state, transition = state.observe(observation)
        if transition is not None:
            transitions.append(transition)
    return state, transitions


@given(mode=_modes, shapes=_observation_shapes)
def test_transitions_alternate_and_start_with_an_entry(
    mode: DegradedMode, shapes: list[tuple[bool, bool]]
) -> None:
    _, transitions = _apply(_sequence(mode, shapes))
    directions = [transition.direction for transition in transitions]

    assert all(earlier is not later for earlier, later in pairwise(directions))
    assert not directions or directions[0] is ModeDirection.ENTERED


@given(mode=_modes, shapes=_observation_shapes)
def test_the_active_flag_always_agrees_with_the_last_transition(
    mode: DegradedMode, shapes: list[tuple[bool, bool]]
) -> None:
    """The derived view and the audit trail cannot disagree about whether a mode is on."""
    state, transitions = _apply(_sequence(mode, shapes))
    expected = bool(transitions) and transitions[-1].direction is ModeDirection.ENTERED
    assert state.is_active(mode) is expected
    assert (mode in state.active_modes) is expected
    if expected:
        assert blocks_new_orders(state) is MODE_RULES[mode].blocks_new_orders


@given(mode=_modes, shapes=_observation_shapes, repeats=st.integers(min_value=1, max_value=3))
def test_redelivering_every_observation_changes_nothing(
    mode: DegradedMode, shapes: list[tuple[bool, bool]], repeats: int
) -> None:
    """At-least-once delivery, stated as the property the consumer has to satisfy."""
    observations = _sequence(mode, shapes)
    once_state, once_transitions = _apply(observations)

    replayed = [observation for observation in observations for _ in range(repeats)]
    replayed_state, replayed_transitions = _apply(replayed)

    assert replayed_state.active_modes == once_state.active_modes
    assert [(entry.mode, entry.direction) for entry in replayed_transitions] == [
        (entry.mode, entry.direction) for entry in once_transitions
    ]


@given(mode=_modes, shapes=_observation_shapes)
def test_an_entry_needs_at_least_as_many_faults_as_the_rule_declares(
    mode: DegradedMode, shapes: list[tuple[bool, bool]]
) -> None:
    rule = MODE_RULES[mode]
    observations = _sequence(mode, shapes)
    state = DegradedModeState()
    consecutive_faults = 0
    for observation in observations:
        consecutive_faults = consecutive_faults + 1 if observation.is_faulted else 0
        state, transition = state.observe(observation)
        if transition is not None and transition.direction is ModeDirection.ENTERED:
            assert consecutive_faults >= rule.consecutive_faults_to_enter


@given(mode=_modes, shapes=_observation_shapes)
def test_an_exit_needs_the_declared_clears_and_a_clean_reconciliation_where_required(
    mode: DegradedMode, shapes: list[tuple[bool, bool]]
) -> None:
    rule = MODE_RULES[mode]
    state = DegradedModeState()
    consecutive_clears = 0
    for observation in _sequence(mode, shapes):
        consecutive_clears = 0 if observation.is_faulted else consecutive_clears + 1
        state, transition = state.observe(observation)
        if transition is not None and transition.direction is ModeDirection.EXITED:
            assert consecutive_clears >= rule.consecutive_clears_to_exit
            if rule.requires_clean_reconciliation_to_exit:
                assert observation.reconciliation_is_clean


@given(mode=_modes, shapes=_observation_shapes)
def test_an_out_of_order_replay_never_reactivates_a_cleared_mode(
    mode: DegradedMode, shapes: list[tuple[bool, bool]]
) -> None:
    """Reordered delivery is a reconnect artefact, not new evidence about the world."""
    observations = _sequence(mode, shapes)
    settled, _ = _apply(observations)

    reordered = settled
    for observation in reversed(observations[:-1]):
        reordered, transition = reordered.observe(observation)
        assert transition is None
    assert reordered.active_modes == settled.active_modes
