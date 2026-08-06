"""LLM agents and their runtime. Knows about prompts, schemas and budgets.

Agents propose; deterministic code disposes. No agent output is acted on in the form
the model produced it: every response is parsed into a schema-validated structure,
and an unparseable response is a failure rather than something to interpret
charitably.

Exactly one module in this package may import a provider SDK -- the gateway, which
also owns quota admission control. It does not exist yet (#72), and nothing here
imports one: this package is the *contract* layer, and it is deliberately callable,
testable and reviewable with no provider, no network and no event loop.

What lives here now:

- `contracts` -- the Pydantic v2 input and output models. `extra="forbid"`,
  `frozen=True`, `strict=True`, decimals from JSON strings, decisions as closed
  `Literal` unions, and no size, notional, leverage or venue-host field anywhere.
- `fencing` -- `fence()`, which wraps untrusted text in a per-call nonce and refuses a
  payload that could close its own fence.
- `parse` -- `parse_or_fail()`, one attempt, no repair, no default, audit row written
  on both paths.
- `declaration` -- the seven-element agent contract, with the forbidden list as the
  load-bearing half.
- `audit` -- the row an agent call leaves behind, and the append-only seam that writes
  it.

Everything not in `__all__` is private and may change without notice.
"""

from fking.agents._errors import AgentOutputInvalid, FencedPayloadRejected
from fking.agents._metrics import AGENT_PARSE_FAILURES, PARSE_FAILURES
from fking.agents.audit import (
    AgentCallContext,
    AgentCallRecord,
    AuditRecorder,
    CompletionResult,
)
from fking.agents.contracts import (
    MAX_RATIONALE_CHARACTERS,
    AgentDecimal,
    AgentInput,
    AgentOutput,
    Conviction,
    CriticVerdict,
    CritiqueRequest,
    RationaleText,
    SentimentRequest,
    ThesisProposal,
    resolve_symbol,
)
from fking.agents.declaration import (
    CRITIC,
    DECLARED_AGENTS,
    SENTIMENT,
    UNIVERSAL_FORBIDDEN_DECISIONS,
    AgentDeclaration,
)
from fking.agents.fencing import (
    UNTRUSTED_CLOSE_PREFIX,
    UNTRUSTED_OPEN_PREFIX,
    fence,
    mint_nonce,
)
from fking.agents.parse import parse_or_fail

__all__: tuple[str, ...] = (
    "AGENT_PARSE_FAILURES",
    "CRITIC",
    "DECLARED_AGENTS",
    "MAX_RATIONALE_CHARACTERS",
    "PARSE_FAILURES",
    "SENTIMENT",
    "UNIVERSAL_FORBIDDEN_DECISIONS",
    "UNTRUSTED_CLOSE_PREFIX",
    "UNTRUSTED_OPEN_PREFIX",
    "AgentCallContext",
    "AgentCallRecord",
    "AgentDecimal",
    "AgentDeclaration",
    "AgentInput",
    "AgentOutput",
    "AgentOutputInvalid",
    "AuditRecorder",
    "CompletionResult",
    "Conviction",
    "CriticVerdict",
    "CritiqueRequest",
    "FencedPayloadRejected",
    "RationaleText",
    "SentimentRequest",
    "ThesisProposal",
    "fence",
    "mint_nonce",
    "parse_or_fail",
    "resolve_symbol",
)
