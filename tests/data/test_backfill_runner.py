"""The backfill end to end: a real database, a stub host, and the corpus it produces.

Never a mocked database. The properties under test here are `ON CONFLICT` semantics, a
composite primary key doing the deduplication, and a view aggregating across three tables --
and a mocked connection would be the writer answering a question about itself
(`CLAUDE.md` section 5).

The archives are the recorded spot kline day shifted onto consecutive dates, with one date
deliberately left unserved. That fabricated hole is the acceptance criterion: it must be
registered as a gap with exact bounds, and a scan of the corpus over that window must return
zero rows -- not an interpolated bar, not a forward fill, nothing.
"""

from __future__ import annotations

import dataclasses
from datetime import UTC, date, datetime
from pathlib import Path
from typing import Final

import pyarrow.parquet as pq
import pytest
import sqlalchemy as sa
from sqlalchemy.ext.asyncio import AsyncEngine

from fking.data.archive import ArchiveCoordinate, ArchiveFetcher
from fking.data.backfill import BackfillRequest, IngestRegistry, run_backfill
from fking.data.backfill.report import BackfillReport
from fking.data.format_resolver import Dataset, Market
from fking.data.parquet import (
    CONTENT_DIGEST_KEY,
    market_dataset_glob,
    partition_path,
    read_connection,
)
from fking.platform.errors import DataIntegrityError
from tests.support import archive_fixtures
from tests.support.archive_stub import (
    InterruptedRunError,
    StubArchiveEgress,
    daily_archives,
    days_in,
    member_of,
)

pytestmark = [pytest.mark.integration, pytest.mark.slow]

RECORDED: Final = archive_fixtures.find(
    market=Market.SPOT, dataset=Dataset.KLINES, archive_date=date(2025, 1, 2), whole=True
)

# A short, exactly-known window. today_utc sits inside January so `resolve_granularity`
# resolves the month to daily archives -- the recent-tail case, where a month is fed by many
# files and the partition-level gap detector is the only thing that can see a missing day.
TODAY: Final[date] = date(2025, 1, 8)
NOW: Final[datetime] = datetime(2025, 1, 8, 6, 0, tzinfo=UTC)
FIRST_DAY: Final[date] = date(2025, 1, 2)
THROUGH: Final[date] = date(2025, 1, 6)
MISSING_DAY: Final[date] = date(2025, 1, 5)
JANUARY: Final[date] = date(2025, 1, 1)

MINUTES_PER_DAY: Final[int] = 1440
SERVED_DAY_COUNT: Final[int] = 4
COMPLETE_DAY_COUNT: Final[int] = 5


def _partition() -> ArchiveCoordinate:
    return ArchiveCoordinate(
        market=Market.SPOT,
        dataset=Dataset.KLINES,
        symbol=RECORDED.symbol,
        archive_date=JANUARY,
        interval=RECORDED.interval,
    )


def _request(write_root: Path) -> BackfillRequest:
    return BackfillRequest(
        market=Market.SPOT,
        dataset=Dataset.KLINES,
        symbols=(RECORDED.symbol,),
        interval=RECORDED.interval,
        through_date=THROUGH,
        today_utc=TODAY,
        now_utc=NOW,
        history_floor_date=JANUARY,
        write_root=write_root,
    )


def _stub(*, days: tuple[date, ...]) -> StubArchiveEgress:
    return StubArchiveEgress(
        daily_archives(
            member=member_of(RECORDED.read()), coordinate=RECORDED.coordinate(), days=days
        )
    )


async def _run(
    egress: StubArchiveEgress, engine: AsyncEngine, write_root: Path, cache_root: Path
) -> BackfillReport:
    return await run_backfill(
        _request(write_root),
        fetcher=ArchiveFetcher(egress=egress, cache_root=cache_root),
        egress=egress,
        registry=IngestRegistry(engine),
    )


async def _scalar(engine: AsyncEngine, statement: str) -> int:
    async with engine.connect() as connection:
        row = (await connection.execute(sa.text(statement))).first()
    assert row is not None
    return int(row[0])


@pytest.mark.asyncio
async def test_a_run_writes_one_partition_and_reports_what_it_read(
    engine: AsyncEngine, tmp_path: Path
) -> None:
    served = tuple(day for day in days_in(FIRST_DAY, THROUGH) if day != MISSING_DAY)
    report = await _run(_stub(days=served), engine, tmp_path / "parquet", tmp_path / "archive")

    symbol = report.symbols[0]
    assert symbol.earliest_archive_date == FIRST_DAY
    assert symbol.partitions_written == 1
    assert symbol.archives_ingested == SERVED_DAY_COUNT
    assert symbol.archives_absent == 1
    assert symbol.rows_out == SERVED_DAY_COUNT * MINUTES_PER_DAY
    assert symbol.rows_rejected == 0
    assert report.rows_in == report.rows_out + report.rows_rejected
    assert partition_path(_partition(), root=tmp_path / "parquet").is_file()


@pytest.mark.asyncio
async def test_the_run_summary_states_the_shortest_usable_history(
    engine: AsyncEngine, tmp_path: Path
) -> None:
    """The sentence a researcher forgets: a hypothesis inherits its shortest input."""
    served = tuple(day for day in days_in(FIRST_DAY, THROUGH) if day != MISSING_DAY)
    report = await _run(_stub(days=served), engine, tmp_path / "parquet", tmp_path / "archive")

    rendered = report.render()
    assert "inherits the shortest history among" in rendered
    assert report.shortest_history_start == datetime(2025, 1, 2, tzinfo=UTC)
    assert "rejected 0 (none)" in rendered


@pytest.mark.asyncio
async def test_a_fabricated_missing_day_becomes_a_gap_with_exact_bounds(
    engine: AsyncEngine, tmp_path: Path
) -> None:
    served = tuple(day for day in days_in(FIRST_DAY, THROUGH) if day != MISSING_DAY)
    await _run(_stub(days=served), engine, tmp_path / "parquet", tmp_path / "archive")

    async with engine.connect() as connection:
        rows = (
            await connection.execute(
                sa.text(
                    "SELECT gap_start_utc, gap_end_utc, gap_kind, missing_bar_count, "
                    "       discovered_at_utc "
                    "FROM coverage_gap ORDER BY gap_start_utc"
                )
            )
        ).all()

    assert len(rows) == 1
    assert rows[0].gap_start_utc == datetime(2025, 1, 5, tzinfo=UTC)
    assert rows[0].gap_end_utc == datetime(2025, 1, 6, tzinfo=UTC)
    assert rows[0].gap_kind == "cadence"
    assert rows[0].missing_bar_count == MINUTES_PER_DAY
    # The column an escalation is keyed on: which completed backtests consumed this range
    # before anybody knew there was a hole in it.
    assert rows[0].discovered_at_utc == NOW


@pytest.mark.asyncio
async def test_no_bar_is_written_inside_the_gap(engine: AsyncEngine, tmp_path: Path) -> None:
    """The interpolation ban, asserted against the corpus rather than against the writer."""
    served = tuple(day for day in days_in(FIRST_DAY, THROUGH) if day != MISSING_DAY)
    write_root = tmp_path / "parquet"
    await _run(_stub(days=served), engine, write_root, tmp_path / "archive")

    glob_sql = market_dataset_glob(_partition(), root=write_root)
    with read_connection() as connection:
        inside_gap = connection.execute(
            f"SELECT count(*) FROM read_parquet('{glob_sql}', hive_partitioning = true) "  # noqa: S608
            f"WHERE open_time_utc >= TIMESTAMPTZ '2025-01-05 00:00:00+00' "
            f"AND open_time_utc < TIMESTAMPTZ '2025-01-06 00:00:00+00'"
        ).fetchone()
        total = connection.execute(
            f"SELECT count(*) FROM read_parquet('{glob_sql}', hive_partitioning = true)"  # noqa: S608
        ).fetchone()

    assert inside_gap is not None
    assert inside_gap[0] == 0
    assert total is not None
    assert total[0] == SERVED_DAY_COUNT * MINUTES_PER_DAY


@pytest.mark.asyncio
async def test_an_interrupted_run_leaves_no_partial_partition(
    engine: AsyncEngine, tmp_path: Path
) -> None:
    """The write is the last statement, so a kill mid-partition leaves nothing behind."""
    served = days_in(FIRST_DAY, THROUGH)
    egress = _stub(days=served)
    egress.interrupt_after(2)

    with pytest.raises(InterruptedRunError):
        await _run(egress, engine, tmp_path / "parquet", tmp_path / "archive")

    assert not partition_path(_partition(), root=tmp_path / "parquet").exists()
    assert await _scalar(engine, "SELECT count(*) FROM ingest_partition") == 0


@pytest.mark.asyncio
async def test_resuming_after_an_interruption_is_byte_identical_and_adds_no_rows(
    engine: AsyncEngine, tmp_path: Path
) -> None:
    """Kill it, re-run it, run it again: same digest, same row counts, no duplicates."""
    served = days_in(FIRST_DAY, THROUGH)
    write_root, cache_root = tmp_path / "parquet", tmp_path / "archive"

    interrupted = _stub(days=served)
    interrupted.interrupt_after(3)
    with pytest.raises(InterruptedRunError):
        await _run(interrupted, engine, write_root, cache_root)

    completed = await _run(_stub(days=served), engine, write_root, cache_root)
    digest_after_resume = _digest_of(write_root)
    partition_rows = await _scalar(engine, "SELECT count(*) FROM ingest_partition")
    file_rows = await _scalar(engine, "SELECT count(*) FROM ingest_file")

    third = _stub(days=served)
    again = await _run(third, engine, write_root, cache_root)

    assert completed.symbols[0].partitions_written == 1
    assert completed.symbols[0].archives_ingested == COMPLETE_DAY_COUNT
    assert _digest_of(write_root) == digest_after_resume
    assert again.symbols[0].partitions_resumed == 1
    assert again.symbols[0].partitions_written == 0
    # Resume is the point: a complete partition costs no download at all on the next run.
    assert third.download_count == 0
    assert await _scalar(engine, "SELECT count(*) FROM ingest_partition") == partition_rows
    assert await _scalar(engine, "SELECT count(*) FROM ingest_file") == file_rows


@pytest.mark.asyncio
async def test_a_partition_that_met_an_absent_archive_is_probed_again(
    engine: AsyncEngine, tmp_path: Path
) -> None:
    """Absence is a claim about upstream, and upstream publishes a missing day sometimes.

    So the partition is not marked complete, the next run asks again, and the day that was
    published in the meantime lands. Caching the 404 would make the fix invisible forever.
    """
    write_root, cache_root = tmp_path / "parquet", tmp_path / "archive"
    served = tuple(day for day in days_in(FIRST_DAY, THROUGH) if day != MISSING_DAY)
    await _run(_stub(days=served), engine, write_root, cache_root)

    healed = await _run(_stub(days=days_in(FIRST_DAY, THROUGH)), engine, write_root, cache_root)

    assert healed.symbols[0].partitions_resumed == 0
    assert healed.symbols[0].archives_absent == 0
    assert healed.symbols[0].rows_out == COMPLETE_DAY_COUNT * MINUTES_PER_DAY
    # The gap row stays. It is a record that the corpus did not hold this range at a known
    # instant, which is exactly what a backtest run before now has to be checked against.
    assert await _scalar(engine, "SELECT count(*) FROM coverage_gap") == 1


@pytest.mark.asyncio
async def test_a_run_that_would_narrow_an_existing_partition_is_refused(
    engine: AsyncEngine, tmp_path: Path
) -> None:
    """A partition is written whole, so a narrower re-run would delete the difference.

    Reachable without operator error: the first run met an absent archive, so the partition
    is deliberately not marked complete and the second run re-derives it -- and if that
    second run asks for fewer days, the rewrite silently truncates the corpus. It raises
    instead, for the same reason the fetcher refuses a cached archive whose checksum no
    longer matches: a quiet repair is how a condition stops being reported by anything.
    """
    write_root, cache_root = tmp_path / "parquet", tmp_path / "archive"
    served = tuple(day for day in days_in(FIRST_DAY, THROUGH) if day != MISSING_DAY)
    await _run(_stub(days=served), engine, write_root, cache_root)

    narrower = dataclasses.replace(_request(write_root), through_date=date(2025, 1, 3))
    egress = _stub(days=served)
    with pytest.raises(DataIntegrityError, match="written whole"):
        await run_backfill(
            narrower,
            fetcher=ArchiveFetcher(egress=egress, cache_root=cache_root),
            egress=egress,
            registry=IngestRegistry(engine),
        )

    glob_sql = market_dataset_glob(_partition(), root=write_root)
    with read_connection() as connection:
        surviving = connection.execute(
            f"SELECT count(*) FROM read_parquet('{glob_sql}', hive_partitioning = true)"  # noqa: S608
        ).fetchone()
    assert surviving is not None
    assert surviving[0] == SERVED_DAY_COUNT * MINUTES_PER_DAY


@pytest.mark.asyncio
async def test_the_coverage_report_states_bounds_gaps_and_gapped_duration(
    engine: AsyncEngine, tmp_path: Path
) -> None:
    served = tuple(day for day in days_in(FIRST_DAY, THROUGH) if day != MISSING_DAY)
    await _run(_stub(days=served), engine, tmp_path / "parquet", tmp_path / "archive")

    coverage = await IngestRegistry(engine).coverage()

    assert len(coverage) == 1
    row = coverage[0]
    assert (row.market, row.dataset, row.symbol, row.bar_interval) == (
        "spot",
        "klines",
        RECORDED.symbol,
        "1m",
    )
    assert row.first_event_time_utc == datetime(2025, 1, 2, tzinfo=UTC)
    assert row.last_event_time_utc == datetime(2025, 1, 6, 23, 59, tzinfo=UTC)
    assert row.row_count == SERVED_DAY_COUNT * MINUTES_PER_DAY
    assert row.gap_count == 1
    assert row.total_gapped_duration.total_seconds() == 24 * 60 * 60
    assert row.missing_bar_count == MINUTES_PER_DAY


@pytest.mark.asyncio
async def test_an_unpublished_symbol_is_reported_rather_than_silently_skipped(
    engine: AsyncEngine, tmp_path: Path
) -> None:
    report = await _run(_stub(days=()), engine, tmp_path / "parquet", tmp_path / "archive")

    assert report.symbols[0].is_published is False
    assert report.unpublished_symbols == (RECORDED.symbol,)
    assert "no published archive" in report.render()
    assert await _scalar(engine, "SELECT count(*) FROM ingest_partition") == 0


@pytest.mark.asyncio
async def test_a_dataset_with_no_cadence_records_absence_rather_than_missing_prints(
    engine: AsyncEngine, tmp_path: Path
) -> None:
    """Trades have no cadence, so a missing day is a publication fact and nothing more.

    A trades archive that was never published says nothing about how many prints are
    missing -- there is no interval to divide by -- so the gap is `absent_archive` with a
    null count. Recording a number there would invent a denominator. Trades are also one
    Parquet file per day, so the absent day is its own empty partition rather than a hole
    inside somebody else's.
    """
    recorded = archive_fixtures.find(
        market=Market.SPOT, dataset=Dataset.TRADES, archive_date=FIRST_DAY, whole=False
    )
    served = (FIRST_DAY, date(2025, 1, 4))
    egress = StubArchiveEgress(
        daily_archives(member=recorded.read(), coordinate=recorded.coordinate(), days=served)
    )
    request = BackfillRequest(
        market=Market.SPOT,
        dataset=Dataset.TRADES,
        symbols=(recorded.symbol,),
        interval=None,
        through_date=date(2025, 1, 4),
        today_utc=TODAY,
        now_utc=NOW,
        history_floor_date=JANUARY,
        write_root=tmp_path / "parquet",
    )

    report = await run_backfill(
        request,
        fetcher=ArchiveFetcher(egress=egress, cache_root=tmp_path / "archive"),
        egress=egress,
        registry=IngestRegistry(engine),
    )

    assert report.symbols[0].partitions_written == len(served)
    assert report.symbols[0].archives_absent == 1
    async with engine.connect() as connection:
        rows = (
            await connection.execute(
                sa.text(
                    "SELECT gap_start_utc, gap_end_utc, gap_kind, missing_bar_count "
                    "FROM coverage_gap"
                )
            )
        ).all()
    assert len(rows) == 1
    assert rows[0].gap_start_utc == datetime(2025, 1, 3, tzinfo=UTC)
    assert rows[0].gap_end_utc == datetime(2025, 1, 4, tzinfo=UTC)
    assert rows[0].gap_kind == "absent_archive"
    assert rows[0].missing_bar_count is None


def _digest_of(write_root: Path) -> str:
    """The content digest the written Parquet file carries, read from its footer."""
    metadata = pq.read_schema(partition_path(_partition(), root=write_root)).metadata
    assert metadata is not None
    return str(metadata[CONTENT_DIGEST_KEY].decode())
