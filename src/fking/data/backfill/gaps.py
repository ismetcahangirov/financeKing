"""Repairing a detected kline gap from the venue's REST endpoint, one gap at a time.

The walk is deliberately boring, and every interesting decision in it is a refusal.

**Only `cadence` and `seam` gaps are repaired.** Those are claims about *bars*: an
interval says how many should be there and they are not, so filling them makes the claim
false. A `disconnect` gap is a claim about *observation* -- "nothing was being watched
between these two instants" -- and fetching the bars does not make that untrue, nor does
it recover the trade prints from the same window. Marking a disconnect gap resolved
because its bars arrived would be the reassuring answer to a question nobody asked.
`sequence` and `absent_archive` gaps are not kline claims at all -- a `sequence` gap is a
claim about the tape, and it is repaired by `fking.data.backfill.trade_gaps`, which pages
the venue on `fromId` because a one-millisecond gap has no time window to ask for.

**The fetch window is wider than the gap, on purpose.** One interval on each side is
requested so that the response overlaps bars the corpus already holds -- and that overlap
*is* the seam. Without it the reconciler has nothing to compare and a disagreement between
the stream's view and the venue's view of a closed bar goes undetected forever, which is
the failure `DATA_PIPELINE.md` section 5 names. An overlap of zero is reported rather than
silently accepted, because "the seam was never tested" is a weaker result than it looks.

**A gap narrows by exactly what arrived.** The residual is recomputed from the minutes the
corpus actually holds after the write, not from arithmetic on what the venue said it sent.
If REST returns forty of sixty missing minutes, twenty remain and the gap is `superseded`
rather than closed -- a partially backfilled gap recorded as closed is worse than an open
one, because the coverage report then tells `backtest` it may run.

**A seam disagreement stops everything and writes nothing**, including the bars that
agreed. `SeamDisagreementError` propagates out of `run` rather than being counted into an
outcome: a venue and a corpus that contradict each other about a final period is not a
per-gap statistic, and continuing to the next symbol would bury it in a summary line.

The clock is injected. A repair that read `datetime.now(UTC)` could not be replayed, and
`resolved_at_utc` -- the instant a range stopped being refused -- would be unreproducible
from the audit trail.
"""

from __future__ import annotations

from collections.abc import Callable, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Final
from uuid import UUID, uuid4

from sqlalchemy.ext.asyncio import AsyncEngine

from fking.data.backfill.registry import (
    GapKind,
    GapResolution,
    IngestRegistry,
    OpenGap,
    RecordedGap,
    SeriesKey,
)
from fking.data.backfill.rest import KlineRestSource
from fking.data.backfill.seam import reconcile_klines
from fking.data.bars import INSERT_BAR, bar_parameters, read_bars, resolve_instrument_ids
from fking.data.format_resolver import Dataset, Market
from fking.data.loaders.records import KlineRecord
from fking.data.parquet.schema import RecordSource
from fking.platform.correlation import correlation_scope
from fking.platform.errors import DataIntegrityError
from fking.platform.logging import get_logger

__all__ = ["KLINE_BACKFILLABLE_GAP_KINDS", "BackfillOutcome", "KlineGapBackfiller"]

_LOG: Final = get_logger(__name__)

# The two kinds that assert a *bar* is missing. See the module docstring for why
# `disconnect` is not among them even on a kline series, and why `sequence` -- a claim
# about the tape rather than about bars -- belongs to `trade_gaps` instead.
#
# Per dataset rather than one set for the package: "can this gap be repaired at all",
# which the coverage report asks, is a different question from "does *this* backfiller
# repair it", and collapsing them is how a sequence gap ends up offered to a walk over a
# minute lattice. `fking.data.backfill.BACKFILLABLE_GAP_KINDS` is the union.
KLINE_BACKFILLABLE_GAP_KINDS: Final[frozenset[GapKind]] = frozenset({GapKind.CADENCE, GapKind.SEAM})

_BAR_INTERVALS: Final[dict[str, timedelta]] = {"1m": timedelta(minutes=1)}


@dataclass(frozen=True, slots=True)
class BackfillOutcome:
    """What one repair pass did, in the terms a coverage report is written in."""

    gaps_examined: int
    gaps_closed: int
    gaps_narrowed: int
    # A gap the endpoint had nothing for. Counted separately rather than folded into
    # `gaps_narrowed`, because narrowing zero minutes is not narrowing: the row is left
    # exactly as it was, keeping its original discovery instant, and a pass that reports
    # only "examined 12, closed 0" cannot say whether that is a venue with no history or
    # a repair that never ran.
    gaps_unrepaired: int
    bars_written: int
    minutes_still_missing: int


def _system_now_utc() -> datetime:
    return datetime.now(UTC)


class KlineGapBackfiller:
    """Repairs one venue's kline gaps from its public REST endpoint."""

    __slots__ = ("_clock", "_engine", "_market", "_registry", "_source", "_venue_id")

    def __init__(
        self,
        *,
        engine: AsyncEngine,
        venue_id: str,
        market: Market,
        source: KlineRestSource,
        clock: Callable[[], datetime] = _system_now_utc,
    ) -> None:
        # `venue_id` and `market` rather than the whole `LiveStreamProfile` they come
        # from: this module would otherwise import `fking.data.live`, whose writer imports
        # this package's registry, and the two `__init__` files would have to be loaded in
        # a particular order forever. The caller holds the profile and unpacks it.
        self._engine = engine
        self._venue_id = venue_id
        self._market = market
        self._source = source
        self._registry = IngestRegistry(engine)
        self._clock = clock

    async def run(self, symbols: Sequence[str], *, bar_interval: str) -> BackfillOutcome:
        """Repair every backfillable kline gap for `symbols`, oldest first.

        Raises:
            SeamDisagreementError: the corpus and the venue describe a closed bar
                differently. Nothing from that seam is written and the pass stops.
            DataUnavailableError: a symbol has no instrument row, or the endpoint refused.
        """
        interval = _BAR_INTERVALS.get(bar_interval)
        if interval is None:
            raise DataIntegrityError(
                f"no duration is declared for bar interval {bar_interval!r}; the repair "
                f"walks a minute lattice and cannot infer one from the string"
            )
        instrument_ids = await resolve_instrument_ids(
            self._engine, venue_id=self._venue_id, symbols=symbols
        )

        examined = closed = narrowed = unrepaired = written = still_missing = 0
        with correlation_scope(uuid4()):
            for symbol in symbols:
                series = SeriesKey(
                    market=self._market,
                    dataset=Dataset.KLINES,
                    symbol=symbol,
                    bar_interval=bar_interval,
                )
                for open_gap in await self._registry.open_gaps(series):
                    if open_gap.gap.gap_kind not in KLINE_BACKFILLABLE_GAP_KINDS:
                        continue
                    examined += 1
                    repaired = await self._repair(
                        open_gap,
                        instrument_id=instrument_ids[symbol],
                        bar_interval=bar_interval,
                        interval=interval,
                    )
                    written += repaired.bars_written
                    still_missing += repaired.minutes_still_missing
                    if repaired.resolution is GapResolution.BACKFILLED:
                        closed += 1
                    elif repaired.resolution is GapResolution.SUPERSEDED:
                        narrowed += 1
                    else:
                        unrepaired += 1

        return BackfillOutcome(
            gaps_examined=examined,
            gaps_closed=closed,
            gaps_narrowed=narrowed,
            gaps_unrepaired=unrepaired,
            bars_written=written,
            minutes_still_missing=still_missing,
        )

    async def _repair(
        self,
        open_gap: OpenGap,
        *,
        instrument_id: UUID,
        bar_interval: str,
        interval: timedelta,
    ) -> _RepairedGap:
        gap = open_gap.gap
        symbol = open_gap.series.symbol
        # One interval on each side: the seam. The corpus is expected to hold the bar
        # bracketing each edge, and comparing those two against the venue's view of them
        # is the only check that the two sources describe the same market at all.
        fetch_from_utc = gap.gap_start_utc - interval
        fetch_until_utc = gap.gap_end_utc + interval

        fetched = await self._source.klines(
            symbol=symbol,
            interval=bar_interval,
            from_utc=fetch_from_utc,
            until_utc=fetch_until_utc,
            now_utc=self._clock(),
        )
        async with self._engine.connect() as connection:
            held = await read_bars(
                connection,
                instrument_id=instrument_id,
                timeframe=bar_interval,
                from_utc=fetch_from_utc,
                until_utc=fetch_until_utc,
            )

        # Raises before anything is written if the two views contradict each other.
        seam = reconcile_klines(held, fetched)
        if seam.agreed == 0:
            _LOG.warning(
                "backfill.seam_untested",
                symbol=symbol,
                gap_start_utc=gap.gap_start_utc.isoformat(),
                gap_end_utc=gap.gap_end_utc.isoformat(),
                held=len(held),
                fetched=len(fetched),
            )

        # From the merged view, not from the fetched page. The reconciler is what proves
        # the two sources describe one market, so writing anything it did not produce
        # would put records on disk that were never compared. Bars the corpus already
        # held are in here too and their insert is a no-op -- `ON CONFLICT DO NOTHING`
        # keeps the row and its original `source`.
        inside_gap = tuple(
            record
            for record in seam.merged
            if gap.gap_start_utc <= record.open_time_utc < gap.gap_end_utc
        )
        bars_written = await self._write(
            inside_gap, instrument_id=instrument_id, bar_interval=bar_interval
        )

        residuals = _residual_gaps(
            gap,
            recovered_open_times=frozenset(record.open_time_utc for record in inside_gap),
            interval=interval,
        )
        still_missing = sum(residual.missing_bar_count or 0 for residual in residuals)
        if _narrows_nothing(gap, residuals):
            # The endpoint had nothing for this window. Leaving the row untouched is the
            # honest outcome: marking it `superseded` and re-inserting its own bounds
            # would deduplicate to the same row while resolving it, and the gap would
            # vanish from every coverage query without a single bar having been recovered.
            _LOG.warning(
                "backfill.gap_unrepaired",
                venue_id=self._venue_id,
                symbol=symbol,
                gap_start_utc=gap.gap_start_utc.isoformat(),
                gap_end_utc=gap.gap_end_utc.isoformat(),
                gap_kind=gap.gap_kind.value,
                fetched=len(fetched),
                minutes_still_missing=still_missing,
            )
            return _RepairedGap(
                resolution=None, bars_written=bars_written, minutes_still_missing=still_missing
            )

        resolution = await self._registry.resolve_gap(
            open_gap, residuals, resolved_at_utc=self._clock()
        )
        _LOG.info(
            "backfill.gap_repaired",
            venue_id=self._venue_id,
            symbol=symbol,
            gap_start_utc=gap.gap_start_utc.isoformat(),
            gap_end_utc=gap.gap_end_utc.isoformat(),
            gap_kind=gap.gap_kind.value,
            resolution=resolution.value,
            seam_bars_agreed=seam.agreed,
            bars_written=bars_written,
            minutes_still_missing=still_missing,
        )
        return _RepairedGap(
            resolution=resolution,
            bars_written=bars_written,
            minutes_still_missing=still_missing,
        )

    async def _write(
        self,
        records: Sequence[KlineRecord],
        *,
        instrument_id: UUID,
        bar_interval: str,
    ) -> int:
        if not records:
            return 0
        inserted = 0
        async with self._engine.begin() as connection:
            for record in records:
                row = (
                    await connection.execute(
                        INSERT_BAR,
                        bar_parameters(
                            record,
                            instrument_id=instrument_id,
                            timeframe=bar_interval,
                            # Not `stream`. A bar recovered hours after the fact has
                            # different latency and a different reconciler behind it, and
                            # the column exists to say which.
                            source=RecordSource.REST_BACKFILL,
                        ),
                    )
                ).first()
                if row is not None:
                    inserted += 1
        return inserted


@dataclass(frozen=True, slots=True)
class _RepairedGap:
    """`resolution` is `None` where the gap was left exactly as it was found."""

    resolution: GapResolution | None
    bars_written: int
    minutes_still_missing: int


def _narrows_nothing(gap: RecordedGap, residuals: Sequence[RecordedGap]) -> bool:
    """The residual set still covers the whole gap, so nothing inside it was recovered."""
    return len(residuals) == 1 and (
        residuals[0].gap_start_utc,
        residuals[0].gap_end_utc,
    ) == (gap.gap_start_utc, gap.gap_end_utc)


def _residual_gaps(
    gap: RecordedGap,
    *,
    recovered_open_times: frozenset[datetime],
    interval: timedelta,
) -> tuple[RecordedGap, ...]:
    """What is still missing inside `gap`, as maximal runs of absent intervals.

    Computed from the minute lattice and the set of open times the corpus now holds,
    rather than from a count of what the venue returned: the venue can return a bar the
    corpus already had, and subtracting page sizes would then close a gap that is still
    holed. Contiguous absent minutes collapse into one row for the same reason the cadence
    detector collapses them -- ten silent minutes were one outage, not ten.

    The residual's kind is inherited from the gap it narrows. A minute that was missing
    for a cadence reason is still missing for that reason after a repair that could not
    reach it, and inventing a new kind would make the reason a function of how often a
    backfill has been attempted.
    """
    residuals: list[RecordedGap] = []
    run_start: datetime | None = None
    run_length = 0

    candidate = gap.gap_start_utc
    while candidate < gap.gap_end_utc:
        if candidate in recovered_open_times:
            if run_start is not None:
                residuals.append(_residual(gap, run_start, run_length, interval))
                run_start, run_length = None, 0
        else:
            if run_start is None:
                run_start = candidate
            run_length += 1
        candidate += interval

    if run_start is not None:
        residuals.append(_residual(gap, run_start, run_length, interval))
    return tuple(residuals)


def _residual(
    gap: RecordedGap, run_start_utc: datetime, run_length: int, interval: timedelta
) -> RecordedGap:
    return RecordedGap(
        gap_start_utc=run_start_utc,
        # Clamped to the parent's end so that a gap whose bounds are not a whole number of
        # intervals -- which the registry admits, because a seam gap is bounded by two
        # observations rather than by the lattice -- cannot produce a residual reaching
        # past the region it narrows.
        gap_end_utc=min(run_start_utc + interval * run_length, gap.gap_end_utc),
        gap_kind=gap.gap_kind,
        missing_bar_count=run_length,
    )
