"""The heap the whole engine runs on, and the three-part key that totally orders it.

The key is `(occurs_at_utc, priority, sequence)` and `sequence` is a monotone counter
assigned at insertion. **No further tiebreaker may be added, and none is needed**: the
counter is unique per queue, so the key is injective and two events never compare equal.

That injectivity is the entire point. A key of `(occurs_at_utc, priority)` alone leaves
same-instant, same-band events tied, and a tie hands the decision to whatever the
comparison falls through to -- `heapq`'s sift order, which is an implementation detail
of CPython and not a documented stable sort, or worse, the payload's own `<`, which for
a dataclass raises and for anything comparable orders by content. None of those is a
decision this engine made, and none of them is written down anywhere a reader could
check it.

The failure mode is what makes this worth a module of its own: it does not crash. It
surfaces months later as two runs of the same `config_hash` disagreeing, at which point
nothing the engine has ever produced can be trusted (`BACKTEST_ENGINE.md` section 5).

Causality is deliberately *not* enforced here. The queue holds no clock, so it cannot
know whether an instant is in the past; that check belongs to the loop, which does. A
queue that also owned the clock would be two things, and the second one would be the
part every test had to construct.
"""

from __future__ import annotations

import heapq
from dataclasses import dataclass
from datetime import datetime

from fking.backtest._errors import BacktestError
from fking.backtest._events import Event, EventPriority

__all__ = ["EventQueue", "QueuedEvent"]


@dataclass(frozen=True, slots=True)
class QueuedEvent:
    """An event with the position the queue gave it.

    `__lt__` is written out rather than obtained from `order=True` so that the fields it
    reads are visible in one place. `order=True` would compare `event` as a fourth
    component whenever the first three tied -- which is exactly the fall-through this
    module exists to make impossible, and it would be invisible in the diff that
    introduced it.
    """

    occurs_at_utc: datetime
    priority: EventPriority
    sequence: int
    event: Event

    @property
    def ordering_key(self) -> tuple[datetime, int, int]:
        """The total order, exposed so a test can assert it is injective."""
        return (self.occurs_at_utc, int(self.priority), self.sequence)

    def __lt__(self, other: QueuedEvent) -> bool:
        return self.ordering_key < other.ordering_key


class EventQueue:
    """A single-consumer priority queue over simulated time.

    Not thread-safe and never will be. The loop is single-threaded by design: parallelism
    is available across independent runs, never inside one, because a second thread is a
    second source of ordering that no seed can reproduce.
    """

    __slots__ = ("_heap", "_sequence")

    def __init__(self) -> None:
        self._heap: list[QueuedEvent] = []
        self._sequence = 0

    def __len__(self) -> int:
        return len(self._heap)

    def schedule(self, event: Event) -> QueuedEvent:
        """Insert `event` and return the position it was given.

        The returned `QueuedEvent` is what the caller records. Re-deriving the sequence
        afterwards would mean reading a counter that has since moved.
        """
        queued = QueuedEvent(
            occurs_at_utc=event.occurs_at_utc,
            priority=event.priority,
            sequence=self._sequence,
            event=event,
        )
        self._sequence += 1
        heapq.heappush(self._heap, queued)
        return queued

    def peek(self) -> QueuedEvent | None:
        """The next event without removing it, or `None` when the queue is empty."""
        return self._heap[0] if self._heap else None

    def pop(self) -> QueuedEvent:
        """Remove and return the next event.

        Raises rather than returning `None` on an empty queue: the loop pops only while
        the queue is non-empty, so reaching this is a defect in the caller and a `None`
        would be discovered several frames later as an attribute error on nothing.
        """
        if not self._heap:
            raise BacktestError("pop from an empty event queue")
        return heapq.heappop(self._heap)
