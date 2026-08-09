"""One normalized batch in, one Parquet file out. Sorted, typed, and idempotent.

Three properties, each closing a failure that is silent without it.

**Sorted by event time before writing.** Predicate pushdown works on row-group
statistics. An unsorted file has min/max ranges that overlap on every row group, so a
filtered scan eliminates no row group and reads the whole file -- and the symptom is a
slow query that looks like a DuckDB problem, not a writer problem. The sort is stable, so
two prints sharing a microsecond keep the order the venue filed them in, which is the
order a trade-id reconciliation expects.

**Typed from the declared schema, never inferred.** pyarrow infers a `Decimal` column's
precision and scale from the values it is handed, so a month whose prices all have two
decimal places produces `decimal128(8, 2)` and the next month produces something else --
two files, one glob, and a scan that fails on a type mismatch nobody introduced.

**Idempotent by content digest, not by clock.** A backfill re-run is the normal case: the
same archive bytes, a different day. Deterministic serialisation alone does not survive
that, because `ingested_at_utc` genuinely differs. So the digest covers the records and
their `source` and deliberately *not* `ingested_at_utc` -- when we happened to read a file
is not part of what the file said -- and a matching digest declines the rewrite. `source`
is inside the digest because a stream-written month later re-fetched from the archive is
the one rewrite that must happen.

**The digest normalises a decimal's scale; the file does not.** `str(Decimal("1.50"))` and
`str(Decimal("1.5"))` differ, and hashing them as different facts is what
`docs/rules/idempotency.md` already refuses for an idempotency key, for the reason that
applies exactly here: they are one economic quantity spelled two ways. The distinction is
not academic once a partition is read back and merged into -- a `decimal128(38, 18)` column
returns every value at eighteen places, so a record that goes in as `1.50` comes out as
`1.500000000000000000`, and a scale-sensitive digest would report a rewrite on every repair
pass over a range that had already been repaired. What lands in the column is unchanged: it
is the venue's value at the schema's scale either way.

**Provenance is per row, not per batch.** `write_records` takes one `source` because the
archive path writes one; `write_sourced_records` takes one per record because a partition
that has been repaired holds prints the socket delivered *and* prints REST returned
afterwards, and those are the two things `source` exists to tell apart
(`DATA_PIPELINE.md` section 6, gate 11). Collapsing them to whichever arrived last would
answer "which rows came from a live stream" with a guess.

The clock is a parameter. A writer that called `datetime.now(UTC)` would make "writing
the same batch twice" untestable, and every `ingested_at_utc` in the corpus would be
unreproducible from the audit trail.
"""

from __future__ import annotations

import hashlib
import os
import tempfile
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path
from typing import Final, Literal

import pyarrow as pa
import pyarrow.parquet as pq

from fking.data.archive import ArchiveCoordinate
from fking.data.format_resolver import Dataset
from fking.data.loaders import ArchiveRecord, KlineRecord, TradeRecord
from fking.data.parquet.layout import (
    DATASET_PARTITION_GRAIN,
    PartitionGrain,
    partition_path,
)
from fking.data.parquet.schema import CONTENT_DIGEST_KEY, RecordSource, schema_for
from fking.platform.errors import DataIntegrityError

__all__ = [
    "SourcedRecord",
    "WriteOutcome",
    "partition_row_count",
    "write_records",
    "write_sourced_records",
]

# ZSTD level 3 (DATA_PIPELINE.md section 6). Level 3 rather than the maximum because the
# corpus is scanned far more often than it is written: past 3 the decompression cost
# stops falling while the write cost keeps climbing, so a higher level buys disk at the
# expense of every query that ever reads the file.
_COMPRESSION: Final[Literal["zstd"]] = "zstd"
_COMPRESSION_LEVEL: Final[int] = 3

# Parquet format 2.6 is what gives `timestamp[us]` a native logical type. Under 1.0 a
# microsecond timestamp is coerced to milliseconds -- silently, and it is exactly the
# three digits VF-015 is about.
_PARQUET_VERSION: Final[Literal["2.6"]] = "2.6"

# ~122k rows: one row group covers roughly three months of one-minute bars or two minutes
# of dense trade prints. Small enough that a filtered scan can eliminate most of a file
# on its statistics, large enough that the statistics footer does not dominate.
_ROW_GROUP_ROWS: Final[int] = 122_880

_DATASET_RECORD_TYPES: Final[dict[Dataset, type[KlineRecord] | type[TradeRecord]]] = {
    Dataset.KLINES: KlineRecord,
    Dataset.TRADES: TradeRecord,
    Dataset.AGG_TRADES: TradeRecord,
}


@dataclass(frozen=True, slots=True)
class WriteOutcome:
    """What one write did, including when it did nothing.

    `was_rewritten` is the interesting field. A backfill that reports only "wrote 43,200
    rows" cannot tell a caller whether it re-derived a month or recognised one it already
    held, and those are different answers to "is this corpus complete".
    """

    path: Path
    rows_written: int
    content_digest_hex: str
    bytes_written: int
    was_rewritten: bool


@dataclass(frozen=True, slots=True)
class SourcedRecord[RecordT: ArchiveRecord]:
    """One record and where it came from.

    A pair rather than a field on the record, because provenance is a fact about how a
    row reached the corpus and not about the bar or the print itself: `KlineRecord` and
    `TradeRecord` are what the venue said, and a `source` inside them would be the first
    field of a canonical record that no source could ever fill in.

    Generic over the record so a reader that returns prints returns
    `SourcedRecord[TradeRecord]` and its caller can reach `venue_trade_id` without a cast.
    The parameter is inferred covariant -- the field is read-only on a frozen dataclass --
    so a sequence of prints is still a sequence this module can write.
    """

    record: RecordT
    source: RecordSource


def write_records(
    records: Sequence[ArchiveRecord],
    *,
    coordinate: ArchiveCoordinate,
    source: RecordSource,
    ingested_at_utc: datetime,
    root: Path,
) -> WriteOutcome:
    """Write `records`, all from one source, to the one Parquet file `coordinate` names.

    The uniform case, which is every archive read and every live tape seal. A partition
    that mixes provenance goes through `write_sourced_records`.

    Args:
        records: Accepted records from one parse. Order is irrelevant -- they are sorted
            here -- but every one must fall inside `coordinate`'s partition.
        coordinate: Which market, dataset, symbol, interval and date this batch is.
        source: `archive`, `stream` or `rest_backfill`. Part of the content digest.
        ingested_at_utc: Wall time of the write, injected. Aware UTC.
        root: The corpus root, typically `data/parquet`.

    Raises:
        DataIntegrityError: the batch is empty, a record's type does not match the
            dataset, `ingested_at_utc` is not aware UTC, or a record falls outside the
            partition `coordinate` names.
    """
    return write_sourced_records(
        tuple(SourcedRecord(record, source) for record in records),
        coordinate=coordinate,
        ingested_at_utc=ingested_at_utc,
        root=root,
    )


def write_sourced_records(
    sourced: Sequence[SourcedRecord[ArchiveRecord]],
    *,
    coordinate: ArchiveCoordinate,
    ingested_at_utc: datetime,
    root: Path,
) -> WriteOutcome:
    """Write records carrying their own provenance to the file `coordinate` names.

    The order `sourced` arrives in is preserved within one instant, because the sort is
    stable and event time is not a total order over a trade tape. A caller that has
    already reconciled a merge -- `fking.data.backfill.seam.reconcile_trades` orders by
    `(instant, aggregate id)` -- gets that order on disk, which is what makes the content
    digest a function of the content rather than of the merge.

    Raises:
        DataIntegrityError: the batch is empty, a record's type does not match the
            dataset, `ingested_at_utc` is not aware UTC, or a record falls outside the
            partition `coordinate` names.
    """
    _require_aware_utc(ingested_at_utc)
    if not sourced:
        raise DataIntegrityError(
            f"refusing to write an empty batch for {coordinate.dataset.value} "
            f"{coordinate.symbol} {coordinate.archive_date.isoformat()}. Once on disk a "
            f"zero-row file is indistinguishable from a gap, and a later scan reads it as "
            f"'we have this period'. A period with no prints is a real observation and it "
            f"is recorded by the coverage registry, not by an empty file"
        )

    schema = schema_for(coordinate.dataset)
    records = tuple(entry.record for entry in sourced)
    _require_matching_record_type(records, coordinate)
    ordered = sorted(sourced, key=lambda entry: _event_time_of(entry.record))
    _require_inside_partition(tuple(entry.record for entry in ordered), coordinate)

    digest_hex = _content_digest(ordered)
    path = partition_path(coordinate, root=root)
    if _already_holds(path, digest_hex):
        return WriteOutcome(
            path=path,
            rows_written=len(ordered),
            content_digest_hex=digest_hex,
            bytes_written=path.stat().st_size,
            was_rewritten=False,
        )

    table = _to_table(
        ordered, schema=schema, ingested_at_utc=ingested_at_utc, digest_hex=digest_hex
    )
    _write_atomically(table, path)
    return WriteOutcome(
        path=path,
        rows_written=len(ordered),
        content_digest_hex=digest_hex,
        bytes_written=path.stat().st_size,
        was_rewritten=True,
    )


# ---------------------------------------------------------------------------
# Checks
# ---------------------------------------------------------------------------


def _require_aware_utc(moment: datetime) -> None:
    if moment.tzinfo is None or moment.utcoffset() != UTC.utcoffset(None):
        raise DataIntegrityError(
            f"ingested_at_utc must be timezone-aware UTC; got {moment!r}. A naive value "
            f"is written as though it were UTC, so the provenance column that exists to "
            f"answer 'when did this row land' answers it wrong by the writer's offset"
        )


def _require_matching_record_type(
    records: Sequence[ArchiveRecord], coordinate: ArchiveCoordinate
) -> None:
    """Every record is the shape the dataset's schema describes.

    Checked over the whole batch rather than the first element: a parse that emitted one
    stray record type would otherwise produce a table pyarrow fills with nulls for the
    columns that record lacks, and a nullable=False schema turns that into an error
    naming a column rather than the cause.
    """
    expected = _DATASET_RECORD_TYPES[coordinate.dataset]
    offenders = {type(record).__name__ for record in records if not isinstance(record, expected)}
    if offenders:
        raise DataIntegrityError(
            f"dataset {coordinate.dataset.value!r} is written from {expected.__name__}, but "
            f"the batch contains {sorted(offenders)}. A mismatched record type produces a "
            f"table of nulls rather than an error, because the columns it lacks are simply "
            f"absent from the values pyarrow is handed"
        )


def _require_inside_partition(
    records: Sequence[ArchiveRecord], coordinate: ArchiveCoordinate
) -> None:
    """No record may fall outside the period its file claims to cover.

    Nothing downstream would notice otherwise: the file writes, the rows parse, and a
    query filtered on `month = '01'` silently returns February data because the partition
    key comes from the *path*, not from the row.
    """
    grain = DATASET_PARTITION_GRAIN[coordinate.dataset]
    target = coordinate.archive_date
    for record in records:
        moment = _event_time_of(record).date()
        inside = (
            moment == target
            if grain is PartitionGrain.DAILY
            else (moment.year, moment.month) == (target.year, target.month)
        )
        if not inside:
            raise DataIntegrityError(
                f"record at {_event_time_of(record).isoformat()} falls outside the partition "
                f"{coordinate.market.value}/{coordinate.dataset.value} "
                f"{coordinate.symbol} {target.isoformat()} ({grain.value}). The partition "
                f"key is read from the path, not from the row, so a stray record is filed "
                f"under a period it does not belong to and every filtered query agrees"
            )


def partition_row_count(path: Path) -> int | None:
    """How many rows the partition at `path` already holds, or `None` if there is none.

    Read from the Parquet footer, so it costs one small seek rather than a scan -- which
    is what makes it affordable as a pre-write guard for a caller that is about to replace
    the file. `None` for an absent or unreadable file, matching `_already_holds`: a
    truncated file from an interrupted write must stay recoverable by re-running the
    write that produced it, and a guard that raised on one would make that impossible.

    The caller decides what a row count means. `write_records` deliberately does not: an
    archive re-read that legitimately produces fewer rows -- a corrected upstream file --
    is a rewrite that must happen, while a live tape seal writing fewer prints than the
    partition holds is a partition about to lose data (`fking.data.live.tape`).
    """
    if not path.is_file():
        return None
    try:
        return int(pq.read_metadata(path).num_rows)
    except pa.ArrowInvalid:
        return None


def _already_holds(path: Path, digest_hex: str) -> bool:
    """Whether `path` is a Parquet file whose stored digest already matches.

    A file whose metadata cannot be read is treated as not matching and is rewritten. The
    alternative -- raising -- would make a truncated file from an interrupted write
    permanently unrecoverable by re-running the backfill, which is the one action an
    operator will reach for.
    """
    if not path.is_file():
        return False
    try:
        metadata = pq.read_schema(path).metadata
    except pa.ArrowInvalid:
        return False
    stored = metadata.get(CONTENT_DIGEST_KEY) if metadata else None
    return stored is not None and stored.decode() == digest_hex


# ---------------------------------------------------------------------------
# Serialisation
# ---------------------------------------------------------------------------


def _event_time_of(record: ArchiveRecord) -> datetime:
    return record.event_time_utc


def _canonical_decimal(quantity: Decimal) -> str:
    """A decimal as one string per quantity, whatever scale it arrived at.

    `normalize()` strips trailing zeros, so `Decimal("1.50")` and `Decimal("1.5")` hash
    alike -- the same normalisation, for the same reason, as the idempotency key in
    `docs/rules/idempotency.md`. Formatted with `f` rather than `str` because
    `normalize()` produces scientific notation above the context's precision
    (`Decimal("1E+3")`), and two spellings of one thousand would put the problem back.

    The file is unaffected: the column is `decimal128(38, 18)` and stores the venue's
    value at the schema's scale either way. What changes is that a partition read back out
    of Parquet -- where every value returns at eighteen places -- digests to the same
    string as the batch that was written, so a repair pass over an already-repaired range
    is a genuine no-op rather than a rewrite of identical data.
    """
    return format(quantity.normalize(), "f")


def _content_digest(sourced: Sequence[SourcedRecord[ArchiveRecord]]) -> str:
    """SHA-256 over the sorted records and their provenance, excluding the write clock.

    Fields are joined with a separator that cannot occur in any of them, so that two
    different field splits cannot produce the same byte string -- `"1|23"` and `"12|3"`
    hash differently, which they would not under bare concatenation. Each record's
    `source` is hashed with the record rather than once for the batch, because a repaired
    partition holds rows from two of them and a batch-level digest could not tell a
    stream-only file from a repaired one with the same rows.
    """
    hasher = hashlib.sha256()
    for entry in sourced:
        hasher.update(b"\x1e")  # record separator
        for value in (*_digest_fields(entry.record), entry.source.value):
            hasher.update(b"\x1f")  # unit separator
            hasher.update(value.encode("utf-8"))
    return hasher.hexdigest()


def _digest_fields(record: ArchiveRecord) -> tuple[str, ...]:
    if isinstance(record, KlineRecord):
        return (
            record.open_time_utc.isoformat(),
            record.close_time_utc.isoformat(),
            _canonical_decimal(record.open_quote_price),
            _canonical_decimal(record.high_quote_price),
            _canonical_decimal(record.low_quote_price),
            _canonical_decimal(record.close_quote_price),
            _canonical_decimal(record.base_volume),
            _canonical_decimal(record.quote_volume),
            str(record.trade_count),
            _canonical_decimal(record.taker_buy_base_volume),
            _canonical_decimal(record.taker_buy_quote_volume),
        )
    return (
        record.venue_trade_id,
        record.event_time_utc.isoformat(),
        _canonical_decimal(record.quote_price),
        _canonical_decimal(record.base_quantity),
        _canonical_decimal(record.quote_quantity),
        str(record.is_buyer_maker),
        str(record.is_best_match),
    )


def _to_table(
    sourced: Sequence[SourcedRecord[ArchiveRecord]],
    *,
    schema: pa.Schema,
    ingested_at_utc: datetime,
    digest_hex: str,
) -> pa.Table:
    columns: dict[str, list[object]] = {name: [] for name in schema.names}
    for entry in sourced:
        for name, value in _row_of(entry.record).items():
            columns[name].append(value)
        columns["source"].append(entry.source.value)
        columns["ingested_at_utc"].append(ingested_at_utc)
    stamped = schema.with_metadata({CONTENT_DIGEST_KEY: digest_hex.encode("utf-8")})
    return pa.table(columns, schema=stamped)


def _row_of(record: ArchiveRecord) -> dict[str, datetime | Decimal | int | str | bool]:
    """The record's own columns, keyed exactly as the schema names them.

    `ignored_field` is absent: the parser keeps Binance's trailing always-zero column so
    that the field count it checks is the file's field count, and it has no reader here.
    """
    if isinstance(record, KlineRecord):
        return {
            "open_time_utc": record.open_time_utc,
            "close_time_utc": record.close_time_utc,
            "open_quote_price": record.open_quote_price,
            "high_quote_price": record.high_quote_price,
            "low_quote_price": record.low_quote_price,
            "close_quote_price": record.close_quote_price,
            "base_volume": record.base_volume,
            "quote_volume": record.quote_volume,
            "trade_count": record.trade_count,
            "taker_buy_base_volume": record.taker_buy_base_volume,
            "taker_buy_quote_volume": record.taker_buy_quote_volume,
        }
    return {
        "venue_trade_id": record.venue_trade_id,
        "event_time_utc": record.event_time_utc,
        "quote_price": record.quote_price,
        "base_quantity": record.base_quantity,
        "quote_quantity": record.quote_quantity,
        "is_buyer_maker": record.is_buyer_maker,
        "is_best_match": record.is_best_match,
    }


def _write_atomically(table: pa.Table, path: Path) -> None:
    """Write to a sibling temporary file, then replace.

    A scan concurrent with a backfill must never observe a half-written Parquet file. It
    would not raise -- a truncated file has no footer, so DuckDB reports it as an invalid
    file and the query fails partway through a multi-hour run, which is the expensive
    version of this problem. `os.replace` is atomic within a filesystem, and the temporary
    file is created in the destination directory so that it is the same filesystem.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    handle, staged_name = tempfile.mkstemp(
        dir=path.parent, prefix=f".{path.name}.", suffix=".staging"
    )
    os.close(handle)
    staged = Path(staged_name)
    try:
        pq.write_table(
            table,
            staged,
            compression=_COMPRESSION,
            compression_level=_COMPRESSION_LEVEL,
            version=_PARQUET_VERSION,
            row_group_size=_ROW_GROUP_ROWS,
            # Statistics are the whole point of sorting the rows: without them a row
            # group carries no min/max and predicate pushdown has nothing to eliminate on.
            write_statistics=True,
            # Off deliberately. The page index is a second statistics structure that
            # pyarrow orders by a hash-seeded traversal in some builds, which makes the
            # file bytes non-deterministic and breaks the idempotency digest for no
            # benefit at these row-group sizes.
            write_page_index=False,
        )
        os.replace(staged, path)
    finally:
        staged.unlink(missing_ok=True)
