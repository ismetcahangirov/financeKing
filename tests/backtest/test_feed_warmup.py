"""Warm-up bars advance state, reach no strategy, and produce no signal and no trade.

The strategy under test is deliberately the worst case: it fires on the *first* event it is
handed, unconditionally. Unwrapped it therefore fires on bar 1, which is a warm-up bar --
that is asserted first, because a gate whose failing input has never been demonstrated is a
gate nobody has shown to work. Wrapped in `WarmupGate`, the same handler cannot fire until
the exposure boundary, because the object is never passed to its strategy method at all.

Why this matters more than it looks: a strategy whose first signals come from a
partially-filled lookback is producing values no live run would ever have had, and they land
in the sample as though they were real. They are not random -- the early part of a lookback
is systematically smoother than the full one -- so every equity curve in the project is
inflated at its left edge, in the same direction, and the inflation survives averaging
across strategies.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path

import pytest

from fking.backtest import (
    Event,
    EventLoop,
    MarketDataEvent,
    RunConfig,
    RunContext,
    TimerEvent,
    WarmupGate,
)
from fking.backtest.feed import FeedSlice, MarketDataFeed, WarmupLeakError
from fking.domain import Bar
from tests.backtest import feed_support as fs
from tests.backtest.registration_support import REGISTERED

pytestmark = pytest.mark.unit

# The shared window: twenty warm-up bars, twenty exposed. The zero-warm-up test uses
# its own shorter window, because a strategy exposed from bar one is a different
# request rather than the same one with a flag.
WARMUP_BAR_COUNT = 20
EXPOSED_BAR_COUNT = 20
UNWARMED_BAR_COUNT = 10


@dataclass
class EagerStrategy:
    """Fires on every event it is asked to decide on, starting with the very first.

    A realistic strategy would make this suite depend on the strategy's correctness rather
    than on the boundary's. What is asserted is *which* bars reached `on_event`, so the
    cheapest handler that records that is the honest one.
    """

    signalled_at: list[datetime] = field(default_factory=list)
    warmed_at: list[datetime] = field(default_factory=list)
    traded_at: list[datetime] = field(default_factory=list)

    def on_warmup_bar(self, event: MarketDataEvent, context: RunContext) -> None:  # noqa: ARG002 - the Protocol's shape
        self.warmed_at.append(event.occurs_at_utc)

    def on_event(self, event: Event, context: RunContext) -> None:
        if isinstance(event, TimerEvent):
            # Stands in for a fill: the only thing that can occur after a signal, and the
            # thing whose count must be zero across the whole warm-up span.
            self.traded_at.append(event.occurs_at_utc)
            return
        self.signalled_at.append(event.occurs_at_utc)
        context.schedule(
            TimerEvent(strategy_id="eager", occurs_at_utc=event.occurs_at_utc, label="filled")
        )


def _config(loaded: FeedSlice) -> RunConfig:
    """The run window a caller derives from a slice: first event to last.

    `RunConfig.start_utc` is the *warm-up* start rather than the exposure boundary,
    because the loop's clock starts there and an event before it is a causality error.
    A run window that began at the exposure boundary would refuse every warm-up bar the
    feed just produced.
    """
    first_event_utc, last_event_utc = loaded.first_event_utc, loaded.last_event_utc
    assert first_event_utc is not None, "an empty slice has no run window"
    assert last_event_utc is not None
    return RunConfig(
        strategy_id="eager",
        strategy_version="1.0.0",
        symbols=("BTCUSDT",),
        parameters={},
        start_utc=first_event_utc,
        end_utc=last_event_utc,
        run_seed=20260801,
    )


def _load(
    tmp_path: Path, *, exposed_minute: int, until_minute: int, warmup_bar_count: int
) -> FeedSlice:
    fs.write_corpus(tmp_path)
    return MarketDataFeed(corpus_root=tmp_path, now_utc=fs.NOW_UTC).load(
        fs.request_for(
            exposed_minute=exposed_minute,
            until_minute=until_minute,
            warmup_bar_count=warmup_bar_count,
        )
    )


def test_the_ungated_strategy_fires_on_bar_one_which_is_a_warm_up_bar(tmp_path: Path) -> None:
    """The deliberate defect. If this ever stops firing, the gate below proves nothing."""
    loaded = _load(tmp_path, exposed_minute=20, until_minute=40, warmup_bar_count=20)
    strategy = EagerStrategy()

    EventLoop(_config(loaded), strategy, registration=REGISTERED).run(loaded.events)

    assert strategy.signalled_at[0] == loaded.events[0].occurs_at_utc
    assert strategy.signalled_at[0] < loaded.exposed_from_utc
    assert len(strategy.signalled_at) == len(loaded.events)


def test_the_gated_strategy_produces_zero_signals_and_zero_trades_during_warm_up(
    tmp_path: Path,
) -> None:
    """The acceptance criterion, on the same handler that fires on bar 1 without the gate."""
    loaded = _load(tmp_path, exposed_minute=20, until_minute=40, warmup_bar_count=20)
    strategy = EagerStrategy()
    gate = WarmupGate(strategy, exposed_from_utc=loaded.exposed_from_utc)

    EventLoop(_config(loaded), gate, registration=REGISTERED).run(loaded.events)

    assert [moment for moment in strategy.signalled_at if moment < loaded.exposed_from_utc] == []
    assert [moment for moment in strategy.traded_at if moment < loaded.exposed_from_utc] == []
    assert len(strategy.signalled_at) == loaded.exposed_event_count == EXPOSED_BAR_COUNT
    assert len(strategy.traded_at) == EXPOSED_BAR_COUNT


def test_warm_up_bars_still_reach_the_state_the_features_are_built_from(
    tmp_path: Path,
) -> None:
    """Not exposed is not discarded. A gate that dropped them would leave the first exposed
    bar with a lookback of one, which is the failure it exists to prevent wearing the other
    costume."""
    loaded = _load(tmp_path, exposed_minute=20, until_minute=40, warmup_bar_count=20)
    strategy = EagerStrategy()
    gate = WarmupGate(strategy, exposed_from_utc=loaded.exposed_from_utc)

    EventLoop(_config(loaded), gate, registration=REGISTERED).run(loaded.events)

    assert len(strategy.warmed_at) == loaded.warmup_event_count == WARMUP_BAR_COUNT
    assert gate.warmup_bar_count == WARMUP_BAR_COUNT
    assert max(strategy.warmed_at) < loaded.exposed_from_utc
    assert len(strategy.warmed_at) + len(strategy.signalled_at) == len(loaded.events)


def test_the_boundary_falls_between_the_bar_that_closes_before_it_and_the_one_that_opens_on_it(
    tmp_path: Path,
) -> None:
    """A bar is dispatched at its close, so the last warm-up bar is the last one to close
    before the boundary -- and the first exposed bar is the one *opening* on it. Comparing
    against the open instead would expose one bar too many, on every run, in the direction
    that hands the strategy an observation it should not have had."""
    loaded = _load(tmp_path, exposed_minute=20, until_minute=40, warmup_bar_count=20)
    strategy = EagerStrategy()
    gate = WarmupGate(strategy, exposed_from_utc=loaded.exposed_from_utc)

    EventLoop(_config(loaded), gate, registration=REGISTERED).run(loaded.events)

    first_exposed = next(
        event.observation
        for event in loaded.events
        if event.occurs_at_utc >= loaded.exposed_from_utc
    )
    assert isinstance(first_exposed, Bar)
    assert first_exposed.open_time_utc == loaded.exposed_from_utc
    assert strategy.signalled_at[0] == first_exposed.close_time_utc


def test_a_non_market_event_during_warm_up_stops_the_run(tmp_path: Path) -> None:
    """Nothing during warm-up can emit a signal, so nothing can fill, acknowledge or time
    out against one. An event that is not a bar before the boundary means the boundary has
    already been crossed somewhere, and continuing would record trades whose first ones came
    from a lookback no live run would ever have had."""
    loaded = _load(tmp_path, exposed_minute=20, until_minute=40, warmup_bar_count=20)
    gate = WarmupGate(EagerStrategy(), exposed_from_utc=loaded.exposed_from_utc)
    smuggled = TimerEvent(
        strategy_id="eager",
        occurs_at_utc=loaded.events[0].occurs_at_utc,
        label="a wake-up nothing could have scheduled",
    )

    with pytest.raises(WarmupLeakError, match="before the exposure boundary"):
        EventLoop(_config(loaded), gate, registration=REGISTERED).run((smuggled, *loaded.events))


def test_zero_warm_up_exposes_the_strategy_from_the_first_bar(tmp_path: Path) -> None:
    """Permitted, and not the default. A request that asks for no warm-up gets none, and the
    gate does not invent one."""
    loaded = _load(tmp_path, exposed_minute=0, until_minute=10, warmup_bar_count=0)
    strategy = EagerStrategy()
    gate = WarmupGate(strategy, exposed_from_utc=loaded.exposed_from_utc)

    EventLoop(_config(loaded), gate, registration=REGISTERED).run(loaded.events)

    assert loaded.warmup_event_count == 0
    assert strategy.warmed_at == []
    assert len(strategy.signalled_at) == UNWARMED_BAR_COUNT
