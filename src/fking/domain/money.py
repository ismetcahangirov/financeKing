"""Amounts that carry the asset they are denominated in.

Most monetary values in this package are a bare `Decimal`, because an `Instrument`
is in scope and supplies the quote asset -- `Fill.fee_quote` needs no asset field
when `fill.instrument.quote_asset` answers the question exactly.

`Money` exists for the places where no `Instrument` is in scope: `Account` holds a
USDT balance and a BTC balance side by side, and `Portfolio` reports cash the same
way. Two concrete callers, which is the bar `.claude/rules/module-boundaries.md` sets
before an abstraction may exist.
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal

from fking.domain._errors import DomainError
from fking.domain._guards import require_asset_code, require_decimal, require_non_negative_decimal


@dataclass(frozen=True, slots=True)
class Money:
    """An exact quantity of one asset.

    Signed: an unrealised loss and a negative funding payment are both `Money`, and
    forcing the sign into a separate field is how a magnitude gets added where it
    should have been subtracted.
    """

    asset: str
    quantity: Decimal

    def __post_init__(self) -> None:
        require_asset_code(self.asset, "asset")
        require_decimal(self.quantity, "quantity")

    def __add__(self, other: Money) -> Money:
        """Add two amounts of the same asset.

        Mixing assets raises rather than converting. There is no exchange rate in
        `domain` and there should not be: a rate has an as-of time, a source and a
        staleness, none of which fit in an arithmetic operator.
        """
        if other.asset != self.asset:
            raise DomainError(
                f"cannot add {other.asset} to {self.asset}; convert explicitly with a "
                f"rate that carries its own as-of time"
            )
        return Money(asset=self.asset, quantity=self.quantity + other.quantity)


@dataclass(frozen=True, slots=True)
class Balance:
    """What a venue reports holding for one asset.

    Free and locked are separate because the difference is the difference between an
    order that fills and one the venue rejects for insufficient margin. Code that
    sizes against total balance will place orders the account cannot back, and the
    rejection arrives on exactly the large orders that matter most.
    """

    asset: str
    free_quantity: Decimal
    locked_quantity: Decimal

    def __post_init__(self) -> None:
        require_asset_code(self.asset, "asset")
        require_non_negative_decimal(self.free_quantity, "free_quantity")
        require_non_negative_decimal(self.locked_quantity, "locked_quantity")

    @property
    def total_quantity(self) -> Decimal:
        """Free plus locked. Never the number to size a new order against."""
        return self.free_quantity + self.locked_quantity
