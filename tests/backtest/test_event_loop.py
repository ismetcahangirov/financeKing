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
    FAILURE_DETAIL_LIMIT_BYTES,
    CausalityError,
    Event,
    EventBudgetExhaustedError,
    EventLoop,
    ExecutionOutcome,
    ExecutionReport,
    ExecutionReportError,
    MarketDataEvent,
    RunConfigError,
    RunContext,
    SimulationClock,
    TimerEvent,
    UnregisteredSpecificationError,
    failure_detail_for,
)
from tests.backtest.registration_support import (
    PATH_LABEL,
    REGISTERED,
    UNREGISTERED,
    RecordingReporter,
)
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
        EventLoop(
            config_for(start_utc=START),
            handler,
            registration=REGISTERED,
            reporter=RecordingReporter(),
            path_label=PATH_LABEL,
        ).run(bar_events(START, how_many=2))


def test_an_initial_event_before_the_window_opens_is_refused() -> None:
    """The clock starts at `start_utc`, so data from before it is the caller's mistake.

    Refused rather than dropped: the caller asked for a window and handed the loop data
    from outside it, and narrowing the window is a decision only the caller can make.
    """
    too_early = MarketDataEvent(observation=bar_at(START - BAR_INTERVAL * 5))
    with pytest.raises(CausalityError, match="before the instant being dispatched"):
        EventLoop(
            config_for(start_utc=START),
            RecordingHandler(),
            registration=REGISTERED,
            reporter=RecordingReporter(),
            path_label=PATH_LABEL,
        ).run([too_early])


def test_scheduling_at_the_current_instant_is_ordinary_and_allowed() -> None:
    """A bar, the fill it caused and the timer it woke legitimately share one timestamp."""
    handler = SchedulingHandler(follow_ups=(fill_event_at(START + BAR_INTERVAL, ordinal=1),))
    trace = EventLoop(
        config_for(start_utc=START),
        handler,
        registration=REGISTERED,
        reporter=RecordingReporter(),
        path_label=PATH_LABEL,
    ).run(bar_events(START, how_many=1))

    assert handler.type_names == ("MarketDataEvent", "FillEvent")
    assert {entry.occurs_at_utc for entry in trace.entries} == {START + BAR_INTERVAL}


def test_events_past_the_window_end_are_dropped_and_counted() -> None:
    """A fill acknowledged after the final bar is ordinary; losing it silently is not."""
    config = config_for(start_utc=START, window=timedelta(minutes=3))
    beyond = TimerEvent(
        strategy_id="s", occurs_at_utc=START + timedelta(minutes=90), label="too-late"
    )
    handler = SchedulingHandler(follow_ups=(beyond,))

    trace = EventLoop(
        config,
        handler,
        registration=REGISTERED,
        reporter=RecordingReporter(),
        path_label=PATH_LABEL,
    ).run(bar_events(START, how_many=2))

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
        EventLoop(
            config,
            _SpinningHandler(),
            registration=REGISTERED,
            reporter=RecordingReporter(),
            path_label=PATH_LABEL,
        ).run(bar_events(START, how_many=1))


def test_the_clock_reaches_each_event_and_never_moves_backwards() -> None:
    handler = _ClockReader()
    EventLoop(
        config_for(start_utc=START),
        handler,
        registration=REGISTERED,
        reporter=RecordingReporter(),
        path_label=PATH_LABEL,
    ).run(bar_events(START, how_many=4))

    assert handler.observed == sorted(handler.observed)
    assert handler.observed[0] == START + BAR_INTERVAL
    assert handler.observed[-1] == START + BAR_INTERVAL * 4


def test_a_run_with_no_events_produces_an_empty_but_identified_trace() -> None:
    """An empty window is a real answer, and it still carries the identity that produced it."""
    trace = EventLoop(
        config_for(start_utc=START),
        RecordingHandler(),
        registration=REGISTERED,
        reporter=RecordingReporter(),
        path_label=PATH_LABEL,
    ).run([])

    assert trace.event_count == 0
    assert trace.entries == ()
    assert trace.events_beyond_window == 0
    assert trace.config_hash


# ---------------------------------------------------------------------------
# What the loop tells the trial ledger
# ---------------------------------------------------------------------------


class _FailingHandler:
    """Raises on the first event it is given."""

    def on_event(self, event: Event, context: RunContext) -> None:  # noqa: ARG002 - the Protocol's shape
        raise _ArchiveUnavailableError("the archive could not serve the training window")


class _ArchiveUnavailableError(Exception):
    """The ordinary way a run dies: the data it needed was not there."""


def test_a_completed_run_reports_exactly_one_execution() -> None:
    """One report per run, not one per event and not one per fold.

    A run is the unit the ledger charges, so a loop that reported per event would charge a
    single configuration once per bar and deflate every Sharpe in the project to zero.
    """
    reporter = RecordingReporter()
    trace = EventLoop(
        config_for(start_utc=START),
        RecordingHandler(),
        registration=REGISTERED,
        reporter=reporter,
        path_label=PATH_LABEL,
    ).run(bar_events(START, how_many=4))

    assert [report.outcome for report in reporter.reports] == [ExecutionOutcome.COMPLETED]
    report = reporter.reports[0]
    assert report.spec_hash == REGISTERED.spec_hash
    assert report.config_hash == trace.config_hash
    assert report.path_label == PATH_LABEL
    assert report.failure_detail is None
    assert report.dispatched_event_count == trace.event_count


def test_a_run_that_raises_is_still_reported_and_carries_its_traceback() -> None:
    """The charge survives the failure, and the exception continues on its way.

    Reported *and* re-raised. Swallowing it would convert a visible failure into a silent
    one; not reporting it would let a search run 28 paths, keep the six that looked good,
    and report six.
    """
    reporter = RecordingReporter()
    with pytest.raises(_ArchiveUnavailableError, match="could not serve"):
        EventLoop(
            config_for(start_utc=START),
            _FailingHandler(),
            registration=REGISTERED,
            reporter=reporter,
            path_label=PATH_LABEL,
        ).run(bar_events(START, how_many=4))

    assert [report.outcome for report in reporter.reports] == [ExecutionOutcome.FAILED]
    report = reporter.reports[0]
    assert report.failure_detail is not None
    assert "_ArchiveUnavailableError" in report.failure_detail
    # The first event was dispatched before the handler raised, so the run is recorded as
    # having got that far rather than as never having started.
    assert report.dispatched_event_count == 1


def test_an_unregistered_run_reports_nothing_at_all() -> None:
    """A refusal is not an execution. Charging it would price registering at the cost of not."""
    reporter = RecordingReporter()
    with pytest.raises(UnregisteredSpecificationError, match="no trial-ledger charge"):
        EventLoop(
            config_for(start_utc=START),
            RecordingHandler(),
            registration=UNREGISTERED,
            reporter=reporter,
            path_label=PATH_LABEL,
        ).run(bar_events(START, how_many=4))

    assert reporter.reports == []


def test_an_oversized_traceback_keeps_its_tail() -> None:
    """Truncation drops the head, because the exception and its innermost frames are last.

    An oversized rendering is bounded because `trial_execution` is append-only and one
    pathological row would be permanent; bounding it from the wrong end would keep the
    outermost frames -- the ones every run shares -- and lose the message that says which
    run this was.

    The overflow is produced with a long message rather than deep recursion, because
    Python collapses repeated frames into "[Previous line repeated N more times]" and a
    recursion test would silently stop exercising the truncation.
    """

    def _fail_at_length() -> None:
        padding = "the archive could not serve the training window; " * 500
        raise _ArchiveUnavailableError(f"{padding}final segment")

    try:
        _fail_at_length()
    except _ArchiveUnavailableError as failure:
        detail = failure_detail_for(failure)

    assert len(detail.encode("utf-8")) <= FAILURE_DETAIL_LIMIT_BYTES
    assert detail.startswith("[traceback truncated")
    assert detail.rstrip().endswith("final segment")


@pytest.mark.parametrize(
    ("outcome", "failure_detail"),
    [
        (ExecutionOutcome.COMPLETED, "a traceback nobody asked for"),
        (ExecutionOutcome.FAILED, None),
    ],
)
def test_a_report_whose_outcome_and_detail_disagree_is_refused(
    outcome: ExecutionOutcome, failure_detail: str | None
) -> None:
    """Refused at construction, so the mismatch names the field rather than the constraint."""
    with pytest.raises(ExecutionReportError, match="must agree"):
        ExecutionReport(
            spec_hash=REGISTERED.spec_hash,
            config_hash="cd" * 32,
            path_label=PATH_LABEL,
            outcome=outcome,
            failure_detail=failure_detail,
            dispatched_event_count=0,
        )


def test_a_report_of_a_negative_event_count_is_refused() -> None:
    with pytest.raises(ExecutionReportError, match="must not be negative"):
        ExecutionReport(
            spec_hash=REGISTERED.spec_hash,
            config_hash="cd" * 32,
            path_label=PATH_LABEL,
            outcome=ExecutionOutcome.COMPLETED,
            failure_detail=None,
            dispatched_event_count=-1,
        )


def test_a_report_carrying_a_traceback_above_the_ledgers_limit_is_refused() -> None:
    """The Python-side guard on the same bound the database enforces.

    Both, rather than either: the database is the one that cannot be bypassed, and this
    one is the one that fails while the run is still in hand to be repeated.
    """
    with pytest.raises(ExecutionReportError, match="above the"):
        ExecutionReport(
            spec_hash=REGISTERED.spec_hash,
            config_hash="cd" * 32,
            path_label=PATH_LABEL,
            outcome=ExecutionOutcome.FAILED,
            failure_detail="x" * (FAILURE_DETAIL_LIMIT_BYTES + 1),
            dispatched_event_count=0,
        )


def test_a_run_without_a_path_label_is_refused() -> None:
    """A blank label makes a charged row unable to say which path it paid for."""
    with pytest.raises(RunConfigError, match="path_label"):
        EventLoop(
            config_for(start_utc=START),
            RecordingHandler(),
            registration=REGISTERED,
            reporter=RecordingReporter(),
            path_label="   ",
        )


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
