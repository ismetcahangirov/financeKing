---
description: Add an LLM agent with an explicit forbidden-decision list, typed IO, token budget, timeout, and escalation path
argument-hint: <agent-name> "<mission in one sentence>"
allowed-tools: Read, Grep, Glob, Bash, Write, Edit
---

Add agent `$1` with mission: $2

Agents sit **on top of** the deterministic core, never inside it. An LLM in the order path is an unbounded-risk design; an LLM in the hypothesis path is a research accelerator. This must be the second.

## 1. Write the forbidden list first

Every agent has: an explicit mission, allowed decisions, **forbidden decisions**, typed inputs and outputs, a token budget, a timeout, and an escalation path. The forbidden list matters more than the allowed list, so write it first.

Forbidden for every agent in this system, without exception:

- Constructing an `Order`, a quantity, a notional, or a leverage figure.
- Deciding whether a strategy passes validation. Agents propose; the deterministic gate disposes.
- Widening the safety allowlist, or emitting any hostname at all.
- Writing to or amending its own memory history.
- Deciding to promote, retire, or re-arm the kill switch.
- Producing free-form text that any downstream code executes, queries, or path-joins.

Then add the forbidden decisions specific to this agent's mission — the ones that are tempting given what it can see.

## 2. Typed IO, schema-validated

```bash
ls src/fking/agents/
grep -rn "BaseModel\|model_validate" src/fking/agents/ | head -20
```

Define Pydantic v2 input and output models in `src/fking/agents/<snake_name>/schema.py`. The output model is the contract:

- Every field typed and bounded. `conviction: Decimal` with a validator pinning it to `[0, 1]`, not a bare float.
- No free-text field that downstream code branches on. Enums for anything that drives control flow.
- **An unparseable response is a failure, not something to interpret charitably.** No repair-by-regex, no "extract the JSON from the prose". Retry once with the schema restated, then fail and escalate.

## 3. Budgets are structural, not advisory

Free-tier quotas are a real architectural constraint. Gemini free tier is primary, Groq free tier is the fallback, both behind the gateway that owns routing, failover, quota accounting, caching, structured-output enforcement, and full prompt/response audit logging.

Declare in the agent's config:

- `max_input_tokens`, `max_output_tokens` — hard, enforced by the gateway, not by hoping the prompt is short.
- `timeout_seconds`.
- `max_calls_per_cycle`.
- Its scheduling priority, so that under quota pressure the scheduler drops this agent rather than a more important one.

**Quota exhaustion must degrade to deterministic-only operation, not stall.** Write and test the degraded path now; it will be exercised, and it will be exercised at the worst time.

## 4. Never construct a client

The agent talks to the gateway. It does not import an SDK, does not build an `httpx` client, does not read an API base URL. `import-linter` enforces this and you should not need it to.

## 5. Memory tier — pick one, deliberately

Three tiers, and conflating them is the standard failure:

- **working** — ephemeral, within one cycle.
- **episodic** — append-only, Postgres. What happened.
- **semantic** — distilled lessons via `pgvector`. What was learned.

Writes are append-only so the agent cannot rewrite history to flatter itself. If this agent writes semantic memory, state what distillation produces an entry and what would make one wrong.

## 6. If it is a Judge or Critic, make it adversarial

Judge and Critic agents are adversarial **by construction**: their success metric is finding flaws, not agreeing. Language models converge easily by default, and an agent panel that converges easily is worthless.

Concretely: give the critic the candidate without the proposer's rationale, score it on defects found rather than on verdict agreement, and track its historical agreement rate. A critic agreeing above ~80% of the time is broken and should be reported as such.

## 7. Prompt

Put the prompt in the prompt library, versioned, not inline in the code. It must state the mission, the forbidden list verbatim, the output schema, and an instruction to fail explicitly rather than guess when inputs are insufficient.

## 8. Tests

- Golden-response tests against recorded real provider responses, not hand-written ones.
- A malformed-response test: truncated JSON, prose wrapper, an extra field, a null in a non-optional field. Each must fail cleanly and escalate.
- A forbidden-output test: feed it inputs that invite a size recommendation and assert the schema makes that unrepresentable.
- A quota-exhaustion test: gateway returns quota-exceeded, and the system degrades to deterministic-only rather than blocking.
- Determinism: temperature pinned and seed injected where the provider supports it; otherwise assert on the schema and bounds rather than exact text.

```bash
make check
```

## 9. Report

Mission, forbidden list, IO schemas, budgets, memory tier, degraded-mode behaviour, and test results.
