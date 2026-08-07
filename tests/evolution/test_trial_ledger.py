"""The global trial ledger, against real Postgres, as the application role.

Mocking the database here would prove that the mock is append-only. Everything this file
asserts lives in the schema rather than in Python: the revoked grants, the
`BEFORE UPDATE OR DELETE` trigger, the advisory-locked running total, and the
`max(declared, executed)` reconciliation computed inside a `BEFORE INSERT` trigger on
`trial_execution`.

The two charge tests are the ones to read first, because they are the two evasions the
counter exists to close and neither is closed by the other. A declared grid abandoned
after twelve points still charges its full 200 -- the decision to stop early *is* the
selection. A declared grid of five that runs sixty configurations charges sixty --
extending a grid is more selection. Charging `max()` rather than a sum is what keeps the
honest case, declaring 200 and running 200, from paying twice.
"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Final
from uuid import uuid4

import pytest
import pytest_asyncio
import sqlalchemy as sa
from alembic import command
from sqlalchemy.exc import DBAPIError, IntegrityError
from sqlalchemy.ext.asyncio import AsyncEngine, create_async_engine

from fking.backtest import (
    Event,
    EventLoop,
    RunConfig,
    RunContext,
    SpecRegistration,
    UnregisteredSpecificationError,
    deflated_sharpe_ratio,
)
from fking.evolution import (
    ObservedPerformance,
    SearchContext,
    TrialLedger,
    TrialLedgerError,
    TrialSpecification,
    TrialSpecificationError,
)
from tests.conftest import alembic_config

pytestmark = [pytest.mark.integration, pytest.mark.slow]

# A frozen instant. Every charge in this suite has to be reproducible from the row alone,
# and a clock read would make the digest a function of when the test ran.
CHARGED_AT: Final[datetime] = datetime(2026, 8, 7, 11, 15, tzinfo=UTC)

CONTEXT: Final[SearchContext] = SearchContext(
    symbol_universe=("BTCUSDT", "ETHUSDT"),
    window_start_utc=datetime(2024, 1, 1, tzinfo=UTC),
    window_end_utc=datetime(2026, 1, 1, tzinfo=UTC),
    feature_ids=frozenset({"momentum.4h", "funding.extremity"}),
)

# A second context: the same grid against a different window. Trials against different
# data do not contaminate each other, and this is what proves the counter is keyed rather
# than global-in-the-crude-sense.
OTHER_CONTEXT: Final[SearchContext] = SearchContext(
    symbol_universe=("BTCUSDT", "ETHUSDT"),
    window_start_utc=datetime(2020, 1, 1, tzinfo=UTC),
    window_end_utc=datetime(2022, 1, 1, tzinfo=UTC),
    feature_ids=frozenset({"momentum.4h", "funding.extremity"}),
)

# sha256 of a run configuration. Any 32-byte digest does; the column only asserts width.
CONFIG_HASH: Final[str] = "1f" * 32

# The numbers issue #82 states its criteria in, named so that a failure message says which
# quantity moved rather than which literal did.
DECLARED_TWO_HUNDRED: Final[int] = 200
ABANDONED_AFTER: Final[int] = 12
DECLARED_FIVE: Final[int] = 5
DECLARED_EIGHT: Final[int] = 8
DECLARED_TWELVE: Final[int] = 12
DECLARED_TWENTY: Final[int] = 20
DECLARED_FORTY: Final[int] = 40
DECLARED_FOUR_THOUSAND: Final[int] = 4000
EXECUTED_SIXTY: Final[int] = 60
CONCURRENT_REGISTRATIONS: Final[int] = 50
GENERATION_TOTAL: Final[int] = 4

# 10 x 10 across the two-symbol universe is the 200-point grid.
GRID_TWO_HUNDRED: Final[tuple[int, int]] = (10, 10)
# 3 x 2 across two symbols: the honest case, declared and run in full.
GRID_TWELVE: Final[tuple[int, int]] = (3, 2)
# 5 x 2 across two symbols, charged once per generation below.
GRID_TWENTY: Final[tuple[int, int]] = (5, 2)
GRID_FORTY: Final[tuple[int, int]] = (5, 4)
# 50 x 40 across two symbols: the large sweep that moves the deflated Sharpe.
GRID_FOUR_THOUSAND: Final[tuple[int, int]] = (50, 40)


def specification(
    *,
    statement: str = "declared grid",
    lineage_id: str = "lin-momentum-0001",
    grid_shape: tuple[int, int] = GRID_TWO_HUNDRED,
    context: SearchContext = CONTEXT,
    holdout_requested: bool = False,
) -> TrialSpecification:
    """A search whose declared grid is `grid_shape[0] * grid_shape[1] * symbols` points."""
    fast, slow = grid_shape
    return TrialSpecification(
        correlation_id=uuid4(),
        statement=statement,
        registered_by="evolution.optimizer",
        parameter_grid={
            "fast_window_bars": tuple(str(candidate) for candidate in range(fast)),
            "slow_window_bars": tuple(str(candidate) for candidate in range(slow)),
        },
        search_context=context,
        lineage_id=lineage_id,
        holdout_requested=holdout_requested,
        human_authorisation_ref=None,
    )


@pytest_asyncio.fixture
async def ledger(app_engine: AsyncEngine) -> TrialLedger:
    """The ledger as `fking_app` -- INSERT and SELECT, and nothing else, ever."""
    return TrialLedger(app_engine)


async def _execute(ledger_under_test: TrialLedger, spec_hash: str, *, path_label: str) -> bool:
    receipt = await ledger_under_test.report_execution(
        spec_hash=spec_hash,
        config_hash=CONFIG_HASH,
        path_label=path_label,
        correlation_id=uuid4(),
        executed_at_utc=CHARGED_AT,
    )
    return receipt.charged


# ---------------------------------------------------------------------------
# Append-only, enforced by the database
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "rewrite",
    [
        "UPDATE trial_ledger SET trials_charged = 1",
        "DELETE FROM trial_ledger",
        "DELETE FROM trial_execution",
    ],
)
async def test_the_application_role_is_refused_before_the_trigger_is_reached(
    app_engine: AsyncEngine, ledger: TrialLedger, rewrite: str
) -> None:
    """The primary control: `fking_app` holds SELECT and INSERT and nothing else.

    `permission denied` rather than the trigger's message, and that is the layering
    working rather than a gap in it. The grant has to be primary because `TRUNCATE`
    fires no row trigger at all, so a posture that relied on the trigger alone would be
    open to the one statement that empties the table in a single line.
    """
    receipt = await ledger.register(specification(grid_shape=(1, 1)), charged_at_utc=CHARGED_AT)
    await _execute(ledger, receipt.spec_hash, path_label="path-0")

    async with app_engine.begin() as connection:
        with pytest.raises(DBAPIError, match="permission denied"):
            await connection.execute(sa.text(rewrite))


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "rewrite",
    [
        "UPDATE trial_ledger SET trials_charged = 1",
        "DELETE FROM trial_ledger",
        "DELETE FROM trial_execution",
    ],
)
async def test_even_the_owner_cannot_rewrite_a_charge(
    engine: AsyncEngine, ledger: TrialLedger, rewrite: str
) -> None:
    """The backstop, exercised by a role the grant does not stop.

    This is the failure the grant cannot reach: a later migration hands a broad role to
    a new service, or somebody opens `psql` as the owner during an incident to "fix" a
    number for a dashboard. The trigger fires regardless of who holds what, and a
    counter that can be edited downward is not a counter -- a reset is indistinguishable
    from a claim that the previous six months of searching never happened.
    """
    receipt = await ledger.register(specification(grid_shape=(1, 1)), charged_at_utc=CHARGED_AT)
    await _execute(ledger, receipt.spec_hash, path_label="path-0")

    async with engine.begin() as connection:
        with pytest.raises(DBAPIError, match="append-only"):
            await connection.execute(sa.text(rewrite))


@pytest.mark.asyncio
async def test_a_specification_cannot_be_registered_twice(ledger: TrialLedger) -> None:
    """One charge per specification, and the ledger is monotone, so a second is permanent."""
    declared = specification()
    await ledger.register(declared, charged_at_utc=CHARGED_AT)

    with pytest.raises(IntegrityError):
        await ledger.register(declared, charged_at_utc=CHARGED_AT)


# ---------------------------------------------------------------------------
# max(declared, executed)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_a_declared_grid_abandoned_early_still_charges_the_whole_grid(
    ledger: TrialLedger,
) -> None:
    """Declare 200, stop at 12 because the 12th looked good: the charge is 200.

    The 188 unrun points were the alternatives that would have been accepted had the
    first twelve looked worse, so stopping is the selection event and charging at
    execution prices it at zero.
    """
    declared = specification(grid_shape=GRID_TWO_HUNDRED)  # 10 * 10 * 2 symbols
    assert declared.declared_grid_point_count == DECLARED_TWO_HUNDRED

    receipt = await ledger.register(declared, charged_at_utc=CHARGED_AT)
    for index in range(ABANDONED_AFTER):
        assert await _execute(ledger, receipt.spec_hash, path_label=f"path-{index}") is False

    assert await ledger.context_trial_count(CONTEXT.context_hash) == DECLARED_TWO_HUNDRED


@pytest.mark.asyncio
async def test_a_declared_grid_that_is_overrun_charges_every_execution(
    ledger: TrialLedger,
) -> None:
    """Declare 5, run 60: the charge is 60. Extending a grid is more selection."""
    # One symbol rather than two, so the declared grid is exactly the five points the
    # criterion names rather than ten.
    declared = TrialSpecification(
        correlation_id=uuid4(),
        statement="declared five",
        registered_by="evolution.optimizer",
        parameter_grid={"fast_window_bars": ("2", "4", "8", "16", "32")},
        search_context=SearchContext(
            symbol_universe=("BTCUSDT",),
            window_start_utc=CONTEXT.window_start_utc,
            window_end_utc=CONTEXT.window_end_utc,
            feature_ids=CONTEXT.feature_ids,
        ),
        lineage_id="lin-overrun-0001",
    )
    assert declared.declared_grid_point_count == DECLARED_FIVE

    receipt = await ledger.register(declared, charged_at_utc=CHARGED_AT)
    charged_executions = [
        await _execute(ledger, receipt.spec_hash, path_label=f"path-{index}")
        for index in range(EXECUTED_SIXTY)
    ]

    # The first five were paid for in advance; the surplus 55 each charge one.
    assert charged_executions[:DECLARED_FIVE] == [False] * DECLARED_FIVE
    assert charged_executions[DECLARED_FIVE:] == [True] * (EXECUTED_SIXTY - DECLARED_FIVE)
    assert await ledger.context_trial_count(receipt.search_context_hash) == EXECUTED_SIXTY


@pytest.mark.asyncio
async def test_declaring_and_running_the_same_number_is_charged_once(
    ledger: TrialLedger,
) -> None:
    """The honest case pays once. Summing declaration and execution would double it."""
    declared = specification(grid_shape=GRID_TWELVE)  # 3 * 2 * 2 symbols
    assert declared.declared_grid_point_count == DECLARED_TWELVE

    receipt = await ledger.register(declared, charged_at_utc=CHARGED_AT)
    for index in range(DECLARED_TWELVE):
        await _execute(ledger, receipt.spec_hash, path_label=f"path-{index}")

    assert await ledger.context_trial_count(CONTEXT.context_hash) == DECLARED_TWELVE


@pytest.mark.asyncio
async def test_an_execution_against_an_unregistered_specification_is_refused(
    ledger: TrialLedger,
) -> None:
    """Running sixty configurations without registering anything is an undeclared search."""
    with pytest.raises(TrialLedgerError, match="never registered"):
        await _execute(ledger, "ab" * 32, path_label="path-0")

    assert await ledger.context_trial_count(CONTEXT.context_hash) == 0


# ---------------------------------------------------------------------------
# Keyed on the search context; two counts within it
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_a_different_search_context_keeps_its_own_count(ledger: TrialLedger) -> None:
    """Trials against different data do not contaminate each other."""
    await ledger.register(specification(grid_shape=GRID_TWO_HUNDRED), charged_at_utc=CHARGED_AT)
    await ledger.register(
        specification(grid_shape=(2, 2), context=OTHER_CONTEXT, statement="other window"),
        charged_at_utc=CHARGED_AT,
    )

    assert await ledger.context_trial_count(CONTEXT.context_hash) == DECLARED_TWO_HUNDRED
    assert await ledger.context_trial_count(OTHER_CONTEXT.context_hash) == DECLARED_EIGHT


@pytest.mark.asyncio
async def test_the_context_count_is_the_sum_across_lineages_not_one_lineage(
    ledger: TrialLedger,
) -> None:
    """A system that deflates only by lineage treats each family as the only search run."""
    await ledger.register(
        specification(grid_shape=GRID_TWO_HUNDRED, lineage_id="lin-a", statement="family a"),
        charged_at_utc=CHARGED_AT,
    )
    await ledger.register(
        specification(grid_shape=GRID_FORTY, lineage_id="lin-b", statement="family b"),
        charged_at_utc=CHARGED_AT,
    )

    assert await ledger.lineage_trial_count(CONTEXT.context_hash, "lin-a") == DECLARED_TWO_HUNDRED
    assert await ledger.lineage_trial_count(CONTEXT.context_hash, "lin-b") == DECLARED_FORTY
    assert (
        await ledger.context_trial_count(CONTEXT.context_hash)
        == DECLARED_TWO_HUNDRED + DECLARED_FORTY
    )


@pytest.mark.asyncio
async def test_a_generation_boundary_does_not_reset_the_count(ledger: TrialLedger) -> None:
    """The failure this closes is counting within a generation and resetting between them.

    A new generation is modelled the way the engine produces one: fresh lineages, fresh
    specifications, the same data. The context count accumulates across all of it, because
    every earlier generation was part of the search that selected the survivors.
    """
    for generation in range(GENERATION_TOTAL):
        await ledger.register(
            specification(
                grid_shape=GRID_TWENTY,
                lineage_id=f"lin-gen-{generation}",
                statement=f"generation {generation}",
            ),
            charged_at_utc=CHARGED_AT + timedelta(days=generation),
        )

    assert (
        await ledger.context_trial_count(CONTEXT.context_hash) == GENERATION_TOTAL * DECLARED_TWENTY
    )
    assert await ledger.lineage_trial_count(CONTEXT.context_hash, "lin-gen-0") == DECLARED_TWENTY


# ---------------------------------------------------------------------------
# Durability and concurrency
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_the_count_survives_a_process_restart(migrated_dsn: str, ledger: TrialLedger) -> None:
    """An in-memory counter is zeroed by exactly the restart that follows an incident."""
    await ledger.register(specification(grid_shape=GRID_TWO_HUNDRED), charged_at_utc=CHARGED_AT)

    restarted = create_async_engine(migrated_dsn)
    try:
        assert (
            await TrialLedger(restarted).context_trial_count(CONTEXT.context_hash)
            == DECLARED_TWO_HUNDRED
        )
    finally:
        await restarted.dispose()


def test_the_count_survives_re_running_the_migrations(migrated_dsn: str) -> None:
    """`make migrate` against a database at head must move no number.

    Synchronous, unlike everything else here: `migrations/env.py` calls `asyncio.run()`
    itself, which raises from inside a running loop.
    """

    async def charge() -> int:
        engine = create_async_engine(migrated_dsn)
        try:
            ledger_under_test = TrialLedger(engine)
            await ledger_under_test.register(
                specification(grid_shape=GRID_TWO_HUNDRED), charged_at_utc=CHARGED_AT
            )
            return await ledger_under_test.context_trial_count(CONTEXT.context_hash)
        finally:
            await engine.dispose()

    async def read() -> int:
        engine = create_async_engine(migrated_dsn)
        try:
            return await TrialLedger(engine).context_trial_count(CONTEXT.context_hash)
        finally:
            await engine.dispose()

    before = asyncio.run(charge())
    command.upgrade(alembic_config(migrated_dsn), "head")

    assert asyncio.run(read()) == before == DECLARED_TWO_HUNDRED


@pytest.mark.asyncio
async def test_fifty_concurrent_registrations_sum_exactly(app_engine: AsyncEngine) -> None:
    """No lost update. The running total is computed in the database under an advisory
    lock, never by a read-then-write in Python -- two writers reading the same predecessor
    produce a total below the sum of their charges, which understates the selection pool
    in the flattering direction.

    A pool wide enough to hold all fifty at once, because a pool of five would serialise
    them and the test would pass without ever exercising the contention it is about.
    """
    concurrent = create_async_engine(
        app_engine.url, pool_size=CONCURRENT_REGISTRATIONS, max_overflow=0
    )
    try:
        ledger_under_test = TrialLedger(concurrent)
        specifications = [
            specification(
                grid_shape=(index + 1, 1),
                lineage_id=f"lin-{index:02d}",
                statement=f"s{index}",
            )
            for index in range(CONCURRENT_REGISTRATIONS)
        ]
        expected = sum(declared.declared_grid_point_count for declared in specifications)

        receipts = await asyncio.gather(
            *(
                ledger_under_test.register(declared, charged_at_utc=CHARGED_AT)
                for declared in specifications
            )
        )

        assert await ledger_under_test.context_trial_count(CONTEXT.context_hash) == expected
        # Every cumulative total is distinct, which is what a lost update destroys: two
        # writers reading the same predecessor produce two rows carrying one total.
        assert len({receipt.cumulative_trials for receipt in receipts}) == CONCURRENT_REGISTRATIONS
        assert max(receipt.cumulative_trials for receipt in receipts) == expected
    finally:
        await concurrent.dispose()


# ---------------------------------------------------------------------------
# The engine refuses an unregistered specification
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_the_engine_refuses_a_specification_the_ledger_never_saw(
    ledger: TrialLedger,
) -> None:
    """No result object, and the ledger is untouched by the refusal.

    Refused rather than auto-registered: registering here would charge the grid at the
    moment of execution, which is exactly the charge point that prices optional stopping
    at zero.
    """
    unregistered = await ledger.registration_for("cd" * 32)
    assert unregistered.trials_charged == 0

    loop = EventLoop(_run_config(), _NullHandler(), registration=unregistered)
    with pytest.raises(UnregisteredSpecificationError, match="no trial-ledger charge"):
        loop.run([])

    assert await ledger.context_trial_count(CONTEXT.context_hash) == 0


@pytest.mark.asyncio
async def test_a_registered_specification_runs_and_carries_its_spec_hash(
    ledger: TrialLedger,
) -> None:
    receipt = await ledger.register(specification(grid_shape=(2, 2)), charged_at_utc=CHARGED_AT)
    registration = await ledger.registration_for(receipt.spec_hash)
    assert registration == SpecRegistration(
        spec_hash=receipt.spec_hash, trials_charged=DECLARED_EIGHT
    )

    trace = EventLoop(_run_config(), _NullHandler(), registration=registration).run([])

    assert trace.spec_hash == receipt.spec_hash
    assert trace.event_count == 0


# ---------------------------------------------------------------------------
# The wiring into the deflated Sharpe
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_the_trial_count_reaches_the_deflated_sharpe_from_the_ledger(
    ledger: TrialLedger,
) -> None:
    """The join this module exists for: `SR*` is computed against what the project ran.

    The same observed performance is deflated twice, once after 200 charged trials and
    once after 4,200. The second must be strictly lower, because every test anyone runs
    makes every future result harder to prove.
    """
    await ledger.register(specification(grid_shape=GRID_TWO_HUNDRED), charged_at_utc=CHARGED_AT)
    performance = ObservedPerformance(
        observed_sharpe=Decimal("0.42"),
        independent_episode_count=37,
        skewness=Decimal("-0.30"),
        kurtosis=Decimal("6.00"),
        sharpe_variance_across_trials=Decimal("0.01"),
    )

    after_two_hundred = await ledger.sharpe_evidence(
        performance, search_context_hash=CONTEXT.context_hash
    )
    assert after_two_hundred.trials_at_time_of_run == DECLARED_TWO_HUNDRED

    await ledger.register(
        specification(
            grid_shape=GRID_FOUR_THOUSAND, lineage_id="lin-sweep", statement="a large sweep"
        ),
        charged_at_utc=CHARGED_AT,
    )
    after_the_sweep = await ledger.sharpe_evidence(
        performance, search_context_hash=CONTEXT.context_hash
    )

    assert after_the_sweep.trials_at_time_of_run == DECLARED_TWO_HUNDRED + DECLARED_FOUR_THOUSAND
    assert deflated_sharpe_ratio(after_the_sweep) < deflated_sharpe_ratio(after_two_hundred)


@pytest.mark.asyncio
async def test_an_empty_context_refuses_to_deflate_rather_than_benchmarking_at_zero(
    ledger: TrialLedger,
) -> None:
    """Zero trials never means "no deflation needed"; it means the search is undeclared."""
    performance = ObservedPerformance(
        observed_sharpe=Decimal("2.10"),
        independent_episode_count=37,
        skewness=Decimal("0"),
        kurtosis=Decimal("3"),
        sharpe_variance_across_trials=Decimal("0.01"),
    )

    with pytest.raises(TrialLedgerError, match="empty ledger"):
        await ledger.sharpe_evidence(performance, search_context_hash=CONTEXT.context_hash)


# ---------------------------------------------------------------------------
# Specifications that cannot be charged are refused before any data is read
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_a_holdout_request_without_an_authorisation_is_refused() -> None:
    with pytest.raises(TrialSpecificationError, match="reading it burns it"):
        specification(holdout_requested=True)


@pytest.mark.unit
def test_a_parameter_with_no_candidates_is_refused() -> None:
    with pytest.raises(TrialSpecificationError, match="charge nothing"):
        TrialSpecification(
            correlation_id=uuid4(),
            statement="an empty axis",
            registered_by="evolution.optimizer",
            parameter_grid={"fast_window_bars": ()},
            search_context=CONTEXT,
            lineage_id="lin-empty",
        )


@pytest.mark.unit
def test_the_same_grid_against_different_data_is_a_different_specification() -> None:
    """Otherwise the second registration is a duplicate-charge error rather than a charge."""
    here = specification(statement="one grid")
    there = specification(statement="one grid", context=OTHER_CONTEXT)

    assert here.spec_hash != there.spec_hash


@pytest.mark.unit
def test_the_search_context_hash_ignores_the_order_of_its_collections() -> None:
    reordered = SearchContext(
        symbol_universe=("ETHUSDT", "BTCUSDT"),
        window_start_utc=CONTEXT.window_start_utc,
        window_end_utc=CONTEXT.window_end_utc,
        feature_ids=CONTEXT.feature_ids,
    )

    assert reordered.context_hash == CONTEXT.context_hash


class _NullHandler:
    """A handler that is never called: every run here starts with an empty queue.

    What these two tests are about is the gate in front of the loop, so the loop is given
    nothing to do. A handler that did something would make the assertion about the run
    rather than about the registration.
    """

    def on_event(self, event: Event, context: RunContext) -> None:  # pragma: no cover
        raise AssertionError(
            f"no event was scheduled, but {type(event).__name__} arrived at "
            f"{context.now_utc().isoformat()}"
        )


def _run_config() -> RunConfig:
    return RunConfig(
        strategy_id="strat-momentum-0001",
        strategy_version="2026.08.0",
        symbols=CONTEXT.symbol_universe,
        parameters={"entry_threshold": Decimal("1.5")},
        start_utc=CONTEXT.window_start_utc,
        end_utc=CONTEXT.window_end_utc,
        run_seed=20260807,
    )
