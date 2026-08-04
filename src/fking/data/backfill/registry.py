"""The coverage and gap registry: what the corpus holds, what it does not, and since when.

Three tables, written here and nowhere else (`migrations/versions/0009_ingest_registry.py`
carries the DDL and the reasoning for the grain split). The rules this module implements
are the ones a re-run depends on:

**A partition row is upserted; a gap row is not.** Re-reading the same archives produces
the same facts, so `ON CONFLICT DO UPDATE` on `ingest_partition` and `ingest_file` is
idempotent by construction. Every updated column takes `EXCLUDED` rather than a `LEAST` or
`GREATEST` of the old and new values: the row has to describe the Parquet file that is
actually on disk, and a widest-ever coverage range merged across runs would describe a file
that never existed. Keeping the row honest is what lets `runner._require_no_narrowing` use
it to refuse a truncating rewrite. `coverage_gap` uses `ON CONFLICT DO NOTHING` instead,
because
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

**A gap that a backfill genuinely closes is *resolved*, which is not any of the above.**
`resolve_gap` writes two columns and no others, and the trigger from `0012_gap_resolution`
is what makes that a property of the table rather than of this module. The bounds, the
kind and `discovered_at_utc` stay exactly as recorded, so the row keeps answering "which
completed backtests consumed this range while it was holed" -- a question that gets *more*
interesting after a repair, because those results are still wrong. A partial recovery
inserts narrower rows carrying the original discovery instant and marks the original
`superseded`; the region is never rewritten in place and never deleted.

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
from fking.data.format_resolver import Dataset, Market
from fking.data.loaders import NormalizationResult
from fking.data.parquet.layout import PartitionGrain
from fking.platform.errors import DataIntegrityError, DataUnavailableError

__all__ = [
    "NO_INTERVAL",
    "CoverageRow",
    "GapKind",
    "GapResolution",
    "IngestRegistry",
    "IngestedFile",
    "OpenGap",
    "PartitionRecord",
    "PartitionState",
    "RecordedGap",
    "SeriesGap",
    "SeriesKey",
]

# The sentinel meaning "this dataset is not keyed by an interval". Declared once, here,
# because it appears in every key and in the CHECK constraint that gives it meaning.
NO_INTERVAL: Final[str] = ""


class GapKind(StrEnum):
    """Why a period holds no rows, and therefore what can honestly be said about it.

    `CADENCE`, `SEAM` and `SEQUENCE` are claims about *records*: something says how many
    should be there and they are not, so `missing_bar_count` is exact. `ABSENT_ARCHIVE`
    is a claim about *publication*, which is all that can be said about a dataset with no
    cadence -- a trades file that does not exist is not evidence that any particular print
    is missing, and recording a count there would invent a denominator. `DISCONNECT` is a
    claim about *observation* and carries no count at all.

    The last two arise only from live ingestion (`fking.data.live`), and the values are
    admitted by the table's `CHECK` in `migrations/versions/0011_live_gap_kinds.py`. They
    are separate kinds rather than reuses of the archive's three because a reader
    grouping by kind is asking "why is this missing", and "the socket dropped" is not the
    same answer as "the host never published it".
    """

    CADENCE = "cadence"
    """Interior to one Parquet partition: bars missing between two bars the corpus holds."""

    SEAM = "seam"
    """Between two partitions: the last bar of one period and the first of the next."""

    ABSENT_ARCHIVE = "absent_archive"
    """The host does not publish this period, for a dataset with no declared cadence."""

    SEQUENCE = "sequence"
    """A live `aggTrade.a` jump: the exact number of prints the venue assigned and we
    did not receive. The only kind carrying an exact count for a dataset with no
    cadence, because the venue's own monotone id supplies the denominator."""

    DISCONNECT = "disconnect"
    """A live socket outage. `missing_bar_count` is NULL even for a kline series: a
    400 ms reconnect inside one minute loses no bar, and the claim being made is that
    nothing was being observed rather than that anything specific is absent."""


class GapResolution(StrEnum):
    """What became of a gap once a backfill reached it (`0012_gap_resolution`).

    There is deliberately no `abandoned`. A gap nobody could fill stays unresolved,
    because "we gave up" and "the data is here" must not read the same way to the
    coverage query a backtest is refused by.
    """

    BACKFILLED = "backfilled"
    """The corpus now holds the whole region the row named."""

    SUPERSEDED = "superseded"
    """Part of it was recovered; narrower rows carry what is still absent."""


@dataclass(frozen=True, slots=True)
class OpenGap:
    """One unresolved gap, with the instant it was first discovered.

    The discovery instant is carried because a partial backfill has to hand it to the
    residual rows it inserts: those minutes were found missing then and are missing
    still, and stamping them with the backfill's own clock would launder a long-known
    hole into a freshly discovered one -- which is exactly the question
    `discovered_at_utc` exists to answer (`DATA_PIPELINE.md` section 11).
    """

    series: SeriesKey
    gap: RecordedGap
    discovered_at_utc: datetime


@dataclass(frozen=True, slots=True)
class IngestedFile:
    """One archive's `NormalizationResult`, keyed by the archive it describes."""

    archive_date: date
    granularity: Granularity
    source_checksum_hex: str
    normalization: NormalizationResult


@dataclass(frozen=True, slots=True)
class SeriesKey:
    """The four columns every registry statement identifies a series by.

    Separate from `ArchiveCoordinate` because a series is not an archive: live ingestion
    (`fking.data.live`) discovers gaps in a series that has no `archive_date` at all, and
    passing an invented date so that a coordinate could be constructed would put a
    fiction into the one structure a reader trusts to say what is missing.
    """

    market: Market
    dataset: Dataset
    symbol: str
    bar_interval: str

    @classmethod
    def from_coordinate(cls, coordinate: ArchiveCoordinate) -> SeriesKey:
        return cls(
            market=coordinate.market,
            dataset=coordinate.dataset,
            symbol=coordinate.symbol,
            bar_interval=coordinate.interval if coordinate.interval is not None else NO_INTERVAL,
        )


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

    The covered dates are the other half, and they are read for a different reason: a
    partition is written whole, so a run whose archives cover *less* than this row records
    would silently delete the difference from the corpus. The runner refuses that rather
    than narrowing (`runner._require_no_narrowing`).
    """

    covered_from_date: date
    covered_through_date: date
    absent_archive_count: int
    content_digest_hex: str
    parquet_path: str
    first_event_time_utc: datetime
    last_event_time_utc: datetime


@dataclass(frozen=True, slots=True)
class SeriesGap:
    """One recorded gap, carrying the series it belongs to.

    `CoverageRow` aggregates gaps into a count and a total duration, which answers "how
    holed is this series" and cannot answer "does *this* window intersect a hole" -- the
    question the availability contract asks before a backtest reads anything (#30). The
    series columns are plain strings for the same reason `CoverageRow`'s are: they come
    back from the database as text, and parsing them into enums here would put a second
    failure mode -- an unrecognised value -- in the middle of a read whose only job is
    reporting.
    """

    market: str
    dataset: str
    symbol: str
    bar_interval: str
    gap: RecordedGap


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
        covered_from_date    = EXCLUDED.covered_from_date,
        covered_through_date = EXCLUDED.covered_through_date,
        archive_count        = EXCLUDED.archive_count,
        absent_archive_count = EXCLUDED.absent_archive_count,
        rows_in              = EXCLUDED.rows_in,
        rows_out             = EXCLUDED.rows_out,
        rows_rejected        = EXCLUDED.rows_rejected,
        first_event_time_utc = EXCLUDED.first_event_time_utc,
        last_event_time_utc  = EXCLUDED.last_event_time_utc,
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

# The one UPDATE this table admits, and the `coverage_gap_resolution_only` trigger from
# 0012 is what makes that a property of the schema rather than of this statement. The
# `resolved_at_utc IS NULL` predicate is not belt and braces: it is how a concurrent
# second resolver learns it lost, by updating zero rows instead of overwriting a verdict.
_RESOLVE_GAP: Final[sa.TextClause] = sa.text(
    """
    UPDATE coverage_gap
       SET resolution = :resolution, resolved_at_utc = :resolved_at_utc
     WHERE market = :market AND dataset = :dataset AND symbol = :symbol
       AND bar_interval = :bar_interval
       AND gap_start_utc = :gap_start_utc AND gap_end_utc = :gap_end_utc
       AND resolved_at_utc IS NULL
    RETURNING 1
    """
)

_SELECT_OPEN_GAPS: Final[sa.TextClause] = sa.text(
    """
    SELECT gap_start_utc, gap_end_utc, gap_kind, missing_bar_count, discovered_at_utc
      FROM coverage_gap
     WHERE market = :market AND dataset = :dataset AND symbol = :symbol
       AND bar_interval = :bar_interval
       AND resolved_at_utc IS NULL
     ORDER BY gap_start_utc
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
                        SELECT covered_from_date, covered_through_date,
                               absent_archive_count, content_digest_hex, parquet_path,
                               first_event_time_utc, last_event_time_utc
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
            covered_from_date=row.covered_from_date,
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
        return await self.record_series_gaps(
            SeriesKey.from_coordinate(coordinate), gaps, discovered_at_utc=discovered_at_utc
        )

    async def record_series_gaps(
        self,
        series: SeriesKey,
        gaps: Sequence[RecordedGap],
        *,
        discovered_at_utc: datetime,
    ) -> int:
        """Register gaps against a series that has no archive behind it.

        The live path's entry point. Identical statement, identical deduplication: a
        downstream reader cannot tell whether a gap came from a missing archive or from
        a dropped socket, and `DATA_PIPELINE.md` section 5 requires exactly that.
        """
        if not gaps:
            return 0
        async with self._engine.begin() as connection:
            return await self._insert_gaps(
                connection,
                _series_key_parameters(series),
                gaps,
                discovered_at_utc=discovered_at_utc,
            )

    async def open_gaps(self, series: SeriesKey) -> tuple[OpenGap, ...]:
        """Every unresolved gap for one series, oldest first.

        Ordered by `gap_start_utc` so a repair walks a series forward: the oldest hole is
        the one most likely to have been consumed by a completed backtest, and it is also
        the one most likely to have fallen off the venue's REST retention -- so failing on
        it first is failing on the informative one.
        """
        async with self._engine.connect() as connection:
            rows = (
                await connection.execute(_SELECT_OPEN_GAPS, _series_key_parameters(series))
            ).all()
        return tuple(
            OpenGap(
                series=series,
                gap=RecordedGap(
                    gap_start_utc=row.gap_start_utc,
                    gap_end_utc=row.gap_end_utc,
                    gap_kind=GapKind(row.gap_kind),
                    missing_bar_count=(
                        None if row.missing_bar_count is None else int(row.missing_bar_count)
                    ),
                ),
                discovered_at_utc=row.discovered_at_utc,
            )
            for row in rows
        )

    async def resolve_gap(
        self,
        open_gap: OpenGap,
        residuals: Sequence[RecordedGap],
        *,
        resolved_at_utc: datetime,
    ) -> GapResolution:
        """Record that a backfill filled all or part of `open_gap`, in one transaction.

        Residuals are inserted *before* the original is marked, so an interruption between
        the two statements leaves the region described twice rather than not at all. Each
        residual carries `open_gap.discovered_at_utc` rather than the backfill's clock:
        those minutes were discovered missing then and are missing still.

        Returns:
            `BACKFILLED` when nothing remains, `SUPERSEDED` when residuals were written.

        Raises:
            DataIntegrityError: a residual is not strictly inside the gap being resolved,
                or a residual reproduces the gap's own bounds. The second case means
                nothing was recovered, and marking the original resolved would make the
                whole hole disappear behind a row the insert had deduplicated away.
            DataUnavailableError: the gap was already resolved, or is not in the registry.
        """
        for residual in residuals:
            if (residual.gap_start_utc, residual.gap_end_utc) == (
                open_gap.gap.gap_start_utc,
                open_gap.gap.gap_end_utc,
            ):
                raise DataIntegrityError(
                    f"a residual reproducing [{residual.gap_start_utc.isoformat()}, "
                    f"{residual.gap_end_utc.isoformat()}) means the backfill recovered "
                    f"nothing; resolving on that would delete the gap rather than narrow it"
                )
            if (
                residual.gap_start_utc < open_gap.gap.gap_start_utc
                or residual.gap_end_utc > open_gap.gap.gap_end_utc
            ):
                raise DataIntegrityError(
                    f"residual [{residual.gap_start_utc.isoformat()}, "
                    f"{residual.gap_end_utc.isoformat()}) falls outside the gap it "
                    f"narrows, [{open_gap.gap.gap_start_utc.isoformat()}, "
                    f"{open_gap.gap.gap_end_utc.isoformat()}). A repair widens nothing"
                )

        resolution = GapResolution.SUPERSEDED if residuals else GapResolution.BACKFILLED
        series = _series_key_parameters(open_gap.series)
        async with self._engine.begin() as connection:
            await self._insert_gaps(
                connection,
                series,
                residuals,
                discovered_at_utc=open_gap.discovered_at_utc,
            )
            # The insert deduplicates by bounds, so a residual colliding with a row that
            # is *already resolved* would be silently swallowed and the region would read
            # as covered while it is not. Rare -- it takes a sub-range that was backfilled
            # once and has gone missing again -- and exactly the shape of failure this
            # whole table exists to make impossible, so it is checked rather than assumed.
            for residual in residuals:
                await self._require_residual_is_open(connection, series, residual)
            marked = (
                await connection.execute(
                    _RESOLVE_GAP,
                    {
                        **series,
                        "gap_start_utc": open_gap.gap.gap_start_utc,
                        "gap_end_utc": open_gap.gap.gap_end_utc,
                        "resolution": resolution.value,
                        "resolved_at_utc": resolved_at_utc,
                    },
                )
            ).first()
        if marked is None:
            raise DataUnavailableError(
                f"no unresolved gap [{open_gap.gap.gap_start_utc.isoformat()}, "
                f"{open_gap.gap.gap_end_utc.isoformat()}) for {open_gap.series.symbol} "
                f"{open_gap.series.dataset.value}; it was resolved by another writer or "
                f"never recorded, and either way this repair must not claim it"
            )
        return resolution

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

    async def recorded_gaps(self) -> tuple[SeriesGap, ...]:
        """Every gap the registry holds, with its bounds, ordered for a stable readout.

        Resolved gaps are excluded: a range a backfill has since filled must not keep
        refusing windows, and `data_coverage` stops counting it for the same reason. The
        row itself stays, because it is still the answer to "which completed backtests
        consumed this range while it was holed".

        Read from `coverage_gap` rather than from `data_coverage`, because the aggregate
        deliberately does not carry bounds and a window check needs them. Ordered by the
        series and then by `gap_start_utc`, so a refusal names the *first* hole a window
        runs into rather than whichever one the planner happened to return first --
        "narrow to before 2021-05-03" is actionable in a way that "there is a hole
        somewhere" is not.
        """
        async with self._engine.connect() as connection:
            rows = (
                await connection.execute(
                    sa.text(
                        """
                        SELECT market, dataset, symbol, bar_interval, gap_start_utc,
                               gap_end_utc, gap_kind, missing_bar_count
                          FROM coverage_gap
                         WHERE resolved_at_utc IS NULL
                         ORDER BY market, dataset, symbol, bar_interval, gap_start_utc
                        """
                    )
                )
            ).all()
        return tuple(
            SeriesGap(
                market=str(row.market),
                dataset=str(row.dataset),
                symbol=str(row.symbol),
                bar_interval=str(row.bar_interval),
                gap=RecordedGap(
                    gap_start_utc=row.gap_start_utc,
                    gap_end_utc=row.gap_end_utc,
                    gap_kind=GapKind(row.gap_kind),
                    missing_bar_count=(
                        None if row.missing_bar_count is None else int(row.missing_bar_count)
                    ),
                ),
            )
            for row in rows
        )

    @staticmethod
    async def _require_residual_is_open(
        connection: AsyncConnection, series: Mapping[str, object], residual: RecordedGap
    ) -> None:
        resolved_at_utc = (
            await connection.execute(
                sa.text(
                    """
                    SELECT resolved_at_utc
                      FROM coverage_gap
                     WHERE market = :market AND dataset = :dataset AND symbol = :symbol
                       AND bar_interval = :bar_interval
                       AND gap_start_utc = :gap_start_utc AND gap_end_utc = :gap_end_utc
                    """
                ),
                {
                    **series,
                    "gap_start_utc": residual.gap_start_utc,
                    "gap_end_utc": residual.gap_end_utc,
                },
            )
        ).scalar_one()
        if resolved_at_utc is not None:
            raise DataIntegrityError(
                f"residual [{residual.gap_start_utc.isoformat()}, "
                f"{residual.gap_end_utc.isoformat()}) collides with a gap already marked "
                f"resolved at {resolved_at_utc.isoformat()}. That range is recorded as "
                f"held and is missing again, which is a corpus that lost data rather than "
                f"a repair that can proceed"
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
    return _series_key_parameters(SeriesKey.from_coordinate(coordinate))


def _series_key_parameters(series: SeriesKey) -> dict[str, object]:
    return {
        "market": series.market.value,
        "dataset": series.dataset.value,
        "symbol": series.symbol,
        "bar_interval": series.bar_interval,
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
