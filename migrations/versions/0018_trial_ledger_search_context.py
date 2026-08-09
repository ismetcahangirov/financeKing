"""Search context, lineage, and the execution half of `max(declared, executed)`.

Revision ID: 0018_trial_ledger_search_context
Revises: 0017_risk_drawdown_state

`0002_audit_substrate` built the substrate this extends: `trial_ledger` is already
append-only, hash-chained, advisory-locked and monotone. What it could not answer is the
two questions issue #82 makes the counter's whole purpose.

**Which search does a trial belong to?** Deflation depends on how many things *the
project* tried against *this data*. Trials run against a different symbol universe, a
different date range or a different feature set do not contaminate each other, so the
counter is keyed on `search_context_hash`. Within one context two figures are kept: the
context-wide count, which feeds `SR*`, and a per-lineage count, which feeds the family
term. A system that deflates only by lineage treats each family as if it were the only
search ever run.

**Did the search overrun the grid it declared?** The declared grid is charged at
registration -- that is the existing `trials_charged` -- and closes optional stopping.
It does not close the opposite evasion: declare five points, run sixty. So every
executed configuration is recorded in `trial_execution`, and the execution whose index
exceeds the declared grid writes a further `trial_ledger` row charging one. Declared plus
the overflow is exactly `max(declared, executed)`, and charging a `max()` rather than a
sum is deliberate: summing would double-charge the honest case of declaring 200 and
running 200, and a defence that punishes correct behaviour gets routed around.

The reconciliation lives in a `BEFORE INSERT` trigger on `trial_execution` rather than in
Python. A counter the caller maintains voluntarily is a counter that will be wrong, and
the same trigger is what refuses an execution against a `spec_hash` nobody registered.

**The three new columns are nullable and carry no `DEFAULT`.** That is the one
`docs/rules/append-only-audit.md` exception, and it is the whole of it: adding a
column changes the table's shape, and a `DEFAULT` or a backfill would make rows written
before this migration *report* a context they never carried. `NULL` is the truthful
record -- we did not capture it then. The digest recipe in `fking_trial_ledger_digest` is
untouched for the same reason, so every existing `row_hash` still verifies; the trigger
below refuses any new row that omits them, which is `NOT NULL` from here forward without
rewriting a single historical row.

`uq_trial_ledger_spec_hash` becomes a partial unique index. One registration per
specification is still the rule -- `entry_kind IS DISTINCT FROM 'execution_overflow'`
covers both the historical `NULL` rows and new registrations -- while a spec that
overran its grid may carry several overflow charges, one per surplus execution.
"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op

revision: str = "0018_trial_ledger_search_context"
down_revision: str | None = "0017_risk_drawdown_state"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_APP: str = "fking_app"

# The same key 0002 reserved for the trial ledger chain. Sharing it rather than taking a
# second one is required, not tidy: an execution insert reads the ledger's declared grid
# and may write a ledger row, so it must serialise against registrations on the same
# lock. Two keys would let a registration and an execution interleave and produce a
# cumulative total below the sum of the charges.
_LEDGER_LOCK_KEY: int = 8812331


def upgrade() -> None:
    # -- trial_ledger: search context, lineage, and what kind of charge a row is --------
    op.execute("ALTER TABLE trial_ledger ADD COLUMN search_context_hash BYTEA")
    op.execute("ALTER TABLE trial_ledger ADD COLUMN lineage_id TEXT")
    op.execute("ALTER TABLE trial_ledger ADD COLUMN entry_kind TEXT")

    op.execute(
        """
        ALTER TABLE trial_ledger
            ADD CONSTRAINT ck_trial_ledger_entry_kind_is_known
            CHECK (entry_kind IS NULL
                   OR entry_kind IN ('registration', 'execution_overflow'))
        """
    )
    # The three travel together or not at all. A row carrying a lineage but no context is
    # a row whose per-lineage count cannot be attributed to a search, which is the shape
    # a partial write would leave behind.
    op.execute(
        """
        ALTER TABLE trial_ledger
            ADD CONSTRAINT ck_trial_ledger_search_context_is_complete
            CHECK (num_nulls(search_context_hash, lineage_id, entry_kind) IN (0, 3))
        """
    )
    op.execute(
        """
        ALTER TABLE trial_ledger
            ADD CONSTRAINT ck_trial_ledger_search_context_hash_is_sha256
            CHECK (search_context_hash IS NULL OR octet_length(search_context_hash) = 32)
        """
    )

    op.execute("ALTER TABLE trial_ledger DROP CONSTRAINT uq_trial_ledger_spec_hash")
    op.execute(
        """
        CREATE UNIQUE INDEX uq_trial_ledger_registration_spec_hash
            ON trial_ledger (spec_hash)
         WHERE entry_kind IS DISTINCT FROM 'execution_overflow'
        """
    )
    op.execute(
        """
        CREATE INDEX ix_trial_ledger_search_context_hash
            ON trial_ledger (search_context_hash, lineage_id)
        """
    )

    # CREATE OR REPLACE keeps the trigger binding and the digest recipe. Two things
    # change: the refusal, so that from here forward a row without its search context
    # does not get written; and the predecessor lookup, which 0002 wrote as
    # `ORDER BY seq DESC` and which loses a charge under concurrency.
    #
    # The reason it loses one is worth stating, because it is invisible in a
    # single-writer test and it flatters. `seq` is an IDENTITY column, so its value is
    # allocated while the tuple is being formed -- *before* the BEFORE INSERT trigger
    # runs and therefore before the advisory lock is taken. Commit order and seq order
    # are then unrelated: a transaction holding seq 11 can commit before one holding
    # seq 10, and the next writer's `ORDER BY seq DESC LIMIT 1` sees 11 and never sees
    # 10, silently dropping seq 10's charge from the running total. Fifty concurrent
    # registrations reproduce it; two rarely do.
    #
    # `ORDER BY cumulative_trials DESC` is immune, because `trials_charged >= 1` makes
    # the running total strictly increasing in commit order, so the largest total is
    # always the true chain tail whatever the sequence handed out. `TrialLedger` also
    # takes the same advisory lock before issuing its INSERT, which pulls the sequence
    # allocation inside the lock and keeps seq order equal to commit order -- that is
    # what `fking.platform.persistence.chain` needs, since it verifies the chain in seq
    # order. Two mechanisms, because the first keeps the count right for a writer that
    # forgot the second, and the second keeps the chain verifiable.
    op.execute(
        f"""
        CREATE OR REPLACE FUNCTION fking_trial_ledger_accumulate() RETURNS trigger
        LANGUAGE plpgsql AS $$
        DECLARE
            previous  bigint;
            last_hash bytea;
        BEGIN
            IF NEW.search_context_hash IS NULL
               OR NEW.lineage_id IS NULL
               OR NEW.entry_kind IS NULL THEN
                RAISE EXCEPTION
                    'trial_ledger requires search_context_hash, lineage_id and '
                    'entry_kind: a charge that cannot be attributed to a search is a '
                    'charge no deflated Sharpe can use'
                    USING ERRCODE = 'not_null_violation';
            END IF;

            -- {_LEDGER_LOCK_KEY} is the fixed key 0002 reserved for this chain. Without
            -- the lock two concurrent registrations read the same predecessor and
            -- produce a cumulative total below the sum of the charges -- which
            -- understates the selection pool and inflates every deflated Sharpe
            -- computed from it.
            PERFORM pg_advisory_xact_lock({_LEDGER_LOCK_KEY});

            SELECT cumulative_trials, row_hash INTO previous, last_hash
              FROM trial_ledger ORDER BY cumulative_trials DESC, seq DESC LIMIT 1;

            -- Monotone by construction: the running total is computed here, never
            -- supplied by the caller.
            NEW.cumulative_trials := COALESCE(previous, 0) + NEW.trials_charged;
            NEW.prev_hash := COALESCE(last_hash, '\\x00'::bytea);
            -- The digest input is unchanged from 0002 on purpose: the new columns sit
            -- outside it, so every row written before this migration still verifies
            -- against the same recipe and the chain has one version rather than two.
            NEW.row_hash  := fking_trial_ledger_digest(
                NEW.prev_hash, NEW.seq, NEW.charged_at_utc, NEW.correlation_id,
                NEW.spec_hash, NEW.registered_by, NEW.statement, NEW.parameter_grid,
                NEW.trials_charged, NEW.cumulative_trials, NEW.holdout_touched);
            RETURN NEW;
        END;
        $$
        """
    )

    # -- trial_execution ---------------------------------------------------------------
    op.execute(
        """
        CREATE TABLE trial_execution (
            seq              BIGINT      GENERATED ALWAYS AS IDENTITY,
            executed_at_utc  TIMESTAMPTZ NOT NULL,
            recorded_at_utc  TIMESTAMPTZ DEFAULT clock_timestamp() NOT NULL,
            correlation_id   UUID        NOT NULL,
            spec_hash        BYTEA       NOT NULL,
            -- 1-based, assigned by the trigger. The caller cannot supply it: a caller
            -- that numbers its own executions can renumber them, and the index is what
            -- decides whether this execution overran the declared grid.
            execution_index  INTEGER     NOT NULL,
            -- The RunConfig identity actually executed, so a surplus execution can be
            -- traced to the configuration that caused the charge.
            config_hash      BYTEA       NOT NULL,
            -- Which CPCV path or walk-forward fold this was. A CPCV evaluation counts
            -- one trial per path, registered as each path completes: batching at the end
            -- lets a crashed run launder its failed paths.
            path_label       TEXT        NOT NULL,
            charged          BOOLEAN     NOT NULL,
            CONSTRAINT pk_trial_execution PRIMARY KEY (seq),
            CONSTRAINT ck_trial_execution_index_is_positive CHECK (execution_index >= 1),
            CONSTRAINT ck_trial_execution_config_hash_is_sha256
                CHECK (octet_length(config_hash) = 32),
            CONSTRAINT uq_trial_execution_spec_hash_execution_index
                UNIQUE (spec_hash, execution_index)
        )
        """
    )
    op.execute("CREATE INDEX ix_trial_execution_correlation_id ON trial_execution (correlation_id)")

    op.execute(
        f"""
        CREATE FUNCTION fking_trial_execution_reconcile() RETURNS trigger
        LANGUAGE plpgsql AS $$
        DECLARE
            registration trial_ledger%ROWTYPE;
            executed     integer;
        BEGIN
            PERFORM pg_advisory_xact_lock({_LEDGER_LOCK_KEY});

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
                                       'execution_index', NEW.execution_index),
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
    op.execute(
        "CREATE TRIGGER trial_execution_reconcile_before_insert BEFORE INSERT ON "
        "trial_execution FOR EACH ROW EXECUTE FUNCTION fking_trial_execution_reconcile()"
    )
    op.execute(
        "CREATE TRIGGER trial_execution_no_update_delete BEFORE UPDATE OR DELETE ON "
        "trial_execution FOR EACH ROW EXECUTE FUNCTION fking_append_only_guard()"
    )

    # -- the two counts a deflated Sharpe is computed from ------------------------------
    #
    # Derived rather than stored. A stored running total per context would need its own
    # column, its own place in the digest and its own migration to add a third grouping;
    # a SUM over an append-only, hash-chained table is exact for the same reason the
    # chain exists -- a row cannot be removed without breaking the link that follows it.
    op.execute(
        """
        CREATE VIEW trial_context_totals AS
        SELECT search_context_hash,
               sum(trials_charged)::bigint AS context_trials,
               count(*) FILTER (WHERE entry_kind = 'registration')::bigint
                   AS registration_rows,
               count(*) FILTER (WHERE entry_kind = 'execution_overflow')::bigint
                   AS overflow_rows,
               max(seq)::bigint AS last_seq
          FROM trial_ledger
         WHERE search_context_hash IS NOT NULL
         GROUP BY search_context_hash
        """
    )
    op.execute(
        """
        CREATE VIEW trial_lineage_totals AS
        SELECT search_context_hash,
               lineage_id,
               sum(trials_charged)::bigint AS lineage_trials,
               max(seq)::bigint AS last_seq
          FROM trial_ledger
         WHERE search_context_hash IS NOT NULL
         GROUP BY search_context_hash, lineage_id
        """
    )

    # Owned by the migrator like every other object, so that `fking_app`'s rights are
    # exactly the grant below and nothing it inherits from owning the table.
    op.execute("ALTER TABLE trial_execution OWNER TO fking_migrator")
    for view in ("trial_context_totals", "trial_lineage_totals"):
        op.execute(f"ALTER VIEW {view} OWNER TO fking_migrator")

    op.execute("REVOKE ALL ON trial_execution FROM PUBLIC")
    op.execute("REVOKE ALL ON trial_execution FROM fking_app")
    op.execute(f"GRANT INSERT, SELECT ON trial_execution TO {_APP}")
    for view in ("trial_context_totals", "trial_lineage_totals"):
        op.execute(f"REVOKE ALL ON {view} FROM PUBLIC")
        op.execute(f"GRANT SELECT ON {view} TO {_APP}")


def downgrade() -> None:
    raise RuntimeError(
        "0018_trial_ledger_search_context is irreversible. Dropping trial_execution "
        "discards the record of which configurations were actually run, and with it "
        "every overflow charge's justification -- so the counter would report a total "
        "no surviving row explains. Dropping the search-context columns is worse: the "
        "charges remain but can no longer be attributed to a search, which silently "
        "collapses every per-context and per-lineage count to the global one. Neither "
        "is recoverable from a later insert. Roll forward with a new migration; if the "
        "intent is to discard a local development database, drop the database rather "
        "than the trial ledger."
    )
