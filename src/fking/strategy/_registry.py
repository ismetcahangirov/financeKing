"""The registry: every strategy the system is allowed to run, and nothing else.

Its one job is to say **no at registration** rather than at the first bar. A strategy
that discovers on bar one that its inputs do not exist has already been scheduled,
already been charged a trial, and already produced a run whose emptiness reads downstream
as "no edge here" rather than as "no data here" -- and that misreading retires a good
hypothesis for an infrastructure reason.

Three refusals, each closing a different route:

1. **A required feature the catalogue does not declare.** The message names what the
   catalogue does hold, because the most frequent cause is an LLM-authored strategy
   requesting a feature that exists in the literature it was trained on rather than in
   this corpus (`ARCHITECTURE.md` section 6).
2. **A warm-up shorter than the longest required feature's lookback.** This is the quiet
   one. The strategy would run, the feature values would arrive, and every one computed
   inside the warm-up window would be short of its own lookback -- which produces values
   that are not wrong so much as computed from a different window than the one declared.
   Requiring the warm-up to cover it is also what makes `_runner`'s "a missing feature
   value after warm-up is an engine bug" a defensible statement rather than a hope.
3. **A duplicate `(strategy_id, strategy_version)`.** That pair is what a survival score,
   a lineage edge and an audit row key on, so keeping the last registration silently
   would attribute one strategy's realised record to a different strategy's code.

**The catalogue is injected, never imported.** `fking.strategy` does not reach into
`fking.data.features.registry`, for the reason `tools/checks/feature_registry.py`
enforces: a consumer importing a feature module is one attribute access from calling a
compute function directly, and a value computed outside `evaluate` derives no
availability stamp and reaches no store. The caller that owns both -- a backtest run, the
API, a test -- passes `FEATURES` in.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from datetime import timedelta

from fking.data.features.spec import FeatureSpec
from fking.strategy._contract import Strategy
from fking.strategy._errors import (
    DuplicateStrategyError,
    FeatureUnavailableError,
    StrategyContractError,
)
from fking.strategy._spec import StrategySpec

__all__ = ["FeatureCatalogue", "StrategyRegistry", "build_registry"]

# Exactly the shape of `fking.data.features.registry.FEATURES`. Named here so a caller
# can see what it is being asked for without importing the feature registry to find out.
FeatureCatalogue = Mapping[tuple[str, int], FeatureSpec]


class StrategyRegistry:
    """Strategies that have been validated against a feature catalogue.

    Mutable by construction, unlike everything else in this package: registration is an
    accumulation, and a frozen registry would mean rebuilding the whole mapping per
    strategy. Nothing here is a domain object, so
    `docs/rules/immutability.md` does not bind it -- but the *specs* it holds are
    frozen, and nothing it returns can be modified through the reference it hands back.
    """

    __slots__ = ("_by_key", "_features")

    def __init__(self, *, features: FeatureCatalogue) -> None:
        self._features = dict(features)
        self._by_key: dict[tuple[str, int], Strategy] = {}

    def register(self, strategy: Strategy) -> Strategy:
        """Validate `strategy`'s specification and admit it. Returns the same object."""
        spec = strategy.spec
        if spec.key in self._by_key:
            raise DuplicateStrategyError(
                f"{spec.describe()} is already registered; the id and version pair is "
                f"what a survival score, a lineage edge and an audit row all key on, so "
                f"two strategies sharing one would attribute one's record to the other"
            )
        self._require_available_features(spec)
        self._require_warm_up_covers_lookbacks(spec)
        self._by_key[spec.key] = strategy
        return strategy

    def resolve(self, strategy_id: str, strategy_version: int) -> Strategy:
        """The strategy for one `(id, version)`.

        Raises rather than returning `None`: every caller is about to run a backtest or
        dispatch a bar, and there is nothing sensible to do with a strategy nobody
        registered.
        """
        try:
            return self._by_key[(strategy_id, strategy_version)]
        except KeyError as unregistered:
            raise StrategyContractError(
                f"no strategy {strategy_id!r} at version {strategy_version} is registered; "
                f"registered strategies are {sorted(self.registered_keys)}"
            ) from unregistered

    @property
    def registered_keys(self) -> frozenset[tuple[str, int]]:
        """Every `(strategy_id, strategy_version)` this registry admitted."""
        return frozenset(self._by_key)

    def registered(self) -> tuple[Strategy, ...]:
        """Every admitted strategy, ordered by key so a report is stable across runs."""
        return tuple(self._by_key[key] for key in sorted(self._by_key))

    def __contains__(self, key: tuple[str, int]) -> bool:
        return key in self._by_key

    def __len__(self) -> int:
        return len(self._by_key)

    def _require_available_features(self, spec: StrategySpec) -> None:
        absent = sorted(
            requirement.describe()
            for requirement in spec.required_features
            if requirement.key not in self._features
        )
        if absent:
            available = sorted(f"{name} v{version}" for name, version in self._features)
            raise FeatureUnavailableError(
                f"{spec.describe()} requires {absent}, which the feature registry does "
                f"not declare. It declares {available}. Register the feature with a "
                f"lookback, an availability lag and a point-in-time proof, or change what "
                f"the strategy asks for; nothing is substituted and no partial series is "
                f"returned"
            )

    def _require_warm_up_covers_lookbacks(self, spec: StrategySpec) -> None:
        deepest = timedelta(0)
        deepest_name = ""
        for requirement in spec.required_features:
            lookback = self._features[requirement.key].lookback
            if lookback > deepest:
                deepest = lookback
                deepest_name = requirement.describe()
        if deepest > spec.warm_up_duration:
            raise StrategyContractError(
                f"{spec.describe()} declares a warm-up of {spec.warm_up_bars} bars at "
                f"{spec.shortest_bar_interval}, worth {spec.warm_up_duration}, and "
                f"requires {deepest_name} whose declared lookback is {deepest}. Every "
                f"value inside that shortfall would be computed from a shorter window "
                f"than the feature declares, which is not an error anywhere -- it is a "
                f"different feature wearing the registered one's name"
            )


def build_registry(
    strategies: Iterable[Strategy], *, features: FeatureCatalogue
) -> StrategyRegistry:
    """A registry holding `strategies`, each validated against `features`."""
    registry = StrategyRegistry(features=features)
    for strategy in strategies:
        registry.register(strategy)
    return registry
