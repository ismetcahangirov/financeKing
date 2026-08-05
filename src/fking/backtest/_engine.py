"""The loop every backtest, walk-forward fold and CPCV path runs on.

Single-threaded, and that is a correctness property rather than a simplification. A
second thread is a second source of ordering, and no seed reproduces it -- so the only
parallelism this project permits is across *independent* runs.

The loop is deliberately thin. It owns three things and delegates everything else:

- **When** each event is dispatched, which is the queue's total order and nothing else.
- **What simulated time is** while a handler runs, which is the popped event's instant.
- **What happened**, as a trace whose digest is comparable byte for byte between runs.

It owns no opinion about strategies, venues, portfolios or costs. Those arrive as an
`EventHandler`, and the whole point of the arrangement is that the same loop drives the
simulated venue and the live ones, so a backtest and a demo run can only differ in the
venue -- never in the harness (`BACKTEST_ENGINE.md` section 3).

**What a handler cannot do** is the other half of the design. `RunContext` exposes
reading the clock, scheduling a future event and deriving a seed. It does not expose
advancing the clock, reordering the queue, or reading an event that has not been
dispatched -- because a handler that could peek ahead would be look-ahead available
through an interface, which no amount of care in the strategy author prevents, and the
strategy author will frequently be an LLM.
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass
from datetime import datetime
from typing import Protocol

from fking.backtest._clock import SimulationClock
from fking.backtest._config import RunConfig, canonical_digest, config_hash
from fking.backtest._errors import CausalityError, EventBudgetExhaustedError
from fking.backtest._events import Event, EventPriority
from fking.backtest._queue import EventQueue
from fking.domain import encode


@dataclass(frozen=True, slots=True)
class TraceEntry:
    """One dispatched event, reduced to what a comparison between two runs needs.

    The event is recorded as a digest rather than in full. A trace of an eighteen-month
    run holds millions of entries, and keeping the payloads would make the trace larger
    than the archive it was computed from -- while adding nothing, because the question
    a trace answers is "did these two runs do the same thing", and a digest answers it
    exactly.
    """

    occurs_at_utc: datetime
    priority: EventPriority
    sequence: int
    event_type: str
    event_digest: str


@dataclass(frozen=True, slots=True)
class RunTrace:
    """What a run did, in a form two runs can be compared on.

    `events_beyond_window` is reported rather than silently absorbed. Events scheduled
    past `end_utc` are ordinary -- a fill acknowledged 180ms after the final bar is one --
    but a run that dropped thousands of them ended mid-flight, and a count of zero where
    a venue was active is itself a finding.
    """

    config_hash: str
    entries: tuple[TraceEntry, ...]
    digest: str
    events_beyond_window: int

    @property
    def event_count(self) -> int:
        """How many events were dispatched."""
        return len(self.entries)


class RunContext:
    """The only surface a handler has on the loop.

    Read the clock, schedule forward, derive a seed. Nothing here can move simulated
    time, and nothing here can see an event that has not been dispatched.
    """

    __slots__ = ("_clock", "_config", "_events_beyond_window", "_queue")

    def __init__(self, *, config: RunConfig, clock: SimulationClock, queue: EventQueue) -> None:
        self._config = config
        self._clock = clock
        self._queue = queue
        self._events_beyond_window = 0

    def now_utc(self) -> datetime:
        """The instant currently being dispatched."""
        return self._clock()

    def seed_for(self, label: str) -> int:
        """This run's seed for a named source of randomness.

        A handler that reaches for the `random` module instead of this makes the run
        irreproducible, and the determinism check is what catches it -- see
        `tests/backtest/test_determinism.py`.
        """
        return self._config.seed_for(label)

    @property
    def events_beyond_window(self) -> int:
        """How many scheduled events fell past the run window's end and were dropped."""
        return self._events_beyond_window

    def schedule(self, event: Event) -> None:
        """Queue `event` for its own instant.

        Scheduling *at* the current instant is ordinary and allowed: a bar, the fill it
        caused and the timer it woke legitimately share one timestamp, and priority and
        sequence order them. Scheduling before it is refused outright -- and refused
        rather than clamped, because a clamped fill happens at a plausible-looking time
        and produces an excellent equity curve with no error anywhere
        (`.claude/rules/no-lookahead.md`).
        """
        occurs_at_utc = event.occurs_at_utc
        current_utc = self._clock()
        if occurs_at_utc < current_utc:
            raise CausalityError(
                f"{type(event).__name__} was scheduled at {occurs_at_utc.isoformat()}, "
                f"before the instant being dispatched, {current_utc.isoformat()}. An "
                f"event cannot be observed before its cause"
            )
        if occurs_at_utc > self._config.end_utc:
            self._events_beyond_window += 1
            return
        self._queue.schedule(event)


class EventHandler(Protocol):
    """Whatever the loop dispatches into.

    One method, because the loop's contract is "here is an event, here is what you may
    do about it" and nothing more. Dispatching by event type is the handler's business:
    a `match` in one place beats seven callbacks the loop has to know the names of, and
    a new event type then fails to compile at the handler rather than being silently
    dropped by a loop that has no branch for it.
    """

    def on_event(self, event: Event, context: RunContext) -> None:
        """Handle one dispatched event. Never called through this Protocol at runtime."""


class EventLoop:
    """Drains a queue in total order, advancing simulated time as it goes."""

    __slots__ = ("_config", "_handler")

    def __init__(self, config: RunConfig, handler: EventHandler) -> None:
        self._config = config
        self._handler = handler

    def run(self, initial_events: Iterable[Event]) -> RunTrace:
        """Dispatch every event until the queue empties, and return the trace.

        `initial_events` is scheduled with the clock still at `start_utc`, so an event
        before the window opens raises rather than being quietly accepted -- the caller
        asked for a window and handed the loop data from outside it, and narrowing the
        window is the caller's decision to make.
        """
        queue = EventQueue()
        clock = SimulationClock(self._config.start_utc)
        context = RunContext(config=self._config, clock=clock, queue=queue)
        for event in initial_events:
            context.schedule(event)

        entries: list[TraceEntry] = []
        while queue:
            if len(entries) >= self._config.event_budget:
                raise EventBudgetExhaustedError(
                    f"run dispatched its full budget of {self._config.event_budget} events "
                    f"with {len(queue)} still queued at "
                    f"{clock().isoformat()}. A handler scheduling at its own instant "
                    f"never advances simulated time and the queue never drains"
                )
            queued = queue.pop()
            clock.advance_to(queued.occurs_at_utc)
            entries.append(
                TraceEntry(
                    occurs_at_utc=queued.occurs_at_utc,
                    priority=queued.priority,
                    sequence=queued.sequence,
                    event_type=type(queued.event).__name__,
                    event_digest=canonical_digest(encode(queued.event)),
                )
            )
            self._handler.on_event(queued.event, context)

        recorded = tuple(entries)
        return RunTrace(
            config_hash=config_hash(self._config),
            entries=recorded,
            digest=canonical_digest(encode(recorded)),
            events_beyond_window=context.events_beyond_window,
        )
