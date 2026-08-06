"""Building the `agent_run` and `agent_call` rows the audit-trail suites read back.

Shared rather than duplicated per file because both suites need the same shape and a
second copy of it would drift: one file would keep passing while the column it stopped
writing became the one nobody verified.
"""

from __future__ import annotations

from collections.abc import Mapping
from datetime import UTC, datetime
from decimal import Decimal
from uuid import UUID, uuid4

import sqlalchemy as sa
from sqlalchemy.ext.asyncio import AsyncConnection

from fking.agents import (
    AgentCallRecord,
    append_agent_call,
    prompt_address,
    register_prompt_template,
    render_prompt,
)

__all__ = ["SENTIMENT_TEMPLATE", "call_record", "open_run", "seed_call"]

# A `string.Template` prompt with two substitutions and a brace-heavy fenced block, which
# is the shape that makes `str.format` unusable here and is therefore worth carrying in
# the fixture rather than a bare "hello $name".
SENTIMENT_TEMPLATE = (
    'You are $agent_id. Reply with JSON matching {"direction": ..., "conviction": ...}.\n'
    "<untrusted>\n$headlines\n</untrusted>\n"
)

_OPEN_RUN = sa.text(
    """
    INSERT INTO agent_run (run_id, agent_id, correlation_id, started_at_utc)
    VALUES (:run_id, :agent_id, :correlation_id, :started_at_utc)
    """
)


async def open_run(
    connection: AsyncConnection, *, agent_id: str = "sentiment", correlation_id: UUID | None = None
) -> tuple[UUID, UUID]:
    """Open one agent run and return `(run_id, correlation_id)`."""
    run_id = uuid4()
    correlation = correlation_id or uuid4()
    await connection.execute(
        _OPEN_RUN,
        {
            "run_id": run_id,
            "agent_id": agent_id,
            "correlation_id": correlation,
            "started_at_utc": datetime(2026, 8, 5, 9, 30, tzinfo=UTC),
        },
    )
    return run_id, correlation


def call_record(
    *,
    run_id: UUID,
    correlation_id: UUID,
    prompt_hash: bytes,
    prompt_variables: Mapping[str, str],
    prompt_text: str,
    response_text: str | None,
    schema_valid: bool,
    agent_id: str = "sentiment",
    call_id: UUID | None = None,
) -> AgentCallRecord:
    return AgentCallRecord(
        call_id=call_id or uuid4(),
        run_id=run_id,
        correlation_id=correlation_id,
        agent_id=agent_id,
        provider="gemini",
        model_id="gemini-2.5-flash-002",
        temperature=Decimal("0"),
        prompt_hash=prompt_hash,
        prompt_variables=prompt_variables,
        prompt_text=prompt_text,
        response_text=response_text,
        prompt_token_count=1_200,
        completion_token_count=64,
        latency_ms=830,
        cache_hit=False,
        schema_valid=schema_valid,
        called_at_utc=datetime(2026, 8, 5, 9, 31, tzinfo=UTC),
    )


async def seed_call(
    connection: AsyncConnection,
    *,
    headlines: str = "BTC funding flipped negative.",
    response_text: str | None = '{"direction":"flat"}',
    schema_valid: bool = True,
    register_template: bool = True,
    agent_id: str = "sentiment",
) -> tuple[int, AgentCallRecord]:
    """Register the template (optionally), render a prompt from it, and append the call.

    `register_template=False` is how a test produces the P0 case: a call whose prompt
    address names nothing, which is what an in-place prompt edit leaves behind.
    """
    variables = {"agent_id": agent_id, "headlines": headlines}
    if register_template:
        address = await register_prompt_template(
            connection,
            agent_id=agent_id,
            template_text=SENTIMENT_TEMPLATE,
            registered_at_utc=datetime(2026, 8, 1, tzinfo=UTC),
        )
    else:
        address = prompt_address(SENTIMENT_TEMPLATE)

    run_id, correlation_id = await open_run(connection, agent_id=agent_id)
    record = call_record(
        run_id=run_id,
        correlation_id=correlation_id,
        prompt_hash=address,
        prompt_variables=variables,
        prompt_text=render_prompt(SENTIMENT_TEMPLATE, variables),
        response_text=response_text,
        schema_valid=schema_valid,
        agent_id=agent_id,
    )
    return await append_agent_call(connection, record), record
