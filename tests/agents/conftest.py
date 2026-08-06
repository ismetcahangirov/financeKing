"""Fixtures for the agent contract suite.

No provider, no network, no database, no event loop. That is not a limitation of the
tests -- it is the property under test: `fking.agents` is the contract layer, and if it
needed any of those to be exercised then the parse path would have something in scope
that could re-ask.

The audit recorder is a list, deliberately. `.claude/rules/testing-rules.md` forbids
mocking the database, and this is not one: `AuditRecorder` is a Protocol whose whole
contract is "append this row", and a list satisfies it exactly. The row's journey into
PostgreSQL belongs to the gateway (#72) and is tested there, against a real server.
"""

from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal
from uuid import UUID

import pytest

from fking.agents import AgentCallContext, AgentCallRecord, CompletionResult

# Fixed ids and a fixed instant. A test that reads the clock is a test whose failure
# cannot be reproduced from the CI log.
CALL_ID = UUID("11111111-1111-4111-8111-111111111111")
RUN_ID = UUID("22222222-2222-4222-8222-222222222222")
CORRELATION_ID = UUID("33333333-3333-4333-8333-333333333333")
CALLED_AT_UTC = datetime(2026, 8, 5, 9, 30, tzinfo=UTC)


class RecordingAudit:
    """An `AuditRecorder` that keeps every row it was handed, in order."""

    def __init__(self) -> None:
        self.rows: list[AgentCallRecord] = []

    def record(self, call_record: AgentCallRecord) -> None:
        self.rows.append(call_record)


@pytest.fixture
def recorder() -> RecordingAudit:
    return RecordingAudit()


@pytest.fixture
def call() -> AgentCallContext:
    return AgentCallContext(
        call_id=CALL_ID,
        run_id=RUN_ID,
        correlation_id=CORRELATION_ID,
        agent_id="sentiment",
        provider="gemini",
        model_id="gemini-2.5-flash-002",
        temperature=Decimal("0"),
        prompt_text="You are an analyst.",
        called_at_utc=CALLED_AT_UTC,
    )


def completion(text: str) -> CompletionResult:
    """A provider response carrying `text` exactly."""
    return CompletionResult(
        text=text,
        prompt_token_count=1_200,
        completion_token_count=64,
        latency_ms=830,
        cache_hit=False,
    )
