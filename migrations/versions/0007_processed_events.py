"""The consumer deduplication table that makes at-least-once delivery survivable.

Revision ID: 0007_processed_events
Revises: 0006_agents

Reversible, and deliberately so. This is not an audit table: it holds no decision and no
history, only the claim that a given consumer group has already applied a given event. It
is derived state, rebuildable in the sense that dropping it degrades the system to
"every event may be applied twice" rather than to "the record of a trade is gone" -- which
is the line `.claude/rules/append-only-audit.md` draws when it makes audit migrations
irreversible.

Two properties of the DDL are load-bearing:

`PRIMARY KEY (consumer_group, idempotency_key)` is the conflict arbiter for the claim
statement. `INSERT ... ON CONFLICT (consumer_group, idempotency_key) DO NOTHING` has no
arbiter without it and errors at runtime, on the first duplicate delivery, which is
precisely the path that must not fail.

The application role holds `DELETE` here, unlike on every audit table. Retention pruning
is a real operation on this table -- a consumer cannot deduplicate against rows it has
pruned, so the window has to be managed -- and there is nothing here worth protecting
from a rewrite: the worst a forged row achieves is skipping one event, which
reconciliation against the exchange corrects.
"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op

revision: str = "0007_processed_events"
down_revision: str | None = "0006_agents"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.execute(
        """
        CREATE TABLE processed_events (
            consumer_group  TEXT        NOT NULL,
            idempotency_key TEXT        NOT NULL,
            stream          TEXT        NOT NULL,
            message_id      TEXT        NOT NULL,
            recorded_at_utc TIMESTAMPTZ NOT NULL DEFAULT clock_timestamp(),
            CONSTRAINT pk_processed_events PRIMARY KEY (consumer_group, idempotency_key)
        )
        """
    )
    op.execute(
        "CREATE INDEX ix_processed_events_recorded_at_utc ON processed_events (recorded_at_utc)"
    )

    op.execute("REVOKE ALL ON processed_events FROM PUBLIC")
    op.execute("GRANT SELECT, INSERT, DELETE ON processed_events TO fking_app")


def downgrade() -> None:
    op.execute("DROP TABLE IF EXISTS processed_events")
