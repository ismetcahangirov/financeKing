"""One attempt at validating a model response. No repair, no rescue, no default.

### The signature is the enforcement

`parse_or_fail` takes a call context, a schema, a completion and an append-only
recorder. There is no gateway, no provider, no client, no session and no event loop in
scope, so a re-ask cannot be added here without changing the function's dependencies --
which is a reviewable diff rather than a two-line patch on the failure path at 1am. The
function is synchronous for the same reason: there is nothing to await, and adding an
`async` would be the first half of adding a second call.

### Why zero attempts rather than one

Both readings the repository used to carry agreed on the thing that matters: a *loop*
over a stochastic generator is a search for output that passes validation, not for
output that is correct. The disagreement was over a single repair with the
`ValidationError` fed back, on the grounds that an error-carrying retry is a correction
signal rather than sampling. Three arguments beat it, and ADR-0020 records them in
full: the parse-failure rate is the instrument that says a prompt and its schema are
not doing their job, and a re-ask suppresses exactly that signal; free-tier quota is
scarce and shared, and a re-ask spends the golden set's budget on the failure path,
unattended; and "exactly one" is one line from "exactly two", whereas zero has no
adjacent value.

Zero is also the reading already encoded in a type -- `AgentSettings.max_reask_attempts`
is `Literal[0]`, so `mypy --strict` refuses any other assignment -- and an unresolved
disagreement sitting in the parse path is precisely where charitable interpretation
creeps back in six months, wearing the other document as justification.

### What is deliberately absent

No regex extracting a JSON object out of prose. No stripping a markdown code fence. No
`json.loads` on a substring. No lowercasing a `Literal`. No default returned on
failure. Each looks like politeness and is a decision layer: the parser deciding, under
no schema and with no audit trail, what the model meant. The failure is not that it
guesses wrong once -- it is that the guesses accumulate into a second decision layer
nobody reviews.
"""

from __future__ import annotations

from pydantic import BaseModel, ValidationError

from fking.agents._errors import AgentOutputInvalid
from fking.agents._metrics import PARSE_FAILURES
from fking.agents.audit import AgentCallContext, AgentCallRecord, AuditRecorder, CompletionResult

__all__ = ["parse_or_fail"]


def parse_or_fail[OutputT: BaseModel](
    call: AgentCallContext,
    schema: type[OutputT],
    completion: CompletionResult,
    recorder: AuditRecorder,
) -> OutputT:
    """`completion.text` validated against `schema`, or a failure that is recorded first.

    The audit row is written on both paths and carries the response verbatim on both.
    On the failure path it is written *before* the exception is raised, so a caller that
    lets `AgentOutputInvalid` propagate still leaves the evidence behind -- the corpus
    of raw failing responses is the highest-value input to the next prompt revision, and
    a row saying "parse failed" and nothing else has thrown it away.

    Raises `AgentOutputInvalid`, whose message names the agent and the call id and never
    the raw text: the response is already on the audit row, and repeating it in an
    exception message puts model-authored text into the log stream.
    """
    try:
        parsed = schema.model_validate_json(completion.text, strict=True)
    except ValidationError as invalid:
        recorder.record(AgentCallRecord.from_call(call, completion, schema_valid=False))
        PARSE_FAILURES.increment(agent=call.agent_id, provider=call.provider)
        raise AgentOutputInvalid(
            f"{call.agent_id} produced output failing {schema.__name__} "
            f"({invalid.error_count()} error(s)); call_id={call.call_id}"
        ) from invalid

    recorder.record(AgentCallRecord.from_call(call, completion, schema_valid=True))
    return parsed
