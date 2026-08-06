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

from dataclasses import dataclass
from decimal import Decimal
from typing import Final

from fking.domain import Direction, Instrument, Side
from fking.strategy._errors import StrategyContractError
from fking.strategy._guards import require_open_unit_fraction

__all__ = ["InvalidationRule"]

_ONE: Final[Decimal] = Decimal("1")
_ZERO: Final[Decimal] = Decimal("0")


@dataclass(frozen=True, slots=True, kw_only=True)
class InvalidationRule:
    """The adverse move, as a fraction of the reference price, that falsifies the thesis.

    One rule per strategy rather than one per signal. A strategy that chooses its
    invalidation distance per signal is choosing its own position size per signal, which
    is the authority `RISK_PHILOSOPHY.md` gives to the risk engine alone.
    """

    adverse_move_fraction: Decimal

    def __post_init__(self) -> None:
        require_open_unit_fraction(self.adverse_move_fraction, "adverse_move_fraction")

    def level_for(
        self,
        *,
        direction: Direction,
        reference_quote_price: Decimal,
        instrument: Instrument,
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

        if direction is Direction.LONG:
            # Below the entry, and snapped down: a long is wrong when price falls, and
            # ROUND_DOWN moves the level further below the entry rather than nearer.
            raw = reference_quote_price * (_ONE - self.adverse_move_fraction)
            snapped = instrument.quantize_quote_price(raw, Side.BUY)
        else:
            raw = reference_quote_price * (_ONE + self.adverse_move_fraction)
            snapped = instrument.quantize_quote_price(raw, Side.SELL)

        if snapped <= _ZERO:
            raise StrategyContractError(
                f"{instrument.symbol} has a tick size of {instrument.tick_size}, which is "
                f"coarser than the whole invalidation distance at a reference price of "
                f"{reference_quote_price}; the level snaps to {snapped} and the thesis "
                f"cannot be expressed on this instrument"
            )
        return snapped
