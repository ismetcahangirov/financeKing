"""Genome identity, genealogy, and the append-only lifecycle event stream.

Revision ID: 0015_evolution_lineage_store
Revises: 0014_alt_observations

**Irreversible.** `downgrade()` raises, for the reason `0002_audit_substrate` gives:
this schema holds the record every promotion, quarantine and retirement is
reconstructed from, and rolling it back is a data-destruction operation dressed as a
schema operation. The reversible half of #83 -- the derived read surface, which is a
projection of these rows and can be rebuilt from them -- is `0016`, so `alembic
downgrade -1` remains a usable operator action rather than a wall.

**Why a separate `evolution` schema.** The `public` tables from `0004` model a strategy
as a row with a writable `lifecycle_state` column. #83 rejects that shape outright: a
state column the application can `UPDATE` is a state column that gets corrected during
an incident, and the correction is exactly the row the investigation needed. The two
models cannot share a namespace without one of them silently becoming the answer to
"what state is this strategy in", so the event-sourced model gets its own schema and
`evolution.strategy` carries no state column at all. `evolution.strategy_current_state`
in `0016` is the only way to ask, and it is a view over the event stream.

**Identity is content, never a row id.** `genome.genome_hash` is a SHA-256 over the
canonical serialisation of the typed expression tree, the parameter vector, the declared
feature set and the declared horizon (`fking.evolution.genome`). Two consequences carry
the whole design:

1. A "fixed" strategy is a new genome. It cannot inherit its predecessor's held-out
   vault access and it re-enters at `proposed`.
2. `structure_hash` is the same digest with the parameter *values* removed, and
   `lineage_id` is derived from it -- so a parameter-only mutation lands in its parent's
   lineage and inherits its accumulated family trial count. Without that, a lineage that
   has consumed 612 trials across nine generations reports itself as a two-trial
   newcomer and the family deflation term believes it.

**Four layers of append-only, as `docs/rules/append-only-audit.md` specifies.**
Revoked `UPDATE`/`DELETE`/`TRUNCATE` for `fking_app`; a `BEFORE UPDATE OR DELETE` row
trigger (`fking_append_only_guard`, shared with `0002` so there is one message and one
place to harden); a per-row hash chain computed *in the database* at insert time; and
this migration refusing to downgrade. The chain is the layer people skip and the only
one that survives a superuser: forbidding a rewrite is not the same as detecting one,
and an agent that could recompute its own chain forward could rewrite its own history to
look better.

**Every one of these tables is append-only, including `strategy` and `genome`.** A
genome is content-addressed, so an `UPDATE` to one is a claim that a hash means something
other than what it digests. A strategy row records that a genome entered the population;
everything that happens to it afterwards is an event.

`EVOLUTION_ENGINE.md` sections 1, 5.6 and 8; `docs/rules/append-only-audit.md`.

**Privilege note.** `CREATE SCHEMA` requires `CREATE` on the database, which
`0008_least_privilege` did not grant -- it granted `CREATE ON SCHEMA public`, which is a
different privilege. The `GRANT` below closes that for every migration after this one,
and, like `CREATE EXTENSION` in `0001`, this one statement needs a connection that owns
the database. `DEPLOYMENT.md` already documents that split for `0001`; this is the
second and last statement in that class.
"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op

revision: str = "0015_evolution_lineage_store"
down_revision: str | None = "0014_alt_observations"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_APP: str = "fking_app"
_INGEST: str = "fking_ingest"
_MIGRATOR: str = "fking_migrator"

_SCHEMA: str = "evolution"

# EVOLUTION_ENGINE.md section 1, verbatim, plus two states that document names for
# conditions the diagram has but does not label:
#
#   `nonexistent` is the state a strategy is in before it exists. It is only ever a
#   from_state -- a CHECK forbids it as a to_state -- and it exists so that the genesis
#   event carries a from_state like every other row rather than a NULL that every reader
#   then has to special-case.
#
#   `quarantined` is what section 8 does to the descendants of a `defect` retirement.
#   It is a real state with capital withdrawn and a re-test pending, and modelling it as
#   `retired` would lose the distinction between "this was tested and failed" and "this
#   inherited a bug from its parent and has not been re-tested".
_STATES: str = (
    "'nonexistent', 'proposed', 'backtested', 'validated', 'paper', 'challenger', "
    "'champion', 'quarantined', 'retired'"
)

# The states that hold capital or produce live decisions. Lineage collapse is measured
# over these and not over the whole population: a genealogically inbred set of retired
# genomes is history, and an inbred set of live ones is a portfolio whose measured
# correlations are about to stop meaning anything (EVOLUTION_ENGINE.md section 5.6).
_LIVE_STATES: str = "'paper', 'challenger', 'champion'"

# States a strategy cannot enter without a score. A transition into one of these with a
# NULL survival_score is a promotion with no evidence behind it, and the gate that made
# it could not be re-derived later.
_SCORED_STATES: str = "'validated', 'paper', 'challenger', 'champion'"

_TABLES: tuple[str, ...] = (
    "genome",
    "genome_parent",
    "strategy",
    "strategy_lifecycle_events",
)

# A fixed advisory-lock key reserved for the lifecycle chain, in the same series as
# 0002's 5510477 (audit_log) and 8812331 (trial_ledger). Serialising the inserts is
# deliberate: the chain has no meaning if two writers can read the same predecessor, and
# the write rate here is bounded by lifecycle transitions per day, which is tiny.
_CHAIN_LOCK_KEY: int = 6620915


def upgrade() -> None:
    op.execute(
        f"""
        DO $$
        BEGIN
            EXECUTE format('GRANT CREATE ON DATABASE %I TO {_MIGRATOR}', current_database());
        END
        $$
        """
    )
    op.execute(f"CREATE SCHEMA {_SCHEMA} AUTHORIZATION {_MIGRATOR}")

    # -- genome ------------------------------------------------------------------------
    op.execute(
        f"""
        CREATE TABLE {_SCHEMA}.genome (
            genome_hash                  BYTEA       NOT NULL,
            -- The same digest with parameter values removed. Two genomes sharing it are
            -- the same hypothesis at different parameter settings, which is what makes
            -- lineage_id derivable rather than assignable.
            structure_hash               BYTEA       NOT NULL,
            lineage_id                   TEXT        NOT NULL,
            generation_number            INTEGER     NOT NULL,
            trial_index_at_creation      BIGINT      NOT NULL,
            -- A JSONB array of operator names, in application order. An array rather
            -- than a single column because section 6 composes them: a mutant may be a
            -- parameter jitter *and* a horizon change, and recording only the last one
            -- would make the population's operator mix unmeasurable.
            mutation_operators           JSONB       NOT NULL,
            scoring_version              TEXT        NOT NULL,
            expression                   JSONB       NOT NULL,
            parameters                   JSONB       NOT NULL,
            feature_ids                  JSONB       NOT NULL,
            holding_horizon_microseconds BIGINT      NOT NULL,
            created_at_utc               TIMESTAMPTZ NOT NULL,
            recorded_at_utc              TIMESTAMPTZ DEFAULT clock_timestamp() NOT NULL,
            CONSTRAINT pk_evolution_genome PRIMARY KEY (genome_hash),
            CONSTRAINT ck_evolution_genome_genome_hash_is_sha256
                CHECK (octet_length(genome_hash) = 32),
            CONSTRAINT ck_evolution_genome_structure_hash_is_sha256
                CHECK (octet_length(structure_hash) = 32),
            CONSTRAINT ck_evolution_genome_generation_number_is_not_negative
                CHECK (generation_number >= 0),
            -- Zero is legitimate here and only here: the first genome this project ever
            -- records is created before anything has been charged to the ledger.
            CONSTRAINT ck_evolution_genome_trial_index_is_not_negative
                CHECK (trial_index_at_creation >= 0),
            CONSTRAINT ck_evolution_genome_mutation_operators_is_an_array
                CHECK (jsonb_typeof(mutation_operators) = 'array'),
            CONSTRAINT ck_evolution_genome_feature_ids_is_an_array
                CHECK (jsonb_typeof(feature_ids) = 'array'),
            CONSTRAINT ck_evolution_genome_parameters_is_an_object
                CHECK (jsonb_typeof(parameters) = 'object'),
            -- A genome with no declared features cannot compute anything, and a
            -- non-positive horizon makes the embargo length in section 5.2 undefined.
            CONSTRAINT ck_evolution_genome_declares_at_least_one_feature
                CHECK (jsonb_array_length(feature_ids) >= 1),
            CONSTRAINT ck_evolution_genome_holding_horizon_is_positive
                CHECK (holding_horizon_microseconds > 0)
        )
        """
    )
    op.execute(f"CREATE INDEX ix_evolution_genome_lineage_id ON {_SCHEMA}.genome (lineage_id)")
    op.execute(
        f"CREATE INDEX ix_evolution_genome_structure_hash ON {_SCHEMA}.genome (structure_hash)"
    )

    # -- genome_parent -----------------------------------------------------------------
    # An edge table rather than a parent column, because crossover has two parents
    # (EVOLUTION_ENGINE.md section 7) and a nullable `parent_2` column is a schema that
    # cannot express a three-parent operator without another migration.
    op.execute(
        f"""
        CREATE TABLE {_SCHEMA}.genome_parent (
            child_genome_hash  BYTEA       NOT NULL,
            parent_genome_hash BYTEA       NOT NULL,
            parent_ordinal     SMALLINT    NOT NULL,
            recorded_at_utc    TIMESTAMPTZ DEFAULT clock_timestamp() NOT NULL,
            CONSTRAINT pk_evolution_genome_parent
                PRIMARY KEY (child_genome_hash, parent_genome_hash),
            CONSTRAINT uq_evolution_genome_parent_child_ordinal
                UNIQUE (child_genome_hash, parent_ordinal),
            CONSTRAINT ck_evolution_genome_parent_ordinal_is_not_negative
                CHECK (parent_ordinal >= 0),
            -- Depth one only, which is all a CHECK can see. A longer cycle needs a
            -- recursive walk and is refused before the insert by
            -- `fking.evolution.store`, whose ancestry walk raises LineageCycleError --
            -- rejected rather than stored, per the acceptance criteria.
            CONSTRAINT ck_evolution_genome_parent_is_acyclic_at_depth_one
                CHECK (child_genome_hash <> parent_genome_hash),
            -- The foreign key is the mechanism behind "an event whose parent hash is
            -- absent raises". A dangling parent would make every ancestry walk from that
            -- child terminate early and silently, which reads as a founder.
            CONSTRAINT fk_evolution_genome_parent_child_genome
                FOREIGN KEY (child_genome_hash) REFERENCES {_SCHEMA}.genome (genome_hash),
            CONSTRAINT fk_evolution_genome_parent_parent_genome
                FOREIGN KEY (parent_genome_hash) REFERENCES {_SCHEMA}.genome (genome_hash)
        )
        """
    )
    op.execute(
        f"CREATE INDEX ix_evolution_genome_parent_parent_genome_hash "
        f"ON {_SCHEMA}.genome_parent (parent_genome_hash)"
    )

    # -- strategy ----------------------------------------------------------------------
    # There is deliberately no lifecycle_state, no status, no is_live and no
    # retired_at_utc. `tests/evolution/test_lifecycle_events_are_append_only.py` asserts
    # that against information_schema, so a later migration cannot add one back without
    # a test failing and somebody having to argue for it.
    op.execute(
        f"""
        CREATE TABLE {_SCHEMA}.strategy (
            strategy_id     TEXT        NOT NULL,
            genome_hash     BYTEA       NOT NULL,
            lineage_id      TEXT        NOT NULL,
            created_at_utc  TIMESTAMPTZ NOT NULL,
            recorded_at_utc TIMESTAMPTZ DEFAULT clock_timestamp() NOT NULL,
            CONSTRAINT pk_evolution_strategy PRIMARY KEY (strategy_id),
            -- One strategy per genome. A second population member carrying the same
            -- genome is the same hypothesis funded twice, which the diversity caps in
            -- section 7 exist to prevent and which would double-count it in every
            -- lineage share.
            CONSTRAINT uq_evolution_strategy_genome_hash UNIQUE (genome_hash),
            CONSTRAINT fk_evolution_strategy_genome
                FOREIGN KEY (genome_hash) REFERENCES {_SCHEMA}.genome (genome_hash)
        )
        """
    )
    op.execute(f"CREATE INDEX ix_evolution_strategy_lineage_id ON {_SCHEMA}.strategy (lineage_id)")

    # -- strategy_lifecycle_events -----------------------------------------------------
    op.execute(
        f"""
        CREATE TABLE {_SCHEMA}.strategy_lifecycle_events (
            seq                               BIGINT GENERATED ALWAYS AS IDENTITY,
            event_id                          UUID            NOT NULL,
            strategy_id                       TEXT            NOT NULL,
            genome_hash                       BYTEA           NOT NULL,
            correlation_id                    UUID            NOT NULL,
            causation_id                      UUID,
            from_state                        TEXT            NOT NULL,
            to_state                          TEXT            NOT NULL,
            reason_class                      TEXT            NOT NULL,
            reason                            TEXT            NOT NULL,
            survival_score                    NUMERIC(38, 18),
            score_components                  JSONB           NOT NULL,
            -- Independent episodes, never raw observations. 41,208 hourly bars holding
            -- 37 funding-extremity episodes is a sample of 37, and a t-statistic
            -- computed as if it were 41,208 is off by roughly 33x.
            independent_episode_count         INTEGER         NOT NULL,
            forward_independent_episode_count INTEGER         NOT NULL,
            global_trial_index                BIGINT          NOT NULL,
            family_trial_index                BIGINT          NOT NULL,
            scoring_version                   TEXT            NOT NULL,
            occurred_at_utc                   TIMESTAMPTZ     NOT NULL,
            recorded_at_utc                   TIMESTAMPTZ DEFAULT clock_timestamp() NOT NULL,
            prev_hash                         BYTEA           NOT NULL,
            row_hash                          BYTEA           NOT NULL,
            CONSTRAINT pk_lifecycle_event PRIMARY KEY (seq),
            -- The producer's own id, stable across a republished event. Unique so that
            -- an at-least-once bus redelivery is a constraint violation the consumer can
            -- treat as "already applied" rather than a second row in an append-only
            -- table that could never be removed (docs/rules/idempotency.md).
            CONSTRAINT uq_lifecycle_event_event_id UNIQUE (event_id),
            CONSTRAINT ck_lifecycle_event_from_state_is_known
                CHECK (from_state IN ({_STATES})),
            CONSTRAINT ck_lifecycle_event_to_state_is_known
                CHECK (to_state IN ({_STATES})),
            CONSTRAINT ck_lifecycle_event_transition_moves CHECK (from_state <> to_state),
            -- `nonexistent` is where a strategy comes from, never where it goes.
            CONSTRAINT ck_lifecycle_event_to_state_is_not_nonexistent
                CHECK (to_state <> 'nonexistent'),
            -- Terminal, per section 8. There is no reactivate and no unretire, and the
            -- database is where that is stated because a rule stated only in a document
            -- is a rule the next mutation operator has not read.
            CONSTRAINT ck_lifecycle_event_retired_is_terminal
                CHECK (from_state <> 'retired'),
            CONSTRAINT ck_lifecycle_event_reason_class_is_known
                CHECK (reason_class IN ('genesis', 'gate_passed', 'gate_failed', 'defect',
                                        'risk', 'decay', 'superseded', 'environmental',
                                        'quarantine', 'operator')),
            CONSTRAINT ck_lifecycle_event_reason_is_stated CHECK (length(reason) > 0),
            CONSTRAINT ck_lifecycle_event_survival_score_is_a_fraction
                CHECK (survival_score IS NULL OR survival_score BETWEEN 0 AND 1),
            CONSTRAINT ck_lifecycle_event_score_components_is_an_object
                CHECK (jsonb_typeof(score_components) = 'object'),
            CONSTRAINT ck_lifecycle_event_episode_count_is_not_negative
                CHECK (independent_episode_count >= 0),
            CONSTRAINT ck_lifecycle_event_forward_episode_count_is_not_negative
                CHECK (forward_independent_episode_count >= 0),
            CONSTRAINT ck_lifecycle_event_global_trial_index_is_not_negative
                CHECK (global_trial_index >= 0),
            CONSTRAINT ck_lifecycle_event_family_trial_index_is_not_negative
                CHECK (family_trial_index >= 0),
            -- The family count is a subset of the global one by construction; a family
            -- ahead of the global total means one of the two counters was reset.
            CONSTRAINT ck_lifecycle_event_family_trials_within_global
                CHECK (family_trial_index <= global_trial_index),
            -- Entering a scored state requires a score, its components, a sample, and a
            -- trial count that was actually read. Zero trials never means "nothing was
            -- tried, so no deflation was needed".
            CONSTRAINT ck_lifecycle_event_scored_state_carries_evidence
                CHECK (
                    to_state NOT IN ({_SCORED_STATES})
                    OR (survival_score IS NOT NULL
                        AND score_components <> '{{}}'::jsonb
                        AND independent_episode_count > 0
                        AND global_trial_index >= 1)
                ),
            CONSTRAINT fk_lifecycle_event_strategy
                FOREIGN KEY (strategy_id) REFERENCES {_SCHEMA}.strategy (strategy_id),
            CONSTRAINT fk_lifecycle_event_genome
                FOREIGN KEY (genome_hash) REFERENCES {_SCHEMA}.genome (genome_hash)
        )
        """
    )
    op.execute(
        f"CREATE INDEX ix_lifecycle_event_strategy_id_seq "
        f"ON {_SCHEMA}.strategy_lifecycle_events (strategy_id, seq)"
    )
    op.execute(
        f"CREATE INDEX ix_lifecycle_event_correlation_id "
        f"ON {_SCHEMA}.strategy_lifecycle_events (correlation_id, occurred_at_utc)"
    )

    # -- the hash chain ----------------------------------------------------------------
    # One SQL function rather than the expression written twice, once in the trigger and
    # once in the verifier. Two copies of a hash recipe drift, and the way you find out
    # is a verification job reporting every row as tampered -- after which it gets muted.
    op.execute(
        f"""
        CREATE FUNCTION {_SCHEMA}.lifecycle_event_digest(
            p_prev_hash               bytea,
            p_seq                     bigint,
            p_event_id                uuid,
            p_strategy_id             text,
            p_genome_hash             bytea,
            p_correlation_id          uuid,
            p_causation_id            uuid,
            p_from_state              text,
            p_to_state                text,
            p_reason_class            text,
            p_reason                  text,
            p_survival_score          numeric,
            p_score_components        jsonb,
            p_episode_count           integer,
            p_forward_episode_count   integer,
            p_global_trial_index      bigint,
            p_family_trial_index      bigint,
            p_scoring_version         text,
            p_occurred_at             timestamptz
        ) RETURNS bytea
        LANGUAGE sql IMMUTABLE AS $$
            -- jsonb text output is canonical: keys are stored sorted and duplicates
            -- removed, unlike json. That is what makes the digest reproducible by a
            -- verifier reading the row back rather than by the writer that produced it.
            SELECT digest(
                p_prev_hash
                || convert_to(p_seq::text, 'UTF8')
                || convert_to(p_event_id::text, 'UTF8')
                || convert_to(p_strategy_id, 'UTF8')
                || p_genome_hash
                || convert_to(p_correlation_id::text, 'UTF8')
                || convert_to(COALESCE(p_causation_id::text, ''), 'UTF8')
                || convert_to(p_from_state, 'UTF8')
                || convert_to(p_to_state, 'UTF8')
                || convert_to(p_reason_class, 'UTF8')
                || convert_to(p_reason, 'UTF8')
                -- to_char, not ::text: numeric's text output keeps the trailing zeros
                -- the writer happened to send, so 0.50 and 0.5 would digest differently
                -- for one score. The scale is fixed at the column's 18 places instead.
                || convert_to(COALESCE(to_char(p_survival_score, 'FM9.999999999999999999'),
                                       ''), 'UTF8')
                || convert_to(p_score_components::text, 'UTF8')
                || convert_to(p_episode_count::text, 'UTF8')
                || convert_to(p_forward_episode_count::text, 'UTF8')
                || convert_to(p_global_trial_index::text, 'UTF8')
                || convert_to(p_family_trial_index::text, 'UTF8')
                || convert_to(p_scoring_version, 'UTF8')
                || convert_to(
                       to_char(p_occurred_at AT TIME ZONE 'UTC',
                               'YYYY-MM-DD"T"HH24:MI:SS.US"Z"'), 'UTF8'),
                'sha256')
        $$
        """
    )
    op.execute(
        f"""
        CREATE FUNCTION {_SCHEMA}.lifecycle_event_chain() RETURNS trigger
        LANGUAGE plpgsql
        SET search_path = pg_catalog, {_SCHEMA}, public
        AS $$
        DECLARE
            last_hash bytea;
        BEGIN
            PERFORM pg_advisory_xact_lock({_CHAIN_LOCK_KEY});

            SELECT row_hash INTO last_hash
              FROM {_SCHEMA}.strategy_lifecycle_events ORDER BY seq DESC LIMIT 1;

            -- The application cannot supply its own hashes: a writer that computes its
            -- own chain values can also compute consistent ones for a forged row.
            NEW.prev_hash := COALESCE(last_hash, '\\x00'::bytea);
            NEW.row_hash  := {_SCHEMA}.lifecycle_event_digest(
                NEW.prev_hash, NEW.seq, NEW.event_id, NEW.strategy_id, NEW.genome_hash,
                NEW.correlation_id, NEW.causation_id, NEW.from_state, NEW.to_state,
                NEW.reason_class, NEW.reason, NEW.survival_score, NEW.score_components,
                NEW.independent_episode_count, NEW.forward_independent_episode_count,
                NEW.global_trial_index, NEW.family_trial_index, NEW.scoring_version,
                NEW.occurred_at_utc);
            RETURN NEW;
        END;
        $$
        """
    )
    op.execute(
        f"CREATE TRIGGER lifecycle_event_chain_before_insert "
        f"BEFORE INSERT ON {_SCHEMA}.strategy_lifecycle_events "
        f"FOR EACH ROW EXECUTE FUNCTION {_SCHEMA}.lifecycle_event_chain()"
    )

    # Re-derives every digest rather than only comparing links: a rewrite that also
    # recomputed the chain forward would pass a link check.
    op.execute(
        f"""
        CREATE FUNCTION {_SCHEMA}.verify_lifecycle_chain(p_since_seq bigint DEFAULT 0)
        RETURNS TABLE (checked_rows bigint, first_broken_seq bigint, reason text)
        LANGUAGE sql
        STABLE
        SET search_path = pg_catalog, {_SCHEMA}, public
        AS $$
            WITH derived AS (
                SELECT e.seq,
                       e.prev_hash,
                       e.row_hash,
                       lag(e.row_hash) OVER (ORDER BY e.seq) AS predecessor,
                       {_SCHEMA}.lifecycle_event_digest(
                           e.prev_hash, e.seq, e.event_id, e.strategy_id, e.genome_hash,
                           e.correlation_id, e.causation_id, e.from_state, e.to_state,
                           e.reason_class, e.reason, e.survival_score, e.score_components,
                           e.independent_episode_count, e.forward_independent_episode_count,
                           e.global_trial_index, e.family_trial_index, e.scoring_version,
                           e.occurred_at_utc) AS recomputed
                  FROM {_SCHEMA}.strategy_lifecycle_events e
                 WHERE e.seq > p_since_seq
            ),
            broken AS (
                SELECT d.seq,
                       CASE WHEN d.recomputed <> d.row_hash
                                 THEN 'row_hash does not match the row contents'
                            ELSE 'prev_hash does not match its predecessor' END AS reason
                  FROM derived d
                 WHERE d.recomputed <> d.row_hash
                    OR (d.predecessor IS NOT NULL AND d.prev_hash <> d.predecessor)
                 ORDER BY d.seq
                 LIMIT 1
            )
            SELECT (SELECT count(*) FROM derived)::bigint,
                   (SELECT b.seq FROM broken b),
                   (SELECT b.reason FROM broken b);
        $$
        """
    )

    # -- immutability and grants -------------------------------------------------------
    for table in _TABLES:
        # The shared guard from 0002. One message, one place to harden, and the same
        # `<table> is append-only: <op> is forbidden` string a caller matches on.
        op.execute(
            f"CREATE TRIGGER {table}_no_update_delete "
            f"BEFORE UPDATE OR DELETE ON {_SCHEMA}.{table} "
            f"FOR EACH ROW EXECUTE FUNCTION public.fking_append_only_guard()"
        )
        op.execute(f"ALTER TABLE {_SCHEMA}.{table} OWNER TO {_MIGRATOR}")
        op.execute(f"REVOKE ALL ON {_SCHEMA}.{table} FROM PUBLIC")
        op.execute(f"REVOKE ALL ON {_SCHEMA}.{table} FROM {_APP}")
        op.execute(f"REVOKE ALL ON {_SCHEMA}.{table} FROM {_INGEST}")
        # TRUNCATE fires no row trigger, so the revoke above is the primary control and
        # the trigger is the backstop, not the other way round.
        op.execute(f"GRANT SELECT, INSERT ON {_SCHEMA}.{table} TO {_APP}")

    op.execute(f"GRANT USAGE ON SCHEMA {_SCHEMA} TO {_APP}")

    digest_signature = (
        "lifecycle_event_digest(bytea, bigint, uuid, text, bytea, uuid, uuid, text, text,"
        " text, text, numeric, jsonb, integer, integer, bigint, bigint, text, timestamptz)"
    )
    verify_signature = "verify_lifecycle_chain(bigint)"
    for signature in (digest_signature, "lifecycle_event_chain()", verify_signature):
        op.execute(f"ALTER FUNCTION {_SCHEMA}.{signature} OWNER TO {_MIGRATOR}")
        # EXECUTE on a function is granted to PUBLIC by default, and PUBLIC is every role
        # this cluster will ever have -- including the ingestion role, which has no
        # business reading the population's history.
        op.execute(f"REVOKE ALL ON FUNCTION {_SCHEMA}.{signature} FROM PUBLIC")

    # The verifier runs on the system beat and on every deploy; a break in the chain is a
    # live risk incident rather than a data-quality ticket. It is SECURITY INVOKER, so the
    # caller needs the digest function too -- and so does the BEFORE INSERT trigger, which
    # is likewise invoker-rights. Granting the digest is therefore what makes an ordinary
    # append work at all, not a convenience.
    op.execute(f"GRANT EXECUTE ON FUNCTION {_SCHEMA}.{verify_signature} TO {_APP}")
    op.execute(f"GRANT EXECUTE ON FUNCTION {_SCHEMA}.{digest_signature} TO {_APP}")

    # The standing instruction that survives the next migration -- the one granting a
    # broad role to a new service because that was the fast way to unblock a deploy.
    op.execute(
        f"ALTER DEFAULT PRIVILEGES FOR ROLE {_MIGRATOR} IN SCHEMA {_SCHEMA} "
        f"REVOKE UPDATE, DELETE, TRUNCATE ON TABLES FROM {_APP}"
    )
    op.execute(
        f"ALTER DEFAULT PRIVILEGES FOR ROLE {_MIGRATOR} IN SCHEMA {_SCHEMA} "
        f"REVOKE ALL ON TABLES FROM PUBLIC"
    )


def downgrade() -> None:
    raise RuntimeError(
        "0015_evolution_lineage_store is irreversible. Dropping the evolution schema "
        "destroys the genealogy every quarantine sweep walks and the lifecycle event "
        "stream every promotion is reconstructed from, and neither is recoverable from "
        "a later insert: a genome hash can be recomputed, but the trial index it was "
        "created at and the score that promoted it cannot. Roll forward with a new "
        "migration; if the intent is to discard a local development database, drop the "
        "database rather than the record."
    )
