"""The agent layer's members of the system error taxonomy.

Two failures live here, and both are refusals rather than faults. Neither is retried,
neither has a fallback value, and neither is recoverable by trying harder -- which is
why they are exceptions rather than sentinel returns: a refusal that a caller can
forget to check is a refusal that will be forgotten.

They subclass `FkingError` so that a handler meaning "any failure this codebase raises
on purpose" catches them, and so that nothing needs `except Exception` to see them.
"""

from __future__ import annotations

from fking.platform.errors import FkingError

__all__ = ["AgentOutputInvalid", "FencedPayloadRejected"]


class AgentOutputInvalid(FkingError):
    """A model response did not satisfy its declared schema.

    Terminal. There is no re-ask, no regex rescue of a JSON object out of prose, and no
    default value substituted for the decision the model failed to make. Every act of
    parser repair is a second decision layer that no test covers and no reviewer reads,
    running in a code path never designed to make trading decisions.

    Zero attempts rather than one is the stricter of the two readings the repository
    used to carry, and it is the one already encoded in a type
    (`AgentSettings.max_reask_attempts: Literal[0]`). ADR-0020 records the rejected
    alternative and why.

    The message names the agent and the audit reference, never the raw response: the
    raw text is already on the audit row, verbatim, and repeating it in an exception
    message puts model-authored text into the log stream, which
    `docs/rules/logging-rules.md` clause 7 forbids.
    """


class FencedPayloadRejected(FkingError):
    """Untrusted content collides with the fence that would delimit it.

    Refused rather than escaped. An escape the model can un-escape is not a boundary,
    and a payload that can close its own fence relocates itself from the data region
    into the instruction region -- which is the entire failure the fence exists to
    prevent.

    Refusing loses one document. Escaping loses the property that the block a model was
    told to treat as data is the block it actually treats as data, for every document
    after it.
    """
