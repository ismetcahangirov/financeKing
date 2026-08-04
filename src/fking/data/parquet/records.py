"""A written partition, back as the records and the provenance it was written from.

The inverse of `writer.write_sourced_records`, and it exists for exactly one caller
shape: a repair that has to merge new rows into a partition. A partition is written
whole, so adding a print means reading the file, reconciling, and writing it again --
and the rows already there have to come back carrying their own `source`, or the rewrite
would relabel every streamed print as whatever the repair fetched.

**pyarrow rather than DuckDB, deliberately.** `reader.read_connection` exists to scan a
*glob* and prune partitions on their path, which is a different job with a different cost
model. Here the file is already known, there is nothing to prune, and what the caller
needs is `Decimal` and aware `datetime` objects rather than a result set -- so going
through SQL would add a type round trip to the one raw_field the corpus cannot afford to get
wrong.

**A raw_field comes back at the column's scale, not at the scale it was written with.**
`decimal128(38, 18)` stores `Decimal("1.50")` and returns `Decimal("1.500000000000000000")`.
Nothing here undoes that, because the information is gone -- the writer's content digest
normalises instead, which is what makes a read-back-merge-rewrite a genuine no-op rather
than a rewrite of identical data.

**Two reads, because the repair asks two different questions.** Deciding *what* to fetch
needs a handful of prints around a gap; performing the rewrite needs the whole day. A day
partition targets 128-512 MB, which is millions of prints -- materialising all of them as
Python dataclasses to decide whether four are missing would cost gigabytes for an answer a
predicate can give in milliseconds. So `read_partition_trade_window` pushes its filter into
the Parquet reader, and `read_partition_trades` -- which is unavoidably whole-file -- runs
only once something is actually going to be written.

Trades only. A kline reader would be the same twelve lines and has no caller, and an
unused branch here is a branch nobody has ever seen return the wrong column.
"""

from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path
from typing import Final

import pyarrow as pa
import pyarrow.compute as pc
import pyarrow.parquet as pq

from fking.data.loaders.records import TradeRecord
from fking.data.parquet.schema import TIMESTAMP_TYPE, RecordSource
from fking.data.parquet.writer import SourcedRecord
from fking.platform.errors import DataIntegrityError

__all__ = ["read_partition_trade_window", "read_partition_trades"]

# Exactly the columns a `TradeRecord` plus its provenance needs. Named rather than taken
# from the schema, so a column added to the schema does not silently become a field this
# reader tries to hand to a constructor that has no parameter for it.
_TRADE_COLUMNS: Final[tuple[str, ...]] = (
    "venue_trade_id",
    "event_time_utc",
    "quote_price",
    "base_quantity",
    "quote_quantity",
    "is_buyer_maker",
    "is_best_match",
    "source",
)


def read_partition_trades(path: Path) -> tuple[SourcedRecord[TradeRecord], ...]:
    """Every print in one trade partition, in the order the file holds them.

    File order rather than a re-sort: the file was written sorted by event time with the
    aggregate-id order preserved inside each instant, and re-deriving that here would mean
    this module having an opinion about tape ordering that the seam already owns.

    Returns an empty tuple for a path that does not exist. That is the ordinary answer for
    a series whose day has never been sealed, and it is the caller -- which knows whether
    a missing partition means "nothing yet" or "something is wrong" -- that decides what
    to do about it.

    Raises:
        DataIntegrityError: the file cannot be read as Parquet, lacks a column this
            reader needs, or carries a `source` outside the declared enum.
    """
    return _read(path, filters=None)


def read_partition_trade_window(
    path: Path, *, at_utc: datetime, venue_trade_ids: frozenset[str]
) -> tuple[SourcedRecord[TradeRecord], ...]:
    """The prints at one instant, plus the prints carrying any of `venue_trade_ids`.

    The two halves of what a tape repair needs to know before it fetches anything: which
    print the gap was measured from -- that one is identified by its instant, because its
    id is precisely what is being derived -- and which of the ids around the gap the corpus
    already holds. One read rather than two, expressed as a disjunction, so a partition is
    opened once per gap.

    An empty `venue_trade_ids` still returns the prints at `at_utc`; the disjunction just
    has one satisfiable arm.

    Raises:
        DataIntegrityError: as `read_partition_trades`.
    """
    # An `Expression` rather than the tuple filter syntax, because the tuple form expresses
    # only a conjunction and what is wanted here is a disjunction.
    #
    # Both operands are typed explicitly. pyarrow infers an empty Python list as a null
    # array and then refuses to compare it against a string column -- so the empty case,
    # which is the first of the repair's two reads, would raise rather than match nothing.
    return _read(
        path,
        filters=(pc.field("event_time_utc") == pa.scalar(at_utc, type=TIMESTAMP_TYPE))
        | pc.field("venue_trade_id").isin(pa.array(sorted(venue_trade_ids), type=pa.string())),
    )


def _read(path: Path, *, filters: pc.Expression | None) -> tuple[SourcedRecord[TradeRecord], ...]:
    if not path.is_file():
        return ()
    try:
        table = pq.read_table(path, columns=list(_TRADE_COLUMNS), filters=filters)
    except (pa.ArrowInvalid, KeyError) as unreadable:
        raise DataIntegrityError(
            f"{path} is not a readable trade partition: {unreadable}. A truncated file "
            f"from an interrupted write reads this way, and so does a partition written "
            f"under a different schema -- both are worth stopping on rather than merging "
            f"into"
        ) from unreadable

    columns = {name: table.column(name).to_pylist() for name in _TRADE_COLUMNS}
    return tuple(
        SourcedRecord(
            record=TradeRecord(
                venue_trade_id=_string(columns["venue_trade_id"][row], "venue_trade_id", path, row),
                event_time_utc=_moment(columns["event_time_utc"][row], path, row),
                quote_price=_decimal(columns["quote_price"][row], "quote_price", path, row),
                base_quantity=_decimal(columns["base_quantity"][row], "base_quantity", path, row),
                quote_quantity=_decimal(
                    columns["quote_quantity"][row], "quote_quantity", path, row
                ),
                is_buyer_maker=_boolean(
                    columns["is_buyer_maker"][row], "is_buyer_maker", path, row
                ),
                is_best_match=_boolean(columns["is_best_match"][row], "is_best_match", path, row),
            ),
            source=_source(columns["source"][row], path, row),
        )
        for row in range(table.num_rows)
    )


def _string(raw_field: object, name: str, path: Path, row: int) -> str:
    if not isinstance(raw_field, str):
        raise DataIntegrityError(
            f"{path} row {row} column {name!r} is a {type(raw_field).__name__}, not a string"
        )
    return raw_field


def _decimal(raw_field: object, name: str, path: Path, row: int) -> Decimal:
    if not isinstance(raw_field, Decimal):
        raise DataIntegrityError(
            f"{path} row {row} column {name!r} is a {type(raw_field).__name__}, not a Decimal. "
            f"A float here means the column was written as `double`, which is the failure "
            f"the declared schema exists to prevent"
        )
    return raw_field


def _boolean(raw_field: object, name: str, path: Path, row: int) -> bool:
    if not isinstance(raw_field, bool):
        raise DataIntegrityError(
            f"{path} row {row} column {name!r} is a {type(raw_field).__name__}, not a boolean"
        )
    return raw_field


def _moment(raw_field: object, path: Path, row: int) -> datetime:
    if not isinstance(raw_field, datetime):
        raise DataIntegrityError(
            f"{path} row {row} column 'event_time_utc' is a "
            f"{type(raw_field).__name__}, not a datetime"
        )
    if raw_field.tzinfo is None or raw_field.utcoffset() != UTC.utcoffset(None):
        raise DataIntegrityError(
            f"{path} row {row} carries event time {raw_field!r}, which is not aware UTC. The "
            f"column is `timestamp[us, tz=UTC]`, so a naive raw_field means the file was "
            f"written by something other than this writer"
        )
    return raw_field


def _source(raw_field: object, path: Path, row: int) -> RecordSource:
    """The row's provenance, refused rather than defaulted when it is not a declared one.

    Defaulting would relabel an unrecognised source as one of ours on the next rewrite,
    which is the one thing gate 11 -- the standing proof that no synthesised rows exist --
    reads this column to rule out.
    """
    if not isinstance(raw_field, str):
        raise DataIntegrityError(
            f"{path} row {row} column 'source' is a {type(raw_field).__name__}, not a string"
        )
    try:
        return RecordSource(raw_field)
    except ValueError as unrecognised:
        raise DataIntegrityError(
            f"{path} row {row} carries source {raw_field!r}, which is not one of "
            f"{sorted(member.value for member in RecordSource)}"
        ) from unrecognised
