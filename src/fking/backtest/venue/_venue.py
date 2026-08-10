"""`BacktestVenue` and `PaperVenue`: one fill simulator, two sources of time.

Neither class prices a fill. Both hold a `FillSimulation` and forward to it, so the
question "do a backtest and a paper run fill the same way" is answered by reading two
constructors rather than by diffing two implementations. That is the whole difference
between architectural parity and disciplinary parity: there is nothing here to keep in
sync, because there is nothing here that differs (`BACKTEST_ENGINE.md` section 3).

What *does* differ is where time comes from, and it is the only thing that differs.

- A backtest is driven by the event loop. It hands the venue an ack when the queue reaches
  the instant the venue scheduled it for, so `BacktestVenue` needs no clock at all.
- A paper run has no queue to reach an instant. Wall time passes on its own, so
  `PaperVenue` holds an injected clock and releases each scheduled ack once that clock has
  gone past it -- `due_events()` is the paper analogue of the loop popping a venue event.

`PaperVenue`'s clock is a constructor argument with no default. A default would be
`datetime.now`, which would put a wall-clock read inside `fking.backtest` --
`tools/checks/clock_isolation.py` treats this package as pure and would reject it, and it
is right to: a paper session replayed against a recorded clock is the only way a paper
disagreement is ever debugged twice.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping
from datetime import datetime, timedelta

from fking.backtest._events import FillEvent, OrderAckEvent, RejectEvent
from fking.backtest.costs import CostModel, SpreadQuantile
from fking.backtest.venue._errors import VenueSimulationError
from fking.backtest.venue._filters import SymbolFilters
from fking.backtest.venue._resting import RestingOrder
from fking.backtest.venue._simulation import (
    DEFAULT_SEQUENTIAL_FILL_GAP,
    FillSimulation,
    SubmissionSchedule,
    VenueReport,
)
from fking.domain import Bar, Order

__all__ = ["BacktestVenue", "PaperVenue"]

#: The shape of every injected clock in this repository: no arguments, a tz-aware UTC
#: instant out. Spelled here rather than imported from `fking.strategy` so this package
#: does not acquire an import edge for a two-token alias.
Clock = Callable[[], datetime]


class BacktestVenue:
    """The simulated venue a backtest runs against, on the event loop's clock.

    The constructor keywords are the simulator's, forwarded unchanged. It could take a
    `FillSimulation` instead, and deliberately does not: a caller that builds the
    simulator itself can hand the same instance to two venues, and two venues sharing one
    order book is a state-corruption bug with no error message.
    """

    __slots__ = ("_simulation",)

    def __init__(  # noqa: PLR0913 - one keyword per calibrated input the simulator needs;
        # a settings object would be a second place to keep the cost model's shape in step.
        self,
        *,
        cost_model: CostModel,
        filters: Mapping[str, SymbolFilters],
        order_rate_budget: int,
        order_rate_window: timedelta,
        quantile: SpreadQuantile = SpreadQuantile.P50,
        sequential_fill_gap: timedelta = DEFAULT_SEQUENTIAL_FILL_GAP,
    ) -> None:
        self._simulation = FillSimulation(
            cost_model=cost_model,
            filters=filters,
            order_rate_budget=order_rate_budget,
            order_rate_window=order_rate_window,
            quantile=quantile,
            sequential_fill_gap=sequential_fill_gap,
        )

    def observe(self, bar: Bar) -> tuple[FillEvent, ...]:
        """Take a closed bar and pay the resting queue what it earned."""
        return self._simulation.observe(bar)

    def schedule_for(self, decided_at_utc: datetime) -> SubmissionSchedule:
        """The three latency stages applied to a decision taken at `decided_at_utc`."""
        return self._simulation.schedule_for(decided_at_utc)

    def submit(self, order: Order, *, decided_at_utc: datetime) -> OrderAckEvent | RejectEvent:
        """Screen an order and answer at the instant the venue would have answered."""
        return self._simulation.submit(order, decided_at_utc=decided_at_utc)

    def resolve_ack(self, ack: OrderAckEvent) -> tuple[FillEvent | RejectEvent, ...]:
        """Turn an acknowledged order into prints, a resting entry, or a refusal."""
        return self._simulation.resolve_ack(ack)

    @property
    def report(self) -> VenueReport:
        """What this venue has done so far, including the reasons it refused."""
        return self._simulation.report

    @property
    def resting_orders(self) -> tuple[RestingOrder, ...]:
        """Everything still working, in a stable order."""
        return self._simulation.resting_orders


class PaperVenue:
    """Live data, simulated fills, live clock.

    The same simulator as a backtest, driven by wall time instead of by a queue. A
    submission is stamped with the instant the caller decided it and validated against the
    clock: a decision stamped ahead of wall time is refused rather than accepted, because
    the only way to produce one is a bug, and the bug it produces is a fill at a price the
    decision could not have seen.

    Acks are held rather than returned resolved. `submit` schedules one at
    `decided_at + decision_to_send + send_to_ack` exactly as the backtest does, and
    `due_events()` releases it once the clock has passed that instant -- so the market data
    that arrived during the latency window is already in the book when the order resolves
    against it, which is the property scheduling latency exists to preserve.
    """

    __slots__ = ("_clock", "_pending_acks", "_simulation")

    def __init__(  # noqa: PLR0913 - the simulator's calibrated inputs plus the clock;
        # collapsing them into a settings object would hide the one injection point that
        # makes a paper session replayable.
        self,
        *,
        clock: Clock,
        cost_model: CostModel,
        filters: Mapping[str, SymbolFilters],
        order_rate_budget: int,
        order_rate_window: timedelta,
        quantile: SpreadQuantile = SpreadQuantile.P50,
        sequential_fill_gap: timedelta = DEFAULT_SEQUENTIAL_FILL_GAP,
    ) -> None:
        self._clock = clock
        self._simulation = FillSimulation(
            cost_model=cost_model,
            filters=filters,
            order_rate_budget=order_rate_budget,
            order_rate_window=order_rate_window,
            quantile=quantile,
            sequential_fill_gap=sequential_fill_gap,
        )
        self._pending_acks: list[OrderAckEvent] = []

    def observe(self, bar: Bar) -> tuple[FillEvent, ...]:
        """Take a closed live bar and pay the resting queue what it earned."""
        return self._simulation.observe(bar)

    def schedule_for(self, decided_at_utc: datetime) -> SubmissionSchedule:
        """The three latency stages applied to a decision taken at `decided_at_utc`."""
        return self._simulation.schedule_for(decided_at_utc)

    def submit(self, order: Order, *, decided_at_utc: datetime) -> OrderAckEvent | RejectEvent:
        """Screen an order, holding any ack until the clock reaches it."""
        now_utc = self._clock()
        if decided_at_utc > now_utc:
            raise VenueSimulationError(
                f"{order.client_order_id} was decided at {decided_at_utc.isoformat()}, "
                f"after the paper clock reads {now_utc.isoformat()}; a decision cannot be "
                f"taken in the future"
            )
        answer = self._simulation.submit(order, decided_at_utc=decided_at_utc)
        if isinstance(answer, OrderAckEvent):
            self._pending_acks.append(answer)
        return answer

    def due_events(self) -> tuple[FillEvent | RejectEvent, ...]:
        """Resolve every held ack the clock has passed, oldest first.

        Called by whatever drives the session -- on each new bar, on a timer, or both.
        Nothing resolves early: an ack scheduled 180ms out stays held until the clock says
        180ms have passed, so a paper run cannot fill against a book that had not yet
        received the market data the real venue would have.
        """
        now_utc = self._clock()
        due = sorted(
            (ack for ack in self._pending_acks if ack.occurs_at_utc <= now_utc),
            key=lambda ack: (ack.occurs_at_utc, ack.order.client_order_id),
        )
        self._pending_acks = [ack for ack in self._pending_acks if ack.occurs_at_utc > now_utc]
        released: list[FillEvent | RejectEvent] = []
        for ack in due:
            released.extend(self._simulation.resolve_ack(ack))
        return tuple(released)

    def resolve_ack(self, ack: OrderAckEvent) -> tuple[FillEvent | RejectEvent, ...]:
        """Resolve one specific held ack, refusing one the clock has not reached.

        Present so a `PaperVenue` satisfies `SimulatedVenue` and can be driven by the same
        handler as a backtest. The clock check is what keeps that honest: a caller cannot
        use this entry point to pull a fill forward past its latency.
        """
        now_utc = self._clock()
        if ack.occurs_at_utc > now_utc:
            raise VenueSimulationError(
                f"ack for {ack.order.client_order_id} is scheduled at "
                f"{ack.occurs_at_utc.isoformat()} and the paper clock reads "
                f"{now_utc.isoformat()}; resolving it now would fill against a book the "
                f"venue has not seen yet"
            )
        self._pending_acks = [held for held in self._pending_acks if held != ack]
        return self._simulation.resolve_ack(ack)

    @property
    def pending_acks(self) -> tuple[OrderAckEvent, ...]:
        """Acks the venue has scheduled and the clock has not yet reached."""
        return tuple(
            sorted(
                self._pending_acks, key=lambda ack: (ack.occurs_at_utc, ack.order.client_order_id)
            )
        )

    @property
    def report(self) -> VenueReport:
        """What this venue has done so far, including the reasons it refused."""
        return self._simulation.report

    @property
    def resting_orders(self) -> tuple[RestingOrder, ...]:
        """Everything still working, in a stable order."""
        return self._simulation.resting_orders
