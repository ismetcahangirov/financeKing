"""The two gap detectors, and why one is not a cheaper version of the other.

`DATA_PIPELINE.md` section 5 requires both, and the reason is that they fail in
different directions:

- The **sequence detector** watches `aggTrade.a` for contiguity. It reports a loss
  within one message and sizes it exactly, because `a` is a monotone integer the venue
  assigns. It is blind to a stream that is connected and sending nothing, because
  silence produces no message to compare against.
- The **cadence detector** requires a closed bar per minute and reports a minute whose
  grace window has expired. It catches exactly the case the sequence detector cannot,
  and it is driven by the clock rather than by arrivals -- which is why `poll` takes
  `now_utc` and the class reads no clock of its own.

Both are pure state machines over injected time. Neither writes anything: they return
`RecordedGap` values, which the router attaches to a series and the writer persists.
That split is what lets the whole detection surface be tested without a database, and
it is why a gap from a live outage is indistinguishable downstream from a gap in the
archive -- the same type, in the same table, differing only in `gap_kind`.

**A detector never merges adjacent gaps and never widens a reported one.** The registry
records each gap once, keyed by its bounds, and its `discovered_at_utc` is the column a
rebuild cannot reproduce (`fking.data.backfill.registry`). Widening a gap after the fact
would move that instant forward on a hole that was already known.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Final

from fking.data.backfill.registry import STREAM_TIMESTAMP_RESOLUTION, GapKind, RecordedGap
from fking.platform.errors import DataIntegrityError

__all__ = [
    "CADENCE_GRACE",
    "STREAM_TIMESTAMP_RESOLUTION",
    "CadenceGapDetector",
    "SequenceGapDetector",
]

# 90 seconds, from `DATA_PIPELINE.md` section 5: "a minute with no closed bar within a
# 90s grace window is a gap". The window is measured from the minute's *open*, which is
# the only edge a detector knows about before anything arrives -- so a 1-minute bar,
# published within about a second of its close, has roughly 30 seconds of slack. Wide
# enough that ordinary jitter never reports a gap, narrow enough that a stream which is
# connected and silent is noticed inside two minutes rather than at the next restart.
CADENCE_GRACE: Final[timedelta] = timedelta(seconds=90)

# Re-exported rather than declared, because the tape repair's residual bounds have to be
# the same width as the gaps this detector opens and neither module can import the other
# (`fking.data.backfill.registry` carries the constant and the argument).


class SequenceGapDetector:
    """Contiguity of `aggTrade.a`, one symbol's stream at a time.

    Mutable by design and deliberately not a domain type: it is infrastructure whose
    whole job is to carry state across messages, in the same way `FrozenClock` is
    (`docs/rules/immutability.md`).
    """

    __slots__ = ("_last_event_time_utc", "_last_id")

    def __init__(self) -> None:
        self._last_id: int | None = None
        self._last_event_time_utc: datetime | None = None

    @property
    def last_id(self) -> int | None:
        """The newest aggregate trade id observed, or `None` before the first message."""
        return self._last_id

    def observe(self, aggregate_trade_id: int, event_time_utc: datetime) -> RecordedGap | None:
        """Record one print, returning the gap it revealed.

        The first print establishes the baseline and can reveal nothing -- a session
        that starts mid-tape has no left edge to measure from, and inventing one would
        report every restart as a loss of every trade since genesis.

        A repeated id returns `None`. The venue does not normally resend on one socket,
        but a duplicate is harmless and reporting it as a negative-width gap is not.

        Raises:
            DataIntegrityError: the id went backwards. `a` is monotone by construction,
                so a decrease means either the frames were reordered -- which cannot
                happen on one TCP connection -- or the stream is not the symbol this
                detector was created for. Both are reasons to stop rather than to
                record a gap.
        """
        previous_id = self._last_id
        previous_event_time_utc = self._last_event_time_utc
        self._last_id = aggregate_trade_id
        self._last_event_time_utc = event_time_utc

        if previous_id is None or previous_event_time_utc is None:
            return None
        if aggregate_trade_id == previous_id:
            return None
        if aggregate_trade_id < previous_id:
            raise DataIntegrityError(
                f"aggregate trade id went backwards: {previous_id} then "
                f"{aggregate_trade_id}. These are monotone by construction, so this is a "
                f"crossed stream or a reordered feed, not a gap"
            )
        missing = aggregate_trade_id - previous_id - 1
        if missing == 0:
            return None

        # Two prints can share a millisecond, and the venue's event times have no finer
        # resolution -- so when the bracketing prints carry the same instant, the
        # missing ones are known to lie inside that millisecond and the smallest region
        # the registry can express is exactly one. Widening further would overstate the
        # gapped duration; a zero-width row is rejected by the table's own CHECK.
        gap_end_utc = max(event_time_utc, previous_event_time_utc + STREAM_TIMESTAMP_RESOLUTION)
        return RecordedGap(
            gap_start_utc=previous_event_time_utc,
            gap_end_utc=gap_end_utc,
            gap_kind=GapKind.SEQUENCE,
            missing_bar_count=missing,
        )


class CadenceGapDetector:
    """One closed bar per interval, judged against a grace window on the injected clock.

    The watermark is the open time of the newest minute this detector has accounted for,
    either by observing its bar or by reporting it missing. Bars arriving above the
    watermark are held until `poll` walks past them, which is what lets a bar for minute
    *n+2* arrive before minute *n+1*'s deadline without minute *n+1* being reported
    prematurely -- the frames are ordered on one socket, but a session that reconnects
    can genuinely see a later bar first.
    """

    __slots__ = ("_grace", "_interval", "_observed", "_watermark_open_time_utc")

    def __init__(
        self,
        *,
        interval: timedelta,
        grace: timedelta = CADENCE_GRACE,
        started_at_utc: datetime,
    ) -> None:
        if interval <= timedelta(0):
            raise ValueError(f"cadence interval must be positive, got {interval!r}")
        self._interval = interval
        self._grace = grace
        # One interval *before* the interval containing the session start, so that the
        # first candidate is the interval the session began inside. That interval's bar
        # is published at its close, which is after the session opened, so it is
        # genuinely expected -- and starting a watermark at the session's own interval
        # would make the very first minute of a silent session unreportable, which is
        # precisely the case this detector exists for. A watermark at the epoch would
        # report every minute since 1970 on the first poll.
        self._watermark_open_time_utc = _floor_to(started_at_utc, self._interval) - self._interval
        self._observed: set[datetime] = set()

    @property
    def watermark_open_time_utc(self) -> datetime:
        """The newest interval accounted for, observed or reported."""
        return self._watermark_open_time_utc

    def observe(self, open_time_utc: datetime) -> None:
        """Record that a closed bar for this interval arrived.

        A bar at or below the watermark is discarded rather than rejected: it is a
        replay of a minute already accounted for, which is what a reconnect that
        overlaps the previous session produces, and the registry deduplicates the
        corresponding write by primary key anyway.
        """
        if open_time_utc > self._watermark_open_time_utc:
            self._observed.add(open_time_utc)

    def poll(self, now_utc: datetime) -> tuple[RecordedGap, ...]:
        """Every interval whose grace window has expired without a bar.

        Contiguous missing intervals are reported as one gap with an exact count, which
        is both fewer rows and a truer description: a stream that was silent for ten
        minutes had one outage, not ten.
        """
        # An interval is due once `grace` has elapsed since it *opened*, which is the
        # spelling `DATA_PIPELINE.md` section 5 uses. Measuring from the close instead
        # would put the deadline a full interval later and let a stream be silent for
        # two and a half minutes before anything was reported.
        deadline = now_utc - self._grace
        gaps: list[RecordedGap] = []
        run_start: datetime | None = None
        run_length = 0

        candidate = self._watermark_open_time_utc + self._interval
        while candidate <= deadline:
            if candidate in self._observed:
                self._observed.discard(candidate)
                if run_start is not None:
                    gaps.append(self._gap(run_start, run_length))
                    run_start, run_length = None, 0
            else:
                if run_start is None:
                    run_start = candidate
                run_length += 1
            self._watermark_open_time_utc = candidate
            candidate += self._interval

        if run_start is not None:
            gaps.append(self._gap(run_start, run_length))
        return tuple(gaps)

    def _gap(self, run_start_utc: datetime, run_length: int) -> RecordedGap:
        return RecordedGap(
            gap_start_utc=run_start_utc,
            gap_end_utc=run_start_utc + self._interval * run_length,
            gap_kind=GapKind.CADENCE,
            missing_bar_count=run_length,
        )


def _floor_to(moment: datetime, interval: timedelta) -> datetime:
    """`moment` snapped down onto the interval lattice anchored at the Unix epoch.

    Anchored at the epoch rather than at the session start, so two sessions started
    seconds apart agree about where minute boundaries are. A lattice anchored on a
    process's own start time would make a bar's minute a function of when the process
    booted.
    """
    epoch_seconds = int(moment.timestamp())
    interval_seconds = int(interval.total_seconds())
    return datetime.fromtimestamp(epoch_seconds - epoch_seconds % interval_seconds, tz=UTC)
