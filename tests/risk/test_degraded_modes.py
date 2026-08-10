"""The five named degraded modes: their transitions, their hysteresis and their metrics.

The assertion issue #54 asks for is the last one in the file: entering and exiting every
mode emits an audit event and moves a labelled metric, checked by reading the exported
series back rather than by grepping for a call.
"""

from __future__ import annotations

from dataclasses import replace
from datetime import UTC, datetime, timedelta
from uuid import UUID, uuid5

import pytest

from fking.risk import (
    MODE_RULES,
    DegradedMode,
    DegradedModeError,
    DegradedModeGate,
    DegradedModeState,
    ModeDirection,
    ModeObservation,
    agent_work_paused,
    blocked_symbols,
    blocks_new_orders,
    kill_switch_trip_required,
    symbols_without_usable_data,
)
from tests.support.metric_readings import metric_readings

pytestmark = pytest.mark.unit

NOW = datetime(2026, 8, 1, 14, 30, tzinfo=UTC)
CAUSE = UUID("11111111-1111-4111-8111-111111111111")
# Deterministic observation ids: a UUID5 over the sequence number, so a redelivery in a
# test is spelled by reusing the number rather than by copying a literal.
_NAMESPACE = UUID("44444444-4444-4444-8444-444444444444")


def _observation(  # noqa: PLR0913 - one parameter per field a test needs to vary; a
    # builder object would move the same six values behind a constructor nobody reads
    sequence: int,
    mode: DegradedMode,
    *,
    is_faulted: bool,
    at: datetime = NOW,
    symbols: tuple[str, ...] = (),
    reconciliation_is_clean: bool = False,
) -> ModeObservation:
    return ModeObservation(
        observation_id=uuid5(_NAMESPACE, f"{mode}-{sequence}"),
        correlation_id=CAUSE,
        mode=mode,
        is_faulted=is_faulted,
        observed_at_utc=at,
        reason="synthetic supervisor observation",
        affected_symbols=symbols,
        reconciliation_is_clean=reconciliation_is_clean,
    )


def test_data_stale_enters_on_one_fault_and_needs_two_fresh_ticks_to_leave() -> None:
    """One fresh tick can be a stale replay from a reconnecting feed."""
    state = DegradedModeState()
    state, entered = state.observe(
        _observation(1, DegradedMode.DATA_STALE, is_faulted=True, symbols=("THINALT",))
    )
    assert entered is not None
    assert entered.direction is ModeDirection.ENTERED
    assert state.is_active(DegradedMode.DATA_STALE)

    state, after_one = state.observe(
        _observation(2, DegradedMode.DATA_STALE, is_faulted=False, at=NOW + timedelta(seconds=1))
    )
    assert after_one is None
    assert state.is_active(DegradedMode.DATA_STALE)

    state, after_two = state.observe(
        _observation(3, DegradedMode.DATA_STALE, is_faulted=False, at=NOW + timedelta(seconds=2))
    )
    assert after_two is not None
    assert after_two.direction is ModeDirection.EXITED
    assert not state.is_active(DegradedMode.DATA_STALE)


def test_a_fault_between_two_clears_restarts_the_exit_count() -> None:
    state = DegradedModeState()
    state, _ = state.observe(_observation(1, DegradedMode.DATA_STALE, is_faulted=True))
    state, _ = state.observe(
        _observation(2, DegradedMode.DATA_STALE, is_faulted=False, at=NOW + timedelta(seconds=1))
    )
    state, _ = state.observe(
        _observation(3, DegradedMode.DATA_STALE, is_faulted=True, at=NOW + timedelta(seconds=2))
    )
    state, transition = state.observe(
        _observation(4, DegradedMode.DATA_STALE, is_faulted=False, at=NOW + timedelta(seconds=3))
    )
    assert transition is None
    assert state.is_active(DegradedMode.DATA_STALE)


def test_exchange_unreachable_needs_two_consecutive_failures_to_enter() -> None:
    """A single failed REST call is the ordinary texture of the testnet."""
    state = DegradedModeState()
    state, first = state.observe(
        _observation(1, DegradedMode.EXCHANGE_UNREACHABLE, is_faulted=True)
    )
    assert first is None
    assert not state.is_active(DegradedMode.EXCHANGE_UNREACHABLE)

    state, second = state.observe(
        _observation(
            2, DegradedMode.EXCHANGE_UNREACHABLE, is_faulted=True, at=NOW + timedelta(seconds=5)
        )
    )
    assert second is not None
    assert state.is_active(DegradedMode.EXCHANGE_UNREACHABLE)
    assert blocks_new_orders(state)


def test_exchange_unreachable_does_not_exit_until_reconciliation_is_clean() -> None:
    """Reconnection is not recovery: the gap is when positions change without us."""
    state = DegradedModeState()
    for sequence in (1, 2):
        state, _ = state.observe(
            _observation(
                sequence,
                DegradedMode.EXCHANGE_UNREACHABLE,
                is_faulted=True,
                at=NOW + timedelta(seconds=sequence),
            )
        )
    state, reconnected = state.observe(
        _observation(
            3,
            DegradedMode.EXCHANGE_UNREACHABLE,
            is_faulted=False,
            at=NOW + timedelta(seconds=10),
            reconciliation_is_clean=False,
        )
    )
    assert reconnected is None
    assert state.is_active(DegradedMode.EXCHANGE_UNREACHABLE)

    state, reconciled = state.observe(
        _observation(
            4,
            DegradedMode.EXCHANGE_UNREACHABLE,
            is_faulted=False,
            at=NOW + timedelta(seconds=20),
            reconciliation_is_clean=True,
        )
    )
    assert reconciled is not None
    assert not state.is_active(DegradedMode.EXCHANGE_UNREACHABLE)


def test_a_redelivered_observation_produces_one_effect() -> None:
    """At-least-once delivery: the same measurement twice is one measurement.

    Delivered twice, the second fault would satisfy `EXCHANGE_UNREACHABLE`'s two-failure
    threshold on one real failure, which is the mode entering on evidence that does not
    exist.
    """
    observation = _observation(1, DegradedMode.EXCHANGE_UNREACHABLE, is_faulted=True)
    state = DegradedModeState()
    state, _ = state.observe(observation)
    state, replayed = state.observe(observation)

    assert replayed is None
    assert not state.is_active(DegradedMode.EXCHANGE_UNREACHABLE)


def test_an_observation_from_before_the_last_one_is_not_applied() -> None:
    """A reordered delivery must not resurrect a mode a later measurement cleared."""
    state = DegradedModeState()
    state, _ = state.observe(
        _observation(1, DegradedMode.DATA_STALE, is_faulted=False, at=NOW + timedelta(minutes=5))
    )
    state, stale_fault = state.observe(
        _observation(2, DegradedMode.DATA_STALE, is_faulted=True, at=NOW)
    )
    assert stale_fault is None
    assert not state.is_active(DegradedMode.DATA_STALE)


def test_database_unavailable_trips_the_kill_switch_on_the_first_observation() -> None:
    """The least negotiable entry: the audit log is a precondition for trading."""
    state = DegradedModeState()
    state, entered = state.observe(
        _observation(1, DegradedMode.DATABASE_UNAVAILABLE, is_faulted=True)
    )
    assert entered is not None
    assert kill_switch_trip_required(state)
    assert blocks_new_orders(state)


def test_llm_quota_exhaustion_changes_nothing_about_trading() -> None:
    """`FAILSAFE.md` section 3.3 as an assertion rather than a paragraph."""
    state = DegradedModeState()
    state, entered = state.observe(
        _observation(1, DegradedMode.LLM_QUOTA_EXHAUSTED, is_faulted=True)
    )
    assert entered is not None
    assert state.is_active(DegradedMode.LLM_QUOTA_EXHAUSTED)
    assert not blocks_new_orders(state)
    assert not kill_switch_trip_required(state)
    assert blocked_symbols(state) == frozenset()
    assert symbols_without_usable_data(state) == frozenset()
    assert agent_work_paused(state)


def test_exactly_one_mode_leaves_trading_untouched() -> None:
    unaffected = {mode for mode, rule in MODE_RULES.items() if not rule.affects_trading}
    assert unaffected == {DegradedMode.LLM_QUOTA_EXHAUSTED}


def test_every_mode_has_a_rule_and_every_rule_names_its_own_mode() -> None:
    assert set(MODE_RULES) == set(DegradedMode)
    assert all(mode is rule.mode for mode, rule in MODE_RULES.items())


def test_symbols_accumulate_while_a_mode_stays_entered() -> None:
    state = DegradedModeState()
    state, _ = state.observe(
        _observation(1, DegradedMode.DATA_STALE, is_faulted=True, symbols=("THINALT",))
    )
    state, again = state.observe(
        _observation(
            2,
            DegradedMode.DATA_STALE,
            is_faulted=True,
            at=NOW + timedelta(seconds=30),
            symbols=("SECONDALT",),
        )
    )
    assert again is None
    assert blocked_symbols(state) == frozenset({"THINALT", "SECONDALT"})
    assert symbols_without_usable_data(state) == frozenset({"THINALT", "SECONDALT"})


def test_feature_store_partial_withholds_values_without_blocking_the_whole_book() -> None:
    state = DegradedModeState()
    state, _ = state.observe(
        _observation(1, DegradedMode.FEATURE_STORE_PARTIAL, is_faulted=True, symbols=("ETHUSDT",))
    )
    assert symbols_without_usable_data(state) == frozenset({"ETHUSDT"})
    assert not blocks_new_orders(state)


def test_an_entry_records_the_first_instant_not_the_latest() -> None:
    state = DegradedModeState()
    state, _ = state.observe(_observation(1, DegradedMode.DATA_STALE, is_faulted=True))
    state, _ = state.observe(
        _observation(2, DegradedMode.DATA_STALE, is_faulted=True, at=NOW + timedelta(minutes=9))
    )
    entry = state.entry_for(DegradedMode.DATA_STALE)
    assert entry is not None
    assert entry.entered_at_utc == NOW


def test_a_rule_that_changes_state_on_no_observations_is_refused() -> None:
    """A threshold of zero is a mode that is entered and exited by nothing at all."""
    rule = MODE_RULES[DegradedMode.DATA_STALE]
    with pytest.raises(DegradedModeError, match="on nothing"):
        replace(rule, consecutive_faults_to_enter=0)


def test_a_transition_reason_may_not_be_blank() -> None:
    with pytest.raises(DegradedModeError, match="carries a reason"):
        ModeObservation(
            observation_id=UUID(int=1),
            correlation_id=CAUSE,
            mode=DegradedMode.DATA_STALE,
            is_faulted=True,
            observed_at_utc=NOW,
            reason="  ",
        )


def test_a_naive_observation_instant_is_refused() -> None:
    with pytest.raises(DegradedModeError, match="timezone-aware UTC"):
        ModeObservation(
            observation_id=UUID(int=1),
            correlation_id=CAUSE,
            mode=DegradedMode.DATA_STALE,
            is_faulted=True,
            observed_at_utc=datetime(2026, 8, 1, 14, 30),  # noqa: DTZ001 - the point
            reason="synthetic",
        )


def test_a_blank_affected_symbol_is_refused() -> None:
    with pytest.raises(DegradedModeError, match="blank symbol"):
        _observation(1, DegradedMode.DATA_STALE, is_faulted=True, symbols=("",))


def test_the_state_a_reader_holds_cannot_be_mutated_through_the_mapping() -> None:
    state = DegradedModeState()
    state, _ = state.observe(_observation(1, DegradedMode.DATA_STALE, is_faulted=True))
    with pytest.raises(TypeError):
        state.progress[DegradedMode.DATA_STALE] = None  # type: ignore[index] # the refusal
    assert state.active_modes == (DegradedMode.DATA_STALE,)


def test_the_gate_adopts_a_state_the_boot_sequence_read() -> None:
    state = DegradedModeState()
    state, _ = state.observe(_observation(1, DegradedMode.DATABASE_UNAVAILABLE, is_faulted=True))
    gate = DegradedModeGate()
    gate.adopt(state)
    assert kill_switch_trip_required(gate.state)


def test_entering_and_exiting_every_mode_emits_an_audit_event_and_moves_a_metric() -> None:
    """The acceptance assertion, read back from the exported series.

    Both directions for all five modes, with the metric checked by label rather than by
    total: a gauge that moves for one mode and not another is exactly the failure a
    single unlabelled reading cannot see.
    """
    with metric_readings() as readings:
        gate = DegradedModeGate()
        audit: list[tuple[DegradedMode, ModeDirection]] = []
        moment = NOW
        for sequence, mode in enumerate(DegradedMode):
            rule = MODE_RULES[mode]
            for step in range(rule.consecutive_faults_to_enter):
                moment += timedelta(seconds=1)
                transition = gate.observe(
                    _observation(
                        sequence * 100 + step,
                        mode,
                        is_faulted=True,
                        at=moment,
                        symbols=("BTCUSDT",),
                    )
                )
                if transition is not None:
                    audit.append((transition.mode, transition.direction))
            for step in range(rule.consecutive_clears_to_exit):
                moment += timedelta(seconds=1)
                transition = gate.observe(
                    _observation(
                        sequence * 100 + 50 + step,
                        mode,
                        is_faulted=False,
                        at=moment,
                        reconciliation_is_clean=True,
                    )
                )
                if transition is not None:
                    audit.append((transition.mode, transition.direction))

        assert gate.state.active_modes == ()
        assert audit == [
            (mode, direction)
            for mode in DegradedMode
            for direction in (ModeDirection.ENTERED, ModeDirection.EXITED)
        ]

        transitions = readings.by_labels("fking_risk_degraded_mode_transitions_total")
        engaged = readings.by_labels("fking_risk_degraded_mode_engaged_count")
        for mode in DegradedMode:
            assert transitions[(("mode", mode.value), ("outcome", "entered"))] == 1
            assert transitions[(("mode", mode.value), ("outcome", "exited"))] == 1
            assert engaged[(("mode", mode.value),)] == 0


def test_the_engaged_gauge_reads_one_while_a_mode_is_entered() -> None:
    with metric_readings() as readings:
        gate = DegradedModeGate()
        gate.observe(_observation(1, DegradedMode.DATA_STALE, is_faulted=True))
        engaged = readings.by_labels("fking_risk_degraded_mode_engaged_count")
        assert engaged[(("mode", DegradedMode.DATA_STALE.value),)] == 1


def test_a_redelivery_does_not_move_the_metric_a_second_time() -> None:
    with metric_readings() as readings:
        gate = DegradedModeGate()
        observation = _observation(1, DegradedMode.DATABASE_UNAVAILABLE, is_faulted=True)
        assert gate.observe(observation) is not None
        assert gate.observe(observation) is None

        transitions = readings.by_labels("fking_risk_degraded_mode_transitions_total")
        key = (("mode", DegradedMode.DATABASE_UNAVAILABLE.value), ("outcome", "entered"))
        assert transitions[key] == 1
