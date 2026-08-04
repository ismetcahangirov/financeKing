"""The declaration is derived from the registry, not maintained by hand.

The acceptance test for that claim is mutation: change what the ingestion registry
records, take a fresh snapshot, and the contract's answer changes with no edit anywhere in
`src/`. A hand-maintained list of what exists is a list that is wrong the first time a
backfill runs, and wrong in the optimistic direction, because nobody ever removes a symbol
from one.

Against a real database, and as the two roles that are actually subject to the grants:
`fking_ingest` writes the registry, `fking_app` reads `data_coverage` and `coverage_gap`
to build the snapshot. A mock here would prove the mock is derived from itself.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest
import sqlalchemy as sa
from sqlalchemy.ext.asyncio import AsyncEngine

from fking.data.backfill.registry import IngestRegistry
from fking.data.features.availability import AvailabilityContract, SeriesAddress
from fking.data.features.registry import registered
from fking.data.format_resolver import Dataset, Market
from fking.platform.errors import DataUnavailableError

pytestmark = [pytest.mark.integration, pytest.mark.slow]

_ADDRESS = SeriesAddress(market=Market.SPOT, symbol="BTCUSDT", dataset=Dataset.KLINES)
_KLINES = registered("trailing_return_fraction", 1)

_INSERT_PARTITION = sa.text(
    """
    INSERT INTO ingest_partition (
        market, dataset, symbol, bar_interval, period_start_date, partition_grain,
        covered_from_date, covered_through_date, archive_count, absent_archive_count,
        rows_in, rows_out, rows_rejected, first_event_time_utc, last_event_time_utc,
        content_digest_hex, parquet_path, written_at_utc
    )
    VALUES (
        'spot', 'klines', 'BTCUSDT', :bar_interval, :period_start_date, 'daily',
        :period_start_date, :period_start_date, 1, 0,
        1440, 1440, 0, :first_event_time_utc, :last_event_time_utc,
        :content_digest_hex, :parquet_path, :written_at_utc
    )
    """
)

_INSERT_GAP = sa.text(
    """
    INSERT INTO coverage_gap (
        market, dataset, symbol, bar_interval, gap_start_utc, gap_end_utc, gap_kind,
        missing_bar_count, discovered_at_utc
    )
    VALUES (
        'spot', 'klines', 'BTCUSDT', :bar_interval, :gap_start_utc, :gap_end_utc, 'cadence',
        :missing_bar_count, :discovered_at_utc
    )
    """
)


async def _record_day(engine: AsyncEngine, *, day: datetime, bar_interval: str = "1m") -> None:
    """One ingested day, spelled the way the backfill would have written it."""
    async with engine.begin() as connection:
        await connection.execute(
            _INSERT_PARTITION,
            {
                "bar_interval": bar_interval,
                "period_start_date": day.date(),
                "first_event_time_utc": day,
                "last_event_time_utc": day + timedelta(hours=23, minutes=59),
                "content_digest_hex": f"{day.date().isoformat()}-{bar_interval}",
                "parquet_path": f"market={bar_interval}/{day.date().isoformat()}.parquet",
                "written_at_utc": datetime(2026, 8, 3, tzinfo=UTC),
            },
        )


async def _record_gap(
    engine: AsyncEngine, *, gap_start_utc: datetime, gap_end_utc: datetime, missing_bars: int
) -> None:
    async with engine.begin() as connection:
        await connection.execute(
            _INSERT_GAP,
            {
                "bar_interval": "1m",
                "gap_start_utc": gap_start_utc,
                "gap_end_utc": gap_end_utc,
                "missing_bar_count": missing_bars,
                "discovered_at_utc": datetime(2026, 8, 3, tzinfo=UTC),
            },
        )


@pytest.mark.asyncio
async def test_an_empty_registry_declares_nothing_and_therefore_permits_nothing(
    app_engine: AsyncEngine,
) -> None:
    """The starting posture, and the right one.

    A corpus nobody has ingested into holds nothing, so every read is refused. The
    alternative -- an absent declaration meaning "unrestricted" -- makes the check
    strongest exactly when there is data and absent exactly when there is none.
    """
    contract = await AvailabilityContract.snapshot(IngestRegistry(app_engine))
    assert contract.declaration(_ADDRESS) is None
    with pytest.raises(DataUnavailableError, match="nothing at all"):
        contract.require(
            _KLINES,
            market=Market.SPOT,
            symbol="BTCUSDT",
            window_start_utc=datetime(2024, 1, 1, tzinfo=UTC),
            window_end_utc=datetime(2024, 1, 2, tzinfo=UTC),
        )


@pytest.mark.asyncio
async def test_ingesting_an_earlier_day_moves_the_declared_earliest_with_no_code_edit(
    app_engine: AsyncEngine, ingest_engine: AsyncEngine
) -> None:
    """The mutation test. Nothing under src/ changes between the two snapshots."""
    await _record_day(ingest_engine, day=datetime(2024, 3, 1, tzinfo=UTC))
    first = await AvailabilityContract.snapshot(IngestRegistry(app_engine))
    declared_first = first.declaration(_ADDRESS)
    assert declared_first is not None
    assert declared_first.earliest_date.isoformat() == "2024-03-01"
    assert declared_first.resolutions == ("1m",)

    await _record_day(ingest_engine, day=datetime(2024, 2, 1, tzinfo=UTC))
    second = await AvailabilityContract.snapshot(IngestRegistry(app_engine))
    declared_second = second.declaration(_ADDRESS)
    assert declared_second is not None
    assert declared_second.earliest_date.isoformat() == "2024-02-01"

    # And the older snapshot still says what it said. It is a snapshot, so a run holding
    # one is reproducible against it rather than against whatever a concurrent backfill
    # has reached.
    assert first.declaration(_ADDRESS) is declared_first


@pytest.mark.asyncio
async def test_a_second_interval_collapses_into_one_declaration(
    app_engine: AsyncEngine, ingest_engine: AsyncEngine
) -> None:
    """One declaration per (market, symbol, dataset), carrying every interval held.

    `DATA_PIPELINE.md` section 8 spells `resolution` singular while the registry keys
    klines by interval, and this is where the two are reconciled.
    """
    await _record_day(ingest_engine, day=datetime(2024, 3, 1, tzinfo=UTC), bar_interval="1m")
    await _record_day(ingest_engine, day=datetime(2024, 1, 1, tzinfo=UTC), bar_interval="1h")
    contract = await AvailabilityContract.snapshot(IngestRegistry(app_engine))

    declared = contract.declaration(_ADDRESS)
    assert declared is not None
    assert declared.resolutions == ("1h", "1m")
    assert declared.earliest_date.isoformat() == "2024-01-01"
    assert declared.describe() == "spot BTCUSDT klines (1h, 1m)"


@pytest.mark.asyncio
async def test_a_gap_recorded_by_a_backfill_starts_refusing_windows_that_cross_it(
    app_engine: AsyncEngine, ingest_engine: AsyncEngine
) -> None:
    """The second half of the mutation test, and the one that changes an answer from
    "here is a series" to "there is a hole here" without anybody editing a check."""
    for day in (datetime(2024, 3, 1, tzinfo=UTC), datetime(2024, 3, 3, tzinfo=UTC)):
        await _record_day(ingest_engine, day=day)

    window_start = datetime(2024, 3, 1, 12, tzinfo=UTC)
    window_end = datetime(2024, 3, 3, 12, tzinfo=UTC)

    permitted = await AvailabilityContract.snapshot(IngestRegistry(app_engine))
    permitted.require(
        _KLINES,
        market=Market.SPOT,
        symbol="BTCUSDT",
        window_start_utc=window_start,
        window_end_utc=window_end,
    )

    await _record_gap(
        ingest_engine,
        gap_start_utc=datetime(2024, 3, 2, tzinfo=UTC),
        gap_end_utc=datetime(2024, 3, 3, tzinfo=UTC),
        missing_bars=1440,
    )
    refusing = await AvailabilityContract.snapshot(IngestRegistry(app_engine))
    with pytest.raises(DataUnavailableError) as refused:
        refusing.require(
            _KLINES,
            market=Market.SPOT,
            symbol="BTCUSDT",
            window_start_utc=window_start,
            window_end_utc=window_end,
        )
    assert "1440 bars" in str(refused.value)
    assert "backfill the gap" in str(refused.value)
