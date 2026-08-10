"""Whether an executed configuration finished, and what killed it if it did not.

Revision ID: 0021_trial_execution_outcome
Revises: 0020_risk_conviction_map

`0018_trial_ledger_search_context` made every executed configuration leave a row in
`trial_execution` and charged the surplus beyond the declared grid. It recorded *that* an
execution happened and said nothing about how it ended, and issue #39 is the case where
that silence costs a number: a run that crashes half way through is still a trial. The
twelve configurations you ran and abandoned are the twelve alternatives you would have
accepted had they looked better, and a ledger that only records completed runs is an
instrument for laundering failed searches -- run 28 CPCV paths, keep the good ones, report
the count of what you kept.

So the outcome is recorded alongside the execution, and a failure carries the traceback
that produced it. The traceback is not decoration: without it a charged row for a crashed
run is indistinguishable from a charged row for a run that was never attempted, and the
first question anyone asks of an unexplained charge is which of those it was.

**Both columns are nullable and carry no `DEFAULT`.** Same reasoning as 0018, and the
same single exception in `docs/rules/append-only-audit.md`: rows written before this
migration did not record an outcome, and a `DEFAULT 'completed'` would have them assert
one. `NULL` is the truthful record. The reconcile trigger refuses a new row that omits
`outcome`, which is `NOT NULL` from here forward without rewriting a historical row --
and rewriting one is exactly what the table forbids.

**The overflow ledger row carries the failure too.** `fking_trial_execution_reconcile()`
already writes a `trial_ledger` charge when an execution overruns the declared grid; it
now builds that row's `parameter_grid` with the outcome and the traceback in it, so the
charged row itself explains what it paid for. Reading `trial_execution` to find out is a
second query nobody runs during an incident, and the ledger is the artefact a deflated
Sharpe is defended with.

`failure_detail` is bounded at 16 KiB by a `CHECK`. A recursion traceback is megabytes,
and `trial_ledger` is append-only, so one pathological row would be permanent. The writer
keeps the *tail* when it truncates, because Python renders the exception type and message
last and the innermost frames just above it.
"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op

revision: str = "0021_trial_execution_outcome"
down_revision: str | None = "0020_risk_conviction_map"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

# The key 0002 reserved for the trial ledger chain, unchanged from 0018. The replaced
# function must take the same one: an execution insert reads the declared grid and may
# write a ledger row, so it has to serialise against registrations on that lock.
_LEDGER_LOCK_KEY: int = 8812331

#: Bytes of traceback kept on a failed execution. Roughly a 120-frame Python traceback at
#: typical frame widths -- deep enough for the engine's dispatch stack plus a handler's,
#: short enough that a runaway recursion cannot make one row of an append-only table
#: larger than the rest of the table.
_FAILURE_DETAIL_LIMIT_BYTES: int = 16384


def upgrade() -> None:
    op.execute("ALTER TABLE trial_execution ADD COLUMN outcome TEXT")
    op.execute("ALTER TABLE trial_execution ADD COLUMN failure_detail TEXT")
    # How far the run got before it ended. "It crashed on the third event" and "it
    # crashed after four hundred thousand" are different incidents, and the traceback
    # alone does not distinguish them.
    op.execute("ALTER TABLE trial_execution ADD COLUMN dispatched_event_count INTEGER")
    op.execute(
        """
        ALTER TABLE trial_execution
            ADD CONSTRAINT ck_trial_execution_dispatched_event_count_is_counted
            CHECK (dispatched_event_count IS NULL OR dispatched_event_count >= 0)
        """
    )

    op.execute(
        """
        ALTER TABLE trial_execution ADD CONSTRAINT ck_trial_execution_outcome_is_known
            CHECK (outcome IS NULL OR outcome IN ('completed', 'failed'))
        """
    )
    # The biconditional rather than two one-way checks: a 'completed' row carrying a
    # traceback is as wrong as a 'failed' row without one, and it is the direction that
    # would otherwise survive -- a copied insert statement that leaves the detail set.
    op.execute(
        """
        ALTER TABLE trial_execution
            ADD CONSTRAINT ck_trial_execution_failure_detail_matches_outcome
            CHECK (outcome IS NULL OR ((outcome = 'failed') = (failure_detail IS NOT NULL)))
        """
    )
    op.execute(
        f"""
        ALTER TABLE trial_execution
            ADD CONSTRAINT ck_trial_execution_failure_detail_is_bounded
            CHECK (failure_detail IS NULL
                   OR octet_length(failure_detail) <= {_FAILURE_DETAIL_LIMIT_BYTES})
        """
    )

    # Replaced whole rather than patched: the body is 0018's with the outcome refusal
    # added and the overflow row's jsonb widened. A trigger function assembled from two
    # migrations is a function nobody can read in one place, and this one decides what a
    # search is charged.
    op.execute(
        f"""
        CREATE OR REPLACE FUNCTION fking_trial_execution_reconcile() RETURNS trigger
        LANGUAGE plpgsql AS $$
        DECLARE
            registration trial_ledger%ROWTYPE;
            executed     integer;
        BEGIN
            PERFORM pg_advisory_xact_lock({_LEDGER_LOCK_KEY});

            IF NEW.outcome IS NULL THEN
                RAISE EXCEPTION
                    'trial_execution: outcome is required; an execution recorded without '
                    'one cannot be distinguished from a run that never started'
                    USING ERRCODE = 'not_null_violation';
            END IF;

            SELECT * INTO registration
              FROM trial_ledger
             WHERE spec_hash = NEW.spec_hash
               AND entry_kind IS DISTINCT FROM 'execution_overflow';
            IF NOT FOUND THEN
                RAISE EXCEPTION
                    'trial_execution: spec_hash % was never registered; an execution '
                    'that skips the ledger is an undeclared search',
                    encode(NEW.spec_hash, 'hex')
                    USING ERRCODE = 'foreign_key_violation';
            END IF;

            SELECT count(*) INTO executed
              FROM trial_execution WHERE spec_hash = NEW.spec_hash;
            NEW.execution_index := executed + 1;

            -- Only the surplus is charged. The declared grid was charged in full at
            -- registration, so charging every execution as well would double-count the
            -- ordinary case of declaring 200 and running 200.
            --
            -- A failed execution is charged on exactly the same terms as a completed one.
            -- The alternative -- refunding a crash -- prices "kill the process once the
            -- numbers look bad" at zero.
            NEW.charged := NEW.execution_index > registration.trials_charged;
            IF NEW.charged THEN
                INSERT INTO trial_ledger (
                    charged_at_utc, correlation_id, spec_hash, registered_by, statement,
                    parameter_grid, n_parameters, n_symbols, n_variants, trials_charged,
                    cumulative_trials, holdout_touched, human_authorisation_ref,
                    prev_hash, row_hash, search_context_hash, lineage_id, entry_kind
                ) VALUES (
                    NEW.executed_at_utc, NEW.correlation_id, NEW.spec_hash,
                    registration.registered_by,
                    format('execution %s of spec %s exceeded the declared grid of %s',
                           NEW.execution_index, encode(NEW.spec_hash, 'hex'),
                           registration.trials_charged),
                    jsonb_build_object('config_hash', encode(NEW.config_hash, 'hex'),
                                       'path_label', NEW.path_label,
                                       'execution_index', NEW.execution_index,
                                       'outcome', NEW.outcome,
                                       'failure_detail', NEW.failure_detail),
                    registration.n_parameters, registration.n_symbols,
                    registration.n_variants,
                    1, 0, registration.holdout_touched,
                    registration.human_authorisation_ref,
                    '\\x00'::bytea, '\\x00'::bytea,
                    registration.search_context_hash, registration.lineage_id,
                    'execution_overflow'
                );
            END IF;
            RETURN NEW;
        END;
        $$
        """
    )


def downgrade() -> None:
    raise RuntimeError(
        "0021_trial_execution_outcome is irreversible. Dropping outcome and "
        "failure_detail discards the only record of which charged executions crashed and "
        "why, and the charges survive the drop -- so what is left is a set of trials the "
        "project paid for and can no longer account for. Restoring the columns cannot "
        "restore their contents, because trial_execution refuses UPDATE. Roll forward "
        "with a new revision."
    )
