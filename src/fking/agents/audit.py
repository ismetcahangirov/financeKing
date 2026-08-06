"""What an agent call leaves behind, and the seam that writes it.

`ARCHITECTURE.md` section 11 requires that any trade be reconstructable months later,
including which agent reasoning contributed to it. That is what these types carry, and
it is why they are shaped like the `agent_call` table rather than like a log line: log
retention expires, and the exact prompt is needed after it does.

### The response is recorded verbatim, especially when it failed

A parse failure recorded as "parse failed" and nothing else has discarded the only
evidence of what went wrong -- and that corpus is the highest-value input to the next
prompt revision. The `agent_call` table encodes the same rule as a `CHECK`: a row may
omit `response_text` only when `schema_valid` is false *and* no response arrived at all.

### Why the recorder is a Protocol here

The parse path must record before it raises, and it must do so without holding anything
that can talk to a provider. A `Protocol` with one method is the narrowest thing that
achieves both: a test satisfies it with a list, and the gateway (#72) satisfies it with
a transaction. Nothing in this package persists anything -- the write is a dependency,
which is what keeps the parse path free of I/O and free of a session.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime, timedelta
from decimal import Decimal
from types import MappingProxyType
from typing import Final, Protocol
from uuid import UUID

__all__ = [
    "AgentCallContext",
    "AgentCallRecord",
    "AuditRecorder",
    "CompletionResult",
]

_UTC_OFFSET: Final[timedelta] = timedelta(0)

# A prompt address is a sha256 digest, and the database computes its own from the
# template text. A length check here turns "somebody passed a hex string" into a failure
# at construction rather than into a row whose prompt can never be resolved.
_SHA256_BYTES: Final[int] = 32


def _require_utc(candidate: datetime, field_name: str) -> datetime:
    """A timezone-aware datetime whose offset is exactly UTC.

    Rejects rather than converts, for the reason `fking.domain` gives: `astimezone(UTC)`
    silently accepts a datetime whose offset was guessed wrong three modules ago, and
    launders the wrong guess into a confident value. Duplicated here rather than
    imported because the domain's guard lives in a private submodule, and a cross-package
    import of a `_`-prefixed module is a boundary violation whichever direction it points
    (`.claude/rules/module-boundaries.md`).
    """
    if candidate.tzinfo is None or candidate.utcoffset() is None:
        raise ValueError(f"{field_name} must be timezone-aware; got naive {candidate!r}")
    if candidate.utcoffset() != _UTC_OFFSET:
        raise ValueError(
            f"{field_name} must be UTC; got offset {candidate.utcoffset()!r} in {candidate!r}"
        )
    return candidate


@dataclass(frozen=True, slots=True)
class AgentCallContext:
    """Everything about a call that is known before the model answers.

    `model_id` is pinned and never a floating alias: the same prompt to
    `gemini-2.5-flash-002` and to its successor are two different experiments, and a
    provider rolling `*-latest` forward changes every result in the project with no
    diff. `AgentDeclaration` refuses the floating spellings at construction.

    `temperature` is a `Decimal` rather than a float because it is compared for
    equality -- the cache is only consulted at exactly zero -- and `0.1 + 0.2 != 0.3`
    is not a property a cache key should inherit.
    """

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
    called_at_utc: datetime

    def __post_init__(self) -> None:
        _require_utc(self.called_at_utc, "called_at_utc")
        if self.temperature < Decimal("0"):
            raise ValueError(f"temperature must not be negative; got {self.temperature}")
        if len(self.prompt_hash) != _SHA256_BYTES:
            raise ValueError(
                f"prompt_hash must be a {_SHA256_BYTES}-byte sha256 address of the "
                f"template this prompt was rendered from; got {len(self.prompt_hash)} bytes"
            )
        # frozen=True protects the binding, not the object bound: without the copy the
        # caller keeps a live handle on a mapping that is about to be hashed into an
        # append-only row (.claude/rules/immutability.md).
        object.__setattr__(self, "prompt_variables", MappingProxyType(dict(self.prompt_variables)))


@dataclass(frozen=True, slots=True)
class CompletionResult:
    """What a provider returned, before anything has interpreted it.

    `text` is the response exactly as it arrived -- no strip, no code-fence removal, no
    JSON extraction. Every one of those is a decision, and a decision made here is one
    nobody reviewed.
    """

    text: str
    prompt_token_count: int
    completion_token_count: int
    latency_ms: int
    cache_hit: bool

    def __post_init__(self) -> None:
        negatives = [
            name
            for name, quantity in (
                ("prompt_token_count", self.prompt_token_count),
                ("completion_token_count", self.completion_token_count),
                ("latency_ms", self.latency_ms),
            )
            if quantity < 0
        ]
        if negatives:
            raise ValueError(f"{negatives} must not be negative")


@dataclass(frozen=True, slots=True)
class AgentCallRecord:
    """One `agent_call` row, assembled but not yet written.

    Mirrors the table column for column, name for name, so a row can be splatted into
    this type with no translation layer -- a mapping dictionary is where `qty` quietly
    gets paired with `notional_usd` (`.claude/rules/naming.md`).
    """

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

    def __post_init__(self) -> None:
        _require_utc(self.called_at_utc, "called_at_utc")
        object.__setattr__(self, "prompt_variables", MappingProxyType(dict(self.prompt_variables)))
        if self.response_text is None and self.schema_valid:
            raise ValueError(
                "schema_valid is true with no response_text; a validated response has "
                "text by definition, and the audit row would claim a decision that has "
                "no evidence behind it"
            )

    @classmethod
    def from_call(
        cls,
        call: AgentCallContext,
        completion: CompletionResult,
        *,
        schema_valid: bool,
    ) -> AgentCallRecord:
        """The row for a call that produced a response, valid or not."""
        return cls(
            call_id=call.call_id,
            run_id=call.run_id,
            correlation_id=call.correlation_id,
            agent_id=call.agent_id,
            provider=call.provider,
            model_id=call.model_id,
            temperature=call.temperature,
            prompt_hash=call.prompt_hash,
            prompt_variables=call.prompt_variables,
            prompt_text=call.prompt_text,
            # Verbatim, and verbatim on the failure path most of all.
            response_text=completion.text,
            prompt_token_count=completion.prompt_token_count,
            completion_token_count=completion.completion_token_count,
            latency_ms=completion.latency_ms,
            cache_hit=completion.cache_hit,
            schema_valid=schema_valid,
            called_at_utc=call.called_at_utc,
        )


class AuditRecorder(Protocol):
    """The append point for an agent call.

    One method, synchronous, returning nothing. Synchronous because the parse path has
    no event loop and should not acquire one; the implementation is free to buffer and
    flush inside the caller's transaction, which is what the gateway does.

    There is no `update`, no `correct` and no `delete`, here or on the table
    (`.claude/rules/append-only-audit.md`). A correction is a new row.
    """

    def record(self, call_record: AgentCallRecord) -> None:
        """Append `call_record`. Must not raise for an ordinary parse failure."""
        ...
