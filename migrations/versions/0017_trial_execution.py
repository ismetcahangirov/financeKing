"""The execution half of the trial charge: one append-only row per configuration run.

Revision ID: 0017_trial_execution
Revises: 0016_evolution_derived_reads

**Irreversible.** `downgrade()` raises, for the reason `0002_audit_substrate` gives:
these rows are half of the denominator of every deflated Sharpe the project reports, and
a denominator that can be dropped is a denominator that will be. The reversible half of
#82 -- the two views that compute `max(declared, executed)` and the global total, both
pure projections of these rows -- is `0018`, so `alembic downgrade -1` remains a usable
operator action rather than a wall.

**Why a second table rather than a second row in `trial_ledger`.** The charge for a
registered specification is `max(declared_grid_size, actual_executions)`
(`.claude/rules/overfitting-defences.md` clause 3), and the two numbers close two
different evasions:

- Declare a 200-point grid, stop at point 12 because the 12th looked good, report 12.
  Closed by the *declaration*: the decision to stop early is the selection event, and
  charging at execution prices it at zero. `trial_ledger.trials_charged` already carries
  that, from `0002`.
- Run 60 configurations having registered a 5-point grid, report the best. Closed by the
  *executions*, which is what this table records.

`trial_ledger` cannot carry the second number. It holds one row per specification --
`uq_trial_ledger_spec_hash` -- charged before any data is read, and executions arrive
afterwards, one at a time, over hours. Recording them there would mean either an
`UPDATE` (which the append-only guard refuses, correctly) or relaxing the uniqueness
that makes a duplicate registration impossible. Both trade away a property that is
load-bearing to buy a column. A separate append-only table keyed to the same
`spec_hash` costs one join in a view and gives up nothing.

**`max()` and not a sum.** Summing would double-charge the honest case -- declare 200,
run 200 -- and a defence that punishes correct behaviour gets routed around inside a
month. The arithmetic lives in `0018`'s `trial_charge` view, so there is exactly one
place it is written.

**One row per CPCV path, reported as the path completes.** Batching the count until the
run finishes lets a crashed or abandoned run launder its failed paths: run 28, keep the
good ones, report the count of what was kept. `UNIQUE (spec_hash, execution_key)` is
what makes reporting each path as it completes safe to retry -- the writer uses
`ON CONFLICT DO NOTHING`, so an at-least-once redelivery charges once.

**`outcome` is recorded and never filtered on.** `EvolutionSettings
.trial_ledger_counts_failed_runs` is a `Literal[True]`, so it cannot be configured away;
this column exists so that a run's fate is reconstructable, not so that a query can
exclude one. A failed configuration was still a configuration that was tried, and a
trial count that only counts successes is a trial count designed to flatter.

**The foreign key is enforcement, not referential tidiness.** An execution reported
against a `spec_hash` nobody registered is refused by the database, which is half of
"`BacktestEngine.run()` refuses an unregistered `spec_hash`" (#39) implemented one layer
below the engine, where it holds regardless of which caller does the reporting.

Four layers of append-only, as `.claude/rules/append-only-audit.md` specifies: revoked
`UPDATE`/`DELETE`/`TRUNCATE` for `fking_app`; the shared `fking_append_only_guard()`
trigger from `0002`; a per-row hash chain computed in the database; and this migration
refusing to downgrade.

`.claude/rules/overfitting-defences.md`, `.claude/rules/append-only-audit.md`.
"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op

revision: str = "0017_trial_execution"
down_revision: str | None = "0016_evolution_derived_reads"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_APP: str = "fking_app"
_INGEST: str = "fking_ingest"
_MIGRATOR: str = "fking_migrator"

_TABLE: str = "trial_execution"

# What became of one configuration's run. All three count identically toward the charge;
# see the module docstring on why nothing filters on this column.
_OUTCOMES: str = "'completed', 'failed', 'abandoned'"

# A fixed advisory-lock key reserved for this chain, in the same series as 0002's
# 5510477 (audit_log) and 8812331 (trial_ledger) and 0015's 6620915 (lifecycle events).
# Deliberately *not* 8812331: the two chains are independent, and sharing a key would
# serialise every execution report behind every registration for no correctness gain.
_CHAIN_LOCK_KEY: int = 8812332

_DIGEST_SIGNATURE: str = (
    "fking_trial_execution_digest(bytea, bigint, bytea, text, timestamptz, uuid, text, text)"
)
_CHAIN_SIGNATURE: str = "fking_trial_execution_chain()"


def upgrade() -> None:
    op.execute(
        f"""
        CREATE TABLE {_TABLE} (
            seq             BIGINT      GENERATED ALWAYS AS IDENTITY,
            spec_hash       BYTEA       NOT NULL,
            -- Identifies the configuration within its specification: a CPCV path id, a
            -- grid point, a fold. Supplied by the reporter and stable across a retry,
            -- which is what makes the uniqueness below a deduplication rather than a
            -- collision.
            execution_key   TEXT        NOT NULL,
            executed_at_utc TIMESTAMPTZ NOT NULL,
            recorded_at_utc TIMESTAMPTZ DEFAULT clock_timestamp() NOT NULL,
            correlation_id  UUID        NOT NULL,
            reported_by     TEXT        NOT NULL,
            outcome         TEXT        NOT NULL,
            prev_hash       BYTEA       NOT NULL,
            row_hash        BYTEA       NOT NULL,
            CONSTRAINT pk_trial_execution PRIMARY KEY (seq),
            -- The idempotency arbiter. Redelivery of a completed path is the normal
            -- case on a bus with at-least-once delivery, and a second row here would
            -- charge a trial that was never run -- in an append-only table, so it could
            -- never be taken back.
            CONSTRAINT uq_trial_execution_spec_hash_execution_key
                UNIQUE (spec_hash, execution_key),
            CONSTRAINT ck_trial_execution_execution_key_is_stated
                CHECK (length(execution_key) > 0),
            CONSTRAINT ck_trial_execution_outcome_is_known
                CHECK (outcome IN ({_OUTCOMES})),
            -- An execution against a specification nobody registered is refused here,
            -- one layer below whichever caller is doing the reporting.
            CONSTRAINT fk_trial_execution_spec_hash_trial_ledger
                FOREIGN KEY (spec_hash) REFERENCES trial_ledger (spec_hash)
        )
        """
    )
    op.execute(
        f"CREATE INDEX ix_{_TABLE}_correlation_id ON {_TABLE} (correlation_id)"
    )

    op.execute(
        f"""
        CREATE FUNCTION fking_trial_execution_digest(
            p_prev_hash      bytea,
            p_seq            bigint,
            p_spec_hash      bytea,
            p_execution_key  text,
            p_executed_at    timestamptz,
            p_correlation_id uuid,
            p_reported_by    text,
            p_outcome        text
        ) RETURNS bytea
        LANGUAGE sql IMMUTABLE AS $$
            SELECT digest(
                p_prev_hash
                || convert_to(p_seq::text, 'UTF8')
                || p_spec_hash
                || convert_to(p_execution_key, 'UTF8')
                || convert_to(
                       to_char(p_executed_at AT TIME ZONE 'UTC',
                               'YYYY-MM-DD"T"HH24:MI:SS.US"Z"'), 'UTF8')
                || convert_to(p_correlation_id::text, 'UTF8')
                || convert_to(p_reported_by, 'UTF8')
                || convert_to(p_outcome, 'UTF8'),
                'sha256')
        $$
        """
    )
    op.execute(
        f"""
        CREATE FUNCTION fking_trial_execution_chain() RETURNS trigger
        LANGUAGE plpgsql AS $$
        DECLARE
            last_hash bytea;
        BEGIN
            PERFORM pg_advisory_xact_lock({_CHAIN_LOCK_KEY});

            SELECT row_hash INTO last_hash FROM {_TABLE} ORDER BY seq DESC LIMIT 1;

            -- The application cannot supply its own hashes: a writer that computes its
            -- own chain values can compute consistent ones for a forged row too.
            NEW.prev_hash := COALESCE(last_hash, '\\x00'::bytea);
            NEW.row_hash  := fking_trial_execution_digest(
                NEW.prev_hash, NEW.seq, NEW.spec_hash, NEW.execution_key,
                NEW.executed_at_utc, NEW.correlation_id, NEW.reported_by, NEW.outcome);
            RETURN NEW;
        END;
        $$
        """
    )
    op.execute(
        f"CREATE TRIGGER {_TABLE}_chain_before_insert BEFORE INSERT ON {_TABLE} "
        f"FOR EACH ROW EXECUTE FUNCTION fking_trial_execution_chain()"
    )
    op.execute(
        f"CREATE TRIGGER {_TABLE}_no_update_delete BEFORE UPDATE OR DELETE ON {_TABLE} "
        f"FOR EACH ROW EXECUTE FUNCTION fking_append_only_guard()"
    )

    # -- ownership and grants ----------------------------------------------------------
    # 0008's ownership sweep ran once, over the tables that existed then. A table created
    # afterwards is owned by whoever ran the migration unless it is said here, and an
    # owner may `ALTER TABLE ... DISABLE TRIGGER` regardless of what has been revoked.
    op.execute(f"ALTER TABLE {_TABLE} OWNER TO {_MIGRATOR}")
    op.execute(f"REVOKE ALL ON {_TABLE} FROM PUBLIC")
    op.execute(f"REVOKE ALL ON {_TABLE} FROM {_APP}")
    op.execute(f"REVOKE ALL ON {_TABLE} FROM {_INGEST}")
    # TRUNCATE fires no row trigger, so this revoke is the primary control and the
    # trigger above is the backstop, not the other way round.
    op.execute(f"GRANT SELECT, INSERT ON {_TABLE} TO {_APP}")

    for signature in (_DIGEST_SIGNATURE, _CHAIN_SIGNATURE):
        op.execute(f"ALTER FUNCTION {signature} OWNER TO {_MIGRATOR}")
        # EXECUTE is granted to PUBLIC by default, and PUBLIC is every role this cluster
        # will ever have -- including the ingestion role, which has no business writing
        # to the trial ledger.
        op.execute(f"REVOKE ALL ON FUNCTION {signature} FROM PUBLIC")
        # Both are SECURITY INVOKER, so the inserting role needs EXECUTE on the trigger
        # function *and* on the digest it calls. Granting these is what makes an ordinary
        # append work at all, not a convenience.
        op.execute(f"GRANT EXECUTE ON FUNCTION {signature} TO {_APP}")


def downgrade() -> None:
    raise RuntimeError(
        "0017_trial_execution is irreversible. Dropping trial_execution discards the "
        "record of which configurations were actually run, which is half of the "
        "max(declared, executed) charge behind every deflated Sharpe this project has "
        "reported -- and unlike a declaration, an execution cannot be reconstructed "
        "from anything that survives, because the run it records has already happened. "
        "Roll forward with a new migration; if the intent is to discard a local "
        "development database, drop the database rather than the record."
    )
