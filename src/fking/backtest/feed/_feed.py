"""The market-data event source, and the gate that refuses a window it cannot serve.

The whole module is one sentence from `BACKTEST_ENGINE.md` section 9 made executable:

> Missing bars in the window -- do not interpolate. Report coverage; narrow the window or
> refuse.

There is no `fill_method`, no `allow_gaps`, no `max_missing_bar_count`. Each of those is a
configuration value whose only purpose is to let a run proceed on bars that were never
observed, and a guard that can be turned off in a config file is not a guard -- it is a
documented procedure for turning it off, used by whoever is in a hurry
(`docs/rules/safety-kernel.md` makes the same argument about the host allowlist).

The reason interpolation is refused rather than merely discouraged is worth keeping next to
the code that refuses it. A forward-filled bar repeats a price at a timestamp where no trade
happened; a linearly interpolated one invents a price that existed nowhere at all. Both are
*tradable* in a simulation: a breakout strategy sees the synthetic level cross its threshold
and is filled at it, and because interpolation is smooth while real markets are not, the
phantom moves are systematically kinder than real ones. The result is not noisy, it is
biased upward, and it looks exactly like a result from a complete corpus.

**Emitted bar count equals archive bar count, always.** `FeedSlice.archive_bar_count` is
taken from the rows the corpus returned and `events` is built one-for-one from them, so the
two can be asserted equal by a test rather than reasoned about. A feed that ever synthesised
a bar would break that equality, which is the point of reporting the number at all.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

from fking.backtest._config import canonical_digest
from fking.backtest._events import MarketDataEvent
from fking.backtest.feed._corpus import ArchiveBar, SeriesRead, read_series
from fking.backtest.feed._coverage import (
    CoverageReport,
    SymbolCoverage,
    gaps_against,
)
from fking.backtest.feed._errors import CorpusIntegrityError, CoverageRefusedError
from fking.backtest.feed._request import FeedRequest, SeriesRequest
from fking.domain import Bar, encode

__all__ = ["FeedSlice", "MarketDataFeed"]


@dataclass(frozen=True, slots=True)
class FeedSlice:
    """The events one window produces, with the coverage that justifies serving them.

    `events` is ordered by `(occurs_at_utc, series label)` and is what
    `EventLoop.run(initial_events)` is fed. The engine's queue re-derives its own total
    order from `(instant, priority, sequence)`, and `sequence` is insertion order -- so the
    order here is load-bearing for the trace even though the queue would accept any.
    """

    events: tuple[MarketDataEvent, ...]
    warmup_event_count: int
    archive_bar_count: int
    exposed_from_utc: datetime
    coverage: CoverageReport

    @property
    def exposed_event_count(self) -> int:
        """How many bars reach strategy evaluation."""
        return len(self.events) - self.warmup_event_count

    @property
    def first_event_utc(self) -> datetime | None:
        """The instant the run's first event is dispatched at, or `None` for an empty slice."""
        return self.events[0].occurs_at_utc if self.events else None

    @property
    def last_event_utc(self) -> datetime | None:
        """The instant the run's last event is dispatched at, or `None` for an empty slice."""
        return self.events[-1].occurs_at_utc if self.events else None

    @property
    def event_sequence_digest(self) -> str:
        """SHA-256 over the canonical JSON of the whole event sequence.

        The quantity two runs of one window are compared on. It covers the events *and*
        their order, so a reordering that leaves the multiset unchanged still moves it --
        which is what makes it a check on the feed rather than on the corpus.
        """
        return canonical_digest(encode(self.events))


class MarketDataFeed:
    """Turns a window over the Parquet corpus into a time-ordered `MarketDataEvent` stream.

    `now_utc` is a constructor argument rather than a clock read, for the reason every other
    instant in this package is: the timestamp plausibility window is a function of `now`, so
    a feed that read the machine's clock would move its own boundary conditions every day it
    ran, and a run replayed next month would not be the run that was recorded.
    """

    __slots__ = ("_corpus_root", "_duckdb_thread_count", "_now_utc")

    def __init__(
        self,
        *,
        corpus_root: Path,
        now_utc: datetime,
        duckdb_thread_count: int | None = None,
    ) -> None:
        self._corpus_root = corpus_root
        self._now_utc = now_utc
        self._duckdb_thread_count = duckdb_thread_count

    def coverage(self, request: FeedRequest) -> CoverageReport:
        """What the corpus holds for `request`, without building any events.

        The pre-run report. `make backtest` prints this before anything else happens,
        because a run refused in the first second costs nothing and a run refused after four
        hours of scanning costs an afternoon.
        """
        return self._coverage_of(request, self._read_all(request))

    def load(self, request: FeedRequest) -> FeedSlice:
        """The event stream for `request`, or a refusal naming the gaps.

        Raises:
            CoverageRefusedError: at least one series is missing at least one bar the window
                names. The message is the rendered report, per symbol, with the ranges.
            AmbiguousEpochUnitError: a partition's epoch unit cannot be resolved.
            CorpusIntegrityError: the corpus holds a bar the request's lattice does not name.
        """
        reads = self._read_all(request)
        report = self._coverage_of(request, reads)
        if not report.is_servable:
            raise CoverageRefusedError(
                f"the window cannot be served from the corpus without inventing bars:\n\n"
                f"{report.render()}"
            )

        pairs: list[tuple[datetime, str, MarketDataEvent]] = []
        warmup_event_count = 0
        archive_bar_count = 0
        for entry in request.series:
            read = reads[entry.label]
            archive_bar_count += len(read.bars)
            for archive_bar in read.bars:
                event = MarketDataEvent(observation=_to_domain_bar(archive_bar, entry))
                if request.is_warmup(archive_bar.open_time_utc):
                    warmup_event_count += 1
                pairs.append((event.occurs_at_utc, entry.label, event))

        # `(instant, label)` and nothing else. Sorting on the instant alone leaves two
        # series sharing a close time ordered by whichever was read first, which is stable
        # today and is not a property of anything -- and a trace that reorders between two
        # runs of one config is the failure that outranks every other on the queue.
        ordered = tuple(event for _, _, event in sorted(pairs, key=lambda pair: (pair[0], pair[1])))
        return FeedSlice(
            events=ordered,
            warmup_event_count=warmup_event_count,
            archive_bar_count=archive_bar_count,
            exposed_from_utc=request.exposed_from_utc,
            coverage=report,
        )

    def _read_all(self, request: FeedRequest) -> dict[str, SeriesRead]:
        return {
            entry.label: read_series(
                root=self._corpus_root,
                market=entry.market,
                symbol=entry.symbol,
                bar_interval=request.bar_interval,
                from_utc=request.warmup_start_utc,
                until_utc=request.until_utc,
                now_utc=self._now_utc,
                duckdb_thread_count=self._duckdb_thread_count,
            )
            for entry in request.series
        }

    def _coverage_of(self, request: FeedRequest, reads: dict[str, SeriesRead]) -> CoverageReport:
        lattice = tuple(request.lattice())
        return CoverageReport(
            bar_interval=request.bar_interval,
            warmup_start_utc=request.warmup_start_utc,
            exposed_from_utc=request.exposed_from_utc,
            until_utc=request.until_utc,
            series=tuple(
                _coverage_for(entry, reads[entry.label], lattice=lattice, request=request)
                for entry in request.series
            ),
        )


def _coverage_for(
    entry: SeriesRequest,
    read: SeriesRead,
    *,
    lattice: Sequence[datetime],
    request: FeedRequest,
) -> SymbolCoverage:
    open_times = tuple(bar.open_time_utc for bar in read.bars)
    _require_on_lattice(entry, open_times, lattice=lattice, request=request)
    return SymbolCoverage(
        market=entry.market,
        symbol=entry.symbol,
        observed_bar_count=len(open_times),
        expected_bar_count=len(lattice),
        first_open_time_utc=open_times[0] if open_times else None,
        last_open_time_utc=open_times[-1] if open_times else None,
        gaps=gaps_against(lattice, open_times, duration=request.duration),
        partition_formats=read.partition_formats,
    )


def _require_on_lattice(
    entry: SeriesRequest,
    open_times: Sequence[datetime],
    *,
    lattice: Sequence[datetime],
    request: FeedRequest,
) -> None:
    """Every held bar must be one the request's lattice names.

    A bar off the lattice is counted as present by a naive length comparison while
    contributing nothing to any instant the window asked for, so a series can read as
    complete while a real hole sits beside a misaligned duplicate of a neighbouring bar.
    That shape is what a partition written at the wrong interval looks like -- 5m bars filed
    under `interval=1m` -- and it is invisible to a count.
    """
    stray = tuple(moment for moment in open_times if moment not in frozenset(lattice))
    if not stray:
        return
    raise CorpusIntegrityError(
        f"{entry.label} holds {len(stray)} bars that are not on the {request.bar_interval} "
        f"lattice the window names, the first at {stray[0].isoformat()}. A misaligned bar is "
        f"counted as present while contributing to no instant the request asked for, so the "
        f"series can read as complete with a real hole beside it"
    )


def _to_domain_bar(archive_bar: ArchiveBar, entry: SeriesRequest) -> Bar:
    """One corpus row as the domain object a strategy is handed.

    The instrument comes from the request rather than the file, because no archive contains
    `tick_size`, `lot_step` or `min_notional_quote`, and a feed that invented them would let
    a backtest fill quantities the venue would have refused.
    """
    return Bar(
        instrument=entry.instrument,
        open_time_utc=archive_bar.open_time_utc,
        close_time_utc=archive_bar.close_time_utc,
        open_quote_price=archive_bar.open_quote_price,
        high_quote_price=archive_bar.high_quote_price,
        low_quote_price=archive_bar.low_quote_price,
        close_quote_price=archive_bar.close_quote_price,
        base_volume=archive_bar.base_volume,
        trade_count=archive_bar.trade_count,
    )
