---
number: 0009
title: Gemini free tier primary with Groq fallback, reachable only through one gateway
date: 2026-08-03
status: accepted
deciders: [ismetcahangirov, architect, prompt-engineer, scheduler]
supersedes: null
superseded_by: null
related_issues: ["#19", "#16"]
related_adrs: [ADR-0001]
---

## Context

The agent layer researches markets, forms hypotheses, judges proposals and drafts mutations. It sits on top of the deterministic core and never inside it (`ARCHITECTURE.md` §9), but it still needs a model provider, and the budget for one is zero.

```
Forces:
- Zero budget, permanently. Free tiers are therefore an architectural
  constraint rather than a starting condition (ARCHITECTURE.md 13).
- Free-tier quota is shared, exhaustible and non-refundable, with a hard
  cliff. Dozens of scheduled agent invocations per day compete for it.
- The system runs unattended. A provider outage or a quota exhaustion at 03:00
  must degrade to something, not stall and not fill the log with one exception
  per scheduled agent per beat.
- Judge and Critic agents are adversarial by construction and are supposed to
  disagree. Caching one agent's answer and serving it to a sibling with an
  identical prompt makes a panel converge for free, which is the failure the
  panel exists to prevent.
- Every prompt, response, model id, provider, temperature, token count and
  latency has to reach the append-only audit log, because a trade must be
  reconstructable months later including which agent reasoning contributed
  (ARCHITECTURE.md 11).
- The published free-tier numbers are unverified. That research was cut short
  by a session limit on 2026-08-01 and is tracked as OQ-001 / #19.

The constraint that forces a decision now:
Every agent in the project calls something. If that something is a provider
SDK imported at the call site, quota accounting, failover, caching and audit
logging have no single place to live and will be reimplemented per agent.
```

## Decision

**We use the Gemini free tier as the primary provider and the Groq free tier as the fallback, and `src/fking/agents/gateway/` is the only module in this repository permitted to import a provider SDK.** The gateway owns routing, failover, persistent quota accounting in Postgres, response caching at temperature zero only, structured-output enforcement, and prompt/response audit logging. Quota is **reserved before a call and reconciled to actual usage after**; exhaustion returns a `Degraded` value that callers handle by taking their deterministic path, rather than raising. Configured limits may only *lower* the effective limit — the gateway takes `min(configured, HARD_CEILING)` against a compiled-in ceiling, so a config edit cannot raise a quota. Model ids are pinned (`gemini-2.5-flash-002`, not a floating alias). Which specific limits apply is measurement, not vendor documentation: the ledger's own history brackets the real numbers (#19).

## Alternatives considered

### Alternative 1 — one provider, called directly from each agent (strongest rejected)

**What it would have given us.** No indirection. An agent that needs a completion calls the SDK, and the code reads exactly like every example in the provider's documentation — which matters when the code is being written and read by language models whose training data is full of that shape. No gateway to maintain, no reservation protocol to get right, no second provider's quirks to absorb, and no abstraction that must be correct before any agent can be written. Given `CLAUDE.md` §3's rule that an abstraction needs two concrete callers before it exists, a gateway written before the first agent is exactly the speculative abstraction the rule forbids.

**Why it lost.** The gateway is not an abstraction over providers; it is the **admission-control point**, and admission control cannot be distributed. Free-tier quota is a single shared exhaustible pool: if eight agents each check "is there quota left?" before their own call, all eight see plenty and all eight fire, and the day's budget is gone by 09:14. Correct behaviour requires reserving an estimate inside the same atomic statement that checks the limit, which requires one writer to one ledger. That is a property of the resource, not a design preference, and it does not survive being copied into eight call sites.

Three further properties have the same shape. Quota state must be **persistent**, because the moment you are most likely to restart is the moment a provider started returning 429 and something crashed in response — an in-memory counter is zeroed by exactly the restart the rate limiting caused. Audit completeness must be **total**: `ARCHITECTURE.md` §11 requires the verbatim prompt and response for every call, and a per-agent call path means one agent forgetting is a hole in exactly the reconstruction an investigation needs. And the two-callers rule is satisfied on day one anyway, because there are two providers.

**What survives the rejection, and is adopted.** The objection that a gateway is speculative is answered by keeping it thin and refusing to make it a prompt framework: it routes, accounts, caches, validates and audits. It does not own prompt construction, retry-with-repair, or agent orchestration. In particular there are **zero re-asks at runtime** — a schema failure fails the call, because a retry loop over a stochastic generator searches for a response that passes validation rather than one that is correct, and it also suppresses the parse-failure rate that is the instrument telling you the prompt is wrong.

### Alternative 2 — a paid provider, or a local model on the development machine

**What it would have given us.** A paid API removes quota as an architectural constraint entirely: no reservation protocol, no priority classes, no degraded mode, no scheduler shaped around a daily budget. Perhaps a third of the gateway's complexity disappears. A local model (Ollama, llama.cpp) removes the network and the rate limit as well, and keeps every prompt on the machine.

**Why it lost.** Paid is out of scope by the project's own definition — zero budget is a standing constraint (`ARCHITECTURE.md` §13), not a phase. Local is more interesting and fails on the machine: the same host runs Postgres, Redis, the observability stack and backtests that ADR-0001 already flags as the memory risk, and a model large enough to produce usefully adversarial critique would contend with the backtest engine for exactly the resource that bounds how much validation is affordable. A small local model that fits alongside everything else produces critique that agrees, which is worse than no critique — an agent panel that converges easily is worthless, and language models converge easily by default (`CLAUDE.md` §10).

### Alternative 3 — do nothing (no agent layer; deterministic system only)

```
Cost of the status quo: the deterministic core -- data, backtest, risk,
execution, evolution -- is complete without agents, so "do nothing" is
genuinely cheap here, unlike in most of these ADRs. What is lost is
hypothesis generation: strategies would come from a human writing them, and
the evolution engine would mutate parameters within a fixed strategy space
rather than search a growing one.
Why that is no longer payable: the project's stated purpose is a system that
decides what rules should exist, not one that executes rules it was given
(CLAUDE.md 1). Without a hypothesis layer it is a well-validated backtesting
framework, which is a different and smaller thing.
```

## Consequences

**What becomes easier**
- Quota exhaustion is a designed state rather than an error path: `Degraded` is a value, callers fall back to deterministic behaviour, and the log stays readable at the moment it matters most.
- Swapping a provider is one adapter plus one `ignore_imports` line in the `import-linter` contract — a reviewable diff, which is the point of the allowlist shape.
- Audit completeness is structural. Every call passes one function, so there is no agent that can forget to log its prompt.
- The real free-tier limits become measurable: the ledger records every 429 with the window that was live and the totals at that moment, so `observed_limits` brackets the truth from above and below without trusting a vendor page (#19).

**What becomes harder**
- Every agent call needs a reservation handle before it can reach a provider, and reconciliation after. That protocol is more code than `client.generate(prompt)` and it must be correct under concurrency.
- Caching is restricted to `temperature == 0`, so any sampled call pays full quota every time. That is the cost of keeping adversarial panels genuinely independent.
- Two providers means two response shapes, two error taxonomies and two sets of quirks behind one interface, plus the risk that the interface flatters whichever was implemented first.
- Configured limits cannot be raised in an emergency, only lowered. A stalled research run at 01:00 has no fast path, deliberately.

**What we now cannot do**
- Call a model from anywhere other than the gateway — no quick script under `src/`, no direct SDK call in a notebook that later becomes a module. Reopening that would put an unaccounted call against a shared exhaustible budget and an unlogged prompt into a system that claims every prompt is reconstructable.

## What would make us revisit this

```
Trigger:   Gemini free-tier availability, as measured by the ledger's own
           429-and-error rate, falls below 90% of attempted calls over any
           rolling 7 days, OR either provider withdraws its free tier.
Observed:  `fking_agent_calls_total` versus `fking_agent_provider_errors_total`
           by provider, and the observed_limits view.
Then:      Promote Groq to primary, or add a third provider as a new adapter
           plus one ignore_imports line, in a superseding ADR. The gateway
           shape is what makes that a small change and is not itself reopened.
```

## Verification

```
Confirmed if:  no scheduled agent run is lost to quota exhaustion without a
               recorded Degraded result and a deterministic fallback having
               executed, and the quota ledger survives every process restart,
               measured by 2027-02-01
Refuted if:    any module outside src/fking/agents/gateway/ imports a provider
               SDK, or an AGENT_SCHEMA_REPAIRS counter appears (a re-ask was
               reintroduced), or a call reaches a provider without a
               reservation
Checked by:    prompt-engineer and scheduler agents, via `make imports`, the
               gateway tests against real Postgres, and the ledger's
               restart-survival test
Review date:   2027-02-01
```

## Definition of done

- [x] `number` is the next unused value in `docs/adr/` and the filename matches `NNNN-<kebab-slug>.md`
- [x] Context names one constraint that forces a decision
- [x] Decision is one paragraph, active voice, and names the owning module
- [x] The strongest rejected alternative is argued at its strongest, and the part of it that was correct is adopted rather than discarded
- [x] "Do nothing" is costed
- [x] All three Consequences lists are non-empty, including what we now cannot do
- [x] The revisit trigger is observable without judgement and names where it is observed
- [x] Verification states both a confirming and a refuting value, with a date and an owner
- [x] Linked from #16 and from `.claude/knowledge/decisions-log.md` (D-018, D-019)
