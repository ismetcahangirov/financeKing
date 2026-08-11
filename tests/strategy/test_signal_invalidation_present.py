"""Every non-flat signal names the price that would prove it wrong. All of them, always.

`Signal.__post_init__` already refuses a directional signal without an invalidation, so
this file could be read as testing the domain type twice. It is not. The failure it guards
against is a strategy that avoids the refusal by *never emitting a directional signal at
all* -- returning `None` on the path that would have needed a level, or emitting flat --
and that failure is invisible to a type check and to every test that asserts what a signal
contains. The assertion here is over a run: signals were emitted, they were directional,
and each one carried a falsification price.

The second half matters more than the first. A level is only load-bearing if it is the one
the declared rule produces, because that level is the denominator of every position sized
from the signal (`RISK_PHILOSOPHY.md` section 3.1). `fking.strategy.step` enforces that on
every bar; this asserts it end to end over a replay, including for the volatility-scaled
rules, where the distance moves with a feature and a strategy could plausibly have used a
stale value or one from the wrong bar.
"""

from __future__ import annotations

import pytest

from fking.domain import Direction
from fking.strategy import SHIPPED_STRATEGIES, StrategyBuilder, initial_state, step
from tests.strategy.harness import (
    BTCUSDT,
    bars_for,
    clock_at,
    exercising_closes,
    feature_values_for,
)

pytestmark = pytest.mark.unit

_SEED = 20260801
_BAR_COUNT = 128


def _strategy_id(build: StrategyBuilder) -> str:
    return str(getattr(build, "__name__", build))


@pytest.mark.parametrize("build", SHIPPED_STRATEGIES, ids=_strategy_id)
def test_every_non_flat_signal_carries_an_invalidation_price(build: StrategyBuilder) -> None:
    strategy = build((BTCUSDT,))
    series = bars_for(strategy.spec, exercising_closes(_BAR_COUNT))
    values = feature_values_for(strategy.spec, series)

    state = initial_state(seed=_SEED)
    directional = 0
    for observed in series:
        outcome = step(
            strategy,
            state,
            observed,
            clock_at(observed.close_time_utc),
            feature_values=values[observed.close_time_utc],
        )
        state = outcome.state
        signal = outcome.signal
        if signal is None or signal.direction is Direction.FLAT:
            continue
        directional += 1
        assert signal.invalidation_quote_price is not None, (
            f"{strategy.spec.describe()} emitted a {signal.direction} signal with no "
            f"invalidation price; there is nothing for the fixed-fractional denominator "
            f"to use and nothing to rest at the venue when the kill switch trips"
        )
        assert signal.invalidation_quote_price > 0

    assert directional, (
        f"{strategy.spec.describe()} emitted no directional signal over {_BAR_COUNT} bars, "
        f"so the assertion above ranged over nothing"
    )


@pytest.mark.parametrize("build", SHIPPED_STRATEGIES, ids=_strategy_id)
def test_the_emitted_level_sits_on_the_losing_side_of_the_decision_close(
    build: StrategyBuilder,
) -> None:
    """A long is invalidated below the close and a short above it.

    `step` already checks the level against the declared rule, which is the stronger
    statement. This one is the sanity check that survives a rule and a strategy agreeing
    with each other and both being wrong -- an inverted sign there yields a stop that is
    already breached at entry, which the risk engine sizes as a near-zero denominator.
    """
    strategy = build((BTCUSDT,))
    series = bars_for(strategy.spec, exercising_closes(_BAR_COUNT))
    values = feature_values_for(strategy.spec, series)

    state = initial_state(seed=_SEED)
    compared = 0
    for observed in series:
        outcome = step(
            strategy,
            state,
            observed,
            clock_at(observed.close_time_utc),
            feature_values=values[observed.close_time_utc],
        )
        state = outcome.state
        signal = outcome.signal
        if signal is None or signal.invalidation_quote_price is None:
            continue
        compared += 1
        if signal.direction is Direction.LONG:
            assert signal.invalidation_quote_price < observed.close_quote_price
        else:
            assert signal.invalidation_quote_price > observed.close_quote_price

    assert compared
