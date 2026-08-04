"""The coverage and gap registry: what the corpus holds, what it does not, and since when.

Three tables, written here and nowhere else (`migrations/versions/0009_ingest_registry.py`
carries the DDL and the reasoning for the grain split). The rules this module implements
are the ones a re-run depends on:

**A partition row is upserted; a gap row is not.** Re-reading the same archives produces
the same facts, so `ON CONFLICT DO UPDATE` on `ingest_partition` and `ingest_file` is
idempotent by construction. `coverage_gap` uses `ON CONFLICT DO NOTHING` instead, because
its `discovered_at_utc` is the one column a rebuild cannot reproduce: it answers "which
completed backtests consumed this range before anybody knew there was a hole in it"
(`DATA_PIPELINE.md` section 11), and an upsert would move it forward on every run until it
answered nothing.

**Gaps are recorded, never merged and never filled.** Two adjacent absent days stay two
rows. Merging means rewriting a row to widen its bounds, which destroys the earlier
discovery instant for the same reason an upsert would -- and `sum(gap_end - gap_start)`
over adjacent rows is the same total either way, so the merge buys presentation at the cost
of the only column that matters. Filling is forbidden outright: a synthesised bar has zero
realised volatility and perfect mean reversion, which is catnip to exactly the strategies
this system exists to reject (`DATA_PIPELINE.md` section 4).

**One transaction per partition.** Not one per run: a backfill of eight years is hours
long, and a run-length transaction would hold locks for its duration and lose everything to
one interruption -- which is the interruption the resume path exists for. Not one per row
either: a partition's Parquet file, its per-archive records and its gaps are one fact about
one period, and a crash between them would leave a corpus file the registry does not know
about, which is precisely the disagreement resume is checked against.

The `bar_interval` column carries `''` where the dataset is not keyed by an interval. It is
a primary-key component, a primary key cannot hold NULL, and the database ties the sentinel
to the dataset with a `CHECK` so it cannot drift into meaning something else.
"""

from __future__ import annotations

import json
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from enum import StrEnum
from typing import Final

import sqlalchemy as sa
from sqlalchemy.ext.asyncio import AsyncConnection, AsyncEngine

from fking.data.archive import ArchiveCoordinate, Granularity
from fking.data.loaders import NormalizationResult
from fking.data.parquet.layout import PartitionGrain

__all__ = [
    "NO_INTERVAL",
    "CoverageRow",
    "GapKind",
    "IngestRegistry",
    "IngestedFile",
    "PartitionRecord",
    "PartitionState",
    "RecordedGap",
]

# The sentinel meaning "this dataset is not keyed by an interval". Declared once, here,
# because it appears in every key and in the CHECK constraint that gives it meaning.
NO_INTERVAL: Final[str] = ""


class GapKind(StrEnum):
    """Why a period holds no rows, and therefore what can honestly be said about it.

    `CADENCE` and `SEAM` are claims about *bars*: the interval says how many should be
    there and they are not, so `missing_bar_count` is exact. `ABSENT_ARCHIVE` is a claim
    about *publication*, which is all that can be said about a dataset with no cadence -- a
    trades file that does not exist is not evidence that any particular print is missing,
    and recording a count there would invent a denominator.
    """

    CADENCE = "cadence"
    """Interior to one Parquet partition: bars missing between two bars the corpus holds."""

    SEAM = "seam"
    """Between two partitions: the last bar of one period and the first of the next."""

    ABSENT_ARCHIVE = "absent_archive"
    """The host does not publish this period, for a dataset with no declared cadence."""


@dataclass(frozen=True, slots=True)
class IngestedFile:
    """One archive's `NormalizationResult`, keyed by the archive it describes."""

    archive_date: date
    granularity: Granularity
    source_checksum_hex: str
    normalization: NormalizationResult


@dataclass(frozen=True, slots=True)
class RecordedGap:
    """A period the corpus does not hold, in half-open event time.

    `[gap_start_utc, gap_end_utc)` names the missing region itself rather than the
    observations bracketing it, which is what makes `gap_end - gap_start` the gap's own
    duration and `sum(...)` over the table a truthful total.
    """

    gap_start_utc: datetime
    gap_end_utc: datetime
    gap_kind: GapKind
    missing_bar_count: int | None

    @property
    def duration(self) -> timedelta:
        return self.gap_end_utc - self.gap_start_utc


@dataclass(frozen=True, slots=True)
class PartitionRecord:
    """Everything one partition's write produced, as one atomic registry fact."""

    coordinate: ArchiveCoordinate
    grain: PartitionGrain
    covered_from_date: date
    covered_through_date: date
    absent_archive_count: int
    first_event_time_utc: datetime
    last_event_time_utc: datetime
    content_digest_hex: str
    parquet_path: str
    written_at_utc: datetime
    files: tuple[IngestedFile, ...]
    gaps: tuple[RecordedGap, ...]

    @property
    def rows_in(self) -> int:
        return sum(ingested.normalization.rows_in for ingested in self.files)

    @property
    def rows_out(self) -> int:
        return sum(ingested.normalization.rows_out for ingested in self.files)

    @property
    def rows_rejected(self) -> int:
        return sum(ingested.normalization.rows_rejected for ingested in self.files)


@dataclass(frozen=True, slots=True)
class PartitionState:
    """What the registry already believes about one partition.

    `content_digest_hex` is the half that makes resume checkable. The runner compares it
    against the digest in the Parquet footer, so "the registry and the corpus agree" is
    demonstrated rather than assumed -- a progress file could agree with neither and be
    believed by both.
    """

    covered_through_date: date
    absent_archive_count: int
    content_digest_hex: str
    parquet_path: str
    first_event_time_utc: datetime
    last_event_time_utc: datetime


@dataclass(frozen=True, slots=True)
class CoverageRow:
    """One line of the coverage report, per `(market, dataset, symbol, interval)`."""

    market: str
    dataset: str
    symbol: str
    bar_interval: str
    first_event_time_utc: datetime
    last_event_time_utc: datetime
    row_count: int
    partition_count: int
    gap_count: int
    total_gapped_duration: timedelta
    missing_bar_count: int


_UPSERT_PARTITION: Final[sa.TextClause] = sa.text(
    """
    INSERT INTO ingest_partition (
        market, dataset, symbol, bar_interval, period_start_date, partition_grain,
        covered_from_date, covered_through_date, archive_count, absent_archive_count,
        rows_in, rows_out, rows_rejected, first_event_time_utc, last_event_time_utc,
        content_digest_hex, parquet_path, written_at_utc
    )
    VALUES (
        :market, :dataset, :symbol, :bar_interval, :period_start_date, :partition_grain,
        :covered_from_date, :covered_through_date, :archive_count, :absent_archive_count,
        :rows_in, :rows_out, :rows_rejected, :first_event_time_utc, :last_event_time_utc,
        :content_digest_hex, :parquet_path, :written_at_utc
    )
    ON CONFLICT (market, dataset, symbol, bar_interval, period_start_date) DO UPDATE SET
        partition_grain      = EXCLUDED.partition_grain,
        covered_from_date    = LEAST(ingest_partition.covered_from_date,
                                     EXCLUDED.covered_from_date),
        covered_through_date = GREATEST(ingest_partition.covered_through_date,
                                        EXCLUDED.covered_through_date),
        archive_count        = EXCLUDED.archive_count,
        absent_archive_count = EXCLUDED.absent_archive_count,
        rows_in              = EXCLUDED.rows_in,
        rows_out             = EXCLUDED.rows_out,
        rows_rejected        = EXCLUDED.rows_rejected,
        first_event_time_utc = LEAST(ingest_partition.first_event_time_utc,
                                     EXCLUDED.first_event_time_utc),
        last_event_time_utc  = GREATEST(ingest_partition.last_event_time_utc,
                                        EXCLUDED.last_event_time_utc),
        content_digest_hex   = EXCLUDED.content_digest_hex,
        parquet_path         = EXCLUDED.parquet_path,
        written_at_utc       = EXCLUDED.written_at_utc
    """
)

_UPSERT_FILE: Final[sa.TextClause] = sa.text(
    """
    INSERT INTO ingest_file (
        market, dataset, symbol, bar_interval, archive_date, granularity,
        period_start_date, source_checksum_hex, rows_in, rows_out, rows_rejected,
        rejection_reasons, epoch_unit_applied, first_event_time_utc, last_event_time_utc,
        ingested_at_utc
    )
    VALUES (
        :market, :dataset, :symbol, :bar_interval, :archive_date, :granularity,
        :period_start_date, :source_checksum_hex, :rows_in, :rows_out, :rows_rejected,
        CAST(:rejection_reasons AS jsonb), :epoch_unit_applied, :first_event_time_utc,
        :last_event_time_utc, :ingested_at_utc
    )
    ON CONFLICT (market, dataset, symbol, bar_interval, archive_date, granularity)
    DO UPDATE SET
        period_start_date    = EXCLUDED.period_start_date,
        source_checksum_hex  = EXCLUDED.source_checksum_hex,
        rows_in              = EXCLUDED.rows_in,
        rows_out             = EXCLUDED.rows_out,
        rows_rejected        = EXCLUDED.rows_rejected,
        rejection_reasons    = EXCLUDED.rejection_reasons,
        epoch_unit_applied   = EXCLUDED.epoch_unit_applied,
        first_event_time_utc = EXCLUDED.first_event_time_utc,
        last_event_time_utc  = EXCLUDED.last_event_time_utc,
        ingested_at_utc      = EXCLUDED.ingested_at_utc
    """
)

# DO NOTHING, not DO UPDATE. The discovery instant is the column a rebuild cannot
# reproduce and the one an escalation is keyed on; an upsert would move it forward on
# every run until it recorded nothing but the most recent backfill.
_INSERT_GAP: Final[sa.TextClause] = sa.text(
    """
    INSERT INTO coverage_gap (
        market, dataset, symbol, bar_interval, gap_start_utc, gap_end_utc, gap_kind,
        missing_bar_count, discovered_at_utc
    )
    VALUES (
        :market, :dataset, :symbol, :bar_interval, :gap_start_utc, :gap_end_utc, :gap_kind,
        :missing_bar_count, :discovered_at_utc
    )
    ON CONFLICT (market, dataset, symbol, bar_interval, gap_start_utc, gap_end_utc)
    DO NOTHING
    RETURNING 1
    """
)


class IngestRegistry:
    """Reads and writes the ingestion registry, one short transaction at a time.

    Holds an engine rather than a connection deliberately. A backfill runs for hours, and a
    caller handed one connection would either wrap the whole run in a transaction -- losing
    every partition to one interruption -- or commit inside somebody else's unit of work.
    """

    def __init__(self, engine: AsyncEngine) -> None:
        self._engine = engine

    async def partition_state(self, coordinate: ArchiveCoordinate) -> PartitionState | None:
        """What the registry holds for this partition, or `None` if it has never seen it."""
        async with self._engine.connect() as connection:
            row = (
                await connection.execute(
                    sa.text(
                        """
                        SELECT covered_through_date, absent_archive_count,
                               content_digest_hex, parquet_path, first_event_time_utc,
                               last_event_time_utc
                          FROM ingest_partition
                         WHERE market = :market AND dataset = :dataset
                           AND symbol = :symbol AND bar_interval = :bar_interval
                           AND period_start_date = :period_start_date
                        """
                    ),
                    {
                        **_series_parameters(coordinate),
                        "period_start_date": coordinate.archive_date,
                    },
                )
            ).first()
        if row is None:
            return None
        return PartitionState(
            covered_through_date=row.covered_through_date,
            absent_archive_count=int(row.absent_archive_count),
            content_digest_hex=str(row.content_digest_hex),
            parquet_path=str(row.parquet_path),
            first_event_time_utc=row.first_event_time_utc,
            last_event_time_utc=row.last_event_time_utc,
        )

    async def last_event_before(self, coordinate: ArchiveCoordinate) -> datetime | None:
        """The newest event time the corpus holds for this series before this period.

        The left-hand side of a seam gap. Read from the registry rather than carried in
        memory from the previous partition, so that a resumed run stitches to what the
        earlier run actually wrote instead of to the first partition this process happened
        to process.
        """
        async with self._engine.connect() as connection:
            row = (
                await connection.execute(
                    sa.text(
                        """
                        SELECT last_event_time_utc
                          FROM ingest_partition
                         WHERE market = :market AND dataset = :dataset
                           AND symbol = :symbol AND bar_interval = :bar_interval
                           AND period_start_date < :period_start_date
                         ORDER BY period_start_date DESC
                         LIMIT 1
                        """
                    ),
                    {
                        **_series_parameters(coordinate),
                        "period_start_date": coordinate.archive_date,
                    },
                )
            ).first()
        return None if row is None else row.last_event_time_utc

    async def record_partition(
        self, record: PartitionRecord, *, discovered_at_utc: datetime
    ) -> int:
        """Write one partition, its per-archive results and its gaps, atomically.

        Returns the number of gap rows that were new. A gap already in the table is not
        counted, which is what makes "this run discovered nothing that was not already
        known" a number a report can state rather than a claim it has to hedge.
        """
        series = _series_parameters(record.coordinate)
        async with self._engine.begin() as connection:
            await connection.execute(
                _UPSERT_PARTITION,
                {
                    **series,
                    "period_start_date": record.coordinate.archive_date,
                    "partition_grain": record.grain.value,
                    "covered_from_date": record.covered_from_date,
                    "covered_through_date": record.covered_through_date,
                    "archive_count": len(record.files),
                    "absent_archive_count": record.absent_archive_count,
                    "rows_in": record.rows_in,
                    "rows_out": record.rows_out,
                    "rows_rejected": record.rows_rejected,
                    "first_event_time_utc": record.first_event_time_utc,
                    "last_event_time_utc": record.last_event_time_utc,
                    "content_digest_hex": record.content_digest_hex,
                    "parquet_path": record.parquet_path,
                    "written_at_utc": record.written_at_utc,
                },
            )
            for ingested in record.files:
                await connection.execute(
                    _UPSERT_FILE,
                    {
                        **series,
                        "archive_date": ingested.archive_date,
                        "granularity": ingested.granularity.value,
                        "period_start_date": record.coordinate.archive_date,
                        "source_checksum_hex": ingested.source_checksum_hex,
                        "rows_in": ingested.normalization.rows_in,
                        "rows_out": ingested.normalization.rows_out,
                        "rows_rejected": ingested.normalization.rows_rejected,
                        "rejection_reasons": json.dumps(
                            _reason_counts(ingested.normalization), sort_keys=True
                        ),
                        "epoch_unit_applied": ingested.normalization.epoch_unit_applied.value,
                        "first_event_time_utc": ingested.normalization.first_event_time_utc,
                        "last_event_time_utc": ingested.normalization.last_event_time_utc,
                        "ingested_at_utc": record.written_at_utc,
                    },
                )
            return await self._insert_gaps(
                connection, series, record.gaps, discovered_at_utc=discovered_at_utc
            )

    async def record_gaps(
        self,
        coordinate: ArchiveCoordinate,
        gaps: Sequence[RecordedGap],
        *,
        discovered_at_utc: datetime,
    ) -> int:
        """Register gaps for a period that produced no partition at all.

        A period whose every archive is absent has no Parquet file and therefore no
        partition row -- writing a zero-row file to have something to point at is the one
        thing the writer refuses, because a later scan reads it as "we have this period".
        """
        if not gaps:
            return 0
        async with self._engine.begin() as connection:
            return await self._insert_gaps(
                connection,
                _series_parameters(coordinate),
                gaps,
                discovered_at_utc=discovered_at_utc,
            )

    async def coverage(self) -> tuple[CoverageRow, ...]:
        """The coverage report, one row per series, ordered for a stable printout."""
        async with self._engine.connect() as connection:
            rows = (
                await connection.execute(
                    sa.text(
                        """
                        SELECT market, dataset, symbol, bar_interval, first_event_time_utc,
                               last_event_time_utc, row_count, partition_count, gap_count,
                               total_gapped_duration, missing_bar_count
                          FROM data_coverage
                         ORDER BY market, dataset, symbol, bar_interval
                        """
                    )
                )
            ).all()
        return tuple(
            CoverageRow(
                market=str(row.market),
                dataset=str(row.dataset),
                symbol=str(row.symbol),
                bar_interval=str(row.bar_interval),
                first_event_time_utc=row.first_event_time_utc,
                last_event_time_utc=row.last_event_time_utc,
                row_count=int(row.row_count),
                partition_count=int(row.partition_count),
                gap_count=int(row.gap_count),
                total_gapped_duration=row.total_gapped_duration,
                missing_bar_count=int(row.missing_bar_count),
            )
            for row in rows
        )

    @staticmethod
    async def _insert_gaps(
        connection: AsyncConnection,
        series: Mapping[str, object],
        gaps: Sequence[RecordedGap],
        *,
        discovered_at_utc: datetime,
    ) -> int:
        newly_discovered = 0
        for gap in gaps:
            inserted = (
                await connection.execute(
                    _INSERT_GAP,
                    {
                        **series,
                        "gap_start_utc": gap.gap_start_utc,
                        "gap_end_utc": gap.gap_end_utc,
                        "gap_kind": gap.gap_kind.value,
                        "missing_bar_count": gap.missing_bar_count,
                        "discovered_at_utc": discovered_at_utc,
                    },
                )
            ).first()
            if inserted is not None:
                newly_discovered += 1
        return newly_discovered


def _series_parameters(coordinate: ArchiveCoordinate) -> dict[str, object]:
    """The four columns identifying a series, spelled the way every statement binds them."""
    return {
        "market": coordinate.market.value,
        "dataset": coordinate.dataset.value,
        "symbol": coordinate.symbol,
        "bar_interval": coordinate.interval if coordinate.interval is not None else NO_INTERVAL,
    }


def _reason_counts(normalization: NormalizationResult) -> dict[str, int]:
    """Rejection tallies as a JSON object keyed by reason.

    Keyed by the enum's value rather than by its name, so the JSON a dashboard reads uses
    the same token as the Prometheus label the counter will carry -- two spellings of one
    reason are two time series that each look like half a problem.
    """
    return {
        reason.value: tally for reason, tally in normalization.rejection_reasons.items() if tally
    }
