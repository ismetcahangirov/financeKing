"""The market-data hypertables: no duplicate bars, compression where it belongs.

The duplicate-bar constraint is the single most valuable line in this schema. A
duplicated bar does not raise, does not look wrong in a chart, and skews every backtest
that touches the affected range -- which is the class of defect that makes a bad
strategy look excellent.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest
import sqlalchemy as sa
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncConnection, AsyncEngine

from fking.platform.persistence.schema import APPEND_ONLY_TABLES

pytestmark = [pytest.mark.integration, pytest.mark.slow]

_INSERT_BAR = sa.text(
    """
    INSERT INTO bar (instrument_id, timeframe, open_time_utc, close_time_utc,
                     open_quote_price, high_quote_price, low_quote_price,
                     close_quote_price, base_volume, quote_volume,
                     taker_buy_base_volume, taker_buy_quote_volume, trade_count, source)
    VALUES (:instrument_id, '1m', :open_time_utc, :close_time_utc,
            :open_quote_price, :high_quote_price, :low_quote_price, :close_quote_price,
            1, 60000, 0, 0, 7, 'archive')
    """
)


async def _seeded_instrument(connection: AsyncConnection) -> uuid.UUID:
    """One instrument to hang bars from. Bars carry a foreign key, so this is required."""
    instrument_id = uuid.uuid4()
    await connection.execute(
        sa.text(
            "INSERT INTO venue (venue_id, display_name) "
            "VALUES ('binance-futures-testnet', 'Binance USD-M Futures Testnet') "
            "ON CONFLICT DO NOTHING"
        )
    )
    await connection.execute(
        sa.text(
            """
            INSERT INTO instrument (instrument_id, venue_id, symbol, market, base_asset,
                                    quote_asset, tick_size, lot_step, min_notional_quote,
                                    listed_at_utc)
            VALUES (:instrument_id, 'binance-futures-testnet', :symbol, 'futures_um',
                    'BTC', 'USDT', 0.1, 0.001, 100, '2019-09-08T00:00:00Z')
            """
        ),
        {"instrument_id": instrument_id, "symbol": f"BTCUSDT{uuid.uuid4().hex[:8]}"},
    )
    return instrument_id


def _bar_parameters(instrument_id: uuid.UUID, open_time_utc: datetime) -> dict[str, object]:
    return {
        "instrument_id": instrument_id,
        "open_time_utc": open_time_utc,
        "close_time_utc": open_time_utc + timedelta(minutes=1),
        "open_quote_price": Decimal("60000.0"),
        "high_quote_price": Decimal("60100.0"),
        "low_quote_price": Decimal("59900.0"),
        "close_quote_price": Decimal("60050.0"),
    }


@pytest.mark.asyncio
async def test_a_duplicate_bar_is_rejected(engine: AsyncEngine) -> None:
    async with engine.begin() as connection:
        instrument_id = await _seeded_instrument(connection)
        parameters = _bar_parameters(instrument_id, datetime(2026, 3, 14, 12, 0, tzinfo=UTC))
        await connection.execute(_INSERT_BAR, parameters)

    async with engine.begin() as connection:
        with pytest.raises(IntegrityError, match="pk_bar"):
            await connection.execute(_INSERT_BAR, parameters)


@pytest.mark.asyncio
async def test_a_high_below_its_own_open_is_rejected(engine: AsyncEngine) -> None:
    """The cheapest data-quality gate here, and it fires on real archive corruption: a
    mis-keyed epoch unit puts a 2026 bar next to a 1970 one, and the merge that follows
    produces exactly this shape."""
    async with engine.begin() as connection:
        instrument_id = await _seeded_instrument(connection)
        parameters = _bar_parameters(instrument_id, datetime(2026, 3, 14, 13, 0, tzinfo=UTC))
        parameters["high_quote_price"] = Decimal("59000.0")

        with pytest.raises(IntegrityError, match="ohlc_brackets_open_and_close"):
            await connection.execute(_INSERT_BAR, parameters)


@pytest.mark.parametrize("table", ["bar", "funding_rate"])
@pytest.mark.asyncio
async def test_market_data_tables_are_compressed_hypertables(
    engine: AsyncEngine, table: str
) -> None:
    async with engine.connect() as connection:
        compression_enabled = await connection.scalar(
            sa.text(
                "SELECT compression_enabled FROM timescaledb_information.hypertables "
                "WHERE hypertable_name = :t"
            ),
            {"t": table},
        )
        compress_after = await connection.scalar(
            sa.text(
                "SELECT config->>'compress_after' FROM timescaledb_information.jobs "
                "WHERE hypertable_name = :t AND proc_name = 'policy_compression'"
            ),
            {"t": table},
        )

    assert compression_enabled is True
    assert compress_after == "30 days"


@pytest.mark.asyncio
async def test_bars_have_no_retention_policy(engine: AsyncEngine) -> None:
    """Deliberate, and the reason belongs in a test rather than only in a comment.

    Historical depth is the asset every walk-forward and CPCV run rests on. A retention
    policy added later would silently shorten every validation window, and the symptom
    would be a strategy that stopped validating for no visible reason.
    """
    async with engine.connect() as connection:
        retention_jobs = (
            await connection.scalars(
                sa.text(
                    "SELECT hypertable_name FROM timescaledb_information.jobs "
                    "WHERE proc_name = 'policy_retention'"
                )
            )
        ).all()
    assert list(retention_jobs) == []


@pytest.mark.asyncio
async def test_no_audit_table_is_compressed(engine: AsyncEngine) -> None:
    """Compression rewrites chunks, which is mutation of append-only data under a
    different name (`DATA_PIPELINE.md` section 6)."""
    async with engine.connect() as connection:
        compressed = (
            await connection.scalars(
                sa.text(
                    "SELECT hypertable_name FROM timescaledb_information.hypertables "
                    "WHERE compression_enabled"
                )
            )
        ).all()
    assert APPEND_ONLY_TABLES.isdisjoint(set(compressed))
