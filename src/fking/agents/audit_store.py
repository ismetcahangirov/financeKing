"""The write and verification paths for the agent call trail.

This is the seam between `fking.agents`' contract types and the append-only tables that
outlive the process. It holds four operations and no policy:

- `register_prompt_template` -- content-addressed, `ON CONFLICT DO NOTHING`, so
  registering the same text twice is a no-op rather than a duplicate or an error.
- `append_agent_call` -- one row, inside the caller's transaction. There is no update, no
  correct and no delete, here or on the table.
- `verify_agent_call_chain` -- re-derives every digest and compares each link, which is
  the only thing that sees a rewrite the grants and the trigger could not refuse.
- `replay_sample` and `agent_contribution` -- the two read paths, one for the scheduled
  verification job and one for reconstructing a decision.

The chain is re-derived by calling the database's own `fking_agent_call_digest()` rather
than by reimplementing the recipe in Python. A verifier with its own copy of a hash recipe
drifts from the trigger, and the way you find out is a job that reports every row as
tampered -- after which somebody mutes the job, and the next real tampering event is
invisible.

**Never a secret in one of these rows.** Audit rows are forever, which makes them the
worst possible place for key material -- including key material that arrived inside a
prompt payload. `append_agent_call` refuses rather than redacting: a redacted audit row is
a row whose contents depend on what the redactor knew, and the call that carried the key
is a call that must be investigated, not cleaned up.
"""

from __future__ import annotations

import json
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
from typing import Final
from uuid import UUID

import sqlalchemy as sa
from sqlalchemy.ext.asyncio import AsyncConnection

from fking.agents.audit import AgentCallRecord
from fking.agents.replay import PromptTemplate, RecordedPrompt, ReplayFinding, verify_sample
from fking.platform.logging import KEY_MATERIAL_MARKERS

__all__ = [
    "AgentContribution",
    "ChainVerification",
    "KeyMaterialInAuditRowError",
    "ReplayVerification",
    "agent_contribution",
    "append_agent_call",
    "register_prompt_template",
    "replay_sample",
    "verify_agent_call_chain",
]


class KeyMaterialInAuditRowError(RuntimeError):
    """A row carrying key material was offered to an append-only table."""


@dataclass(frozen=True, slots=True)
class ChainVerification:
    """The result of re-deriving the `agent_call` chain."""

    checked_row_count: int
    first_broken_seq: int | None
    reason: str | None

    @property
    def is_intact(self) -> bool:
        return self.first_broken_seq is None


@dataclass(frozen=True, slots=True)
class ReplayVerification:
    """One pass of the scheduled prompt-hash replay job."""

    sampled_call_count: int
    findings: tuple[ReplayFinding, ...]

    @property
    def is_clean(self) -> bool:
        return not self.findings


@dataclass(frozen=True, slots=True)
class AgentContribution:
    """One agent call, as read back for reconstruction. Mirrors the row, verbatim."""

    seq: int
    call_id: UUID
    run_id: UUID
    correlation_id: UUID
    agent_id: str
    provider: str
    model_id: str
    temperature: Decimal
    prompt_hash: bytes
    prompt_variables: Mapping[str, str]
    prompt_text: str
    response_text: str | None
    prompt_token_count: int
    completion_token_count: int
    latency_ms: int
    cache_hit: bool
    schema_valid: bool
    called_at_utc: datetime


_REGISTER_TEMPLATE: Final = sa.text(
    """
    INSERT INTO prompt_template (prompt_hash, agent_id, template_text, registered_at_utc)
    VALUES ('\\x00'::bytea, :agent_id, :template_text, :registered_at_utc)
    ON CONFLICT (prompt_hash) DO NOTHING
    RETURNING prompt_hash
    """
)

_RESOLVE_ADDRESS: Final = sa.text(
    "SELECT digest(convert_to(:template_text, 'UTF8'), 'sha256') AS prompt_hash"
)

_APPEND_CALL: Final = sa.text(
    """
    INSERT INTO agent_call (
        call_id, run_id, correlation_id, agent_id, provider, model_id, temperature,
        prompt_hash, prompt_variables, prompt_text, response_text, prompt_token_count,
        completion_token_count, latency_ms, cache_hit, schema_valid, called_at_utc,
        prev_hash, row_hash
    )
    VALUES (
        :call_id, :run_id, :correlation_id, :agent_id, :provider, :model_id, :temperature,
        :prompt_hash, cast(:prompt_variables as jsonb), :prompt_text, :response_text,
        :prompt_token_count, :completion_token_count, :latency_ms, :cache_hit,
        :schema_valid, :called_at_utc, '\\x00'::bytea, '\\x00'::bytea
    )
    RETURNING seq
    """
)

# Both hashes go in as placeholders and are overwritten by the BEFORE INSERT trigger. The
# application cannot supply its own chain values, which is the point: a writer that
# computes its own can compute consistent ones for a forged row.

_VERIFY_CHAIN: Final = sa.text(
    """
    SELECT seq,
           prev_hash,
           row_hash,
           lag(row_hash) OVER (ORDER BY seq) AS predecessor,
           fking_agent_call_digest(
               prev_hash, seq, call_id, run_id, correlation_id, agent_id, provider,
               model_id, temperature, prompt_hash, prompt_variables, prompt_text,
               response_text, prompt_token_count, completion_token_count, latency_ms,
               cache_hit, schema_valid, called_at_utc) AS recomputed
      FROM agent_call
     WHERE seq > :since_seq
     ORDER BY seq
    """
)

_SAMPLE_CALLS: Final = sa.text(
    """
    SELECT call_id, correlation_id, agent_id, prompt_hash, prompt_variables, prompt_text
      FROM agent_call
     -- Deterministic given the same rows and the same seed, so a finding reproduces from
     -- the job's own log rather than only from the row it happened to draw.
     ORDER BY md5(call_id::text || :seed)
     LIMIT :sample_size
    """
)

_LOAD_TEMPLATES: Final = sa.text(
    "SELECT prompt_hash, agent_id, template_text FROM prompt_template "
    "WHERE prompt_hash = ANY(:addresses)"
)

_CONTRIBUTION: Final = sa.text(
    """
    SELECT seq, call_id, run_id, correlation_id, agent_id, provider, model_id, temperature,
           prompt_hash, prompt_variables, prompt_text, response_text, prompt_token_count,
           completion_token_count, latency_ms, cache_hit, schema_valid, called_at_utc
      FROM agent_call
     WHERE correlation_id = :correlation_id
     ORDER BY seq
    """
)


def _first_key_material_field(record: AgentCallRecord) -> str | None:
    candidates: list[tuple[str, str]] = [
        ("prompt_text", record.prompt_text),
        ("response_text", record.response_text or ""),
        *(
            (f"prompt_variables.{name}", supplied)
            for name, supplied in record.prompt_variables.items()
        ),
    ]
    for field_name, text in candidates:
        if any(marker in text for marker in KEY_MATERIAL_MARKERS):
            return field_name
    return None


def _variables_json(variables: Mapping[str, str]) -> str:
    # sort_keys so the JSONB text the digest is taken over does not depend on the order a
    # caller happened to build the mapping in.
    return json.dumps(dict(variables), sort_keys=True)


async def register_prompt_template(
    connection: AsyncConnection, *, agent_id: str, template_text: str, registered_at_utc: datetime
) -> bytes:
    """Register `template_text` and return its content address.

    Idempotent by address: the same text registered twice yields the same hash and writes
    one row. The address is computed by the database in both branches, so a caller never
    holds an address the registry disagrees with.
    """
    inserted = await connection.scalar(
        _REGISTER_TEMPLATE,
        {
            "agent_id": agent_id,
            "template_text": template_text,
            "registered_at_utc": registered_at_utc,
        },
    )
    if inserted is not None:
        return bytes(inserted)
    existing = await connection.scalar(_RESOLVE_ADDRESS, {"template_text": template_text})
    if existing is None:  # pragma: no cover - digest() always returns a value
        raise RuntimeError("prompt_template address could not be resolved")
    return bytes(existing)


async def append_agent_call(connection: AsyncConnection, record: AgentCallRecord) -> int:
    """Append one call and return its chain position.

    Inside the caller's transaction, so the row and whatever else the caller is writing
    commit together or not at all.
    """
    offending = _first_key_material_field(record)
    if offending is not None:
        raise KeyMaterialInAuditRowError(
            f"agent call {record.call_id} carries key material in {offending}; an audit "
            f"row is permanent, so this call is an incident to investigate rather than a "
            f"payload to redact"
        )
    seq = await connection.scalar(
        _APPEND_CALL,
        {
            "call_id": record.call_id,
            "run_id": record.run_id,
            "correlation_id": record.correlation_id,
            "agent_id": record.agent_id,
            "provider": record.provider,
            "model_id": record.model_id,
            "temperature": record.temperature,
            "prompt_hash": record.prompt_hash,
            "prompt_variables": _variables_json(record.prompt_variables),
            "prompt_text": record.prompt_text,
            "response_text": record.response_text,
            "prompt_token_count": record.prompt_token_count,
            "completion_token_count": record.completion_token_count,
            "latency_ms": record.latency_ms,
            "cache_hit": record.cache_hit,
            "schema_valid": record.schema_valid,
            "called_at_utc": record.called_at_utc,
        },
    )
    if seq is None:  # pragma: no cover - RETURNING always yields on success
        raise RuntimeError("agent_call INSERT returned no seq")
    return int(seq)


async def verify_agent_call_chain(
    connection: AsyncConnection, *, since_seq: int = 0
) -> ChainVerification:
    """Re-derive every digest and compare every link, reporting the first divergence.

    Re-deriving matters as much as the link check: a rewrite that also recomputed the
    chain forward passes a link-only check, and a `pg_restore` from a doctored dump is
    exactly the actor with the access to do that.
    """
    rows = (await connection.execute(_VERIFY_CHAIN, {"since_seq": since_seq})).all()
    for index, row in enumerate(rows):
        if bytes(row.recomputed) != bytes(row.row_hash):
            return ChainVerification(index, int(row.seq), "row_hash does not match the row content")
        if row.predecessor is not None and bytes(row.prev_hash) != bytes(row.predecessor):
            return ChainVerification(
                index, int(row.seq), "prev_hash does not match its predecessor"
            )
    return ChainVerification(len(rows), None, None)


async def replay_sample(
    connection: AsyncConnection, *, sample_size: int, seed: str
) -> ReplayVerification:
    """Sample historical calls and prove each one still renders from its template.

    Bounded by `sample_size` because the job runs on the system beat and the table grows
    without limit; deterministic in `seed` because a finding nobody can reproduce is a
    finding that gets argued with.
    """
    if sample_size < 1:
        raise ValueError(f"sample_size must be at least 1; got {sample_size}")

    sampled = (
        await connection.execute(_SAMPLE_CALLS, {"sample_size": sample_size, "seed": seed})
    ).all()
    recorded = tuple(
        RecordedPrompt(
            call_id=row.call_id,
            correlation_id=row.correlation_id,
            agent_id=row.agent_id,
            prompt_hash=bytes(row.prompt_hash),
            prompt_variables=dict(row.prompt_variables),
            prompt_text=row.prompt_text,
        )
        for row in sampled
    )
    addresses = sorted({candidate.prompt_hash for candidate in recorded})
    template_rows = (
        (await connection.execute(_LOAD_TEMPLATES, {"addresses": addresses})).all()
        if addresses
        else []
    )
    templates = {
        bytes(row.prompt_hash): PromptTemplate(
            prompt_hash=bytes(row.prompt_hash),
            agent_id=row.agent_id,
            template_text=row.template_text,
        )
        for row in template_rows
    }
    return ReplayVerification(len(recorded), verify_sample(recorded, templates))


async def agent_contribution(
    connection: AsyncConnection, correlation_id: UUID
) -> tuple[AgentContribution, ...]:
    """Every agent call that contributed to one decision, in chain order.

    Reads only `agent_call`. A reconstruction that needs a running process, or a second
    table still agreeing, is not a reconstruction (`ARCHITECTURE.md` section 11).
    """
    rows = (await connection.execute(_CONTRIBUTION, {"correlation_id": correlation_id})).all()
    return tuple(
        AgentContribution(
            seq=int(row.seq),
            call_id=row.call_id,
            run_id=row.run_id,
            correlation_id=row.correlation_id,
            agent_id=row.agent_id,
            provider=row.provider,
            model_id=row.model_id,
            temperature=row.temperature,
            prompt_hash=bytes(row.prompt_hash),
            prompt_variables=dict(row.prompt_variables),
            prompt_text=row.prompt_text,
            response_text=row.response_text,
            prompt_token_count=int(row.prompt_token_count),
            completion_token_count=int(row.completion_token_count),
            latency_ms=int(row.latency_ms),
            cache_hit=bool(row.cache_hit),
            schema_valid=bool(row.schema_valid),
            called_at_utc=row.called_at_utc,
        )
        for row in rows
    )
