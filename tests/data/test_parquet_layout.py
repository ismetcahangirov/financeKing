"""The canonical Parquet layout and the DuckDB read path (#24).

Every assertion here is about a property that fails *silently* when it is wrong, which
is why they are written against real files and a real DuckDB rather than against a mock
of either:

- A path that lands one directory off still writes, still reads back, and only stops
  matching a glob some other module builds three months later.
- A `double` OHLCV column round-trips every value a test happens to choose and loses the
  eighteenth decimal on the ones production produces.
- An unsorted file has row-group statistics whose ranges overlap on every row group, so
  predicate pushdown eliminates nothing -- and the symptom is a slow scan that looks
  like a DuckDB problem rather than a writer problem.
- A glob one segment too short unions spot and futures rows, which had different epoch
  units for part of history.
"""

from __future__ import annotations

import hashlib
import random
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from typing import Final

import duckdb
import pyarrow.parquet as pq
import pytest

from fking.data.archive import ArchiveCoordinate
from fking.data.format_resolver import Dataset, Market
from fking.data.loaders import IMPLEMENTED_DATASETS, ArchiveRecord, KlineRecord, TradeRecord
from fking.data.parquet import (
    DATASET_PARTITION_GRAIN,
    DATASET_SCHEMAS,
    MONEY_COLUMN_SUFFIXES,
    MONEY_TYPE,
    RecordSource,
    market_dataset_glob,
    partition_path,
    read_connection,
    scanned_file_count,
    write_records,
)
from fking.platform.errors import DataIntegrityError

INGESTED_AT = datetime(2026, 8, 4, 11, 0, tzinfo=UTC)
LATER = datetime(2026, 8, 5, 11, 0, tzinfo=UTC)

# 18 significant decimal places, the full scale of NUMERIC(38, 18). Chosen because it is
# not representable in binary floating point: a writer that stored these as `double`
# would fail this exact value while passing on anything a round number.
DUST = Decimal("0.000000010000000001")

# Binance files a kline's close_time as the last representable instant *inside* the
# interval, which on a microsecond-epoch spot archive is 59.999999 past the minute. The
# trailing six digits are what a `timestamp[ms]` column would drop.
LAST_MICROSECOND: Final[int] = 999_999

# The offset used to demonstrate what an unpinned DuckDB session does to a TIMESTAMPTZ.
# A fixed-offset zone rather than a named one, so the test does not depend on a tzdata
# revision or on ICU being loadable.
DEMO_OFFSET: Final[timedelta] = timedelta(hours=4)


def kline_at(minute: int, *, open_quote_price: Decimal = DUST, month: int = 1) -> KlineRecord:
    opened = datetime(2025, month, 2, tzinfo=UTC) + timedelta(minutes=minute)
    return KlineRecord(
        open_time_utc=opened,
        close_time_utc=opened + timedelta(seconds=59, microseconds=LAST_MICROSECOND),
        open_quote_price=open_quote_price,
        high_quote_price=Decimal("94001.10"),
        low_quote_price=Decimal("93000.00"),
        close_quote_price=Decimal("93500.55"),
        base_volume=Decimal("12.34567890"),
        quote_volume=Decimal("1160000.123456789012345678"),
        trade_count=417,
        taker_buy_base_volume=Decimal("6.00000000"),
        taker_buy_quote_volume=Decimal("560000.10"),
        ignored_field="0",
    )


def trade_at(second: int) -> TradeRecord:
    return TradeRecord(
        venue_trade_id=str(4_000_000 + second),
        event_time_utc=datetime(2025, 1, 2, tzinfo=UTC) + timedelta(seconds=second),
        quote_price=DUST,
        base_quantity=Decimal("0.00100000"),
        quote_quantity=Decimal("93.50055"),
        is_buyer_maker=second % 2 == 0,
        is_best_match=True,
    )


SPOT_KLINES = ArchiveCoordinate(
    market=Market.SPOT,
    dataset=Dataset.KLINES,
    symbol="BTCUSDT",
    archive_date=date(2025, 1, 2),
    interval="1m",
)
SPOT_TRADES = ArchiveCoordinate(
    market=Market.SPOT,
    dataset=Dataset.TRADES,
    symbol="BTCUSDT",
    archive_date=date(2025, 1, 2),
)
FUTURES_KLINES = ArchiveCoordinate(
    market=Market.FUTURES_UM,
    dataset=Dataset.KLINES,
    symbol="BTCUSDT",
    archive_date=date(2025, 1, 2),
    interval="1m",
)


@pytest.fixture(name="root")
def root_fixture(tmp_path: Path) -> Path:
    return tmp_path / "parquet"


# ---------------------------------------------------------------------------
# Layout
# ---------------------------------------------------------------------------


def test_a_spot_kline_month_lands_at_the_declared_hive_path(root: Path) -> None:
    outcome = write_records(
        (kline_at(0),),
        coordinate=SPOT_KLINES,
        source=RecordSource.ARCHIVE,
        ingested_at_utc=INGESTED_AT,
        root=root,
    )
    assert outcome.path == (
        root
        / "market=spot"
        / "dataset=klines"
        / "symbol=BTCUSDT"
        / "interval=1m"
        / "year=2025"
        / "month=01"
        / "part-2025-01.parquet"
    )
    assert outcome.path.is_file()


def test_a_spot_trade_day_lands_at_the_declared_hive_path(root: Path) -> None:
    outcome = write_records(
        (trade_at(0),),
        coordinate=SPOT_TRADES,
        source=RecordSource.ARCHIVE,
        ingested_at_utc=INGESTED_AT,
        root=root,
    )
    assert outcome.path == (
        root
        / "market=spot"
        / "dataset=trades"
        / "symbol=BTCUSDT"
        / "year=2025"
        / "month=01"
        / "day=02"
        / "part-2025-01-02.parquet"
    )


def test_bars_are_monthly_and_trades_are_daily(root: Path) -> None:
    """The two granularities are the layout decision most easily lost in a refactor."""
    bar_path = partition_path(SPOT_KLINES, root=root)
    trade_path = partition_path(SPOT_TRADES, root=root)
    assert "day=" not in str(bar_path)
    assert "day=02" in str(trade_path)
    # A second day in the same month is the same bar file and a different trade file.
    next_day = date(2025, 1, 3)
    assert (
        partition_path(
            ArchiveCoordinate(
                market=Market.SPOT,
                dataset=Dataset.KLINES,
                symbol="BTCUSDT",
                archive_date=next_day,
                interval="1m",
            ),
            root=root,
        )
        == bar_path
    )
    assert (
        partition_path(
            ArchiveCoordinate(
                market=Market.SPOT,
                dataset=Dataset.TRADES,
                symbol="BTCUSDT",
                archive_date=next_day,
            ),
            root=root,
        )
        != trade_path
    )


def test_a_dataset_without_a_canonical_schema_is_refused(root: Path) -> None:
    coordinate = ArchiveCoordinate(
        market=Market.FUTURES_UM,
        dataset=Dataset.BOOK_DEPTH,
        symbol="BTCUSDT",
        archive_date=date(2025, 1, 2),
    )
    with pytest.raises(DataIntegrityError, match="no canonical schema"):
        partition_path(coordinate, root=root)


# ---------------------------------------------------------------------------
# Types on disk
# ---------------------------------------------------------------------------


def _describe(connection: duckdb.DuckDBPyConnection, glob_sql: str) -> dict[str, str]:
    rows = connection.execute(
        f"DESCRIBE SELECT * FROM read_parquet('{glob_sql}', hive_partitioning = true)"
    ).fetchall()
    return {str(row[0]): str(row[1]) for row in rows}


@pytest.mark.parametrize(
    ("coordinate", "records"),
    [
        pytest.param(SPOT_KLINES, (kline_at(0),), id="klines"),
        pytest.param(SPOT_TRADES, (trade_at(0),), id="trades"),
    ],
)
def test_every_money_column_reads_back_as_decimal_38_18(
    root: Path,
    coordinate: ArchiveCoordinate,
    records: tuple[ArchiveRecord, ...],
) -> None:
    """Keyed on the name suffix, not a hand-listed set.

    A column added later is covered the moment it is named `_price`, `_volume` or
    `_quantity`, which is the point of the suffix convention. A hand-maintained list
    would silently stop covering the new column, and `double` is Parquet's default for
    a Python `Decimal` that arrives without an explicit type.
    """
    write_records(
        records,
        coordinate=coordinate,
        source=RecordSource.ARCHIVE,
        ingested_at_utc=INGESTED_AT,
        root=root,
    )
    with read_connection() as connection:
        described = _describe(connection, market_dataset_glob(coordinate, root=root))

    money_columns = [name for name in described if name.endswith(tuple(MONEY_COLUMN_SUFFIXES))]
    assert money_columns, "the suffix convention found no money columns to check"
    assert {described[name] for name in money_columns} == {"DECIMAL(38,18)"}


def test_a_dust_quantity_round_trips_exactly(root: Path) -> None:
    write_records(
        (kline_at(0, open_quote_price=DUST),),
        coordinate=SPOT_KLINES,
        source=RecordSource.ARCHIVE,
        ingested_at_utc=INGESTED_AT,
        root=root,
    )
    with read_connection() as connection:
        value = connection.execute(
            f"SELECT open_quote_price FROM "
            f"read_parquet('{market_dataset_glob(SPOT_KLINES, root=root)}')"
        ).fetchall()[0][0]
    assert isinstance(value, Decimal)
    assert value == DUST
    # Equality alone would pass for Decimal("1E-8"); the scale must survive too.
    assert value.as_tuple().exponent == DUST.as_tuple().exponent


def test_timestamps_read_back_as_aware_utc(root: Path) -> None:
    write_records(
        (kline_at(0),),
        coordinate=SPOT_KLINES,
        source=RecordSource.ARCHIVE,
        ingested_at_utc=INGESTED_AT,
        root=root,
    )
    with read_connection() as connection:
        opened, ingested = connection.execute(
            f"SELECT open_time_utc, ingested_at_utc FROM "
            f"read_parquet('{market_dataset_glob(SPOT_KLINES, root=root)}')"
        ).fetchall()[0]
    assert opened == datetime(2025, 1, 2, tzinfo=UTC)
    assert ingested == INGESTED_AT
    assert opened.utcoffset() == timedelta(0)


def test_the_read_connection_pins_the_session_timezone_to_utc() -> None:
    with read_connection() as connection:
        setting = connection.execute("SELECT current_setting('TimeZone')").fetchall()
    assert setting == [("UTC",)]


def test_an_unpinned_connection_localises_the_instant(root: Path) -> None:
    """Proves the pin is protecting against something real, on a UTC machine too.

    `test_timestamps_read_back_as_aware_utc` only fails without the pin when the
    developer's machine is not in UTC, which makes it a test that passes in CI while the
    defect is live. This one sets the offending timezone explicitly, so it is the same
    test everywhere.

    The instant is preserved -- that is what makes the defect quiet. Every equality
    assertion against a UTC datetime still passes; only `.hour`, `.date()` and anything
    that later drops the tzinfo are wrong, and they are wrong by the offset.
    """
    write_records(
        (kline_at(0),),
        coordinate=SPOT_KLINES,
        source=RecordSource.ARCHIVE,
        ingested_at_utc=INGESTED_AT,
        root=root,
    )
    query = (
        f"SELECT open_time_utc FROM read_parquet('{market_dataset_glob(SPOT_KLINES, root=root)}')"
    )
    unpinned = duckdb.connect(database=":memory:")
    try:
        unpinned.execute("SET TimeZone = 'Etc/GMT-4'")
        localised = unpinned.execute(query).fetchall()[0][0]
    finally:
        unpinned.close()

    with read_connection() as connection:
        pinned = connection.execute(query).fetchall()[0][0]

    assert localised == pinned, "the instant is the same either way; that is the trap"
    assert localised.utcoffset() == DEMO_OFFSET
    assert localised.hour == DEMO_OFFSET // timedelta(hours=1)
    assert pinned.utcoffset() == timedelta(0)
    assert pinned.hour == 0


def test_the_close_time_microseconds_survive(root: Path) -> None:
    """Spot archives from 2025-01-01 are microsecond epochs; a `ms` column truncates."""
    record = kline_at(0)
    write_records(
        (record,),
        coordinate=SPOT_KLINES,
        source=RecordSource.ARCHIVE,
        ingested_at_utc=INGESTED_AT,
        root=root,
    )
    with read_connection() as connection:
        closed = connection.execute(
            f"SELECT close_time_utc FROM "
            f"read_parquet('{market_dataset_glob(SPOT_KLINES, root=root)}')"
        ).fetchall()[0][0]
    assert closed == record.close_time_utc
    assert closed.microsecond == LAST_MICROSECOND


def test_the_always_zero_trailing_column_is_not_stored(root: Path) -> None:
    """The parser keeps `ignored_field` so the field count is the file's field count.

    On disk it is a column of zeroes with no reader, so it is dropped here rather than
    carried forward -- and the drop is asserted so nobody restores it by reflex.
    """
    write_records(
        (kline_at(0),),
        coordinate=SPOT_KLINES,
        source=RecordSource.ARCHIVE,
        ingested_at_utc=INGESTED_AT,
        root=root,
    )
    with read_connection() as connection:
        described = _describe(connection, market_dataset_glob(SPOT_KLINES, root=root))
    assert "ignored_field" not in described


def test_provenance_columns_are_written(root: Path) -> None:
    """Gate 11 of the quality gate queries `source`; it can only do so if it is here."""
    write_records(
        (trade_at(0),),
        coordinate=SPOT_TRADES,
        source=RecordSource.STREAM,
        ingested_at_utc=INGESTED_AT,
        root=root,
    )
    with read_connection() as connection:
        origin = connection.execute(
            f"SELECT DISTINCT source FROM "
            f"read_parquet('{market_dataset_glob(SPOT_TRADES, root=root)}')"
        ).fetchall()
    assert origin == [("stream",)]


# ---------------------------------------------------------------------------
# Sort order
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("coordinate", "records", "column"),
    [
        pytest.param(
            SPOT_KLINES,
            tuple(kline_at(minute) for minute in range(60)),
            "open_time_utc",
            id="klines",
        ),
        pytest.param(
            SPOT_TRADES,
            tuple(trade_at(second) for second in range(60)),
            "event_time_utc",
            id="trades",
        ),
    ],
)
def test_rows_are_written_in_non_decreasing_event_time_order(
    root: Path,
    coordinate: ArchiveCoordinate,
    records: tuple[ArchiveRecord, ...],
    column: str,
) -> None:
    """Shuffled in, sorted out.

    Predicate pushdown works on row-group statistics. An unsorted file has min/max
    ranges that overlap on every row group, so a filtered scan eliminates nothing and
    reads the whole file -- and the partitioning appears to have failed for a reason
    that looks like a DuckDB problem.
    """
    shuffled = list(records)
    random.Random(20260804).shuffle(shuffled)
    assert [record.event_time_utc for record in shuffled] != [
        record.event_time_utc for record in records
    ], "the shuffle did not change the order, so this test would prove nothing"

    write_records(
        tuple(shuffled),
        coordinate=coordinate,
        source=RecordSource.ARCHIVE,
        ingested_at_utc=INGESTED_AT,
        root=root,
    )
    with read_connection() as connection:
        # No ORDER BY: the assertion is about the order on disk, not the order DuckDB
        # can produce.
        written = [
            row[0]
            for row in connection.execute(
                f"SELECT {column} FROM read_parquet('{market_dataset_glob(coordinate, root=root)}')"
            ).fetchall()
        ]
    assert written == sorted(written)
    assert len(written) == len(records)


# ---------------------------------------------------------------------------
# Partition pruning
# ---------------------------------------------------------------------------


GRID_SYMBOLS: Final[tuple[str, ...]] = ("BTCUSDT", "ETHUSDT")
GRID_MONTHS: Final[tuple[int, ...]] = (1, 2)


def _write_kline_grid(root: Path) -> int:
    """One file per (symbol, month). Returns how many files that is."""
    for symbol in GRID_SYMBOLS:
        for month in GRID_MONTHS:
            write_records(
                (kline_at(0, month=month),),
                coordinate=ArchiveCoordinate(
                    market=Market.SPOT,
                    dataset=Dataset.KLINES,
                    symbol=symbol,
                    archive_date=date(2025, month, 2),
                    interval="1m",
                ),
                source=RecordSource.ARCHIVE,
                ingested_at_utc=INGESTED_AT,
                root=root,
            )
    return len(GRID_SYMBOLS) * len(GRID_MONTHS)


def test_a_symbol_and_month_filter_touches_fewer_files(root: Path) -> None:
    """The property the whole layout exists to buy, measured rather than assumed."""
    written = _write_kline_grid(root)
    glob_sql = market_dataset_glob(SPOT_KLINES, root=root)
    with read_connection() as connection:
        unfiltered = scanned_file_count(connection, glob_sql=glob_sql)
        filtered = scanned_file_count(
            connection,
            glob_sql=glob_sql,
            predicate_sql="symbol = 'BTCUSDT' AND month = '01'",
        )
    assert unfiltered == written
    assert filtered == 1
    assert filtered < unfiltered


def test_a_single_market_glob_returns_no_rows_from_the_other_market(root: Path) -> None:
    """Spot and futures had different epoch units for part of history (VF-015).

    The partition boundary is what stops a query unioning them, so a glob rooted above
    `market=` is a defect rather than a convenience.
    """
    write_records(
        (kline_at(0),),
        coordinate=SPOT_KLINES,
        source=RecordSource.ARCHIVE,
        ingested_at_utc=INGESTED_AT,
        root=root,
    )
    write_records(
        (kline_at(0),),
        coordinate=FUTURES_KLINES,
        source=RecordSource.ARCHIVE,
        ingested_at_utc=INGESTED_AT,
        root=root,
    )
    with read_connection() as connection:
        spot_markets = connection.execute(
            f"SELECT DISTINCT market FROM read_parquet("
            f"'{market_dataset_glob(SPOT_KLINES, root=root)}', hive_partitioning = true)"
        ).fetchall()
        futures_markets = connection.execute(
            f"SELECT DISTINCT market FROM read_parquet("
            f"'{market_dataset_glob(FUTURES_KLINES, root=root)}', hive_partitioning = true)"
        ).fetchall()
    assert spot_markets == [("spot",)]
    assert futures_markets == [("futures_um",)]


def test_the_reader_builds_one_glob_per_market_and_dataset(root: Path) -> None:
    """There is no spelling of `market_dataset_glob` that spans two markets."""
    spot = market_dataset_glob(SPOT_KLINES, root=root)
    futures = market_dataset_glob(FUTURES_KLINES, root=root)
    assert "market=spot/dataset=klines" in spot
    assert "market=futures_um/dataset=klines" in futures
    assert spot != futures
    # Forward slashes even on Windows: DuckDB's glob syntax is not the OS separator.
    assert "\\" not in spot


def test_narrowing_stops_at_the_symbol_and_does_not_pin_the_period(root: Path) -> None:
    """A glob pinned to the coordinate's own month silently returns one month.

    The common scan is one symbol over a date *range*, and a range belongs in the
    predicate. Narrowing past `interval` would turn an optimisation into a filter the
    caller did not ask for, and the symptom is a short result rather than an error.
    """
    narrowed = market_dataset_glob(SPOT_KLINES, root=root, narrow_to_symbol=True)
    assert "symbol=BTCUSDT/interval=1m" in narrowed
    assert "year=" not in narrowed
    assert "month=" not in narrowed


def test_narrowing_to_a_symbol_still_spans_every_month_it_holds(root: Path) -> None:
    written = _write_kline_grid(root)
    narrowed = market_dataset_glob(SPOT_KLINES, root=root, narrow_to_symbol=True)
    with read_connection() as connection:
        touched = scanned_file_count(connection, glob_sql=narrowed)
    # One symbol out of two, but both of its months.
    assert touched == written // len(GRID_SYMBOLS)
    assert touched == len(GRID_MONTHS)


# ---------------------------------------------------------------------------
# Idempotency
# ---------------------------------------------------------------------------


def _digest(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def test_writing_the_same_batch_twice_produces_one_file_with_identical_bytes(
    root: Path,
) -> None:
    records = tuple(kline_at(minute) for minute in range(5))
    first = write_records(
        records,
        coordinate=SPOT_KLINES,
        source=RecordSource.ARCHIVE,
        ingested_at_utc=INGESTED_AT,
        root=root,
    )
    before = _digest(first.path)

    second = write_records(
        records,
        coordinate=SPOT_KLINES,
        source=RecordSource.ARCHIVE,
        ingested_at_utc=INGESTED_AT,
        root=root,
    )
    assert second.path == first.path
    assert list(first.path.parent.iterdir()) == [first.path]
    assert _digest(second.path) == before
    assert first.was_rewritten is True
    assert second.was_rewritten is False


def test_a_rerun_on_a_later_day_does_not_rewrite_the_file(root: Path) -> None:
    """The real re-backfill: same bytes upstream, a clock that has moved.

    Idempotency defined as "deterministic serialisation" alone would fail here, because
    `ingested_at_utc` is genuinely different. The writer compares a content digest of
    the records -- which excludes `ingested_at_utc`, since when we happened to read a
    file is not part of what the file said -- and declines to rewrite.
    """
    records = tuple(kline_at(minute) for minute in range(5))
    first = write_records(
        records,
        coordinate=SPOT_KLINES,
        source=RecordSource.ARCHIVE,
        ingested_at_utc=INGESTED_AT,
        root=root,
    )
    before = _digest(first.path)

    second = write_records(
        records,
        coordinate=SPOT_KLINES,
        source=RecordSource.ARCHIVE,
        ingested_at_utc=LATER,
        root=root,
    )
    assert second.was_rewritten is False
    assert _digest(second.path) == before
    assert second.content_digest_hex == first.content_digest_hex


def test_changed_content_does_rewrite_the_file(root: Path) -> None:
    """The counterpart. A digest that never differs is a digest nobody is checking."""
    first = write_records(
        (kline_at(0),),
        coordinate=SPOT_KLINES,
        source=RecordSource.ARCHIVE,
        ingested_at_utc=INGESTED_AT,
        root=root,
    )
    extended = (kline_at(0), kline_at(1))
    second = write_records(
        extended,
        coordinate=SPOT_KLINES,
        source=RecordSource.ARCHIVE,
        ingested_at_utc=INGESTED_AT,
        root=root,
    )
    assert second.was_rewritten is True
    assert second.content_digest_hex != first.content_digest_hex
    assert second.rows_written == len(extended)


def test_the_content_digest_is_stored_in_the_files_own_metadata(root: Path) -> None:
    """Stored in the file rather than in a sidecar.

    A sidecar can be deleted, copied without its file, or left behind by a partial
    restore, and every one of those makes the digest describe a file it is not
    attached to.
    """
    outcome = write_records(
        (kline_at(0),),
        coordinate=SPOT_KLINES,
        source=RecordSource.ARCHIVE,
        ingested_at_utc=INGESTED_AT,
        root=root,
    )
    metadata = pq.read_schema(outcome.path).metadata
    assert metadata is not None
    assert metadata[b"fking.content_digest"].decode() == outcome.content_digest_hex


def test_the_digest_does_not_depend_on_the_order_records_arrive_in(root: Path) -> None:
    """Sorting happens before hashing, so a re-download in a different order is a no-op."""
    records = tuple(kline_at(minute) for minute in range(5))
    shuffled = list(records)
    random.Random(4).shuffle(shuffled)

    first = write_records(
        records,
        coordinate=SPOT_KLINES,
        source=RecordSource.ARCHIVE,
        ingested_at_utc=INGESTED_AT,
        root=root,
    )
    second = write_records(
        tuple(shuffled),
        coordinate=SPOT_KLINES,
        source=RecordSource.ARCHIVE,
        ingested_at_utc=INGESTED_AT,
        root=root,
    )
    assert second.content_digest_hex == first.content_digest_hex
    assert second.was_rewritten is False


def test_the_digest_distinguishes_the_provenance_of_otherwise_equal_rows(
    root: Path,
) -> None:
    """A stream-sourced bar and an archive-sourced bar are not the same row.

    If `source` were outside the digest, a backfill that re-fetched a stream-written
    month from the archive would decline to correct it -- which is the one rewrite that
    must happen.
    """
    first = write_records(
        (kline_at(0),),
        coordinate=SPOT_KLINES,
        source=RecordSource.STREAM,
        ingested_at_utc=INGESTED_AT,
        root=root,
    )
    second = write_records(
        (kline_at(0),),
        coordinate=SPOT_KLINES,
        source=RecordSource.ARCHIVE,
        ingested_at_utc=INGESTED_AT,
        root=root,
    )
    assert second.content_digest_hex != first.content_digest_hex
    assert second.was_rewritten is True


# ---------------------------------------------------------------------------
# Refusals
# ---------------------------------------------------------------------------


def test_a_record_type_that_does_not_match_the_dataset_is_refused(root: Path) -> None:
    with pytest.raises(DataIntegrityError, match="TradeRecord"):
        write_records(
            (trade_at(0),),
            coordinate=SPOT_KLINES,
            source=RecordSource.ARCHIVE,
            ingested_at_utc=INGESTED_AT,
            root=root,
        )


def test_a_naive_ingested_at_is_refused(root: Path) -> None:
    with pytest.raises(DataIntegrityError, match="timezone-aware UTC"):
        write_records(
            (kline_at(0),),
            coordinate=SPOT_KLINES,
            source=RecordSource.ARCHIVE,
            ingested_at_utc=datetime(2026, 8, 4, 11, 0),
            root=root,
        )


def test_an_empty_batch_is_refused(root: Path) -> None:
    """An empty file is indistinguishable from a gap once it is on disk.

    A day with no prints is a real observation and it is recorded by the coverage
    registry (#26), not by a zero-row Parquet file that a later scan reads as "we have
    this month" (`DATA_PIPELINE.md` section 4).
    """
    with pytest.raises(DataIntegrityError, match="empty"):
        write_records(
            (),
            coordinate=SPOT_KLINES,
            source=RecordSource.ARCHIVE,
            ingested_at_utc=INGESTED_AT,
            root=root,
        )


def test_records_outside_the_coordinates_partition_are_refused(root: Path) -> None:
    """The check that stops February bars landing in the January file.

    Nothing downstream would notice: the file reads, the rows parse, and a query
    filtered on `month = '01'` silently returns February data.
    """
    with pytest.raises(DataIntegrityError, match="outside the partition"):
        write_records(
            (
                kline_at(0),
                KlineRecord(
                    **{
                        **{field: getattr(kline_at(0), field) for field in KlineRecord.__slots__},
                        "open_time_utc": datetime(2025, 2, 1, tzinfo=UTC),
                    }
                ),
            ),
            coordinate=SPOT_KLINES,
            source=RecordSource.ARCHIVE,
            ingested_at_utc=INGESTED_AT,
            root=root,
        )


def test_a_truncated_file_is_rewritten_rather_than_trusted(root: Path) -> None:
    """The one action an operator reaches for after an interrupted write is a re-run.

    A file with no Parquet footer cannot be asked for its digest. Treating that as "no
    match" makes the re-run repair it; raising instead would make the corpus
    unrecoverable by the only obvious remedy, and the message would name a footer rather
    than the interruption that caused it.
    """
    outcome = write_records(
        (kline_at(0),),
        coordinate=SPOT_KLINES,
        source=RecordSource.ARCHIVE,
        ingested_at_utc=INGESTED_AT,
        root=root,
    )
    outcome.path.write_bytes(b"PAR1 and then the process was killed")

    repaired = write_records(
        (kline_at(0),),
        coordinate=SPOT_KLINES,
        source=RecordSource.ARCHIVE,
        ingested_at_utc=INGESTED_AT,
        root=root,
    )
    assert repaired.was_rewritten is True
    assert repaired.content_digest_hex == outcome.content_digest_hex
    with read_connection() as connection:
        rows = connection.execute(
            f"SELECT count(*) FROM read_parquet('{market_dataset_glob(SPOT_KLINES, root=root)}')"
        ).fetchall()
    assert rows == [(1,)]


def test_no_staging_file_is_left_behind(root: Path) -> None:
    """The write stages to a sibling and `os.replace`s it, so a scan never sees a
    half-written file. A staging file that survived would be matched by the `**/*.parquet`
    glob on the next read."""
    outcome = write_records(
        (kline_at(0),),
        coordinate=SPOT_KLINES,
        source=RecordSource.ARCHIVE,
        ingested_at_utc=INGESTED_AT,
        root=root,
    )
    assert list(outcome.path.parent.iterdir()) == [outcome.path]


# ---------------------------------------------------------------------------
# The tables that must agree
# ---------------------------------------------------------------------------


def test_every_dataset_with_a_parser_has_a_schema_and_a_grain() -> None:
    """Four tables describe the same set of datasets and are maintained separately.

    `DECLARED_FORMATS` says a file's format is known, `_PARSERS` says its layout can be
    read, `DATASET_SCHEMAS` says its records can be stored, and `DATASET_PARTITION_GRAIN`
    says where. A dataset present in one and absent from another fails at a different
    point in the pipeline each time, so the drift is asserted rather than hoped for.
    """
    assert set(DATASET_SCHEMAS) == set(IMPLEMENTED_DATASETS)
    assert set(DATASET_PARTITION_GRAIN) == set(DATASET_SCHEMAS)


def test_every_money_typed_column_carries_a_money_suffix() -> None:
    """The suffix convention is what makes the DECIMAL(38,18) assertion total.

    A money column named without one of these suffixes is invisible to that check, so
    this is the check on the check.
    """
    misnamed = {
        (dataset.value, field.name)
        for dataset, schema in DATASET_SCHEMAS.items()
        for field in schema
        if field.type == MONEY_TYPE and not field.name.endswith(tuple(MONEY_COLUMN_SUFFIXES))
    }
    assert misnamed == set()
