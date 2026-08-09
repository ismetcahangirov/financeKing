"""The supervisor: opens sessions, reconnects, and records every outage as a gap.

`DATA_PIPELINE.md` section 5 makes a reconnect a **scheduled event, not an exception**.
Binance closes a healthy connection every 24 hours and pings every ~3 minutes expecting
a pong within 10, so a supervisor that logs a reconnect at ERROR reports a daily
incident forever and trains its reader to ignore the one that matters.

Three responsibilities live here and nowhere else:

**The reconnect decision.** `run_once` is where the `except` sits -- outside the read
loop, wrapping it, so a transport failure *ends* the iteration rather than resuming it.
The caught set is `fking.platform.safety.TRANSPORT_ERRORS`, which is exactly "the socket
died". A `DataIntegrityError` from a frame this session cannot parse is not in that set
and propagates out of `run_forever`, stopping the process: a venue whose payload shape
changed is not something to retry into for a week.

**A gap per outage, always.** `mark_disconnected` runs in the `finally`, so it runs on
the clean 24-hour close as well as on a reset, and the gap is closed by the first event
after the reconnect. A 400 ms reconnect that loses nothing still leaves a row, because a
reconnect that recovers invisibly is how a missing minute becomes unexplainable nine
months later -- and the cost of the honest version is one row.

**The cadence timer, which also seals the tape.** The cadence detector answers "did a bar
arrive for that minute", which cannot be driven by arrivals: the case it exists for is a
socket that is connected and silent, which produces nothing to poll on. So a companion
task polls the router on a fixed interval for as long as the session is open, and is
cancelled with it. The tape seal rides the same timer for the same reason -- a day ends
whether or not a print arrives to notice it -- and it runs in a worker thread, because
sealing writes a whole day of prints to Parquet and doing that on the event loop would
stall the read of every socket for the duration.

The clock and the RNG are injected. A supervisor that read `datetime.now(UTC)` and the
module-level `random` could not be replayed, and the backoff schedule could not be
asserted (`docs/rules/time-and-timezones.md`).
"""

from __future__ import annotations

import asyncio
import contextlib
import random
from collections.abc import Callable
from contextlib import AbstractAsyncContextManager
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Final, NoReturn, Protocol
from uuid import uuid4

from fking.data.live.backoff import reconnect_delay_seconds
from fking.data.live.router import LiveGap, LiveRouter
from fking.data.live.session import WebSocketConnection, read_frames
from fking.data.live.streams import LiveStreamProfile, combined_stream_url
from fking.data.live.tape import SealedPartition, TapeCorpusWriter
from fking.data.live.writer import LiveMarketDataWriter
from fking.platform.correlation import correlation_scope
from fking.platform.logging import get_logger
from fking.platform.safety import TRANSPORT_ERRORS, guarded_ws_connect

__all__ = ["CADENCE_POLL_INTERVAL_SECONDS", "LiveIngestSupervisor", "SessionOutcome"]

_LOG: Final = get_logger(__name__)

# Every 15 seconds. Well inside the 90-second cadence grace, so a silent minute is
# reported within one poll of its deadline rather than up to a poll interval late, and
# far enough apart that a poll that finds nothing is free. The poll is a pure walk over
# per-series watermarks; it touches the database only when it has a gap to write.
CADENCE_POLL_INTERVAL_SECONDS: Final[float] = 15.0


class ConnectionFactory(Protocol):
    """Opens a validated WebSocket. `guarded_ws_connect` in production.

    Injected so that a test can replay recorded frames and then fail the socket at a
    chosen point -- the "mock the exchange, against recorded real responses" seam
    `docs/rules/testing-rules.md` requires. It is not a switch: the default is the
    guarded transport, and nothing in `src/fking` passes anything else.
    """

    def __call__(self, url: str) -> AbstractAsyncContextManager[WebSocketConnection]: ...


@dataclass(frozen=True, slots=True)
class SessionOutcome:
    """What one session did, from connect to disconnect."""

    frames_received: int
    open_klines_skipped: int
    bars_written: int
    # Spooled, not written: a print is durable on disk the moment it is appended, and it
    # becomes a Parquet row a day later. Reporting it as "written" would conflate the two
    # and make a session that spooled a million prints and sealed nothing look complete.
    prints_spooled: int
    partitions_sealed: int
    gaps_recorded: int
    ended_because: str


def _system_now_utc() -> datetime:
    return datetime.now(UTC)


class LiveIngestSupervisor:
    """Runs one venue's live market-data ingestion until something unrecoverable stops it."""

    __slots__ = (
        "_clock",
        "_connect",
        "_poll_interval_seconds",
        "_profile",
        "_rng",
        "_router",
        "_symbols",
        "_tape",
        "_writer",
    )

    def __init__(
        self,
        *,
        profile: LiveStreamProfile,
        symbols: tuple[str, ...],
        router: LiveRouter,
        writer: LiveMarketDataWriter,
        tape: TapeCorpusWriter,
        clock: Callable[[], datetime] = _system_now_utc,
        rng: random.Random | None = None,
        connect: ConnectionFactory = guarded_ws_connect,
        poll_interval_seconds: float = CADENCE_POLL_INTERVAL_SECONDS,
    ) -> None:
        self._profile = profile
        self._symbols = symbols
        self._router = router
        self._writer = writer
        # Required rather than optional. Both profiles list `aggTrades` in
        # `persisted_datasets`, so a session constructed without a tape writer would route
        # every print and drop it -- which is the state #146 exists to end, and an
        # `| None` default is how it would quietly come back.
        self._tape = tape
        self._clock = clock
        # SystemRandom rather than the default Mersenne Twister: jitter across processes
        # is the point, and a seeded default would give two replicas started by the same
        # deploy an identical reconnect schedule -- which is the herd the jitter exists
        # to break up.
        self._rng = rng if rng is not None else random.SystemRandom()
        self._connect = connect
        self._poll_interval_seconds = poll_interval_seconds

    @property
    def stream_url(self) -> str:
        return combined_stream_url(self._profile, self._symbols)

    async def run_forever(self) -> NoReturn:
        """Reconnect indefinitely, backing off between attempts.

        There is no attempt count at which stopping is better than trying again: a
        market-data session that gives up leaves a system that looks alive with a frozen
        view of the market, which `FAILSAFE.md` names as the worst available outcome.
        The gap registry is what makes the outage visible while it lasts.

        Returns never. It exits only by propagating an exception that is not a transport
        failure, or by cancellation.
        """
        attempt = 0
        while True:
            outcome = await self.run_once()
            if outcome.frames_received > 0:
                # A session that received data was healthy; the next outage starts its
                # own schedule. Without this reset, one bad hour leaves every subsequent
                # reconnect sleeping at the cap for the life of the process.
                attempt = 0
            delay_seconds = reconnect_delay_seconds(attempt, rng=self._rng)
            _LOG.warning(
                "live.reconnect_scheduled",
                venue_id=self._profile.venue_id,
                attempt=attempt,
                delay_seconds=round(delay_seconds, 3),
                ended_because=outcome.ended_because,
                frames_received=outcome.frames_received,
            )
            await asyncio.sleep(delay_seconds)
            attempt += 1

    async def run_once(self) -> SessionOutcome:
        """One session: connect, consume until the socket dies, record the outage.

        Returns rather than raises on a transport failure, because a dropped socket is
        the expected end of a session here. Everything else propagates.
        """
        correlation_id = uuid4()
        url = self.stream_url
        frames_received = 0
        open_klines_skipped = 0
        bars_written = 0
        prints_spooled = 0
        partitions_sealed = 0
        gaps_recorded = 0
        ended_because = "connection closed"

        with correlation_scope(correlation_id):
            _LOG.info(
                "live.session_opening",
                venue_id=self._profile.venue_id,
                symbols=list(self._symbols),
                streams=list(self._profile.stream_suffixes),
                url=url,
            )
            poller: asyncio.Task[None] | None = None
            try:
                async with self._connect(url) as connection:
                    poller = asyncio.create_task(self._poll_cadence())
                    _LOG.info("live.session_connected", venue_id=self._profile.venue_id)
                    # Close the previous session's outage here rather than when the
                    # first frame arrives: the right edge is the instant we resumed
                    # listening, which is now, and a quiet series would otherwise leave
                    # the gap open until it happened to print.
                    gaps_recorded += await self._write_gaps(
                        self._router.close_pending_gaps(self._clock())
                    )
                    # Once on connect as well as on the timer, so a spool a previous
                    # process left behind becomes a partition at startup rather than at
                    # the first poll -- which on a session that dies inside fifteen
                    # seconds would be never.
                    partitions_sealed += len(await self._seal_tape())
                    async for raw in read_frames(connection):
                        frames_received += 1
                        routed = self._router.route(raw, now_utc=self._clock())
                        open_klines_skipped += routed.open_klines
                        bars_written += await self._writer.write_bars(routed.bars)
                        prints_spooled += self._tape.append(routed.trades)
                        gaps_recorded += await self._write_gaps(routed.gaps)
            except TRANSPORT_ERRORS as dropped:
                ended_because = f"{type(dropped).__name__}: {dropped}"
            finally:
                if poller is not None:
                    poller.cancel()
                    # Suppresses the cancellation this line just requested, and nothing
                    # else: a poller that failed for its own reason completed before the
                    # cancel and `await` re-raises that failure here rather than leaving
                    # it as an unretrieved task exception nobody ever sees.
                    with contextlib.suppress(asyncio.CancelledError):
                        await poller
                self._router.mark_disconnected(self._clock())
                # Closes the spool handles and seals nothing. A session that stops at noon
                # has half a day spooled, and the half-day stays a spool: the next session
                # continues the same file, and only a day that has ended becomes a
                # partition (`fking.data.live.tape`).
                self._tape.close()

            _LOG.warning(
                "live.session_ended",
                venue_id=self._profile.venue_id,
                ended_because=ended_because,
                frames_received=frames_received,
                open_klines_skipped=open_klines_skipped,
                bars_written=bars_written,
                prints_spooled=prints_spooled,
                partitions_sealed=partitions_sealed,
                gaps_recorded=gaps_recorded,
            )
        return SessionOutcome(
            frames_received=frames_received,
            open_klines_skipped=open_klines_skipped,
            bars_written=bars_written,
            prints_spooled=prints_spooled,
            partitions_sealed=partitions_sealed,
            gaps_recorded=gaps_recorded,
            ended_because=ended_because,
        )

    async def close_open_gaps(self) -> int:
        """Close any disconnect gap still open, at the current instant.

        Called when the process is shutting down rather than reconnecting. Without it a
        supervisor that stops while disconnected leaves the outage unrecorded, which is
        the "a reconnect always opens a gap" rule failing through the door marked "we
        were about to reconnect".
        """
        return await self._write_gaps(self._router.close_pending_gaps(self._clock()))

    async def _poll_cadence(self) -> None:
        """Report cadence gaps and seal elapsed tape days, on a timer.

        Returns nothing and is never awaited for a value: it ends by cancellation when
        the session does. What it records is counted where it is written and logged
        individually, so the tally is not lost by the cancellation -- which is why
        `SessionOutcome.partitions_sealed` counts only the seal on connect and the
        `live.tape_sealed` line is the record of the rest.
        """
        while True:
            await asyncio.sleep(self._poll_interval_seconds)
            await self._write_gaps(self._router.poll(self._clock()))
            await self._seal_tape()

    async def _seal_tape(self) -> tuple[SealedPartition, ...]:
        """Seal every tape day that has ended, off the event loop.

        In a thread because the work is a whole day of prints serialised to Parquet --
        seconds of CPU on a busy symbol -- and doing it inline would stop reading the
        socket for that long, which on a venue that pings every three minutes expecting a
        pong within ten is a self-inflicted disconnect.
        """
        return await asyncio.to_thread(self._tape.seal_elapsed_days, now_utc=self._clock())

    async def _write_gaps(self, gaps: tuple[LiveGap, ...]) -> int:
        if not gaps:
            return 0
        now_utc = self._clock()
        recorded = await self._writer.write_gaps(gaps, discovered_at_utc=now_utc)
        for entry in gaps:
            _LOG.warning(
                "live.gap_detected",
                venue_id=self._profile.venue_id,
                symbol=entry.series.symbol,
                dataset=entry.series.dataset.value,
                bar_interval=entry.series.bar_interval,
                gap_kind=entry.gap.gap_kind.value,
                gap_start_utc=entry.gap.gap_start_utc.isoformat(),
                gap_end_utc=entry.gap.gap_end_utc.isoformat(),
                missing_bar_count=entry.gap.missing_bar_count,
            )
        return recorded
