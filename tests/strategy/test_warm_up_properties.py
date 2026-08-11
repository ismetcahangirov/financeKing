"""Over arbitrary bar counts and arbitrary price paths, no signal precedes the warm-up.

Example-based tests confirm the counts somebody thought of. The interesting cases are the
ones nobody enumerates: a series exactly one bar shorter than the warm-up, one exactly at
it, a path that never crosses the entry threshold at all, and a path that crosses it on
the very first bar -- which is precisely where a strategy that policed its own warm-up
with `>` instead of `>=` would leak one decision.

Quantified over `SHIPPED_STRATEGIES`, so a strategy added to the package inherits this
property with no edit here.
"""

from __future__ import annotations

from decimal import Decimal

import pytest
from hypothesis import given
from hypothesis import strategies as st

from fking.strategy import SHIPPED_STRATEGIES, StrategyBuilder, initial_state, step
from tests.strategy.harness import BTCUSDT, bars_for, clock_at, feature_values_for

pytestmark = [pytest.mark.property, pytest.mark.unit]

# Comfortably inside the range where a 0.01 tick and a 1% adverse move produce an
# invalidation level several ticks away from the entry. Prices near the tick size would
# make the level snap to zero, which is a real refusal but a different test's subject.
_CLOSES = st.decimals(
    min_value=Decimal("1000"),
    max_value=Decimal("150000"),
    places=2,
    allow_nan=False,
    allow_infinity=False,
)


def _strategy_id(build: StrategyBuilder) -> str:
    return str(getattr(build, "__name__", build))


@pytest.mark.parametrize("build", SHIPPED_STRATEGIES, ids=_strategy_id)
@given(closes=st.lists(_CLOSES, min_size=1, max_size=40))
def test_no_signal_is_emitted_before_the_warm_up_is_consumed(
    build: StrategyBuilder, closes: list[Decimal]
) -> None:
    strategy = build((BTCUSDT,))
    warm_up_bars = strategy.spec.warm_up_bars
    series = bars_for(strategy.spec, tuple(closes))
    values = feature_values_for(strategy.spec, series)

    state = initial_state(seed=20260801)
    for index, observed in enumerate(series):
        outcome = step(
            strategy,
            state,
            observed,
            clock_at(observed.close_time_utc),
            feature_values=values[observed.close_time_utc],
        )
        state = outcome.state
        if index < warm_up_bars - 1:
            assert outcome.signal is None, (
                f"{strategy.spec.describe()} emitted on bar {index + 1} of a declared "
                f"{warm_up_bars}-bar warm-up"
            )
        assert state.bars_consumed == index + 1


@pytest.mark.parametrize("build", SHIPPED_STRATEGIES, ids=_strategy_id)
@given(closes=st.lists(_CLOSES, min_size=1, max_size=40))
def test_every_emitted_signal_satisfies_the_domain_invariants(
    build: StrategyBuilder, closes: list[Decimal]
) -> None:
    """Conviction inside `[0, 1]`, a directional signal always carrying a level.

    `Signal.__post_init__` enforces both, so this asserts the strategy never constructs an
    argument set that would raise -- which on an arbitrary price path is where a
    conviction formula divides by something it assumed was positive.
    """
    strategy = build((BTCUSDT,))
    series = bars_for(strategy.spec, tuple(closes))
    values = feature_values_for(strategy.spec, series)

    state = initial_state(seed=20260801)
    for observed in series:
        outcome = step(
            strategy,
            state,
            observed,
            clock_at(observed.close_time_utc),
            feature_values=values[observed.close_time_utc],
        )
        state = outcome.state
        if outcome.signal is None:
            continue
        assert Decimal("0") <= outcome.signal.conviction <= Decimal("1")
        assert outcome.signal.is_actionable == (outcome.signal.invalidation_quote_price is not None)
