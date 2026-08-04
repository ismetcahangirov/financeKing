"""The operational `bar` table, as the two writers that touch it see it.

Extracted from `fking.data.live.writer` when the REST gap backfill became the second
writer (#28). One `INSERT` statement rather than two, because the two would have to agree
about `ON CONFLICT` behaviour and about the column order forever, and the way they stop
agreeing is that somebody adds a column to one of them.

The writers differ in exactly one binding -- `source` -- and that difference is the
reason both exist: a bar the socket delivered on time is `stream`, a bar recovered from
`/api/v3/klines` hours later is `rest_backfill`, and folding them together would erase
the distinction in the one column that answers how a row reached the corpus.

**`ON CONFLICT DO NOTHING`, never `DO UPDATE`.** A closed bar is immutable, so a second
arrival for the same `(instrument_id, timeframe, open_time_utc)` is a duplicate delivery
rather than a correction -- and a `DO UPDATE` would let a later frame silently rewrite a
bar a backtest has already consumed. Where two sources genuinely disagree about a closed
bar, that is `fking.data.backfill.seam`'s escalation, not something a write path resolves
by preference.

`read_bars` reconstructs `KlineRecord`s with `ignored_field=""`. The column does not exist
in the table and inventing the archive's `"0"` filler would make a row read back as though
it had come from a CSV whose trailing column we had counted. `""` is what the live parser
writes for a record that never had one, and the field is excluded from the seam comparison
for exactly this reason.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from datetime import datetime
from typing import Final
from uuid import UUID

import sqlalchemy as sa
from sqlalchemy.ext.asyncio import AsyncConnection, AsyncEngine

from fking.data.loaders.records import KlineRecord
from fking.data.parquet.schema import RecordSource
from fking.platform.errors import DataUnavailableError

__all__ = [
    "INSERT_BAR",
    "bar_parameters",
    "read_bars",
    "resolve_instrument_ids",
]

# The stream has no equivalent of the archive's trailing always-zero column, and the
# table stores neither. Spelled here as well as in `fking.data.live.frames` because this
# is the read side and the two must agree for a seam comparison to mean anything.
_NO_IGNORED_FIELD: Final[str] = ""

INSERT_BAR: Final[sa.TextClause] = sa.text(
    """
    INSERT INTO bar (
        instrument_id, timeframe, open_time_utc, close_time_utc,
        open_quote_price, high_quote_price, low_quote_price, close_quote_price,
        base_volume, quote_volume, taker_buy_base_volume, taker_buy_quote_volume,
        trade_count, source
    )
    VALUES (
        :instrument_id, :timeframe, :open_time_utc, :close_time_utc,
        :open_quote_price, :high_quote_price, :low_quote_price, :close_quote_price,
        :base_volume, :quote_volume, :taker_buy_base_volume, :taker_buy_quote_volume,
        :trade_count, :source
    )
    ON CONFLICT (instrument_id, timeframe, open_time_utc) DO NOTHING
    RETURNING 1
    """
)

_SELECT_INSTRUMENT: Final[sa.TextClause] = sa.text(
    """
    SELECT instrument_id
      FROM instrument
     WHERE venue_id = :venue_id AND symbol = :symbol
    """
)

_SELECT_BARS: Final[sa.TextClause] = sa.text(
    """
    SELECT open_time_utc, close_time_utc,
           open_quote_price, high_quote_price, low_quote_price, close_quote_price,
           base_volume, quote_volume, taker_buy_base_volume, taker_buy_quote_volume,
           trade_count
      FROM bar
     WHERE instrument_id = :instrument_id
       AND timeframe = :timeframe
       AND open_time_utc >= :from_utc
       AND open_time_utc <  :until_utc
     ORDER BY open_time_utc
    """
)


def bar_parameters(
    record: KlineRecord,
    *,
    instrument_id: UUID,
    timeframe: str,
    source: RecordSource,
) -> dict[str, object]:
    """Bind one record for `INSERT_BAR`."""
    return {
        "instrument_id": instrument_id,
        "timeframe": timeframe,
        "open_time_utc": record.open_time_utc,
        "close_time_utc": record.close_time_utc,
        "open_quote_price": record.open_quote_price,
        "high_quote_price": record.high_quote_price,
        "low_quote_price": record.low_quote_price,
        "close_quote_price": record.close_quote_price,
        "base_volume": record.base_volume,
        "quote_volume": record.quote_volume,
        "taker_buy_base_volume": record.taker_buy_base_volume,
        "taker_buy_quote_volume": record.taker_buy_quote_volume,
        "trade_count": record.trade_count,
        "source": source.value,
    }


async def resolve_instrument_ids(
    engine: AsyncEngine, *, venue_id: str, symbols: Sequence[str]
) -> Mapping[str, UUID]:
    """Look up every symbol's instrument id, or refuse the whole set.

    All-or-nothing rather than best-effort. A partial mapping is a session or a backfill
    that looks healthy while one symbol's data goes nowhere, and the refusal names both
    what is missing and what this venue does hold so the answer is "seed the instrument"
    rather than "find the bug".

    Raises:
        DataUnavailableError: at least one symbol has no `instrument` row on `venue_id`.
    """
    resolved: dict[str, UUID] = {}
    missing: list[str] = []
    async with engine.connect() as connection:
        for symbol in symbols:
            row = (
                await connection.execute(
                    _SELECT_INSTRUMENT, {"venue_id": venue_id, "symbol": symbol}
                )
            ).first()
            if row is None:
                missing.append(symbol)
            else:
                resolved[symbol] = UUID(str(row.instrument_id))
    if missing:
        known = sorted(resolved)
        raise DataUnavailableError(
            f"{sorted(missing)} have no instrument row on {venue_id}; this venue holds "
            f"{known or '(none)'}. Seed the instrument before subscribing to it -- a bar "
            f"with no instrument id cannot be written, and dropping it would be a session "
            f"that looks healthy while one symbol's data goes nowhere"
        )
    return resolved


async def read_bars(
    connection: AsyncConnection,
    *,
    instrument_id: UUID,
    timeframe: str,
    from_utc: datetime,
    until_utc: datetime,
) -> tuple[KlineRecord, ...]:
    """Bars the corpus holds whose open time lies in `[from_utc, until_utc)`.

    Half-open on the same convention as every other window in this package, so that two
    adjacent reads neither repeat a bar nor skip one.
    """
    rows = (
        await connection.execute(
            _SELECT_BARS,
            {
                "instrument_id": instrument_id,
                "timeframe": timeframe,
                "from_utc": from_utc,
                "until_utc": until_utc,
            },
        )
    ).all()
    return tuple(
        KlineRecord(
            open_time_utc=row.open_time_utc,
            close_time_utc=row.close_time_utc,
            open_quote_price=row.open_quote_price,
            high_quote_price=row.high_quote_price,
            low_quote_price=row.low_quote_price,
            close_quote_price=row.close_quote_price,
            base_volume=row.base_volume,
            quote_volume=row.quote_volume,
            trade_count=int(row.trade_count),
            taker_buy_base_volume=row.taker_buy_base_volume,
            taker_buy_quote_volume=row.taker_buy_quote_volume,
            ignored_field=_NO_IGNORED_FIELD,
        )
        for row in rows
    )
