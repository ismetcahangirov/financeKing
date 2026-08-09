"""The backfill loop: plan, fetch, gate, write once per partition, register, resume.

One partition at a time, sequentially, in ascending period order. Three reasons, and the
third is the one that forbids parallelism rather than merely not needing it:

1. A seam gap is the distance between one partition's last bar and the next partition's
   first, so the periods have to be finished in order for the left-hand side to exist.
2. The expensive step is not the network. Every cache hit re-hashes the archive on disk,
   and the writer re-serialises a whole month; both are CPU- and disk-bound, so
   concurrency buys much less here than the request count suggests.
3. An interruption must leave the corpus and the registry agreeing. They are made to agree
   per partition -- write the Parquet file, then commit the registry row that names its
   digest -- and interleaving partitions would multiply the windows in which they do not.

**Resume is derived, never remembered.** There is no progress file. A partition is skipped
when the registry holds a row whose coverage already reaches the run's target date, whose
`absent_archive_count` is zero, and whose `content_digest_hex` is the digest the Parquet
file on disk actually carries. All three must hold: the first two say the work was done,
and the third says the corpus still has it. A progress file can disagree with both and be
believed by neither.

**An absent archive is re-probed on every run.** Absence is a claim about upstream, and
upstream publishes a missing day sometimes. Caching a 404 would make that fix permanently
invisible, so a partition that met one is not marked complete -- it costs one eighty-byte
request per absent day on the next run, and it is the only way a corrected archive is ever
picked up.

**A gate refusal stops the run; an absent archive does not.** Those are different
conditions. A 404 is an ordinary answer about publication. A gate refusal means a format
drifted -- a boolean encoding, an epoch unit, a header row -- which is uniform across files
rather than local to one, so continuing would ingest a corpus-wide fault one file at a time
(`CLAUDE.md` section 4, `DATA_PIPELINE.md` section 11).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from typing import Final

import pyarrow as pa
import pyarrow.parquet as pq
import structlog

from fking.data.archive import (
    ArchiveFetcher,
    FetchedArchive,
)
from fking.data.backfill.plan import (
    ArchiveSeries,
    PartitionPlan,
    PlannedArchive,
    discover_earliest_archive_date,
    plan_partitions,
)
from fking.data.backfill.registry import (
    GapKind,
    IngestedFile,
    IngestRegistry,
    PartitionRecord,
    PartitionState,
    RecordedGap,
)
from fking.data.backfill.report import BackfillReport, SymbolReport
from fking.data.format_resolver import Dataset, Market, resolve_archive_format
from fking.data.loaders import (
    DEFAULT_MAX_REJECTION_FRACTION,
    IngestionSpec,
    NormalizationResult,
)
from fking.data.parquet.layout import partition_path
from fking.data.parquet.schema import CONTENT_DIGEST_KEY
from fking.data.quality import ArchiveMember, ingest_partition
from fking.data.quality.gates import CADENCE_INTERVALS, CadenceGap
from fking.platform.errors import DataIntegrityError
from fking.platform.safety.archive import ArchiveEgress, ArchiveUnavailableError

__all__ = ["BackfillRequest", "run_backfill"]

_LOG: Final = structlog.get_logger(__name__)

_ONE_DAY: Final[timedelta] = timedelta(days=1)


@dataclass(frozen=True, slots=True)
class BackfillRequest:
    """One `make ingest` invocation, fully declared.

    `today_utc` and `now_utc` are parameters rather than clock reads. The first decides
    which archives are published monthly and which daily, so a replayed run has to resolve
    the same URLs the first one did; the second is the timestamp-plausibility reference for
    every parse, and a bound re-read per file drifts mid-run.
    """

    market: Market
    dataset: Dataset
    symbols: tuple[str, ...]
    interval: str | None
    through_date: date
    today_utc: date
    now_utc: datetime
    history_floor_date: date
    write_root: Path
    max_rejection_fraction: Decimal = DEFAULT_MAX_REJECTION_FRACTION

    def series(self, symbol: str) -> ArchiveSeries:
        """This request's series identity for one of its symbols."""
        return ArchiveSeries(
            market=self.market,
            dataset=self.dataset,
            symbol=symbol,
            interval=self.interval,
        )


@dataclass(frozen=True, slots=True)
class _FetchedMember:
    """One archive that was published, paired with the plan entry that asked for it."""

    planned: PlannedArchive
    fetched: FetchedArchive


@dataclass(slots=True)
class _SymbolTally:
    """Mutable accumulator for one symbol's run, private to this module.

    Created in `_backfill_symbol`, threaded through the private partition helpers below,
    and frozen into a `SymbolReport` at that function's return. It is deliberately not a
    domain type and it never leaves this module: what escapes is the frozen report, so no
    caller can hold a reference whose meaning changes underneath it
    (`docs/rules/immutability.md`).
    """

    symbol: str
    earliest_date: date | None = None
    partitions_written: int = 0
    partitions_resumed: int = 0
    archives_ingested: int = 0
    archives_absent: int = 0
    rows_in: int = 0
    rows_out: int = 0
    rows_rejected: int = 0
    rejection_reasons: dict[str, int] = field(default_factory=dict)
    gaps_recorded: int = 0
    gaps_newly_discovered: int = 0
    gapped_duration: timedelta = timedelta(0)
    first_event_time_utc: datetime | None = None
    last_event_time_utc: datetime | None = None


async def run_backfill(
    request: BackfillRequest,
    *,
    fetcher: ArchiveFetcher,
    egress: ArchiveEgress,
    registry: IngestRegistry,
) -> BackfillReport:
    """Backfill every requested symbol and return what the run actually did.

    Raises:
        DataIntegrityError: a quality gate refused a file, a format is undeclared for a
            date in range, or archive publication is not contiguous at a symbol's start.
            Every one of these is a condition that repeats across files, so the run stops
            rather than ingesting a corpus-wide fault one archive at a time.
    """
    symbols = [
        await _backfill_symbol(symbol, request, fetcher=fetcher, egress=egress, registry=registry)
        for symbol in request.symbols
    ]
    return BackfillReport(
        market=request.market,
        dataset=request.dataset,
        interval=request.interval,
        through_date=request.through_date,
        symbols=tuple(symbols),
    )


async def _backfill_symbol(
    symbol: str,
    request: BackfillRequest,
    *,
    fetcher: ArchiveFetcher,
    egress: ArchiveEgress,
    registry: IngestRegistry,
) -> SymbolReport:
    tally = _SymbolTally(symbol=symbol)
    series = request.series(symbol)
    earliest = await discover_earliest_archive_date(
        series,
        egress=egress,
        floor_date=request.history_floor_date,
        today_utc=request.today_utc,
    )
    if earliest is None:
        _LOG.warning(
            "backfill.symbol_unpublished",
            symbol=symbol,
            market=request.market.value,
            dataset=request.dataset.value,
            floor_date=request.history_floor_date.isoformat(),
        )
        return _freeze(tally)

    tally.earliest_date = earliest
    for plan in plan_partitions(
        series,
        earliest_date=earliest,
        through_date=request.through_date,
        today_utc=request.today_utc,
    ):
        await _backfill_partition(plan, request, tally, fetcher=fetcher, registry=registry)
    return _freeze(tally)


async def _backfill_partition(
    plan: PartitionPlan,
    request: BackfillRequest,
    tally: _SymbolTally,
    *,
    fetcher: ArchiveFetcher,
    registry: IngestRegistry,
) -> None:
    state = await registry.partition_state(plan.coordinate)
    if _is_complete(state, plan, request):
        _resume(plan, request, tally, state)
        return

    present: list[_FetchedMember] = []
    absent_days: list[date] = []
    for planned in plan.archives:
        fetched = await _fetch_or_none(fetcher, planned, request)
        if fetched is None:
            absent_days.append(planned.coordinate.archive_date)
        else:
            present.append(_FetchedMember(planned=planned, fetched=fetched))

    tally.archives_absent += len(absent_days)
    if not present:
        await _register_absent_period(plan, request, tally, absent_days, registry=registry)
        return

    covered_from = min(member.planned.covered_from_date for member in present)
    covered_through = max(member.planned.covered_through_date for member in present)
    _require_no_narrowing(plan, state, covered_from=covered_from, covered_through=covered_through)

    ingested = ingest_partition(
        tuple(
            ArchiveMember(
                archive_bytes=member.fetched.path.read_bytes(),
                spec=_spec_for(member.fetched, request),
                source=member.fetched.url,
            )
            for member in present
        ),
        coordinate=plan.coordinate,
        write_root=request.write_root,
        ingested_at_utc=request.now_utc,
    )

    previous_last = await registry.last_event_before(plan.coordinate)
    gaps = (
        *_seam_gaps(previous_last, ingested.first_event_time_utc, request.interval),
        *_cadence_gaps(ingested.cadence_gaps, request.interval),
    )
    newly_discovered = await registry.record_partition(
        PartitionRecord(
            coordinate=plan.coordinate,
            grain=plan.grain,
            covered_from_date=covered_from,
            covered_through_date=covered_through,
            absent_archive_count=len(absent_days),
            first_event_time_utc=ingested.first_event_time_utc,
            last_event_time_utc=ingested.last_event_time_utc,
            content_digest_hex=ingested.write.content_digest_hex,
            parquet_path=str(ingested.write.path),
            written_at_utc=request.now_utc,
            files=tuple(
                IngestedFile(
                    archive_date=member.planned.coordinate.archive_date,
                    granularity=member.planned.granularity,
                    source_checksum_hex=member.fetched.sha256_hex,
                    normalization=outcome.normalization,
                )
                for member, outcome in zip(present, ingested.members, strict=True)
            ),
            gaps=gaps,
        ),
        discovered_at_utc=request.now_utc,
    )

    tally.partitions_written += 1
    tally.archives_ingested += len(present)
    _accumulate_gaps(tally, gaps, newly_discovered=newly_discovered)
    for outcome in ingested.members:
        _accumulate_rows(tally, outcome.normalization)
    _observe_bounds(tally, ingested.first_event_time_utc, ingested.last_event_time_utc)
    _LOG.info(
        "backfill.partition_written",
        partition=plan.label,
        path=str(ingested.write.path),
        rows_out=ingested.write.rows_written,
        archives=len(present),
        absent_archives=len(absent_days),
        gaps=len(gaps),
        rewritten=ingested.write.was_rewritten,
    )


def _is_complete(
    state: PartitionState | None, plan: PartitionPlan, request: BackfillRequest
) -> bool:
    """Whether this partition can be skipped, checked against the registry *and* the corpus.

    The required coverage is `min(plan.covered_through_date, request.through_date)`: a
    monthly archive covers days beyond a run that asked for fewer, and demanding the archive's
    own end would re-fetch a finished month on every shorter run.
    """
    if state is None or state.absent_archive_count > 0:
        return False
    if state.covered_through_date < min(plan.covered_through_date, request.through_date):
        return False
    destination = partition_path(plan.coordinate, root=request.write_root)
    return _stored_digest(destination) == state.content_digest_hex


def _resume(
    plan: PartitionPlan,
    request: BackfillRequest,
    tally: _SymbolTally,
    state: PartitionState | None,
) -> None:
    if state is None:  # pragma: no cover - _is_complete returns False for None
        raise DataIntegrityError(f"{plan.label} was resumed with no registry row")
    tally.partitions_resumed += 1
    _observe_bounds(tally, state.first_event_time_utc, state.last_event_time_utc)
    _LOG.info(
        "backfill.partition_resumed",
        partition=plan.label,
        path=str(partition_path(plan.coordinate, root=request.write_root)),
    )


def _require_no_narrowing(
    plan: PartitionPlan,
    state: PartitionState | None,
    *,
    covered_from: date,
    covered_through: date,
) -> None:
    """Refuse a rewrite that would cover less of the period than the corpus already holds.

    A partition is written whole, so the write about to happen *replaces* the Parquet file
    rather than adding to it. If this run's archives span less than the registry records --
    a deliberately narrower `--through`, or a tail day that has stopped being published --
    the difference is silently deleted from the corpus, and the only surviving evidence is
    a row count nobody is comparing against last week's.

    So it raises. Widening the run or removing the partition on purpose are both fine; doing
    it by accident, in the middle of an eight-year backfill, is not. This is the same posture
    the fetcher takes toward a cached archive that no longer matches its checksum: a quiet
    repair is how the condition stops being reported by anything.
    """
    if state is None:
        return
    if covered_from <= state.covered_from_date and covered_through >= state.covered_through_date:
        return
    raise DataIntegrityError(
        f"{plan.label} already holds "
        f"[{state.covered_from_date.isoformat()}, {state.covered_through_date.isoformat()}] "
        f"and this run covers only "
        f"[{covered_from.isoformat()}, {covered_through.isoformat()}]. A partition is written "
        f"whole, so continuing would delete the difference from the corpus. Widen the run "
        f"(--through), or delete the partition and its registry row deliberately if the "
        f"narrower range is what you want"
    )


async def _register_absent_period(
    plan: PartitionPlan,
    request: BackfillRequest,
    tally: _SymbolTally,
    absent_days: list[date],
    *,
    registry: IngestRegistry,
) -> None:
    """Record a period whose archives are all absent, without inventing a file for it.

    For a dataset with a declared cadence the absence is already described exactly by the
    seam gap between the surrounding partitions, so nothing is recorded here and the period
    is simply outside coverage. For a dataset with no cadence there is no seam to compute
    and no missing-bar count to state, so the only honest claim is that the archive was not
    published -- which is what `absent_archive` means.
    """
    if request.interval is not None:
        _LOG.info(
            "backfill.partition_absent",
            partition=plan.label,
            absent_archives=len(absent_days),
            note="described by the seam gap between the surrounding partitions",
        )
        return
    gaps = tuple(
        RecordedGap(
            gap_start_utc=_midnight(absent),
            gap_end_utc=_midnight(absent + _ONE_DAY),
            gap_kind=GapKind.ABSENT_ARCHIVE,
            missing_bar_count=None,
        )
        for absent in absent_days
    )
    newly_discovered = await registry.record_gaps(
        plan.coordinate, gaps, discovered_at_utc=request.now_utc
    )
    _accumulate_gaps(tally, gaps, newly_discovered=newly_discovered)


async def _fetch_or_none(
    fetcher: ArchiveFetcher, planned: PlannedArchive, request: BackfillRequest
) -> FetchedArchive | None:
    """Fetch one archive, or `None` where the host does not publish it.

    `ArchiveUnavailableError` is the only exception absorbed anywhere in this module, and
    it is absorbed because it is an *answer*: a symbol that had not listed yet, a day the
    exchange did not trade, a monthly file not yet published. Every other failure -- a
    checksum that mismatched twice, an unparseable sibling, a gate refusal -- propagates,
    because each is uniform across files and would otherwise be ingested one file at a time.
    """
    try:
        return await fetcher.fetch(
            planned.coordinate, today_utc=request.today_utc, granularity=planned.granularity
        )
    except ArchiveUnavailableError:
        _LOG.info(
            "backfill.archive_absent",
            symbol=planned.coordinate.symbol,
            archive_date=planned.coordinate.archive_date.isoformat(),
            granularity=planned.granularity.value,
        )
        return None


def _spec_for(fetched: FetchedArchive, request: BackfillRequest) -> IngestionSpec:
    """The parse spec for one fetched archive, with its format resolved for its own date.

    Resolved per archive rather than once per run. A backfill spanning 2024-12 and 2025-01
    crosses the spot microsecond cutover, and a format hoisted out of this loop would apply
    one side of that boundary to both -- trap 1, in the form a bulk run makes easiest to
    write (`docs/adr/0013`).
    """
    return IngestionSpec(
        coordinate=fetched.coordinate,
        archive_format=resolve_archive_format(
            market=fetched.coordinate.market,
            dataset=fetched.coordinate.dataset,
            archive_date=fetched.coordinate.archive_date,
        ),
        source_checksum_hex=fetched.sha256_hex,
        now_utc=request.now_utc,
        max_rejection_fraction=request.max_rejection_fraction,
    )


def _cadence_gaps(gaps: tuple[CadenceGap, ...], interval: str | None) -> tuple[RecordedGap, ...]:
    """Translate bracketing bar times into the missing region between them.

    `CadenceGap` names the two bars that *are* present. The registry stores the region that
    is not, so the start moves forward by one interval: a gap reported between 09:00 and
    09:05 on a 1m series is missing 09:01 through 09:04, and recording it as `[09:00, 09:05)`
    would overstate every gap in the corpus by two bars.
    """
    if interval is None:
        return ()
    duration = CADENCE_INTERVALS[interval]
    return tuple(
        RecordedGap(
            gap_start_utc=gap.after_open_time_utc + duration,
            gap_end_utc=gap.before_open_time_utc,
            gap_kind=GapKind.CADENCE,
            missing_bar_count=gap.missing_bar_count,
        )
        for gap in gaps
    )


def _seam_gaps(
    previous_last_utc: datetime | None, first_utc: datetime, interval: str | None
) -> tuple[RecordedGap, ...]:
    """The gap between the previous partition's last bar and this partition's first.

    Absent for the first partition of a series: there is no preceding bar, so nothing is
    missing -- coverage simply starts here. Recording a gap there would turn every symbol's
    listing date into a hole stretching back to the beginning of the archive.
    """
    if interval is None or previous_last_utc is None:
        return ()
    duration = CADENCE_INTERVALS[interval]
    expected_next = previous_last_utc + duration
    if first_utc <= expected_next:
        return ()
    missing, remainder = divmod(first_utc - expected_next, duration)
    return (
        RecordedGap(
            gap_start_utc=expected_next,
            gap_end_utc=first_utc,
            gap_kind=GapKind.SEAM,
            missing_bar_count=int(missing) + (1 if remainder else 0),
        ),
    )


def _stored_digest(destination: Path) -> str | None:
    """The content digest the Parquet file on disk carries, or `None`.

    A file that cannot be opened returns `None` rather than raising, so a truncated file
    left by an interrupted write is re-derived by the next run -- which is the one action an
    operator will take, and it has to work.
    """
    if not destination.is_file():
        return None
    try:
        metadata = pq.read_schema(destination).metadata
    except (pa.ArrowInvalid, OSError):
        return None
    stored = metadata.get(CONTENT_DIGEST_KEY) if metadata else None
    return None if stored is None else stored.decode()


def _midnight(day: date) -> datetime:
    return datetime(day.year, day.month, day.day, tzinfo=UTC)


def _accumulate_gaps(
    tally: _SymbolTally, gaps: tuple[RecordedGap, ...], *, newly_discovered: int
) -> None:
    tally.gaps_recorded += len(gaps)
    tally.gaps_newly_discovered += newly_discovered
    tally.gapped_duration += sum((gap.duration for gap in gaps), timedelta(0))


def _accumulate_rows(tally: _SymbolTally, normalization: NormalizationResult) -> None:
    tally.rows_in += normalization.rows_in
    tally.rows_out += normalization.rows_out
    tally.rows_rejected += normalization.rows_rejected
    for reason, tallied in normalization.rejection_reasons.items():
        if tallied:
            tally.rejection_reasons[reason.value] = (
                tally.rejection_reasons.get(reason.value, 0) + tallied
            )


def _observe_bounds(tally: _SymbolTally, first: datetime, last: datetime) -> None:
    tally.first_event_time_utc = (
        first if tally.first_event_time_utc is None else min(tally.first_event_time_utc, first)
    )
    tally.last_event_time_utc = (
        last if tally.last_event_time_utc is None else max(tally.last_event_time_utc, last)
    )


def _freeze(tally: _SymbolTally) -> SymbolReport:
    return SymbolReport(
        symbol=tally.symbol,
        earliest_archive_date=tally.earliest_date,
        partitions_written=tally.partitions_written,
        partitions_resumed=tally.partitions_resumed,
        archives_ingested=tally.archives_ingested,
        archives_absent=tally.archives_absent,
        rows_in=tally.rows_in,
        rows_out=tally.rows_out,
        rows_rejected=tally.rows_rejected,
        rejection_reasons=dict(tally.rejection_reasons),
        gaps_recorded=tally.gaps_recorded,
        gaps_newly_discovered=tally.gaps_newly_discovered,
        gapped_duration=tally.gapped_duration,
        first_event_time_utc=tally.first_event_time_utc,
        last_event_time_utc=tally.last_event_time_utc,
    )
