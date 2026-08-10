"""Backtest/live parity, asserted on signals and nothing else.

Fills legitimately differ between a simulated venue and a replayed one -- different
latency realisations, different queue outcomes. Signals must not differ at all. A parity
test that asserted on fills would fail for correct reasons, get marked flaky, and be
deleted inside a quarter; this one fails only when parity is actually broken.

The negative control is the half that makes the first half mean anything. A test that
passes because both runs emitted nothing, or because the assertion is too loose to see a
difference, is indistinguishable from a test that works -- so
`test_a_venue_conditional_strategy_breaks_parity` builds exactly the drift this design
exists to prevent and asserts the check fails on it, loudly, rather than warning.
"""

from __future__ import annotations

import sys
from collections.abc import Callable
from types import CodeType
from typing import Final

import pytest

from fking.backtest.venue import (
    ReplayVenue,
    VenueRecorder,
    VenueRecording,
    VenueRecordingError,
)
from fking.backtest.venue import _simulation as simulation_module
from fking.domain import Bar, Signal
from fking.strategy import Clock, StrategySpec, StrategyState
from fking.strategy.trailing_return import TrailingReturnContinuation
from tests.backtest.parity_support import (
    ORDER_BASE_QUANTITY,
    assert_signal_parity,
    parity_bars,
    run_session,
    signal_fingerprints,
)
from tests.backtest.venue_support import (
    EPOCH,
    SteppingClock,
    make_bar,
    make_order,
    make_paper_venue,
    make_venue,
)
from tests.strategy.harness import BTCUSDT

pytestmark = pytest.mark.unit

_SIMULATION_FILE: Final[str] = simulation_module.__file__


def _strategy() -> TrailingReturnContinuation:
    return TrailingReturnContinuation((BTCUSDT,))


class VenueConditionalStrategy:
    """A strategy that behaves differently depending on how the run was wired.

    This is the defect, written down. Nobody adds it in one step: they add "the paper run
    needs one extra bar of confirmation because live data arrives late", the parity test
    is not looking at signals, and six weeks later the backtest and the paper run are two
    different strategies sharing one name and one track record.
    """

    __slots__ = ("_inner", "_suppress")

    def __init__(self, inner: TrailingReturnContinuation, *, suppress: bool) -> None:
        self._inner = inner
        self._suppress = suppress

    @property
    def spec(self) -> StrategySpec:
        return self._inner.spec

    def evaluate(self, state: StrategyState, bar: Bar, clock: Clock) -> Signal | None:
        signal = self._inner.evaluate(state, bar, clock)
        if self._suppress and state.bars_consumed % 2 == 0:
            return None
        return signal


def _recorded_backtest_run() -> tuple[VenueRecording, list[Signal]]:
    """Run the strategy against `BacktestVenue`, capturing everything the venue answered."""
    bars = parity_bars()
    venue = make_venue()
    recorder = VenueRecorder()
    outcome, _ = run_session(strategy=_strategy(), venue=venue, bars=bars, recorder=recorder)
    return recorder.build(venue.report), outcome.signals


def test_backtest_and_replay_emit_an_identical_signal_sequence() -> None:
    """The acceptance criterion: one strategy, one window, two venues, one signal stream."""
    recording, backtest_signals = _recorded_backtest_run()

    replay_outcome, _ = run_session(
        strategy=_strategy(),
        venue=ReplayVenue(recording=recording),
        bars=parity_bars(),
    )

    assert backtest_signals, "a parity test over an empty signal stream proves nothing"
    assert signal_fingerprints(backtest_signals) == signal_fingerprints(replay_outcome.signals)


def test_both_runs_really_reached_the_venue() -> None:
    """Parity over a run that never submitted an order would be parity over nothing."""
    recording, signals = _recorded_backtest_run()

    assert signals
    assert recording.responses
    assert any(response.fills for response in recording.responses)


def test_a_venue_conditional_strategy_breaks_parity() -> None:
    """The negative control: the check fails on drift rather than warning about it."""
    bars = parity_bars()
    backtest_venue = make_venue()
    recorder = VenueRecorder()
    backtest_outcome, _ = run_session(
        strategy=_strategy(), venue=backtest_venue, bars=bars, recorder=recorder
    )

    drifted_outcome, _ = run_session(
        strategy=VenueConditionalStrategy(_strategy(), suppress=True),
        venue=make_venue(),
        bars=bars,
    )

    assert signal_fingerprints(drifted_outcome.signals) != signal_fingerprints(
        backtest_outcome.signals
    )
    with pytest.raises(AssertionError):
        assert_signal_parity(backtest_outcome, drifted_outcome)


def test_a_drifted_run_cannot_be_replayed_against_the_recording_it_claims() -> None:
    """The second line of defence, and the louder one.

    A run whose signals drifted submits different orders at different instants, so the
    replay is asked for a response the recorded session never produced. It raises rather
    than improvising one -- a replay that filled in a plausible substitute would turn a
    divergence into a slightly different but perfectly presentable result.
    """
    bars = parity_bars()
    backtest_venue = make_venue()
    recorder = VenueRecorder()
    run_session(strategy=_strategy(), venue=backtest_venue, bars=bars, recorder=recorder)

    with pytest.raises(VenueRecordingError, match="different session"):
        run_session(
            strategy=VenueConditionalStrategy(_strategy(), suppress=True),
            venue=ReplayVenue(recording=recorder.build(backtest_venue.report)),
            bars=bars,
        )


def _executed_lines(exercise: Callable[[], None]) -> frozenset[int]:
    """Which lines of the fill simulator `exercise` ran.

    `sys.monitoring` under its own tool id rather than a nested `coverage` run. Coverage is
    already tracing this process under `--cov`, and a second `settrace`-based collector
    started inside it silently replaces the first -- so the measurement would corrupt the
    coverage report the same command is producing. The monitoring API is multi-tool by
    construction and cannot.
    """
    monitoring = sys.monitoring
    tool_id = next(
        candidate
        for candidate in range(monitoring.PROFILER_ID, monitoring.OPTIMIZER_ID + 1)
        if monitoring.get_tool(candidate) is None
    )
    seen: set[int] = set()

    def on_line(code: CodeType, line_number: int) -> object:
        if code.co_filename != _SIMULATION_FILE:
            # DISABLE retires this tool's LINE events for that code object entirely, so
            # the cost of measuring one module is not paid by every other module.
            return monitoring.DISABLE
        seen.add(line_number)
        return None

    monitoring.use_tool_id(tool_id, "fking-parity-lines")
    try:
        monitoring.register_callback(tool_id, monitoring.events.LINE, on_line)
        monitoring.set_events(tool_id, monitoring.events.LINE)
        exercise()
    finally:
        monitoring.set_events(tool_id, monitoring.events.NO_EVENTS)
        monitoring.register_callback(tool_id, monitoring.events.LINE, None)
        monitoring.free_tool_id(tool_id)
        monitoring.restart_events()
    return frozenset(seen)


def test_paper_and_backtest_execute_the_same_fill_simulation_lines() -> None:
    """The two venues do not merely agree: they run the same lines of the same module.

    Behavioural equality would be satisfied by two implementations that happen to compute
    the same numbers today. Line identity is only satisfied by there being one
    implementation, which is the property the refactor bought.
    """
    bar = make_bar()
    order = make_order(base_quantity=str(ORDER_BASE_QUANTITY))
    backtest_venue = make_venue()
    clock = SteppingClock(EPOCH)
    paper_venue = make_paper_venue(clock=clock)

    def exercise_backtest() -> None:
        backtest_venue.observe(bar)
        ack = backtest_venue.submit(order, decided_at_utc=bar.close_time_utc)
        backtest_venue.resolve_ack(ack)  # type: ignore[arg-type]  # a clean order always acks

    def exercise_paper() -> None:
        paper_venue.observe(bar)
        clock.advance(bar.close_time_utc - EPOCH)
        paper_venue.submit(order, decided_at_utc=bar.close_time_utc)
        clock.advance(paper_venue.schedule_for(bar.close_time_utc).earliest_fill_at_utc - EPOCH)
        paper_venue.due_events()

    backtest_lines = _executed_lines(exercise_backtest)
    paper_lines = _executed_lines(exercise_paper)

    assert backtest_lines, "the measurement saw nothing; the tool id or the filter is wrong"
    assert backtest_lines == paper_lines


def test_paper_and_backtest_print_the_same_fill() -> None:
    """The same bar and the same order produce the same print, down to the fee."""
    bar = make_bar()
    order = make_order(base_quantity=str(ORDER_BASE_QUANTITY))
    backtest_venue = make_venue()
    clock = SteppingClock(bar.close_time_utc)
    paper_venue = make_paper_venue(clock=clock)

    backtest_venue.observe(bar)
    backtest_ack = backtest_venue.submit(order, decided_at_utc=bar.close_time_utc)
    backtest_venue.resolve_ack(backtest_ack)  # type: ignore[arg-type]  # a clean order acks

    paper_venue.observe(bar)
    paper_venue.submit(order, decided_at_utc=bar.close_time_utc)
    clock.advance(
        paper_venue.schedule_for(bar.close_time_utc).earliest_fill_at_utc - bar.close_time_utc
    )
    paper_venue.due_events()

    assert [record.fill for record in backtest_venue.report.fills] == [
        record.fill for record in paper_venue.report.fills
    ]
