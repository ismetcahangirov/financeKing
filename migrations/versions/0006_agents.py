"""Agent runs and the verbatim record of every LLM call.

Revision ID: 0006_agents
Revises: 0005_execution_and_risk

Reversible, and it is the head, which is what makes `make migrate-down` a real command
rather than one that reports "0002 is irreversible" and stops.

`prompt_text` and `response_text` are stored here rather than logged, for two separate
reasons. They are large -- a research prompt with fenced market data runs tens of
kilobytes -- and payloads in the log stream evict the operational history an incident
needs. More importantly, log retention *expires*, and `ARCHITECTURE.md` section 11
requires the exact prompt and response months later. A payload in Loki is a payload you
will not have. The log line carries `audit_ref` and nothing else from the payload
(`.claude/rules/logging-rules.md` clause 7).

`model_id` is a column rather than a detail because the same prompt sent to two
successive models is two different experiments. A provider silently rolling a `*-latest`
alias forward changes every result in the project with no diff, and without this column
nothing after the roll can be attributed to the model that produced it.
"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op

revision: str = "0006_agents"
down_revision: str | None = "0005_execution_and_risk"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.execute(
        """
        CREATE TABLE agent_run (
            run_id           UUID        NOT NULL,
            agent_id         TEXT        NOT NULL,
            correlation_id   UUID        NOT NULL,
            started_at_utc   TIMESTAMPTZ NOT NULL,
            completed_at_utc TIMESTAMPTZ,
            outcome          TEXT,
            escalated_to     TEXT,
            recorded_at_utc  TIMESTAMPTZ DEFAULT clock_timestamp() NOT NULL,
            CONSTRAINT pk_agent_run PRIMARY KEY (run_id),
            CONSTRAINT ck_agent_run_outcome_is_known
                CHECK (outcome IS NULL OR outcome IN ('succeeded', 'failed', 'degraded')),
            -- A run that finished without an outcome, or carries an outcome while still
            -- running, is a row that no scheduler query can interpret.
            CONSTRAINT ck_agent_run_completion_carries_an_outcome
                CHECK ((completed_at_utc IS NULL) = (outcome IS NULL))
        )
        """
    )
    op.execute("CREATE INDEX ix_agent_run_correlation_id ON agent_run (correlation_id)")
    op.execute(
        "CREATE INDEX ix_agent_run_agent_id_started_at_utc ON agent_run (agent_id, started_at_utc)"
    )

    op.execute(
        """
        CREATE TABLE agent_call (
            call_id                UUID            NOT NULL,
            run_id                 UUID            NOT NULL,
            provider               TEXT            NOT NULL,
            model_id               TEXT            NOT NULL,
            temperature            NUMERIC(38, 18) NOT NULL,
            prompt_text            TEXT            NOT NULL,
            response_text          TEXT,
            prompt_token_count     INTEGER         NOT NULL,
            completion_token_count INTEGER         NOT NULL,
            latency_ms             INTEGER         NOT NULL,
            cache_hit              BOOLEAN         NOT NULL,
            schema_valid           BOOLEAN         NOT NULL,
            called_at_utc          TIMESTAMPTZ     NOT NULL,
            recorded_at_utc        TIMESTAMPTZ     DEFAULT clock_timestamp() NOT NULL,
            CONSTRAINT pk_agent_call PRIMARY KEY (call_id),
            CONSTRAINT ck_agent_call_temperature_is_not_negative CHECK (temperature >= 0),
            CONSTRAINT ck_agent_call_prompt_token_count_is_not_negative
                CHECK (prompt_token_count >= 0),
            CONSTRAINT ck_agent_call_completion_token_count_is_not_negative
                CHECK (completion_token_count >= 0),
            CONSTRAINT ck_agent_call_latency_ms_is_not_negative CHECK (latency_ms >= 0),
            -- A response that failed schema validation is still recorded verbatim: that
            -- row is the evidence a prompt regressed, and discarding it is how a
            -- systematically bad prompt reports a healthy failure rate. Only a call that
            -- produced no response at all may leave the text null.
            CONSTRAINT ck_agent_call_valid_response_has_text
                CHECK (response_text IS NOT NULL OR schema_valid = false),
            CONSTRAINT fk_agent_call_run_id_agent_run
                FOREIGN KEY (run_id) REFERENCES agent_run (run_id)
        )
        """
    )
    op.execute("CREATE INDEX ix_agent_call_run_id ON agent_call (run_id)")
    op.execute(
        "CREATE INDEX ix_agent_call_model_id_called_at_utc ON agent_call (model_id, called_at_utc)"
    )

    op.execute("REVOKE ALL ON agent_call FROM PUBLIC")
    op.execute("REVOKE ALL ON agent_call FROM fking_app")
    op.execute("GRANT INSERT, SELECT ON agent_call TO fking_app")
    op.execute(
        "CREATE TRIGGER agent_call_no_update_delete BEFORE UPDATE OR DELETE ON agent_call "
        "FOR EACH ROW EXECUTE FUNCTION fking_append_only_guard()"
    )
    op.execute("GRANT SELECT, INSERT, UPDATE, DELETE ON agent_run TO fking_app")


def downgrade() -> None:
    op.execute("DROP TABLE IF EXISTS agent_call")
    op.execute("DROP TABLE IF EXISTS agent_run")
