"""One session harness, driven by the real event loop, parameterised only by the venue.

The harness is the test's evidence, so it is built to make cheating visible. There is one
`on_event`, one strategy call site and one order-construction site, and the venue arrives
as a constructor argument typed `SimulatedVenue` -- which carries no discriminant, so the
handler could not branch on which venue it holds even if somebody wanted it to.

`ParitySession` deliberately runs on `EventLoop` rather than on a hand-rolled for-loop
over bars. A for-loop would prove that two venues agree under *this file's* ordering; the
loop is the ordering the engine actually uses, including the rule that a bar at an instant
is dispatched before a fill at the same instant, and parity under a different ordering
would be parity nobody can rely on.

Orders are constructed here rather than by `fking.risk`. That is a scope decision, not an
oversight: the property under test is that the *signal* stream is venue-independent, and
wiring the real sizing engine would add a portfolio, a correlation matrix and a
calibration map to a test whose subject is none of those. `tools/checks/order_construction`
governs `src/fking`, where the rule that only `fking.risk` builds an `Order` belongs.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Final
from uuid import NAMESPACE_URL, UUID, uuid5

from fking.backtest import (
    Event,
    EventLoop,
    FillEvent,
    MarketDataEvent,
    OrderAckEvent,
    RejectEvent,
    RunConfig,
    RunContext,
    RunTrace,
)
from fking.backtest.venue import SimulatedVenue, VenueRecorder
from fking.domain import (
    Bar,
    Direction,
    Fill,
    Instrument,
    Order,
    OrderType,
    Side,
    Signal,
    TimeInForce,
)
from fking.strategy import FeatureRequirement, Strategy, StrategyState, initial_state, step
from tests.backtest.registration_support import PATH_LABEL, REGISTERED, RecordingReporter
from tests.strategy.harness import (
    BTCUSDT,
    SERIES_START_UTC,
    bars_from_closes,
    clock_at,
    feature_values_for,
    rising_closes,
)
from tests.support.run_config import config_for

_ORDER_NAMESPACE: Final[UUID] = uuid5(NAMESPACE_URL, "https://fking.local/test/parity/order")

#: Small enough to clear the recorded ±1% depth band and large enough to clear the
#: recorded 5.00 USDT notional floor at a 64,000 mark.
ORDER_BASE_QUANTITY: Final[Decimal] = Decimal("0.01")

#: Long enough to clear `TrailingReturnContinuation`'s declared warm-up with room for the
#: signal stream to be non-trivial, short enough that the run is milliseconds.
BAR_COUNT: Final[int] = 40

RUN_SEED: Final[int] = 20260801

#: Ten basis points through the close, which clears the modelled 2bp touch with room to
#: spare. Large enough that a change in the spread fixture does not silently stop the
#: orders crossing; small enough to stay inside the recorded PERCENT_PRICE_BY_SIDE band.
MARKETABLE_OFFSET: Final[Decimal] = Decimal("0.001")


def parity_bars() -> tuple[Bar, ...]:
    """The window every parity run is driven over."""
    return bars_from_closes(rising_closes(BAR_COUNT), instrument=BTCUSDT)


def parity_config(bars: tuple[Bar, ...]) -> RunConfig:
    """A window that closes after the last bar plus enough room for its latency tail."""
    return config_for(
        symbols=(BTCUSDT.symbol,),
        start_utc=SERIES_START_UTC,
        window=bars[-1].close_time_utc - SERIES_START_UTC + timedelta(hours=1),
        run_seed=RUN_SEED,
    )


@dataclass(slots=True)
class SessionOutcome:
    """What one run of the harness produced.

    `signals` is the assertion target and `fills` is present so a test can show that the
    two runs genuinely differed somewhere -- a parity test that passes because both
    venues did nothing is not a parity test.
    """

    signals: list[Signal] = field(default_factory=list)
    fills: list[Fill] = field(default_factory=list)
    rejections: list[str] = field(default_factory=list)


class ParitySession:
    """Strategy, orders and portfolio bookkeeping; venue by constructor argument."""

    def __init__(
        self,
        *,
        strategy: Strategy,
        venue: SimulatedVenue,
        feature_values_at: Mapping[datetime, Mapping[FeatureRequirement, Decimal]],
        recorder: VenueRecorder | None = None,
    ) -> None:
        self._strategy = strategy
        self._venue = venue
        self._feature_values_at: Mapping[datetime, Mapping[FeatureRequirement, Decimal]] = (
            feature_values_at
        )
        self._recorder = recorder
        self._state: StrategyState = initial_state(seed=RUN_SEED)
        self._submitted = 0
        self.outcome = SessionOutcome()

    def on_event(self, event: Event, context: RunContext) -> None:
        """Dispatch one event. A `match` in one place, as `EventHandler` intends."""
        match event:
            case MarketDataEvent(observation=Bar() as bar):
                self._on_bar(bar, context)
            case OrderAckEvent():
                self._on_ack(event, context)
            case FillEvent():
                self.outcome.fills.append(event.fill)
            case RejectEvent():
                self.outcome.rejections.append(event.reason)
            case _:
                pass

    def _on_bar(self, bar: Bar, context: RunContext) -> None:
        observed = self._venue.observe(bar)
        if self._recorder is not None:
            self._recorder.record_observed(observed)
        for fill_event in observed:
            context.schedule(fill_event)

        outcome = step(
            self._strategy,
            self._state,
            bar,
            clock_at(bar.close_time_utc),
            feature_values=self._features_at(bar.close_time_utc),
        )
        self._state = outcome.state
        if outcome.signal is None:
            return
        self.outcome.signals.append(outcome.signal)
        if outcome.signal.direction is Direction.FLAT:
            return
        order = self._order_for(outcome.signal, bar)
        answer = self._venue.submit(order, decided_at_utc=context.now_utc())
        if self._recorder is not None:
            self._recorder.record_submission(answer)
        context.schedule(answer)

    def _on_ack(self, ack: OrderAckEvent, context: RunContext) -> None:
        resolved = self._venue.resolve_ack(ack)
        if self._recorder is not None:
            self._recorder.record_ack_outcome(resolved)
        for event in resolved:
            context.schedule(event)

    def _features_at(self, as_of: datetime) -> Mapping[FeatureRequirement, Decimal]:
        return self._feature_values_at.get(as_of, {})

    def _order_for(self, signal: Signal, bar: Bar) -> Order:
        """A marketable limit order, with an id derived from the submission count.

        Priced through the touch rather than at the close. A limit resting *at* the close
        of a rising series never fills, and a parity run in which no order ever filled
        would compare two empty fill streams and call it agreement -- so the session would
        pass while proving nothing about the venue at all.

        The id is deterministic in the run rather than in the wall clock, so the same
        session replayed against a recording asks for the same client order ids, and a run
        that diverged asks for one the recording does not hold, which raises.
        """
        self._submitted += 1
        client_order_id = f"fk-parity-{self._submitted:04d}"
        instrument: Instrument = bar.instrument
        side = Side.BUY if signal.direction is Direction.LONG else Side.SELL
        return Order(
            order_id=uuid5(_ORDER_NAMESPACE, client_order_id),
            client_order_id=client_order_id,
            correlation_id=uuid5(_ORDER_NAMESPACE, f"{client_order_id}-correlation"),
            instrument=instrument,
            side=side,
            order_type=OrderType.LIMIT,
            time_in_force=TimeInForce.GTC,
            base_quantity=ORDER_BASE_QUANTITY,
            limit_quote_price=instrument.quantize_quote_price(
                bar.close_quote_price * (Decimal("1") + MARKETABLE_OFFSET * side.signed_multiplier),
                side,
            ),
            created_at_utc=bar.close_time_utc,
        )


def run_session(
    *,
    strategy: Strategy,
    venue: SimulatedVenue,
    bars: tuple[Bar, ...],
    recorder: VenueRecorder | None = None,
) -> tuple[SessionOutcome, RunTrace]:
    """Drive `bars` through the real event loop against `venue`."""
    session = ParitySession(
        strategy=strategy,
        venue=venue,
        feature_values_at=feature_values_for(strategy.spec, bars),
        recorder=recorder,
    )
    loop = EventLoop(
        parity_config(bars),
        session,
        registration=REGISTERED,
        reporter=RecordingReporter(),
        path_label=PATH_LABEL,
    )
    trace = loop.run(MarketDataEvent(observation=bar) for bar in bars)
    return session.outcome, trace


def signal_fingerprints(signals: list[Signal]) -> list[str]:
    """Every field of every signal as exact text, in emission order.

    Exact decimal text rather than a numeric comparison: `Decimal("0.10")` and
    `Decimal("0.1")` are equal numerically and are two different beliefs about precision,
    and a parity check that tolerates the difference tolerates the fifteenth digit moving
    -- which is what a leak looks like before it looks like anything else.
    """
    return [
        "|".join(
            (
                signal.strategy_id,
                signal.instrument.symbol,
                signal.direction.value,
                format(signal.conviction, "f"),
                str(signal.horizon),
                signal.decided_at_utc.astimezone(UTC).isoformat(),
                (
                    ""
                    if signal.invalidation_quote_price is None
                    else format(signal.invalidation_quote_price, "f")
                ),
            )
        )
        for signal in signals
    ]


def assert_signal_parity(left: SessionOutcome, right: SessionOutcome) -> None:
    """Fail unless the two runs emitted the identical `Signal` sequence.

    An assertion rather than a returned boolean, and never a warning. A parity check that
    warns is a parity check that is ignored, and the thing it is ignoring is every
    backtest result the project has produced becoming unfalsifiable.
    """
    assert signal_fingerprints(left.signals) == signal_fingerprints(right.signals)
