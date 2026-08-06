"""The warm-up boundary, enforced by the shape of the interface rather than by a flag.

A warm-up bar advances whatever holds state -- the feature store -- and is never exposed
to strategy evaluation. The tempting implementation is a boolean on the event, or an
`if context.now_utc() < exposed_from` at the top of the strategy's handler. Both put the
guarantee in the hands of every handler ever written, and this system writes its own
strategies: an LLM-authored handler will read the flag if the type system lets it forget,
and it will forget in the case that matters, because a partially-warmed feature produces a
plausible number rather than an error.

So the gate is a *handler*. `WarmupGate` implements the engine's `EventHandler` and speaks
to a `FeedHandler` with two methods. A bar before the exposure boundary reaches
`on_warmup_bar` and there is no code path by which it reaches `on_event` -- the strategy
side of the handler is not passed the object at all, so it cannot decide to look.

The second half is `WarmupLeakError`. Nothing during warm-up can emit a signal, so nothing
can acknowledge, fill or reject an order at a warm-up instant, and a timer can only have
been scheduled by something that acted on a warm-up bar. Any non-market-data event before
the boundary therefore means the boundary has already been crossed somewhere, and the run
is stopped rather than allowed to record trades whose first ones came from a lookback no
live run would ever have had.

The boundary is compared against `Event.occurs_at_utc`, which for a bar is its **close** --
the first instant its high, low and close are facts. So the last warm-up bar is the last
one whose close precedes `exposed_from_utc`, and the first exposed bar is the one opening
at `exposed_from_utc` itself. Comparing against the open instead would expose one bar too
many, every run, in the direction that adds an observation the strategy should not have had.
"""

from __future__ import annotations

from datetime import datetime
from typing import Protocol

from fking.backtest._engine import RunContext
from fking.backtest._events import Event, MarketDataEvent
from fking.backtest.feed._errors import WarmupLeakError

__all__ = ["FeedHandler", "WarmupGate"]


class FeedHandler(Protocol):
    """A handler that distinguishes warming its state from being asked to decide.

    Two methods rather than one plus a flag, because the distinction has to be visible in
    the *signature*: a handler with one method and an `is_warmup` argument compiles whether
    or not the argument is read.
    """

    def on_warmup_bar(self, event: MarketDataEvent, context: RunContext) -> None:
        """Advance state from a bar the strategy must not see. Never called via Protocol."""

    def on_event(self, event: Event, context: RunContext) -> None:
        """Handle a dispatched event at or after the exposure boundary. Never via Protocol."""


class WarmupGate:
    """Routes events either side of the exposure boundary, and refuses to blur it."""

    __slots__ = ("_exposed_from_utc", "_handler", "_warmup_bar_count")

    def __init__(self, handler: FeedHandler, *, exposed_from_utc: datetime) -> None:
        self._handler = handler
        self._exposed_from_utc = exposed_from_utc
        self._warmup_bar_count = 0

    @property
    def warmup_bar_count(self) -> int:
        """How many bars were routed to warm-up. Compared against what the feed reported."""
        return self._warmup_bar_count

    def on_event(self, event: Event, context: RunContext) -> None:
        """Dispatch one event to the warm-up side or the strategy side of the handler.

        Raises:
            WarmupLeakError: a non-market-data event was dispatched before the boundary.
        """
        if event.occurs_at_utc >= self._exposed_from_utc:
            self._handler.on_event(event, context)
            return
        if not isinstance(event, MarketDataEvent):
            raise WarmupLeakError(
                f"{type(event).__name__} was dispatched at {event.occurs_at_utc.isoformat()}, "
                f"before the exposure boundary {self._exposed_from_utc.isoformat()}. Warm-up "
                f"bars reach no strategy, so nothing during warm-up can emit a signal and "
                f"nothing can fill, acknowledge or time out against one -- this event means "
                f"something acted on a partially-filled lookback"
            )
        self._warmup_bar_count += 1
        self._handler.on_warmup_bar(event, context)
