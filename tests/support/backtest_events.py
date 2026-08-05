"""Event builders and the handler stubs the engine suite dispatches into.

The handlers here are deliberately minimal. A realistic strategy would make the
determinism suite depend on the strategy's correctness rather than on the loop's, and
the loop is what these tests are about: the only behaviour a handler needs is to be
observable in the trace.

`DrawingHandler` exists in two shapes on purpose -- one taking its seed from the run,
one drawing from the machine. The second is the deliberate defect, in the same spirit as
the leaky feature specs the look-ahead probe is verified against: a determinism check
that no failing input can break is a check that has never been shown to work.
"""

from __future__ import annotations

import random
from collections.abc import Iterator, Sequence
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from decimal import Decimal
from uuid import UUID

from fking.backtest import Event, FillEvent, MarketDataEvent, RunContext, TimerEvent
from fking.domain import Bar, Fill, Instrument, Side
from tests.support.domain_factory import BTCUSDT

BAR_INTERVAL = timedelta(minutes=1)


def bar_at(
    open_time_utc: datetime,
    *,
    instrument: Instrument = BTCUSDT,
    close_quote_price: str = "64200.00",
) -> Bar:
    """One closed minute bar, knowable at `open_time_utc + BAR_INTERVAL`."""
    return Bar(
        instrument=instrument,
        open_time_utc=open_time_utc,
        close_time_utc=open_time_utc + BAR_INTERVAL,
        open_quote_price=Decimal("64000.00"),
        high_quote_price=Decimal("64500.00"),
        low_quote_price=Decimal("63800.00"),
        close_quote_price=Decimal(close_quote_price),
        base_volume=Decimal("12.5"),
        trade_count=4210,
    )


def bar_events(start_utc: datetime, how_many: int) -> tuple[MarketDataEvent, ...]:
    """`how_many` consecutive minute bars, as the events the loop is fed."""
    return tuple(
        MarketDataEvent(observation=bar_at(start_utc + BAR_INTERVAL * index))
        for index in range(how_many)
    )


def fill_event_at(event_time_utc: datetime, *, ordinal: int = 1) -> FillEvent:
    """A fill stamped at `event_time_utc`; `ordinal` only keeps distinct fills distinct."""
    return FillEvent(
        fill=Fill(
            fill_id=UUID(int=ordinal),
            order_id=UUID(int=1),
            venue_trade_id=str(ordinal),
            instrument=BTCUSDT,
            side=Side.BUY,
            event_time_utc=event_time_utc,
            quote_price=Decimal("64000.00"),
            base_quantity=Decimal("0.01000"),
            fee_quote=Decimal("0.64"),
        )
    )


@dataclass
class RecordingHandler:
    """Records what it was dispatched, in the order it arrived, and does nothing else."""

    dispatched: list[Event] = field(default_factory=list)

    def on_event(self, event: Event, context: RunContext) -> None:  # noqa: ARG002 - the Protocol's shape
        self.dispatched.append(event)

    @property
    def type_names(self) -> tuple[str, ...]:
        return tuple(type(event).__name__ for event in self.dispatched)


@dataclass
class SchedulingHandler:
    """Schedules a queue of follow-up events, one per market-data event dispatched.

    The follow-ups are supplied by the caller, so a test can hand it events at the
    *current* instant and assert that a mid-drain insertion still lands in the total
    order rather than at the front.
    """

    follow_ups: Sequence[Event]
    dispatched: list[Event] = field(default_factory=list)
    _pending: Iterator[Event] | None = None

    def on_event(self, event: Event, context: RunContext) -> None:
        self.dispatched.append(event)
        if not isinstance(event, MarketDataEvent):
            return
        if self._pending is None:
            self._pending = iter(self.follow_ups)
        follow_up = next(self._pending, None)
        if follow_up is not None:
            context.schedule(follow_up)

    @property
    def type_names(self) -> tuple[str, ...]:
        return tuple(type(event).__name__ for event in self.dispatched)


@dataclass
class DrawingHandler:
    """Draws a number per market-data event and writes it into the trace as a timer label.

    With `use_run_seed=True` the stream comes from `context.seed_for`, which is the only
    sanctioned source of randomness inside a run. With `use_run_seed=False` it comes from
    the machine's entropy -- the defect the determinism check exists to catch.
    """

    use_run_seed: bool
    label: str = "drawing-handler"
    drawn: list[int] = field(default_factory=list)
    _rng: random.Random | None = None

    def on_event(self, event: Event, context: RunContext) -> None:
        if not isinstance(event, MarketDataEvent):
            return
        if self._rng is None:
            self._rng = (
                random.Random(context.seed_for(self.label))  # noqa: S311 - reproducibility is the point
                if self.use_run_seed
                # The deliberate defect: unseeded, so two runs of one config diverge.
                else random.SystemRandom()
            )
        draw = self._rng.randrange(1_000_000_000)
        self.drawn.append(draw)
        context.schedule(
            TimerEvent(
                strategy_id="drawing",
                occurs_at_utc=event.occurs_at_utc,
                label=f"draw-{draw}",
            )
        )
