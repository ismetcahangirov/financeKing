"""Twelve re-fits leave twelve rows in the real `trial_ledger`, and a crash leaves its own.

`tests/backtest/test_walk_forward_harness.py` proves that `run_walk_forward` invokes the
injected charge exactly once per planned fold. That is a statement about a callable. This
file is the statement about the database, and the two are not the same claim: the property
that matters at the gate is that the charge is *durable* -- that a fold which crashed after
being charged cannot get its trial back, because `trial_ledger` refuses `UPDATE` and
`DELETE` at the database and the running total is computed by a trigger rather than
supplied by the caller (`.claude/rules/append-only-audit.md`).

A recording list cannot show that. A list can be truncated, and a mock ledger is a ledger
whose monotonicity is whatever the test author wrote. Against the real table, the twelve
rows are twelve rows and nothing in this process can make them eleven.

The charge is deliberately not batched into one row of `trials_charged = 12`. A re-fit is a
distinct configuration evaluated against a distinct context, so each one carries its own
`spec_hash` -- and the `UNIQUE` constraint on that column then makes double-charging a
single fold an `IntegrityError` rather than an accounting drift nobody notices
(`.claude/rules/overfitting-defences.md`).

`run_walk_forward` is synchronous and the driver is asyncpg, so the harness runs in a
worker thread and the charger hands each insert back to the test's event loop, blocking
until it commits. That ordering is the point rather than an artefact: the row is committed
before the evaluator for that fold is called, which is what makes the charge survive a
crash inside it.
"""

from __future__ import annotations

import asyncio
import hashlib
import uuid
from collections.abc import Sequence
from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest
import sqlalchemy as sa
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncEngine

from fking.backtest.walkforward import (
    Fold,
    FoldEvaluationError,
    OutOfSampleObservation,
    WalkForwardReport,
    WalkForwardScheme,
    build_schedule,
    run_walk_forward,
    segment_windows,
)
from tests.backtest.walk_forward_support import plan_for

pytestmark = [pytest.mark.integration, pytest.mark.slow]

SEGMENT_SPAN = timedelta(days=5)

#: The issue's acceptance criterion: a run with twelve re-fits leaves twelve charges.
REFIT_TOTAL = 12

#: Folds whose evaluator raises after the charge has already committed.
CRASHING_FOLDS = frozenset({4, 9})

_INSERT_FOLD_CHARGE = sa.text(
    """
    INSERT INTO trial_ledger (charged_at_utc, correlation_id, spec_hash, registered_by,
                              statement, parameter_grid, n_parameters, n_symbols,
                              n_variants, trials_charged, cumulative_trials,
                              holdout_touched, human_authorisation_ref,
                              prev_hash, row_hash)
    VALUES (:charged_at_utc, :correlation_id, :spec_hash, 'backtest.walkforward',
            :statement, cast(:parameter_grid as jsonb), 5, 1, 1, 1, 0, false, NULL,
            '\\x00'::bytea, '\\x00'::bytea)
    RETURNING seq, cumulative_trials
    """
)


def _spec_hash(run_id: uuid.UUID, fold: Fold) -> bytes:
    """One digest per re-fit, distinct across folds and stable within one.

    Derived from the fold's own boundaries rather than from its index: two runs of the
    same plan describe the same twelve configurations, and a hash over the index alone
    would collide across unrelated plans while a hash including the run id keeps the two
    runs of one plan distinguishable in the ledger.
    """
    material = "|".join(
        [
            str(run_id),
            str(fold.fold_index),
            fold.train_start_utc.isoformat(),
            fold.train_end_utc.isoformat(),
            fold.test_start_utc.isoformat(),
            fold.test_end_utc.isoformat(),
        ]
    )
    return hashlib.sha256(material.encode("utf-8")).digest()


class PostgresFoldLedger:
    """Charges one `trial_ledger` row per fold, committed before the fold is evaluated.

    Blocking on `run_coroutine_threadsafe` is what makes "committed before" true rather
    than merely scheduled. A charge that were fired and not awaited would still produce
    twelve rows at the end of the run and would prove nothing about the ordering the
    crash test depends on.
    """

    def __init__(
        self, engine: AsyncEngine, loop: asyncio.AbstractEventLoop, run_id: uuid.UUID
    ) -> None:
        self._engine = engine
        self._loop = loop
        self._run_id = run_id
        self.charged_seqs: list[int] = []

    async def insert_charge(self, fold: Fold) -> int:
        """Append this fold's ledger row and return its `seq`."""
        async with self._engine.begin() as connection:
            row = (
                await connection.execute(
                    _INSERT_FOLD_CHARGE,
                    {
                        "charged_at_utc": datetime.now(UTC),
                        "correlation_id": self._run_id,
                        "spec_hash": _spec_hash(self._run_id, fold),
                        "statement": (
                            f"walk-forward re-fit {fold.fold_index} fitted to "
                            f"{fold.train_end_utc.isoformat()}"
                        ),
                        "parameter_grid": '{"scheme": "anchored"}',
                    },
                )
            ).one()
        return int(row.seq)

    def __call__(self, fold: Fold) -> None:
        future = asyncio.run_coroutine_threadsafe(self.insert_charge(fold), self._loop)
        self.charged_seqs.append(future.result())


def _observations(fold: Fold) -> tuple[OutOfSampleObservation, ...]:
    return tuple(
        OutOfSampleObservation(
            fold_index=fold.fold_index,
            observed_from_utc=opened_at,
            observed_to_utc=closed_at,
            sharpe_ratio=Decimal("1.0") - Decimal("0.01") * index,
        )
        for index, (opened_at, closed_at) in enumerate(segment_windows(fold, SEGMENT_SPAN))
    )


async def _run_against_postgres(
    engine: AsyncEngine, *, failing_indices: frozenset[int] = frozenset()
) -> tuple[WalkForwardReport, PostgresFoldLedger, uuid.UUID, int]:
    """Run the whole schedule with a Postgres-backed charger; return the global delta too."""
    run_id = uuid.uuid4()
    schedule = build_schedule(plan_for(WalkForwardScheme.ANCHORED, fold_target=REFIT_TOTAL))
    ledger = PostgresFoldLedger(engine, asyncio.get_running_loop(), run_id)

    def evaluate(fold: Fold) -> Sequence[OutOfSampleObservation]:
        if fold.fold_index in failing_indices:
            raise FoldEvaluationError(
                f"fold {fold.fold_index}: the archive could not serve the training window"
            )
        return _observations(fold)

    async with engine.connect() as connection:
        before = int(await connection.scalar(sa.text("SELECT n FROM global_trial_count")) or 0)

    report = await asyncio.to_thread(run_walk_forward, schedule, evaluate=evaluate, charge=ledger)

    async with engine.connect() as connection:
        after = int(await connection.scalar(sa.text("SELECT n FROM global_trial_count")) or 0)

    return report, ledger, run_id, after - before


async def _charged_fold_statements(engine: AsyncEngine, run_id: uuid.UUID) -> tuple[str, ...]:
    async with engine.connect() as connection:
        rows = (
            await connection.execute(
                sa.text(
                    "SELECT statement FROM trial_ledger WHERE correlation_id = :run_id ORDER BY seq"
                ),
                {"run_id": run_id},
            )
        ).all()
    return tuple(str(row.statement) for row in rows)


def _fold_indices_in_ledger_order(statements: Sequence[str]) -> list[int]:
    """The fold index each charged row names, in the order the ledger stored them.

    Read back out of the row rather than trusted from the loop that wrote it: the claim
    under test is that the ledger holds one row per re-fit in fold order, and a list built
    from the writer's own counter would assert that the writer counted correctly.
    """
    indices: list[int] = []
    for statement in statements:
        prefix, _, remainder = statement.partition("walk-forward re-fit ")
        if prefix or not remainder:
            raise AssertionError(f"ledger row is not a walk-forward charge: {statement!r}")
        indices.append(int(remainder.split()[0]))
    return indices


@pytest.mark.asyncio
async def test_twelve_refits_leave_twelve_rows_in_the_trial_ledger(engine: AsyncEngine) -> None:
    report, ledger, run_id, global_delta = await _run_against_postgres(engine)

    statements = await _charged_fold_statements(engine, run_id)

    assert report.trials_charged == REFIT_TOTAL
    assert len(statements) == REFIT_TOTAL
    assert len(ledger.charged_seqs) == REFIT_TOTAL
    # The running total is computed by the trigger, so a caller cannot understate it.
    assert global_delta == REFIT_TOTAL
    # Rows land in fold order, so the ledger reads as the search actually proceeded.
    assert _fold_indices_in_ledger_order(statements) == list(range(REFIT_TOTAL))


@pytest.mark.asyncio
async def test_a_fold_that_crashes_keeps_its_row_because_the_ledger_cannot_refund(
    engine: AsyncEngine,
) -> None:
    """The charge is committed before the evaluator, and `trial_ledger` refuses deletes.

    This is the property the deflated Sharpe rests on. A harness that charged on
    completion would leave ten rows here, the selection benchmark would be computed from
    ten configurations rather than twelve, and every strategy in the population would be
    deflated by slightly too little -- in the optimistic direction, permanently.
    """
    report, _ledger, run_id, global_delta = await _run_against_postgres(
        engine, failing_indices=CRASHING_FOLDS
    )

    statements = await _charged_fold_statements(engine, run_id)

    assert report.folds_completed == REFIT_TOTAL - len(CRASHING_FOLDS)
    assert report.fold_failure_total == len(CRASHING_FOLDS)
    assert report.trials_charged == REFIT_TOTAL
    # Twelve rows, not ten: the crashed folds were charged and nothing refunds them.
    assert len(statements) == REFIT_TOTAL
    assert global_delta == REFIT_TOTAL
    assert _fold_indices_in_ledger_order(statements) == list(range(REFIT_TOTAL))


@pytest.mark.asyncio
async def test_charging_the_same_refit_twice_is_refused_by_the_database(
    engine: AsyncEngine,
) -> None:
    """Each re-fit's `spec_hash` is unique, so a double charge fails loudly.

    Without this the failure mode is silent in the other direction: a retried run that
    re-charged its folds would inflate the trial count, deflate every Sharpe too hard, and
    reject strategies for an accounting reason.
    """
    run_id = uuid.uuid4()
    schedule = build_schedule(plan_for(WalkForwardScheme.ANCHORED, fold_target=REFIT_TOTAL))
    ledger = PostgresFoldLedger(engine, asyncio.get_running_loop(), run_id)
    fold = schedule.folds[0]

    await ledger.insert_charge(fold)

    with pytest.raises(IntegrityError, match="spec_hash"):
        await ledger.insert_charge(fold)
