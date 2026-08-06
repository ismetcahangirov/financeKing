"""What a strategy is, as a frozen typed object rather than as prose in a docstring.

Everything downstream reads this. The engine subscribes instruments and intervals from
it, the registry validates required features against the feature catalogue before a
single bar is dispatched, the runner suppresses signals until the declared warm-up has
elapsed, the evolution engine mutates inside the declared parameter bounds, and the risk
engine sizes against the level the declared invalidation rule produces.

The consequence worth stating: **anything a strategy does that is not in here cannot be
reproduced from the specification alone.** A bar interval it reads but did not declare
is data the backtest never gated. A warm-up it polices internally is a warm-up the
walk-forward embargo cannot account for. A threshold chosen by hand is a search the
trial ledger was never charged for. Each of those is invisible in a diff of the spec,
which is precisely where a reviewer looks.

Every field is required and keyword-only. Omitting one is a `TypeError` that names it,
which is a better failure than a default -- and the values somebody would default are
`warm_up_bars=0` and `required_features=()`, both of which are the permissive answer.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import timedelta
from typing import Final

from fking.domain import Instrument
from fking.strategy._errors import StrategyContractError
from fking.strategy._guards import (
    require_positive_duration,
    require_positive_int,
    require_text,
)
from fking.strategy._invalidation import InvalidationRule
from fking.strategy._parameters import ParameterSpace

__all__ = ["FeatureRequirement", "StrategySpec"]

# A thesis shorter than this is not a sentence. The bound is deliberately low: the check
# is against an empty or placeholder string, not an attempt to grade prose.
_MINIMUM_THESIS_CHARACTERS: Final[int] = 30


@dataclass(frozen=True, slots=True)
class FeatureRequirement:
    """One feature series a strategy needs, named the way the feature registry keys it.

    `feature_version` travels with the name everywhere, for the reason
    `fking.data.features.spec` gives: a reference carrying only a name resolves to
    "whatever definition is current", and that is the read that makes a historical result
    irreproducible. A strategy validated against v1 and run against v2 is a different
    experiment with the same lineage id.
    """

    feature_name: str
    feature_version: int

    def __post_init__(self) -> None:
        require_text(self.feature_name, "feature_name")
        require_positive_int(self.feature_version, "feature_version")

    @property
    def key(self) -> tuple[str, int]:
        """The `(name, version)` pair `fking.data.features.registry` keys `FEATURES` on."""
        return (self.feature_name, self.feature_version)

    def describe(self) -> str:
        return f"{self.feature_name} v{self.feature_version}"


@dataclass(frozen=True, slots=True, kw_only=True)
class StrategySpec:
    """A registrable strategy declaration."""

    strategy_id: str
    strategy_version: int
    thesis: str
    instruments: tuple[Instrument, ...]
    bar_intervals: tuple[timedelta, ...]
    required_features: tuple[FeatureRequirement, ...]
    warm_up_bars: int
    parameters: ParameterSpace
    invalidation: InvalidationRule
    signal_horizon: timedelta

    def __post_init__(self) -> None:
        require_text(self.strategy_id, "strategy_id")
        require_positive_int(self.strategy_version, "strategy_version")
        thesis = require_text(self.thesis, "thesis")
        if len(thesis) < _MINIMUM_THESIS_CHARACTERS:
            raise StrategyContractError(
                f"the thesis for {self.strategy_id} is {len(thesis)} characters and states "
                f"nothing falsifiable; say what is believed and what would disprove it"
            )
        require_positive_int(self.warm_up_bars, "warm_up_bars")
        require_positive_duration(self.signal_horizon, "signal_horizon")
        self._require_declared_instruments()
        self._require_declared_intervals()
        self._require_distinct_features()

        # The defaults are bound once here so that a spec carrying a default outside its
        # own bounds is refused at declaration rather than at the first construction of
        # a strategy from it.
        self.parameters.bind()

    def _require_declared_instruments(self) -> None:
        if not self.instruments:
            raise StrategyContractError(
                f"{self.strategy_id} declares no instruments; the engine subscribes from "
                f"this field, so a strategy that names none is one that reads bars the "
                f"backtest never gated"
            )
        symbols = [instrument.symbol for instrument in self.instruments]
        if len(set(symbols)) != len(symbols):
            raise StrategyContractError(
                f"{self.strategy_id} declares a duplicate instrument in {sorted(symbols)}"
            )

    def _require_declared_intervals(self) -> None:
        if not self.bar_intervals:
            raise StrategyContractError(
                f"{self.strategy_id} declares no bar intervals; the interval is what "
                f"turns warm_up_bars into a duration, and a strategy that does not state "
                f"it cannot have its embargo sized"
            )
        for interval in self.bar_intervals:
            require_positive_duration(interval, "bar_interval")
        if len(set(self.bar_intervals)) != len(self.bar_intervals):
            raise StrategyContractError(
                f"{self.strategy_id} declares a duplicate bar interval in "
                f"{sorted(self.bar_intervals)}"
            )

    def _require_distinct_features(self) -> None:
        keys = [requirement.key for requirement in self.required_features]
        if len(set(keys)) != len(keys):
            raise StrategyContractError(
                f"{self.strategy_id} requires the same feature twice: "
                f"{sorted(requirement.describe() for requirement in self.required_features)}"
            )

    @property
    def key(self) -> tuple[str, int]:
        """The registry key. An id alone is not an identity -- versions coexist."""
        return (self.strategy_id, self.strategy_version)

    @property
    def shortest_bar_interval(self) -> timedelta:
        """The shortest interval declared.

        `warm_up_duration` is measured against this rather than the longest, because it
        is the conservative direction: it produces the *smallest* duration the warm-up
        can be worth, so a warm-up that clears a feature's lookback against this clears
        it against every declared interval.
        """
        return min(self.bar_intervals)

    @property
    def warm_up_duration(self) -> timedelta:
        """How much history the warm-up is worth, at the shortest declared interval."""
        return self.shortest_bar_interval * self.warm_up_bars

    def declares(self, instrument: Instrument) -> bool:
        """Whether this strategy subscribed to `instrument`."""
        return instrument in self.instruments

    def declares_interval(self, interval: timedelta) -> bool:
        """Whether this strategy subscribed to bars of length `interval`."""
        return interval in self.bar_intervals

    def describe(self) -> str:
        return f"{self.strategy_id} v{self.strategy_version}"
