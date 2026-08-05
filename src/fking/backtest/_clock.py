"""Simulated time, advanced by the loop and read by everything else.

The shape is `Callable[[], datetime]`, matching every other injected clock in this
repository (`fking.platform.scheduler`, `fking.data.live.supervisor`). A `Clock`
protocol with a `now()` method was the alternative and buys nothing here: the callers
only ever need the current instant, and a one-method protocol is a callable with extra
ceremony that every test then has to implement.

Two properties make it worth being a class rather than a closure:

**It refuses to move backwards.** Simulated time going back is not a recoverable state,
it is the queue having handed the loop an event out of order -- and the loop would
otherwise keep running, dispatching events at instants that disagree with the trace it
is writing.

**Only the loop can advance it.** `advance_to` is public because a leading underscore
across a module boundary is a promise nobody keeps, but nothing outside `_engine` calls
it, and what a handler receives is `RunContext.now_utc`, which is the read half alone.
"""

from __future__ import annotations

from datetime import datetime

from fking.backtest._errors import CausalityError
from fking.backtest._guards import require_utc


class SimulationClock:
    """The loop's position in simulated time.

    Starts at the run window's first instant rather than at the first event, so an event
    scheduled before the window opens is a causality violation rather than a silent
    backwards step at the very first pop.
    """

    __slots__ = ("_now_utc",)

    def __init__(self, start_utc: datetime) -> None:
        self._now_utc = require_utc(start_utc, "start_utc")

    def __call__(self) -> datetime:
        """The current simulated instant."""
        return self._now_utc

    def advance_to(self, instant_utc: datetime) -> None:
        """Move to `instant_utc`. Called by the event loop and by nothing else.

        Advancing to the *same* instant is allowed and ordinary: a bar, the fill it
        caused and the timer it woke can all share one timestamp, and they are ordered
        by priority and sequence rather than by the clock.
        """
        require_utc(instant_utc, "instant_utc")
        if instant_utc < self._now_utc:
            raise CausalityError(
                f"simulated time cannot move backwards: asked to advance to "
                f"{instant_utc.isoformat()} from {self._now_utc.isoformat()}. The event "
                f"queue produced an out-of-order pop, which makes every result from this "
                f"run void rather than merely wrong"
            )
        self._now_utc = instant_utc
