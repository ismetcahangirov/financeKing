"""Proving a recorded agent call is still reconstructable, months after it happened.

An audit row that *contains* a prompt proves what was sent. It does not prove which
prompt version sent it, and it does not prove anyone can still produce that text from the
artefacts the repository holds. Those are the two questions an investigation asks, and
this module answers them mechanically:

1. **Resolvability.** Does `agent_call.prompt_hash` name a row in `prompt_template`? A
   hash that does not resolve is a **P0 finding**, not a data-quality ticket: it means a
   prompt was edited in place or lost, and some window of agent reasoning is permanently
   unauditable. Nothing downstream can tell you how wide that window is.
2. **Reproducibility.** Substituting the recorded `prompt_variables` into the resolved
   template must reproduce the recorded `prompt_text` byte for byte. If it does not, the
   stored artefacts no longer describe the call, which is the same failure wearing a
   different hat.

**Replay verifies rendering, never model output.** Re-running the model proves nothing --
it is stochastic, it costs quota that `llm_quota_ledger` has to ration, and a verification
job that gets skipped when quota is tight verifies nothing. The sample is bounded and the
work is local, so this job never needs a reservation at all.

`string.Template` rather than `str.format`: a rendered prompt contains fenced market data
and JSON schemas, both of which are full of braces, and `format` would treat them as
placeholders. `$`-substitution with `safe_substitute` disabled means an unknown or missing
variable raises rather than silently rendering a prompt nobody sent.
"""

from __future__ import annotations

import hashlib
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from string import Template
from typing import Final, Literal
from uuid import UUID

__all__ = [
    "P0",
    "PromptTemplate",
    "RecordedPrompt",
    "ReplayFinding",
    "ReplayReason",
    "prompt_address",
    "render_prompt",
    "verify_recorded_prompt",
    "verify_sample",
]

# Every finding this module can produce is P0. That is not severity inflation: each one
# means a specific span of agent reasoning cannot be reconstructed, and the span is not
# knowable from the finding itself.
P0: Final[str] = "p0"

ReplayReason = Literal[
    "unresolvable_prompt_hash",
    "template_edited_in_place",
    "prompt_not_reproducible",
]


def prompt_address(template_text: str) -> bytes:
    """The content address of a prompt template.

    Mirrors `fking_prompt_template_address()` exactly -- sha256 over the UTF-8 bytes of
    the template. Two implementations of one digest recipe drift, and the way you find
    out is a verification job that reports every row as tampered, after which the job
    gets muted. The database's copy is authoritative; this one exists so a caller can
    address a template before it has been registered.
    """
    return hashlib.sha256(template_text.encode("utf-8")).digest()


def render_prompt(template_text: str, variables: Mapping[str, str]) -> str:
    """Substitute `variables` into `template_text`, refusing anything ambiguous.

    Raises `KeyError` for a placeholder with no value and `ValueError` for a malformed
    one. Both are caught by `verify_recorded_prompt`, which turns them into findings --
    but they are raised rather than repaired, because a prompt rendered with a guessed
    value is a prompt nobody sent.
    """
    return Template(template_text).substitute(variables)


@dataclass(frozen=True, slots=True)
class PromptTemplate:
    """One `prompt_template` row."""

    prompt_hash: bytes
    agent_id: str
    template_text: str


@dataclass(frozen=True, slots=True)
class RecordedPrompt:
    """The prompt half of one `agent_call` row, as sampled for replay."""

    call_id: UUID
    correlation_id: UUID
    agent_id: str
    prompt_hash: bytes
    prompt_variables: Mapping[str, str]
    prompt_text: str


@dataclass(frozen=True, slots=True)
class ReplayFinding:
    """A recorded call that cannot be reconstructed. Always P0."""

    call_id: UUID
    correlation_id: UUID
    prompt_hash_hex: str
    reason: ReplayReason
    detail: str
    severity: str = P0


def verify_recorded_prompt(
    recorded: RecordedPrompt, template: PromptTemplate | None
) -> ReplayFinding | None:
    """Return the finding this call produces, or `None` if it reconstructs cleanly.

    Pure: no clock, no I/O, no sampling. The database supplies the pair and this decides,
    which is what lets the whole decision table be exercised without a container.
    """
    address = recorded.prompt_hash.hex()
    if template is None:
        return ReplayFinding(
            call_id=recorded.call_id,
            correlation_id=recorded.correlation_id,
            prompt_hash_hex=address,
            reason="unresolvable_prompt_hash",
            detail=(
                f"agent {recorded.agent_id} recorded prompt_hash {address} with no row in "
                f"prompt_template; the prompt was edited in place or lost, and every call "
                f"carrying this address is unauditable"
            ),
        )

    # The registry is content-addressed by a database trigger, so this can only fail if
    # the row was rewritten underneath the trigger -- which is precisely the event the
    # append-only guard cannot refuse and the chain is there to expose.
    recomputed = prompt_address(template.template_text)
    if recomputed != template.prompt_hash:
        return ReplayFinding(
            call_id=recorded.call_id,
            correlation_id=recorded.correlation_id,
            prompt_hash_hex=address,
            reason="template_edited_in_place",
            detail=(
                f"prompt_template row {template.prompt_hash.hex()} holds text addressing "
                f"{recomputed.hex()}; the stored text is not the text that hash was taken over"
            ),
        )

    try:
        rendered = render_prompt(template.template_text, recorded.prompt_variables)
    except (KeyError, ValueError) as unrenderable:
        return ReplayFinding(
            call_id=recorded.call_id,
            correlation_id=recorded.correlation_id,
            prompt_hash_hex=address,
            reason="prompt_not_reproducible",
            detail=(
                f"template {address} does not render from the recorded variables: "
                f"{type(unrenderable).__name__}: {unrenderable}"
            ),
        )

    if rendered != recorded.prompt_text:
        return ReplayFinding(
            call_id=recorded.call_id,
            correlation_id=recorded.correlation_id,
            prompt_hash_hex=address,
            reason="prompt_not_reproducible",
            detail=(
                f"re-rendering template {address} produced {len(rendered)} characters "
                f"against the {len(recorded.prompt_text)} recorded; the stored artefacts "
                f"no longer reproduce the call"
            ),
        )
    return None


def verify_sample(
    sample: Sequence[RecordedPrompt], templates: Mapping[bytes, PromptTemplate]
) -> tuple[ReplayFinding, ...]:
    """Every finding in `sample`, in the order the calls were sampled."""
    findings = [
        finding
        for recorded in sample
        if (finding := verify_recorded_prompt(recorded, templates.get(recorded.prompt_hash)))
        is not None
    ]
    return tuple(findings)
