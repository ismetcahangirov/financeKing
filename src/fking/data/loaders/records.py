"""The canonical records a parser produces: one bar, one trade.

**These are deliberately not `fking.domain.Bar` and `fking.domain.Tick`.** A `Bar` holds
an `Instrument`, which carries `tick_size`, `lot_step` and `min_notional_quote` -- venue
filters that no archive file contains. A loader that constructed one would have to invent
them, and an invented `lot_step` is worse than an absent one: a backtest would then fill
quantities the venue would have refused, which is the failure `Instrument`'s docstring
exists to prevent. The canonical bar schema in `DATA_PIPELINE.md` section 6 also carries
`quote_volume` and the taker-buy columns, which a `Bar` correctly does not -- a strategy
reading a bar has no business seeing taker-buy flow unless it asked for that feature.

Turning a `KlineRecord` into a `Bar` is the job of whatever holds the instrument
definitions, which is not the parser.

These types carry no validation. Every invariant an archive row can violate is checked in
the parser, where a violation becomes a *counted rejection* rather than an exception --
one malformed row out of 3.5 million must not stop a backfill, and a record type that
raised in its constructor would give the parser no way to say "reject this one and keep
the tally". Validation lives at the boundary; a record is what having passed it looks
like.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal

__all__ = ["ArchiveRecord", "KlineRecord", "TradeRecord"]


@dataclass(frozen=True, slots=True)
class KlineRecord:
    """One closed OHLCV interval as the archive filed it.

    The interval is half-open `[open_time_utc, close_time_utc)` by convention, even
    though Binance files `close_time` as the last representable instant *inside* the
    interval -- 59.999999 seconds past the minute on a microsecond-epoch spot file. The
    raw value is kept rather than rounded up, because a normalisation applied here is a
    normalisation a reconciliation against the archive would have to undo.

    `ignored_field` is Binance's trailing always-zero column. It is retained rather than
    dropped so that the field count is the file's field count: a parser that discarded it
    would accept an 11-column row, and an 11-column row means a column was removed
    upstream, which is exactly the event worth failing on.
    """

    open_time_utc: datetime
    close_time_utc: datetime
    open_quote_price: Decimal
    high_quote_price: Decimal
    low_quote_price: Decimal
    close_quote_price: Decimal
    base_volume: Decimal
    quote_volume: Decimal
    trade_count: int
    taker_buy_base_volume: Decimal
    taker_buy_quote_volume: Decimal
    ignored_field: str

    @property
    def event_time_utc(self) -> datetime:
        """The instant this bar is keyed on: its open.

        The open rather than the close, because a point-in-time query is keyed on the left
        edge of a half-open `[open, close)` interval, and because a bar keyed on its close
        is a bar that appears to have existed before it did
        (`docs/rules/no-lookahead.md`). Named to match `TradeRecord.event_time_utc` so
        that code handling either shape needs no `isinstance` branch to ask when.
        """
        return self.open_time_utc


@dataclass(frozen=True, slots=True)
class TradeRecord:
    """One trade print as the archive filed it.

    `venue_trade_id` stays a string. It is an identifier, not a quantity: nothing adds
    two of them, and it is the join key a REST-backfill seam reconciles on
    (`docs/rules/exchange-integration.md`), so the value that must match is the value
    the venue sent rather than whatever an int round trip produces.

    `is_buyer_maker` is the field trap 3 corrupts. It is the aggressor side inverted --
    a maker buyer means the *taker* sold -- and it is the only field in this record whose
    wrongness leaves every other column looking correct.
    """

    venue_trade_id: str
    event_time_utc: datetime
    quote_price: Decimal
    base_quantity: Decimal
    quote_quantity: Decimal
    is_buyer_maker: bool
    is_best_match: bool


ArchiveRecord = KlineRecord | TradeRecord
"""Every record shape a declared dataset can produce."""
