"""The order path and the risk record it passed through.

Revision ID: 0005_execution_and_risk
Revises: 0004_strategy_and_evolution

Reversible. Exchange state is the source of truth for orders, fills and balances
(`ARCHITECTURE.md` section 7), so these tables are rebuildable by reconciliation -- which
is exercised regularly rather than theoretically, because Binance spot testnet is wiped
roughly every 30 days with the API keys left intact.

`order` is quoted because it is a reserved word. `trade_order` or `order_record` would
avoid the quoting and would be visible in every query for the lifetime of the schema.

Five of the seven tables here are append-only. `order` is not, because an order's status
genuinely changes -- and the `venue_seq` column is what stops a redelivered `submitted`
event arriving after `filled` from resurrecting it.
"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op

revision: str = "0005_execution_and_risk"
down_revision: str | None = "0004_strategy_and_evolution"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_APPEND_ONLY = (
    "fill",
    "position_snapshot",
    "account_snapshot",
    "risk_decision",
    "limit_breach",
    "kill_switch_event",
)


def upgrade() -> None:
    op.execute(
        """
        CREATE TABLE "order" (
            order_id             UUID            NOT NULL,
            client_order_id      TEXT            NOT NULL,
            correlation_id       UUID            NOT NULL,
            instrument_id        UUID            NOT NULL,
            strategy_version_id  UUID,
            side                 TEXT            NOT NULL,
            order_type           TEXT            NOT NULL,
            time_in_force        TEXT            NOT NULL,
            status               TEXT            NOT NULL,
            base_quantity        NUMERIC(38, 18) NOT NULL,
            limit_quote_price    NUMERIC(38, 18),
            decision_quote_price NUMERIC(38, 18) NOT NULL,
            venue_order_id       TEXT,
            venue_seq            BIGINT,
            created_at_utc       TIMESTAMPTZ     NOT NULL,
            submitted_at_utc     TIMESTAMPTZ,
            terminal_at_utc      TIMESTAMPTZ,
            recorded_at_utc      TIMESTAMPTZ     DEFAULT clock_timestamp() NOT NULL,
            CONSTRAINT pk_order PRIMARY KEY (order_id),
            -- The exchange-side idempotency key. UNIQUE here so a duplicate placement
            -- fails locally before it reaches the venue.
            CONSTRAINT uq_order_client_order_id UNIQUE (client_order_id),
            CONSTRAINT ck_order_side_is_known CHECK (side IN ('buy', 'sell')),
            CONSTRAINT ck_order_order_type_is_known CHECK (order_type IN ('market', 'limit')),
            CONSTRAINT ck_order_time_in_force_is_known
                CHECK (time_in_force IN ('gtc', 'ioc', 'fok')),
            CONSTRAINT ck_order_status_is_known
                CHECK (status IN ('pending', 'submitted', 'partially_filled', 'filled',
                                  'cancelled', 'rejected', 'expired')),
            CONSTRAINT ck_order_base_quantity_is_positive CHECK (base_quantity > 0),
            CONSTRAINT ck_order_decision_quote_price_is_positive
                CHECK (decision_quote_price > 0),
            -- A market order carrying a price is one somebody meant to send as a limit:
            -- the venue ignores the field and fills at whatever the book offers.
            CONSTRAINT ck_order_limit_price_matches_order_type
                CHECK ((order_type = 'limit' AND limit_quote_price IS NOT NULL
                        AND limit_quote_price > 0)
                       OR (order_type = 'market' AND limit_quote_price IS NULL)),
            CONSTRAINT fk_order_instrument_id_instrument
                FOREIGN KEY (instrument_id) REFERENCES instrument (instrument_id),
            CONSTRAINT fk_order_strategy_version_id_strategy_version
                FOREIGN KEY (strategy_version_id)
                REFERENCES strategy_version (strategy_version_id)
        )
        """
    )
    op.execute('CREATE INDEX ix_order_correlation_id ON "order" (correlation_id)')
    op.execute('CREATE INDEX ix_order_instrument_id ON "order" (instrument_id)')
    op.execute('CREATE INDEX ix_order_strategy_version_id ON "order" (strategy_version_id)')

    op.execute(
        """
        CREATE TABLE fill (
            fill_id            UUID            NOT NULL,
            order_id           UUID            NOT NULL,
            venue_trade_id     TEXT            NOT NULL,
            instrument_id      UUID            NOT NULL,
            side               TEXT            NOT NULL,
            event_time_utc     TIMESTAMPTZ     NOT NULL,
            quote_price        NUMERIC(38, 18) NOT NULL,
            base_quantity      NUMERIC(38, 18) NOT NULL,
            fee_quote          NUMERIC(38, 18) NOT NULL,
            realised_pnl_quote NUMERIC(38, 18) NOT NULL,
            slippage_bp        NUMERIC(38, 18) NOT NULL,
            recorded_at_utc    TIMESTAMPTZ     DEFAULT clock_timestamp() NOT NULL,
            CONSTRAINT pk_fill PRIMARY KEY (fill_id),
            -- The venue's trade id is the only identifier both sides of a reconciliation
            -- agree on, so it is what deduplicates an at-least-once redelivery. Without
            -- this a redelivered fill doubles the position, and the reconciler then
            -- reports the divergence as an exchange bug.
            CONSTRAINT uq_fill_order_id_venue_trade_id UNIQUE (order_id, venue_trade_id),
            CONSTRAINT ck_fill_side_is_known CHECK (side IN ('buy', 'sell')),
            CONSTRAINT ck_fill_quote_price_is_positive CHECK (quote_price > 0),
            CONSTRAINT ck_fill_base_quantity_is_positive CHECK (base_quantity > 0),
            CONSTRAINT ck_fill_fee_quote_is_not_negative CHECK (fee_quote >= 0),
            CONSTRAINT fk_fill_order_id_order FOREIGN KEY (order_id) REFERENCES "order" (order_id),
            CONSTRAINT fk_fill_instrument_id_instrument
                FOREIGN KEY (instrument_id) REFERENCES instrument (instrument_id)
        )
        """
    )
    op.execute(
        "CREATE INDEX ix_fill_instrument_id_event_time_utc ON fill (instrument_id, event_time_utc)"
    )

    op.execute(
        """
        CREATE TABLE position_snapshot (
            snapshot_id               UUID            NOT NULL,
            instrument_id             UUID            NOT NULL,
            observed_at_utc           TIMESTAMPTZ     NOT NULL,
            source                    TEXT            NOT NULL,
            direction                 TEXT            NOT NULL,
            base_quantity             NUMERIC(38, 18) NOT NULL,
            average_entry_quote_price NUMERIC(38, 18) NOT NULL,
            unrealised_pnl_quote      NUMERIC(38, 18) NOT NULL,
            realised_pnl_quote        NUMERIC(38, 18) NOT NULL,
            recorded_at_utc           TIMESTAMPTZ     DEFAULT clock_timestamp() NOT NULL,
            CONSTRAINT pk_position_snapshot PRIMARY KEY (snapshot_id),
            CONSTRAINT ck_position_snapshot_source_is_known CHECK (source IN ('local', 'venue')),
            CONSTRAINT ck_position_snapshot_direction_is_known
                CHECK (direction IN ('long', 'short', 'flat')),
            -- Flat is exactly zero and is a distinct state, never a tiny long. A
            -- residual dust quantity that reads as flat is what later fails a LOT_SIZE
            -- filter with -1013 on a value that prints as if it were correct.
            CONSTRAINT ck_position_snapshot_flat_is_exactly_zero
                CHECK ((direction = 'flat') = (base_quantity = 0)),
            CONSTRAINT ck_position_snapshot_base_quantity_is_unsigned CHECK (base_quantity >= 0),
            CONSTRAINT fk_position_snapshot_instrument_id_instrument
                FOREIGN KEY (instrument_id) REFERENCES instrument (instrument_id)
        )
        """
    )
    op.execute(
        "CREATE INDEX ix_position_snapshot_instrument_id_observed_at_utc "
        "ON position_snapshot (instrument_id, observed_at_utc)"
    )

    op.execute(
        """
        CREATE TABLE account_snapshot (
            snapshot_id        UUID            NOT NULL,
            venue_id           TEXT            NOT NULL,
            observed_at_utc    TIMESTAMPTZ     NOT NULL,
            source             TEXT            NOT NULL,
            equity_usd         NUMERIC(38, 18) NOT NULL,
            free_balance_usd   NUMERIC(38, 18) NOT NULL,
            margin_balance_usd NUMERIC(38, 18) NOT NULL,
            recorded_at_utc    TIMESTAMPTZ     DEFAULT clock_timestamp() NOT NULL,
            CONSTRAINT pk_account_snapshot PRIMARY KEY (snapshot_id),
            CONSTRAINT ck_account_snapshot_source_is_known CHECK (source IN ('local', 'venue')),
            CONSTRAINT fk_account_snapshot_venue_id_venue
                FOREIGN KEY (venue_id) REFERENCES venue (venue_id)
        )
        """
    )
    op.execute(
        "CREATE INDEX ix_account_snapshot_venue_id_observed_at_utc "
        "ON account_snapshot (venue_id, observed_at_utc)"
    )

    op.execute(
        """
        CREATE TABLE risk_decision (
            decision_id            UUID            NOT NULL,
            correlation_id         UUID            NOT NULL,
            instrument_id          UUID            NOT NULL,
            strategy_version_id    UUID,
            verdict                TEXT            NOT NULL,
            order_id               UUID,
            rejection_reason       TEXT,
            conviction             NUMERIC(38, 18) NOT NULL,
            portfolio_notional_usd NUMERIC(38, 18) NOT NULL,
            decided_at_utc         TIMESTAMPTZ     NOT NULL,
            recorded_at_utc        TIMESTAMPTZ     DEFAULT clock_timestamp() NOT NULL,
            CONSTRAINT pk_risk_decision PRIMARY KEY (decision_id),
            CONSTRAINT ck_risk_decision_verdict_is_known
                CHECK (verdict IN ('approved', 'rejected')),
            -- An approved decision with no order is one nothing can act on; a rejected
            -- decision carrying an order is one somebody downstream acts on anyway,
            -- because the order is right there and the verdict is only a string.
            CONSTRAINT ck_risk_decision_verdict_governs_order_and_reason
                CHECK ((verdict = 'approved' AND order_id IS NOT NULL
                        AND rejection_reason IS NULL)
                       OR (verdict = 'rejected' AND order_id IS NULL
                           AND rejection_reason IS NOT NULL)),
            CONSTRAINT ck_risk_decision_conviction_is_a_fraction
                CHECK (conviction BETWEEN 0 AND 1),
            CONSTRAINT fk_risk_decision_instrument_id_instrument
                FOREIGN KEY (instrument_id) REFERENCES instrument (instrument_id),
            CONSTRAINT fk_risk_decision_strategy_version_id_strategy_version
                FOREIGN KEY (strategy_version_id)
                REFERENCES strategy_version (strategy_version_id),
            CONSTRAINT fk_risk_decision_order_id_order
                FOREIGN KEY (order_id) REFERENCES "order" (order_id)
        )
        """
    )
    for column in ("correlation_id", "instrument_id", "order_id", "strategy_version_id"):
        op.execute(f"CREATE INDEX ix_risk_decision_{column} ON risk_decision ({column})")

    op.execute(
        """
        CREATE TABLE limit_breach (
            breach_id         UUID            NOT NULL,
            decision_id       UUID,
            correlation_id    UUID            NOT NULL,
            limit_name        TEXT            NOT NULL,
            limit_unit        TEXT            NOT NULL,
            threshold_in_unit NUMERIC(38, 18) NOT NULL,
            observed_in_unit  NUMERIC(38, 18) NOT NULL,
            breached_at_utc   TIMESTAMPTZ     NOT NULL,
            recorded_at_utc   TIMESTAMPTZ     DEFAULT clock_timestamp() NOT NULL,
            CONSTRAINT pk_limit_breach PRIMARY KEY (breach_id),
            -- The unit is a column because the limits are heterogeneous -- USD
            -- notionals, ratios, counts per minute -- and two numbers with no stated
            -- unit are two numbers somebody will compare.
            CONSTRAINT ck_limit_breach_limit_unit_is_known
                CHECK (limit_unit IN ('usd', 'ratio', 'count', 'per_minute')),
            CONSTRAINT fk_limit_breach_decision_id_risk_decision
                FOREIGN KEY (decision_id) REFERENCES risk_decision (decision_id)
        )
        """
    )
    op.execute("CREATE INDEX ix_limit_breach_correlation_id ON limit_breach (correlation_id)")
    op.execute("CREATE INDEX ix_limit_breach_decision_id ON limit_breach (decision_id)")

    op.execute(
        """
        CREATE TABLE kill_switch_event (
            event_id        UUID        NOT NULL,
            event_type      TEXT        NOT NULL,
            reason          TEXT        NOT NULL,
            actor           TEXT        NOT NULL,
            correlation_id  UUID        NOT NULL,
            occurred_at_utc TIMESTAMPTZ NOT NULL,
            recorded_at_utc TIMESTAMPTZ DEFAULT clock_timestamp() NOT NULL,
            CONSTRAINT pk_kill_switch_event PRIMARY KEY (event_id),
            -- Two rows rather than one row updated on clear. A `cleared_at_utc` column
            -- would be an UPDATE on an append-only table, and the workaround for that is
            -- always to make the table not append-only.
            CONSTRAINT ck_kill_switch_event_event_type_is_known
                CHECK (event_type IN ('tripped', 'cleared'))
        )
        """
    )
    op.execute(
        "CREATE INDEX ix_kill_switch_event_occurred_at_utc ON kill_switch_event (occurred_at_utc)"
    )

    for table in _APPEND_ONLY:
        op.execute(f"REVOKE ALL ON {table} FROM PUBLIC")
        op.execute(f"REVOKE ALL ON {table} FROM fking_app")
        op.execute(f"GRANT INSERT, SELECT ON {table} TO fking_app")
        op.execute(
            f"CREATE TRIGGER {table}_no_update_delete BEFORE UPDATE OR DELETE ON {table} "
            f"FOR EACH ROW EXECUTE FUNCTION fking_append_only_guard()"
        )

    op.execute('GRANT SELECT, INSERT, UPDATE, DELETE ON "order" TO fking_app')


def downgrade() -> None:
    for table in (
        "kill_switch_event",
        "limit_breach",
        "risk_decision",
        "account_snapshot",
        "position_snapshot",
        "fill",
    ):
        op.execute(f"DROP TABLE IF EXISTS {table}")
    op.execute('DROP TABLE IF EXISTS "order"')
