"""Reading bars out of the Parquet corpus, and resolving the unit they were stamped in.

Two decisions here are worth stating, because both have a version that works on the
corpus this project writes today and breaks on the one it will hold in two years.

**The epoch unit is resolved for every partition read, not only for the ones that need
it.** A partition our own writer produced carries `timestamp[us, tz=UTC]`, so the unit
question was settled at ingest and there is, on the face of it, nothing left to ask. It is
still asked, because "this file was normalised by something whose divisor we have on
record" and "this file contains plausible-looking datetimes" are different claims, and only
the first is checkable. A partition dated outside every declared segment in
`fking.data.format_resolver` was written under an assumption nobody wrote down, and reading
it silently is how a corpus acquires a region whose timestamps are confidently wrong. The
resolved unit is reported back to the caller, so a mixed spot/futures run can *show* that
it read microseconds on one leg and milliseconds on the other rather than asserting it.

**The window predicate is applied in Python; only the partition keys are pushed into SQL.**
Pushing `open_time_utc >= ?` down would be faster and would also mean handing DuckDB a
timestamp comparison against a column whose type is the very thing being validated -- a
file whose first column is a raw `BIGINT` epoch would fail with a type error naming a cast
rather than with `AmbiguousEpochUnitError` naming the partition. `year` and `month` are
pushed down, and they are the keys that eliminate *files*, which is the return the layout in
`fking.data.parquet.layout` was designed to buy.

The DuckDB connection is per call and in-memory, from `fking.data.parquet.read_connection`,
which also pins the session timezone to UTC -- without which a `TIMESTAMP WITH TIME ZONE`
comes back carrying the machine's local offset and every wall-clock reading of it is wrong
by that offset while every instant comparison still passes.
"""

from __future__ import annotations

import calendar
from collections.abc import Iterator, Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from typing import Final

from fking.backtest.feed._coverage import PartitionFormat
from fking.backtest.feed._errors import (
    AmbiguousEpochUnitError,
    CorpusIntegrityError,
    FeedRequestError,
)
from fking.data.archive import ArchiveCoordinate
from fking.data.format_resolver import (
    Dataset,
    EpochUnit,
    Market,
    epoch_to_utc,
    resolve_archive_format,
)
from fking.data.parquet import market_dataset_glob, read_connection
from fking.platform.errors import DataIntegrityError

__all__ = ["ArchiveBar", "SeriesRead", "read_series", "resolve_partition_epoch_unit"]

# Exactly the columns a `fking.domain.Bar` needs, plus the two partition keys the unit is
# resolved on. Named rather than `SELECT *` so that a column added to the canonical schema
# does not silently change the width of every row this module unpacks.
_COLUMNS: Final[tuple[str, ...]] = (
    "open_time_utc",
    "close_time_utc",
    "open_quote_price",
    "high_quote_price",
    "low_quote_price",
    "close_quote_price",
    "base_volume",
    "trade_count",
    "year",
    "month",
)

_ZERO: Final[timedelta] = timedelta(0)
_DECEMBER: Final[int] = 12
# The window is half-open on open times, so the last instant it can name is one microsecond
# below `until_utc` -- which is the resolution a `timestamp[us]` column carries.
_ONE_MICROSECOND: Final[timedelta] = timedelta(microseconds=1)


@dataclass(frozen=True, slots=True)
class ArchiveBar:
    """One bar as the corpus holds it, normalised to aware UTC and exact decimals.

    Deliberately not a `fking.domain.Bar`: a `Bar` carries an `Instrument`, and which
    instrument a row belongs to is the caller's knowledge rather than the file's. The
    conversion happens in `_feed`, where the `SeriesRequest` naming the instrument is in
    scope.
    """

    open_time_utc: datetime
    close_time_utc: datetime
    open_quote_price: Decimal
    high_quote_price: Decimal
    low_quote_price: Decimal
    close_quote_price: Decimal
    base_volume: Decimal
    trade_count: int


@dataclass(frozen=True, slots=True)
class SeriesRead:
    """Everything one series' read produced, including what it proves about provenance.

    `partition_formats` is not decoration. It is the evidence that the epoch unit was
    resolved per `(market, date)` rather than assumed once for the process, and it is what
    a mixed-market run is asserted on.
    """

    bars: tuple[ArchiveBar, ...]
    partition_formats: tuple[PartitionFormat, ...]


def resolve_partition_epoch_unit(*, market: Market, year: int, month: int) -> EpochUnit:
    """The declared epoch unit for one monthly kline partition, or a refusal.

    Resolved at both ends of the month and compared. Klines are partitioned monthly and the
    one cutover this project knows about -- Binance spot moving to microsecond epochs -- falls
    on 2025-01-01, a month boundary, so today the two ends always agree. They are both
    resolved anyway because the next cutover is not obliged to land on the first of a month,
    and a month split between two units read under either one is wrong for half its rows
    with nothing downstream able to notice.

    Raises:
        AmbiguousEpochUnitError: the pair is undeclared, the date falls outside every
            declared segment, the month spans two encodings, or the declared encoding is not
            an epoch at all.
    """
    first_day = date(year, month, 1)
    last_day = date(year, month, calendar.monthrange(year, month)[1])
    try:
        opening = resolve_archive_format(
            market=market, dataset=Dataset.KLINES, archive_date=first_day
        )
        closing = resolve_archive_format(
            market=market, dataset=Dataset.KLINES, archive_date=last_day
        )
    except DataIntegrityError as undeclared:
        raise AmbiguousEpochUnitError(
            f"no archive format is declared for {market.value}/klines covering "
            f"{first_day.isoformat()}..{last_day.isoformat()}, so the unit its timestamps were "
            f"normalised in is unknown: {undeclared}. The partition is refused rather than "
            f"read under the neighbouring month's divisor -- spot and futures differ by a "
            f"factor of a thousand for part of history"
        ) from undeclared

    if opening.timestamp_encoding is not closing.timestamp_encoding:
        raise AmbiguousEpochUnitError(
            f"{market.value}/klines {year:04d}-{month:02d} spans two declared timestamp "
            f"encodings ({opening.timestamp_encoding.value} then "
            f"{closing.timestamp_encoding.value}); a monthly partition read under either one "
            f"is wrong for half its rows. Split the partition at the cutover"
        )

    try:
        return opening.require_epoch_unit()
    except DataIntegrityError as not_an_epoch:
        raise AmbiguousEpochUnitError(
            f"{market.value}/klines {year:04d}-{month:02d} is declared as "
            f"{opening.timestamp_encoding.value}, which is not an epoch: {not_an_epoch}"
        ) from not_an_epoch


def months_between(from_utc: datetime, until_utc: datetime) -> tuple[tuple[int, int], ...]:
    """Every `(year, month)` partition a half-open window on open times can touch."""
    last_instant = until_utc - _ONE_MICROSECOND
    months: list[tuple[int, int]] = []
    year, month = from_utc.year, from_utc.month
    while (year, month) <= (last_instant.year, last_instant.month):
        months.append((year, month))
        year, month = (year + 1, 1) if month == _DECEMBER else (year, month + 1)
    return tuple(months)


def _partition_predicate(months: Sequence[tuple[int, int]]) -> str:
    """A DuckDB predicate over the hive keys, eliminating files rather than rows.

    `month` is a zero-padded VARCHAR and `year` a BIGINT, matching what
    `fking.data.parquet.layout` writes -- `month = 1` matches nothing against a padded
    corpus and says so with an empty result rather than an error.
    """
    return " OR ".join(f"(year = {year:d} AND month = '{month:02d}')" for year, month in months)


def _candidate_files(
    *, root: Path, market: Market, symbol: str, bar_interval: str
) -> Iterator[Path]:
    prefix = (
        root
        / f"market={market.value}"
        / f"dataset={Dataset.KLINES.value}"
        / f"symbol={symbol}"
        / f"interval={bar_interval}"
    )
    if not prefix.is_dir():
        return
    yield from prefix.rglob("*.parquet")


def read_series(  # noqa: PLR0913 -- see below
    *,
    root: Path,
    market: Market,
    symbol: str,
    bar_interval: str,
    from_utc: datetime,
    until_utc: datetime,
    now_utc: datetime,
    duckdb_thread_count: int | None = None,
) -> SeriesRead:
    """Bars the corpus holds for one series with an open time in `[from_utc, until_utc)`.

    Eight keyword-only parameters, and PLR0913 is silenced rather than satisfied. Six of them
    are the coordinates of the read -- where the corpus is, which corpus, which symbol, which
    interval, and the two ends of the window -- and folding them into a request object would
    mean constructing a second object to describe the first. The other two are the injection
    points a reviewer most needs to see: `now_utc` is the plausibility reference that makes
    the read replayable, and `duckdb_thread_count` is the knob the determinism proof varies.
    Both would disappear inside a bundle.

    Returns an empty read for a series with no partitions at all. That is the ordinary shape
    of "this symbol has never been ingested", and the coverage report -- which knows the
    lattice the caller asked for -- is where it becomes a refusal naming the whole window as
    one gap.

    Raises:
        AmbiguousEpochUnitError: a partition's epoch unit cannot be resolved.
        CorpusIntegrityError: a column holds a type the canonical schema forbids, or two
            rows share an open time.
        DataIntegrityError: a raw epoch normalises outside the plausible window. Raised by
            `fking.data.format_resolver.epoch_to_utc` and deliberately not relabelled here:
            its message already names the value, the unit it was read in and the window it
            fell outside, and anything this package wrapped it in would be vaguer.
        FeedRequestError: `duckdb_thread_count` is not a positive integer.
    """
    if not any(
        _candidate_files(root=root, market=market, symbol=symbol, bar_interval=bar_interval)
    ):
        return SeriesRead(bars=(), partition_formats=())

    months = months_between(from_utc, until_utc)
    units = {
        (year, month): resolve_partition_epoch_unit(market=market, year=year, month=month)
        for year, month in months
    }
    coordinate = ArchiveCoordinate(
        market=market,
        dataset=Dataset.KLINES,
        symbol=symbol,
        archive_date=from_utc.date(),
        interval=bar_interval,
    )
    glob_sql = market_dataset_glob(coordinate, root=root, narrow_to_symbol=True)
    projection = ", ".join(f'"{column}"' for column in _COLUMNS)
    where = f"{market.value}/{symbol}"

    with read_connection() as connection:
        if duckdb_thread_count is not None:
            connection.execute(f"SET threads = {_thread_count(duckdb_thread_count):d}")
        rows = connection.execute(
            # `read_parquet` takes a path pattern rather than a bind parameter, so there is
            # no parameterised spelling of this call. Both interpolated values are built
            # from typed partition keys, here and in
            # `fking.data.parquet.layout.market_dataset_glob`, never from a caller's string.
            f"SELECT {projection} FROM read_parquet('{glob_sql}', hive_partitioning = true) "  # noqa: S608
            f"WHERE {_partition_predicate(months)}"
        ).fetchall()

    read = tuple(
        _bar_from(dict(zip(_COLUMNS, row, strict=True)), where=where, units=units, now_utc=now_utc)
        for row in rows
    )
    inside = tuple(bar for bar in read if from_utc <= bar.open_time_utc < until_utc)
    # Sorted here rather than by the query, because the ordering key is the *normalised*
    # instant and the column it came from may have been a raw epoch. Sorting on the raw
    # column would order a month of microseconds after a month of milliseconds regardless
    # of which came first in time.
    ordered = tuple(sorted(inside, key=lambda entry: entry.open_time_utc))
    _require_distinct_open_times(ordered, where=where)
    touched = sorted({(bar.open_time_utc.year, bar.open_time_utc.month) for bar in ordered})
    return SeriesRead(
        bars=ordered,
        partition_formats=tuple(
            PartitionFormat(year_month=f"{year:04d}-{month:02d}", epoch_unit=units[year, month])
            for year, month in touched
        ),
    )


def _thread_count(candidate: int) -> int:
    """A positive DuckDB thread count.

    A knob on throughput and on nothing else. It is deliberately absent from `RunConfig`:
    anything that can change without changing the result must stay out of the run's
    identity, or two runs producing identical numbers carry different hashes and the
    determinism check compares nothing. The determinism suite varies it precisely to prove
    the digest does not move with it.
    """
    if not isinstance(candidate, int) or isinstance(candidate, bool) or candidate < 1:
        raise FeedRequestError(
            f"duckdb_thread_count must be a positive int, got {candidate!r}; DuckDB refuses "
            f"zero and the refusal names a setting rather than a run"
        )
    return candidate


def _require_distinct_open_times(bars: Sequence[ArchiveBar], *, where: str) -> None:
    """Two rows at one open time are two answers to one question, not extra data.

    A duplicate would be dispatched as two `MarketDataEvent`s at one instant, so every count
    derived from the run doubles for that bar and any rolling window over it is computed
    from a series one observation longer than the lattice says it is.
    """
    seen: set[datetime] = set()
    for bar in bars:
        if bar.open_time_utc in seen:
            raise CorpusIntegrityError(
                f"{where} holds two bars opening at {bar.open_time_utc.isoformat()}. The "
                f"corpus is keyed on (instrument, timeframe, open_time) and a duplicate means "
                f"two partitions claim the same period -- dispatching both would double every "
                f"count derived from that instant"
            )
        seen.add(bar.open_time_utc)


def _bar_from(
    row: Mapping[str, object],
    *,
    where: str,
    units: Mapping[tuple[int, int], EpochUnit],
    now_utc: datetime,
) -> ArchiveBar:
    year = _as_int(row["year"], column="year", where=where)
    month = int(_as_text(row["month"], column="month", where=where))
    unit = units[year, month]
    partition = f"{where} {year:04d}-{month:02d}"
    return ArchiveBar(
        open_time_utc=_as_moment(
            row["open_time_utc"],
            column="open_time_utc",
            where=partition,
            unit=unit,
            now_utc=now_utc,
        ),
        close_time_utc=_as_moment(
            row["close_time_utc"],
            column="close_time_utc",
            where=partition,
            unit=unit,
            now_utc=now_utc,
        ),
        open_quote_price=_as_decimal(
            row["open_quote_price"], column="open_quote_price", where=partition
        ),
        high_quote_price=_as_decimal(
            row["high_quote_price"], column="high_quote_price", where=partition
        ),
        low_quote_price=_as_decimal(
            row["low_quote_price"], column="low_quote_price", where=partition
        ),
        close_quote_price=_as_decimal(
            row["close_quote_price"], column="close_quote_price", where=partition
        ),
        base_volume=_as_decimal(row["base_volume"], column="base_volume", where=partition),
        trade_count=_as_int(row["trade_count"], column="trade_count", where=partition),
    )


def _as_moment(
    raw_field: object, *, column: str, where: str, unit: EpochUnit, now_utc: datetime
) -> datetime:
    """An instant, whether the column holds a normalised timestamp or a raw epoch.

    A `datetime` is the shape `fking.data.parquet.writer` produces and is accepted once its
    offset is checked. An `int` is a file this project did not write -- a hand-staged
    partition, or one produced by an older tool -- and it is normalised with the unit
    declared for its own `(market, month)`, never with a default. `epoch_to_utc` then applies
    the plausibility window, which is what turns a thousand-fold unit error into a refusal
    naming the unit rather than a 1970 timestamp nobody looks at.
    """
    if isinstance(raw_field, datetime):
        if raw_field.tzinfo is None or raw_field.utcoffset() != _ZERO:
            raise CorpusIntegrityError(
                f"{where} column {column!r} holds {raw_field!r}, which is not aware UTC. The "
                f"canonical column is `timestamp[us, tz=UTC]`, so a naive value means the "
                f"partition was written by something other than fking.data.parquet.writer"
            )
        # Re-stamped with the stdlib UTC. DuckDB hands back pytz's UTC; the instant is the
        # same, but two tzinfo objects with one offset are not the same object, and letting
        # a library's identity reach an object that gets hashed is how a digest acquires a
        # dependency on a transitive version.
        return raw_field.astimezone(UTC)
    if isinstance(raw_field, int) and not isinstance(raw_field, bool):
        return epoch_to_utc(raw_field, unit=unit, now_utc=now_utc)
    raise CorpusIntegrityError(
        f"{where} column {column!r} is a {type(raw_field).__name__}, which is neither a "
        f"timestamp nor an integer epoch"
    )


def _as_decimal(raw_field: object, *, column: str, where: str) -> Decimal:
    if not isinstance(raw_field, Decimal):
        raise CorpusIntegrityError(
            f"{where} column {column!r} is a {type(raw_field).__name__}, not a Decimal. A "
            f"float here means the column was written as `double`, which is the failure the "
            f"declared decimal128(38, 18) schema exists to prevent"
        )
    return raw_field


def _as_int(raw_field: object, *, column: str, where: str) -> int:
    if not isinstance(raw_field, int) or isinstance(raw_field, bool):
        raise CorpusIntegrityError(
            f"{where} column {column!r} is a {type(raw_field).__name__}, not an integer"
        )
    return raw_field


def _as_text(raw_field: object, *, column: str, where: str) -> str:
    if not isinstance(raw_field, str):
        raise CorpusIntegrityError(
            f"{where} partition key {column!r} is a {type(raw_field).__name__}, not a string. "
            f"`month` is written zero-padded, which is what keeps DuckDB typing it VARCHAR "
            f"rather than BIGINT"
        )
    return raw_field
