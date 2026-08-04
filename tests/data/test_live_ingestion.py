"""A live session, replayed from recorded frames, including the socket dropping.

The connection is a replay of `tests/fixtures/streams/`, driven through the same
`LiveIngestSupervisor` production uses -- the only injected difference is the factory
that opens the socket, which is the "mock the exchange, against recorded real responses"
seam `.claude/rules/testing-rules.md` requires. Nothing here hand-writes a frame.

The writer is a recording double rather than a real database. That is not a mocked
store: `test_live_ingestion_against_postgres.py` runs the same writes against real
Postgres, and what is being asserted here is *which records the session decided to
write*, which is a decision the database cannot answer. Splitting them keeps the
decision assertions fast and the persistence assertions honest.
"""

from __future__ import annotations

import asyncio
import json
import random
from collections.abc import AsyncIterator, Mapping, Sequence
from contextlib import asynccontextmanager
from datetime import UTC, datetime, timedelta

import pytest

from fking.data.backfill.registry import GapKind
from fking.data.format_resolver import Dataset
from fking.data.live import (
    LiveBar,
    LiveGap,
    LiveIngestSupervisor,
    LiveRouter,
    LiveStreamProfile,
)
from fking.data.live.streams import LIVE_STREAM_PROFILES
from fking.platform.errors import DataIntegrityError
from tests.support.stream_fixtures import RecordedStream, recorded_streams

pytestmark = [pytest.mark.unit, pytest.mark.asyncio]

SYMBOL = "BTCUSDT"
SPOT = LIVE_STREAM_PROFILES["binance-spot-testnet"]
ALL_RECORDINGS = recorded_streams()

# The tape has to be long enough that three prints can be excised from the middle with
# a contiguous run either side; below this the excision is at an endpoint and the
# detector has nothing to compare against.
MINIMUM_AGG_TRADES_FOR_AN_EXCISION = 6
FRAMES_BEFORE_THE_DROP = 20
SESSIONS_BEFORE_THE_REFUSAL = 2


class SocketDroppedError(ConnectionResetError):
    """What the replay raises where the recording ends.

    A `ConnectionResetError` -- an `OSError` -- because that is what a real drop looks
    like to the supervisor, and the supervisor's recoverable set is
    `fking.platform.safety.TRANSPORT_ERRORS`. Raising something outside that set would
    test a path production never takes.
    """


class ReplayConnection:
    """Serves recorded frames, then fails the way a dropped socket does."""

    def __init__(self, frames: Sequence[str]) -> None:
        self._frames = list(frames)
        self._index = 0

    async def recv(self) -> str:
        if self._index >= len(self._frames):
            raise SocketDroppedError("recording exhausted")
        frame = self._frames[self._index]
        self._index += 1
        return frame


class ReplayFactory:
    """A connection factory that serves one recording per session, in order."""

    def __init__(self, *sessions: Sequence[str]) -> None:
        self._sessions = list(sessions)
        self.urls: list[str] = []

    @asynccontextmanager
    async def __call__(self, url: str) -> AsyncIterator[ReplayConnection]:
        self.urls.append(url)
        if not self._sessions:
            raise SocketDroppedError("no further recordings")
        yield ReplayConnection(self._sessions.pop(0))


class RecordingWriter:
    """Captures what the session decided to persist. Never asserts about storage."""

    def __init__(self) -> None:
        self.bars: list[LiveBar] = []
        self.gaps: list[LiveGap] = []

    async def write_bars(self, bars: Sequence[LiveBar]) -> int:
        self.bars.extend(bars)
        return len(bars)

    async def write_gaps(self, gaps: Sequence[LiveGap], *, discovered_at_utc: datetime) -> int:
        # The instant is the writer's business, not the session's; it is accepted and
        # ignored here so the double matches the signature the supervisor calls.
        del discovered_at_utc
        self.gaps.extend(gaps)
        return len(gaps)


class SteppedClock:
    """A clock that advances a fixed step per read. Injected, never `datetime.now`."""

    def __init__(self, start: datetime, step: timedelta = timedelta(milliseconds=1)) -> None:
        self._now = start
        self._step = step

    def __call__(self) -> datetime:
        self._now += self._step
        return self._now

    def advance(self, delta: timedelta) -> None:
        self._now += delta


def _spot_recording() -> RecordedStream:
    return next(record for record in ALL_RECORDINGS if record.venue_id == SPOT.venue_id)


def _kline_frames(frames: Sequence[str]) -> list[Mapping[str, object]]:
    payloads = [json.loads(frame)["data"] for frame in frames]
    return [payload["k"] for payload in payloads if payload["e"] == "kline"]


def _epoch_ms(kline: Mapping[str, object]) -> int:
    """The kline's open epoch, read the way a JSON document hands it over."""
    open_time = kline["t"]
    assert isinstance(open_time, int)
    return open_time


def _first_event_time(frames: Sequence[str]) -> datetime:
    return datetime.fromtimestamp(json.loads(frames[0])["data"]["E"] / 1000, tz=UTC)


def _after_last_event(frames: Sequence[str]) -> datetime:
    """One second past the recording's newest event time.

    The wall clock a replay runs on has to be consistent with the event times inside the
    recording, because a disconnect gap's right edge is one or the other depending on
    whether the session reconnected or shut down. Starting the clock at an arbitrary
    instant would make the shutdown case produce a backwards gap -- which the router
    correctly refuses, for a reason that has nothing to do with the code under test.
    """
    newest = max(json.loads(frame)["data"]["E"] for frame in frames)
    return datetime.fromtimestamp(newest / 1000, tz=UTC) + timedelta(seconds=1)


def _supervisor(
    profile: LiveStreamProfile,
    factory: ReplayFactory,
    writer: RecordingWriter,
    clock: SteppedClock,
    *,
    started_at_utc: datetime,
) -> LiveIngestSupervisor:
    router = LiveRouter(profile, (SYMBOL,), started_at_utc=started_at_utc)
    return LiveIngestSupervisor(
        profile=profile,
        symbols=(SYMBOL,),
        router=router,
        writer=writer,  # type: ignore[arg-type]  # the recording double; see the module docstring
        clock=clock,
        rng=random.Random(20260804),
        connect=factory,
        # Long enough that the cadence poller never fires inside a replay, which runs in
        # microseconds. The cadence path has its own tests; mixing it in here would make
        # the bar assertions depend on how fast the machine ran.
        poll_interval_seconds=3600.0,
    )


async def test_open_klines_are_never_written_and_the_closed_bar_for_the_minute_is() -> None:
    """The single most important assertion in the live path.

    An open kline is a partial aggregate that will change; persisting one and updating
    it later is a mutation of a time series, and the backtest then reads a value the
    live system never saw in that form.
    """
    recording = _spot_recording()
    frames = recording.frames()
    klines = _kline_frames(frames)
    open_minutes = {_epoch_ms(kline) for kline in klines if not kline["x"]}
    closed_minutes = {_epoch_ms(kline) for kline in klines if kline["x"]}
    # The fixture-integrity suite guarantees this; restated here so a failure below
    # points at the code rather than at the corpus.
    assert open_minutes & closed_minutes

    writer = RecordingWriter()
    factory = ReplayFactory(frames)
    clock = SteppedClock(_after_last_event(frames))
    outcome = await _supervisor(
        SPOT, factory, writer, clock, started_at_utc=_first_event_time(frames)
    ).run_once()

    written_minutes = {int(bar.record.open_time_utc.timestamp() * 1000) for bar in writer.bars}
    assert written_minutes == closed_minutes
    assert not written_minutes & open_minutes - closed_minutes
    assert outcome.open_klines_skipped == sum(1 for kline in klines if not kline["x"])
    assert outcome.bars_written == len(closed_minutes)
    assert all(bar.series.dataset is Dataset.KLINES for bar in writer.bars)


async def test_a_healthy_replay_reports_no_sequence_gap() -> None:
    """The detector must stay quiet on a clean tape, or its alarms mean nothing."""
    frames = _spot_recording().frames()
    writer = RecordingWriter()
    await _supervisor(
        SPOT,
        ReplayFactory(frames),
        writer,
        SteppedClock(_after_last_event(frames)),
        started_at_utc=_first_event_time(frames),
    ).run_once()

    assert [gap for gap in writer.gaps if gap.gap.gap_kind is GapKind.SEQUENCE] == []


async def test_a_skipped_aggregate_trade_id_is_reported_with_its_exact_size() -> None:
    """A real recording with one aggTrade frame removed: the venue's own numbering is
    what makes the size exact, so the gap must be three prints wide when three are cut."""
    frames = list(_spot_recording().frames())
    agg_indices = [
        index for index, frame in enumerate(frames) if json.loads(frame)["data"]["e"] == "aggTrade"
    ]
    assert len(agg_indices) >= MINIMUM_AGG_TRADES_FOR_AN_EXCISION
    excised = agg_indices[2:5]
    surviving = [frame for index, frame in enumerate(frames) if index not in excised]

    writer = RecordingWriter()
    await _supervisor(
        SPOT,
        ReplayFactory(surviving),
        writer,
        SteppedClock(_after_last_event(surviving)),
        started_at_utc=_first_event_time(surviving),
    ).run_once()

    sequence_gaps = [gap for gap in writer.gaps if gap.gap.gap_kind is GapKind.SEQUENCE]
    assert len(sequence_gaps) == 1
    assert sequence_gaps[0].gap.missing_bar_count == len(excised)
    assert sequence_gaps[0].series.dataset is Dataset.AGG_TRADES


async def test_a_drop_mid_stream_records_a_gap_even_when_the_reconnect_is_instant() -> None:
    """A 400 ms reconnect that loses nothing still leaves a row.

    Reconnects that recover invisibly are how a missing minute becomes unexplainable
    nine months later, and the cost of the honest version is one row per outage.
    """
    frames = list(_spot_recording().frames())
    first_half = frames[:FRAMES_BEFORE_THE_DROP]
    second_half = frames[FRAMES_BEFORE_THE_DROP:]

    writer = RecordingWriter()
    clock = SteppedClock(_after_last_event(frames))
    supervisor = _supervisor(
        SPOT,
        ReplayFactory(first_half, second_half),
        writer,
        clock,
        started_at_utc=_first_event_time(frames),
    )

    first = await supervisor.run_once()
    assert first.frames_received == len(first_half)
    assert "SocketDroppedError" in first.ended_because

    # The reconnect completes inside one second of simulated time. The gap is recorded
    # anyway, and its bounds are the venue's own event times either side of the seam.
    clock.advance(timedelta(milliseconds=400))
    second = await supervisor.run_once()
    assert second.frames_received == len(second_half)

    disconnects = [gap for gap in writer.gaps if gap.gap.gap_kind is GapKind.DISCONNECT]
    assert disconnects, "a reconnect always opens a gap, even a 400 ms one"
    for entry in disconnects:
        assert entry.gap.gap_end_utc > entry.gap.gap_start_utc
        # NULL, not zero: the claim is that nothing was observed, not that a specific
        # number of bars is absent.
        assert entry.gap.missing_bar_count is None


async def test_a_session_that_never_reconnects_still_closes_its_gap_on_shutdown() -> None:
    """Otherwise the outage goes unrecorded through the door marked "we were about to
    reconnect"."""
    frames = _spot_recording().frames()
    writer = RecordingWriter()
    clock = SteppedClock(_after_last_event(frames))
    supervisor = _supervisor(
        SPOT, ReplayFactory(frames), writer, clock, started_at_utc=_first_event_time(frames)
    )

    await supervisor.run_once()
    clock.advance(timedelta(seconds=5))
    closed = await supervisor.close_open_gaps()

    assert closed == len(SPOT.persisted_datasets)
    assert all(gap.gap.gap_kind is GapKind.DISCONNECT for gap in writer.gaps)


async def test_the_session_subscribes_to_the_url_the_profile_declares() -> None:
    """A recorded fixture proves nothing about a session pointed somewhere else."""
    frames = _spot_recording().frames()
    writer = RecordingWriter()
    factory = ReplayFactory(frames)
    supervisor = _supervisor(
        SPOT,
        factory,
        writer,
        SteppedClock(_after_last_event(frames)),
        started_at_utc=_first_event_time(frames),
    )

    await supervisor.run_once()

    assert factory.urls == [supervisor.stream_url]
    assert factory.urls[0].startswith("wss://stream.testnet.binance.vision/stream?streams=")
    assert factory.urls[0] == _spot_recording().source_url


class SilentConnection:
    """A socket that stays open and sends nothing until released.

    The case the cadence detector exists for, and the one the sequence detector
    structurally cannot see. Released by an event rather than by a sleep so the test
    asserts on a condition rather than on how fast the machine ran.
    """

    def __init__(self, released: asyncio.Event) -> None:
        self._released = released

    async def recv(self) -> str:
        await self._released.wait()
        raise SocketDroppedError("stream closed while silent")


class GapSignallingWriter(RecordingWriter):
    """A recording writer that releases a socket once it has seen its first gap."""

    def __init__(self, released: asyncio.Event) -> None:
        super().__init__()
        self._released = released

    async def write_gaps(self, gaps: Sequence[LiveGap], *, discovered_at_utc: datetime) -> int:
        written = await super().write_gaps(gaps, discovered_at_utc=discovered_at_utc)
        if gaps:
            self._released.set()
        return written


async def test_a_connected_but_silent_stream_produces_a_cadence_gap_on_the_timer() -> None:
    """No frame ever arrives, so nothing but the timer can report anything.

    This is the assertion that shows the cadence poller is wired into the session at
    all: with only arrival-driven detection, a socket that is open and silent produces
    no gap and the process reports health while the market moves without it.
    """
    released = asyncio.Event()
    writer = GapSignallingWriter(released)
    started = datetime(2026, 8, 4, 12, 0, tzinfo=UTC)
    router = LiveRouter(SPOT, (SYMBOL,), started_at_utc=started)

    @asynccontextmanager
    async def connect(url: str) -> AsyncIterator[SilentConnection]:
        del url
        yield SilentConnection(released)

    supervisor = LiveIngestSupervisor(
        profile=SPOT,
        symbols=(SYMBOL,),
        router=router,
        writer=writer,  # type: ignore[arg-type]  # the recording double
        # Far enough past the session start that the first poll is already overdue,
        # so the assertion does not depend on wall-clock time passing.
        clock=SteppedClock(started + timedelta(minutes=5)),
        rng=random.Random(20260804),
        connect=connect,
        poll_interval_seconds=0.01,
    )

    outcome = await supervisor.run_once()

    assert outcome.frames_received == 0
    cadence_gaps = [gap for gap in writer.gaps if gap.gap.gap_kind is GapKind.CADENCE]
    assert cadence_gaps, "a connected silent stream must still report missing minutes"
    assert all(gap.series.dataset is Dataset.KLINES for gap in cadence_gaps)
    assert all((gap.gap.missing_bar_count or 0) > 0 for gap in cadence_gaps)


async def test_run_forever_reconnects_after_a_transport_failure_and_stops_at_anything_else() -> (
    None
):
    """The two halves of the reconnect policy, in one run.

    A dropped socket is the expected end of a session and is retried. A frame the parser
    cannot understand is not: a venue whose payload shape changed is not something to
    retry into for a week, so it leaves `run_forever` and stops the process.
    """
    frames = list(_spot_recording().frames())
    corrupt = json.dumps({"stream": "btcusdt@aggTrade", "data": {"e": "somethingNew"}})

    writer = RecordingWriter()
    factory = ReplayFactory(frames[:10], [corrupt])
    supervisor = _supervisor(
        SPOT,
        factory,
        writer,
        SteppedClock(_after_last_event(frames)),
        started_at_utc=_first_event_time(frames),
    )

    with pytest.raises(DataIntegrityError, match="did not subscribe"):
        await supervisor.run_forever()

    # Two sessions: the first ended in a transport failure and was retried, the second
    # ended in a refusal and was not.
    assert len(factory.urls) == SESSIONS_BEFORE_THE_REFUSAL
