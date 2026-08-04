"""What a live session writes, asserted against a real database.

Never a mock. The claims here are all claims about the store rather than about the
session: that a bar lands with `source = 'stream'`, that a replayed minute does not
become a second row, that `coverage_gap` admits the two kinds only live ingestion
produces, and that a `disconnect` gap is allowed to carry a NULL count while the
constraint still refuses a zero. Every one of those is enforced by a `CHECK`, a primary
key or a grant, and a mocked connection would be the writer answering a question about
itself (`TESTING.md`, `CLAUDE.md` section 5).

The engine is `ingest_engine`: `bar` and `coverage_gap` are `INGEST_OWNED`, so this is
also a check that the live path can write as the role it will actually run as.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest
import sqlalchemy as sa
from sqlalchemy.exc import DBAPIError
from sqlalchemy.ext.asyncio import AsyncEngine

from fking.data.backfill.registry import GapKind, RecordedGap, SeriesKey
from fking.data.format_resolver import Dataset, Market
from fking.data.live import BAR_SOURCE_STREAM, LiveBar, LiveGap, LiveMarketDataWriter
from fking.data.loaders.records import KlineRecord
from fking.platform.errors import DataUnavailableError

pytestmark = [pytest.mark.integration, pytest.mark.asyncio]

VENUE_ID = "binance-spot-testnet"
SYMBOL = "BTCUSDT"
OPEN_TIME = datetime(2026, 8, 4, 12, 0, tzinfo=UTC)

KLINE_SERIES = SeriesKey(
    market=Market.SPOT, dataset=Dataset.KLINES, symbol=SYMBOL, bar_interval="1m"
)
TRADE_SERIES = SeriesKey(
    market=Market.SPOT, dataset=Dataset.AGG_TRADES, symbol=SYMBOL, bar_interval=""
)

TWO_NEW_ROWS = 2
THREE_MISSING_PRINTS = 3


async def _seed_instrument(admin_engine: AsyncEngine, symbol: str) -> uuid.UUID:
    """Insert the reference row as the migrator, not as the ingest role.

    `venue` and `instrument` are APP_MUTABLE: `fking_ingest` holds nothing on them, and
    that is the point -- the role that writes market data cannot invent the instrument
    it is writing about. Seeding through the admin engine is how a test says so.
    """
    instrument_id = uuid.uuid4()
    async with admin_engine.begin() as connection:
        await connection.execute(
            sa.text(
                "INSERT INTO venue (venue_id, display_name) "
                "VALUES (:venue_id, 'Binance Spot Testnet') ON CONFLICT DO NOTHING"
            ),
            {"venue_id": VENUE_ID},
        )
        await connection.execute(
            sa.text(
                """
                INSERT INTO instrument (instrument_id, venue_id, symbol, market,
                                        base_asset, quote_asset, tick_size, lot_step,
                                        min_notional_quote, listed_at_utc)
                VALUES (:instrument_id, :venue_id, :symbol, 'spot', 'BTC', 'USDT',
                        0.01, 0.00001, 10, '2017-08-17T00:00:00Z')
                """
            ),
            {"instrument_id": instrument_id, "venue_id": VENUE_ID, "symbol": symbol},
        )
    return instrument_id


def _bar(minute: int) -> LiveBar:
    open_time = OPEN_TIME + timedelta(minutes=minute)
    return LiveBar(
        series=KLINE_SERIES,
        record=KlineRecord(
            open_time_utc=open_time,
            # 59.999 seconds, the way Binance files it: the last representable instant
            # inside the interval rather than the next interval's open.
            close_time_utc=open_time + timedelta(seconds=59, milliseconds=999),
            open_quote_price=Decimal("63871.48000000"),
            high_quote_price=Decimal("63871.48000000"),
            low_quote_price=Decimal("63856.38000000"),
            close_quote_price=Decimal("63865.98000000"),
            base_volume=Decimal("0.49511000"),
            quote_volume=Decimal("31619.46136530"),
            trade_count=50,
            taker_buy_base_volume=Decimal("0.19930000"),
            taker_buy_quote_volume=Decimal("12727.47258060"),
            ignored_field="",
        ),
    )


async def _bar_rows(
    engine: AsyncEngine, instrument_id: uuid.UUID
) -> list[sa.Row[tuple[object, ...]]]:
    async with engine.connect() as connection:
        return list(
            (
                await connection.execute(
                    sa.text(
                        "SELECT open_time_utc, close_quote_price, base_volume, source "
                        "FROM bar WHERE instrument_id = :instrument_id ORDER BY open_time_utc"
                    ),
                    {"instrument_id": instrument_id},
                )
            ).all()
        )


async def test_a_closed_bar_lands_with_the_stream_source_and_exact_decimals(
    engine: AsyncEngine, ingest_engine: AsyncEngine
) -> None:
    instrument_id = await _seed_instrument(engine, SYMBOL)
    writer = LiveMarketDataWriter(ingest_engine, venue_id=VENUE_ID)
    await writer.resolve_instruments([SYMBOL])

    assert await writer.write_bars([_bar(0)]) == 1

    rows = await _bar_rows(ingest_engine, instrument_id)
    assert len(rows) == 1
    assert rows[0].source == BAR_SOURCE_STREAM
    # NUMERIC(38, 18) round trip: the value that came out of the venue's own characters
    # is the value in the column, to the digit.
    assert rows[0].close_quote_price == Decimal("63865.98000000")
    assert rows[0].base_volume == Decimal("0.49511000")


async def test_a_replayed_minute_does_not_become_a_second_row(
    engine: AsyncEngine, ingest_engine: AsyncEngine
) -> None:
    """A reconnect replays the minutes overlapping the previous session. A bar is
    immutable once closed, so the second arrival is a duplicate delivery, not a
    correction -- and the count must say so."""
    instrument_id = await _seed_instrument(engine, SYMBOL)
    writer = LiveMarketDataWriter(ingest_engine, venue_id=VENUE_ID)
    await writer.resolve_instruments([SYMBOL])

    assert await writer.write_bars([_bar(0), _bar(1)]) == TWO_NEW_ROWS
    assert await writer.write_bars([_bar(1), _bar(2)]) == 1

    rows = await _bar_rows(ingest_engine, instrument_id)
    assert [row.open_time_utc for row in rows] == [
        OPEN_TIME,
        OPEN_TIME + timedelta(minutes=1),
        OPEN_TIME + timedelta(minutes=2),
    ]


async def test_a_symbol_with_no_instrument_row_is_refused_before_the_session_opens(
    engine: AsyncEngine, ingest_engine: AsyncEngine
) -> None:
    """Dropping it would be a session that looks healthy while one symbol goes nowhere."""
    await _seed_instrument(engine, SYMBOL)
    writer = LiveMarketDataWriter(ingest_engine, venue_id=VENUE_ID)

    with pytest.raises(DataUnavailableError, match="no instrument row"):
        await writer.resolve_instruments([SYMBOL, "DOGEUSDT"])


async def test_the_live_gap_kinds_are_admitted_by_the_table(
    ingest_engine: AsyncEngine,
) -> None:
    """Migration 0011 widened the CHECK; without it every live gap is a failed insert."""
    writer = LiveMarketDataWriter(ingest_engine, venue_id=VENUE_ID)
    discovered_at = datetime(2026, 8, 4, 12, 30, tzinfo=UTC)

    recorded = await writer.write_gaps(
        [
            LiveGap(
                series=TRADE_SERIES,
                gap=RecordedGap(
                    gap_start_utc=OPEN_TIME,
                    gap_end_utc=OPEN_TIME + timedelta(seconds=2),
                    gap_kind=GapKind.SEQUENCE,
                    missing_bar_count=THREE_MISSING_PRINTS,
                ),
            ),
            LiveGap(
                series=KLINE_SERIES,
                gap=RecordedGap(
                    gap_start_utc=OPEN_TIME,
                    gap_end_utc=OPEN_TIME + timedelta(milliseconds=400),
                    gap_kind=GapKind.DISCONNECT,
                    missing_bar_count=None,
                ),
            ),
        ],
        discovered_at_utc=discovered_at,
    )
    assert recorded == TWO_NEW_ROWS

    async with ingest_engine.connect() as connection:
        rows = (
            await connection.execute(
                sa.text(
                    "SELECT gap_kind, missing_bar_count, dataset FROM coverage_gap "
                    "WHERE symbol = :symbol ORDER BY gap_kind"
                ),
                {"symbol": SYMBOL},
            )
        ).all()
    assert [(row.gap_kind, row.missing_bar_count) for row in rows] == [
        ("disconnect", None),
        ("sequence", THREE_MISSING_PRINTS),
    ]


async def test_a_rediscovered_gap_is_not_counted_twice(ingest_engine: AsyncEngine) -> None:
    """`discovered_at_utc` answers which completed backtests consumed a range before
    anyone knew there was a hole in it. An upsert would move it forward until it
    answered nothing."""
    writer = LiveMarketDataWriter(ingest_engine, venue_id=VENUE_ID)
    gap = LiveGap(
        series=KLINE_SERIES,
        gap=RecordedGap(
            gap_start_utc=OPEN_TIME,
            gap_end_utc=OPEN_TIME + timedelta(minutes=1),
            gap_kind=GapKind.CADENCE,
            missing_bar_count=1,
        ),
    )
    first_seen = datetime(2026, 8, 4, 12, 30, tzinfo=UTC)

    assert await writer.write_gaps([gap], discovered_at_utc=first_seen) == 1
    assert await writer.write_gaps([gap], discovered_at_utc=first_seen + timedelta(hours=6)) == 0

    async with ingest_engine.connect() as connection:
        discovered = (
            await connection.execute(
                sa.text("SELECT discovered_at_utc FROM coverage_gap WHERE symbol = :symbol"),
                {"symbol": SYMBOL},
            )
        ).scalar_one()
    assert discovered == first_seen


async def test_a_disconnect_gap_may_not_claim_zero_missing_bars(
    ingest_engine: AsyncEngine,
) -> None:
    """NULL and 0 are different claims, and the table only admits the honest one.

    Zero would say "we checked and nothing is absent"; the truth during an outage is
    that nothing was being checked. The `CHECK` is what stops a future writer choosing
    the reassuring version.
    """
    async with ingest_engine.begin() as connection:
        with pytest.raises(DBAPIError):
            await connection.execute(
                sa.text(
                    """
                    INSERT INTO coverage_gap (market, dataset, symbol, bar_interval,
                        gap_start_utc, gap_end_utc, gap_kind, missing_bar_count,
                        discovered_at_utc)
                    VALUES ('spot', 'klines', :symbol, '1m', :start, :end,
                            'disconnect', 0, :discovered)
                    """
                ),
                {
                    "symbol": SYMBOL,
                    "start": OPEN_TIME,
                    "end": OPEN_TIME + timedelta(seconds=1),
                    "discovered": OPEN_TIME,
                },
            )


async def test_an_unknown_gap_kind_is_still_refused(ingest_engine: AsyncEngine) -> None:
    """Widening the CHECK must not have turned it into a free-text column: a typo'd kind
    would become a fourth silent category that no report groups by."""
    async with ingest_engine.begin() as connection:
        with pytest.raises(DBAPIError):
            await connection.execute(
                sa.text(
                    """
                    INSERT INTO coverage_gap (market, dataset, symbol, bar_interval,
                        gap_start_utc, gap_end_utc, gap_kind, missing_bar_count,
                        discovered_at_utc)
                    VALUES ('spot', 'klines', :symbol, '1m', :start, :end,
                            'disconnected', NULL, :discovered)
                    """
                ),
                {
                    "symbol": SYMBOL,
                    "start": OPEN_TIME,
                    "end": OPEN_TIME + timedelta(seconds=1),
                    "discovered": OPEN_TIME,
                },
            )
