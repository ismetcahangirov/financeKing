"""Registration is where a specification is refused, and the refusal is the whole point.

The acceptance criterion this file carries: a spec requiring a feature the store does not
declare raises at *registration time*, not at the first bar. Everything else here exists
because the same argument applies to the other two ways a spec can be unrunnable.
"""

from __future__ import annotations

from datetime import timedelta
from decimal import Decimal

import pytest

from fking.data.features.registry import FEATURES
from fking.strategy import (
    SHIPPED_STRATEGIES,
    DuplicateStrategyError,
    FeatureRequirement,
    FeatureUnavailableError,
    InvalidationRule,
    ParameterSpace,
    StrategyContractError,
    StrategyRegistry,
    StrategySpec,
    build_registry,
)
from fking.strategy.trailing_return import TrailingReturnContinuation
from tests.strategy.doubles import SilentStrategy
from tests.strategy.harness import BTCUSDT

pytestmark = pytest.mark.unit

_TRAILING_RETURN = FeatureRequirement(feature_name="trailing_return_fraction", feature_version=1)
_THESIS = (
    "A stand-in thesis long enough to be a sentence: the declared edge persists over the "
    "declared horizon and is wrong once the declared adverse move happens."
)


def spec_requiring(
    requirements: tuple[FeatureRequirement, ...],
    *,
    warm_up_bars: int = 8,
    bar_interval: timedelta = timedelta(minutes=15),
    strategy_id: str = "double",
) -> StrategySpec:
    return StrategySpec(
        strategy_id=strategy_id,
        strategy_version=1,
        thesis=_THESIS,
        instruments=(BTCUSDT,),
        bar_intervals=(bar_interval,),
        required_features=requirements,
        warm_up_bars=warm_up_bars,
        parameters=ParameterSpace(),
        invalidation=InvalidationRule(adverse_move_fraction=Decimal("0.01")),
        signal_horizon=timedelta(hours=1),
    )


def test_a_feature_the_catalogue_does_not_declare_is_refused_at_registration() -> None:
    """The acceptance criterion, stated directly.

    `register` is the last moment before the strategy is scheduled and charged a trial.
    Discovering the absence on bar one produces an empty run, and an empty run reads
    downstream as "no edge" rather than as "no data".
    """
    registry = StrategyRegistry(features=FEATURES)
    invented = FeatureRequirement(feature_name="order_book_imbalance_top_10", feature_version=1)

    with pytest.raises(FeatureUnavailableError, match="order_book_imbalance_top_10 v1"):
        registry.register(SilentStrategy(spec_requiring((invented,))))


def test_the_refusal_names_what_the_catalogue_does_hold() -> None:
    """A bare refusal sends the reader looking for a bug; a named one redirects the work."""
    registry = StrategyRegistry(features=FEATURES)
    invented = FeatureRequirement(feature_name="queue_position_estimate", feature_version=1)

    with pytest.raises(FeatureUnavailableError) as refused:
        registry.register(SilentStrategy(spec_requiring((invented,))))

    assert "trailing_return_fraction v1" in str(refused.value)


def test_a_registered_feature_at_an_unregistered_version_is_refused() -> None:
    """The version travels with the name, so v2 of a registered feature is still absent."""
    registry = StrategyRegistry(features=FEATURES)
    future_version = FeatureRequirement(feature_name="trailing_return_fraction", feature_version=2)

    with pytest.raises(FeatureUnavailableError, match="trailing_return_fraction v2"):
        registry.register(SilentStrategy(spec_requiring((future_version,))))


def test_a_warm_up_shorter_than_the_deepest_lookback_is_refused() -> None:
    """Two bars of 15 minutes cannot cover a one-hour lookback.

    Every value computed inside the shortfall would come from a shorter window than the
    feature declares -- which raises nothing, anywhere, and is a different feature wearing
    the registered one's name.
    """
    registry = StrategyRegistry(features=FEATURES)

    with pytest.raises(StrategyContractError, match="lookback"):
        registry.register(SilentStrategy(spec_requiring((_TRAILING_RETURN,), warm_up_bars=2)))


def test_a_warm_up_exactly_covering_the_lookback_is_admitted() -> None:
    """The boundary is inclusive: four 15-minute bars are exactly one hour."""
    registry = StrategyRegistry(features=FEATURES)
    registry.register(SilentStrategy(spec_requiring((_TRAILING_RETURN,), warm_up_bars=4)))

    assert ("double", 1) in registry


def test_a_strategy_requiring_nothing_is_admitted() -> None:
    """An empty requirement tuple is a legal declaration, not an oversight to reject."""
    registry = StrategyRegistry(features=FEATURES)
    registry.register(SilentStrategy(spec_requiring(())))

    assert len(registry) == 1


def test_registering_the_same_id_and_version_twice_is_refused() -> None:
    registry = StrategyRegistry(features=FEATURES)
    registry.register(SilentStrategy(spec_requiring((_TRAILING_RETURN,))))

    with pytest.raises(DuplicateStrategyError, match="already registered"):
        registry.register(SilentStrategy(spec_requiring((_TRAILING_RETURN,))))


def test_two_versions_of_one_strategy_coexist() -> None:
    """`(id, version)` is the identity, so a mutated child does not evict its parent."""
    registry = StrategyRegistry(features=FEATURES)
    first = spec_requiring((_TRAILING_RETURN,))
    second = StrategySpec(
        strategy_id=first.strategy_id,
        strategy_version=2,
        thesis=first.thesis,
        instruments=first.instruments,
        bar_intervals=first.bar_intervals,
        required_features=first.required_features,
        warm_up_bars=first.warm_up_bars,
        parameters=first.parameters,
        invalidation=first.invalidation,
        signal_horizon=first.signal_horizon,
    )
    registry.register(SilentStrategy(first))
    registry.register(SilentStrategy(second))

    assert registry.registered_keys == frozenset({("double", 1), ("double", 2)})


def test_resolving_an_unregistered_strategy_raises_rather_than_returning_none() -> None:
    registry = StrategyRegistry(features=FEATURES)

    with pytest.raises(StrategyContractError, match="is registered"):
        registry.resolve("never-written", 1)


def test_resolve_returns_the_object_that_was_registered() -> None:
    registry = StrategyRegistry(features=FEATURES)
    strategy = registry.register(TrailingReturnContinuation((BTCUSDT,)))

    assert registry.resolve("trailing-return-continuation", 1) is strategy


def test_every_shipped_strategy_registers_against_the_real_feature_catalogue() -> None:
    """The one that decays if unwritten.

    Quantified over `SHIPPED_STRATEGIES` rather than over a list maintained here, so a
    strategy added to the package inherits this with no edit. A shipped strategy declaring
    a feature nobody registered would otherwise be discovered by whoever first deployed it.
    """
    registry = build_registry(
        [build((BTCUSDT,)) for build in SHIPPED_STRATEGIES], features=FEATURES
    )

    assert len(registry) == len(SHIPPED_STRATEGIES)
    assert ("trailing-return-continuation", 1) in registry


def test_requiring_two_features_of_equal_depth_is_admitted() -> None:
    """The deepest lookback governs, and a second requirement no deeper does not change it."""
    registry = StrategyRegistry(features=FEATURES)
    volatility = FeatureRequirement(feature_name="trailing_realised_volatility", feature_version=1)
    registry.register(SilentStrategy(spec_requiring((_TRAILING_RETURN, volatility))))

    assert [strategy.spec.describe() for strategy in registry.registered()] == ["double v1"]
