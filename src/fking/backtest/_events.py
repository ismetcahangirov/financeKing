"""Every event the loop can carry, and the priority that orders it against the others.

The priority table is the load-bearing part of this module and it is not a performance
tuning knob. Market data is 0 and venue events are 2 **on purpose**: when a fill and a
bar carry the same instant, the bar is dispatched first. The alternative lets a strategy
observe its own fill before the price that caused it, which is a look-ahead channel
wearing an ordering detail's clothes (`BACKTEST_ENGINE.md` section 2).

Three events derive `occurs_at_utc` from the record they carry rather than storing it:
a bar is knowable at its close, a trade at its print, a fill at the instant the venue
stamped on it. Storing the instant separately would make it possible to schedule a bar
at its *open* time -- which is the single most productive look-ahead bug there is,
because every feature computed from that bar then reads a candle that has not finished
forming, and nothing raises.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
from enum import IntEnum
from typing import ClassVar

from fking.backtest._guards import require_finite_decimal, require_text, require_utc
from fking.domain import Bar, Fill, Instrument, Order, Tick, Venue

__all__ = [
    "Event",
    "EventPriority",
    "FillEvent",
    "FundingEvent",
    "MarketDataEvent",
    "OrderAckEvent",
    "ReconciliationEvent",
    "RejectEvent",
    "TimerEvent",
]


class EventPriority(IntEnum):
    """Which event wins when two share an instant. Lower is dispatched first.

    An `IntEnum` rather than a bare int so the ordering key stays readable in a trace and
    so a new event type must choose a named band rather than inventing a number between
    two existing ones. The bands are stated in `BACKTEST_ENGINE.md` section 2 and are
    part of the engine's contract: changing one changes every historical result.
    """

    MARKET_DATA = 0
    FUNDING = 1
    VENUE = 2
    TIMER = 3
    RECONCILIATION = 4


@dataclass(frozen=True, slots=True)
class MarketDataEvent:
    """A closed bar or a printed trade becoming observable.

    `occurs_at_utc` is derived, never supplied. A `Bar` is dispatched at its
    `close_time_utc` because that is the first instant its high, low and close are
    facts; a `Tick` at the instant the venue printed it.
    """

    observation: Bar | Tick

    priority: ClassVar[EventPriority] = EventPriority.MARKET_DATA

    @property
    def occurs_at_utc(self) -> datetime:
        if isinstance(self.observation, Bar):
            return self.observation.close_time_utc
        return self.observation.event_time_utc


@dataclass(frozen=True, slots=True)
class FundingEvent:
    """A perpetual's funding settlement.

    Charged on the position held at this exact instant, never on average exposure. The
    discreteness is real and exploitable -- a strategy flat thirty seconds before
    settlement pays nothing and one holding through pays in full -- so it is modelled as
    an event rather than as an accrual (`BACKTEST_ENGINE.md` section 4.6).

    `funding_rate` is signed and dimensionless: longs pay the shorts when it is
    positive. Not `_fraction`, because that suffix is reserved for values in `[0, 1]`
    and a negative funding rate is the ordinary case in a bear market.
    """

    instrument: Instrument
    occurs_at_utc: datetime
    funding_rate: Decimal

    priority: ClassVar[EventPriority] = EventPriority.FUNDING

    def __post_init__(self) -> None:
        require_utc(self.occurs_at_utc, "occurs_at_utc")
        require_finite_decimal(self.funding_rate, "funding_rate")


@dataclass(frozen=True, slots=True)
class OrderAckEvent:
    """The venue acknowledging an order it has accepted but not yet filled.

    Scheduled by the venue at `submitted_at + ack_latency` rather than applied
    immediately, so the market moves during the interval exactly as it would live. A
    post-hoc basis-point penalty cannot reproduce the case where price moved through the
    limit during that window and the order never filled at all.
    """

    order: Order
    occurs_at_utc: datetime

    priority: ClassVar[EventPriority] = EventPriority.VENUE

    def __post_init__(self) -> None:
        require_utc(self.occurs_at_utc, "occurs_at_utc")


@dataclass(frozen=True, slots=True)
class FillEvent:
    """One execution, possibly partial.

    `occurs_at_utc` is the fill's own `event_time_utc` and cannot be set to anything
    else. A fill dispatched at a time other than the one recorded on it would reconcile
    against the venue at one instant and drive the portfolio at another, and the
    difference is invisible in every artefact except a byte-comparison of two runs.
    """

    fill: Fill

    priority: ClassVar[EventPriority] = EventPriority.VENUE

    @property
    def occurs_at_utc(self) -> datetime:
        return self.fill.event_time_utc


@dataclass(frozen=True, slots=True)
class RejectEvent:
    """The venue refusing an order, at the instant it would have acknowledged one.

    A rejection is an outcome, not an absence of one: it is dispatched, recorded and
    counted, because a backtest with zero rejections against a venue that rejects in
    reality is optimistic in a way no cost parameter captures.

    `reason` is free text until the venue simulator lands the closed rejection taxonomy
    (size against depth, lot and notional filters, price bands, rate budget). It is
    written into the trace verbatim, so a reason that changes between two runs of one
    config is a determinism failure rather than a cosmetic difference.
    """

    order: Order
    occurs_at_utc: datetime
    reason: str

    priority: ClassVar[EventPriority] = EventPriority.VENUE

    def __post_init__(self) -> None:
        require_utc(self.occurs_at_utc, "occurs_at_utc")
        require_text(self.reason, "reason")


@dataclass(frozen=True, slots=True)
class TimerEvent:
    """A wake-up a strategy asked for.

    Priority 3, below every venue event, so a strategy woken at the same instant as one
    of its own fills sees the fill first. A timer that fired before the fill would let a
    strategy act on a position it does not yet know it has.
    """

    strategy_id: str
    occurs_at_utc: datetime
    label: str

    priority: ClassVar[EventPriority] = EventPriority.TIMER

    def __post_init__(self) -> None:
        require_text(self.strategy_id, "strategy_id")
        require_utc(self.occurs_at_utc, "occurs_at_utc")
        require_text(self.label, "label")


@dataclass(frozen=True, slots=True)
class ReconciliationEvent:
    """A sweep comparing local state against the venue's.

    Last in the ordering because reconciliation reads state and must therefore see every
    other event at its instant already applied. It never fires under `BacktestVenue` --
    there is no second copy of the truth to diverge from -- and it exists here anyway
    because the loop is shared with the live venues, and a priority band invented later
    by whichever module needs it first is how two runs of one config stop agreeing.
    """

    venue: Venue
    occurs_at_utc: datetime

    priority: ClassVar[EventPriority] = EventPriority.RECONCILIATION

    def __post_init__(self) -> None:
        require_utc(self.occurs_at_utc, "occurs_at_utc")


type Event = (
    MarketDataEvent
    | FundingEvent
    | OrderAckEvent
    | FillEvent
    | RejectEvent
    | TimerEvent
    | ReconciliationEvent
)
