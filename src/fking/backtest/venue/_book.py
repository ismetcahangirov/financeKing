"""The modelled book: a touch quote and the traded range it has to stay inside.

There is no order book in the archive. What exists is a closed bar and a calibrated
spread distribution, so the quote is built from the bar's close as a mid with half the
calibrated spread on each side, and the depth behind it is the calibrated
`DepthProfile` -- the quoted top-of-book quantity and the +-1% band, and nothing else
(`SOURCES.md` section 2, VF-017).

The bar is kept alongside the quote rather than being reduced to it, because every fill
this venue prints has to be inside `[low, high]`. That is not a sanity check bolted on
afterwards: a price outside the bar's range is a price at which nobody traded, so a fill
there is a trade with a counterparty that did not exist, and it is worth exactly as much
as the difference between it and the nearest real print.

A quote is built only from a bar that has *closed*. `MarketDataEvent` dispatches a bar at
`close_time_utc` for the same reason, and the two facts have to agree -- a quote derived
from a bar whose high is not yet a fact is look-ahead with no error attached
(`.claude/rules/no-lookahead.md`).
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from decimal import ROUND_DOWN, ROUND_UP, Decimal
from typing import Final

from fking.backtest.costs import BPS_PER_UNIT, DepthProfile
from fking.backtest.venue._errors import VenueSimulationError
from fking.domain import Bar, Instrument, Side

_TWO: Final = Decimal("2")


@dataclass(frozen=True, slots=True)
class TouchQuote:
    """The top of the modelled book as of one closed bar, plus that bar's traded range.

    `bid_quote_price` and `ask_quote_price` may sit outside `[low_quote_price,
    high_quote_price]` -- a quote is not a trade, and a wide spread around a close that
    was itself the bar's extreme puts one side beyond the range legitimately. Fills are
    clamped into the range; quotes are not, because clamping the quote would narrow the
    modelled spread exactly when the market was widest.
    """

    instrument: Instrument
    as_of_utc: datetime
    bid_quote_price: Decimal
    ask_quote_price: Decimal
    low_quote_price: Decimal
    high_quote_price: Decimal
    base_volume: Decimal
    depth: DepthProfile

    def __post_init__(self) -> None:
        if self.bid_quote_price > self.ask_quote_price:
            raise VenueSimulationError(
                f"{self.instrument.symbol} quote at {self.as_of_utc.isoformat()} is "
                f"crossed: bid {self.bid_quote_price} above ask {self.ask_quote_price}"
            )

    def touch_for(self, side: Side) -> Decimal:
        """The price an aggressor on `side` pays: a buy lifts the ask, a sell hits the bid."""
        return self.ask_quote_price if side is Side.BUY else self.bid_quote_price

    def clamp_into_range(self, quote_price: Decimal) -> Decimal:
        """Pull a modelled fill price back inside the bar's traded range.

        Clamped rather than rejected because the price is modelled -- the spread and the
        depth walk are both estimates, and an estimate landing a tick outside a real
        range is an estimate, not a causality violation. What it must never do is stay
        outside, so the direction of the clamp is always toward a price somebody
        actually traded at.
        """
        if quote_price < self.low_quote_price:
            return self.low_quote_price
        if quote_price > self.high_quote_price:
            return self.high_quote_price
        return quote_price


def quote_from_bar(bar: Bar, *, spread_bps: Decimal, depth: DepthProfile) -> TouchQuote:
    """Build the modelled touch from a closed bar and a calibrated spread.

    The mid is the bar's close, not its VWAP or its typical price: the close is the last
    thing that was true about the interval, and it is the only one of the three that a
    live venue would also have been showing at that instant.

    Each side is snapped away from the mid -- the bid down, the ask up -- so tick
    rounding can only widen the modelled spread. Rounding a bid up would hand the
    simulator a better price than the lattice allows, one tick at a time, on every fill.
    """
    if spread_bps < 0:
        raise VenueSimulationError(f"spread_bps must not be negative; got {spread_bps}")
    half_spread = bar.close_quote_price * spread_bps / BPS_PER_UNIT / _TWO
    instrument = bar.instrument
    tick = instrument.tick_size
    bid = ((bar.close_quote_price - half_spread) / tick).quantize(
        Decimal("1"), rounding=ROUND_DOWN
    ) * tick
    ask = ((bar.close_quote_price + half_spread) / tick).quantize(
        Decimal("1"), rounding=ROUND_UP
    ) * tick
    return TouchQuote(
        instrument=instrument,
        as_of_utc=bar.close_time_utc,
        bid_quote_price=bid,
        ask_quote_price=ask,
        low_quote_price=bar.low_quote_price,
        high_quote_price=bar.high_quote_price,
        base_volume=bar.base_volume,
        depth=depth,
    )
