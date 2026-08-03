"""The append-only audit substrate: audit_log and trial_ledger.

Revision ID: 0002_audit_substrate
Revises: 0001_extensions_and_reference

**Irreversible.** `downgrade()` raises, and the reason is in the exception rather than in
a comment because the person who needs it is running `alembic downgrade` during an
incident and will not be reading this docstring.

Four layers, and each one closes a hole the others do not:

1. **Revoked grants.** The primary control, because `TRUNCATE` does not fire row
   triggers and therefore cannot be intercepted by one.
2. **A `BEFORE UPDATE OR DELETE` row trigger.** The backstop for the migration that
   later hands a broad role to a new service -- `GRANT ALL ON ALL TABLES` because that
   was the fast way to unblock a deploy. Grants are gone; the trigger still holds.
3. **A per-row hash chain.** Forbidding a rewrite is not the same as detecting one. A
   superuser, a `pg_dump`/restore or direct file access can still change history, and
   the chain is what makes that *visible* -- without it a missing row is
   indistinguishable from a row that never existed.
4. **This migration being irreversible.** Rolling back a schema that holds the audit
   trail is a data-destruction operation dressed as a schema operation.

The digest expression lives in a SQL function rather than being written twice, once in
the trigger and once in the verifier. Two copies of a hash recipe drift, and the way you
find out is a verification job that reports every row as tampered -- after which the job
gets muted.

`.claude/rules/append-only-audit.md`, `.claude/rules/overfitting-defences.md`.
"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op

revision: str = "0002_audit_substrate"
down_revision: str | None = "0001_extensions_and_reference"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    # -- the shared immutability guard -------------------------------------------------
    op.execute(
        """
        CREATE FUNCTION fking_append_only_guard() RETURNS trigger
        LANGUAGE plpgsql AS $$
        BEGIN
            RAISE EXCEPTION
                '% is append-only: % is forbidden; write a correcting row instead',
                TG_TABLE_NAME, TG_OP
                USING ERRCODE = 'restrict_violation';
        END;
        $$
        """
    )

    # -- audit_log ---------------------------------------------------------------------
    op.execute(
        """
        CREATE TABLE audit_log (
            seq             BIGINT      GENERATED ALWAYS AS IDENTITY,
            occurred_at_utc TIMESTAMPTZ NOT NULL,
            recorded_at_utc TIMESTAMPTZ DEFAULT clock_timestamp() NOT NULL,
            correlation_id  UUID        NOT NULL,
            causation_id    UUID,
            actor           TEXT        NOT NULL,
            event_type      TEXT        NOT NULL,
            subject_id      TEXT        NOT NULL,
            payload         JSONB       NOT NULL,
            prev_hash       BYTEA       NOT NULL,
            row_hash        BYTEA       NOT NULL,
            CONSTRAINT pk_audit_log PRIMARY KEY (seq)
        )
        """
    )
    op.execute(
        "CREATE INDEX ix_audit_log_correlation_id_occurred_at_utc "
        "ON audit_log (correlation_id, occurred_at_utc)"
    )
    op.execute(
        "CREATE INDEX ix_audit_log_event_type_occurred_at_utc "
        "ON audit_log (event_type, occurred_at_utc)"
    )

    op.execute(
        """
        CREATE FUNCTION fking_audit_log_digest(
            p_prev_hash      bytea,
            p_seq            bigint,
            p_occurred_at    timestamptz,
            p_correlation_id uuid,
            p_causation_id   uuid,
            p_actor          text,
            p_event_type     text,
            p_subject_id     text,
            p_payload        jsonb
        ) RETURNS bytea
        LANGUAGE sql IMMUTABLE AS $$
            -- jsonb text output is canonical: keys are stored sorted and duplicates
            -- removed, unlike json. That is what makes the digest reproducible by a
            -- verifier that reads the row back.
            SELECT digest(
                p_prev_hash
                || convert_to(p_seq::text, 'UTF8')
                || convert_to(
                       to_char(p_occurred_at AT TIME ZONE 'UTC',
                               'YYYY-MM-DD"T"HH24:MI:SS.US"Z"'), 'UTF8')
                || convert_to(p_correlation_id::text, 'UTF8')
                || convert_to(COALESCE(p_causation_id::text, ''), 'UTF8')
                || convert_to(p_actor, 'UTF8')
                || convert_to(p_event_type, 'UTF8')
                || convert_to(p_subject_id, 'UTF8')
                || convert_to(p_payload::text, 'UTF8'),
                'sha256')
        $$
        """
    )
    op.execute(
        """
        CREATE FUNCTION fking_audit_log_chain() RETURNS trigger
        LANGUAGE plpgsql AS $$
        DECLARE
            last_hash bytea;
        BEGIN
            -- 5510477 is a fixed key reserved for the audit chain. Serialising audit
            -- inserts is deliberate: the chain has no meaning if two writers can read
            -- the same predecessor. The audit write rate is bounded by decisions per
            -- second, which is small here, so the contention is affordable and the
            -- alternative is not.
            PERFORM pg_advisory_xact_lock(5510477);

            SELECT row_hash INTO last_hash FROM audit_log ORDER BY seq DESC LIMIT 1;
            NEW.prev_hash := COALESCE(last_hash, '\\x00'::bytea);
            NEW.row_hash  := fking_audit_log_digest(
                NEW.prev_hash, NEW.seq, NEW.occurred_at_utc, NEW.correlation_id,
                NEW.causation_id, NEW.actor, NEW.event_type, NEW.subject_id, NEW.payload);
            RETURN NEW;
        END;
        $$
        """
    )
    op.execute(
        "CREATE TRIGGER audit_log_chain_before_insert BEFORE INSERT ON audit_log "
        "FOR EACH ROW EXECUTE FUNCTION fking_audit_log_chain()"
    )
    op.execute(
        "CREATE TRIGGER audit_log_no_update_delete BEFORE UPDATE OR DELETE ON audit_log "
        "FOR EACH ROW EXECUTE FUNCTION fking_append_only_guard()"
    )

    # -- trial_ledger ------------------------------------------------------------------
    op.execute(
        """
        CREATE TABLE trial_ledger (
            seq                     BIGINT      GENERATED ALWAYS AS IDENTITY,
            charged_at_utc          TIMESTAMPTZ NOT NULL,
            recorded_at_utc         TIMESTAMPTZ DEFAULT clock_timestamp() NOT NULL,
            correlation_id          UUID        NOT NULL,
            spec_hash               BYTEA       NOT NULL,
            registered_by           TEXT        NOT NULL,
            statement               TEXT        NOT NULL,
            parameter_grid          JSONB       NOT NULL,
            n_parameters            INTEGER     NOT NULL,
            n_symbols               INTEGER     NOT NULL,
            n_variants              INTEGER     NOT NULL,
            trials_charged          INTEGER     NOT NULL,
            cumulative_trials       BIGINT      NOT NULL,
            holdout_touched         BOOLEAN     DEFAULT false NOT NULL,
            human_authorisation_ref TEXT,
            prev_hash               BYTEA       NOT NULL,
            row_hash                BYTEA       NOT NULL,
            CONSTRAINT pk_trial_ledger PRIMARY KEY (seq),
            CONSTRAINT ck_trial_ledger_n_parameters_is_not_negative CHECK (n_parameters >= 0),
            CONSTRAINT ck_trial_ledger_n_symbols_is_positive CHECK (n_symbols >= 1),
            CONSTRAINT ck_trial_ledger_n_variants_is_positive CHECK (n_variants >= 1),
            CONSTRAINT ck_trial_ledger_trials_charged_is_positive CHECK (trials_charged >= 1),
            -- Reading the permanently held-out period burns it, and burning it is a
            -- decision a human takes once, by name.
            CONSTRAINT ck_trial_ledger_holdout_needs_authorisation
                CHECK (NOT holdout_touched OR human_authorisation_ref IS NOT NULL),
            -- One charge per specification. A second registration of the same grid is a
            -- duplicate charge, and the ledger is monotone, so it could never be undone.
            CONSTRAINT uq_trial_ledger_spec_hash UNIQUE (spec_hash)
        )
        """
    )
    op.execute("CREATE INDEX ix_trial_ledger_correlation_id ON trial_ledger (correlation_id)")

    op.execute(
        """
        CREATE FUNCTION fking_trial_ledger_digest(
            p_prev_hash         bytea,
            p_seq               bigint,
            p_charged_at        timestamptz,
            p_correlation_id    uuid,
            p_spec_hash         bytea,
            p_registered_by     text,
            p_statement         text,
            p_parameter_grid    jsonb,
            p_trials_charged    integer,
            p_cumulative_trials bigint,
            p_holdout_touched   boolean
        ) RETURNS bytea
        LANGUAGE sql IMMUTABLE AS $$
            SELECT digest(
                p_prev_hash
                || convert_to(p_seq::text, 'UTF8')
                || convert_to(
                       to_char(p_charged_at AT TIME ZONE 'UTC',
                               'YYYY-MM-DD"T"HH24:MI:SS.US"Z"'), 'UTF8')
                || convert_to(p_correlation_id::text, 'UTF8')
                || p_spec_hash
                || convert_to(p_registered_by, 'UTF8')
                || convert_to(p_statement, 'UTF8')
                || convert_to(p_parameter_grid::text, 'UTF8')
                || convert_to(p_trials_charged::text, 'UTF8')
                || convert_to(p_cumulative_trials::text, 'UTF8')
                || convert_to(p_holdout_touched::text, 'UTF8'),
                'sha256')
        $$
        """
    )
    op.execute(
        """
        CREATE FUNCTION fking_trial_ledger_accumulate() RETURNS trigger
        LANGUAGE plpgsql AS $$
        DECLARE
            previous  bigint;
            last_hash bytea;
        BEGIN
            -- 8812331 is a fixed key reserved for this chain. Without the lock two
            -- concurrent registrations read the same predecessor and produce a
            -- cumulative total below the sum of the charges -- which understates the
            -- selection pool and inflates every deflated Sharpe computed from it.
            PERFORM pg_advisory_xact_lock(8812331);

            SELECT cumulative_trials, row_hash INTO previous, last_hash
              FROM trial_ledger ORDER BY seq DESC LIMIT 1;

            -- Monotone by construction: the running total is computed here, never
            -- supplied by the caller.
            NEW.cumulative_trials := COALESCE(previous, 0) + NEW.trials_charged;
            NEW.prev_hash := COALESCE(last_hash, '\\x00'::bytea);
            NEW.row_hash  := fking_trial_ledger_digest(
                NEW.prev_hash, NEW.seq, NEW.charged_at_utc, NEW.correlation_id,
                NEW.spec_hash, NEW.registered_by, NEW.statement, NEW.parameter_grid,
                NEW.trials_charged, NEW.cumulative_trials, NEW.holdout_touched);
            RETURN NEW;
        END;
        $$
        """
    )
    op.execute(
        "CREATE TRIGGER trial_ledger_accumulate_before_insert BEFORE INSERT ON trial_ledger "
        "FOR EACH ROW EXECUTE FUNCTION fking_trial_ledger_accumulate()"
    )
    op.execute(
        "CREATE TRIGGER trial_ledger_no_update_delete BEFORE UPDATE OR DELETE ON trial_ledger "
        "FOR EACH ROW EXECUTE FUNCTION fking_append_only_guard()"
    )

    op.execute(
        """
        CREATE VIEW global_trial_count AS
        SELECT COALESCE(max(cumulative_trials), 0)::bigint AS n FROM trial_ledger
        """
    )

    # -- grants ------------------------------------------------------------------------
    for table in ("audit_log", "trial_ledger"):
        op.execute(f"REVOKE ALL ON {table} FROM PUBLIC")
        op.execute(f"REVOKE ALL ON {table} FROM fking_app")
        op.execute(f"GRANT INSERT, SELECT ON {table} TO fking_app")
    op.execute("GRANT SELECT ON global_trial_count TO fking_app")


def downgrade() -> None:
    raise RuntimeError(
        "0002_audit_substrate is irreversible. Dropping audit_log destroys the record "
        "every trade is reconstructed from, and dropping trial_ledger resets the global "
        "trial counter, which invalidates every deflated Sharpe this project has ever "
        "reported. Neither is recoverable from a later insert. Roll forward with a new "
        "migration; if the intent is to discard a local development database, drop the "
        "database rather than the audit trail."
    )
