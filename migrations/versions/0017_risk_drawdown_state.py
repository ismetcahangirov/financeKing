"""The drawdown state a restart must not be allowed to recompute.

Revision ID: 0017_risk_drawdown_state
Revises: 0016_evolution_derived_reads

`#52` puts the high-water mark, the 00:00 UTC day anchor and the trailing-24h window
under three limits. All three are budgets measured against a *remembered* number, and
the failure this table exists to prevent is that the number is not remembered.

**Why the schema, and not just the code.** Equity peaked at 100, the drawdown budget is
20%, current equity is 85. The process restarts. If the high-water mark initialises from
current equity the budget is now 20% below 85 -- the system has quietly granted itself
another 15% of drawdown at exactly the moment the evidence says it should have less. No
log line is wrong and the dashboard reads `drawdown: 0.0%`. `fking.risk.drawdown` refuses
to derive a peak from current equity (`restore` has nine keyword arguments and no
defaults), and this is where the values it refuses to invent are kept.

`ck_risk_drawdown_state_peak_is_at_least_current` restates that invariant in the
database. It is deliberately redundant with `DrawdownState.__post_init__`: the type
guards the process that is running now, and the constraint guards the row against every
writer this schema will ever have, including a repair script run by hand during an
incident -- which is precisely when somebody would be tempted to "fix" a peak downward.

**The trailing window is a child table, not a JSON column.** Every mark carries an equity
figure, and an equity figure in a `jsonb` blob is invisible to the
`information_schema` scan that asserts no money column is `DOUBLE PRECISION`
(`.claude/rules/decimal-and-money.md`). `NUMERIC(38, 18)` per mark is what makes that
scan able to see them. The surrogate `state_id` rather than a composite
`(scope, subject_id)` foreign key is not decoration either: an unindexed foreign-key
column makes every parent delete a sequential scan of the child, and the composite form
leaves `subject_id` uncovered by any leading index.

**Deliberately not append-only.** `.claude/rules/append-only-audit.md` governs the tables
a trade is reconstructed from; this is live operational state whose whole purpose is to
be updated on every fill and every mark. What a breach *was* is recorded in
`limit_breach` and `audit_log`, both of which are append-only. What this table promises
is narrower: the row survives the process.

`downgrade` drops both tables, and that is a real loss rather than a formality -- the
high-water mark is not derivable from anything else here, because the equity series it
was computed from is a stream of marks, not a stored history. It is still reversible in
the sense the release drill requires (nothing references these rows), so it drops rather
than refusing; recovery after a downgrade is a reconciliation against the venue, which
`ARCHITECTURE.md` section 7 makes the source of truth anyway.
"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op

revision: str = "0017_risk_drawdown_state"
down_revision: str | None = "0016_evolution_derived_reads"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_APP: str = "fking_app"
_INGEST: str = "fking_ingest"
_MIGRATOR: str = "fking_migrator"
_STATE: str = "risk_drawdown_state"
_MARK: str = "risk_drawdown_mark"

# Mirrors fking.risk.drawdown.Scope and fking.risk.drawdown.LimitName, asserted equal to
# them in tests/platform/persistence/test_schema_contract.py.
_SCOPES: str = "'strategy', 'portfolio'"
_LIMIT_NAMES: str = "'drawdown', 'daily_loss', 'rolling_loss'"

# `date_trunc('day', timestamptz)` truncates in the *session* time zone, so the same row
# would satisfy this constraint on a UTC connection and violate it on a connection whose
# TimeZone was set to anything else. Converting to a naive timestamp at an explicit UTC
# offset and back removes the session from the expression entirely.
_ON_A_UTC_DAY_BOUNDARY: str = (
    "day_start_utc = date_trunc('day', day_start_utc AT TIME ZONE 'UTC') AT TIME ZONE 'UTC'"
)

# All five breach columns are present together or absent together. A half-written breach
# is a subject that reads as halted with no threshold to explain it, or as trading with a
# recorded breach nobody acts on -- and the second one is the dangerous direction.
_BREACH_IS_WHOLE: str = (
    "num_nonnulls(breach_limit_name, breach_observed_ratio, breach_budget_ratio, "
    "breached_at_utc) IN (0, 4)"
)


def upgrade() -> None:
    op.execute(
        f"""
        CREATE TABLE {_STATE} (
            state_id              UUID            NOT NULL,
            scope                 TEXT            NOT NULL,
            subject_id            TEXT            NOT NULL,
            peak_equity_usd       NUMERIC(38, 18) NOT NULL,
            current_equity_usd    NUMERIC(38, 18) NOT NULL,
            day_start_utc         TIMESTAMPTZ     NOT NULL,
            day_open_equity_usd   NUMERIC(38, 18) NOT NULL,
            observed_at_utc       TIMESTAMPTZ     NOT NULL,
            breach_limit_name     TEXT,
            breach_observed_ratio NUMERIC(38, 18),
            breach_budget_ratio   NUMERIC(38, 18),
            breached_at_utc       TIMESTAMPTZ,
            recorded_at_utc       TIMESTAMPTZ     NOT NULL DEFAULT clock_timestamp(),
            CONSTRAINT pk_{_STATE} PRIMARY KEY (state_id),
            CONSTRAINT uq_{_STATE}_scope_subject_id UNIQUE (scope, subject_id),
            CONSTRAINT ck_{_STATE}_scope_is_known CHECK (scope IN ({_SCOPES})),
            CONSTRAINT ck_{_STATE}_subject_id_is_not_blank
                CHECK (btrim(subject_id) <> ''),
            -- Every ratio in this module divides by an equity figure, so zero equity
            -- does not make a drawdown smaller -- it makes it undefined.
            CONSTRAINT ck_{_STATE}_equity_is_positive
                CHECK (peak_equity_usd > 0 AND current_equity_usd > 0
                       AND day_open_equity_usd > 0),
            CONSTRAINT ck_{_STATE}_peak_is_at_least_current
                CHECK (peak_equity_usd >= current_equity_usd),
            CONSTRAINT ck_{_STATE}_day_start_is_a_utc_boundary
                CHECK ({_ON_A_UTC_DAY_BOUNDARY}),
            CONSTRAINT ck_{_STATE}_day_start_precedes_the_observation
                CHECK (day_start_utc <= observed_at_utc),
            CONSTRAINT ck_{_STATE}_breach_is_whole CHECK ({_BREACH_IS_WHOLE}),
            CONSTRAINT ck_{_STATE}_breach_limit_name_is_known
                CHECK (breach_limit_name IS NULL OR breach_limit_name IN ({_LIMIT_NAMES})),
            CONSTRAINT ck_{_STATE}_breach_ratios_are_fractions
                CHECK ((breach_observed_ratio IS NULL OR breach_observed_ratio >= 0)
                       AND (breach_budget_ratio IS NULL OR breach_budget_ratio > 0))
        )
        """
    )

    op.execute(
        f"""
        CREATE TABLE {_MARK} (
            state_id        UUID            NOT NULL,
            observed_at_utc TIMESTAMPTZ     NOT NULL,
            equity_usd      NUMERIC(38, 18) NOT NULL,
            recorded_at_utc TIMESTAMPTZ     NOT NULL DEFAULT clock_timestamp(),
            CONSTRAINT pk_{_MARK} PRIMARY KEY (state_id, observed_at_utc),
            -- CASCADE rather than RESTRICT: a window without its state row is a set of
            -- equity readings nothing can interpret, and leaving it behind would make a
            -- later re-open of the same subject inherit a stranger's window.
            CONSTRAINT fk_{_MARK}_state_id_{_STATE}
                FOREIGN KEY (state_id) REFERENCES {_STATE} (state_id) ON DELETE CASCADE,
            CONSTRAINT ck_{_MARK}_equity_is_positive CHECK (equity_usd > 0)
        )
        """
    )

    # APP_MUTABLE in fking.platform.persistence.privileges. The application owns both
    # tables outright; ingestion has no business in either.
    #
    # Ownership is the group role rather than the login role the migration connected as,
    # so the objects move with the privilege class and not with a credential --
    # `test_every_table_is_owned_by_the_migrator_role` asserts that over the whole
    # catalogue.
    for table in (_STATE, _MARK):
        op.execute(f"ALTER TABLE {table} OWNER TO {_MIGRATOR}")
        op.execute(f"REVOKE ALL ON {table} FROM PUBLIC")
        op.execute(f"REVOKE ALL ON {table} FROM {_APP}")
        op.execute(f"REVOKE ALL ON {table} FROM {_INGEST}")
        op.execute(f"GRANT SELECT, INSERT, UPDATE, DELETE ON {table} TO {_APP}")


def downgrade() -> None:
    op.execute(f"DROP TABLE {_MARK}")
    op.execute(f"DROP TABLE {_STATE}")
