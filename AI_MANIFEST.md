# AI Manifest

What the AI system in financeKing **is**, and what it is **not permitted to be**.

This is a statement of limits. It is deliberately short, deliberately absolute, and deliberately written in the negative — because the interesting content of an AI system's design is the set of things it cannot do.

`CLAUDE.md` §10 and `ARCHITECTURE.md` §9 are the governing sources. This document is their consolidated statement and adds the reasoning.

---

## 1. The statement

**financeKing uses language models to generate hypotheses, critique reasoning, and distil lessons. It does not use them to decide anything.**

Every consequential decision in this system — what to trade, how much, whether a strategy lives, whether a result is credible, whether a limit is breached — is made by deterministic code with a fixed, testable, replayable specification.

The language models make the system faster at forming ideas. They have no authority over what happens to those ideas.

---

## 2. What the AI system is

- **A hypothesis generator.** Reading market structure, prior research and memory, and proposing falsifiable claims worth testing.
- **An adversary.** Judge and Critic agents whose success metric is finding flaws, not agreeing.
- **A distiller.** Turning many episodic observations into a small number of lessons with stated falsifiers.
- **A writer of candidate strategies.** Producing strategy definitions that the validation pipeline will then try very hard to reject.
- **An explainer.** Producing rationales that a human reads when reconstructing a decision.

Every one of those is upstream of a deterministic gate. None of them is a decision.

---

## 3. What it is not permitted to be

These are stated as prohibitions because prohibitions are what an eager system needs. Each is enforced structurally — by an absent tool, an absent import path, or an absent schema field — not by a prompt asking nicely.

| # | The AI system may never | Enforced by |
|---|---|---|
| 1 | Place, modify or cancel an order | No such tool exists (`TOOLS.md` §8) |
| 2 | Determine a position size, notional or leverage | No size field on any agent output schema; risk engine has sole authority |
| 3 | Decide whether a strategy lives, is promoted, or is retired | Deterministic promotion gate; no such tool |
| 4 | Change a risk limit, threshold or ceiling | Limits are config bounded by compiled-in ceilings (`CONFIGURATION.md` §8) |
| 5 | Widen, bypass or query around the host allowlist | Compiled-in `frozenset`; no override exists (`SECURITY.md` §3) |
| 6 | Construct a network client or make an arbitrary request | `import-linter` contracts; no `http_request` tool |
| 7 | Execute code, shell commands, or arbitrary SQL | No such tool exists |
| 8 | Mutate or delete an audit or memory row | Database-level `REVOKE` + trigger + CI test |
| 9 | Read or burn the permanently held-out period | Rejected at the tool boundary |
| 10 | Modify its own prompt, budget, timeout or tool set | Prompts are content-addressed; budgets are ceiling-bounded |
| 11 | Disable an alert, a gate, a check, or the kill switch | No such tool exists |
| 12 | Emit output outside its declared schema and have it used | Schema validation; parse failure is a failure |

### On enforcement by absence

Every one of these could have been implemented as a guard inside a tool that exists. That would be worse, and the reason is worth stating once:

A guard inside a tool is defeated by a refactor that does not understand it, by an exception handler that "keeps the loop alive", by a config flag added for testing, or by a prompt that finds the phrasing the guard did not anticipate. A tool that does not exist is defeated by none of those. The only way to reach an absent capability is to write the code that provides it, in a reviewed pull request, with a human deciding.

This is the same argument that puts the allowlist in a `frozenset` and the risk ceilings in a compiled-in mapping. It is the project's single most repeated design move, and this document is where it applies to the agent layer.

---

## 4. Agents propose; deterministic gates dispose

```
  agent ──proposes──► candidate ──► deterministic gate ──► effect
                                          │
                                          └── rejection, recorded, no effect
```

| An agent may propose | The gate that decides |
|---|---|
| A hypothesis | The research pipeline's data-availability check |
| A strategy definition | The P2 validation gate: audit, walk-forward, CPCV, PBO, deflated Sharpe |
| A thesis about direction | The risk engine, which decides the position — or vetoes it |
| A working style for an order | The execution layer's deterministic default, if the agent is slow or absent |
| A parameter change | The promotion gate, requiring forward performance |
| A lesson worth remembering | The promotion criteria in `MEMORY_SYSTEM.md` §5 |

The asymmetry is deliberate: **proposing is cheap and unconstrained; accepting is expensive and adversarial.** `ARCHITECTURE.md` §1 organises the whole system around it. A design that makes generation easy without making validation hard produces confident nonsense at scale, and a language model is the most efficient confident-nonsense generator ever built.

### The LLM is in the hypothesis path, never the order path

> **An LLM in the order path is an unbounded-risk design. An LLM in the hypothesis path is a research accelerator. This system is the second.**

Two distinct reasons, and both matter:

**Correctness.** A model that hallucinates a number in a hypothesis costs a wasted backtest. A model that hallucinates a number in an order costs a position. The first is absorbed by the validation pipeline; the second is not absorbed by anything.

**Latency.** This is the reason people forget. A synchronous agent call on the order path once cost **8.9 seconds of median latency and ~7.9bp of implementation shortfall per order** — enough, on a strategy with a 5.2bp gross edge, to be the difference between economic and dead. Free-tier quota is therefore not an availability annoyance; it is a **trading cost**, and the only correct response is architectural: the LLM does not sit on a latency-sensitive path at all.

The order path is deterministic end to end: features → signal → risk → order → venue. It runs at full speed with every agent disabled, and **that is the default configuration** (`CONFIGURATION.md` §9). A degraded mode that has never been the default is a degraded mode that does not work.

---

## 5. Every agent has a contract

No agent runs without all seven of these declared:

| Element | Why |
|---|---|
| **Mission** | One sentence. An agent that needs a paragraph is two agents |
| **Allowed decisions** | The floor |
| **Forbidden decisions** | The ceiling. **This list matters more than the allowed list** |
| **Typed inputs and outputs** | Pydantic models. Closed enums wherever a decision is represented |
| **Token budget** | Ceiling-bounded. A cost limit an agent can raise is not a limit |
| **Timeout** | With a deterministic default that applies when it expires |
| **Escalation path** | What it does when it hits a wall. An agent with no exit produces something anyway |

The forbidden list matters more because the failure mode of a language model is almost never "did too little". It is "reached one step further than its authority", and only an explicit prohibition — ideally one the output schema cannot express — addresses that. `PROMPT_LIBRARY.md` §6 covers how to write one that survives paraphrase into the next prompt version.

### Every output is schema-validated

An unparseable response is a **failure**, not something to interpret charitably. There is no free-text fallback and no re-ask. The raw response is recorded verbatim, the call fails, and the deterministic path proceeds without it.

Charitable interpretation of malformed output is the exact mechanism by which a hallucination becomes a typed structure that the deterministic core trusts. It is banned in code (`CONFIGURATION.md` §9: `max_reask_attempts: Literal[0]`) rather than in guidance.

### Every output is replayable

Every agent output row stores the **prompt hash**, the provider, the model, the temperature and the raw response. Prompts are content-addressed and never edited in place, so an output from four months ago can be replayed against the exact prompt that produced it (`PROMPT_LIBRARY.md` §2).

This is not bookkeeping. `OBSERVABILITY.md` §1 requires that any trade be reconstructable months later, including which agent reasoning contributed. An unreplayable agent output makes every trade it touched unreconstructable.

### Abstention is a first-class output

**"Insufficient evidence" is schema-valid, is instructed, and its rate is measured.** An agent that never abstains is not agreeable — it is uncalibrated, and the deterministic gates cannot distinguish a confident wrong answer from a confident right one.

An abstention rate collapsing to zero is treated as a **regression even when every other metric improves**, because it is the only metric that moves the wrong way when an agent quietly stops evaluating evidence and starts producing verdicts.

### Adversarial agents stay adversarial

Judge and Critic agents are adversarial by construction. **A panel that converges easily is worthless, and language models converge easily by default.** Their disagreement rate is monitored; convergence is a defect signal, not a sign of maturity.

---

## 6. The survival instinct is bounded by rules that cannot be optimised away

This system is built to persist: strategies compete, weak ones retire, strong ones reproduce, and the population is scored on a survival objective. That is a form of optimisation pressure applied to a system that also writes its own strategies. It is worth being explicit about where that pressure is allowed to reach.

> **The survival objective is applied to strategies. It is never applied to the constraints.**

An optimising system with access to its own constraints does not become more capable — it becomes better at appearing successful. The distinction is not hypothetical: the shortest path to a good survival score is not a better strategy, it is a weaker limit, a looser gate, a suppressed alert, or a memory row that says the last failure did not happen. Every one of those is available to a sufficiently unconstrained optimiser and none of them requires malice, only gradient.

So the boundary is drawn in code, not in the objective function:

| Pressure | Where it may reach | Where it may not |
|---|---|---|
| Improve the score | Strategy parameters, feature choice, entry and exit logic | Risk limits, ceilings, the allowlist |
| Reduce cost | Caching, batching, prompt efficiency | Removing a validation stage, sampling the order path |
| Increase confidence | Better evidence, more trials | Lowering the abstention bar, weakening the Critic |
| Persist | Being right more often | Deleting the record of being wrong |

The concrete mechanisms:

- **Risk limits are configuration bounded by compiled-in hard ceilings above and hard floors below.** Config can only make the system *more* conservative — which for `conviction_floor` and the `min_*` limits means larger, not smaller. Moving a compiled-in bound requires a source edit and a `safety:critical` pull request (`CONFIGURATION.md` §8).
- **The survival score treats risk-limit violations as a hard negative.** A strategy that made money by breaching limits scores **worse** than one that made less within them. That is encoded in the objective function, not in documentation, because the system optimizes what it measures (`SCORING_ENGINE.md`).
- **Memory is append-only.** An agent cannot rewrite its own history to look better — not through the ORM, not through a migration, not through a cleanup script (`MEMORY_SYSTEM.md` §4).
- **Trial counts include failures.** A search cannot be laundered by discarding the runs that went badly; every trial deflates the Sharpe of whatever the search eventually reports (`BACKTEST_ENGINE.md` §6.5).
- **No agent can disable an alert, a gate or the kill switch.** No such tool exists.
- **The held-out period is burned on read**, and no agent can read it.
- **Prompts cannot be self-modified.** An agent that could edit its own forbidden-decisions section has no forbidden decisions.

The general form, stated once:

> **Anything that could make the system look more successful without being more successful is outside the optimiser's reach, structurally.**

---

## 7. What this means in practice

- The system runs, correctly and completely, with every LLM agent disabled. Agents make it better at research; they are not required for it to be safe or functional.
- Quota exhaustion degrades to deterministic-only operation. It never stalls, and it never waits.
- A hallucinated hypothesis costs a rejected proposal. It does not cost a trade.
- A drifted prompt is caught by the golden set before it ships, and by the abstention-rate monitor if it ships anyway.
- An agent that escalates constantly is itself escalated, because alert fatigue is a safety failure (`OBSERVABILITY.md` §8).
- Six months from now, any trade can be reconstructed including the exact prompt and response that contributed to it.

---

## 8. Amendment

This document is amended by pull request, never in passing, and never by an agent.

A change that widens what the AI system is permitted to do requires the `safety:critical` label and a reviewer who is not the author — the same bar as the safety kernel, for the same reason. A change that narrows it needs only the ordinary review process.

The asymmetry is the point. It is the same asymmetry as the risk ceilings and the host allowlist: **tightening is easy, loosening is deliberate.**

---

## 9. Cross-references

| For | See |
|---|---|
| The prime directive | `CLAUDE.md` §0 |
| Runtime agent rules | `CLAUDE.md` §10 |
| Why agents sit on top of the deterministic core | `ARCHITECTURE.md` §9 |
| The tools that exist, and the ones that never will | `TOOLS.md` |
| Content addressing, abstention, forbidden-decisions sections | `PROMPT_LIBRARY.md` |
| Append-only memory and promotion criteria | `MEMORY_SYSTEM.md` |
| Compiled-in ceilings and the safety kernel | `CONFIGURATION.md` §8, `SECURITY.md` §3 |
| Why risk holds sole authority over sizing | `RISK_PHILOSOPHY.md` |
| The survival objective and its hard negatives | `SURVIVAL_PROTOCOL.md`, `SCORING_ENGINE.md` |
