# Workflow — Agent Onboarding

Adding an LLM agent to the runtime. Agents sit **on top of** the deterministic core, never inside it: an LLM in the order path is an unbounded-risk design; an LLM in the hypothesis path is a research accelerator.

---

## 1. Justify the agent

What decision does it inform that deterministic code cannot? If the answer is "it would be faster", write the deterministic version — it will also be cheaper, reproducible, and not subject to a free-tier quota.

Valid roles: hypothesis generation, research synthesis, adversarial critique, anomaly narration, post-mortem drafting.

Invalid roles: anything in the order path, anything that decides a gate outcome, anything that decides promotion or retirement.

---

## 2. Write the forbidden list before anything else

Every agent has an explicit mission, allowed decisions, **forbidden decisions**, typed IO, a token budget, a timeout, and an escalation path. The forbidden list matters more than the allowed list.

Universally forbidden, no exceptions:
- Constructing an `Order`, quantity, notional, or leverage
- Deciding whether a strategy passes validation
- Emitting a hostname, or touching the safety allowlist
- Amending its own memory
- Promoting, retiring, or re-arming the kill switch
- Producing text that downstream code executes, queries, or path-joins

Then the mission-specific ones — the temptations this particular agent will face given what it can see.

---

## 3. Schema first

Run `/add-agent <name> "<mission>"`.

Pydantic v2 models for input and output. The output schema is the real contract: bounded types, enums for anything driving control flow, no free-text field that code branches on.

**An unparseable response is a failure, not something to interpret charitably.** No regex repair, no "pull the JSON out of the prose". One retry with the schema restated, then fail and escalate.

Design the schema so forbidden outputs are **unrepresentable**, not merely rejected. If the agent has no field in which to express a position size, it cannot propose one.

---

## 4. Budgets and quota

Gemini free tier primary, Groq free tier fallback, both behind the gateway that owns routing, failover, quota accounting, caching, structured-output enforcement, and full prompt/response audit logging.

The agent never constructs a client, never imports a provider SDK, never reads an API base URL. It talks to the gateway.

Declare and enforce: `max_input_tokens`, `max_output_tokens`, `timeout_seconds`, `max_calls_per_cycle`, and a scheduling priority so the scheduler knows which agent to drop first under quota pressure.

**Free-tier quotas are an architectural constraint, not a budget line.** Quota exhaustion must degrade to deterministic-only operation, never stall the loop. Write that path now and test it — it will be exercised, at the worst possible time.

---

## 5. Memory tier — choose deliberately

- **working** — ephemeral, one cycle
- **episodic** — append-only Postgres; what happened
- **semantic** — distilled lessons via `pgvector`; what was learned

Conflating them is the standard failure. Writes are append-only so the agent cannot rewrite history to flatter itself. If it writes semantic memory, state what triggers a distillation and what would make one wrong.

---

## 6. If it is a Judge or Critic, build it adversarial

Judge and Critic agents are adversarial **by construction** — their success metric is finding flaws, not agreeing. An agent panel that converges easily is worthless, and language models converge easily by default.

Concretely:
- Give the critic the candidate **without** the proposer's rationale
- Score it on defects found and confirmed, not on verdict agreement
- Track its historical agreement rate; above ~80% agreement it is broken, and the system should say so rather than continuing to consult it

---

## 7. Prompt

Versioned in the prompt library, never inline in code. States the mission, the forbidden list verbatim, the output schema, and an instruction to fail explicitly rather than guess when inputs are insufficient.

Prompt changes are code changes: reviewed, versioned, and attributable. An agent whose behaviour changed for reasons nobody can reconstruct is worse than no agent.

---

## 8. Test before wiring it in

- Golden responses from **recorded real** provider responses, not hand-written
- Malformed-response cases: truncated JSON, prose wrapper, extra field, null in a required field — each fails cleanly and escalates
- Forbidden-output case: inputs that invite a position-size recommendation, asserting the schema makes it unrepresentable
- Quota-exhaustion case: degrades to deterministic-only rather than blocking
- Timeout case: escalation path fires and is audited

```bash
make check
```

---

## 9. Shadow before trusting

Run it for a full cycle with its output **logged and ignored**. Compare its proposals against what the deterministic gates would have done. An agent whose proposals the gates reject 100% of the time is producing noise; one whose proposals the gates accept 100% of the time is producing nothing the gates did not already know.

---

## 10. Wire it in

Its output feeds a **gate**, never an action. The validation gate decides whether a proposed strategy lives; the risk engine decides the position; the promotion gate decides whether a parameter applies.

If wiring it in requires a human to hand-approve its output, the gate is missing — file that issue instead of adding the approval step.

---

## 11. Watch quota and agreement

After enabling: quota consumption against the free-tier limit, escalation frequency, parse-failure rate, and — for critics — agreement rate. All four are leading indicators; the alert goes on them, not on the agent having already stopped.
