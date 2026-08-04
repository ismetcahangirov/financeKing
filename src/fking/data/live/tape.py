"""Where a live `aggTrade` print lands: a spool on disk, then one whole daily partition.

Until this module existed the tape was parsed for the sequence detector and dropped
(`fking.data.live.router`). That was not an oversight -- `DATA_PIPELINE.md` section 6 puts
trades in Parquet, written **whole partitions at a time**, and there is no operational
trade table to hold a print in the meantime. A print therefore has to be accumulated for a
whole UTC day before it can become a file, and that is the entire design problem.

**The day is accumulated on disk, never in memory.** A day of BTCUSDT prints runs to
millions of rows; a day of them for a forty-symbol universe does not fit in the process,
and "buffer the day per symbol" is the obvious design that stops working at exactly the
scale this system is for. So `append` writes each print to a line-buffered NDJSON spool
file keyed by `(series, UTC day)` and keeps nothing. **The memory bound is one open file
handle per subscribed symbol** -- tens of kilobytes for the whole universe -- and it does
not grow with the day, the print rate, or how long the session has been up.

Line buffering rather than the default 8 KB block, and that is a deliberate trade. A
process crash cannot lose an appended print, because every line has already been handed to
the operating system. A *machine* crash can lose the tail of a spool, because nothing here
calls `fsync`: a print is not a fill, and an fsync per print would cap live ingestion at
the disk's IOPS to protect a row that the venue will still be serving from REST tomorrow.
The `bar` and audit paths, which do hold facts nobody re-serves, are in PostgreSQL and get
its durability rather than this one.

**The spool is the day's single accumulator, and it outlives the process.** A session
restarted at noon appends to the same file, and a spool left behind by a session that died
is sealed by the next one -- which is why `seal_elapsed_days` reads the spool directory
rather than any in-memory state. Nothing re-reads a written partition to merge into it:
`decimal128(38, 18)` re-scales `Decimal("1.5")` to eighteen places on the round trip, so
records read back out of Parquet hash differently from the ones that were written, and a
merge through that path would rewrite a partition and its content digest every pass.

**A rewrite may not shrink a partition.** A partition is written whole, so a seal that
found only part of a day -- a stray late print spooled after the day was already sealed,
most plausibly -- would replace a complete file with an incomplete one and nothing
downstream would notice. The row count in the existing file's Parquet footer is compared
against what is about to be written, and a shrink is refused rather than resolved. That is
the same refusal `fking.data.backfill.runner` makes about an archive partition, for the
same reason.

**Sealing waits out a grace window past midnight.** The day a print belongs to comes from
its own `event_time_utc`, and a print stamped 23:59:59.999 can arrive after the clock has
passed midnight. Sealing the instant the date changes would race that print into a spool
for a day that has just become a file.

Deduplication at seal is `fking.data.backfill.seam.reconcile_trades` with an empty held
side, which is not a trick: a print delivered twice across a reconnect is exactly the
same-id-different-arrival case the trade seam exists to collapse, and routing it through
the seam means a *contradiction* -- one id, two prices -- escalates here as loudly as it
would in a REST repair.
"""

from __future__ import annotations

import json
from collections.abc import Iterator, Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, date, datetime, time, timedelta
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import IO, Final

from fking.data.archive import ArchiveCoordinate
from fking.data.backfill.registry import SeriesKey
from fking.data.backfill.seam import reconcile_trades
from fking.data.format_resolver import Dataset, Market
from fking.data.live.router import LiveTrade
from fking.data.loaders.records import TradeRecord
from fking.data.parquet.layout import partition_path
from fking.data.parquet.schema import RecordSource
from fking.data.parquet.writer import partition_row_count, write_records
from fking.platform.errors import DataIntegrityError
from fking.platform.logging import get_logger

__all__ = [
    "SEAL_GRACE",
    "SPOOL_SUFFIX",
    "SealedPartition",
    "TapeCorpusWriter",
    "spool_path",
]

_LOG: Final = get_logger(__name__)

# Fifteen minutes past midnight before a day may be sealed. Sized from the failure it
# prevents rather than from taste: a print's day comes from the venue's own trade time,
# and the distance between that and our clock is bounded by the socket's latency plus the
# clock skew a live session tolerates -- both seconds, not minutes. Fifteen minutes is
# three orders of magnitude of slack on a boundary crossed once a day, and the cost of
# the slack is that yesterday's file appears fifteen minutes into today.
SEAL_GRACE: Final[timedelta] = timedelta(minutes=15)

SPOOL_SUFFIX: Final[str] = ".jsonl"

_MIDNIGHT: Final[time] = time(0, 0, 0)


@dataclass(frozen=True, slots=True)
class SealedPartition:
    """What one day's seal produced.

    `duplicates_dropped` is reported rather than swallowed. It is how many prints the
    session received twice, which is a direct measurement of reconnect overlap -- and a
    number that suddenly stops being zero says the socket is being reset in a way the
    reconnect log has not made obvious.
    """

    series: SeriesKey
    day: date
    path: Path
    prints_written: int
    duplicates_dropped: int
    was_rewritten: bool
    content_digest_hex: str


def spool_path(series: SeriesKey, day: date, *, spool_root: Path) -> Path:
    """The one spool file a print for `series` on `day` is appended to.

    Hive-style key/value segments matching the corpus layout, so an operator who has
    learned to read one directory tree can read the other, and so the parse back out of
    the path is anchored on named keys rather than on position.
    """
    return (
        spool_root
        / f"market={series.market.value}"
        / f"dataset={series.dataset.value}"
        / f"symbol={series.symbol}"
        / f"{day.isoformat()}{SPOOL_SUFFIX}"
    )


class TapeCorpusWriter:
    """Accumulates the live trade tape and seals each UTC day into one Parquet partition.

    Synchronous by design. `append` is a line-buffered write of a few hundred bytes per
    print, which is far cheaper than the thread hop that would take it off the event loop;
    `seal_elapsed_days` writes a whole day and is the one the caller should run in a
    thread, which `fking.data.live.supervisor` does.
    """

    __slots__ = ("_corpus_root", "_handles", "_seal_grace", "_spool_root")

    def __init__(
        self,
        *,
        corpus_root: Path,
        spool_root: Path,
        seal_grace: timedelta = SEAL_GRACE,
    ) -> None:
        if seal_grace < timedelta(0):
            raise ValueError(f"seal grace cannot be negative, got {seal_grace!r}")
        self._corpus_root = corpus_root
        self._spool_root = spool_root
        self._seal_grace = seal_grace
        self._handles: dict[tuple[SeriesKey, date], IO[str]] = {}

    def append(self, trades: Sequence[LiveTrade]) -> int:
        """Spool `trades`, returning how many prints were written.

        The day a print belongs to is read from its own `event_time_utc`, never from the
        clock. A print that arrived at 00:00:01 carrying a trade time of 23:59:59 belongs
        to yesterday's file, and filing it under today's would put it in a partition whose
        path claims a period it is not in -- which every partition-filtered query would
        then agree with.
        """
        if not trades:
            return 0
        for (series, day), spooled in _group_by_day(trades).items():
            handle = self._handle_for(series, day)
            handle.write("".join(f"{_spool_line(record)}\n" for record in spooled))
        return len(trades)

    def seal_elapsed_days(self, *, now_utc: datetime) -> tuple[SealedPartition, ...]:
        """Turn every spool for a day that has ended into its Parquet partition.

        Reads the spool directory rather than this instance's state, so a spool a previous
        process left behind is sealed by whichever session runs next. Ordered by series and
        then by day, so a run that seals several days does so oldest first and a failure
        part-way leaves the newer spools untouched rather than a hole in the middle.

        Raises:
            DataIntegrityError: a spool line is malformed, or the seal would replace an
                existing partition with fewer prints than it already holds.
        """
        _require_aware_utc(now_utc, "now_utc")
        sealed: list[SealedPartition] = []
        for series, day, path in sorted(
            self._elapsed_spools(now_utc=now_utc),
            key=lambda entry: (entry[0].symbol, entry[0].dataset.value, entry[1]),
        ):
            sealed.append(self._seal(series, day, path, now_utc=now_utc))
        return tuple(sealed)

    def close(self) -> None:
        """Close every open spool handle, sealing nothing.

        Shutdown is not a day boundary. A session that stops at noon has half a day
        spooled, and writing that half as the day's partition would put a file on disk
        claiming a period the corpus holds only part of -- which is the one thing
        `fking.data.parquet.write_records` refuses an empty batch to avoid. The spool stays
        and the next session continues it.
        """
        for handle in self._handles.values():
            handle.close()
        self._handles.clear()

    # ------------------------------------------------------------------
    # Internals
    # ------------------------------------------------------------------

    def _handle_for(self, series: SeriesKey, day: date) -> IO[str]:
        handle = self._handles.get((series, day))
        if handle is not None:
            return handle
        path = spool_path(series, day, spool_root=self._spool_root)
        path.parent.mkdir(parents=True, exist_ok=True)
        # buffering=1 is line buffering, and it is the durability decision this module's
        # docstring argues for: every print reaches the operating system as it is written,
        # so only a machine crash can lose one.
        opened = path.open("a", buffering=1, encoding="utf-8", newline="\n")
        self._handles[(series, day)] = opened
        return opened

    def _elapsed_spools(self, *, now_utc: datetime) -> Iterator[tuple[SeriesKey, date, Path]]:
        if not self._spool_root.is_dir():
            return
        for path in self._spool_root.rglob(f"*{SPOOL_SUFFIX}"):
            series, day = _parse_spool_path(path, spool_root=self._spool_root)
            if now_utc >= self._sealable_from(day):
                yield series, day, path

    def _sealable_from(self, day: date) -> datetime:
        return datetime.combine(day + timedelta(days=1), _MIDNIGHT, tzinfo=UTC) + self._seal_grace

    def _seal(
        self, series: SeriesKey, day: date, path: Path, *, now_utc: datetime
    ) -> SealedPartition:
        spooled = _read_spool(path)
        # The empty case is a spool file that exists with nothing in it, which a crash
        # between `open` and the first write produces. Removing it is correct: a day with
        # no prints is a real observation and it is recorded by the coverage registry, not
        # by a zero-row Parquet file that a later scan reads as "we have this period".
        if not spooled:
            self._discard(series, day, path)
            return SealedPartition(
                series=series,
                day=day,
                path=partition_path(_coordinate(series, day), root=self._corpus_root),
                prints_written=0,
                duplicates_dropped=0,
                was_rewritten=False,
                content_digest_hex="",
            )

        seam = reconcile_trades((), spooled)
        coordinate = _coordinate(series, day)
        target = partition_path(coordinate, root=self._corpus_root)
        _require_no_narrowing(target, prints_to_write=len(seam.merged), series=series, day=day)

        outcome = write_records(
            seam.merged,
            coordinate=coordinate,
            # Every print in a spool arrived on the socket. A print recovered from REST
            # after the fact is a different provenance and will be written by a different
            # path; collapsing them here would make gate 11's question -- which rows came
            # from a live stream and when -- unanswerable.
            source=RecordSource.STREAM,
            ingested_at_utc=now_utc,
            root=self._corpus_root,
        )
        self._discard(series, day, path)
        _LOG.info(
            "live.tape_sealed",
            symbol=series.symbol,
            dataset=series.dataset.value,
            day=day.isoformat(),
            prints_written=outcome.rows_written,
            duplicates_dropped=len(spooled) - len(seam.merged),
            was_rewritten=outcome.was_rewritten,
            path=str(outcome.path),
        )
        return SealedPartition(
            series=series,
            day=day,
            path=outcome.path,
            prints_written=outcome.rows_written,
            duplicates_dropped=len(spooled) - len(seam.merged),
            was_rewritten=outcome.was_rewritten,
            content_digest_hex=outcome.content_digest_hex,
        )

    def _discard(self, series: SeriesKey, day: date, path: Path) -> None:
        """Close the day's handle and remove its spool, in that order.

        The order is not cosmetic. Windows refuses to unlink a file that is still open, so
        a seal that removed first would raise on the platform this project is developed on
        and pass on the one it deploys to.
        """
        handle = self._handles.pop((series, day), None)
        if handle is not None:
            handle.close()
        path.unlink(missing_ok=True)


def _coordinate(series: SeriesKey, day: date) -> ArchiveCoordinate:
    return ArchiveCoordinate(
        market=series.market, dataset=series.dataset, symbol=series.symbol, archive_date=day
    )


def _require_no_narrowing(
    target: Path, *, prints_to_write: int, series: SeriesKey, day: date
) -> None:
    held_rows = partition_row_count(target)
    if held_rows is not None and held_rows > prints_to_write:
        raise DataIntegrityError(
            f"refusing to rewrite {target} for {series.symbol} {day.isoformat()} with "
            f"{prints_to_write} prints when it already holds {held_rows}. A partition is "
            f"written whole, so this would delete prints the corpus has -- most likely a "
            f"late print spooled after the day was sealed. Reconcile the spool against "
            f"the partition by hand; a seal cannot decide which of the two is the day"
        )


def _group_by_day(
    trades: Sequence[LiveTrade],
) -> Mapping[tuple[SeriesKey, date], list[TradeRecord]]:
    grouped: dict[tuple[SeriesKey, date], list[TradeRecord]] = {}
    for trade in trades:
        key = (trade.series, trade.record.event_time_utc.date())
        grouped.setdefault(key, []).append(trade.record)
    return grouped


def _spool_line(record: TradeRecord) -> str:
    """One print as a single JSON object, on one line, with no whitespace.

    Decimals go out through `str` and come back through `Decimal(str)`, so scale
    survives: `Decimal("1.50")` is what the venue filed and it is what the Parquet
    content digest hashes (`fking.data.parquet.writer`). A spool that normalised to
    `1.5` would make the sealed partition a function of the spool format rather than of
    the tape, and a re-seal would then disagree with the file it had already written.

    `sort_keys` so the bytes are a function of the print and not of the field order a
    dataclass happens to declare, which is what makes two spools of the same session
    comparable at all.
    """
    return json.dumps(
        {
            "venue_trade_id": record.venue_trade_id,
            "event_time_utc": record.event_time_utc.isoformat(),
            "quote_price": str(record.quote_price),
            "base_quantity": str(record.base_quantity),
            "quote_quantity": str(record.quote_quantity),
            "is_buyer_maker": record.is_buyer_maker,
            "is_best_match": record.is_best_match,
        },
        separators=(",", ":"),
        sort_keys=True,
    )


def _read_spool(path: Path) -> tuple[TradeRecord, ...]:
    """Every print in one spool file, in the order it was appended.

    A line that does not parse raises rather than being skipped. The tail of a spool is
    where a torn write would land, and dropping it silently would lose prints that the
    sequence detector never reported missing -- an absence with no gap row, which is the
    one shape of hole the coverage registry cannot describe.
    """
    records: list[TradeRecord] = []
    with path.open("r", encoding="utf-8") as handle:
        for number, line in enumerate(handle, start=1):
            stripped = line.strip()
            if not stripped:
                continue
            records.append(_spool_record(stripped, path=path, line_number=number))
    return tuple(records)


def _spool_record(line: str, *, path: Path, line_number: int) -> TradeRecord:
    """One spool line to the print it was written from.

    Field by field rather than by unpacking a comprehension over a field-name table: the
    record's fields have three different types, and a `**dict[str, object]` splat would
    type-check as whatever the dict's value type widened to, which is exactly the
    boundary where a boolean read as a decimal must not pass silently.
    """
    try:
        decoded: object = json.loads(line)
    except json.JSONDecodeError as malformed:
        raise DataIntegrityError(
            f"{path}:{line_number} is not JSON: {line[:120]!r}. A spool line is written by "
            f"one append of one line, so a torn one means the file was damaged rather than "
            f"interleaved, and the prints it held cannot be recovered from it"
        ) from malformed
    if not isinstance(decoded, dict):
        raise DataIntegrityError(
            f"{path}:{line_number} is a {type(decoded).__name__}, not a JSON object"
        )
    document: Mapping[str, object] = decoded
    return TradeRecord(
        venue_trade_id=_spool_string(document, "venue_trade_id", path=path, number=line_number),
        event_time_utc=_spool_moment(document, path=path, number=line_number),
        quote_price=_spool_decimal(document, "quote_price", path=path, number=line_number),
        base_quantity=_spool_decimal(document, "base_quantity", path=path, number=line_number),
        quote_quantity=_spool_decimal(document, "quote_quantity", path=path, number=line_number),
        is_buyer_maker=_spool_boolean(document, "is_buyer_maker", path=path, number=line_number),
        is_best_match=_spool_boolean(document, "is_best_match", path=path, number=line_number),
    )


def _spool_field(document: Mapping[str, object], name: str, *, path: Path, number: int) -> object:
    if name not in document:
        raise DataIntegrityError(f"{path}:{number} carries no {name!r} field")
    return document[name]


def _spool_string(document: Mapping[str, object], name: str, *, path: Path, number: int) -> str:
    raw_field = _spool_field(document, name, path=path, number=number)
    if not isinstance(raw_field, str):
        raise DataIntegrityError(
            f"{path}:{number} field {name!r} is a {type(raw_field).__name__}, not a string"
        )
    return raw_field


def _spool_decimal(
    document: Mapping[str, object], name: str, *, path: Path, number: int
) -> Decimal:
    raw_field = _spool_string(document, name, path=path, number=number)
    try:
        return Decimal(raw_field)
    except InvalidOperation as malformed:
        raise DataIntegrityError(
            f"{path}:{number} field {name!r} is {raw_field!r}, which is not a decimal"
        ) from malformed


def _spool_boolean(document: Mapping[str, object], name: str, *, path: Path, number: int) -> bool:
    raw_field = _spool_field(document, name, path=path, number=number)
    if not isinstance(raw_field, bool):
        raise DataIntegrityError(
            f"{path}:{number} field {name!r} is {raw_field!r}, not a JSON boolean. "
            f"is_buyer_maker is the aggressor side inverted, and a truthy string read as "
            f"a flag would leave every other column of the print correct"
        )
    return raw_field


def _spool_moment(document: Mapping[str, object], *, path: Path, number: int) -> datetime:
    raw_field = _spool_string(document, "event_time_utc", path=path, number=number)
    try:
        moment = datetime.fromisoformat(raw_field)
    except ValueError as malformed:
        raise DataIntegrityError(
            f"{path}:{number} event time {raw_field!r} is not an ISO 8601 instant"
        ) from malformed
    _require_aware_utc(moment, f"{path}:{number} event time")
    return moment


def _require_aware_utc(moment: datetime, described_as: str) -> None:
    if moment.tzinfo is None or moment.utcoffset() != UTC.utcoffset(None):
        raise DataIntegrityError(
            f"{described_as} must be timezone-aware UTC; got {moment!r}. The day a print "
            f"is filed under is read from this instant, so an offset moves prints across "
            f"partition boundaries by that offset"
        )


def _parse_spool_path(path: Path, *, spool_root: Path) -> tuple[SeriesKey, date]:
    """The series and day a spool file's path names.

    Anchored on the Hive keys rather than on position, so a directory that does not come
    from `spool_path` is refused instead of being read as whichever series its depth
    happened to line up with.

    Raises:
        DataIntegrityError: the path is not one `spool_path` produces, or names a market,
            dataset or day this system does not recognise.
    """
    try:
        relative = path.relative_to(spool_root)
    except ValueError as outside:
        raise DataIntegrityError(f"{path} is not inside the spool root {spool_root}") from outside

    segments = relative.parts
    expected_depth = 4
    if len(segments) != expected_depth:
        raise DataIntegrityError(
            f"{path} has {len(segments)} segments below the spool root, expected "
            f"{expected_depth}: market=/dataset=/symbol=/<day>{SPOOL_SUFFIX}"
        )
    market_token, dataset_token, symbol_token, day_token = segments
    try:
        return (
            SeriesKey(
                market=Market(_hive_value(market_token, "market", path=path)),
                dataset=Dataset(_hive_value(dataset_token, "dataset", path=path)),
                symbol=_hive_value(symbol_token, "symbol", path=path),
                # `''`, the registry's sentinel for a dataset that is not keyed by an
                # interval. The tape has no interval and never will.
                bar_interval="",
            ),
            date.fromisoformat(day_token.removesuffix(SPOOL_SUFFIX)),
        )
    except ValueError as unrecognised:
        raise DataIntegrityError(
            f"{path} names a market, dataset or day this system does not recognise"
        ) from unrecognised


def _hive_value(segment: str, key: str, *, path: Path) -> str:
    prefix = f"{key}="
    if not segment.startswith(prefix) or len(segment) == len(prefix):
        raise DataIntegrityError(f"{path} segment {segment!r} is not a {prefix!r} key")
    return segment.removeprefix(prefix)
