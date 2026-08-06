"""The agent call trail refuses rewrites, and detects the one it cannot refuse.

Against a real PostgreSQL. A mocked database here would prove the mock is append-only:
the guarantees under test are a revoked grant, a `BEFORE UPDATE OR DELETE` trigger and a
digest computed by the server, and a mock has none of the three.
"""

from __future__ import annotations

import uuid

import pytest
import sqlalchemy as sa
from sqlalchemy.exc import DBAPIError
from sqlalchemy.ext.asyncio import AsyncEngine

from fking.agents import (
    KeyMaterialInAuditRowError,
    agent_contribution,
    append_agent_call,
    prompt_address,
    verify_agent_call_chain,
)
from tests.support.agent_rows import call_record, open_run, seed_call

pytestmark = [pytest.mark.integration, pytest.mark.slow]

# Three appends is the smallest chain where "the second row was rewritten" is a claim
# about a link rather than about the head or the tail.
_CHAINED_CALL_COUNT = 3
_CHAINED_HEADLINES = ("first", "second", "third")


@pytest.mark.asyncio
async def test_a_recorded_call_cannot_be_updated(engine: AsyncEngine) -> None:
    async with engine.begin() as connection:
        seq, _ = await seed_call(connection)

    async with engine.begin() as connection:
        with pytest.raises(DBAPIError, match="append-only"):
            await connection.execute(
                sa.text("UPDATE agent_call SET response_text = 'edited' WHERE seq = :s"),
                {"s": seq},
            )


@pytest.mark.asyncio
async def test_a_recorded_call_cannot_be_deleted(engine: AsyncEngine) -> None:
    async with engine.begin() as connection:
        seq, _ = await seed_call(connection)

    async with engine.begin() as connection:
        with pytest.raises(DBAPIError, match="append-only"):
            await connection.execute(sa.text("DELETE FROM agent_call WHERE seq = :s"), {"s": seq})


@pytest.mark.asyncio
async def test_a_registered_prompt_template_cannot_be_edited_in_place(
    engine: AsyncEngine,
) -> None:
    """The failure the replay job exists to detect must be refused outright first."""
    async with engine.begin() as connection:
        _, record = await seed_call(connection)

    async with engine.begin() as connection:
        with pytest.raises(DBAPIError, match="append-only"):
            await connection.execute(
                sa.text(
                    "UPDATE prompt_template SET template_text = 'rewritten' WHERE prompt_hash = :h"
                ),
                {"h": record.prompt_hash},
            )


@pytest.mark.asyncio
async def test_a_writer_cannot_choose_a_prompt_address_that_disagrees_with_its_text(
    engine: AsyncEngine,
) -> None:
    """Content addressing is the database's job, not the caller's.

    A writer that can supply its own address can supply a consistent one for a template
    it never used, which is the same hole the chain closes one level up.
    """
    async with engine.begin() as connection:
        await connection.execute(
            sa.text(
                "INSERT INTO prompt_template (prompt_hash, agent_id, template_text, "
                "registered_at_utc) VALUES (:forged, 'sentiment', :text, now())"
            ),
            {"forged": b"\x11" * 32, "text": "You are $agent_id."},
        )
        stored = await connection.scalar(
            sa.text("SELECT prompt_hash FROM prompt_template WHERE template_text = :text"),
            {"text": "You are $agent_id."},
        )
    assert bytes(stored) == prompt_address("You are $agent_id.")


@pytest.mark.asyncio
async def test_a_parse_failure_row_keeps_the_raw_response_verbatim(
    engine: AsyncEngine,
) -> None:
    """The highest-value corpus for the next prompt revision is the responses that failed.

    A row that records "parse failed" and nothing else has discarded the only evidence of
    what went wrong.
    """
    raw = '```json\n{"direction": "LONG",}\n```  <- trailing comma, fenced'
    async with engine.begin() as connection:
        seq, _ = await seed_call(connection, response_text=raw, schema_valid=False)

    async with engine.connect() as connection:
        row = (
            await connection.execute(
                sa.text("SELECT response_text, schema_valid FROM agent_call WHERE seq = :s"),
                {"s": seq},
            )
        ).one()
    assert row.response_text == raw
    assert row.schema_valid is False


@pytest.mark.asyncio
async def test_key_material_is_refused_rather_than_redacted(engine: AsyncEngine) -> None:
    """Audit rows are forever, which makes them the worst place for a key."""
    async with engine.begin() as connection:
        run_id, correlation_id = await open_run(connection)
        poisoned = call_record(
            run_id=run_id,
            correlation_id=correlation_id,
            prompt_hash=b"\x00" * 32,
            prompt_variables={"headlines": "-----BEGIN PRIVATE KEY-----"},
            prompt_text="You are sentiment.",
            response_text=None,
            schema_valid=False,
        )
        with pytest.raises(KeyMaterialInAuditRowError, match=r"prompt_variables\.headlines"):
            await append_agent_call(connection, poisoned)


@pytest.mark.asyncio
async def test_chain_is_intact_across_appends(engine: AsyncEngine) -> None:
    async with engine.begin() as connection:
        for headline in _CHAINED_HEADLINES:
            await seed_call(connection, headlines=headline)

    async with engine.connect() as connection:
        verification = await verify_agent_call_chain(connection)
    assert verification.is_intact
    assert verification.checked_row_count == _CHAINED_CALL_COUNT


@pytest.mark.asyncio
async def test_chain_detects_a_superuser_rewrite_at_the_exact_seq(
    engine: AsyncEngine,
) -> None:
    """The layer people skip, and the only one that sees a rewrite it cannot refuse.

    Disabling the trigger is exactly what a superuser or a doctored `pg_restore` has the
    access to do. Neither the grant nor the trigger sees the result; the chain does, and
    it names the row.
    """
    seqs: list[int] = []
    async with engine.begin() as connection:
        for headline in _CHAINED_HEADLINES:
            seq, _ = await seed_call(connection, headlines=headline)
            seqs.append(seq)

    async with engine.begin() as connection:
        await connection.execute(
            sa.text("ALTER TABLE agent_call DISABLE TRIGGER agent_call_no_update_delete")
        )
        try:
            await connection.execute(
                sa.text("UPDATE agent_call SET response_text = 'forged' WHERE seq = :s"),
                {"s": seqs[1]},
            )
        finally:
            await connection.execute(
                sa.text("ALTER TABLE agent_call ENABLE TRIGGER agent_call_no_update_delete")
            )

    async with engine.connect() as connection:
        verification = await verify_agent_call_chain(connection)
    assert verification.first_broken_seq == seqs[1]
    assert verification.reason == "row_hash does not match the row content"


@pytest.mark.asyncio
async def test_one_decision_is_reconstructable_from_agent_call_alone(
    engine: AsyncEngine,
) -> None:
    """Every field a reconstruction needs comes back off the row, with no join."""
    async with engine.begin() as connection:
        _, record = await seed_call(connection)

    async with engine.connect() as connection:
        contributions = await agent_contribution(connection, record.correlation_id)

    assert len(contributions) == 1
    contribution = contributions[0]
    assert contribution.prompt_text == record.prompt_text
    assert contribution.response_text == record.response_text
    assert contribution.model_id == "gemini-2.5-flash-002"
    assert contribution.provider == "gemini"
    assert contribution.temperature == record.temperature
    assert contribution.prompt_token_count == record.prompt_token_count
    assert contribution.completion_token_count == record.completion_token_count
    assert contribution.latency_ms == record.latency_ms
    assert contribution.cache_hit is False
    assert contribution.schema_valid is True
    assert contribution.correlation_id == record.correlation_id


@pytest.mark.asyncio
async def test_an_unrelated_correlation_id_reconstructs_to_nothing(
    engine: AsyncEngine,
) -> None:
    async with engine.begin() as connection:
        await seed_call(connection)

    async with engine.connect() as connection:
        assert await agent_contribution(connection, uuid.uuid4()) == ()
