# Decision Framework

How to choose between options in this project.

`CLAUDE.md` §8 says what to decide yourself and what to ask about. This document is the method behind that split, and the harder cases it does not cover.

---

## 1. Reversibility is the primary axis

Sort every decision into one of two categories before doing anything else.

**Two-way door.** You can undo it. Decide in minutes, alone, and move. Do not write a document. Do not present options. Do not ask. Presenting a menu for a reversible decision transfers work upward and produces a worse outcome than just picking, because you have the context and the person you asked does not.

**One-way door.** Undoing it costs more than making it did. Slow down, write it down, and usually ask.

Most decisions are two-way doors and are treated as one-way doors out of caution, which is the most common way engineering time is wasted. The correct default for an unclassified decision is *two-way, decide now*.

### What makes a door one-way in this project specifically

Generic advice says "database choices and public APIs". Here the list is different:

| One-way | Why |
|---|---|
| Anything that appears in `domain` type signatures | Every module depends inward on `domain`. Changing a domain type is a change to every caller, and `domain` is what the other nine modules agree about. |
| Anything written to an audit table | Audit tables are append-only, enforced by the database. A badly shaped audit row is permanent. You can add a column; you can never fix the millions of rows already written in the old shape. |
| Anything that burns a scarce resource | The held-out vault (`EVOLUTION_ENGINE.md` §5.3), the global trial counter `K`, and calendar time itself. These do not replenish on demand. |
| Anything that changes the objective function | The population breeds against whatever the scoring engine rewards. Reverting the code does not revert the population. |
| Data ingestion and normalisation semantics | A normalisation bug does not corrupt the pipeline; it corrupts the *stored history*, and every backtest ever run on it. |
| Adopting a framework rather than a library | See §4. |

Note what is *not* on that list: the database engine, the web framework, the exchange client, the deployment topology, most of the module internals. Those are all replaceable behind interfaces we own, which is the entire reason the module boundaries exist.

### The subtlety: reversibility is measured against feedback latency

A decision is not reversible in the abstract. It is reversible *if you find out it was wrong while it is still cheap to undo*.

Changing a scoring weight is, mechanically, editing a config value — the most reversible action imaginable. But you cannot tell whether the change was correct until the validation-versus-forward rank correlation has accumulated enough pairs, which takes two quarters (`SCORING_ENGINE.md` §6). By then the evolution engine has run tens of thousands of trials optimising against the new weight and the population has drifted. The config change is trivially reversible; its consequences are not.

So the real test is:

```
Is (time to detect the mistake) shorter than (time for committed state to accumulate)?
```

If yes, two-way door. If no, one-way door regardless of how easy the edit is to revert. This is why scoring changes carry the same review level as risk-limit changes despite being, superficially, just numbers in a file.

---

## 2. Cost of being wrong

For decisions that are genuinely close, the ranking metric is not probability of being wrong. It is:

```
expected cost  ≈  P(wrong)  ×  cost per unit time  ×  time to detect
```

The third term dominates and is the one nobody estimates. Some detection latencies in this system, measured honestly:

| Mistake | Detected by | Latency |
|---|---|---|
| Type error | `mypy --strict` | seconds |
| Wrong order quantity | Property test / reconciliation | minutes |
| Exchange response misparsed | Recorded-response test / runtime validation | minutes to hours |
| Risk limit misconfigured | Startup clamp warning, or the limit binding | hours to weeks |
| Cost model calibrated on testnet | Forward-versus-backtest divergence | weeks |
| Scoring weight wrong | Rank correlation / haircut drift | 2 quarters |
| Look-ahead bias in a feature | The adversarial test, **or never** | ∞ |

Look-ahead is at the bottom of that table and it is the reason the adversarial leak test is not optional. A defect with unbounded detection latency has unbounded expected cost no matter how small its per-unit-time damage, and the only way to bound it is to build a detector. **When a decision creates a failure mode with no detector, the decision is not "choose A or B" — it is "build the detector first".**

That reframing resolves a surprising number of arguments. If two options are close and one has a cheap detector, take that one and build the detector; the comparison is now empirical instead of rhetorical.

---

## 3. When to prototype, when to decide on paper

Three kinds of uncertainty, and only one of them is answered by writing code.

**Empirical uncertainty — prototype.** "Does `ccxt` expose the futures user-data stream the way the docs claim?" "What does the spot trade CSV actually contain?" "How long does a CPCV run take on three years of 1-minute bars?" These have answers that exist independently of what we want, and no amount of reasoning produces them. Prototype, timeboxed to 90 minutes.

If the timebox expires without an answer, the question was not empirical — you have been designing, not measuring. Stop and go to the next category.

**Structural or value uncertainty — decide on paper.** "Should the risk engine sit in the order path or beside it?" "Should conviction scale notional or the risk budget?" "Is a violation recorded when it was blocked?" A prototype cannot answer these because they are questions about what we want the system to guarantee. Building both and looking at them tells you which was easier to build, which is not the question. Write the argument down instead; if you cannot write the argument, you do not have one.

**Latency-bound uncertainty — do neither.** "Does this strategy work?" looks empirical and is not, because the answer requires forward time you have not spent yet. Prototyping it produces a backtest, which burns a trial, raises `SR*` for every strategy in the population, and returns a number you already know not to trust (`EVOLUTION_ENGINE.md` §4). Recognising this category is the difference between a research process and an expensive random number generator. The correct move is to *start the clock* — get the candidate into paper — not to search harder.

**Prototype hygiene.** Spikes live on a `research/*` branch and are deleted, never merged. A prototype that gets cleaned up and merged is the worst artefact in the repository: it has production status and spike-quality thinking behind it, and the person who reads it in six months has no way to tell. If a spike revealed something worth keeping, write it fresh, test-first, and cite the spike in the commit message.

---

## 4. Weighing a library adoption

The usual checklist — stars, maintenance, licence, downloads — is table stakes and mostly noise. The questions that actually predict pain:

**1. Does it appear in signatures we own?**
If a library's types show up in `domain`, or in a function signature crossing a module boundary, you have not adopted a library. You have adopted its domain model, and that is a one-way door. This is why `NautilusTrader` was rejected despite being technically excellent (`ARCHITECTURE.md` §4): the objection was never quality, it was that adopting it makes the risk and evolution engines plugins to its lifecycle instead of components with authority over it.

**2. Do we call it, or does it call us?**
Libraries you call. Frameworks call you. A framework in the core owns control flow, and control flow is what the architecture is. Frameworks at the edges (FastAPI in `api`, pytest in tests) are fine because the edges are where inversion is harmless.

**3. What is the exit cost, counted in files?**
The rule: **any external dependency must be reachable from at most one module, behind at most one adapter file.** If a proposed dependency cannot satisfy that, it is not a dependency decision, it is an architecture decision, and it needs an ADR. Measured in practice by `grep -rl '^import <lib>' src/ | wc -l`, target 1.

**4. Is its release cadence sane in both directions?**
Too slow is a known risk. Too fast is the one people miss: the official `binance-sdk-*` packages shipped 11 and 16 major versions in roughly twelve months. For an unattended system, a dependency that breaks its API twice a quarter is not a dependency, it is a recurring outage. `binance-connector` being frozen and `python-binance` being broken for spot user data are the more familiar failure shapes; the churning-SDK shape is equally disqualifying and looks like health on a graph.

**5. What does it drag in?**
Transitive dependency count, and specifically whether it pulls a native extension, a CUDA wheel, or a second async runtime. A pure-Python library with 3 dependencies and mediocre quality is usually a better bet than an excellent one that makes the Docker image 4 GB and the CI matrix conditional.

**Synthesis:** a library you can hide behind an adapter is cheap almost regardless of its quality, because replacing it is a one-file change. A library that shapes your types is expensive almost regardless of its quality, because replacing it is a rewrite. Judge the coupling before judging the library.

---

## 5. The two-concrete-callers rule, and its exception

`CLAUDE.md` §3 states it: an abstraction requires two concrete callers before it exists. Three clarifications and one carve-out.

**"Concrete" means merged, in `src/`, and not a test.** Tests do not count, ever. A test is written after the abstraction and shaped to fit it, so using tests as the second caller is circular — you are validating the abstraction against a caller you wrote to validate it.

**"Two" means two that differ.** Two callers doing the same thing with different variable names are one caller. The abstraction's job is to capture what varies, and you cannot see what varies from a single point.

**Write the second implementation first, then extract.** Extracting from two real implementations produces an interface shaped by reality. Designing an interface and then writing two implementations against it produces an interface shaped by the first implementation, with the second one bent to fit.

### The carve-out: abstractions that exist to constrain, not to share

`guarded_client()` has one shape of caller and would fail the two-caller rule. It exists anyway, and correctly, because its purpose is not code reuse. Its purpose is to be *the only path*, so that the absence of alternatives is checkable.

The same applies to `RiskEngine.decide()` as the sole constructor of `Order`, and to `BacktestEngine.run()` as the sole incrementer of the trial counter. These are chokepoints, not shared implementations.

The distinction:

- An abstraction for **reuse** is justified by having callers. Two, minimum, or you are guessing.
- An abstraction for **constraint** is justified by what it forbids. Introduce it before the first caller, because its value is that no second path ever exists, and a second path is much cheaper to prevent than to remove.

Confusing these produces both errors: speculative interfaces defended as "safety", and real safety boundaries deleted for having one caller. Ask what the abstraction is *for*. If the answer is "so nothing else can do X", the two-caller rule does not apply.

---

## 6. Recording a decision

Three tiers. Using the wrong one is itself a failure — an ADR for a reversible choice is noise that trains people to skim, and a commit message for a one-way door loses the reasoning.

| Tier | For | Where |
|---|---|---|
| Commit message | Reversible, local, obvious in hindsight | `git` |
| Inline comment with a source | A constant, a magic number, a workaround for external behaviour | The line above it |
| ADR | One-way doors (§1) | `docs/adr/NNNN-slug.md`, immutable once accepted |

Every non-obvious constant gets the second tier. `# Binance spot switched to microsecond timestamps on 2025-01-01; futures did not. See ADR 0013.` is worth more than the code it sits above, because without it someone deletes the conditional as redundant.

### What an ADR must contain

Beyond the standard context/decision/consequences:

**The strongest rejected alternative, stated in its own best terms.** Not a strawman. The test: could an advocate of the rejected option read your description and agree it is fair? If not, you have not understood the option and the ADR records advocacy, not a decision. `ARCHITECTURE.md` §4 does this properly for `NautilusTrader` — it calls it "genuinely strong", names its real advantages, and then gives a specific reason it does not fit. That is the standard.

**The condition that would change our mind.** Every ADR states what evidence would make the decision wrong. "Revisit if the strategy population exceeds 50 and CPCV runtime exceeds four hours." "Revisit if `ccxt` breaks the spot `session.logon` path."

This is the same discipline the system imposes on strategies. A `Signal` without an `invalidation` level is a hope, not a thesis (`ARCHITECTURE.md` §5). An ADR without a revisit condition is a preference, not a decision — and it is the ADRs without revisit conditions that get treated as permanent long after the world changed, because nobody knows what would justify reopening them.

Superseding an ADR means writing a new one and leaving both. The record of rejected paths is the valuable part; a decision log you can rewrite is not a log.

---

## 7. When to escalate

`CLAUDE.md` §8 lists four triggers: architecture-changing with both readings plausible, requires a credential or signup, involves money or legal exposure or the safety kernel, or proceeding wrongly would waste substantial work. Three additions.

**Asymmetric reversal cost.** Escalate when the decision is cheap for you to make and expensive for the user to unmake. Data model and audit schema decisions are the recurring example: five minutes to write, permanent once rows exist.

**You cannot state the revisit condition.** If you cannot say what would prove the decision wrong, you do not understand the trade-off well enough to make it alone. That is a signal, not a failure — say so, and say what is unclear.

**Never escalate a two-way door.** If it is reversible within the feedback window, deciding badly and correcting is strictly cheaper than a round trip through a human. Escalating reversible decisions is not caution; it is offloading, and it degrades the value of the escalations that matter.

**Format.** One topic, with a recommendation attached and the reasoning compressed to a few sentences. "I recommend A because X; say so if you would rather have B" beats "which of these do you want?", because the first can be answered with one word and the second requires the reader to reconstruct your analysis. Do everything not blocked on the answer first, then ask (`CLAUDE.md` §8).

---

## 8. Deciding when the evidence is weak

The case this document exists for. You have two options, the evidence does not separate them, and you have to ship.

**Default to the option that fails loudly.**

Not the option most likely to be right — you have already established you cannot tell. The option whose *failure* is most detectable. Rank failure modes:

| | Failure mode | Verdict |
|---|---|---|
| 1 | Refuses to start | Best |
| 2 | Crashes at the point of error, with the state in the traceback | |
| 3 | Returns an explicit error value the caller must handle | |
| 4 | Logs a warning and enters a named degraded mode | |
| 5 | Silently substitutes a default | |
| 6 | Silently returns a plausible number | **Catastrophic** |

The gap between 1 and 6 is not a factor of a few. In a trading system a plausible wrong number propagates into a position, a fill, an attributed PnL, a survival score, and a breeding decision, and by the time anyone notices, the population has been selected on it. A refusal to start costs an hour.

Applied:

| Situation | Loud option | Quiet option |
|---|---|---|
| Requested feature not in the feature store | Raise `FeatureUnavailable` | Impute, forward-fill, or return zeros |
| Exchange response has an unexpected field | Reject and alert | Parse what you recognise and continue |
| Risk config key missing | Abort startup | Use a built-in default |
| Timestamp unit ambiguous | Fail with the raw value in the message | Guess by magnitude |
| Reconciliation mismatch below dust threshold | Log at `WARN` with both values | Round and move on |
| Strategy returns `NaN` conviction | Reject the signal, record a discipline violation | Clamp to 0 |

The quiet column is, in every row, the option that produces a working system today and an unfalsifiable one in three months.

**Corollary: prefer a higher probability of a loud error to a lower probability of a silent one.** A 30% chance of an option that crashes when wrong beats a 10% chance of an option that silently degrades, because you will find out about the first and pay for it once. This inverts the usual instinct — it says pick the option more likely to be wrong — and it is correct here because the cost is dominated by detection latency (§2), not by frequency.

**Corollary: when in doubt about a *quantity*, choose the smaller one.** Smaller position, shorter horizon, tighter limit, fewer trials, less leverage. Every risk parameter in this system has the property that the conservative error is bounded and the aggressive error is not (`RISK_PHILOSOPHY.md` §3.3). Under genuine uncertainty, pick the bounded side. It is the same argument as fractional Kelly, applied to engineering decisions instead of position sizes.
