"""Repairing a `sequence` gap in the trade tape, one gap at a time.

A sequence gap is a claim about *records*: the venue's own monotone `aggTrade.a` says N
prints exist between two we received, and we did not receive them. That makes it
repairable in a way a `disconnect` gap is not -- and it makes the repair a walk over an
**id range**, not over a time window.

**The gap's bounds are timestamps and the fetch is not.** `coverage_gap` records
`[gap_start_utc, gap_end_utc)`, and for a loss between two prints that share a millisecond
those bounds are one millisecond apart. There is no time window to ask the venue for. What
there is, is arithmetic: the detector recorded `missing_bar_count = N` from
`next_id - previous_id - 1`, so if the corpus can supply `previous_id` the missing ids are
exactly `previous_id + 1 .. previous_id + N` and `/aggTrades?fromId=` takes them directly.

**`previous_id` comes from the corpus, and it is looked up rather than approximated.** The
gap's left bound *is* the event time of the print before the loss, so the id we want is the
largest one the corpus holds at exactly that instant -- largest because several prints can
share a millisecond, and the detector measured from the newest of them. If the corpus holds
none there, the print the gap was measured from is gone and every id derived from it would
be wrong; the gap is left open and the pass says so rather than fetching a nearby range
that looks plausible.

**The fetch overlaps both brackets, and that overlap is the seam.** `N + 2` prints from
`previous_id` means the venue's view of the two prints the corpus already holds comes back
with the missing ones, so `reconcile_trades` has something to compare. Without it the two
views of one execution are never checked against each other, which is the failure
`DATA_PIPELINE.md` section 5 names. An overlap of zero is reported rather than silently
accepted.

**Only a sealed day is repaired.** The corpus for a day still in progress is a spool, not a
partition (`fking.data.live.tape`), and reconciling against a file that is still being
appended to is reconciling against a moving target -- worse, the rewrite would race the
seal for the same path. A gap whose days are not all sealed is counted as deferred and
picked up by the next pass.

**A residual is bounded by the prints that bracket it, because an absent print has no
time.** This is the whole difference from the kline repair, which recomputes its residual
from a minute lattice. A print we still do not hold has a known id and an unknown instant,
so the only honest bounds for a run of absent ids are the event times of the recovered or
held prints either side of it. Runs are maximal, so those two prints always exist.

**A partition is rewritten whole, and the rows already in it keep their own `source`.** A
repaired day holds prints the socket delivered and prints REST returned afterwards, which
is exactly what `source` exists to tell apart, so the merge goes out through
`write_sourced_records` rather than being stamped with one value.

The clock is injected. A repair that read `datetime.now(UTC)` could not be replayed, and
`resolved_at_utc` -- the instant a range stopped being refused -- would be unreproducible
from the audit trail.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from typing import Final
from uuid import uuid4

from sqlalchemy.ext.asyncio import AsyncEngine

from fking.data.archive import ArchiveCoordinate
from fking.data.backfill.agg_trades import AggTradeRestSource
from fking.data.backfill.registry import (
    NO_INTERVAL,
    STREAM_TIMESTAMP_RESOLUTION,
    GapKind,
    GapResolution,
    IngestRegistry,
    OpenGap,
    RecordedGap,
    SeriesKey,
)
from fking.data.backfill.seam import reconcile_trades
from fking.data.format_resolver import Dataset, Market
from fking.data.loaders.records import TradeRecord
from fking.data.parquet.layout import partition_path
from fking.data.parquet.records import read_partition_trade_window, read_partition_trades
from fking.data.parquet.schema import RecordSource
from fking.data.parquet.writer import SourcedRecord, write_sourced_records
from fking.platform.correlation import correlation_scope
from fking.platform.logging import get_logger

__all__ = ["TRADE_BACKFILLABLE_GAP_KINDS", "TradeBackfillOutcome", "TradeGapBackfiller"]

_LOG: Final = get_logger(__name__)

# The one kind that asserts a *print* is missing. `disconnect` stays out for the same
# reason it does on a kline series: it is a claim about observation, and recovering
# records does not make "nothing was being watched" false.
TRADE_BACKFILLABLE_GAP_KINDS: Final[frozenset[GapKind]] = frozenset({GapKind.SEQUENCE})


@dataclass(frozen=True, slots=True)
class TradeBackfillOutcome:
    """What one tape repair pass did, in the terms a coverage report is written in."""

    gaps_examined: int
    gaps_closed: int
    gaps_narrowed: int
    # A gap the endpoint had nothing for. Separate from `gaps_narrowed`, because
    # recovering zero prints is not narrowing: the row is left exactly as it was, keeping
    # its original discovery instant.
    gaps_unrepaired: int
    # A gap whose days are not all sealed yet, so the corpus for them is still a spool.
    # Separate again, and the distinction matters: unrepaired means the venue had nothing,
    # deferred means we did not ask.
    gaps_deferred: int
    prints_written: int
    prints_still_missing: int


def _system_now_utc() -> datetime:
    return datetime.now(UTC)


class TradeGapBackfiller:
    """Repairs one venue's tape sequence gaps from its public `aggTrades` endpoint."""

    __slots__ = ("_clock", "_corpus_root", "_engine", "_market", "_registry", "_source")

    def __init__(
        self,
        *,
        engine: AsyncEngine,
        market: Market,
        source: AggTradeRestSource,
        corpus_root: Path,
        clock: Callable[[], datetime] = _system_now_utc,
    ) -> None:
        self._engine = engine
        self._market = market
        self._source = source
        self._corpus_root = corpus_root
        self._registry = IngestRegistry(engine)
        self._clock = clock

    async def run(self, symbols: Sequence[str]) -> TradeBackfillOutcome:
        """Repair every backfillable tape gap for `symbols`, oldest first.

        Raises:
            SeamDisagreementError: the corpus and the venue describe one execution
                differently. Nothing from that seam is written and the pass stops.
            DataUnavailableError: the endpoint refused.
            DataIntegrityError: a partition cannot be read, or a recovered print falls
                outside the gap it was fetched for.
        """
        tally = _Tally()
        with correlation_scope(uuid4()):
            for symbol in symbols:
                series = SeriesKey(
                    market=self._market,
                    dataset=Dataset.AGG_TRADES,
                    symbol=symbol,
                    bar_interval=NO_INTERVAL,
                )
                for open_gap in await self._registry.open_gaps(series):
                    if open_gap.gap.gap_kind not in TRADE_BACKFILLABLE_GAP_KINDS:
                        continue
                    tally.examined += 1
                    await self._repair(open_gap, tally)
        return tally.outcome()

    async def _repair(self, open_gap: OpenGap, tally: _Tally) -> None:
        gap = open_gap.gap
        series = open_gap.series
        # Two passes over the same days, and the split is what keeps a repair affordable.
        # This one asks only for the prints at the gap's left bound, so the id the whole
        # repair is derived from costs a predicate rather than a day of dataclasses.
        anchors = self._held_window(series, gap, at_utc=gap.gap_start_utc, venue_trade_ids=())
        if anchors is None:
            tally.deferred += 1
            _LOG.info(
                "backfill.tape_gap_deferred",
                symbol=series.symbol,
                gap_start_utc=gap.gap_start_utc.isoformat(),
                gap_end_utc=gap.gap_end_utc.isoformat(),
                reason="a day this gap spans has not been sealed into a partition yet",
            )
            return

        previous_id = _left_bracket_id(anchors, gap, symbol=series.symbol)
        if previous_id is None:
            tally.unrepaired += 1
            return

        missing_count = gap.missing_bar_count or 0
        window_ids = tuple(str(previous_id + step) for step in range(missing_count + 2))
        window = self._held_window(
            series, gap, at_utc=gap.gap_start_utc, venue_trade_ids=window_ids
        )
        # The days were all present a moment ago and a repair pass is the only writer, so
        # `None` here would mean a partition disappeared mid-gap.
        held = {} if window is None else {entry.record.venue_trade_id: entry for entry in window}
        held_in_window = tuple(
            held[venue_trade_id].record for venue_trade_id in window_ids if venue_trade_id in held
        )
        fetched = await self._source.agg_trades(
            symbol=series.symbol,
            from_id=previous_id,
            print_count=missing_count + 2,
            now_utc=self._clock(),
        )

        # Raises before anything is written if the two views contradict each other.
        seam = reconcile_trades(held_in_window, fetched)
        if seam.agreed == 0:
            _LOG.warning(
                "backfill.tape_seam_untested",
                symbol=series.symbol,
                gap_start_utc=gap.gap_start_utc.isoformat(),
                held=len(held_in_window),
                fetched=len(fetched),
            )

        recovered = tuple(
            record
            for record in seam.merged
            if record.venue_trade_id not in held
            and previous_id < int(record.venue_trade_id) <= previous_id + missing_count
        )
        tally.written += self._write(series, recovered)

        instants = {int(record.venue_trade_id): record.event_time_utc for record in seam.merged}
        residuals = _residual_gaps(
            gap,
            previous_id=previous_id,
            missing_count=missing_count,
            # Held *and* recovered, not recovered alone. A gap row is frozen when the
            # detector writes it while the tape keeps arriving, so the corpus can already
            # hold a print inside an open gap -- and counting it as absent would
            # re-declare a print missing that is on disk.
            present_ids=frozenset(int(venue_trade_id) for venue_trade_id in held)
            | frozenset(int(record.venue_trade_id) for record in recovered),
            instants=instants,
        )
        tally.still_missing += sum(residual.missing_bar_count or 0 for residual in residuals)

        if not recovered:
            # The endpoint had nothing inside this gap. Leaving the row untouched is the
            # honest outcome: marking it `superseded` and re-inserting its own bounds
            # would deduplicate to the same row while resolving it, and the gap would
            # vanish from every coverage query without a print having been recovered.
            tally.unrepaired += 1
            _LOG.warning(
                "backfill.tape_gap_unrepaired",
                symbol=series.symbol,
                gap_start_utc=gap.gap_start_utc.isoformat(),
                gap_end_utc=gap.gap_end_utc.isoformat(),
                missing_prints=missing_count,
                fetched=len(fetched),
            )
            return

        resolution = await self._registry.resolve_gap(
            open_gap, residuals, resolved_at_utc=self._clock()
        )
        if resolution is GapResolution.BACKFILLED:
            tally.closed += 1
        else:
            tally.narrowed += 1
        _LOG.info(
            "backfill.tape_gap_repaired",
            symbol=series.symbol,
            gap_start_utc=gap.gap_start_utc.isoformat(),
            gap_end_utc=gap.gap_end_utc.isoformat(),
            resolution=resolution.value,
            seam_prints_agreed=seam.agreed,
            prints_written=len(recovered),
            prints_still_missing=sum(residual.missing_bar_count or 0 for residual in residuals),
        )

    def _held_window(
        self,
        series: SeriesKey,
        gap: RecordedGap,
        *,
        at_utc: datetime,
        venue_trade_ids: Sequence[str],
    ) -> tuple[SourcedRecord[TradeRecord], ...] | None:
        """The prints at `at_utc` and under `venue_trade_ids`, across the gap's days.

        `None` means at least one of those days has no partition, which on a live series
        means it has not been sealed yet. That is a "come back later", not a failure: the
        prints are in a spool and will be in a partition after the next rollover, and
        repairing against a spool that is still being appended to would reconcile against
        a moving target.
        """
        window: list[SourcedRecord[TradeRecord]] = []
        for day in _days_spanned(gap):
            path = partition_path(_coordinate(series, day), root=self._corpus_root)
            if not path.is_file():
                return None
            window.extend(
                read_partition_trade_window(
                    path, at_utc=at_utc, venue_trade_ids=frozenset(venue_trade_ids)
                )
            )
        return tuple(window)

    def _write(self, series: SeriesKey, recovered: Sequence[TradeRecord]) -> int:
        """Merge the recovered prints into each day's partition and rewrite it whole.

        Per day rather than per gap: a print's partition comes from its own event time,
        and a gap that straddles midnight recovers prints on both sides of it.

        This is the one place the whole partition is materialised, and it is unavoidable:
        a Parquet file is rewritten whole, so every row it will still hold has to be
        handed to the writer. It runs only when there is something to write, which is why
        the analysis above reads through a predicate instead.
        """
        written = 0
        for day, day_prints in _group_by_day(recovered).items():
            path = partition_path(_coordinate(series, day), root=self._corpus_root)
            existing = read_partition_trades(path)
            by_id: dict[str, RecordSource] = {
                entry.record.venue_trade_id: entry.source for entry in existing
            }
            for record in day_prints:
                # Not `stream`. A print recovered hours after the fact has different
                # latency and a different reconciler behind it, and the column exists to
                # say which.
                by_id[record.venue_trade_id] = RecordSource.REST_BACKFILL
            merged = reconcile_trades(tuple(entry.record for entry in existing), day_prints).merged
            write_sourced_records(
                tuple(SourcedRecord(record, by_id[record.venue_trade_id]) for record in merged),
                coordinate=_coordinate(series, day),
                ingested_at_utc=self._clock(),
                root=self._corpus_root,
            )
            written += len(day_prints)
        return written


@dataclass(slots=True)
class _Tally:
    """Running counts for one pass. Mutable infrastructure, never a domain object."""

    examined: int = 0
    closed: int = 0
    narrowed: int = 0
    unrepaired: int = 0
    deferred: int = 0
    written: int = 0
    still_missing: int = 0

    def outcome(self) -> TradeBackfillOutcome:
        return TradeBackfillOutcome(
            gaps_examined=self.examined,
            gaps_closed=self.closed,
            gaps_narrowed=self.narrowed,
            gaps_unrepaired=self.unrepaired,
            gaps_deferred=self.deferred,
            prints_written=self.written,
            prints_still_missing=self.still_missing,
        )


def _coordinate(series: SeriesKey, day: date) -> ArchiveCoordinate:
    return ArchiveCoordinate(
        market=series.market, dataset=series.dataset, symbol=series.symbol, archive_date=day
    )


def _days_spanned(gap: RecordedGap) -> tuple[date, ...]:
    """Every UTC day a print inside this gap could belong to.

    The end bound is inclusive of its own day even though the interval is half-open,
    because the print bracketing the gap on the right carries exactly `gap_end_utc` when
    the two prints do not share a millisecond -- and that print is the one the id
    arithmetic needs.
    """
    first = gap.gap_start_utc.date()
    last = gap.gap_end_utc.date()
    span = (last - first).days
    return tuple(first + timedelta(days=step) for step in range(span + 1))


def _group_by_day(records: Sequence[TradeRecord]) -> Mapping[date, tuple[TradeRecord, ...]]:
    grouped: dict[date, list[TradeRecord]] = {}
    for record in records:
        grouped.setdefault(record.event_time_utc.date(), []).append(record)
    return {day: tuple(day_records) for day, day_records in grouped.items()}


def _left_bracket_id(
    anchors: Sequence[SourcedRecord[TradeRecord]], gap: RecordedGap, *, symbol: str
) -> int | None:
    """The aggregate id of the print the gap was measured from, or `None`.

    The gap's left bound is that print's own event time, so the id is the largest one the
    corpus holds at exactly that instant -- `max`, because several prints can share a
    millisecond, and the one the detector measured from is the newest of them: it saw this
    print immediately before the jump, so nothing with a higher id can carry the same
    instant.

    `None` when the corpus holds no print at exactly that instant. Every id in the repair
    is derived from this one, so falling back to a nearby print would fetch a plausible
    range that is not the missing one, write it, and close the gap over prints that are
    still absent.
    """
    at_the_bound = [
        int(entry.record.venue_trade_id)
        for entry in anchors
        if entry.record.event_time_utc == gap.gap_start_utc
    ]
    if not at_the_bound:
        _LOG.warning(
            "backfill.tape_gap_left_bracket_missing",
            symbol=symbol,
            gap_start_utc=gap.gap_start_utc.isoformat(),
            reason="the print this gap was measured from is not in the corpus",
        )
        return None
    return max(at_the_bound)


def _residual_gaps(
    gap: RecordedGap,
    *,
    previous_id: int,
    missing_count: int,
    present_ids: frozenset[int],
    instants: Mapping[int, datetime],
) -> tuple[RecordedGap, ...]:
    """What is still missing inside `gap`, as maximal runs of absent aggregate ids.

    Bounded by the prints either side of each run rather than by arithmetic on the parent
    gap: an absent print has an id and no instant, so the only times available are the
    ones the bracketing prints carry. A maximal run always has both -- the id below it and
    the id above it are present by definition.

    "Present" is what the corpus holds now, which is not the same as what this pass
    recovered. The gap's `missing_bar_count` was fixed when the detector wrote the row and
    the tape has kept arriving since, so a print inside the gap can already be on disk --
    and a residual computed from this pass's recoveries alone would declare it missing
    again and report one run where there are two.

    Where both brackets share a millisecond the residual is one millisecond wide, matching
    what the detector does for the same reason: the venue's event times have no finer
    resolution, and a zero-width row is refused by the table's own CHECK.

    The kind is inherited. A print missing for a sequence reason is still missing for that
    reason after a repair that could not reach it.
    """
    residuals: list[RecordedGap] = []
    run_start_id: int | None = None

    for step in range(1, missing_count + 2):
        candidate_id = previous_id + step
        still_missing = step <= missing_count and candidate_id not in present_ids
        if still_missing:
            if run_start_id is None:
                run_start_id = candidate_id
            continue
        if run_start_id is not None:
            residuals.append(
                _residual(
                    gap,
                    run_start_id=run_start_id,
                    run_end_id=candidate_id - 1,
                    instants=instants,
                )
            )
            run_start_id = None
    return tuple(residuals)


def _residual(
    gap: RecordedGap,
    *,
    run_start_id: int,
    run_end_id: int,
    instants: Mapping[int, datetime],
) -> RecordedGap:
    opened_at = instants[run_start_id - 1]
    closed_at = max(instants[run_end_id + 1], opened_at + STREAM_TIMESTAMP_RESOLUTION)
    return RecordedGap(
        gap_start_utc=opened_at,
        # Clamped to the parent's bounds so a one-millisecond widening at the right edge
        # cannot produce a residual reaching past the region it narrows, which the
        # registry refuses outright.
        gap_end_utc=min(closed_at, gap.gap_end_utc),
        gap_kind=gap.gap_kind,
        missing_bar_count=run_end_id - run_start_id + 1,
    )
