"""The one shape every simulated venue presents to the loop.

Three implementations exist before the abstraction does, which is the bar `CLAUDE.md`
section 3 sets: `BacktestVenue`, `PaperVenue` and `ReplayVenue`. `DemoVenue` will be the
fourth and lands with the testnet adapters (#11, P4).

The point of writing it down is not polymorphism for its own sake -- it is that a handler
typed against `SimulatedVenue` *cannot* ask which venue it holds. There is no discriminant
on this Protocol, no `kind`, no `is_backtest`, and nothing to switch on; the only way to
branch would be `isinstance`, which `tools/checks/venue_isolation.py` refuses across
`src/fking`. Parity is then a property of the type rather than of anybody's diligence
(`BACKTEST_ENGINE.md` section 3).

`decided_at_utc` is a parameter of `submit` rather than something a venue reads, for the
same reason. The instant a decision was taken belongs to whatever took it -- the event
loop in a backtest, wall time in a paper run -- and a venue that sourced its own would
give two runs of one configuration two different answers.
"""

from __future__ import annotations

from datetime import datetime
from typing import Protocol

from fking.backtest._events import FillEvent, OrderAckEvent, RejectEvent
from fking.backtest.venue._simulation import VenueReport
from fking.domain import Bar, Order

__all__ = ["SimulatedVenue"]


class SimulatedVenue(Protocol):
    """What a backtest, a paper run and a replay all answer to.

    Deliberately narrower than `fking.execution.ExecutionVenue`, and synchronous. That
    interface is the request surface of a *real* exchange -- balances, positions, open
    orders, cancel-replace -- and every one of those is an `await` against a network the
    simulated venues do not have. Widening this Protocol to match would mean three
    implementations of `fetch_balances` that answer from local state, which is a second,
    unreconciled copy of the truth in a system whose first rule about state is that the
    exchange owns it (`ARCHITECTURE.md` section 7).
    """

    def observe(self, bar: Bar) -> tuple[FillEvent, ...]:
        """Take a closed bar and return whatever resting interest it paid out."""

    def submit(self, order: Order, *, decided_at_utc: datetime) -> OrderAckEvent | RejectEvent:
        """Answer a submission at the instant the venue would have answered it."""

    def resolve_ack(self, ack: OrderAckEvent) -> tuple[FillEvent | RejectEvent, ...]:
        """Turn an acknowledged order into prints, a resting entry, or a refusal."""

    @property
    def report(self) -> VenueReport:
        """What this venue has done so far, including the reasons it refused."""
