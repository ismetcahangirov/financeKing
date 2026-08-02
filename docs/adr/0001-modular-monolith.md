---
number: 0001
title: Modular monolith with statically enforced boundaries, not microservices
date: 2026-08-03
status: accepted
deciders: [ismetcahangirov, architect, cto]
supersedes: null
superseded_by: null
related_issues: ["#10", "#16", "#1"]
related_adrs: [ADR-0010]
---

## Context

Six stages have to run on a loop forever without supervision — acquire data, form hypotheses, generate strategies, validate them, execute the survivors, evaluate and feed back. Each is a plausible service boundary, and drawing them as services is the reflex answer.

```
Forces:
- The stages are genuinely separable, and the seams between them are where
  correctness lives: strategy must not reach the order path, execution must not
  construct its own HTTP client. Those boundaries have to be real, not polite.
- There is one developer, one machine, and zero budget. Independent scaling,
  independent deployment and team autonomy -- the three things microservices are
  for -- have no claimant here.
- Position state must be agreed on by risk, execution and the reconciler at every
  instant. A boundary between them is a place where two components can hold
  different beliefs about how much BTC we are long.
- Any trade must be reconstructable end to end months later (ARCHITECTURE.md 11).
  Inside one process a correlation ID is a context variable; across processes it
  is a distributed-tracing problem.
- The system will eventually write parts of itself via LLM agents, so boundaries
  that depend on the author having read a document will be crossed.

The constraint that forces a decision now:
The module layout is the first thing #10 lays down, and every subsequent issue
imports against it. Choosing later means moving every import in the repository.
```

## Decision

**We build a single Python process organised into strictly bounded modules under `src/fking/`, with the boundaries enforced by `import-linter` contracts in `pyproject.toml` rather than by convention.** Dependencies point inward toward `fking.domain`, which imports nothing but the standard library; `fking.platform` sits below the layering and is importable by anyone. The Next.js dashboard is the one separate deployable, and it talks to the monolith over HTTP (ADR-0002). This decision covers process topology and the mechanism that enforces it; it does not fix the module list, which changes as the system grows, and it does not preclude extracting a module later.

## Alternatives considered

### Alternative 1 — microservices, one per stage (strongest rejected)

**What it would have given us.** The boundaries would be physically impossible to cross rather than merely checked: `strategy` could not import `execution` because `execution` would not exist in its address space, which is a stronger guarantee than any linter provides. Each stage could be restarted, redeployed and rolled back alone — attractive for a system whose agent layer will be rewritten far more often than its execution layer. Backtest sweeps are embarrassingly parallel and would scale horizontally without touching the trading path. And the failure isolation is real: an OOM in a Monte Carlo run cannot take the order manager down with it, which is not true in a monolith.

**Why it lost.** The cost is paid in the exact place this system cannot afford it. Position state has to be agreed on by risk, execution and reconciliation continuously, because exchange state is the source of truth and local state converges to it (`ARCHITECTURE.md` §7). Splitting those three across a network turns every disagreement into a distributed-consensus question, and the disagreements are not hypothetical — Binance spot testnet wipes roughly every 30 days with keys intact (VF-005), so the reconciler regularly discovers that everything it believed is false. Resolving that inside one process is a function call against one object graph. Resolving it across three services is a protocol, and a protocol written by one person under time pressure to handle a monthly state wipe is where the real bugs would live.

The second cost is diagnostic. `ARCHITECTURE.md` §11 requires that a trade be fully reconstructable from the audit log alone. In one process, the correlation ID minted at bar close is a `contextvar` that survives every `await` and every `TaskGroup` child for free. Across services it is a header that each hop must propagate correctly, and the hop that forgets is discovered during an investigation, which is exactly when the trail matters.

Third, the benefits are claims about a future that has no schedule. There is no second developer to gain autonomy, no traffic to scale for, and the parallelism argument applies to backtests, which are batch jobs that can be parallelised with processes inside one deployable.

**What survives the rejection, and is adopted.** The strongest part of the argument is that a boundary you can only cross by choosing to is better than one you cross by accident. That is why the boundaries are `import-linter` contracts run in CI rather than a diagram: `strategy -> execution` fails the build with the offending line number, and `exhaustive = true` means a newly created top-level module fails until someone places it in the layer order. The guarantee is weaker than address-space separation, and it is checked on every commit rather than believed.

### Alternative 2 — a monolith with modules but no enforcement

**What it would have given us.** All of the above with none of the CI configuration, and no fights with a linter over an import that is "obviously fine". Boundaries maintained by review and a clear directory layout, which is how most codebases work and is not obviously worse.

**Why it lost.** The reviewer will frequently be a language model with no memory of this document, and the author will often be one too. An LLM-authored strategy will import the order manager and size its own position, with a comment explaining why this case is different (`ARCHITECTURE.md` §5), and it will pass a human skim because the code looks reasonable. A convention that depends on the author having read `RISK_PHILOSOPHY.md` is not a constraint on a system that generates its own authors. Enforcement is also what makes the extraction claim honest: modules whose boundaries were never checked have hidden coupling, so "extraction is mechanical later" would be a comforting fiction rather than a property.

### Alternative 3 — do nothing

```
Cost of the status quo: #10 cannot land, and every issue in P0-P7 imports
against a layout that does not exist. There is no code to preserve, so the
"status quo" is an empty src/ directory.
Why that is no longer payable: the layout is a prerequisite for all 100+
downstream issues, not one of them.
```

## Consequences

**What becomes easier**
- Refactoring across boundaries is one commit with one test run — no version negotiation, no compatibility window, no coordinated deploy.
- A stack trace answers questions that would otherwise need a trace viewer, and the correlation ID propagates through `contextvars` without any module cooperating.
- Local development is `make up` plus one process; the whole system runs on a laptop, which is what makes the zero-budget assumption hold (`ARCHITECTURE.md` §13).
- Extraction stays available: the seams are proven by CI rather than assumed, so a module that must move can move.

**What becomes harder**
- Every new top-level module must be placed in the layer order before CI passes (`exhaustive = true`), including throwaway ones. That friction is deliberate and it will be felt.
- One process means one memory budget and one GIL. A backtest sweep and the live loop compete, which is why #109 exists as a performance issue rather than as a scaling one.
- Third-party imports must be routed rather than reached for: `execution` importing `httpx` is a contract violation even when the call is harmless, because the contract cannot tell harmless from the start of a bypass (ADR-0006).

**What we now cannot do**
- Deploy or restart one stage without restarting all of them. A prompt change and a risk-limit change ship in the same process, so the agent layer cannot iterate faster than the trading core. Reopening this means extracting a module — which the boundaries make mechanical, but which still costs a network protocol, a deployment target and a distributed trace.

## What would make us revisit this

```
Trigger:   Peak resident set of the single process exceeds 70% of the host's
           RAM for three consecutive days, OR a backtest sweep and the live
           loop contend such that p99 signal-to-order latency exceeds 2s.
Observed:  Grafana panels `process.resident_memory_bytes` and
           `risk.decide.duration_seconds` p99.
Then:      Extract `backtest` first -- it is batch, it is the memory hog, and
           it holds no position state -- in a superseding ADR.
```

## Verification

```
Confirmed if:  `lint-imports` reports zero broken contracts on every merge to
               main through 2027-02-01, with no contract deleted or weakened
Refuted if:    any contract is relaxed to unblock a feature, or the extraction
               trigger above fires
Checked by:    cto agent, via `make imports` and the contract diff on each PR
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
- [x] Linked from #16 and from `.claude/knowledge/decisions-log.md` (D-006, D-029)
