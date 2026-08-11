"""The look-ahead probe, at the strategy layer, parametrised over the shipped strategies.

Two disjoint failures, two clauses.

**A strategy handed bars from the future must ignore them.** This is the asymmetry the
shared backtest/live code path exists to expose: live has no future bars, so a strategy
filtering only on a lower bound behaves identically there and leaks in backtest, where the
whole history sits in memory. The test constructs the state directly, with the future
already in it, because the runner refuses to *dispatch* a future bar and would otherwise
make the case unreachable.

**Replacing the future must not move the past.** Everything after a cut is tripled and
thirded in alternation -- nine times the return magnitude, sign inverted -- and every
signal decided at or before the cut must be byte-identical. That catches a feature read
that reached forward, a state that aliased across the cut, and any comparison against a
value the decision could not have seen.

The third test is the one that makes the first two mean anything. `_PeekingStrategy` reads
`state.recent_bars[-1]` where a correct strategy reads `state.visible_bars(as_of)[-1]`, and
the probe must fail on it. A probe never observed to fail might be asserting `True == True`.
It lives here and never in `src/`, for the same reason `tests/lookahead/leaky.py` does: a
deliberately broken implementation inside the package would eventually be registered by
somebody who read only its name.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from datetime import datetime
from decimal import Decimal

import pytest

from fking.domain import Bar, Direction, Instrument, Signal
from fking.strategy import (
    SHIPPED_STRATEGIES,
    Clock,
    FeatureRequirement,
    Strategy,
    StrategyBuilder,
    StrategySpec,
    StrategyState,
    initial_state,
    replay,
    step,
)
from fking.strategy.trailing_return import TrailingReturnContinuation
from tests.strategy.harness import (
    BTCUSDT,
    bars_for,
    clock_at,
    exercising_closes,
    feature_values_for,
    poison_after,
    signal_digest,
)

pytestmark = pytest.mark.unit

_SEED = 20260801
_BAR_COUNT = 128


def _strategy_id(build: StrategyBuilder) -> str:
    return str(getattr(build, "__name__", build))


def state_over(
    bars: Sequence[Bar], feature_values: Mapping[FeatureRequirement, Decimal]
) -> StrategyState:
    """A state holding exactly `bars`, as an engine with no filtering would have built it."""
    return StrategyState(
        bars_consumed=len(bars),
        recent_bars=tuple(bars),
        feature_values=feature_values,
        seed=_SEED,
    )


@pytest.mark.parametrize("build", SHIPPED_STRATEGIES, ids=_strategy_id)
def test_replacing_the_future_does_not_move_a_decision_taken_before_it(
    build: StrategyBuilder,
) -> None:
    strategy = build((BTCUSDT,))
    series = bars_for(strategy.spec, exercising_closes(_BAR_COUNT))
    cut_utc = series[len(series) // 2].close_time_utc

    baseline = replay(
        strategy, series, seed=_SEED, feature_values_at=feature_values_for(strategy.spec, series)
    )
    poisoned_series = poison_after(series, cut_utc=cut_utc)
    poisoned = replay(
        strategy,
        poisoned_series,
        seed=_SEED,
        feature_values_at=feature_values_for(strategy.spec, poisoned_series),
    )

    before = tuple(signal for signal in baseline if signal.decided_at_utc <= cut_utc)
    after = tuple(signal for signal in poisoned if signal.decided_at_utc <= cut_utc)

    assert before, (
        f"{strategy.spec.describe()} decided nothing at or before {cut_utc.isoformat()}, "
        f"so the comparison verified nothing"
    )
    assert signal_digest(before) == signal_digest(after), (
        f"{strategy.spec.describe()} changed a decision at or before "
        f"{cut_utc.isoformat()} when the data after it changed; it read the future"
    )


@pytest.mark.parametrize("build", SHIPPED_STRATEGIES, ids=_strategy_id)
def test_bars_timestamped_after_the_clock_are_ignored(build: StrategyBuilder) -> None:
    """The state itself carries the future, so only the strategy's own filter can save it."""
    strategy = build((BTCUSDT,))
    honest, tempted = _decide_with_and_without_the_future(strategy)

    assert honest is not None, "the decision bar must produce a signal, or nothing is compared"
    assert tempted is not None
    assert signal_digest([honest]) == signal_digest([tempted])


def test_the_probe_detects_a_deliberate_leak() -> None:
    """If this passes, the clause above is broken and its result is void."""
    honest, tempted = _decide_with_and_without_the_future(_PeekingStrategy((BTCUSDT,)))

    assert honest is not None
    assert tempted is not None
    assert signal_digest([honest]) != signal_digest([tempted]), (
        "a strategy reading state.recent_bars[-1] produced the same signal with the future "
        "in state as without it; the probe cannot distinguish a leak from correctness"
    )


class _PeekingStrategy:
    """Decides from the newest bar in state rather than the newest bar that had closed.

    Identical in live, where those are the same bar. Different in backtest, where they are
    not -- and it raises in neither.
    """

    __slots__ = ("_spec",)

    def __init__(self, instruments: tuple[Instrument, ...]) -> None:
        self._spec = TrailingReturnContinuation(instruments).spec

    @property
    def spec(self) -> StrategySpec:
        return self._spec

    def evaluate(self, state: StrategyState, bar: Bar, clock: Clock) -> Signal | None:
        if not state.recent_bars:
            return None
        peeked = state.recent_bars[-1]  # the leak, in one expression
        return Signal(
            strategy_id=self._spec.strategy_id,
            instrument=bar.instrument,
            direction=Direction.LONG,
            conviction=Decimal("0.5"),
            horizon=self._spec.signal_horizon,
            invalidation_quote_price=self._spec.invalidation.level_for(
                direction=Direction.LONG,
                reference_quote_price=peeked.close_quote_price,
                instrument=bar.instrument,
            ),
            rationale=f"decided from the newest bar in state, closing {peeked.close_quote_price}",
            decided_at_utc=clock(),
        )


def _first_signalling_index(strategy: Strategy, series: Sequence[Bar]) -> int:
    """The first bar on which `strategy` actually decides something.

    A fixed index -- the midpoint, say -- cannot serve every strategy at once: a breakout
    baseline decides on the bar that makes a new extreme and a mean-reversion baseline
    decides on one that explicitly does not, so no single bar produces a signal from both.
    Comparing "no signal" against "no signal" would pass this probe forever.
    """
    spec = strategy.spec
    values = feature_values_for(spec, series)
    state = initial_state(seed=_SEED)
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
            return index
    raise AssertionError(
        f"{spec.describe()} decided nothing over {len(series)} bars, so the probe would "
        f"compare an absence against an absence"
    )


def _decide_with_and_without_the_future(
    strategy: Strategy,
) -> tuple[Signal | None, Signal | None]:
    """One decision taken twice: once from a truthful state, once from a state holding
    bars that had not happened yet."""
    spec = strategy.spec
    series = bars_for(strategy.spec, exercising_closes(_BAR_COUNT))
    decision_index = _first_signalling_index(strategy, series)
    decision_bar = series[decision_index]
    values = feature_values_for(spec, series)[decision_bar.close_time_utc]

    window = spec.warm_up_bars + 1
    start = max(0, decision_index - window + 1)

    def clock() -> datetime:
        return decision_bar.close_time_utc

    honest = strategy.evaluate(
        state_over(series[start : decision_index + 1], values), decision_bar, clock
    )
    tempted = strategy.evaluate(
        state_over(series[start : decision_index + 1 + window], values), decision_bar, clock
    )
    return honest, tempted
