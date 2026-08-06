"""What the runner refuses, and what it lets through.

Every refusal here is a defect in the engine driving the strategy rather than a market
condition, and each one would otherwise become a decision taken on data the run was never
gated on.
"""

from __future__ import annotations

from datetime import timedelta
from decimal import Decimal

import pytest

from fking.domain import Direction, Signal
from fking.strategy import (
    FeatureRequirement,
    ObservationRefusedError,
    SignalRefusedError,
    StrategySpec,
    initial_state,
    replay,
    step,
)
from fking.strategy.trailing_return import TrailingReturnContinuation
from tests.strategy.doubles import ScriptedStrategy
from tests.strategy.harness import (
    BTCUSDT,
    ETHUSDT,
    bars_from_closes,
    clock_at,
    feature_values_for,
    rising_closes,
)

pytestmark = pytest.mark.unit

_SEED = 20260801


def scriptable_spec(reference: StrategySpec) -> StrategySpec:
    """`reference` with no required features and a one-bar warm-up.

    Rebuilt rather than mutated: `StrategySpec` is frozen, which is what stops a caller
    quietly shortening a warm-up the walk-forward embargo was sized against. The scripted
    strategy needs to reach `evaluate` on the first bar so the signal checks are reachable.
    """
    return StrategySpec(
        strategy_id=reference.strategy_id,
        strategy_version=reference.strategy_version,
        thesis=reference.thesis,
        instruments=reference.instruments,
        bar_intervals=reference.bar_intervals,
        required_features=(),
        warm_up_bars=1,
        parameters=reference.parameters,
        invalidation=reference.invalidation,
        signal_horizon=reference.signal_horizon,
    )


def test_a_bar_on_an_undeclared_instrument_is_refused() -> None:
    """An ETH bar reaching a BTC-only strategy is data the backtest never gated, and it
    would evaluate perfectly happily."""
    strategy = TrailingReturnContinuation((BTCUSDT,))
    foreign = bars_from_closes(rising_closes(1), instrument=ETHUSDT)[0]

    with pytest.raises(ObservationRefusedError, match="ETHUSDT"):
        step(strategy, initial_state(seed=1), foreign, clock_at(foreign.close_time_utc))


def test_a_bar_of_an_undeclared_interval_is_refused() -> None:
    """`warm_up_bars` is only a duration against a declared interval."""
    strategy = TrailingReturnContinuation((BTCUSDT,))
    hourly = bars_from_closes(rising_closes(1), interval=timedelta(hours=1))[0]

    with pytest.raises(ObservationRefusedError, match="declares"):
        step(strategy, initial_state(seed=1), hourly, clock_at(hourly.close_time_utc))


def test_a_bar_that_has_not_closed_yet_is_refused() -> None:
    """Refused, not ignored. A current bar from the engine's future is an engine defect,
    and swallowing it would hide the defect while continuing to produce decisions."""
    strategy = TrailingReturnContinuation((BTCUSDT,))
    observed = bars_from_closes(rising_closes(1))[0]

    with pytest.raises(ObservationRefusedError, match="has not finished"):
        step(
            strategy,
            initial_state(seed=1),
            observed,
            clock_at(observed.close_time_utc - timedelta(seconds=1)),
        )


def test_a_bar_older_than_one_already_consumed_is_refused() -> None:
    strategy = TrailingReturnContinuation((BTCUSDT,))
    series = bars_from_closes(rising_closes(3))
    state = initial_state(seed=1)
    for observed in series:
        state = step(strategy, state, observed, clock_at(observed.close_time_utc)).state

    with pytest.raises(ObservationRefusedError, match="went backwards"):
        step(strategy, state, series[0], clock_at(series[-1].close_time_utc))


def test_a_feature_value_the_spec_never_declared_is_refused() -> None:
    """An undeclared input is one no availability check gated and no probe covered."""
    strategy = TrailingReturnContinuation((BTCUSDT,))
    undeclared = FeatureRequirement(feature_name="invented", feature_version=1)
    observed = bars_from_closes(rising_closes(1))[0]

    with pytest.raises(ObservationRefusedError, match="does not"):
        step(
            strategy,
            initial_state(seed=1),
            observed,
            clock_at(observed.close_time_utc),
            feature_values={undeclared: Decimal("0.1")},
        )


def test_a_declared_feature_missing_after_warm_up_is_refused() -> None:
    """Registration proves the warm-up covers every declared lookback, so an absent value
    here is the engine failing to subscribe rather than the series being short."""
    strategy = TrailingReturnContinuation((BTCUSDT,))
    warm_up_bars = strategy.spec.warm_up_bars
    series = bars_from_closes(rising_closes(warm_up_bars))

    state = initial_state(seed=1)
    for observed in series[: warm_up_bars - 1]:
        outcome = step(strategy, state, observed, clock_at(observed.close_time_utc))
        assert outcome.signal is None
        state = outcome.state

    final = series[warm_up_bars - 1]
    with pytest.raises(ObservationRefusedError, match="no value"):
        step(strategy, state, final, clock_at(final.close_time_utc))


def test_no_signal_is_emitted_until_the_declared_warm_up_has_been_consumed() -> None:
    """Suppressed by the engine, not by the strategy body.

    A strategy policing its own warm-up makes its first meaningful bar a property of its
    code rather than of its declaration, and the embargo is sized from the declaration.
    """
    strategy = TrailingReturnContinuation((BTCUSDT,))
    warm_up_bars = strategy.spec.warm_up_bars
    series = bars_from_closes(rising_closes(warm_up_bars + 4))
    values = feature_values_for(strategy.spec, series)

    state = initial_state(seed=1)
    emitted_at: list[int] = []
    for index, observed in enumerate(series):
        outcome = step(
            strategy,
            state,
            observed,
            clock_at(observed.close_time_utc),
            feature_values=values[observed.close_time_utc],
        )
        state = outcome.state
        if outcome.signal is not None:
            emitted_at.append(index)

    assert emitted_at, "the series must produce a signal, or the suppression proves nothing"
    assert min(emitted_at) >= warm_up_bars - 1


def test_a_signal_stamped_at_anything_but_the_injected_instant_is_refused() -> None:
    """A decision time that is not `clock()` means a second clock exists somewhere."""
    reference = TrailingReturnContinuation((BTCUSDT,)).spec
    observed = bars_from_closes(rising_closes(1))[0]
    misstamped = Signal(
        strategy_id=reference.strategy_id,
        instrument=BTCUSDT,
        direction=Direction.LONG,
        conviction=Decimal("0.5"),
        horizon=reference.signal_horizon,
        invalidation_quote_price=reference.invalidation.level_for(
            direction=Direction.LONG,
            reference_quote_price=observed.close_quote_price,
            instrument=BTCUSDT,
        ),
        rationale="a decision stamped an hour late",
        decided_at_utc=observed.close_time_utc + timedelta(hours=1),
    )
    strategy = ScriptedStrategy(scriptable_spec(reference), misstamped)

    with pytest.raises(SignalRefusedError, match="second clock"):
        step(strategy, initial_state(seed=1), observed, clock_at(observed.close_time_utc))


def test_a_signal_whose_invalidation_level_is_not_the_declared_one_is_refused() -> None:
    """The level is the denominator of every position sized from this signal, so a
    strategy free to name any level is a strategy free to size itself."""
    reference = TrailingReturnContinuation((BTCUSDT,)).spec
    observed = bars_from_closes(rising_closes(1))[0]
    widened = Signal(
        strategy_id=reference.strategy_id,
        instrument=BTCUSDT,
        direction=Direction.LONG,
        conviction=Decimal("0.5"),
        horizon=reference.signal_horizon,
        invalidation_quote_price=observed.close_quote_price / Decimal("2"),
        rationale="a stop widened to double the position",
        decided_at_utc=observed.close_time_utc,
    )
    strategy = ScriptedStrategy(scriptable_spec(reference), widened)

    with pytest.raises(SignalRefusedError, match="denominator"):
        step(strategy, initial_state(seed=1), observed, clock_at(observed.close_time_utc))


def test_a_signal_carrying_an_undeclared_horizon_is_refused() -> None:
    """The horizon sizes the walk-forward embargo and the label alignment."""
    reference = TrailingReturnContinuation((BTCUSDT,)).spec
    observed = bars_from_closes(rising_closes(1))[0]
    stretched = Signal(
        strategy_id=reference.strategy_id,
        instrument=BTCUSDT,
        direction=Direction.FLAT,
        conviction=Decimal("0"),
        horizon=reference.signal_horizon * 3,
        invalidation_quote_price=None,
        rationale="a horizon the specification never declared",
        decided_at_utc=observed.close_time_utc,
    )
    strategy = ScriptedStrategy(scriptable_spec(reference), stretched)

    with pytest.raises(SignalRefusedError, match="horizon"):
        step(strategy, initial_state(seed=1), observed, clock_at(observed.close_time_utc))


def test_a_signal_attributed_to_another_strategy_is_refused() -> None:
    reference = TrailingReturnContinuation((BTCUSDT,)).spec
    observed = bars_from_closes(rising_closes(1))[0]
    misattributed = Signal(
        strategy_id="somebody-else",
        instrument=BTCUSDT,
        direction=Direction.FLAT,
        conviction=Decimal("0"),
        horizon=reference.signal_horizon,
        invalidation_quote_price=None,
        rationale="attributed to a strategy that did not decide it",
        decided_at_utc=observed.close_time_utc,
    )
    strategy = ScriptedStrategy(scriptable_spec(reference), misattributed)

    with pytest.raises(SignalRefusedError, match="attributed"):
        step(strategy, initial_state(seed=1), observed, clock_at(observed.close_time_utc))


def test_a_flat_signal_is_admitted_and_carries_no_invalidation_level() -> None:
    """Flat is an instruction the risk engine nets against, not the absence of one."""
    reference = TrailingReturnContinuation((BTCUSDT,)).spec
    observed = bars_from_closes(rising_closes(1))[0]
    flat = Signal(
        strategy_id=reference.strategy_id,
        instrument=BTCUSDT,
        direction=Direction.FLAT,
        conviction=Decimal("0"),
        horizon=reference.signal_horizon,
        invalidation_quote_price=None,
        rationale="close whatever is open; there is nothing to invalidate",
        decided_at_utc=observed.close_time_utc,
    )
    strategy = ScriptedStrategy(scriptable_spec(reference), flat)

    outcome = step(strategy, initial_state(seed=1), observed, clock_at(observed.close_time_utc))

    assert outcome.signal is flat
    assert flat.is_actionable is False


def test_the_retained_history_is_bounded_by_the_declared_warm_up() -> None:
    """Unbounded retention would let a strategy reach further back than the lookback its
    specification declared, which is history no embargo was sized for."""
    strategy = TrailingReturnContinuation((BTCUSDT,))
    series = bars_from_closes(rising_closes(40))
    values = feature_values_for(strategy.spec, series)

    state = initial_state(seed=1)
    for observed in series:
        state = step(
            strategy,
            state,
            observed,
            clock_at(observed.close_time_utc),
            feature_values=values[observed.close_time_utc],
        ).state

    assert state.bars_consumed == len(series)
    assert len(state.recent_bars) == strategy.spec.warm_up_bars + 1


def test_step_returns_a_new_state_and_leaves_the_previous_one_untouched() -> None:
    """A state shared between the runner and a replay fold is look-ahead by aliasing."""
    strategy = TrailingReturnContinuation((BTCUSDT,))
    observed = bars_from_closes(rising_closes(1))[0]
    before = initial_state(seed=_SEED)

    after = step(strategy, before, observed, clock_at(observed.close_time_utc)).state

    assert before.bars_consumed == 0
    assert before.recent_bars == ()
    assert after.bars_consumed == 1
    assert after.seed == _SEED


def test_a_signal_on_a_different_instrument_from_the_bar_is_refused() -> None:
    """The decision was taken on one instrument's bar; naming another is a mis-routed
    belief the risk engine would net against the wrong exposure."""
    reference = TrailingReturnContinuation((BTCUSDT,)).spec
    observed = bars_from_closes(rising_closes(1))[0]
    misrouted = Signal(
        strategy_id=reference.strategy_id,
        instrument=ETHUSDT,
        direction=Direction.FLAT,
        conviction=Decimal("0"),
        horizon=reference.signal_horizon,
        invalidation_quote_price=None,
        rationale="decided on a BTCUSDT bar and emitted on ETHUSDT",
        decided_at_utc=observed.close_time_utc,
    )
    strategy = ScriptedStrategy(scriptable_spec(reference), misrouted)

    with pytest.raises(SignalRefusedError, match="ETHUSDT"):
        step(strategy, initial_state(seed=1), observed, clock_at(observed.close_time_utc))


def test_replay_decides_at_the_instant_each_bar_became_knowable() -> None:
    strategy = TrailingReturnContinuation((BTCUSDT,))
    series = bars_from_closes(rising_closes(16))
    signals = replay(
        strategy,
        series,
        seed=20260801,
        feature_values_at=feature_values_for(strategy.spec, series),
    )

    assert signals
    closes = {observed.close_time_utc for observed in series}
    assert all(signal.decided_at_utc in closes for signal in signals)
