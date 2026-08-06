"""Epoch units are resolved per `(market, date)`, and an ambiguous pair raises.

Binance spot archives became microsecond epochs on 2025-01-01. USDⓈ-M futures archives
stayed on milliseconds, then and now (VF-015, `docs/adr/0013`). A run over both legs
therefore reads two units, and a single divisor applied across it misplaces one leg by a
factor of a thousand -- which puts a 2025 bar in 1970 or in the year 56,000 for that leg
while the other leg is correct, and produces a backtest that looks either brilliant or
broken depending on which way it went.

The recorded fixtures make that testable rather than notional: `tests/fixtures/archives/`
holds a whole 2025-01-02 day for *both* corpora, which are on opposite sides of the split.

The synthetic partitions below are the other half. Our own writer normalises timestamps at
ingest, so a corpus this project produced never exercises the divisor -- which is precisely
why a file it did *not* produce must not be read on a guess. Those files are built here with
pyarrow rather than committed, so the mutation is visible in the diff that asserts on it.
"""

from __future__ import annotations

from collections.abc import Sequence
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from pathlib import Path

import pyarrow as pa
import pyarrow.parquet as pq
import pytest

from fking.backtest.feed import (
    AmbiguousEpochUnitError,
    CorpusIntegrityError,
    FeedRequest,
    MarketDataFeed,
    SeriesRequest,
    resolve_partition_epoch_unit,
)
from fking.data import format_resolver
from fking.data.format_resolver import (
    SPOT_ARCHIVE_GENESIS,
    ArchiveFormat,
    Dataset,
    EpochUnit,
    Market,
    TimestampEncoding,
)
from fking.platform.errors import DataIntegrityError
from tests.backtest import feed_support as fs

pytestmark = pytest.mark.unit

MILLISECOND_DIVISOR = 1_000
MICROSECOND_DIVISOR = 1_000_000


def _raw_epoch_schema(*, money: pa.DataType, instant: pa.DataType) -> pa.Schema:
    """The canonical kline schema with the two instant columns left as raw epochs.

    What a partition staged by an older tool, or by a hand-run export, looks like: every
    other column is right, and the one thing that decides where the bars sit in time is a
    bare integer whose unit is nowhere in the file.
    """
    fields: list[pa.Field[pa.DataType]] = [
        pa.field("open_time_utc", instant, nullable=False),
        pa.field("close_time_utc", instant, nullable=False),
        pa.field("open_quote_price", money, nullable=False),
        pa.field("high_quote_price", money, nullable=False),
        pa.field("low_quote_price", money, nullable=False),
        pa.field("close_quote_price", money, nullable=False),
        pa.field("base_volume", money, nullable=False),
        pa.field("quote_volume", money, nullable=False),
        pa.field("trade_count", pa.int64(), nullable=False),
        pa.field("taker_buy_base_volume", money, nullable=False),
        pa.field("taker_buy_quote_volume", money, nullable=False),
        pa.field("source", pa.string(), nullable=False),
        pa.field("ingested_at_utc", pa.timestamp("us", tz="UTC"), nullable=False),
    ]
    return pa.schema(fields)


def write_raw_epoch_partition(  # noqa: PLR0913 -- one parameter per way a foreign file differs
    root: Path,
    *,
    market: Market,
    opens_utc: Sequence[datetime],
    divisor: int,
    archive_month: date,
    money_type: pa.DataType | None = None,
    instant_type: pa.DataType | None = None,
) -> None:
    """Write a partition whose instant columns are integer epochs in `divisor` units.

    `money_type` and `instant_type` override the canonical column types, so a test can
    stage the two files the declared schema exists to make impossible: OHLCV as `double`,
    and a timestamp with no zone.
    """
    partition = (
        root
        / f"market={market.value}"
        / "dataset=klines"
        / "symbol=BTCUSDT"
        / "interval=1m"
        / f"year={archive_month.year:04d}"
        / f"month={archive_month.month:02d}"
    )
    partition.mkdir(parents=True, exist_ok=True)
    resolved_money = pa.decimal128(38, 18) if money_type is None else money_type
    resolved_instant = pa.int64() if instant_type is None else instant_type
    money = _money("95000", len(opens_utc), resolved_money)
    table = pa.table(
        {
            "open_time_utc": _instants(opens_utc, divisor=divisor, instant_type=resolved_instant),
            "close_time_utc": _instants(
                [moment + timedelta(minutes=1, microseconds=-1) for moment in opens_utc],
                divisor=divisor,
                instant_type=resolved_instant,
            ),
            "open_quote_price": money,
            "high_quote_price": money,
            "low_quote_price": money,
            "close_quote_price": money,
            "base_volume": _money("1", len(opens_utc), resolved_money),
            "quote_volume": _money("1", len(opens_utc), resolved_money),
            "trade_count": [1] * len(opens_utc),
            "taker_buy_base_volume": _money("0.5", len(opens_utc), resolved_money),
            "taker_buy_quote_volume": _money("0.5", len(opens_utc), resolved_money),
            "source": ["archive"] * len(opens_utc),
            "ingested_at_utc": [fs.INGESTED_AT_UTC] * len(opens_utc),
        },
        schema=_raw_epoch_schema(money=resolved_money, instant=resolved_instant),
    )
    pq.write_table(
        table, partition / f"part-{archive_month.year:04d}-{archive_month.month:02d}.parquet"
    )


def _money(quantity: str, row_count: int, money_type: pa.DataType) -> list[object]:
    """Column values in whatever type the file is being staged with.

    pyarrow refuses to convert a `Decimal` into a `double`, which is itself the point:
    a partition that holds OHLCV as `double` was written from floats, so that is what
    the fixture has to hand it.
    """
    if pa.types.is_floating(money_type):
        return [float(quantity)] * row_count
    return [Decimal(quantity)] * row_count


def _instants(
    opens_utc: Sequence[datetime], *, divisor: int, instant_type: pa.DataType
) -> list[object]:
    """Raw epochs for an integer column, or the datetimes themselves for a timestamp one."""
    if pa.types.is_integer(instant_type):
        return [int(moment.timestamp()) * divisor for moment in opens_utc]
    return [moment.replace(tzinfo=None) for moment in opens_utc]


def _feed(root: Path) -> MarketDataFeed:
    return MarketDataFeed(corpus_root=root, now_utc=fs.NOW_UTC)


def _request(*, market: Market, from_utc: datetime, bar_count: int) -> FeedRequest:
    return FeedRequest(
        series=(SeriesRequest(market=market, instrument=fs.INSTRUMENTS[market]),),
        bar_interval="1m",
        exposed_from_utc=from_utc,
        until_utc=from_utc + timedelta(minutes=bar_count),
        warmup_bar_count=0,
    )


# ---------------------------------------------------------------------------
# The declared table
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("market", "year", "month", "expected"),
    [
        (Market.SPOT, 2024, 12, EpochUnit.MILLISECONDS),
        (Market.SPOT, 2025, 1, EpochUnit.MICROSECONDS),
        (Market.FUTURES_UM, 2024, 12, EpochUnit.MILLISECONDS),
        (Market.FUTURES_UM, 2025, 1, EpochUnit.MILLISECONDS),
    ],
)
def test_the_unit_is_a_property_of_market_and_month(
    market: Market, year: int, month: int, expected: EpochUnit
) -> None:
    """Spot crosses on 2025-01-01 and futures never does. One divisor for the process
    would be wrong on one side of that table whichever value it held."""
    assert resolve_partition_epoch_unit(market=market, year=year, month=month) is expected


@pytest.mark.parametrize(
    ("market", "year", "month"),
    [
        (Market.SPOT, 2016, 12),  # before the spot corpus exists at all
        (Market.FUTURES_UM, 2018, 5),  # before USDⓈ-M futures launched
    ],
)
def test_an_undeclared_month_raises_rather_than_borrowing_a_divisor(
    market: Market, year: int, month: int
) -> None:
    with pytest.raises(AmbiguousEpochUnitError, match="no archive format is declared"):
        resolve_partition_epoch_unit(market=market, year=year, month=month)


def test_a_month_split_between_two_encodings_raises(monkeypatch: pytest.MonkeyPatch) -> None:
    """The check that earns its keep only after the next cutover.

    Today's one cutover falls on a month boundary, so the two ends of every month agree and
    this branch never fires against the real table. The next one is under no obligation to,
    and a monthly partition read under either of two units is wrong for half its rows with
    nothing downstream able to notice -- so the table is replaced here with one whose
    segments split mid-month, which is the shape the check exists for.
    """
    mid_month = date(2025, 1, 15)
    split = {
        (Market.SPOT, Dataset.KLINES): (
            ArchiveFormat(
                market=Market.SPOT,
                dataset=Dataset.KLINES,
                timestamp_encoding=TimestampEncoding.EPOCH_MILLISECONDS,
                has_header_row=False,
                boolean_encoding=None,
                boolean_columns=(),
                declared_from_date=SPOT_ARCHIVE_GENESIS,
                declared_until_date=mid_month,
            ),
            ArchiveFormat(
                market=Market.SPOT,
                dataset=Dataset.KLINES,
                timestamp_encoding=TimestampEncoding.EPOCH_MICROSECONDS,
                has_header_row=False,
                boolean_encoding=None,
                boolean_columns=(),
                declared_from_date=mid_month,
                declared_until_date=None,
            ),
        )
    }
    monkeypatch.setattr(format_resolver, "DECLARED_FORMATS", split)

    with pytest.raises(AmbiguousEpochUnitError, match="spans two declared timestamp encodings"):
        resolve_partition_epoch_unit(market=Market.SPOT, year=2025, month=1)


def test_a_kline_month_declared_as_a_datetime_string_has_no_unit_to_read(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The third refusal: a declaration that is not an epoch at all.

    The USDⓈ-M `metrics` archive already stamps a naive datetime string rather than an
    epoch (VF-029), so the encoding exists in the declared vocabulary and klines could
    acquire it. A reader that fell back to a divisor for it would be dividing a value
    that was never a number.
    """
    naive = {
        (Market.SPOT, Dataset.KLINES): (
            ArchiveFormat(
                market=Market.SPOT,
                dataset=Dataset.KLINES,
                timestamp_encoding=TimestampEncoding.NAIVE_UTC_DATETIME,
                has_header_row=False,
                boolean_encoding=None,
                boolean_columns=(),
                declared_from_date=SPOT_ARCHIVE_GENESIS,
                declared_until_date=None,
            ),
        )
    }
    monkeypatch.setattr(format_resolver, "DECLARED_FORMATS", naive)

    with pytest.raises(AmbiguousEpochUnitError, match="which is not an epoch"):
        resolve_partition_epoch_unit(market=Market.SPOT, year=2025, month=1)


# ---------------------------------------------------------------------------
# The read path
# ---------------------------------------------------------------------------


def test_a_mixed_spot_futures_window_reports_a_unit_per_leg(tmp_path: Path) -> None:
    """The acceptance criterion. Both legs come from real recordings on opposite sides of
    the 2025-01-01 split, and the report says which unit each was read in."""
    fs.write_corpus(tmp_path, market=Market.SPOT)
    fs.write_corpus(tmp_path, market=Market.FUTURES_UM)

    report = _feed(tmp_path).coverage(
        fs.request_for(
            exposed_minute=20,
            until_minute=60,
            warmup_bar_count=20,
            markets=(Market.SPOT, Market.FUTURES_UM),
        )
    )

    units = {
        entry.label: [declared.epoch_unit for declared in entry.partition_formats]
        for entry in report.series
    }
    assert units == {
        "spot/BTCUSDT": [EpochUnit.MICROSECONDS],
        "futures_um/BTCUSDT": [EpochUnit.MILLISECONDS],
    }
    assert "2025-01=us" in report.render()
    assert "2025-01=ms" in report.render()


def test_both_legs_of_a_mixed_window_land_on_the_same_instants(tmp_path: Path) -> None:
    """Two units, one timeline. If the divisor were shared, one leg's bars would sit a
    thousand-fold away from the other's and the merge would silently interleave nothing."""
    fs.write_corpus(tmp_path, market=Market.SPOT)
    fs.write_corpus(tmp_path, market=Market.FUTURES_UM)

    loaded = _feed(tmp_path).load(
        fs.request_for(
            exposed_minute=20,
            until_minute=40,
            warmup_bar_count=0,
            markets=(Market.SPOT, Market.FUTURES_UM),
        )
    )

    opens = [fs.bar_of(event).open_time_utc for event in loaded.events]
    assert sorted(set(opens)) == list(fs.minutes(*range(20, 40)))
    # Twenty instants, two series: the merge interleaves rather than replacing one with
    # the other.
    assert len(opens) == 2 * len(set(opens))


def test_a_raw_epoch_partition_is_normalised_with_its_own_declared_unit(
    tmp_path: Path,
) -> None:
    """A file this project did not write, in a declared month, read under the declared unit.

    Futures klines are milliseconds; the file below is milliseconds; the bars land where
    they should. Without this the refusals above would be indistinguishable from a reader
    that simply refuses every integer column.
    """
    from_utc = datetime(2025, 1, 2, 3, 0, tzinfo=UTC)
    write_raw_epoch_partition(
        tmp_path,
        market=Market.FUTURES_UM,
        opens_utc=[from_utc + timedelta(minutes=index) for index in range(5)],
        divisor=MILLISECOND_DIVISOR,
        archive_month=date(2025, 1, 1),
    )

    loaded = _feed(tmp_path).load(
        _request(market=Market.FUTURES_UM, from_utc=from_utc, bar_count=5)
    )

    assert [fs.bar_of(event).open_time_utc for event in loaded.events] == [
        from_utc + timedelta(minutes=index) for index in range(5)
    ]


def test_a_raw_epoch_partition_in_an_undeclared_month_raises(tmp_path: Path) -> None:
    """The acceptance criterion's other half: ambiguous raises, it is not divided by a
    default.

    USDⓈ-M futures launched on 2019-09-08, so a 2018 partition is a file whose unit nobody
    has ever verified. Reading it under the neighbouring segment's divisor would be a guess
    dressed as a fact, and the corpus would acquire a region whose timestamps are
    confidently wrong.
    """
    from_utc = datetime(2018, 5, 2, 3, 0, tzinfo=UTC)
    write_raw_epoch_partition(
        tmp_path,
        market=Market.FUTURES_UM,
        opens_utc=[from_utc + timedelta(minutes=index) for index in range(5)],
        divisor=MILLISECOND_DIVISOR,
        archive_month=date(2018, 5, 1),
    )

    with pytest.raises(AmbiguousEpochUnitError, match="no archive format is declared"):
        _feed(tmp_path).coverage(_request(market=Market.FUTURES_UM, from_utc=from_utc, bar_count=5))


def test_a_raw_epoch_in_the_wrong_unit_trips_the_plausibility_window(tmp_path: Path) -> None:
    """The declaration is right, the file is not, and the magnitude check catches it.

    Spot 2025-01 is declared microseconds. The file below holds milliseconds, so read under
    the declaration every bar lands in 1970 -- and a run that accepted them would report an
    empty window rather than a corrupt one, because nothing in 1970 is inside any window
    anybody asks for. The refusal comes from `fking.data.format_resolver` and is deliberately
    not relabelled here: its message already names the value, the unit and the window, and
    anything this package added would be vaguer.
    """
    from_utc = datetime(2025, 1, 2, 3, 0, tzinfo=UTC)
    write_raw_epoch_partition(
        tmp_path,
        market=Market.SPOT,
        opens_utc=[from_utc + timedelta(minutes=index) for index in range(5)],
        divisor=MILLISECOND_DIVISOR,
        archive_month=date(2025, 1, 1),
    )

    with pytest.raises(DataIntegrityError, match="outside the plausible window"):
        _feed(tmp_path).coverage(_request(market=Market.SPOT, from_utc=from_utc, bar_count=5))


def test_the_microsecond_declaration_is_what_makes_a_spot_raw_epoch_readable(
    tmp_path: Path,
) -> None:
    """The positive control for the test above: the same shape of file, in the unit spot
    actually declares, reads correctly."""
    from_utc = datetime(2025, 1, 2, 3, 0, tzinfo=UTC)
    write_raw_epoch_partition(
        tmp_path,
        market=Market.SPOT,
        opens_utc=[from_utc + timedelta(minutes=index) for index in range(5)],
        divisor=MICROSECOND_DIVISOR,
        archive_month=date(2025, 1, 1),
    )

    loaded = _feed(tmp_path).load(_request(market=Market.SPOT, from_utc=from_utc, bar_count=5))

    assert [fs.bar_of(event).open_time_utc for event in loaded.events] == [
        from_utc + timedelta(minutes=index) for index in range(5)
    ]


# ---------------------------------------------------------------------------
# Column types
# ---------------------------------------------------------------------------


def test_an_ohlcv_column_written_as_a_double_is_refused(tmp_path: Path) -> None:
    """The failure the declared `decimal128(38, 18)` schema exists to prevent.

    Parquet stores a `Decimal` as `double` without complaint if asked, and every value a
    test happens to choose round-trips. What does not is the eighteenth decimal on the ones
    production produces -- and by the time a float reaches a `Bar` the rounding predates
    anything this codebase can do about it.
    """
    from_utc = datetime(2025, 1, 2, 3, 0, tzinfo=UTC)
    write_raw_epoch_partition(
        tmp_path,
        market=Market.FUTURES_UM,
        opens_utc=[from_utc + timedelta(minutes=index) for index in range(3)],
        divisor=MILLISECOND_DIVISOR,
        archive_month=date(2025, 1, 1),
        money_type=pa.float64(),
    )

    with pytest.raises(CorpusIntegrityError, match="not a Decimal"):
        _feed(tmp_path).coverage(_request(market=Market.FUTURES_UM, from_utc=from_utc, bar_count=3))


def test_a_timestamp_column_with_no_zone_is_refused_rather_than_assumed_utc(
    tmp_path: Path,
) -> None:
    """A naive column is a partition written by something other than this project's writer.

    Reading it as UTC would launder whatever zone produced it into a confident value, and
    the bars would sit a whole offset away from the ones they are merged with -- silently,
    because every comparison between two naive instants still succeeds.
    """
    from_utc = datetime(2025, 1, 2, 3, 0, tzinfo=UTC)
    write_raw_epoch_partition(
        tmp_path,
        market=Market.FUTURES_UM,
        opens_utc=[from_utc + timedelta(minutes=index) for index in range(3)],
        divisor=MILLISECOND_DIVISOR,
        archive_month=date(2025, 1, 1),
        instant_type=pa.timestamp("us"),
    )

    with pytest.raises(CorpusIntegrityError, match="not aware UTC"):
        _feed(tmp_path).coverage(_request(market=Market.FUTURES_UM, from_utc=from_utc, bar_count=3))
