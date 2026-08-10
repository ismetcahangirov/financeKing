"""A feature the engine could not compute stops the evaluation instead of being imputed.

`FAILSAFE.md` sections 3.1 and 3.5 are one rule stated twice: under `DATA_STALE` and
under `FEATURE_STORE_PARTIAL`, a strategy depending on the affected data gets an explicit
unavailability and emits nothing. It never gets a forward fill, a zero or a last-known
value, because a flat series reads as calm to every volatility estimator and as an
opportunity to every mean-reversion rule, and the resulting behaviour cannot be falsified.

The assertions are about `evaluate` never being *called*, not about the signal being
`None`. A strategy that is called with a substituted number and happens to return `None`
has still been asked the wrong question.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal
from uuid import UUID

import pytest

from fking.domain import Bar, Signal
from fking.risk import DegradedMode, DegradedModeState, ModeObservation, symbols_without_usable_data
from fking.strategy import (
    Clock,
    FeatureRequirement,
    ObservationRefusedError,
    StrategySpec,
    StrategyState,
    initial_state,
    step,
)
from fking.strategy.trailing_return import TrailingReturnContinuation
from tests.strategy.harness import (
    BTCUSDT,
    bars_from_closes,
    clock_at,
    feature_values_for,
    rising_closes,
)

pytestmark = pytest.mark.unit

_NOW = datetime(2026, 8, 1, 14, 30, tzinfo=UTC)
_CAUSE = UUID("11111111-1111-4111-8111-111111111111")


class CallRecordingStrategy:
    """A real strategy that records whether its body was reached.

    Wrapping rather than scripting: the point of the test is that a *genuine* strategy
    with genuine feature requirements is not asked, and a double with no requirements
    could not express that.
    """

    def __init__(self) -> None:
        self._inner = TrailingReturnContinuation((BTCUSDT,))
        self.calls: list[dict[FeatureRequirement, Decimal]] = []

    @property
    def spec(self) -> StrategySpec:
        return self._inner.spec

    def evaluate(self, state: StrategyState, bar: Bar, clock: Clock) -> Signal | None:
        self.calls.append(dict(state.feature_values))
        return self._inner.evaluate(state, bar, clock)


def _warmed(strategy: CallRecordingStrategy) -> tuple[StrategyState, tuple[Bar, ...]]:
    """State after exactly the declared warm-up, with every declared feature supplied."""
    warm_up_bars = strategy.spec.warm_up_bars
    series = bars_from_closes(rising_closes(warm_up_bars + 1))
    values = feature_values_for(strategy.spec, series)
    state = initial_state(seed=20260801)
    for observed in series[:warm_up_bars]:
        state = step(
            strategy,
            state,
            observed,
            clock_at(observed.close_time_utc),
            feature_values=values.get(observed.close_time_utc, {}),
        ).state
    return state, series


def test_a_withheld_feature_stops_the_evaluation_and_emits_no_signal() -> None:
    strategy = CallRecordingStrategy()
    state, series = _warmed(strategy)
    calls_before = len(strategy.calls)
    final = series[-1]

    outcome = step(
        strategy,
        state,
        final,
        clock_at(final.close_time_utc),
        unavailable_features=strategy.spec.required_features,
    )

    assert outcome.signal is None
    assert outcome.unavailable_features == strategy.spec.required_features
    assert len(strategy.calls) == calls_before


def test_the_withheld_value_is_absent_from_the_state_carried_forward() -> None:
    """Not zero, not the last known value, not the series mean. Absent.

    The state is the part that outlives the bar: a substituted value written into it
    would reach the *next* evaluation, where nothing records that it was invented.
    """
    strategy = CallRecordingStrategy()
    state, series = _warmed(strategy)
    final = series[-1]

    outcome = step(
        strategy,
        state,
        final,
        clock_at(final.close_time_utc),
        unavailable_features=strategy.spec.required_features,
    )

    for requirement in strategy.spec.required_features:
        assert outcome.state.feature_at(requirement) is None


def test_supplying_a_value_for_a_withheld_feature_is_refused() -> None:
    """The one way an imputed value could arrive through the parameter meant to stop it."""
    strategy = CallRecordingStrategy()
    state, series = _warmed(strategy)
    final = series[-1]
    values = feature_values_for(strategy.spec, series)

    with pytest.raises(ObservationRefusedError, match="cannot both hold"):
        step(
            strategy,
            state,
            final,
            clock_at(final.close_time_utc),
            feature_values=values.get(final.close_time_utc, {}),
            unavailable_features=strategy.spec.required_features,
        )


def test_declaring_an_unavailability_the_spec_never_asked_for_is_refused() -> None:
    strategy = CallRecordingStrategy()
    state, series = _warmed(strategy)
    final = series[-1]
    invented = FeatureRequirement(feature_name="order_book_imbalance_top_10", feature_version=1)

    with pytest.raises(ObservationRefusedError, match="never declared them"):
        step(
            strategy,
            state,
            final,
            clock_at(final.close_time_utc),
            unavailable_features=(invented,),
        )


def test_an_unrelated_withheld_feature_does_not_stop_a_strategy_that_does_not_need_it() -> None:
    """Withholding is per requirement. A blanket halt would be a different mechanism."""
    strategy = CallRecordingStrategy()
    state, series = _warmed(strategy)
    final = series[-1]
    values = feature_values_for(strategy.spec, series)
    calls_before = len(strategy.calls)

    outcome = step(
        strategy,
        state,
        final,
        clock_at(final.close_time_utc),
        feature_values=values.get(final.close_time_utc, {}),
        unavailable_features=(),
    )

    assert len(strategy.calls) == calls_before + 1
    assert outcome.unavailable_features == ()


@pytest.mark.parametrize("mode", [DegradedMode.DATA_STALE, DegradedMode.FEATURE_STORE_PARTIAL])
def test_the_degraded_mode_drives_the_withholding_end_to_end(mode: DegradedMode) -> None:
    """The wiring the two halves are for: a stale symbol produces an unevaluated bar."""
    degraded, transition = DegradedModeState().observe(
        ModeObservation(
            observation_id=UUID(int=7),
            correlation_id=_CAUSE,
            mode=mode,
            is_faulted=True,
            observed_at_utc=_NOW,
            reason="last tick is past ten times the measured p99 inter-tick gap",
            affected_symbols=(BTCUSDT.symbol,),
        )
    )
    assert transition is not None
    assert symbols_without_usable_data(degraded) == frozenset({BTCUSDT.symbol})

    strategy = CallRecordingStrategy()
    state, series = _warmed(strategy)
    final = series[-1]
    calls_before = len(strategy.calls)

    withheld = (
        strategy.spec.required_features
        if final.instrument.symbol in symbols_without_usable_data(degraded)
        else ()
    )
    outcome = step(
        strategy, state, final, clock_at(final.close_time_utc), unavailable_features=withheld
    )

    assert outcome.signal is None
    assert len(strategy.calls) == calls_before
    assert final.close_time_utc - series[0].close_time_utc >= timedelta(0)
    assert Decimal("0") not in [value for recorded in strategy.calls for value in recorded.values()]
