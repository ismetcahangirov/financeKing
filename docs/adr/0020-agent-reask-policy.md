---
number: 0020
title: Zero re-asks on a schema failure
date: 2026-08-05
status: accepted
deciders: [ismetcahangirov, prompt-engineer, architect]
supersedes: null
superseded_by: null
related_issues: ["#70", "#6", "#72"]
related_adrs: ["ADR-0009"]
---

## Context

The repository said two different things about what happens when a model response fails
schema validation, and both said it in a place that reads as authoritative.

```
Forces:
- A retry loop over a stochastic generator is a search for a response that passes
  validation, not for one that is correct. Both sides of the disagreement accept
  this; neither proposes a loop.
- One repair with the ValidationError attached is genuinely different from
  resampling: the model is told what was wrong, which is information it did not
  have on the first attempt.
- The parse-failure rate is the instrument that says a prompt and its schema are
  not doing their job. Anything that absorbs failures blinds it.
- Free-tier quota is shared and exhaustible. A repair spends, unattended, at
  whatever hour the failure happens, the budget the golden set needs.
- The system runs with agents disabled by default and must be fully functional
  that way, so a failed agent call is an ordinary, well-exercised condition
  rather than an outage.

The constraint that forces a decision now:
CONFIGURATION.md section 9 and PROMPT_LIBRARY.md section 3 specify
`max_reask_attempts: Literal[0]`. `.claude/rules/llm-output-handling.md`
previously allowed exactly one repair attempt with the ValidationError fed back.
Issue #70 ships the parse path, and a parse path cannot implement both. An
unresolved disagreement sitting in that function is precisely where charitable
interpretation creeps back in six months, wearing the other document as
justification.
```

## Decision

**Zero re-asks. `max_reask_attempts` is `Literal[0]`, not an `int`.**

A schema failure fails the agent call. Concretely, in `fking.agents.parse.parse_or_fail`:

1. The raw response is written **verbatim** to the `agent_call` audit row with
   `schema_valid = false`, before anything else happens.
2. `fking_agents_parse_failures_total{agent, provider}` is incremented.
3. `AgentOutputInvalid` is raised, naming the agent and the call id and **not** the raw
   text — the text is on the audit row, and repeating it in an exception message puts
   model-authored text into the log stream.
4. The caller takes its deterministic path, which is the same path it takes when quota
   is exhausted. An agent that cannot produce valid output and an agent that cannot be
   called at all are the same condition from the caller's side, and collapsing them
   keeps the deterministic fallback on one well-exercised code path instead of two.

The enforcement is threefold and each layer catches something the others do not:

- **The type.** `AgentSettings.max_reask_attempts: Literal[0] = 0` in
  `fking.platform.config.settings`. `mypy --strict` rejects any other assignment at
  type-check time; Pydantic rejects it at construction. A setting that could be raised
  in a `.env` is not a limit, and this is the kind of limit that gets raised at 1am by
  someone whose research run is stalling.
- **The signature.** `parse_or_fail(call, schema, completion, recorder)` has no
  gateway, no provider, no client and no event loop in scope, and is synchronous. A
  second attempt cannot be added without changing the function's dependencies, which is
  a reviewable diff rather than a two-line patch on the failure path.
- **The absent metric.** There is no `AGENT_SCHEMA_REPAIRS` counter and no
  `fking_agents_schema_repairs_total` metric. `tests/agents/test_no_reask.py` asserts
  their absence over the source tree, because such a counter would have nothing to
  count unless a repair had been reintroduced.

The repair capability is not lost. It moves to the golden-set harness, where the
`ValidationError` is fed back to the **prompt author** rather than to the model
mid-decision — a loop that costs a developer's attention rather than the runtime's
quota, and that produces a prompt fix rather than a per-call patch.

`.claude/rules/llm-output-handling.md` now states zero and names the one-repair design
as the rejected alternative, so the three documents agree. This ADR is the durable
record of *why*, which a rule file's prose is not: a rule can be edited back by anyone
who finds it inconvenient, and an accepted ADR can only be superseded by another one.

## Consequences

The parse-failure rate becomes a direct measurement rather than a residual. Every
failure is exactly one call the agent could not contribute to, so
`parse_failures / calls` is exactly the fraction of decisions made without that agent.
That is what lets the alert threshold be as tight as 2%: there is no repair mechanism
quietly absorbing the first two-thirds of the problem before the counter sees it.

The cost is real and accepted: a response that a human would call "obviously nearly
right" is discarded. Provider-side structured output (Gemini's response schema, Groq's
JSON mode) reduces how often that happens, behind the gateway, and does not change the
rule — a provider claiming it returned schema-valid JSON is still an external system
making a claim about hostile input, and validation happens on our side regardless.

## Alternatives considered

### Alternative 1 — exactly one repair, with the ValidationError fed back (strongest rejected)

**What it would have given us.** This is the position `.claude/rules/llm-output-handling.md`
previously held, and the distinction it draws is real. Resampling the same prompt is a
search over the model's output distribution; re-asking with `"field 'conviction': decimal
fields must be JSON strings, not JSON numbers"` attached supplies information the model
did not have, and the correction rate on well-specified schemas is high. It is the
behaviour every structured-output library ships by default, it converts a class of
transient formatting slips into successful calls, and it costs one extra call only on
the paths that were going to fail anyway. On a research agent whose output feeds a
validation gate rather than an order, the downside of a successful repair is genuinely
small.

**Why it lost.** Three arguments, in order of weight.

First, **it blinds the instrument.** The parse-failure rate is the only signal that a
prompt and its schema have drifted apart, and it is the signal `PROMPT_LIBRARY.md`
section 3 asks to drive to zero. A repair suppresses exactly that signal: a
systematically bad prompt reports a healthy failure rate while paying double for every
call it makes. The failure mode is not a wrong answer, it is a defect that never
surfaces — and this system runs unattended, so nothing else is watching.

Second, **quota is scarce, shared and non-refundable.** A repair spends the budget the
golden set needs, on the failure path, at whatever hour the failure happens. Under the
priority floors in `.claude/rules/quota-management.md` that spend is taken from
whichever class is admitted next, which is not the class that caused it.

Third, **"exactly one" is not a stable boundary.** It is one line from "exactly two",
and it is the kind of limit raised by someone in a hurry. Zero has no adjacent value:
raising it requires writing a loop, adding a gateway to a signature that has none, and
explaining both in a pull request. That asymmetry — tightening is easy, loosening is
deliberate — is the same one `AI_MANIFEST.md` section 8 applies to what the AI system is
permitted to do at all.

**What survives the rejection, and is adopted.** The observation that an
error-carrying retry is a correction signal rather than sampling is correct, and it is
why the repair loop exists at all — in the golden-set harness, where the
`ValidationError` reaches the prompt author. What was rejected is running that loop
per-call, unattended, against production quota, in a code path that was never designed
to make trading decisions.

### Alternative 2 — repair the response in the parser rather than re-asking

Strip a markdown code fence, extract the first JSON object with a regex, lowercase a
`Literal`. No extra call, no quota spend, and it fixes the most common failure shapes.

Rejected outright, and more firmly than Alternative 1. Every act of parser repair is a
decision made by *our parser* rather than by the model, under no schema, with no audit
trail. The failure is not that it guesses wrong once — it is that the guesses accumulate
into an undocumented second decision layer that nobody reviews. A regex that rescues a
JSON object out of prose will, given a model that echoed a fenced document back, happily
capture an object embedded in an untrusted news headline.

### Alternative 3 — make the attempt count configurable with a default of 0

Rejected because a default is a value someone overrides. The whole point of `Literal[0]`
is that `mypy` refuses the override, so the setting cannot be raised in a `.env`, in a
TOML file, or by an operator at 1am. This is the same mechanism as the compiled-in host
allowlist and the compiled-in agent ceilings, applied to the same class of hazard.
