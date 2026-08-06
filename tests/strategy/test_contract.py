"""The state a strategy reads, and the two silences the reference strategy answers with.

`StrategyState` is validated at construction rather than trusted, because the engine that
builds it is the thing most likely to be wrong: an out-of-order history reaching `evaluate`
would make `recent_bars[-1]` mean something different from what every strategy assumes it
means, and nothing about that raises.
"""

from __future__ import annotations

from decimal import Decimal

import pytest

from fking.domain import Direction
from fking.strategy import (
    FeatureRequirement,
    StrategyContractError,
    StrategyState,
    decimal_parameter,
    initial_state,
)
from fking.strategy.trailing_return import TrailingReturnContinuation
from tests.strategy.harness import BTCUSDT, bars_from_closes, clock_at, rising_closes

pytestmark = pytest.mark.unit

_TRAILING_RETURN = FeatureRequirement(feature_name="trailing_return_fraction", feature_version=1)


def test_a_negative_bar_count_is_refused() -> None:
    with pytest.raises(StrategyContractError, match="bars_consumed"):
        StrategyState(bars_consumed=-1, recent_bars=(), feature_values={}, seed=1)


def test_a_history_that_does_not_ascend_is_refused() -> None:
    """A descending or repeating history makes `recent_bars[-1]` mean something other than
    "the newest bar", which every strategy assumes it means."""
    series = bars_from_closes(rising_closes(3))

    with pytest.raises(StrategyContractError, match="ascend strictly"):
        StrategyState(
            bars_consumed=3,
            recent_bars=(series[2], series[1], series[0]),
            feature_values={},
            seed=1,
        )


def test_the_feature_mapping_cannot_be_mutated_through_the_reference_it_was_built_from() -> None:
    """`frozen=True` protects the binding, not the mapping bound to it -- so the state
    copies and proxies, and a strategy cannot observe values appearing mid-evaluation."""
    supplied: dict[FeatureRequirement, Decimal] = {_TRAILING_RETURN: Decimal("0.02")}
    state = StrategyState(bars_consumed=0, recent_bars=(), feature_values=supplied, seed=1)

    supplied[_TRAILING_RETURN] = Decimal("0.99")

    assert state.feature_at(_TRAILING_RETURN) == Decimal("0.02")


def test_a_requirement_with_no_value_reads_as_absent_rather_than_zero() -> None:
    """Zero is an opinion; absence is not, and a strategy must be able to tell them apart."""
    state = initial_state(seed=1)

    assert state.feature_at(_TRAILING_RETURN) is None


def test_visible_bars_excludes_a_bar_whose_close_has_not_happened() -> None:
    series = bars_from_closes(rising_closes(4))
    state = StrategyState(bars_consumed=4, recent_bars=series, feature_values={}, seed=1)

    visible = state.visible_bars(series[1].close_time_utc)

    assert visible == series[:2]


def test_the_reference_strategy_is_silent_when_nothing_has_closed_yet() -> None:
    """`as_of` before the first close leaves the visible window empty, and an empty window
    is no opinion rather than a flat one."""
    strategy = TrailingReturnContinuation((BTCUSDT,))
    series = bars_from_closes(rising_closes(4))
    state = StrategyState(bars_consumed=4, recent_bars=series, feature_values={}, seed=1)

    assert strategy.evaluate(state, series[0], clock_at(series[0].open_time_utc)) is None


def test_the_reference_strategy_is_silent_when_its_required_feature_has_no_value() -> None:
    strategy = TrailingReturnContinuation((BTCUSDT,))
    series = bars_from_closes(rising_closes(4))
    state = StrategyState(bars_consumed=4, recent_bars=series, feature_values={}, seed=1)

    assert strategy.evaluate(state, series[-1], clock_at(series[-1].close_time_utc)) is None


def test_the_reference_strategy_is_silent_below_its_declared_entry_threshold() -> None:
    """The threshold comes from the declared space, so this is the parameter doing work."""
    strategy = TrailingReturnContinuation((BTCUSDT,))
    series = bars_from_closes(rising_closes(4))
    declared = decimal_parameter(strategy.spec.parameters.bind(), "entry_return_fraction")
    state = StrategyState(
        bars_consumed=4,
        recent_bars=series,
        feature_values={_TRAILING_RETURN: declared / 2},
        seed=1,
    )

    assert strategy.evaluate(state, series[-1], clock_at(series[-1].close_time_utc)) is None


def test_raising_the_declared_entry_threshold_silences_a_signal_that_would_otherwise_fire() -> None:
    """The parameter is searchable because it changes behaviour, not because it is listed."""
    series = bars_from_closes(rising_closes(4))
    trailing = {_TRAILING_RETURN: Decimal("0.01")}
    state = StrategyState(bars_consumed=4, recent_bars=series, feature_values=trailing, seed=1)
    clock = clock_at(series[-1].close_time_utc)

    permissive = TrailingReturnContinuation((BTCUSDT,), {"entry_return_fraction": Decimal("0.005")})
    demanding = TrailingReturnContinuation((BTCUSDT,), {"entry_return_fraction": Decimal("0.02")})

    assert permissive.evaluate(state, series[-1], clock) is not None
    assert demanding.evaluate(state, series[-1], clock) is None


def test_a_negative_trailing_return_produces_a_short_invalidated_above_the_close() -> None:
    """The symmetric branch, which a rising-price fixture never reaches."""
    strategy = TrailingReturnContinuation((BTCUSDT,))
    series = bars_from_closes(rising_closes(4))
    state = StrategyState(
        bars_consumed=4,
        recent_bars=series,
        feature_values={_TRAILING_RETURN: Decimal("-0.03")},
        seed=1,
    )

    signal = strategy.evaluate(state, series[-1], clock_at(series[-1].close_time_utc))

    assert signal is not None
    assert signal.direction is Direction.SHORT
    assert signal.invalidation_quote_price is not None
    assert signal.invalidation_quote_price > series[-1].close_quote_price


def test_conviction_saturates_at_one_rather_than_exceeding_it() -> None:
    strategy = TrailingReturnContinuation((BTCUSDT,))
    series = bars_from_closes(rising_closes(4))
    state = StrategyState(
        bars_consumed=4,
        recent_bars=series,
        feature_values={_TRAILING_RETURN: Decimal("0.9")},
        seed=1,
    )

    signal = strategy.evaluate(state, series[-1], clock_at(series[-1].close_time_utc))

    assert signal is not None
    assert signal.conviction == Decimal("1")
