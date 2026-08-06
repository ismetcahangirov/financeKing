"""Two replays of one bar sequence under one seed produce a byte-identical signal stream.

This is the acceptance criterion, and it is not a formality. `EVOLUTION_ENGINE.md` scores
strategies on replayed history; a strategy whose replay does not reproduce makes the
survival score a measurement of noise, and the evolution engine then breeds toward the
noise. The failure is silent in every other respect -- two runs both complete, both emit
signals, and only a digest comparison notices they disagree.

The digest is exact decimal text, so a `1e-15` difference fails. That sensitivity is
asserted here rather than assumed: a comparison that cannot see a small difference will
pass on the nondeterminism that only moves the fifteenth digit today.
"""

from __future__ import annotations

from dataclasses import replace
from decimal import Decimal

import pytest

from fking.strategy import SHIPPED_STRATEGIES, StrategyBuilder, replay
from tests.strategy.harness import (
    BTCUSDT,
    bars_from_closes,
    feature_values_for,
    rising_closes,
    signal_digest,
)

pytestmark = pytest.mark.unit

_SEED = 20260801
_BAR_COUNT = 48


def _strategy_id(build: StrategyBuilder) -> str:
    """The builder's own name, so a failing parameter id names the strategy."""
    return str(getattr(build, "__name__", build))


@pytest.mark.parametrize("build", SHIPPED_STRATEGIES, ids=_strategy_id)
def test_replaying_the_same_bars_under_the_same_seed_is_byte_identical(
    build: StrategyBuilder,
) -> None:
    strategy = build((BTCUSDT,))
    series = bars_from_closes(rising_closes(_BAR_COUNT))
    values = feature_values_for(strategy.spec, series)

    once = replay(strategy, series, seed=_SEED, feature_values_at=values)
    twice = replay(strategy, series, seed=_SEED, feature_values_at=values)

    assert once, "an empty signal stream digests identically to another empty one"
    assert signal_digest(once) == signal_digest(twice)


@pytest.mark.parametrize("build", SHIPPED_STRATEGIES, ids=_strategy_id)
def test_a_fresh_strategy_instance_replays_to_the_same_stream(build: StrategyBuilder) -> None:
    """State cannot survive between instances either.

    The first test would pass on a strategy caching something on `self`, because it reuses
    one object. This one constructs a second instance from the same declared defaults.
    """
    series = bars_from_closes(rising_closes(_BAR_COUNT))
    first = build((BTCUSDT,))
    second = build((BTCUSDT,))
    values = feature_values_for(first.spec, series)

    assert signal_digest(
        replay(first, series, seed=_SEED, feature_values_at=values)
    ) == signal_digest(replay(second, series, seed=_SEED, feature_values_at=values))


def test_the_digest_rejects_a_one_femto_perturbation() -> None:
    """`1e-15` is a failure, not a rounding difference.

    A nondeterminism that only moves the fifteenth digit today moves the third digit on a
    different fold, and a comparison that tolerates the first will pass the second.
    """
    strategy = SHIPPED_STRATEGIES[0]((BTCUSDT,))
    series = bars_from_closes(rising_closes(_BAR_COUNT))
    signals = replay(
        strategy,
        series,
        seed=_SEED,
        feature_values_at=feature_values_for(strategy.spec, series),
    )
    assert signals

    nudged = (
        replace(signals[0], conviction=signals[0].conviction - Decimal("1e-15")),
        *signals[1:],
    )
    assert signal_digest(signals) != signal_digest(nudged)


def test_the_digest_distinguishes_values_that_compare_equal() -> None:
    """`Decimal("0.5") == Decimal("0.50")` is `True`, and they are not the same emission.

    A rescaling that changed a quantum without changing a quantity is a change to what the
    audit row will hold, and the digest's job is to notice changes rather than to agree
    with `__eq__`.
    """
    strategy = SHIPPED_STRATEGIES[0]((BTCUSDT,))
    series = bars_from_closes(rising_closes(_BAR_COUNT))
    signals = replay(
        strategy,
        series,
        seed=_SEED,
        feature_values_at=feature_values_for(strategy.spec, series),
    )
    assert signals

    tenth = (replace(signals[0], conviction=Decimal("0.5")), *signals[1:])
    padded = (replace(signals[0], conviction=Decimal("0.50")), *signals[1:])

    assert tenth[0].conviction == padded[0].conviction
    assert signal_digest(tenth) != signal_digest(padded)
