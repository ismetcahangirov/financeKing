"""The gates that need a real store: gate 11, and "a refusal writes nothing" end to end.

Never a mock. The whole point of a standing query is that it does not trust the writer, and
a mocked connection would be the writer answering a question about itself.

The gate-11 tamper test has to break the `CHECK` constraint on purpose to reach the
condition it checks. That is not a contrived setup -- it is the exact shape of every way a
synthesised row can genuinely arrive: a migration that recreates the table without the
constraint, a restore from a dump taken before it existed, a `COPY` into a partition, or a
future migration that relaxes it for a third source. Forbidding a write and demonstrating
that none happened are different claims, and gate 11 is the second one.

The last test is the acceptance criterion stated against the two stores at once: a file
that fails a gate mid-parse leaves no Parquet path behind and leaves the `bar` hypertable's
row count exactly where it was.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path

import pytest
import sqlalchemy as sa
from sqlalchemy.ext.asyncio import AsyncConnection, AsyncEngine

from fking.data.quality import (
    Gate,
    QualityGateError,
    assert_no_synthesised_rows,
    ingest_archive,
)
from fking.data.quality.standing import count_synthesised_rows
from tests.support import corrupt_fixtures

pytestmark = [pytest.mark.integration, pytest.mark.slow]

_BAR_OPEN_TIME_UTC = datetime(2025, 1, 2, tzinfo=UTC)

_INSERT_BAR = sa.text(
    """
    INSERT INTO bar (instrument_id, timeframe, open_time_utc, close_time_utc,
                     open_quote_price, high_quote_price, low_quote_price,
                     close_quote_price, base_volume, quote_volume,
                     taker_buy_base_volume, taker_buy_quote_volume, trade_count, source)
    VALUES (:instrument_id, '1m', :open_time_utc, :close_time_utc,
            60000, 60100, 59900, 60050, 1, 60000, 0, 0, 7, :source)
    """
)


async def _seeded_instrument(connection: AsyncConnection) -> uuid.UUID:
    instrument_id = uuid.uuid4()
    await connection.execute(
        sa.text(
            "INSERT INTO venue (venue_id, display_name) "
            "VALUES ('binance-spot-testnet', 'Binance Spot Testnet') ON CONFLICT DO NOTHING"
        )
    )
    await connection.execute(
        sa.text(
            """
            INSERT INTO instrument (instrument_id, venue_id, symbol, market, base_asset,
                                    quote_asset, tick_size, lot_step, min_notional_quote,
                                    listed_at_utc)
            VALUES (:instrument_id, 'binance-spot-testnet', :symbol, 'spot',
                    'BTC', 'USDT', 0.01, 0.00001, 10, '2017-08-17T00:00:00Z')
            """
        ),
        {"instrument_id": instrument_id, "symbol": f"BTCUSDT{uuid.uuid4().hex[:8]}"},
    )
    return instrument_id


async def _insert_bar(
    connection: AsyncConnection, instrument_id: uuid.UUID, *, source: str, minute: int
) -> None:
    open_time_utc = _BAR_OPEN_TIME_UTC + timedelta(minutes=minute)
    await connection.execute(
        _INSERT_BAR,
        {
            "instrument_id": instrument_id,
            "open_time_utc": open_time_utc,
            "close_time_utc": open_time_utc + timedelta(minutes=1),
            "source": source,
        },
    )


@pytest.mark.asyncio
async def test_a_store_holding_only_archive_and_stream_rows_passes(engine: AsyncEngine) -> None:
    async with engine.begin() as connection:
        instrument_id = await _seeded_instrument(connection)
        await _insert_bar(connection, instrument_id, source="archive", minute=0)
        await _insert_bar(connection, instrument_id, source="stream", minute=1)

    async with engine.connect() as connection:
        assert await count_synthesised_rows(connection) == ()
        await assert_no_synthesised_rows(connection)


@pytest.mark.asyncio
async def test_an_interpolated_row_is_found_and_its_source_is_named(engine: AsyncEngine) -> None:
    """The constraint is the control; this is the verification. They fail independently."""
    async with engine.begin() as connection:
        instrument_id = await _seeded_instrument(connection)
        await _insert_bar(connection, instrument_id, source="archive", minute=0)
        # Exactly what a restore from a pre-constraint dump leaves behind.
        await connection.execute(sa.text("ALTER TABLE bar DROP CONSTRAINT ck_bar_source_is_known"))
        await _insert_bar(connection, instrument_id, source="interpolated", minute=1)

    async with engine.connect() as connection:
        reports = await count_synthesised_rows(connection)
        assert len(reports) == 1
        assert reports[0].table_name == "bar"
        assert reports[0].row_count == 1
        assert reports[0].observed_sources == ("interpolated",)

        with pytest.raises(QualityGateError) as refusal:
            await assert_no_synthesised_rows(connection)
        assert refusal.value.gate is Gate.NO_SYNTHESISED_ROWS
        # The message must carry the source, not only a count: "seventeen rows are
        # synthesised" starts an investigation; "seventeen rows claim 'interpolated'"
        # finishes it.
        assert "interpolated" in str(refusal.value)
        assert "re-derive the affected range" in str(refusal.value)


@pytest.mark.asyncio
async def test_the_constraint_refuses_the_row_the_gate_would_have_found(
    engine: AsyncEngine,
) -> None:
    """Both layers are checked, because a repo with only one of them looks like this one.

    If this ever starts passing an insert through, gate 11 is the only thing left standing
    and its scheduled run becomes load-bearing rather than a backstop.
    """
    async with engine.begin() as connection:
        instrument_id = await _seeded_instrument(connection)
        with pytest.raises(sa.exc.IntegrityError):
            await _insert_bar(connection, instrument_id, source="interpolated", minute=0)


@pytest.mark.asyncio
async def test_a_decimal_price_survives_the_round_trip_the_gate_reads(
    engine: AsyncEngine,
) -> None:
    """Guards the fixture rather than the gate: a float here would make the rest of this
    module assert against numbers the corpus would never hold."""
    async with engine.begin() as connection:
        instrument_id = await _seeded_instrument(connection)
        await _insert_bar(connection, instrument_id, source="archive", minute=0)

    async with engine.connect() as connection:
        stored = (
            await connection.execute(
                sa.text("SELECT close_quote_price FROM bar WHERE instrument_id = :i"),
                {"i": instrument_id},
            )
        ).scalar_one()
    assert isinstance(stored, Decimal)


@pytest.mark.asyncio
async def test_a_failed_gate_leaves_both_stores_untouched(
    engine: AsyncEngine, tmp_path: Path
) -> None:
    """Nothing partial: no Parquet path, and the hypertable count is where it was.

    The archive here fails gate 6 more than a thousand rows into the file, so the refusal
    happens after most of the work and after the point at which a write-then-verify design
    would already have produced a file. That is the ordering under test, not the arithmetic.
    """
    async with engine.begin() as connection:
        instrument_id = await _seeded_instrument(connection)
        await _insert_bar(connection, instrument_id, source="archive", minute=0)

    async with engine.connect() as connection:
        before = (await connection.execute(sa.text("SELECT count(*) FROM bar"))).scalar_one()

    corrupt = corrupt_fixtures.find("spot_klines_high_below_close")
    with pytest.raises(QualityGateError) as refusal:
        ingest_archive(
            corrupt.read(),
            corrupt.spec(),
            source=corrupt.name,
            write_root=tmp_path,
            ingested_at_utc=datetime(2026, 8, 4, 12, 0, tzinfo=UTC),
        )

    assert refusal.value.gate is Gate.OHLC_COHERENCE
    assert not list(tmp_path.rglob("*"))

    async with engine.connect() as connection:
        after = (await connection.execute(sa.text("SELECT count(*) FROM bar"))).scalar_one()
    assert after == before
