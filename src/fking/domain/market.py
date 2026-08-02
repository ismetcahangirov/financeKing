"""Observations of the market: one aggregated bar, one individual trade."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal

from fking.domain._errors import DomainError
from fking.domain._guards import (
    require_non_negative_decimal,
    require_positive_decimal,
    require_text,
    require_utc,
)
from fking.domain.enums import Side
from fking.domain.instrument import Instrument


@dataclass(frozen=True, slots=True)
class Bar:
    """One OHLCV interval, closed.

    A `Bar` object only ever describes a *finished* interval. There is no `is_closed`
    flag, because a flag is a thing every consumer must remember to check and one of
    them will not -- and the one that does not is computing a feature from a partial
    bar, which is look-ahead that produces a plausible equity curve rather than an
    error (`.claude/rules/no-lookahead.md`).
    """

    instrument: Instrument
    open_time_utc: datetime
    close_time_utc: datetime
    open_quote_price: Decimal
    high_quote_price: Decimal
    low_quote_price: Decimal
    close_quote_price: Decimal
    base_volume: Decimal
    trade_count: int

    def __post_init__(self) -> None:
        require_utc(self.open_time_utc, "open_time_utc")
        require_utc(self.close_time_utc, "close_time_utc")
        require_positive_decimal(self.open_quote_price, "open_quote_price")
        require_positive_decimal(self.high_quote_price, "high_quote_price")
        require_positive_decimal(self.low_quote_price, "low_quote_price")
        require_positive_decimal(self.close_quote_price, "close_quote_price")
        require_non_negative_decimal(self.base_volume, "base_volume")

        if self.close_time_utc <= self.open_time_utc:
            raise DomainError(
                f"close_time_utc {self.close_time_utc.isoformat()} must follow "
                f"open_time_utc {self.open_time_utc.isoformat()}"
            )
        if not isinstance(self.trade_count, int) or isinstance(self.trade_count, bool):
            raise DomainError(f"trade_count must be an int, got {self.trade_count!r}")
        if self.trade_count < 0:
            raise DomainError(f"trade_count must not be negative; got {self.trade_count}")

        # The OHLC ordering check is the cheapest data-quality gate in the system and
        # it fires on real archive corruption: a mis-keyed epoch unit puts a 2026 bar
        # next to a 1970 one, and the merge that follows produces a high below its own
        # open. See .claude/rules/time-and-timezones.md.
        extremes = (self.open_quote_price, self.close_quote_price)
        if self.high_quote_price < max(extremes) or self.low_quote_price > min(extremes):
            raise DomainError(
                f"{self.instrument.symbol} bar at {self.open_time_utc.isoformat()} has "
                f"high {self.high_quote_price} / low {self.low_quote_price} that do not "
                f"bracket open {self.open_quote_price} and close {self.close_quote_price}"
            )

    @property
    def is_empty(self) -> bool:
        """Whether the interval saw no trades at all.

        A zero-volume bar is a real observation on an illiquid testnet symbol, not a
        gap. Discarding it silently shortens the series and moves every rolling window
        computed from it.
        """
        return self.trade_count == 0


@dataclass(frozen=True, slots=True)
class Tick:
    """One trade printed by the venue.

    `venue_trade_id` rather than a locally minted id: it is the only identifier both
    sides of a reconciliation agree on, and gap detection across a REST backfill seam
    joins on it (`.claude/rules/exchange-integration.md`).
    """

    instrument: Instrument
    venue_trade_id: str
    event_time_utc: datetime
    quote_price: Decimal
    base_quantity: Decimal
    aggressor_side: Side

    def __post_init__(self) -> None:
        require_text(self.venue_trade_id, "venue_trade_id")
        require_utc(self.event_time_utc, "event_time_utc")
        require_positive_decimal(self.quote_price, "quote_price")
        require_positive_decimal(self.base_quantity, "base_quantity")

    @property
    def notional_quote(self) -> Decimal:
        """Traded value in the quote asset."""
        return self.quote_price * self.base_quantity
