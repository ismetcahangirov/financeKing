# Rule — Append-Only Audit

## The rule

**An audit log the application can rewrite is not an audit log.** Append-only is enforced by PostgreSQL, never by the ORM, never by convention, never by "we only ever insert".

Every audit table carries all four of these:

1. **Revoked privileges.** `REVOKE UPDATE, DELETE, TRUNCATE ON <table> FROM fking_app`, and `REVOKE ALL ... FROM PUBLIC`. The application role can `INSERT` and `SELECT`, nothing else.
2. **A `BEFORE UPDATE OR DELETE` row trigger that raises.** Grants can be widened by a later migration that hands a broad role to a new service; the trigger fires regardless of who holds what.
3. **A per-row hash chain.** `prev_hash` and `row_hash`, computed in the database at insert time. Forbidding a rewrite is not the same as detecting one — a superuser, a `pg_dump`/restore, or direct file access can still change history, and the chain is what makes that *visible*.
4. **Irreversible migrations.** `downgrade()` on an audit migration raises. Rolling back a schema that holds the audit trail is a data-destruction operation dressed as a schema operation.

The governing requirement, from `../../ARCHITECTURE.md` §11: **any trade must be fully reconstructable from the audit log alone, months later, with no access to application memory.** If reconstructing a decision requires a variable that existed only in a running process, the audit is incomplete regardless of how many rows it has.

Agent memory obeys the same rule for the same reason: **an agent cannot rewrite its own history to look better** (`../../CLAUDE.md` §10).

## Why

Immutability in the application layer is a statement about the code that exists today. The audit log has to be trustworthy against the code that exists in eighteen months, written by a different session with no memory of this one, plus whatever an LLM agent generates along the way.

The specific failure this closes is not someone maliciously editing a fill. It is:

- A migration adds a `fking_worker` role and grants it `ALL ON ALL TABLES IN SCHEMA public` because that was the fast way to unblock a deploy. Grants alone are now gone; the trigger still holds.
- An `UPDATE audit_log SET payload = ... WHERE seq = ...` written to "fix" a malformed JSON blob during an incident. Both grant and trigger refuse it. The correct fix is a new row that corrects the old one, which is also the fix that leaves evidence.
- A restore from a doctored dump. Neither grant nor trigger sees this — the chain does. Row *n*'s `prev_hash` no longer equals row *n−1*'s `row_hash`, and the verification job flags the exact seq where history diverges.

The hash chain is the part people skip, and it is the part that converts "we believe this is complete" into "we can demonstrate this is complete". Without it, a missing row is indistinguishable from a row that never existed.

**Correlation IDs originate at the top of the flow**, not at the point of logging. A `correlation_id` minted in the audit writer tells you which log lines came from one function call. A `correlation_id` minted when the market-data tick arrived, and carried across every module boundary (`../../ARCHITECTURE.md` §3), tells you which features, which strategy version, which risk decision, which agent reasoning and which fill belong to one trade. That is the difference between a searchable log and a reconstructable one.

## What must be audited

Non-negotiable, one row per event, all carrying the originating `correlation_id`:

| Event class | Row contents that make reconstruction possible |
|---|---|
| **Risk decisions and rejections** | Input `Signal`, portfolio state read, every limit evaluated with its threshold and observed value, the resulting `Order` or the rejection reason. A rejection is an audited decision, not an absence of one. |
| **Orders and fills** | The full outbound payload including `clientOrderId`, the venue's raw response, every fill with price/quantity/fee/`tradeId`, and slippage against the decision price. |
| **Agent prompts and responses** | Verbatim prompt, verbatim response, model id, provider, temperature, prompt/completion token counts, latency, whether the response was cache-served, and the schema validation outcome. Model id matters: the same prompt to `gemini-2.5-flash` and its successor are different experiments. |
| **Strategy lifecycle transitions** | From-state, to-state, the survival score components that drove it, the promotion gate's verdict, the lineage parent. |
| **Trial charges** | Handled by `trial_ledger` (`./overfitting-defences.md`), which is an audit table under this rule. |

The test of sufficiency is not "did we log the event". It is: *given only these rows, can I answer why this order had this size at this moment?* If the answer needs a number that was computed and discarded, that number belongs in the payload.

## Incorrect

```python
# src/fking/platform/persistence/audit.py
from sqlalchemy.orm import Mapped, mapped_column

from fking.platform.persistence.base import Base


class AuditLog(Base):
    __tablename__ = "audit_log"

    id: Mapped[int] = mapped_column(primary_key=True)
    occurred_at: Mapped[datetime]
    event_type: Mapped[str]
    payload: Mapped[dict[str, Any]] = mapped_column(JSONB)


class AuditRepository:
    """Append-only audit log."""

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def append(self, event: AuditEvent) -> None:
        # The only write path. Nothing here ever updates or deletes.
        self._session.add(
            AuditLog(
                occurred_at=event.occurred_at,
                event_type=event.event_type,
                payload=event.payload,
            )
        )
```

What goes wrong at runtime: nothing, for a while. Then an incident happens, and someone opens `psql` and runs `UPDATE audit_log SET payload = payload || '{"corrected": true}' WHERE id = 41822;` to make a dashboard render. It succeeds silently — `fking_app` still holds `UPDATE`, there is no trigger, and the docstring is a comment. Six weeks later a reconciliation discrepancy is traced back through the audit log and the row is wrong, with no record that it was ever changed and no way to recover what it said. Separately, a `pg_restore` from a stale dump silently drops 300 rows; there is nothing in the schema that can notice, because `id` gaps are indistinguishable from rolled-back transactions.

## Correct

```python
# migrations/versions/0009_audit_log_append_only.py
"""audit_log: append-only, hash-chained, monthly partitions.

Revision ID: 0009_audit_log_append_only
Revises: 0008_execution_fills
"""

from __future__ import annotations

from alembic import op

revision: str = "0009_audit_log_append_only"
down_revision: str = "0008_execution_fills"


def upgrade() -> None:
    op.execute("CREATE EXTENSION IF NOT EXISTS pgcrypto")

    # A partitioned table cannot use GENERATED AS IDENTITY on PostgreSQL 16, so
    # the sequence is explicit. seq is the chain order and is global across
    # partitions.
    op.execute("CREATE SEQUENCE audit_log_seq AS bigint INCREMENT BY 1 NO CYCLE")
    op.execute(
        """
        CREATE TABLE audit_log (
            seq            bigint      NOT NULL DEFAULT nextval('audit_log_seq'),
            occurred_at    timestamptz NOT NULL,
            recorded_at    timestamptz NOT NULL DEFAULT clock_timestamp(),
            correlation_id uuid        NOT NULL,
            causation_id   uuid,
            actor          text        NOT NULL,
            event_type     text        NOT NULL,
            subject_id     text        NOT NULL,
            payload        jsonb       NOT NULL,
            prev_hash      bytea       NOT NULL,
            row_hash       bytea       NOT NULL,
            PRIMARY KEY (seq, occurred_at)
        ) PARTITION BY RANGE (occurred_at)
        """
    )
    op.execute(
        """
        CREATE INDEX audit_log_correlation_idx
            ON audit_log (correlation_id, occurred_at)
        """
    )

    op.execute(
        """
        CREATE FUNCTION audit_log_chain() RETURNS trigger
        LANGUAGE plpgsql AS $$
        DECLARE
            last_hash bytea;
        BEGIN
            -- 5510477 is a fixed key reserved for the audit chain. Serialising
            -- audit inserts is deliberate: the chain has no meaning if two
            -- writers can read the same predecessor. Audit write rate is bounded
            -- by decisions per second, which is small here, so the contention is
            -- affordable and the alternative is not.
            PERFORM pg_advisory_xact_lock(5510477);

            SELECT row_hash INTO last_hash
              FROM audit_log
             ORDER BY seq DESC
             LIMIT 1;

            NEW.prev_hash := COALESCE(last_hash, '\\x00'::bytea);

            -- jsonb text output is canonical: keys are stored sorted and
            -- duplicates removed, unlike json. That is what makes the digest
            -- reproducible by the verifier.
            NEW.row_hash := digest(
                NEW.prev_hash
                || convert_to(NEW.seq::text, 'UTF8')
                || convert_to(
                       to_char(NEW.occurred_at AT TIME ZONE 'UTC',
                               'YYYY-MM-DD"T"HH24:MI:SS.US"Z"'), 'UTF8')
                || convert_to(NEW.correlation_id::text, 'UTF8')
                || convert_to(COALESCE(NEW.causation_id::text, ''), 'UTF8')
                || convert_to(NEW.actor, 'UTF8')
                || convert_to(NEW.event_type, 'UTF8')
                || convert_to(NEW.subject_id, 'UTF8')
                || convert_to(NEW.payload::text, 'UTF8'),
                'sha256');
            RETURN NEW;
        END;
        $$
        """
    )
    op.execute(
        """
        CREATE TRIGGER audit_log_chain_before_insert
            BEFORE INSERT ON audit_log
            FOR EACH ROW EXECUTE FUNCTION audit_log_chain()
        """
    )

    op.execute(
        """
        CREATE FUNCTION audit_log_immutable() RETURNS trigger
        LANGUAGE plpgsql AS $$
        BEGIN
            RAISE EXCEPTION
                'audit_log is append-only: % on % is forbidden; write a '
                'correcting row instead', TG_OP, TG_TABLE_NAME
                USING ERRCODE = 'restrict_violation';
        END;
        $$
        """
    )
    # BEFORE ROW triggers on a partitioned parent are cloned onto every
    # partition, including ones attached later. The monthly partition created in
    # 2027 inherits this without anyone remembering to add it.
    op.execute(
        """
        CREATE TRIGGER audit_log_no_update_delete
            BEFORE UPDATE OR DELETE ON audit_log
            FOR EACH ROW EXECUTE FUNCTION audit_log_immutable()
        """
    )

    # TRUNCATE does not fire row triggers at all, which is why the grant is the
    # primary control and the trigger is the backstop, not the other way round.
    op.execute("REVOKE ALL ON audit_log FROM PUBLIC")
    op.execute("REVOKE UPDATE, DELETE, TRUNCATE ON audit_log FROM fking_app")
    op.execute("GRANT INSERT, SELECT ON audit_log TO fking_app")
    op.execute("GRANT USAGE ON SEQUENCE audit_log_seq TO fking_app")

    # Future partitions inherit the same posture without a migration each month.
    op.execute(
        """
        ALTER DEFAULT PRIVILEGES IN SCHEMA public
            REVOKE UPDATE, DELETE, TRUNCATE ON TABLES FROM fking_app
        """
    )

    op.execute(
        """
        CREATE TABLE audit_log_2026_08 PARTITION OF audit_log
            FOR VALUES FROM ('2026-08-01 00:00:00+00') TO ('2026-09-01 00:00:00+00')
        """
    )


def downgrade() -> None:
    raise RuntimeError(
        "0009 is irreversible: dropping audit_log destroys the record that every "
        "trade is reconstructed from. Roll forward with a new migration."
    )
```

```python
# src/fking/platform/persistence/audit.py
from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Mapping
from uuid import UUID

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncConnection


@dataclass(frozen=True, slots=True)
class AuditEvent:
    occurred_at: datetime  # tz-aware UTC, rejected at construction otherwise
    correlation_id: UUID  # minted at the top of the flow, never here
    causation_id: UUID | None
    actor: str  # "risk", "execution", "agents.quant", ...
    event_type: str
    subject_id: str
    payload: Mapping[str, Any]


async def append(conn: AsyncConnection, event: AuditEvent) -> int:
    """Insert one audit row inside the caller's transaction and return its seq.

    There is no update(), no delete(), and no correct(). A correction is a new
    event whose causation_id points at the row being corrected.
    """
    row = (
        await conn.execute(
            text(
                """
                INSERT INTO audit_log (
                    occurred_at, correlation_id, causation_id, actor,
                    event_type, subject_id, payload, prev_hash, row_hash
                )
                VALUES (
                    :occurred_at, :correlation_id, :causation_id, :actor,
                    :event_type, :subject_id, cast(:payload as jsonb),
                    '\\x00'::bytea, '\\x00'::bytea
                )
                RETURNING seq
                """
            ),
            {
                "occurred_at": event.occurred_at,
                "correlation_id": event.correlation_id,
                "causation_id": event.causation_id,
                "actor": event.actor,
                "event_type": event.event_type,
                "subject_id": event.subject_id,
                "payload": json.dumps(event.payload, sort_keys=True, default=str),
            },
        )
    ).first()
    if row is None:  # pragma: no cover - RETURNING always yields on success
        raise RuntimeError("audit_log INSERT returned no seq")
    return int(row[0])
```

The `'\x00'` placeholders for `prev_hash` and `row_hash` are overwritten by the `BEFORE INSERT` trigger. The application cannot supply its own hashes, which is the point: a writer that computes its own chain values can also compute consistent ones for a forged row.

## Enforcement

**Tests** (`tests/platform/persistence/test_audit_log.py`, against real Postgres — mocking the database here would prove the mock is append-only):

```python
import pytest
from sqlalchemy import text
from sqlalchemy.exc import DBAPIError


async def test_update_raises(conn, appended_seq: int) -> None:
    with pytest.raises(DBAPIError, match="append-only"):
        await conn.execute(
            text("UPDATE audit_log SET actor = 'x' WHERE seq = :s"),
            {"s": appended_seq},
        )


async def test_delete_raises(conn, appended_seq: int) -> None:
    with pytest.raises(DBAPIError, match="append-only"):
        await conn.execute(
            text("DELETE FROM audit_log WHERE seq = :s"), {"s": appended_seq}
        )


async def test_app_role_holds_no_update_or_delete_privilege(conn) -> None:
    row = (
        await conn.execute(
            text(
                """
                SELECT has_table_privilege('fking_app', 'audit_log', 'UPDATE') AS may_update,
                       has_table_privilege('fking_app', 'audit_log', 'DELETE') AS may_delete,
                       has_table_privilege('fking_app', 'audit_log', 'INSERT') AS may_insert
                """
            )
        )
    ).one()
    assert (row.may_update, row.may_delete, row.may_insert) == (False, False, True)


async def test_new_partitions_inherit_the_immutability_trigger(conn) -> None:
    await conn.execute(
        text(
            """
            CREATE TABLE audit_log_2027_01 PARTITION OF audit_log
                FOR VALUES FROM ('2027-01-01+00') TO ('2027-02-01+00')
            """
        )
    )
    triggers = (
        await conn.execute(
            text(
                "SELECT tgname FROM pg_trigger "
                "WHERE tgrelid = 'audit_log_2027_01'::regclass AND NOT tgisinternal"
            )
        )
    ).scalars().all()
    assert "audit_log_no_update_delete" in triggers


async def test_chain_detects_a_superuser_rewrite(conn, superuser_conn) -> None:
    seqs = [await append(conn, event) for event in three_events()]
    await superuser_conn.execute(
        text("ALTER TABLE audit_log DISABLE TRIGGER audit_log_no_update_delete")
    )
    await superuser_conn.execute(
        text("UPDATE audit_log SET payload = '{}'::jsonb WHERE seq = :s"),
        {"s": seqs[1]},
    )
    report = await verify_chain(conn)
    assert report.first_broken_seq == seqs[1]
```

**The chain-verification job** runs on the APScheduler hourly beat and on every deploy, and re-derives each digest rather than only comparing links — a rewrite that also recomputed the chain forward would pass a link check:

```python
# src/fking/platform/persistence/audit_verify.py
@dataclass(frozen=True, slots=True)
class ChainVerification:
    checked_rows: int
    first_broken_seq: int | None
    reason: str | None


async def verify_chain(
    conn: AsyncConnection, *, since_seq: int = 0
) -> ChainVerification:
    """Re-derive every row_hash and compare each prev_hash to its predecessor."""
    rows = (
        await conn.execute(
            text(
                """
                SELECT seq, prev_hash, row_hash,
                       lag(row_hash) OVER (ORDER BY seq) AS predecessor,
                       digest(
                           prev_hash
                           || convert_to(seq::text, 'UTF8')
                           || convert_to(to_char(occurred_at AT TIME ZONE 'UTC',
                              'YYYY-MM-DD"T"HH24:MI:SS.US"Z"'), 'UTF8')
                           || convert_to(correlation_id::text, 'UTF8')
                           || convert_to(COALESCE(causation_id::text, ''), 'UTF8')
                           || convert_to(actor, 'UTF8')
                           || convert_to(event_type, 'UTF8')
                           || convert_to(subject_id, 'UTF8')
                           || convert_to(payload::text, 'UTF8'),
                           'sha256') AS recomputed
                  FROM audit_log
                 WHERE seq > :since
                 ORDER BY seq
                """
            ),
            {"since": since_seq},
        )
    ).all()

    for index, row in enumerate(rows):
        if row.recomputed != row.row_hash:
            return ChainVerification(index, row.seq, "row_hash does not match content")
        if row.predecessor is not None and row.prev_hash != row.predecessor:
            return ChainVerification(index, row.seq, "prev_hash does not match seq-1")
    return ChainVerification(len(rows), None, None)
```

A non-null `first_broken_seq` pages a human immediately and is treated as a live risk incident, not a data-quality ticket.

**Retention.** Rows are never deleted. Partitions older than three months are `DETACH`ed, written to `data/audit/year=<y>/month=<m>/audit.parquet`, verified by re-reading and recomputing the chain segment, and only then dropped from Postgres. The detached segment's terminal `row_hash` stays in a small `audit_chain_anchors` table so the live chain still links back to archived history — otherwise archiving is indistinguishable from truncation.

**`import-linter`** keeps the write path singular:

```toml
[[tool.importlinter.contracts]]
name = "audit rows are written only through the audit module"
type = "forbidden"
source_modules = [
    "fking.agents", "fking.evolution", "fking.execution", "fking.risk",
]
forbidden_modules = ["fking.platform.persistence.audit_tables"]
allow_indirect_imports = true
```

## The one exception

**Schema migrations that add a nullable column to an audit table are allowed. Backfilling values into existing audit rows is not.**

The boundary is exact, and it is the boundary between the table's shape and the table's content:

- **Allowed:** `ALTER TABLE audit_log ADD COLUMN provider_request_id text` with no `DEFAULT` and no `NOT NULL`. Existing rows get `NULL`, which is the truthful record — we did not capture that field when those rows were written, and `NULL` says so. Their `row_hash` is unchanged because the new column is not in the digest input, so the chain still verifies. New rows populate it, and the digest definition is versioned forward with a `chain_version` column so the verifier knows which input recipe applies to which seq range.
- **Forbidden:** `UPDATE audit_log SET provider_request_id = ... WHERE ...`, in any form, including a "one-time backfill" run by a superuser with the trigger disabled, including reconstructing the value from another table where it is unambiguously derivable. It does not matter that the derived value is correct. An audit row that was edited after the fact is an audit row whose contents are a function of when someone last looked at it, and the chain break it causes is indistinguishable from tampering — which means the next real tampering event gets dismissed as "probably that backfill".

The same boundary applies to `ADD COLUMN ... NOT NULL DEFAULT <value>`, which PostgreSQL 16 implements without rewriting the heap but which nevertheless makes existing rows *report* a value they never carried. That is a backfill with better performance characteristics, and it is forbidden for the same reason.

If historical rows genuinely need enrichment, write new rows: one `audit.enrichment` event per enriched subject, with `causation_id` pointing at the original `seq`. The enrichment is then itself auditable, timestamped, and attributable — which is what you wanted from the backfill and could not get from it.
