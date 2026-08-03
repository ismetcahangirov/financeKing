"""Extensions, database roles, and the reference tables everything else points at.

Revision ID: 0001_extensions_and_reference
Revises:

Reversible. Nothing here holds a decision, a fill or an audit row; reference data is
re-seedable from `python -m fking.platform.persistence`.

The three roles are created with the narrowest thing that makes the append-only
guarantee testable and nothing more. The full least-privilege privilege matrix -- login
roles, per-schema defaults, the ingest/app split on the feature store -- is #106. What
is here is the minimum without which "the application role holds no UPDATE on audit
tables" is a sentence nobody can check, and an unverifiable guarantee is not one.
"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op

revision: str = "0001_extensions_and_reference"
down_revision: str | None = None
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

# NOLOGIN group roles. A role that cannot log in cannot be the role a leaked password
# belongs to, and #106 attaches login roles as members of these.
_ROLES: tuple[str, ...] = ("fking_migrator", "fking_app", "fking_ingest")


def upgrade() -> None:
    # pgcrypto supplies digest() for the audit hash chain in 0002. Created here because
    # CREATE EXTENSION is not transactional on every version and belongs in the
    # migration that has nothing else to lose.
    op.execute("CREATE EXTENSION IF NOT EXISTS pgcrypto")
    op.execute("CREATE EXTENSION IF NOT EXISTS timescaledb")

    for role in _ROLES:
        # IF NOT EXISTS is not available for CREATE ROLE on PostgreSQL 16, and a role
        # is a cluster-level object that may already exist from a previous database on
        # the same server -- so the check is explicit rather than left to an error.
        # `role` iterates a module-level tuple of string literals and CREATE ROLE takes
        # no bind parameters, so there is no non-interpolating spelling of this. See the
        # per-file S608 ignore in pyproject.toml for why that is acceptable here and
        # nowhere else.
        op.execute(
            f"""
            DO $$
            BEGIN
                IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = '{role}') THEN
                    CREATE ROLE {role} NOLOGIN;
                END IF;
            END
            $$
            """
        )

    op.execute(
        """
        CREATE TABLE venue (
            venue_id        TEXT        NOT NULL,
            display_name    TEXT        NOT NULL,
            is_testnet      BOOLEAN     DEFAULT true NOT NULL,
            recorded_at_utc TIMESTAMPTZ DEFAULT clock_timestamp() NOT NULL,
            CONSTRAINT pk_venue PRIMARY KEY (venue_id),
            CONSTRAINT ck_venue_venue_id_is_known
                CHECK (venue_id IN ('binance-spot-testnet', 'binance-futures-testnet',
                                    'bybit-testnet')),
            -- A production venue row is unrepresentable. The compiled-in host allowlist
            -- is what stops a production request; this stops the database from becoming
            -- a second, softer answer to "which venues exist".
            CONSTRAINT ck_venue_venue_is_a_testnet CHECK (is_testnet)
        )
        """
    )

    op.execute(
        """
        CREATE TABLE instrument (
            instrument_id      UUID            NOT NULL,
            venue_id           TEXT            NOT NULL,
            symbol             TEXT            NOT NULL,
            market             TEXT            NOT NULL,
            base_asset         TEXT            NOT NULL,
            quote_asset        TEXT            NOT NULL,
            tick_size          NUMERIC(38, 18) NOT NULL,
            lot_step           NUMERIC(38, 18) NOT NULL,
            min_notional_quote NUMERIC(38, 18) NOT NULL,
            listed_at_utc      TIMESTAMPTZ     NOT NULL,
            delisted_at_utc    TIMESTAMPTZ,
            recorded_at_utc    TIMESTAMPTZ     DEFAULT clock_timestamp() NOT NULL,
            CONSTRAINT pk_instrument PRIMARY KEY (instrument_id),
            CONSTRAINT uq_instrument_venue_id_symbol UNIQUE (venue_id, symbol),
            CONSTRAINT ck_instrument_market_is_known CHECK (market IN ('spot', 'futures_um')),
            CONSTRAINT ck_instrument_tick_size_is_positive CHECK (tick_size > 0),
            CONSTRAINT ck_instrument_lot_step_is_positive CHECK (lot_step > 0),
            CONSTRAINT ck_instrument_min_notional_quote_is_positive CHECK (min_notional_quote > 0),
            CONSTRAINT ck_instrument_assets_differ CHECK (base_asset <> quote_asset),
            CONSTRAINT ck_instrument_delisting_follows_listing
                CHECK (delisted_at_utc IS NULL OR delisted_at_utc > listed_at_utc),
            CONSTRAINT fk_instrument_venue_id_venue
                FOREIGN KEY (venue_id) REFERENCES venue (venue_id)
        )
        """
    )
    op.execute("CREATE INDEX ix_instrument_venue_id ON instrument (venue_id)")

    op.execute(
        """
        CREATE TABLE venue_maintenance_window (
            window_id        UUID        NOT NULL,
            venue_id         TEXT        NOT NULL,
            starts_at_utc    TIMESTAMPTZ NOT NULL,
            ends_at_utc      TIMESTAMPTZ NOT NULL,
            announced_at_utc TIMESTAMPTZ NOT NULL,
            reason           TEXT        NOT NULL,
            recorded_at_utc  TIMESTAMPTZ DEFAULT clock_timestamp() NOT NULL,
            CONSTRAINT pk_venue_maintenance_window PRIMARY KEY (window_id),
            CONSTRAINT ck_venue_maintenance_window_window_is_ordered
                CHECK (ends_at_utc > starts_at_utc),
            CONSTRAINT fk_venue_maintenance_window_venue_id_venue
                FOREIGN KEY (venue_id) REFERENCES venue (venue_id)
        )
        """
    )
    op.execute(
        "CREATE INDEX ix_venue_maintenance_window_venue_id_starts_at_utc "
        "ON venue_maintenance_window (venue_id, starts_at_utc)"
    )

    # Reference data is mutable: instrument filters are reconciled against the venue's
    # own exchangeInfo at startup, so a stale tick size has to be correctable. #106 owns
    # the full privilege matrix; what is here keeps the schema coherent rather than
    # half-granted, which is the state in which nobody can tell an intentional omission
    # from a forgotten line.
    for table in ("venue", "instrument", "venue_maintenance_window"):
        op.execute(f"GRANT SELECT, INSERT, UPDATE, DELETE ON {table} TO fking_app")


def downgrade() -> None:
    op.execute("DROP TABLE IF EXISTS venue_maintenance_window")
    op.execute("DROP TABLE IF EXISTS instrument")
    op.execute("DROP TABLE IF EXISTS venue")
    # Roles and extensions are deliberately left in place. A role is a cluster-level
    # object that another database on the same server may be relying on, and dropping
    # timescaledb would take every hypertable in the cluster with it -- neither is this
    # migration's to remove, and a downgrade that reaches outside its own database is
    # how a rollback becomes an outage.
