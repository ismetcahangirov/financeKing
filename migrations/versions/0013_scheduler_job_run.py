"""What a system beat needs from the schema: a claim per fire time, closed exactly once.

Revision ID: 0013_scheduler_job_run
Revises: 0012_gap_resolution

`#108` adds the beat that owns every recurring job -- ingestion, gap detection,
reconciliation, chain verification. The scheduling itself is code
(`fking.platform.scheduler`); what has to survive a restart is *run state*, and this is
the one table that holds it.

**The primary key is the whole durability story.** `(job_id, scheduled_fire_utc)` is not
an index choice: it is the arbiter `INSERT ... ON CONFLICT DO NOTHING` needs, and it is
what makes "a restart does not re-fire a window that already ran" a property of a unique
index rather than of a code path somebody has to keep correct. `scheduled_fire_utc` is
what the run is *for* -- an hourly ingestion replaying 04:00 is a run of 04:00 whenever
it actually happens -- which is why the identity is durable across processes at all. A
key of `(job_id, started_at)` would be unique per attempt and therefore deduplicate
nothing.

**A run is closed once, and only in its completion columns.** The obvious implementation
of "the job finished" is an unconstrained `UPDATE`, and it admits two rewrites that must
not exist: moving `scheduled_fire_utc`, which relabels which window a run covered, and
re-finishing a closed run, which overwrites a recorded failure with a later success and
leaves nothing saying the first attempt happened. `scheduler_job_run_completion_only`
refuses both. The `finished_at_utc IS NULL` predicate in the application's own `UPDATE`
is the same rule stated from the other side -- it is how a second writer learns it lost,
by updating zero rows.

**Deliberately not append-only.** `docs/rules/append-only-audit.md` governs tables a
trade is reconstructed from; this one is operational state, and a run genuinely has two
states. The audit story for what the system *did* is `audit_log` (#94). What this table
promises is narrower and is enforced here: a claim is never rewritten, and a completion
is written once.

`downgrade` drops the table. That is safe in a way `0012`'s was not: every row here is
derivable in the only sense that matters -- losing them makes the beat re-fire the
current window once and no data is destroyed, because nothing else references it. The
cost is one duplicate execution of every idempotent job, which is the cost the job-level
idempotency requirement in `.claude/agents/scheduler.md` already exists to absorb.
"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op

revision: str = "0013_scheduler_job_run"
down_revision: str | None = "0012_gap_resolution"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_APP: str = "fking_app"
_INGEST: str = "fking_ingest"
_MIGRATOR: str = "fking_migrator"
_TABLE: str = "scheduler_job_run"

# Mirrors fking.platform.scheduler.JobOutcome, asserted equal to it in
# tests/platform/persistence/test_schema_contract.py.
_OUTCOMES: str = "'succeeded', 'failed', 'abandoned'"


def upgrade() -> None:
    op.execute(
        f"""
        CREATE TABLE {_TABLE} (
            job_id             TEXT        NOT NULL,
            scheduled_fire_utc TIMESTAMPTZ NOT NULL,
            correlation_id     UUID        NOT NULL,
            is_catch_up        BOOLEAN     NOT NULL,
            finished_at_utc    TIMESTAMPTZ,
            outcome            TEXT,
            failure_reason     TEXT,
            recorded_at_utc    TIMESTAMPTZ NOT NULL DEFAULT clock_timestamp(),
            CONSTRAINT pk_{_TABLE} PRIMARY KEY (job_id, scheduled_fire_utc),
            CONSTRAINT ck_{_TABLE}_outcome_is_known
                CHECK (outcome IS NULL OR outcome IN ({_OUTCOMES})),
            CONSTRAINT ck_{_TABLE}_completion_carries_an_outcome
                CHECK ((finished_at_utc IS NULL) = (outcome IS NULL)),
            CONSTRAINT ck_{_TABLE}_reason_needs_an_outcome
                CHECK (failure_reason IS NULL OR outcome IS NOT NULL),
            CONSTRAINT ck_{_TABLE}_success_carries_no_reason
                CHECK (outcome <> 'succeeded' OR failure_reason IS NULL)
        )
        """
    )
    # Partial, on the predicate the boot sweep and the overlap check both use. Unfinished
    # rows are a handful at any instant however long the beat has run, so a full index
    # would be almost entirely rows neither query can match.
    op.execute(
        f"""
        CREATE INDEX ix_{_TABLE}_unfinished
            ON {_TABLE} (job_id, scheduled_fire_utc)
         WHERE finished_at_utc IS NULL
        """
    )

    op.execute(
        f"""
        CREATE FUNCTION {_TABLE}_completion_is_the_only_rewrite() RETURNS trigger
        LANGUAGE plpgsql AS $$
        BEGIN
            IF ROW(NEW.job_id, NEW.scheduled_fire_utc, NEW.correlation_id,
                   NEW.is_catch_up, NEW.recorded_at_utc)
               IS DISTINCT FROM
               ROW(OLD.job_id, OLD.scheduled_fire_utc, OLD.correlation_id,
                   OLD.is_catch_up, OLD.recorded_at_utc)
            THEN
                RAISE EXCEPTION
                    'scheduler_job_run is rewritable only in its completion: the job, '
                    'the fire time and the correlation id are what the run is identified '
                    'and traced by. Claim a new fire time instead of moving this one'
                    USING ERRCODE = 'restrict_violation';
            END IF;

            IF OLD.outcome IS NOT NULL THEN
                RAISE EXCEPTION
                    'run % of job % already finished as %; re-finishing it would '
                    'replace a recorded outcome with a later one and leave nothing '
                    'saying the first attempt happened',
                    OLD.scheduled_fire_utc, OLD.job_id, OLD.outcome
                    USING ERRCODE = 'restrict_violation';
            END IF;

            RETURN NEW;
        END;
        $$
        """
    )
    op.execute(
        f"""
        CREATE TRIGGER {_TABLE}_completion_only
            BEFORE UPDATE ON {_TABLE}
            FOR EACH ROW EXECUTE FUNCTION {_TABLE}_completion_is_the_only_rewrite()
        """
    )

    # APP_MUTABLE in fking.platform.persistence.privileges: the application owns this
    # table outright and ingestion has no business in it. The narrowing that matters is
    # the trigger above, which a privilege class cannot express.
    # Ownership is the group role, not the login role the migration connected as. An
    # object owned by a login role would move with the credential rather than with the
    # privilege class, and `test_every_table_is_owned_by_the_migrator_role` asserts it
    # over the whole catalogue rather than over the tables somebody remembered.
    op.execute(f"ALTER TABLE {_TABLE} OWNER TO {_MIGRATOR}")
    op.execute(f"ALTER FUNCTION {_TABLE}_completion_is_the_only_rewrite() OWNER TO {_MIGRATOR}")

    op.execute(f"REVOKE ALL ON {_TABLE} FROM PUBLIC")
    op.execute(f"REVOKE ALL ON {_TABLE} FROM {_APP}")
    op.execute(f"REVOKE ALL ON {_TABLE} FROM {_INGEST}")
    op.execute(f"GRANT SELECT, INSERT, UPDATE, DELETE ON {_TABLE} TO {_APP}")


def downgrade() -> None:
    op.execute(f"DROP TRIGGER {_TABLE}_completion_only ON {_TABLE}")
    op.execute(f"DROP FUNCTION {_TABLE}_completion_is_the_only_rewrite()")
    op.execute(f"DROP TABLE {_TABLE}")
