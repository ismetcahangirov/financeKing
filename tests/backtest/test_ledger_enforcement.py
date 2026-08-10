"""`EventLoop.run()` as the trial ledger's enforcement point, against real Postgres.

`tests/evolution/test_trial_ledger.py` proves the ledger's arithmetic: that a declared
grid abandoned early still charges its full 200, that an overrun grid charges every
execution, that the running total is computed under an advisory lock. Those are statements
about `TrialLedger`. This file is the statement about the *engine*, and the two are not the
same claim: the ledger being able to charge correctly is worth nothing if a run can reach a
number without going through it.

So every assertion here goes through `EventLoop.run()` with a reporter wired to the real
table. What is under test is that there is no path to a result outside the ledger's view --

- a `spec_hash` with no charge does not execute, and refuses before the event stream is
  read at all, which is why the initial events are a generator that records being touched;
- twelve runs against a declared 200-point grid still cost 200, because stopping early is
  the selection event;
- sixty runs against a declared five-point grid cost sixty, because extending a grid is
  more selection;
- a run that crashes is charged exactly as a completed one is, and carries the traceback,
  because a ledger that only records successful runs lets a search keep the paths it liked
  and report the count of what it kept.

A recording list could show none of this. A list can be truncated; `trial_ledger` refuses
`UPDATE` and `DELETE` at the database, so the rows these tests leave are rows nothing in
this process can take back.

`EventLoop.run()` is synchronous and the driver is asyncpg, so each run happens in a worker
thread and `LedgerReporter` hands its insert back to the test's event loop, blocking until
it commits. The blocking is the point rather than an artefact: `run()` must not return
before the ledger has counted the execution, or a caller could read a result the ledger
does not yet know about.
"""

from __future__ import annotations

import asyncio
import uuid
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Final

import pytest
import pytest_asyncio
import sqlalchemy as sa
from sqlalchemy.exc import DBAPIError
from sqlalchemy.ext.asyncio import AsyncEngine, create_async_engine

from fking.backtest import (
    Event,
    EventLoop,
    ExecutionOutcome,
    ExecutionReport,
    RunConfig,
    RunContext,
    RunTrace,
    SpecRegistration,
    TimerEvent,
    UnregisteredSpecificationError,
)
from fking.evolution import SearchContext, TrialLedger, TrialLedgerError, TrialSpecification

pytestmark = [pytest.mark.integration, pytest.mark.slow]

# A frozen instant. Every charge has to be reproducible from the row alone, and a clock
# read would make the assertions a function of when the suite ran.
CHARGED_AT: Final[datetime] = datetime(2026, 8, 10, 9, 0, tzinfo=UTC)

CONTEXT: Final[SearchContext] = SearchContext(
    symbol_universe=("BTCUSDT",),
    window_start_utc=datetime(2024, 1, 1, tzinfo=UTC),
    window_end_utc=datetime(2026, 1, 1, tzinfo=UTC),
    feature_ids=frozenset({"momentum.4h"}),
)

# A second search against different data. Trials there do not contaminate the count for
# CONTEXT, and the global figure is the sum of both -- which is what "global" has to mean
# for `SR*` to be computed against what the project searched rather than what one study did.
OTHER_CONTEXT: Final[SearchContext] = SearchContext(
    symbol_universe=("ETHUSDT",),
    window_start_utc=datetime(2020, 1, 1, tzinfo=UTC),
    window_end_utc=datetime(2022, 1, 1, tzinfo=UTC),
    feature_ids=frozenset({"funding.extremity"}),
)

# The issue's two numbers, named so that a failure says which quantity moved.
DECLARED_TWO_HUNDRED: Final[int] = 200
ABANDONED_AFTER: Final[int] = 12
DECLARED_FIVE: Final[int] = 5
EXECUTED_SIXTY: Final[int] = 60
DECLARED_ONE: Final[int] = 1

#: The event the crashing handler dies on, and the count the failed report must carry.
CRASH_ON_EVENT: Final[int] = 3
#: Two writers, which is the smallest number that can lose an update.
CONCURRENT_WRITERS: Final[int] = 2

PATH_LABEL: Final[str] = "cpcv-path-01"

#: PostgreSQL's SQLSTATE for `restrict_violation`, which `fking_append_only_guard()`
#: raises. Asserted as a code rather than only as a message, because the message is ours
#: to reword and the code is what a caller can branch on.
RESTRICT_VIOLATION: Final[str] = "23001"

_GLOBAL_TRIALS = sa.text("SELECT n FROM global_trial_count")


def _grid(point_total: int) -> dict[str, tuple[str, ...]]:
    """A one-parameter grid with exactly `point_total` candidates.

    One parameter rather than two factors, because the declared grid multiplies by the
    symbol universe and by the variant count, and a test whose expected charge depends on
    three numbers stops saying which of them the engine got wrong.
    """
    return {"fast_window_bars": tuple(str(candidate) for candidate in range(point_total))}


def _specification(
    *, point_total: int, context: SearchContext = CONTEXT, lineage_id: str = "lin-enforce-0001"
) -> TrialSpecification:
    return TrialSpecification(
        correlation_id=uuid.uuid4(),
        statement=f"a declared grid of {point_total} points",
        registered_by="tests.backtest.test_ledger_enforcement",
        parameter_grid=_grid(point_total),
        search_context=context,
        lineage_id=lineage_id,
    )


def _run_config(*, execution_index: int = 0) -> RunConfig:
    """One configuration per execution, so each run carries its own `config_hash`.

    Distinct configurations rather than the same one sixty times: an overrun grid is sixty
    *different* configurations, and a ledger holding sixty identical `config_hash` values
    could not tell a real overrun from a retry loop.
    """
    return RunConfig(
        strategy_id="strat-enforcement-0001",
        strategy_version="2026.08.0",
        symbols=CONTEXT.symbol_universe,
        parameters={"fast_window_bars": Decimal(execution_index)},
        start_utc=CONTEXT.window_start_utc,
        end_utc=CONTEXT.window_end_utc,
        run_seed=20260810,
    )


class _NullHandler:
    """Never called: the runs that use it start with an empty queue."""

    def on_event(self, event: Event, context: RunContext) -> None:  # pragma: no cover
        raise AssertionError(
            f"no event was scheduled, but {type(event).__name__} arrived at "
            f"{context.now_utc().isoformat()}"
        )


class ArchiveUnavailableError(Exception):
    """Stands in for the ordinary way a run dies: the data it needed was not there."""


class _CrashingHandler:
    """Raises on the `crash_on`-th event, after the loop has done real work."""

    def __init__(self, *, crash_on: int) -> None:
        self._crash_on = crash_on
        self.events_seen = 0

    def on_event(self, event: Event, context: RunContext) -> None:
        self.events_seen += 1
        if self.events_seen >= self._crash_on:
            raise ArchiveUnavailableError(
                f"the archive could not serve {event.occurs_at_utc.isoformat()}, "
                f"reached at simulated {context.now_utc().isoformat()}"
            )


class LedgerReporter:
    """The loop's synchronous report, landed in the real table before `run()` returns.

    Blocking on `run_coroutine_threadsafe(...).result()` is what makes "before it returns"
    true rather than merely scheduled. A reporter that fired and did not await would still
    leave the right number of rows at the end of a suite and would prove nothing about the
    ordering the crash test depends on.
    """

    def __init__(
        self, ledger: TrialLedger, event_loop: asyncio.AbstractEventLoop, correlation_id: uuid.UUID
    ) -> None:
        self._ledger = ledger
        self._event_loop = event_loop
        self._correlation_id = correlation_id
        self.reports: list[ExecutionReport] = []

    def report_execution(self, execution: ExecutionReport) -> None:
        self.reports.append(execution)
        future = asyncio.run_coroutine_threadsafe(
            self._ledger.report_execution(
                execution,
                correlation_id=self._correlation_id,
                executed_at_utc=CHARGED_AT,
            ),
            self._event_loop,
        )
        future.result()


@pytest_asyncio.fixture
async def ledger(app_engine: AsyncEngine) -> TrialLedger:
    """The ledger as `fking_app`: INSERT and SELECT, and nothing else, ever."""
    return TrialLedger(app_engine)


async def _global_trials(engine: AsyncEngine) -> int:
    async with engine.connect() as connection:
        return int(await connection.scalar(_GLOBAL_TRIALS) or 0)


async def _run_in_thread(
    registration: SpecRegistration,
    reporter: LedgerReporter,
    *,
    execution_index: int,
    handler: _NullHandler | _CrashingHandler | None = None,
    initial_events: tuple[Event, ...] = (),
) -> RunTrace:
    """One `EventLoop.run()` off the event loop, so the reporter can block on it."""
    loop = EventLoop(
        _run_config(execution_index=execution_index),
        handler if handler is not None else _NullHandler(),
        registration=registration,
        reporter=reporter,
        path_label=f"cpcv-path-{execution_index:02d}",
    )
    return await asyncio.to_thread(loop.run, initial_events)


# ---------------------------------------------------------------------------
# An unregistered specification does not execute
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_an_unregistered_spec_hash_is_refused_before_any_data_is_read(
    ledger: TrialLedger, app_engine: AsyncEngine
) -> None:
    """The refusal comes before the event stream is touched, not after the first bar.

    Asserted with a generator rather than a list, because the distinction is the whole
    property: reading one bar and then refusing is a run that has already opened the
    archive, and on a feed backed by a paid API it is a run that has already spent money.
    Refusing first is also what makes the refusal safe to retry.
    """
    touched = False

    def events() -> Iterator[Event]:
        nonlocal touched
        touched = True
        yield TimerEvent(
            strategy_id="strat-enforcement-0001",
            occurs_at_utc=CONTEXT.window_start_utc,
            label="wake",
        )

    unregistered = await ledger.registration_for("cd" * 32)
    assert unregistered.trials_charged == 0

    reporter = LedgerReporter(ledger, asyncio.get_running_loop(), uuid.uuid4())
    loop = EventLoop(
        _run_config(),
        _NullHandler(),
        registration=unregistered,
        reporter=reporter,
        path_label=PATH_LABEL,
    )

    with pytest.raises(UnregisteredSpecificationError, match="no trial-ledger charge"):
        loop.run(events())

    assert touched is False
    # Nothing executed, so nothing is charged: charging a refusal would price registering
    # a specification at the cost of not registering one.
    assert reporter.reports == []
    assert await _global_trials(app_engine) == 0


@pytest.mark.asyncio
async def test_an_unregistered_execution_report_is_refused_by_the_database_too(
    ledger: TrialLedger,
) -> None:
    """The gate is not only in Python. Bypassing `EventLoop` still hits the trigger.

    Without this the engine's check is a convention: anyone holding a `TrialLedger` could
    report an execution for a specification nobody declared, and the row would carry a
    charge attributed to a search that does not exist.
    """
    report = ExecutionReport(
        spec_hash="ab" * 32,
        config_hash="cd" * 32,
        path_label=PATH_LABEL,
        outcome=ExecutionOutcome.COMPLETED,
        failure_detail=None,
        dispatched_event_count=0,
    )
    with pytest.raises(TrialLedgerError, match="never registered"):
        await ledger.report_execution(
            report, correlation_id=uuid.uuid4(), executed_at_utc=CHARGED_AT
        )


# ---------------------------------------------------------------------------
# max(declared, executed), driven through the engine
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_a_declared_grid_abandoned_after_twelve_runs_still_charges_two_hundred(
    ledger: TrialLedger, app_engine: AsyncEngine
) -> None:
    """Optional stopping is priced at the declaration, not at the twelfth run.

    The 188 configurations never executed are the alternatives that would have been
    accepted had the first twelve looked worse, so they were searched in every sense that
    matters to a deflation.
    """
    receipt = await ledger.register(
        _specification(point_total=DECLARED_TWO_HUNDRED), charged_at_utc=CHARGED_AT
    )
    registration = await ledger.registration_for(receipt.spec_hash)
    assert registration.trials_charged == DECLARED_TWO_HUNDRED

    reporter = LedgerReporter(ledger, asyncio.get_running_loop(), uuid.uuid4())
    for execution_index in range(ABANDONED_AFTER):
        await _run_in_thread(registration, reporter, execution_index=execution_index)

    assert len(reporter.reports) == ABANDONED_AFTER
    assert await _global_trials(app_engine) == DECLARED_TWO_HUNDRED
    assert await ledger.context_trial_count(CONTEXT.context_hash) == DECLARED_TWO_HUNDRED


@pytest.mark.asyncio
async def test_a_declared_grid_of_five_that_executes_sixty_runs_charges_sixty(
    ledger: TrialLedger, app_engine: AsyncEngine
) -> None:
    """Undeclared search is priced at the run. Extending a grid is more selection.

    The charge is 60 rather than 65: the first five executions were paid for by the
    declaration, and summing the two would double-charge the honest case of declaring 200
    and running 200 -- a defence that punishes correct behaviour gets routed around.
    """
    receipt = await ledger.register(
        _specification(point_total=DECLARED_FIVE), charged_at_utc=CHARGED_AT
    )
    registration = await ledger.registration_for(receipt.spec_hash)
    assert registration.trials_charged == DECLARED_FIVE

    reporter = LedgerReporter(ledger, asyncio.get_running_loop(), uuid.uuid4())
    for execution_index in range(EXECUTED_SIXTY):
        await _run_in_thread(registration, reporter, execution_index=execution_index)

    assert len(reporter.reports) == EXECUTED_SIXTY
    assert await _global_trials(app_engine) == EXECUTED_SIXTY
    assert await ledger.context_trial_count(CONTEXT.context_hash) == EXECUTED_SIXTY


# ---------------------------------------------------------------------------
# The charge survives failure
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_a_run_that_raises_leaves_a_charged_row_carrying_the_traceback(
    ledger: TrialLedger, app_engine: AsyncEngine
) -> None:
    """A crash mid-run is a consumed trial, and the charged row says what killed it.

    Declared one point and executed two, so the crashing run is the surplus and therefore
    the one that writes a `trial_ledger` charge. Without the charge, a search could run 28
    CPCV paths, keep the six that looked good, and report six -- the ledger would become
    the instrument for laundering the other 22.
    """
    receipt = await ledger.register(
        _specification(point_total=DECLARED_ONE), charged_at_utc=CHARGED_AT
    )
    registration = await ledger.registration_for(receipt.spec_hash)
    reporter = LedgerReporter(ledger, asyncio.get_running_loop(), uuid.uuid4())

    await _run_in_thread(registration, reporter, execution_index=0)

    bars = tuple(
        TimerEvent(
            strategy_id="strat-enforcement-0001",
            occurs_at_utc=CONTEXT.window_start_utc + timedelta(hours=index),
            label=f"wake-{index}",
        )
        for index in range(4)
    )
    with pytest.raises(ArchiveUnavailableError, match="could not serve"):
        await _run_in_thread(
            registration,
            reporter,
            execution_index=1,
            handler=_CrashingHandler(crash_on=CRASH_ON_EVENT),
            initial_events=bars,
        )

    failed = reporter.reports[-1]
    assert failed.outcome is ExecutionOutcome.FAILED
    assert failed.failure_detail is not None
    assert "ArchiveUnavailableError" in failed.failure_detail
    # How far it got, which is the difference between a configuration that cannot start
    # and one that dies at hour nine of an eighteen-month replay.
    assert failed.dispatched_event_count == CRASH_ON_EVENT

    async with app_engine.connect() as connection:
        stored = (
            await connection.execute(
                sa.text(
                    """
                    SELECT outcome, failure_detail, dispatched_event_count, charged
                      FROM trial_execution
                     WHERE spec_hash = decode(:spec_hash, 'hex')
                     ORDER BY execution_index
                    """
                ),
                {"spec_hash": receipt.spec_hash},
            )
        ).all()
        overflow = (
            await connection.execute(
                sa.text(
                    """
                    SELECT parameter_grid
                      FROM trial_ledger
                     WHERE spec_hash = decode(:spec_hash, 'hex')
                       AND entry_kind = 'execution_overflow'
                    """
                ),
                {"spec_hash": receipt.spec_hash},
            )
        ).all()

    assert [row.outcome for row in stored] == ["completed", "failed"]
    assert stored[0].failure_detail is None
    assert "ArchiveUnavailableError" in str(stored[1].failure_detail)
    assert [row.charged for row in stored] == [False, True]

    # The charged ledger row itself explains what it paid for. Reading `trial_execution`
    # to find out is a second query nobody runs during an incident.
    assert len(overflow) == 1
    assert overflow[0].parameter_grid["outcome"] == "failed"
    assert "ArchiveUnavailableError" in str(overflow[0].parameter_grid["failure_detail"])

    # Two executions against a one-point grid: 1 declared + 1 surplus.
    assert await _global_trials(app_engine) == DECLARED_ONE + 1


# ---------------------------------------------------------------------------
# The counter cannot be refunded, reset, or lost to a concurrent writer
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "statement",
    [
        "UPDATE trial_ledger SET trials_charged = 1",
        "DELETE FROM trial_ledger",
    ],
)
@pytest.mark.asyncio
async def test_the_charge_cannot_be_rewritten_or_removed(
    ledger: TrialLedger, engine: AsyncEngine, statement: str
) -> None:
    """`restrict_violation` from the trigger, exercised as the owner rather than the app.

    As `fking_app` this would fail on the missing grant and never reach the trigger, which
    proves the grant rather than the backstop. The backstop is the one that matters after
    a later migration hands a broad role to a new service, or when somebody opens `psql`
    during an incident to "fix" a number for a dashboard.

    Asserted here as well as in the ledger suite because it is what makes every other
    assertion in this file worth making: a charge that can be edited afterwards turns the
    enforcement point into a formality.
    """
    await ledger.register(_specification(point_total=DECLARED_FIVE), charged_at_utc=CHARGED_AT)

    with pytest.raises(DBAPIError, match="append-only") as refusal:
        async with engine.begin() as connection:
            await connection.execute(sa.text(statement))

    # The SQLSTATE, not only the message: the message is ours to change and the code is
    # what a caller can branch on.
    assert getattr(refusal.value.orig, "sqlstate", None) == RESTRICT_VIOLATION


@pytest.mark.asyncio
async def test_two_concurrent_writers_never_lower_the_cumulative_total(
    ledger: TrialLedger, migrated_dsn: str
) -> None:
    """Two registrations racing produce two distinct totals, and the higher is the sum.

    The failure this excludes is the read-then-write one: both writers read the same
    predecessor, both write `predecessor + charge`, and the ledger ends holding one
    charge's worth of two searches. That understates the selection pool in the flattering
    direction, which is the direction nobody investigates.
    """
    second = create_async_engine(migrated_dsn)
    try:
        writer = TrialLedger(second)
        first_receipt, second_receipt = await asyncio.gather(
            ledger.register(
                _specification(point_total=DECLARED_FIVE, lineage_id="lin-a"),
                charged_at_utc=CHARGED_AT,
            ),
            writer.register(
                _specification(point_total=DECLARED_TWO_HUNDRED, lineage_id="lin-b"),
                charged_at_utc=CHARGED_AT,
            ),
        )

        totals = {first_receipt.cumulative_trials, second_receipt.cumulative_trials}
        # Two distinct totals: one writer saw the other's charge, which a lost update
        # would collapse into a single number appearing twice.
        assert len(totals) == CONCURRENT_WRITERS
        assert max(totals) == DECLARED_FIVE + DECLARED_TWO_HUNDRED
        assert await ledger.global_trial_count() == DECLARED_FIVE + DECLARED_TWO_HUNDRED
    finally:
        await second.dispose()


# ---------------------------------------------------------------------------
# The count that gates a run is the global one
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_the_global_count_query_targets_the_global_view(
    ledger: TrialLedger, app_engine: AsyncEngine
) -> None:
    """The statement that reaches the server names `global_trial_count` and nothing else.

    Captured off the connection rather than read off the source, so a refactor that keeps
    the method name and changes the query fails here. The failure mode this excludes is a
    silent substitution: `SELECT sum(trials_charged) WHERE study_id = ...` returns a
    plausible number, deflates every Sharpe against it, and looks identical from the
    outside. The difference is 200 against 50,000, and `SR*` grows as `sqrt(2 ln K)` --
    slowly enough that the wrong number never looks wrong.
    """
    executed: list[str] = []

    @sa.event.listens_for(app_engine.sync_engine, "before_cursor_execute")
    def _capture(_conn: object, _cursor: object, statement: str, *_rest: object) -> None:
        executed.append(statement)

    try:
        await ledger.global_trial_count()
    finally:
        sa.event.remove(app_engine.sync_engine, "before_cursor_execute", _capture)

    assert executed == ["SELECT n FROM global_trial_count"]


@pytest.mark.asyncio
async def test_the_global_count_spans_searches_rather_than_one_study(
    ledger: TrialLedger, app_engine: AsyncEngine
) -> None:
    """Two searches against different data, one global figure covering both.

    A counter keyed per study is the common real-world failure and it gives `K` around 200
    where the true figure is nearer 50,000. The per-context counts stay separate -- trials
    against different data do not contaminate each other -- but the global figure does not
    reset, ever, and that is what an operator reads to know what the project has spent.
    """
    await ledger.register(_specification(point_total=DECLARED_FIVE), charged_at_utc=CHARGED_AT)
    await ledger.register(
        _specification(point_total=DECLARED_TWO_HUNDRED, context=OTHER_CONTEXT),
        charged_at_utc=CHARGED_AT,
    )

    assert await ledger.context_trial_count(CONTEXT.context_hash) == DECLARED_FIVE
    assert await ledger.context_trial_count(OTHER_CONTEXT.context_hash) == DECLARED_TWO_HUNDRED
    assert await ledger.global_trial_count() == DECLARED_FIVE + DECLARED_TWO_HUNDRED
    assert await _global_trials(app_engine) == DECLARED_FIVE + DECLARED_TWO_HUNDRED
