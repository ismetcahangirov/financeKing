"""What is being traded, and the lattice the venue will accept it on."""

from __future__ import annotations

from dataclasses import dataclass
from decimal import ROUND_DOWN, ROUND_UP, Decimal
from typing import Final

from fking.domain._errors import DomainError
from fking.domain._guards import require_asset_code, require_positive_decimal, require_text
from fking.domain.enums import Side, Venue

_ONE: Final = Decimal("1")


def _snap(quantity: Decimal, step: Decimal, rounding: str) -> Decimal:
    """Round `quantity` onto the lattice of multiples of `step`.

    Division onto the lattice rather than `Decimal.quantize(step)`, because Binance
    step sizes are not always powers of ten -- `0.005` occurs -- and `quantize` can
    only snap to a decimal exponent. A value quantized to two places is still off the
    0.005 lattice half the time, and the venue rejects it with `-1013` on a value that
    prints as if it were correct.
    """
    return (quantity / step).quantize(_ONE, rounding=rounding) * step


@dataclass(frozen=True, slots=True)
class Instrument:
    """A tradable symbol on one venue, with the filters that symbol enforces.

    The filters live here rather than in `execution` because they change what a
    quantity *means*: a base quantity that is not a multiple of `lot_step` is not a
    slightly imprecise order, it is not an order at all. A backtest that fills such a
    quantity is measuring a trade the venue would have refused.
    """

    venue: Venue
    symbol: str
    base_asset: str
    quote_asset: str
    tick_size: Decimal
    lot_step: Decimal
    min_notional_quote: Decimal

    def __post_init__(self) -> None:
        # Not require_asset_code: Binance spot testnet serves a deliberate non-ASCII
        # symbol in exchangeInfo, and whatever code points the venue sent are what we
        # must send back. Classifying a symbol as tradable is the venue adapter's job
        # (docs/rules/exchange-integration.md); refusing to hold it is not.
        require_text(self.symbol, "symbol")
        require_asset_code(self.base_asset, "base_asset")
        require_asset_code(self.quote_asset, "quote_asset")
        require_positive_decimal(self.tick_size, "tick_size")
        require_positive_decimal(self.lot_step, "lot_step")
        require_positive_decimal(self.min_notional_quote, "min_notional_quote")
        if self.base_asset == self.quote_asset:
            raise DomainError(
                f"{self.symbol} cannot have {self.base_asset} as both base and quote asset"
            )

    def quantize_base_quantity(self, base_quantity: Decimal) -> Decimal:
        """Snap a quantity onto `lot_step`, toward zero.

        `ROUND_DOWN` truncates toward zero, so a short's magnitude also shrinks.
        `ROUND_FLOOR` would round -1.005 down to -1.01 and *increase* the short by one
        step, which is a larger position than the risk engine authorised.
        """
        return _snap(base_quantity, self.lot_step, ROUND_DOWN)

    def quantize_quote_price(self, quote_price: Decimal, side: Side) -> Decimal:
        """Snap a price onto `tick_size`, away from crossing the book.

        A bid rounds down and an ask rounds up, so snapping never reaches further into
        the book than the decision intended. Rounding a bid up by one tick is a
        different trade from the one the risk engine priced, and it is systematically
        the worse one.
        """
        rounding = ROUND_DOWN if side is Side.BUY else ROUND_UP
        return _snap(quote_price, self.tick_size, rounding)

    def notional_quote(self, base_quantity: Decimal, quote_price: Decimal) -> Decimal:
        """Value of `base_quantity` at `quote_price`, in the quote asset."""
        return base_quantity * quote_price

    def meets_min_notional(self, base_quantity: Decimal, quote_price: Decimal) -> bool:
        """Whether an order of this quantity at this price clears the venue's floor.

        Magnitude, not signed value: a short of 0.001 BTC and a long of 0.001 BTC face
        the same MIN_NOTIONAL filter.
        """
        return abs(self.notional_quote(base_quantity, quote_price)) >= self.min_notional_quote
