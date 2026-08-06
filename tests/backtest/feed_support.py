"""Building a Parquet corpus for the feed suite out of the recorded archives.

Every bar these tests read is a real `data.binance.vision` row: the recorded `.zip` under
`tests/fixtures/archives/` is parsed by the production loader with its own declared format
and written by the production writer. Nothing here authors a bar.

That matters more for this suite than for most. The two things the feed has to get right --
the 2025-01-01 spot microsecond cutover and the fact that a day is 1440 bars with no
promise that all of them are present -- are exactly the two things a hand-written fixture
would encode as whatever its author assumed. The spot and futures recordings for
2025-01-02 are on opposite sides of the epoch-unit split, which is what makes a mixed
spot/futures window testable at all.

**Gaps are made by omission, in the test, never by editing a recording.** `write_corpus`
takes the open times to leave out and writes the remaining rows; the file on disk is then a
genuine partition that happens to be short, which is the shape a truncated archive or an
interrupted backfill actually produces.
"""

from __future__ import annotations

from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from functools import lru_cache
from pathlib import Path
from typing import Final

from fking.backtest import MarketDataEvent
from fking.backtest.feed import FeedRequest, SeriesRequest
from fking.data.archive import ArchiveCoordinate
from fking.data.format_resolver import Dataset, Market
from fking.data.loaders import KlineRecord, extract_single_member, parse_klines
from fking.data.parquet import RecordSource, write_records
from fking.domain import Bar, Instrument, Venue
from tests.support import archive_fixtures

# The one day both corpora have a full recorded archive for, and the day that puts spot on
# the microsecond side of the 2025-01-01 cutover while futures stays on milliseconds.
RECORDED_DAY: Final[date] = date(2025, 1, 2)
DAY_START_UTC: Final[datetime] = datetime(2025, 1, 2, tzinfo=UTC)
BAR_INTERVAL: Final[str] = "1m"
MINUTE: Final[timedelta] = timedelta(minutes=1)

# Fixed rather than read from the clock: `ingested_at_utc` is provenance and a test that
# moved it would rewrite a partition it had already written.
INGESTED_AT_UTC: Final[datetime] = datetime(2026, 8, 4, 11, 0, tzinfo=UTC)

# The plausibility reference the feed is constructed with. After the recorded day and
# before anything absurd, so a raw-epoch value read under the wrong unit still lands
# outside the window.
NOW_UTC: Final[datetime] = datetime(2026, 8, 4, tzinfo=UTC)

# Binance spot testnet `exchangeInfo` filters, matching tests/support/domain_factory.
BTCUSDT_SPOT: Final[Instrument] = Instrument(
    venue=Venue.BINANCE_SPOT_TESTNET,
    symbol="BTCUSDT",
    base_asset="BTC",
    quote_asset="USDT",
    tick_size=Decimal("0.01"),
    lot_step=Decimal("0.00001"),
    min_notional_quote=Decimal("10.00"),
)
BTCUSDT_FUTURES: Final[Instrument] = Instrument(
    venue=Venue.BINANCE_FUTURES_TESTNET,
    symbol="BTCUSDT",
    base_asset="BTC",
    quote_asset="USDT",
    tick_size=Decimal("0.10"),
    lot_step=Decimal("0.001"),
    min_notional_quote=Decimal("5.00"),
)

INSTRUMENTS: Final[dict[Market, Instrument]] = {
    Market.SPOT: BTCUSDT_SPOT,
    Market.FUTURES_UM: BTCUSDT_FUTURES,
}


@dataclass(frozen=True, slots=True)
class WrittenCorpus:
    """A corpus root and exactly the records that were written into it."""

    root: Path
    records: tuple[KlineRecord, ...]

    @property
    def open_times(self) -> tuple[datetime, ...]:
        return tuple(record.open_time_utc for record in self.records)


@lru_cache(maxsize=4)
def recorded_klines(market: Market) -> tuple[KlineRecord, ...]:
    """Every bar in the recorded whole-day archive for `market`, parsed by the loader.

    Cached because parsing 1,440 rows per test is the only slow thing in this suite and the
    result is a tuple of frozen records, so no test can reach another's copy.
    """
    recorded = archive_fixtures.find(
        market=market, dataset=Dataset.KLINES, archive_date=RECORDED_DAY, whole=True
    )
    member = extract_single_member(recorded.read(), source=recorded.label)
    bars, outcome = parse_klines(member, recorded.spec(), source=recorded.label)
    if outcome.rows_rejected:
        raise AssertionError(
            f"{recorded.label} produced {outcome.rows_rejected} rejected rows; the fixture "
            f"corpus must be built from a clean parse or its bar count means nothing"
        )
    return bars


def write_corpus(
    root: Path,
    *,
    market: Market = Market.SPOT,
    omit_open_times: Iterable[datetime] = (),
    records: Sequence[KlineRecord] | None = None,
) -> WrittenCorpus:
    """Write the recorded day into `root` as a Parquet partition, minus `omit_open_times`.

    `records` overrides the source entirely, for the tests that need a corpus the archive
    does not contain -- a duplicated open time, a bar off the lattice. Everything else goes
    through the recording.
    """
    omitted = frozenset(omit_open_times)
    source = recorded_klines(market) if records is None else tuple(records)
    kept = tuple(record for record in source if record.open_time_utc not in omitted)
    write_records(
        kept,
        coordinate=ArchiveCoordinate(
            market=market,
            dataset=Dataset.KLINES,
            symbol="BTCUSDT",
            archive_date=RECORDED_DAY,
            interval=BAR_INTERVAL,
        ),
        source=RecordSource.ARCHIVE,
        ingested_at_utc=INGESTED_AT_UTC,
        root=root,
    )
    return WrittenCorpus(root=root, records=kept)


def request_for(
    *,
    exposed_minute: int,
    until_minute: int,
    warmup_bar_count: int,
    markets: Sequence[Market] = (Market.SPOT,),
) -> FeedRequest:
    """A request over the recorded day, stated in minute offsets into it."""
    return FeedRequest(
        series=tuple(
            SeriesRequest(market=market, instrument=INSTRUMENTS[market]) for market in markets
        ),
        bar_interval=BAR_INTERVAL,
        exposed_from_utc=DAY_START_UTC + MINUTE * exposed_minute,
        until_utc=DAY_START_UTC + MINUTE * until_minute,
        warmup_bar_count=warmup_bar_count,
    )


def bar_of(event: MarketDataEvent) -> Bar:
    """The `Bar` inside a market-data event.

    `MarketDataEvent.observation` is `Bar | Tick` because the loop carries both, and a
    test that reached for `.open_time_utc` through the union would be asserting on a
    narrowing the type checker cannot see. The feed emits only bars today, and this is
    the one place that says so.
    """
    assert isinstance(event.observation, Bar), (
        f"the feed emitted a {type(event.observation).__name__}; it produces bars"
    )
    return event.observation


def minutes(*offsets: int) -> tuple[datetime, ...]:
    """Open times at the given minute offsets into the recorded day."""
    return tuple(DAY_START_UTC + MINUTE * offset for offset in offsets)


def config_toml(
    *,
    corpus_root: Path,
    exposed_from_utc: datetime,
    until_utc: datetime,
    warmup_bar_count: int,
    market: Market = Market.SPOT,
) -> str:
    """A backtest configuration file body for the CLI tests.

    Decimals are quoted, which is the file-level half of the Decimal-from-str rule: TOML
    has a float type, and `tick_size = 0.01` would arrive already rounded.
    """
    instrument = INSTRUMENTS[market]
    return "\n".join(
        [
            f'corpus_root = "{corpus_root.as_posix()}"',
            f'bar_interval = "{BAR_INTERVAL}"',
            f'exposed_from_utc = "{exposed_from_utc.isoformat()}"',
            f'until_utc = "{until_utc.isoformat()}"',
            f"warmup_bar_count = {warmup_bar_count}",
            f'now_utc = "{NOW_UTC.isoformat()}"',
            "",
            "[[series]]",
            f'market = "{market.value}"',
            f'venue = "{instrument.venue.value}"',
            f'symbol = "{instrument.symbol}"',
            f'base_asset = "{instrument.base_asset}"',
            f'quote_asset = "{instrument.quote_asset}"',
            f'tick_size = "{instrument.tick_size}"',
            f'lot_step = "{instrument.lot_step}"',
            f'min_notional_quote = "{instrument.min_notional_quote}"',
            "",
        ]
    )
