"""Frames in, canonical records and gaps out. No I/O, no clock of its own.

This is where every decision `DATA_PIPELINE.md` section 5 states actually happens, and
it is deliberately pure so that all of them can be asserted without a socket or a
database:

- an open kline never becomes a bar;
- an `aggTrade` id jump becomes a sized gap within one message;
- a minute with no closed bar becomes a gap once its grace window expires;
- a socket drop becomes a gap with exact bounds, from the last thing observed to the
  instant listening resumed.

The router owns one detector pair per series and the last event time per series. It does
not own the connection, does not decide when to reconnect, and does not write: the
supervisor drives it and the writer persists what it returns. That split is what lets
the disconnect case be tested at the same level as the others -- `mark_disconnected` is
an ordinary method call rather than a socket that has to be made to fail.

**Two gaps can describe one outage, and that is intended.** A three-minute drop produces
a `disconnect` gap over the unobserved window *and*, once their grace windows expire, a
`cadence` gap naming the exact minutes whose bars are absent. They answer different
questions -- "when were we not listening" and "which bars are missing" -- and the
availability contract (#30) refuses a window intersecting either, so the redundancy
costs nothing where it matters. It does mean `data_coverage.total_gapped_duration`
counts an outage twice, which is why that column is a diagnostic aggregate rather than
an accounting figure. In the rare case where both rows carry byte-identical bounds, the
registry's `ON CONFLICT DO NOTHING` keeps the first and the series is still reported as
gapped.

The `aggTrade` tape is parsed into `TradeRecord` and returned alongside the bars, and it
is **not persisted here**. There is no operational trade table -- `DATA_PIPELINE.md`
section 6 puts the tape in Parquet, written whole partitions at a time -- so a print is
spooled by `fking.data.live.tape` and becomes a Parquet row a day later, on the same
split as everything else in this package: the router decides, the writer persists.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Final

from fking.data.backfill.registry import NO_INTERVAL, GapKind, RecordedGap, SeriesKey
from fking.data.format_resolver import Dataset
from fking.data.live.detectors import CADENCE_GRACE, CadenceGapDetector, SequenceGapDetector
from fking.data.live.frames import (
    AggTradeFrame,
    BookTickerFrame,
    KlineFrame,
    MarkPriceFrame,
    parse_frame,
)
from fking.data.live.streams import LiveStreamProfile
from fking.data.loaders.records import KlineRecord, TradeRecord
from fking.platform.errors import DataIntegrityError

__all__ = ["LiveBar", "LiveGap", "LiveRouter", "LiveTrade", "RoutedFrame"]

_BAR_INTERVALS: Final[dict[str, timedelta]] = {"1m": timedelta(minutes=1)}


@dataclass(frozen=True, slots=True)
class LiveBar:
    """A closed bar, and the series it belongs to."""

    series: SeriesKey
    record: KlineRecord


@dataclass(frozen=True, slots=True)
class LiveTrade:
    """A trade print, and the series it belongs to."""

    series: SeriesKey
    record: TradeRecord


@dataclass(frozen=True, slots=True)
class LiveGap:
    """A gap, and the series it belongs to."""

    series: SeriesKey
    gap: RecordedGap


@dataclass(frozen=True, slots=True)
class RoutedFrame:
    """Everything one frame produced.

    Every field is a tuple rather than an optional single value, so a caller writes one
    loop per kind instead of a `None` check per kind -- and a frame that produces two
    gaps (a sequence jump that also closes a pending disconnect) needs no special case.
    """

    bars: tuple[LiveBar, ...] = ()
    trades: tuple[LiveTrade, ...] = ()
    gaps: tuple[LiveGap, ...] = ()
    open_klines: int = 0


@dataclass(slots=True)
class _SeriesState:
    """Per-series detection state. Mutable infrastructure, never a domain object."""

    last_event_time_utc: datetime | None = None
    disconnected_at_utc: datetime | None = None
    sequence: SequenceGapDetector | None = None
    cadence: CadenceGapDetector | None = None


class LiveRouter:
    """Routes one venue's subscribed streams for a fixed symbol set."""

    __slots__ = ("_profile", "_state")

    def __init__(
        self,
        profile: LiveStreamProfile,
        symbols: tuple[str, ...],
        *,
        started_at_utc: datetime,
        cadence_grace: timedelta = CADENCE_GRACE,
    ) -> None:
        if not symbols:
            raise ValueError("a router with no symbols would route nothing")
        self._profile = profile
        self._state: dict[SeriesKey, _SeriesState] = {}
        for symbol in symbols:
            for dataset in profile.persisted_datasets:
                series = self._series(symbol, dataset)
                state = _SeriesState()
                if dataset is Dataset.KLINES:
                    state.cadence = CadenceGapDetector(
                        interval=_BAR_INTERVALS[series.bar_interval],
                        grace=cadence_grace,
                        started_at_utc=started_at_utc,
                    )
                else:
                    state.sequence = SequenceGapDetector()
                self._state[series] = state

    @property
    def series_keys(self) -> tuple[SeriesKey, ...]:
        """Every persisted series this router tracks, in a stable order."""
        return tuple(sorted(self._state, key=lambda key: (key.dataset.value, key.symbol)))

    def route(self, raw: str, *, now_utc: datetime) -> RoutedFrame:
        """One raw frame to whatever it produced.

        `now_utc` is the ingestion instant, used only as the plausibility reference for
        epoch normalisation. It is a parameter rather than a `datetime.now(UTC)` call so
        that replaying a recorded fixture years later normalises the same integers the
        same way (`.claude/rules/time-and-timezones.md`).
        """
        frame = parse_frame(raw)
        if isinstance(frame, KlineFrame):
            return self._route_kline(frame, now_utc=now_utc)
        if isinstance(frame, AggTradeFrame):
            return self._route_agg_trade(frame, now_utc=now_utc)
        # bookTicker and markPrice are subscribed for the live risk path and persist
        # nothing, so they produce no records -- but they are still evidence the socket
        # is alive, which is why they are parsed rather than filtered before parsing.
        if isinstance(frame, BookTickerFrame | MarkPriceFrame):
            return RoutedFrame()
        raise DataIntegrityError(f"no route for frame type {type(frame).__name__}")

    def poll(self, now_utc: datetime) -> tuple[LiveGap, ...]:
        """Cadence gaps whose grace window has now expired.

        Called on a timer by the supervisor rather than on frame arrival, because the
        condition being detected is the *absence* of arrivals -- a detector driven only
        by messages cannot see a stream that is connected and silent.
        """
        gaps: list[LiveGap] = []
        for series, state in self._state.items():
            if state.cadence is None:
                continue
            gaps.extend(LiveGap(series, gap) for gap in state.cadence.poll(now_utc))
        return tuple(gaps)

    def mark_disconnected(self, at_utc: datetime) -> None:
        """Open a pending disconnect gap on every persisted series.

        The left edge is the newest event observed on that series, falling back to the
        drop instant when the series has produced nothing yet. Taking the last event
        rather than the drop instant is what makes the gap cover a socket that died
        quietly: a connection killed by a missed pong surfaces as an error up to ten
        minutes after the last frame, and anchoring on the error would report ten
        minutes of unobserved market as observed.

        Idempotent. A second drop before the first gap has closed keeps the earlier left
        edge, because the unobserved region runs from the first drop.
        """
        for state in self._state.values():
            if state.disconnected_at_utc is None:
                state.disconnected_at_utc = state.last_event_time_utc or at_utc

    def close_pending_gaps(self, at_utc: datetime) -> tuple[LiveGap, ...]:
        """Close every open disconnect gap at `at_utc`, which is a wall-clock instant.

        Called on reconnect and again on shutdown. Both edges are wall clock -- the last
        thing we saw, and the moment we resumed listening -- rather than the event time
        of the first frame after the reconnect. That distinction is not cosmetic: a
        kline's event time is the bar's *open*, up to a full interval before the frame
        arrived, so closing on it would routinely produce a gap that ends before it
        starts and the router would refuse a reconnect that was entirely healthy.

        A supervisor that stops while disconnected must call this too. Without it the
        outage goes unrecorded through the door marked "we were about to reconnect",
        which is the rule failing in the one case it was written for.

        Raises:
            DataIntegrityError: the closing instant is not after the opening one. Only a
                clock that went backwards produces that, and a live ingester whose clock
                jumps is recording timestamps nothing can be reconciled against.
        """
        gaps: list[LiveGap] = []
        for series, state in self._state.items():
            opened_at = state.disconnected_at_utc
            if opened_at is None:
                continue
            state.disconnected_at_utc = None
            if at_utc <= opened_at:
                raise DataIntegrityError(
                    f"{series.symbol} {series.dataset.value} closed a disconnect gap at "
                    f"{at_utc.isoformat()}, which is not after it opened at "
                    f"{opened_at.isoformat()}. The clock went backwards across a "
                    f"reconnect; every timestamp this session writes is now suspect"
                )
            gaps.append(LiveGap(series, _disconnect_gap(opened_at, at_utc)))
        return tuple(gaps)

    def _series(self, symbol: str, dataset: Dataset) -> SeriesKey:
        return SeriesKey(
            market=self._profile.market,
            dataset=dataset,
            symbol=symbol,
            bar_interval=(
                _interval_of(self._profile) if dataset is Dataset.KLINES else NO_INTERVAL
            ),
        )

    def _state_for(self, symbol: str, dataset: Dataset) -> _SeriesState:
        series = self._series(symbol, dataset)
        state = self._state.get(series)
        if state is None:
            raise DataIntegrityError(
                f"received a {dataset.value} frame for {symbol}, which this session did "
                f"not subscribe to; subscribed series are "
                f"{[f'{key.symbol}/{key.dataset.value}' for key in self.series_keys]}"
            )
        return state

    def _route_kline(self, frame: KlineFrame, *, now_utc: datetime) -> RoutedFrame:
        state = self._state_for(frame.symbol, Dataset.KLINES)
        if not frame.is_closed:
            # Not an observation for gap purposes either. An open kline is republished
            # every second or two, so treating it as an event would let a series whose
            # bars stopped closing look continuously alive.
            return RoutedFrame(open_klines=1)

        record = frame.to_record(now_utc=now_utc)
        series = self._series(frame.symbol, Dataset.KLINES)
        self._observe(state, record.open_time_utc)
        if state.cadence is not None:
            state.cadence.observe(record.open_time_utc)
        return RoutedFrame(bars=(LiveBar(series, record),))

    def _route_agg_trade(self, frame: AggTradeFrame, *, now_utc: datetime) -> RoutedFrame:
        state = self._state_for(frame.symbol, Dataset.AGG_TRADES)
        record = frame.to_record(now_utc=now_utc)
        series = self._series(frame.symbol, Dataset.AGG_TRADES)
        self._observe(state, record.event_time_utc)
        gaps: tuple[LiveGap, ...] = ()
        if state.sequence is not None:
            sequence_gap = state.sequence.observe(frame.aggregate_trade_id, record.event_time_utc)
            if sequence_gap is not None:
                gaps = (LiveGap(series, sequence_gap),)
        return RoutedFrame(trades=(LiveTrade(series, record),), gaps=gaps)

    @staticmethod
    def _observe(state: _SeriesState, event_time_utc: datetime) -> None:
        """Record an arrival, so a later drop knows where the observed region ended.

        `max`, not assignment: a reconnect replays frames the previous session already
        saw, and letting the newest observation move backwards would make the next
        disconnect gap re-cover a period the corpus holds.
        """
        state.last_event_time_utc = (
            event_time_utc
            if state.last_event_time_utc is None
            else max(state.last_event_time_utc, event_time_utc)
        )


def _disconnect_gap(opened_at_utc: datetime, closed_at_utc: datetime) -> RecordedGap:
    return RecordedGap(
        gap_start_utc=opened_at_utc,
        gap_end_utc=closed_at_utc,
        gap_kind=GapKind.DISCONNECT,
        # NULL, not zero, and not a computed bar count. The claim is that nothing was
        # being observed over this window; how many bars that cost is the cadence
        # detector's question, and answering it here would be a second, weaker answer to
        # a question that already has an exact one.
        missing_bar_count=None,
    )


def _interval_of(profile: LiveStreamProfile) -> str:
    """The kline interval this profile subscribes to, taken from its own stream names."""
    for suffix in profile.stream_suffixes:
        if suffix.startswith("kline_"):
            return suffix.removeprefix("kline_")
    raise DataIntegrityError(
        f"{profile.venue_id} persists klines but subscribes to no kline stream: "
        f"{profile.stream_suffixes}"
    )
