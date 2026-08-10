"""How a strategy computes the price at which its thesis is wrong.

Declared as data rather than computed in the body, for a reason that only shows up two
layers downstream. `RISK_PHILOSOPHY.md` section 3.1 sizes a position as
`q = (r_used * E) / |P_entry - P_invalidation|`, so the invalidation level *is* the
denominator of every position this system takes. A level computed by an arbitrary
expression inside `evaluate()` is a denominator that cannot be predicted from the
specification, cannot be mutated by the evolution engine, and cannot be checked against
the signal the strategy actually emitted.

Declaring it as a rule makes all three possible, and `fking.strategy.step` checks the
emitted level against the rule on every bar -- so a strategy that quietly widens its own
stop to obtain a larger position is refused rather than sized.

The snapping direction is the other half. A level is snapped *away* from the reference
price in both directions, so quantisation can only move the stop further out. Rounding
it inward would tighten the stop by up to one tick without the strategy having said so,
which increases position size on exactly the instruments whose tick is coarsest relative
to their price.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from decimal import Decimal
from typing import Final

from fking.domain import Direction, Instrument, Side
from fking.strategy._errors import StrategyContractError
from fking.strategy._guards import require_open_unit_fraction
from fking.strategy._requirements import FeatureRequirement

__all__ = ["InvalidationRule"]

_ONE: Final[Decimal] = Decimal("1")
_ZERO: Final[Decimal] = Decimal("0")

_NO_FEATURES: Final[Mapping[FeatureRequirement, Decimal]] = {}


@dataclass(frozen=True, slots=True, kw_only=True)
class InvalidationRule:
    """The adverse move, as a fraction of the reference price, that falsifies the thesis.

    One rule per strategy rather than one per signal. A strategy that chooses its
    invalidation distance per signal is choosing its own position size per signal, which
    is the authority `RISK_PHILOSOPHY.md` gives to the risk engine alone.

    **Scaling the distance off a declared feature keeps that property.** A stop at a fixed
    fraction of price is the same distance in a dead market and in a crash, so it is either
    too tight to survive normal noise or too wide to size against; every volatility-scaled
    stop in the literature exists for that reason. What makes this one safe is *where* the
    scaling is declared. `volatility_feature` names a feature the specification already
    requires, and `fking.strategy.step` recomputes the level from the same value it handed
    the strategy -- so the strategy still cannot name a distance of its own, and the level
    remains predictable from the specification, mutable by the evolution engine, and
    checkable against what was emitted.

    **The floor and the cap are not defensive coding.** The distance is the denominator of
    `q = (r_used * E) / |P_entry - P_invalidation|`. An unbounded feature-derived
    denominator turns a stalled market -- where a realised-volatility estimate collapses
    towards zero -- into an unbounded position, and it does so through arithmetic no
    reviewer sees. `adverse_move_fraction` is that floor whether or not a feature is
    declared, and `maximum_adverse_move_fraction` is the other end: a volatility spike must
    not be able to widen the stop past the point where the position stops being
    interpretable.
    """

    adverse_move_fraction: Decimal
    volatility_feature: FeatureRequirement | None = None
    volatility_multiple: Decimal = _ZERO
    maximum_adverse_move_fraction: Decimal | None = None

    def __post_init__(self) -> None:
        require_open_unit_fraction(self.adverse_move_fraction, "adverse_move_fraction")
        if self.volatility_feature is None:
            if self.volatility_multiple != _ZERO:
                raise StrategyContractError(
                    f"volatility_multiple is {self.volatility_multiple} and no "
                    f"volatility_feature is declared; a multiple of nothing is a number "
                    f"that reads as a scaled stop and behaves as a fixed one"
                )
            if self.maximum_adverse_move_fraction is not None:
                raise StrategyContractError(
                    "maximum_adverse_move_fraction caps a scaled distance and no "
                    "volatility_feature is declared; a fixed fraction is already its own cap"
                )
            return
        if self.volatility_multiple <= _ZERO:
            raise StrategyContractError(
                f"volatility_multiple must be positive when "
                f"{self.volatility_feature.describe()} scales the distance; got "
                f"{self.volatility_multiple}"
            )
        if self.maximum_adverse_move_fraction is None:
            raise StrategyContractError(
                f"{self.volatility_feature.describe()} scales the invalidation distance "
                f"and no maximum_adverse_move_fraction bounds it; the distance is the "
                f"denominator of every position sized from this strategy, and an "
                f"unbounded one is an unbounded quantity"
            )
        require_open_unit_fraction(
            self.maximum_adverse_move_fraction, "maximum_adverse_move_fraction"
        )
        if self.maximum_adverse_move_fraction < self.adverse_move_fraction:
            raise StrategyContractError(
                f"maximum_adverse_move_fraction {self.maximum_adverse_move_fraction} is "
                f"below the floor {self.adverse_move_fraction}; the bounds cross, so no "
                f"distance satisfies both"
            )

    def adverse_move_fraction_for(
        self, feature_values: Mapping[FeatureRequirement, Decimal] | None = None
    ) -> Decimal:
        """The distance this rule produces given the values the decision was taken on.

        Both bounds are applied to the scaled value rather than to the feature, so the
        clamp is visible in the fraction a reader can compare against the emitted level.
        """
        if self.volatility_feature is None:
            return self.adverse_move_fraction
        supplied = _NO_FEATURES if feature_values is None else feature_values
        observed = supplied.get(self.volatility_feature)
        if observed is None:
            raise StrategyContractError(
                f"the invalidation distance scales off "
                f"{self.volatility_feature.describe()} and no value for it was supplied; "
                f"substituting the floor would size a position against a distance nobody "
                f"declared"
            )
        if observed < _ZERO:
            raise StrategyContractError(
                f"{self.volatility_feature.describe()} reported {observed}; a dispersion "
                f"cannot be negative, and the value would invert the stop through the "
                f"entry price"
            )
        scaled = self.volatility_multiple * observed
        if scaled < self.adverse_move_fraction:
            return self.adverse_move_fraction
        if (
            self.maximum_adverse_move_fraction is not None
            and scaled > self.maximum_adverse_move_fraction
        ):
            return self.maximum_adverse_move_fraction
        return scaled

    def level_for(
        self,
        *,
        direction: Direction,
        reference_quote_price: Decimal,
        instrument: Instrument,
        feature_values: Mapping[FeatureRequirement, Decimal] | None = None,
    ) -> Decimal:
        """The price at which a `direction` thesis taken at `reference_quote_price` is wrong."""
        if direction is Direction.FLAT:
            raise StrategyContractError(
                "a flat signal asserts nothing, so it has no invalidation level; "
                "Signal refuses one on a flat direction for the same reason"
            )
        if reference_quote_price <= _ZERO:
            raise StrategyContractError(
                f"reference_quote_price must be positive; got {reference_quote_price}"
            )

        distance = self.adverse_move_fraction_for(feature_values)
        if direction is Direction.LONG:
            # Below the entry, and snapped down: a long is wrong when price falls, and
            # ROUND_DOWN moves the level further below the entry rather than nearer.
            raw = reference_quote_price * (_ONE - distance)
            snapped = instrument.quantize_quote_price(raw, Side.BUY)
        else:
            raw = reference_quote_price * (_ONE + distance)
            snapped = instrument.quantize_quote_price(raw, Side.SELL)

        if snapped <= _ZERO:
            raise StrategyContractError(
                f"{instrument.symbol} has a tick size of {instrument.tick_size}, which is "
                f"coarser than the whole invalidation distance at a reference price of "
                f"{reference_quote_price}; the level snaps to {snapped} and the thesis "
                f"cannot be expressed on this instrument"
            )
        return snapped
