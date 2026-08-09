"""Strategy identity, versions, lineage, lifecycle, and the evolution record.

Revision ID: 0004_strategy_and_evolution
Revises: 0003_market_data

Reversible, and that needs a word because three of these tables are append-only.
Append-only means the *application* cannot rewrite history; it does not mean a schema
that has never held a row cannot be dropped. The two tables whose loss is unrecoverable
-- the audit log and the trial ledger -- are in 0002, which refuses to downgrade at all.
These are reconstructable from those two plus the strategy definitions, which is the
line this project draws between "inconvenient" and "the record is gone".

`strategy_version` carries `spec_hash` because the trial ledger charges against it. If
the hash changes between registration and test the result is void rather than weak
(`docs/rules/overfitting-defences.md`), and a column that only the ledger could
verify against would leave that check with nothing to compare.
"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op

revision: str = "0004_strategy_and_evolution"
down_revision: str | None = "0003_market_data"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_LIFECYCLE_STATES = (
    "'proposed', 'validating', 'paper', 'challenger', 'champion', 'quarantined', 'retired'"
)


def upgrade() -> None:
    op.execute(
        """
        CREATE TABLE strategy (
            strategy_id     TEXT        NOT NULL,
            family          TEXT        NOT NULL,
            origin          TEXT        NOT NULL,
            created_at_utc  TIMESTAMPTZ NOT NULL,
            recorded_at_utc TIMESTAMPTZ DEFAULT clock_timestamp() NOT NULL,
            CONSTRAINT pk_strategy PRIMARY KEY (strategy_id),
            CONSTRAINT ck_strategy_origin_is_known
                CHECK (origin IN ('human', 'agent', 'mutation', 'crossover'))
        )
        """
    )

    op.execute(
        f"""
        CREATE TABLE strategy_version (
            strategy_version_id        UUID        NOT NULL,
            strategy_id                TEXT        NOT NULL,
            version_number             INTEGER     NOT NULL,
            parameters                 JSONB       NOT NULL,
            genome                     JSONB       NOT NULL,
            parent_strategy_version_id UUID,
            spec_hash                  BYTEA       NOT NULL,
            lifecycle_state            TEXT        NOT NULL,
            created_at_utc             TIMESTAMPTZ NOT NULL,
            recorded_at_utc            TIMESTAMPTZ DEFAULT clock_timestamp() NOT NULL,
            CONSTRAINT pk_strategy_version PRIMARY KEY (strategy_version_id),
            CONSTRAINT uq_strategy_version_strategy_id_version_number
                UNIQUE (strategy_id, version_number),
            CONSTRAINT ck_strategy_version_lifecycle_state_is_known
                CHECK (lifecycle_state IN ({_LIFECYCLE_STATES})),
            CONSTRAINT ck_strategy_version_version_number_is_positive CHECK (version_number >= 1),
            -- Depth one only. A full cycle check needs a recursive query and belongs in
            -- the lineage code; this catches the case a mutation operator actually
            -- produces, which is a strategy declared its own parent.
            CONSTRAINT ck_strategy_version_lineage_is_acyclic_at_depth_one
                CHECK (parent_strategy_version_id <> strategy_version_id),
            CONSTRAINT fk_strategy_version_strategy_id_strategy
                FOREIGN KEY (strategy_id) REFERENCES strategy (strategy_id),
            CONSTRAINT fk_strategy_version_parent_strategy_version_id_strategy_version
                FOREIGN KEY (parent_strategy_version_id)
                REFERENCES strategy_version (strategy_version_id)
        )
        """
    )
    op.execute("CREATE INDEX ix_strategy_version_strategy_id ON strategy_version (strategy_id)")
    op.execute(
        "CREATE INDEX ix_strategy_version_parent_strategy_version_id "
        "ON strategy_version (parent_strategy_version_id)"
    )

    op.execute(
        f"""
        CREATE TABLE strategy_lifecycle_transition (
            transition_id       UUID            NOT NULL,
            strategy_version_id UUID            NOT NULL,
            correlation_id      UUID            NOT NULL,
            from_state          TEXT            NOT NULL,
            to_state            TEXT            NOT NULL,
            reason              TEXT            NOT NULL,
            survival_score      NUMERIC(38, 18),
            decided_at_utc      TIMESTAMPTZ     NOT NULL,
            recorded_at_utc     TIMESTAMPTZ     DEFAULT clock_timestamp() NOT NULL,
            CONSTRAINT pk_strategy_lifecycle_transition PRIMARY KEY (transition_id),
            CONSTRAINT ck_strategy_lifecycle_transition_from_state_is_known
                CHECK (from_state IN ({_LIFECYCLE_STATES})),
            CONSTRAINT ck_strategy_lifecycle_transition_to_state_is_known
                CHECK (to_state IN ({_LIFECYCLE_STATES})),
            CONSTRAINT ck_strategy_lifecycle_transition_transition_moves
                CHECK (from_state <> to_state),
            -- Named explicitly: the project convention would generate 69 characters and
            -- Postgres truncates at 63, so the constraint would be called one thing in
            -- the repository and another in the database.
            CONSTRAINT fk_lifecycle_transition_strategy_version_id
                FOREIGN KEY (strategy_version_id)
                REFERENCES strategy_version (strategy_version_id)
        )
        """
    )
    op.execute(
        "CREATE INDEX ix_strategy_lifecycle_transition_strategy_version_id "
        "ON strategy_lifecycle_transition (strategy_version_id)"
    )
    op.execute(
        "CREATE INDEX ix_strategy_lifecycle_transition_correlation_id "
        "ON strategy_lifecycle_transition (correlation_id)"
    )

    op.execute(
        """
        CREATE TABLE generation (
            generation_id     UUID        NOT NULL,
            generation_number INTEGER     NOT NULL,
            started_at_utc    TIMESTAMPTZ NOT NULL,
            completed_at_utc  TIMESTAMPTZ,
            population_size   INTEGER     NOT NULL,
            recorded_at_utc   TIMESTAMPTZ DEFAULT clock_timestamp() NOT NULL,
            CONSTRAINT pk_generation PRIMARY KEY (generation_id),
            CONSTRAINT uq_generation_generation_number UNIQUE (generation_number),
            CONSTRAINT ck_generation_generation_number_is_not_negative
                CHECK (generation_number >= 0),
            CONSTRAINT ck_generation_population_size_is_not_negative CHECK (population_size >= 0),
            CONSTRAINT ck_generation_generation_is_ordered
                CHECK (completed_at_utc IS NULL OR completed_at_utc >= started_at_utc)
        )
        """
    )

    op.execute(
        """
        CREATE TABLE evaluation (
            evaluation_id                  UUID            NOT NULL,
            strategy_version_id            UUID            NOT NULL,
            generation_id                  UUID,
            window_start_utc               TIMESTAMPTZ     NOT NULL,
            window_end_utc                 TIMESTAMPTZ     NOT NULL,
            is_forward                     BOOLEAN         NOT NULL,
            trade_count                    INTEGER         NOT NULL,
            independent_episode_count      INTEGER         NOT NULL,
            survival_score                 NUMERIC(38, 18) NOT NULL,
            deflated_sharpe                NUMERIC(38, 18) NOT NULL,
            fold_sign_consistency_fraction NUMERIC(38, 18) NOT NULL,
            global_trial_count             BIGINT          NOT NULL,
            evaluated_at_utc               TIMESTAMPTZ     NOT NULL,
            recorded_at_utc                TIMESTAMPTZ     DEFAULT clock_timestamp() NOT NULL,
            CONSTRAINT pk_evaluation PRIMARY KEY (evaluation_id),
            CONSTRAINT ck_evaluation_window_is_ordered CHECK (window_end_utc > window_start_utc),
            CONSTRAINT ck_evaluation_trade_count_is_not_negative CHECK (trade_count >= 0),
            CONSTRAINT ck_evaluation_independent_episode_count_is_not_negative
                CHECK (independent_episode_count >= 0),
            CONSTRAINT ck_evaluation_fold_sign_consistency_fraction_is_a_fraction
                CHECK (fold_sign_consistency_fraction BETWEEN 0 AND 1),
            CONSTRAINT ck_evaluation_survival_score_is_a_fraction
                CHECK (survival_score BETWEEN 0 AND 1),
            -- Zero means the ledger was empty or unreachable. It never means "nothing
            -- was tried, so no deflation was needed", and a row recording a deflated
            -- Sharpe computed against zero trials is a row that flatters by construction.
            CONSTRAINT ck_evaluation_global_trial_count_was_read CHECK (global_trial_count >= 1),
            CONSTRAINT fk_evaluation_strategy_version_id_strategy_version
                FOREIGN KEY (strategy_version_id)
                REFERENCES strategy_version (strategy_version_id),
            CONSTRAINT fk_evaluation_generation_id_generation
                FOREIGN KEY (generation_id) REFERENCES generation (generation_id)
        )
        """
    )
    op.execute("CREATE INDEX ix_evaluation_strategy_version_id ON evaluation (strategy_version_id)")
    op.execute("CREATE INDEX ix_evaluation_generation_id ON evaluation (generation_id)")

    op.execute(
        f"""
        CREATE TABLE promotion (
            promotion_id        UUID            NOT NULL,
            strategy_version_id UUID            NOT NULL,
            -- NOT NULL: a promotion with no evaluation behind it is a decision with no
            -- evidence, and the gate that made it could not be re-derived later.
            evaluation_id       UUID            NOT NULL,
            from_state          TEXT            NOT NULL,
            to_state            TEXT            NOT NULL,
            global_trial_count  BIGINT          NOT NULL,
            deflated_sharpe     NUMERIC(38, 18) NOT NULL,
            decided_at_utc      TIMESTAMPTZ     NOT NULL,
            recorded_at_utc     TIMESTAMPTZ     DEFAULT clock_timestamp() NOT NULL,
            CONSTRAINT pk_promotion PRIMARY KEY (promotion_id),
            CONSTRAINT ck_promotion_from_state_is_known CHECK (from_state IN ({_LIFECYCLE_STATES})),
            CONSTRAINT ck_promotion_to_state_is_known CHECK (to_state IN ({_LIFECYCLE_STATES})),
            CONSTRAINT ck_promotion_global_trial_count_was_read CHECK (global_trial_count >= 1),
            CONSTRAINT fk_promotion_strategy_version_id_strategy_version
                FOREIGN KEY (strategy_version_id)
                REFERENCES strategy_version (strategy_version_id),
            CONSTRAINT fk_promotion_evaluation_id_evaluation
                FOREIGN KEY (evaluation_id) REFERENCES evaluation (evaluation_id)
        )
        """
    )
    op.execute("CREATE INDEX ix_promotion_strategy_version_id ON promotion (strategy_version_id)")
    op.execute("CREATE INDEX ix_promotion_evaluation_id ON promotion (evaluation_id)")

    op.execute(
        """
        CREATE TABLE retirement (
            retirement_id           UUID        NOT NULL,
            strategy_version_id     UUID        NOT NULL,
            reason_class            TEXT        NOT NULL,
            detail                  TEXT        NOT NULL,
            quarantines_descendants BOOLEAN     NOT NULL,
            decided_at_utc          TIMESTAMPTZ NOT NULL,
            recorded_at_utc         TIMESTAMPTZ DEFAULT clock_timestamp() NOT NULL,
            CONSTRAINT pk_retirement PRIMARY KEY (retirement_id),
            CONSTRAINT ck_retirement_reason_class_is_known
                CHECK (reason_class IN ('decayed', 'risk_violation', 'superseded',
                                        'structural_break', 'operator')),
            CONSTRAINT fk_retirement_strategy_version_id_strategy_version
                FOREIGN KEY (strategy_version_id)
                REFERENCES strategy_version (strategy_version_id)
        )
        """
    )
    op.execute("CREATE INDEX ix_retirement_strategy_version_id ON retirement (strategy_version_id)")

    for table in ("strategy_lifecycle_transition", "promotion", "retirement"):
        op.execute(f"REVOKE ALL ON {table} FROM PUBLIC")
        op.execute(f"REVOKE ALL ON {table} FROM fking_app")
        op.execute(f"GRANT INSERT, SELECT ON {table} TO fking_app")
        op.execute(
            f"CREATE TRIGGER {table}_no_update_delete BEFORE UPDATE OR DELETE ON {table} "
            f"FOR EACH ROW EXECUTE FUNCTION fking_append_only_guard()"
        )

    for table in ("strategy", "strategy_version", "generation", "evaluation"):
        op.execute(f"GRANT SELECT, INSERT, UPDATE, DELETE ON {table} TO fking_app")


def downgrade() -> None:
    for table in (
        "retirement",
        "promotion",
        "evaluation",
        "generation",
        "strategy_lifecycle_transition",
        "strategy_version",
        "strategy",
    ):
        op.execute(f"DROP TABLE IF EXISTS {table}")
