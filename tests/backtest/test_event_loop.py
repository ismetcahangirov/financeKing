"""What the loop refuses, and what it advances.

Every refusal here is a refusal rather than a repair, and the reason is the same in each
case: the repaired form still produces a number. A clamped fill happens at a
plausible-looking instant, a truncated run reports a shorter window as though it were
the one requested, and a spinning loop reports nothing at all while occupying the
machine. None of those surfaces as an error, and all three make the result
unfalsifiable.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from fking.backtest import (
    CausalityError,
    Event,
    EventBudgetExhaustedError,
    EventLoop,
    MarketDataEvent,
    RunConfigError,
    RunContext,
    SimulationClock,
    TimerEvent,
)
from tests.backtest.registration_support import REGISTERED
from tests.support.backtest_events import (
    BAR_INTERVAL,
    RecordingHandler,
    SchedulingHandler,
    bar_at,
    bar_events,
    fill_event_at,
)
from tests.support.run_config import config_for

pytestmark = pytest.mark.unit

START = datetime(2026, 8, 1, 0, 0, tzinfo=UTC)


class _SpinningHandler:
    """Schedules a fresh event at the instant it is being dispatched at, forever."""

    def __init__(self) -> None:
        self._ordinal = 0

    def on_event(self, event: Event, context: RunContext) -> None:  # noqa: ARG002 - the Protocol's shape
        self._ordinal += 1
        context.schedule(fill_event_at(context.now_utc(), ordinal=self._ordinal))


class _ClockReader:
    """Records the instant the loop reports while each event is being dispatched."""

    def __init__(self) -> None:
        self.observed: list[datetime] = []

    def on_event(self, event: Event, context: RunContext) -> None:  # noqa: ARG002 - the Protocol's shape
        self.observed.append(context.now_utc())


def test_an_event_scheduled_before_the_current_instant_is_refused() -> None:
    """The follow-up is stamped a minute before the bar that scheduled it."""
    handler = SchedulingHandler(follow_ups=(fill_event_at(START, ordinal=1),))

    with pytest.raises(CausalityError, match="before the instant being dispatched"):
        EventLoop(config_for(start_utc=START), handler, registration=REGISTERED).run(
            bar_events(START, how_many=2)
        )


def test_an_initial_event_before_the_window_opens_is_refused() -> None:
    """The clock starts at `start_utc`, so data from before it is the caller's mistake.

    Refused rather than dropped: the caller asked for a window and handed the loop data
    from outside it, and narrowing the window is a decision only the caller can make.
    """
    too_early = MarketDataEvent(observation=bar_at(START - BAR_INTERVAL * 5))
    with pytest.raises(CausalityError, match="before the instant being dispatched"):
        EventLoop(config_for(start_utc=START), RecordingHandler(), registration=REGISTERED).run(
            [too_early]
        )


def test_scheduling_at_the_current_instant_is_ordinary_and_allowed() -> None:
    """A bar, the fill it caused and the timer it woke legitimately share one timestamp."""
    handler = SchedulingHandler(follow_ups=(fill_event_at(START + BAR_INTERVAL, ordinal=1),))
    trace = EventLoop(config_for(start_utc=START), handler, registration=REGISTERED).run(
        bar_events(START, how_many=1)
    )

    assert handler.type_names == ("MarketDataEvent", "FillEvent")
    assert {entry.occurs_at_utc for entry in trace.entries} == {START + BAR_INTERVAL}


def test_events_past_the_window_end_are_dropped_and_counted() -> None:
    """A fill acknowledged after the final bar is ordinary; losing it silently is not."""
    config = config_for(start_utc=START, window=timedelta(minutes=3))
    beyond = TimerEvent(
        strategy_id="s", occurs_at_utc=START + timedelta(minutes=90), label="too-late"
    )
    handler = SchedulingHandler(follow_ups=(beyond,))

    trace = EventLoop(config, handler, registration=REGISTERED).run(bar_events(START, how_many=2))

    assert trace.events_beyond_window == 1
    assert [entry.event_type for entry in trace.entries] == ["MarketDataEvent"] * 2
    assert all(entry.occurs_at_utc <= config.end_utc for entry in trace.entries)


def test_a_run_that_never_advances_its_clock_is_stopped_by_its_budget() -> None:
    """The hang this catches: a handler scheduling at its own instant, forever.

    Under an unattended evolution cycle that is a machine occupied indefinitely rather
    than a crash -- nothing times out and the generation never completes.
    """
    config = config_for(start_utc=START, event_budget=64)
    with pytest.raises(EventBudgetExhaustedError, match="budget of 64 events"):
        EventLoop(config, _SpinningHandler(), registration=REGISTERED).run(
            bar_events(START, how_many=1)
        )


def test_the_clock_reaches_each_event_and_never_moves_backwards() -> None:
    handler = _ClockReader()
    EventLoop(config_for(start_utc=START), handler, registration=REGISTERED).run(
        bar_events(START, how_many=4)
    )

    assert handler.observed == sorted(handler.observed)
    assert handler.observed[0] == START + BAR_INTERVAL
    assert handler.observed[-1] == START + BAR_INTERVAL * 4


def test_a_run_with_no_events_produces_an_empty_but_identified_trace() -> None:
    """An empty window is a real answer, and it still carries the identity that produced it."""
    trace = EventLoop(config_for(start_utc=START), RecordingHandler(), registration=REGISTERED).run(
        []
    )

    assert trace.event_count == 0
    assert trace.entries == ()
    assert trace.events_beyond_window == 0
    assert trace.config_hash


def test_the_simulation_clock_refuses_to_move_backwards() -> None:
    clock = SimulationClock(START)
    clock.advance_to(START + BAR_INTERVAL)
    clock.advance_to(START + BAR_INTERVAL)  # the same instant is ordinary

    assert clock() == START + BAR_INTERVAL
    with pytest.raises(CausalityError, match="cannot move backwards"):
        clock.advance_to(START)


def test_the_simulation_clock_refuses_a_naive_start() -> None:
    with pytest.raises(RunConfigError, match="timezone-aware"):
        SimulationClock(datetime(2026, 8, 1, 0, 0))  # noqa: DTZ001 - the value under test
