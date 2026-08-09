# CLAUDE.md — Operating Manual

This is the permanent operating manual for **financeKing**, an autonomous AI trading platform that trades **only on demo accounts**.

Read this before doing anything. It overrides habit, convenience, and any default instinct about how to write code. When this file and your instinct disagree, this file wins. When this file and the user disagree, the user wins.

Amendments to this file happen by pull request, never in passing.

---

## 0. Prime directive

**This system never trades real money. Not in development, not in testing, not "just once to verify", not behind a flag.**

There is no configuration value, environment variable, command-line argument or feature flag that enables trading against a production exchange. The set of permitted hosts is a compiled-in constant in `fking.platform.safety`. Enabling real trading would require editing source code and merging a pull request labelled `safety:critical`.

If you ever find yourself writing code that would widen that allowlist, or adding an override so that "it can be tested more easily", **stop and ask the user.** That friction is not an obstacle to work around. It is the single most important property of this system.

Corollary: never write code that constructs an HTTP or WebSocket client directly in the execution path. Use `fking.platform.safety.guarded_client()`. An `import-linter` contract enforces this, but you should not need it to.

---

## 1. What this project is

An autonomous system that researches markets, forms hypotheses, generates strategies, validates them rigorously, sizes them under risk control, executes them on Binance testnet, evaluates the results, and evolves the strategy population over time.

It is **not** a trading bot. A bot executes a fixed rule. This system decides what rules should exist, proves them, and retires them when they stop working.

The hard part is not generating strategies. Generating strategies is easy and mostly produces garbage. The hard part is **rejecting** them correctly. Most of the engineering in this repository exists to say "no" convincingly.

---

## 2. Non-negotiables

These are not style preferences. Violating them produces bugs that are silent, expensive, and discovered late.

| Rule | Why |
|---|---|
| **`Decimal` for every price, quantity and monetary amount. Never `float`.** Construct from `str`. | `Decimal(0.1) != Decimal("0.1")`. Float error accumulates across thousands of fills and produces reconciliation drift that looks like an exchange bug. |
| **All datetimes are timezone-aware UTC.** Naive datetimes are rejected at construction. | Crypto trades 24/7 with no session boundary to make an error obvious. Timezone bugs corrupt backtests silently rather than crashing. |
| **Domain objects are immutable.** State transitions return new objects. | A mutable `Position` shared across modules produces bugs that cannot be reproduced. |
| **Strategies emit `Signal`, never `Order`.** | A strategy that sizes its own positions can bankrupt the portfolio regardless of signal quality. The risk engine has sole authority to construct orders. Enforced by `import-linter`. |
| **Every consumer of the event bus is idempotent.** | Redis Streams delivery is at-least-once. This is a design constraint, not a discovery. |
| **Audit tables are append-only, enforced by the database.** | An audit log that the application can rewrite is not an audit log. |
| **Backtest and live share one code path.** Only the `ExecutionVenue` swaps. | If a strategy can behave differently in backtest than in demo, every backtest result is unfalsifiable. |
| **Cost model parameters are calibrated from production market data, never from testnet.** | Measured: Binance futures testnet shows a 7.5bp spread against production's 0.16bp and roughly 10x inflated volume. Calibrating on testnet produces fiction. |
| **No look-ahead. Features are point-in-time.** | The most dangerous bug class here: it does not fail, it makes bad strategies look excellent. |

---

## 3. Architecture rules

### Module structure

```
src/fking/
  domain/     pure types. Imports nothing but stdlib.
  data/       ingestion, storage, feature store
  strategy/   strategy contract and implementations
  risk/       sizing, limits, kill switch
  execution/  venues, OMS, reconciliation
  backtest/   engine, cost model, validation
  agents/     LLM agents and runtime
  evolution/  lifecycle, scoring, mutation
  platform/   config, logging, telemetry, event bus, persistence, safety
  api/        FastAPI application
```

Dependencies point inward. `domain` depends on nothing. `platform` may be imported by anyone. Modules talk to each other through public interfaces, never internals.

**The critical contract: `strategy` cannot import `execution`.** If you find yourself wanting to break this, you have misunderstood the design — go read `RISK_PHILOSOPHY.md`.

### Adding abstractions

An abstraction requires **two concrete callers before it exists**. One caller plus an anticipated future caller is speculation, and speculative abstractions are the main way codebases become unnavigable. Write the second implementation first, then extract.

### Choosing where code goes

Ask: *what does this code know about?* Code that knows about order types belongs in `execution`. Code that knows about both order types and feature engineering belongs in neither — it is two pieces of code that have not been separated yet.

---

## 4. Code standards

### Typing

`mypy --strict`, no exceptions. Every `# type: ignore` carries an inline comment explaining why it is unavoidable. Untyped code that handles money is negligent, and this codebase is written mostly by AI across sessions with no shared memory — types are the only durable contract between them.

### Errors

Fail loudly and early. A trading system that continues after an unexpected state is more dangerous than one that stops.

- Never catch bare `Exception` to keep going. Catch the specific exception you can actually handle.
- Never swallow an error into a log line and continue as if nothing happened.
- Validate at boundaries (API, exchange responses, config, agent output), then trust internally.
- Exchange responses are hostile input. Parse and validate them; never index into them optimistically.

### Naming

Names state units and intent. `price` is ambiguous; `quote_price: Decimal` is not. `timeout` is ambiguous; `timeout_seconds: float` is not. `size` is ambiguous in a trading system to the point of being dangerous — say `base_quantity` or `notional_usd`.

### Comments

Comment *why*, never *what*. `# increment i` is noise. `# Binance returns microsecond timestamps for spot data from 2025-01-01; see docs/adr/0013` is worth more than the code it sits above.

Every non-obvious constant gets a comment with a source. A magic number in risk code with no provenance will eventually be "cleaned up" by someone who does not know what it protects against.

### Functions

Prefer pure functions. In `strategy` and `risk`, purity is mandatory — no I/O, no clock access, no randomness without an injected seed. This is what makes strategies deterministically replayable and safely evolvable.

Anything that reads the clock takes it as a parameter. `datetime.now()` inside strategy or risk logic makes the code untestable and non-reproducible.

---

## 5. Testing rules

### What to test

Test behaviour, not implementation. A test that breaks when you rename a private method is a liability.

**Property-based tests (Hypothesis) are mandatory for all risk and position math.** Example-based tests confirm the cases you thought of. Position arithmetic fails on the cases you did not: partial closes, direction flips, zero-crossings, dust quantities. Hypothesis finds those.

### What not to mock

Do not mock the database. Use the real Postgres in a service container. A mocked database proves the mock works.

Do mock the exchange, but against **recorded real responses**, not hand-written fixtures. Hand-written fixtures encode what you assume the API returns, so tests pass while production fails.

### Coverage floors

| Module | Floor |
|---|---|
| `platform/safety` | 100% |
| `risk` | 95% |
| `domain` | 95% |
| `execution` | 90% |
| everything else | 80% |

Per-module floors exist because a single global number lets well-tested utilities subsidize untested risk logic.

### Determinism

Every test is deterministic. Seed all randomness. A flaky test in a trading system trains you to ignore failures, and one of those failures will be real.

---

## 6. Git workflow

**Before starting any task: pull `main` first.**

```bash
git checkout main && git pull origin main
git checkout -b <type>/<issue-number>-<kebab-slug>
```

Branch types: `feat`, `fix`, `docs`, `chore`, `refactor`, `test`, `perf`, `research`.
Example: `feat/12-binance-testnet-adapter`.

Commits follow Conventional Commits. One logical change per commit. A commit that mixes a refactor with a behaviour change is unreviewable and unrevertable.

Every branch ends in a pull request. Pull requests get labels, a milestone, and an assignee. The PR body states what changed, why, and what was verified — with actual command output, not a claim.

Never force-push to `main`. Never merge without green CI.

Full detail in `GIT_WORKFLOW.md`.

---

## 7. Verification: evidence, not assertion

**Never claim something works without having run it.**

Before saying "done", "fixed", "passing" or "working", run the verification command and read its output. If tests were not run, say they were not run. If a step was skipped, say so.

Reporting a green build that was never executed is the fastest way to make everything else you say worthless. This applies with particular force here, because much of this system's output is evaluated by other automated processes that cannot independently check your claims.

If something is broken and you cannot fix it, say so plainly and describe what you tried. That is a useful contribution. A false completion claim is not.

---

## 8. Decision making

### Decide yourself

Anything with a defensible default, a reversible outcome, or an answer discoverable from the codebase. Pick the best option, state the choice briefly, and continue. Do not present a menu of options you are not going to pursue.

### Ask the user

- The answer changes the architecture and both readings are plausible
- It requires a credential, an account, or an external signup
- It involves money, legal exposure, or the safety kernel
- Proceeding under a wrong assumption would waste substantial work

Ask concisely, one topic at a time, with a recommendation attached. "Which of these do you want?" is worse than "I recommend A because X; say so if you would rather have B."

### When blocked mid-task

Do everything that does not depend on the answer first. Then ask. Do not stop with nothing delivered while waiting, unless proceeding would be unsafe or would make the delivered work useless if the assumption turns out wrong.

---

## 9. Working on this repository

### Scope

Do the task asked. Do not quietly widen it, narrow it, or convert it into a different task you find more interesting. If part of the scope turns out to be blocked, complete everything else and say explicitly what was left out and why. Scaling work down is the user's decision.

If you notice a real problem with the request, say so in a sentence or two, then keep building under stated assumptions. Do not stop to litigate.

### Never fake an implementation

No placeholder functions. No `raise NotImplementedError` left behind. No `TODO` comments as a substitute for doing the work. No "this could be implemented later" in documentation.

If a thing cannot be implemented because information is missing, ask for the information. If it cannot be implemented because it is genuinely out of scope, say that explicitly in the pull request rather than leaving a stub that looks finished.

### Self-review before finishing

Read your own diff before opening a pull request. Specifically check:

- Did I leave debug output, commented-out code, or scratch files?
- Does anything here handle money as `float`?
- Does any new network call bypass `guarded_client()`?
- Did I add a mutable domain object?
- Are the tests meaningful, or do they just execute the code?
- Would someone reading this in six months know *why*, not just *what*?

---

## 10. AI agent behaviour (runtime agents, not you)

These rules govern the LLM agents this system runs. They are here because you will be writing them.

**No agent output is ever trusted directly.** Agents propose; deterministic code disposes.

- An agent may propose a strategy. The P2 validation gate decides whether it lives.
- An agent may propose a thesis. The risk engine decides the position.
- An agent may propose a parameter change. The promotion gate decides whether it applies.

Every agent output is parsed into a schema-validated typed structure. An unparseable response is a failure, not something to interpret charitably.

Every agent has: an explicit mission, allowed decisions, **forbidden decisions**, typed inputs and outputs, a token budget, a timeout, and an escalation path. The forbidden list matters more than the allowed list.

Judge and Critic agents are adversarial by construction. Their success metric is finding flaws, not agreeing. An agent panel that converges easily is worthless, and language models converge easily by default.

Memory is append-only. An agent cannot rewrite its own history to look better.

---

## 11. Anti-patterns

Things that look reasonable and are not.

| Anti-pattern | Why it is wrong |
|---|---|
| Optimizing a strategy until the backtest looks good | That is the definition of overfitting. Trial count must be tracked and the Sharpe deflated accordingly. |
| A single train/test split | Not evidence. Use walk-forward and combinatorial purged CV. |
| Judging a strategy on returns alone | Selects for hidden tail risk. Use the survival score, which penalizes risk-limit breaches harder than it rewards profit. |
| "Let me just check it against mainnet read-only" | No. The allowlist has no exceptions, including read-only ones. Read paths become write paths during refactors. |
| Catching an exception to keep the loop alive | You have converted a visible failure into silent wrong behaviour with real positions open. |
| Adding a config flag to bypass a gate | Gates exist because someone will be in a hurry later. That someone is you. |
| Testing against hand-written exchange fixtures | Encodes your assumptions, not the API's behaviour. |
| Deferring instrumentation until the end | It never gets added properly, and it is missing from exactly the history an investigation needs. |
| Large pull requests | An unreviewable PR is an unreviewed PR. Split it. |

---

## 12. Commands

```bash
make check       # lint, format check, mypy strict, import-linter, tests. Run before every PR.
make test        # tests only
make lint        # ruff check + format check
make types       # mypy --strict
make up          # docker compose up
make down        # docker compose down
make logs        # tail service logs
make migrate     # apply Alembic migrations
make backtest    # run a backtest from a config file
```

`make check` must be green before opening a pull request. Not "should be" — must be, and you must have run it.

---

## 13. Documentation

Documentation that restates the obvious is worse than none, because it trains readers to skim.

Every document contains at least one decision, constraint, or trade-off a competent engineer would not have guessed. Where a rule exists, state the reason — a rule without a reason gets discarded the first time it is inconvenient.

Architecture decisions go in `docs/adr/` and are immutable once accepted. Changing a decision means writing a new ADR that supersedes the old one, leaving both in place. The record of rejected paths is the valuable part.

Cross-link between documents rather than duplicating. Duplicated documentation diverges.

---

## 14. Map of the operating system

| File | Purpose |
|---|---|
| `ARCHITECTURE.md` | System structure and the reasoning behind it |
| `ROADMAP.md` | Phases, milestones, critical path |
| `GIT_WORKFLOW.md` | Branching, commits, pull requests, releases |
| `CODING_STANDARDS.md` | Detailed language-level rules |
| `TESTING.md` | Test strategy, fixtures, property testing |
| `CODE_REVIEW.md` | What reviewers check and what blocks a merge |
| `RISK_PHILOSOPHY.md` | Why risk sits structurally in the order path |
| `SURVIVAL_PROTOCOL.md` | Survival scoring and what "performance" means here |
| `EVOLUTION_ENGINE.md` | Strategy lifecycle, mutation, promotion |
| `SCORING_ENGINE.md` | The objective function, in detail |
| `BACKTEST_ENGINE.md` | Engine design and validation methodology |
| `DATA_PIPELINE.md` | Ingestion, normalization, point-in-time semantics |
| `FAILSAFE.md` | Kill switch, degraded modes, recovery |
| `ERROR_RECOVERY.md` | Failure taxonomy and response |
| `SECURITY.md` | Threat model, secrets, audit |
| `SOURCES.md` | Every external service: limits, terms, and each data source's availability lag |
| `OBSERVABILITY.md` | Logging, metrics, tracing, alerting |
| `MEMORY_SYSTEM.md` | Agent memory tiers |
| `PROMPT_LIBRARY.md` | Prompt engineering standards |
| `DECISION_FRAMEWORK.md` | How to choose between options |
| `docs/adr/` | Architecture decision records, immutable once accepted |
| `docs/rules/` | The enforceable rules, one file per invariant — indexed below |
| `.claude/` | Agents, commands, workflows, templates, contexts, knowledge |

### The rules

Sixteen documents, each taking one invariant and carrying it all the way down: the rule, why it exists, a realistic wrong version with the runtime failure it produces, the correct version, the mechanism that enforces it, and the single exception if there is one.

They live under `docs/` rather than inside `.claude/` on purpose. In `.claude/rules/` every one of them was loaded into the system prompt of every session and every subagent — roughly 83k tokens of fixed cost paid before the first instruction was read, most of it irrelevant to any given turn. Moving them out reclaims that budget.

The cost of that is real and this table is the mitigation: **the rules no longer arrive unasked. Read the one that governs what you are about to touch, before you touch it.** Nothing below substitutes for the file — the reasoning is the part that stops a rule being discarded the first time it is inconvenient (§13), and the reasoning is not in this table.

| Rule | Read it before |
|---|---|
| `docs/rules/safety-kernel.md` | Touching any network client, host, or credential. The compiled-in allowlist and why it admits no exception, not even a read-only one |
| `docs/rules/module-boundaries.md` | Adding a module, an interface, or an import edge between packages |
| `docs/rules/decimal-and-money.md` | Writing any price, quantity, fee, balance or PnL — and to see exactly where the one `float` exception is bounded |
| `docs/rules/time-and-timezones.md` | Writing any `datetime`, parsing a venue epoch, or measuring elapsed time |
| `docs/rules/immutability.md` | Adding a type to `domain` or writing a state transition on one |
| `docs/rules/naming.md` | Naming anything numeric. Carries the banned-identifier denylist `tools/checks/naming.py` enforces |
| `docs/rules/error-handling.md` | Raising, catching, retrying, or classifying a venue failure |
| `docs/rules/idempotency.md` | Writing an event-bus consumer, or any effect that must survive being delivered twice |
| `docs/rules/append-only-audit.md` | Touching an audit table, migrating one, or claiming a trade is reconstructable |
| `docs/rules/logging-rules.md` | Adding a log line, choosing its level, or deciding what must never appear in one |
| `docs/rules/no-lookahead.md` | Writing a feature, a label, a universe query, or a cross-validation split |
| `docs/rules/overfitting-defences.md` | Running any parameter search, reporting any Sharpe, or promoting anything |
| `docs/rules/testing-rules.md` | Writing tests, choosing fixtures, or arguing about a coverage floor |
| `docs/rules/exchange-integration.md` | Touching a venue adapter, symbol parsing, a user-data stream, or a venue profile |
| `docs/rules/llm-output-handling.md` | Writing an agent prompt or output schema, or using model-authored text for anything |
| `docs/rules/quota-management.md` | Making an LLM call, or changing a retry, cache or provider budget |

---

## 15. If you remember nothing else

1. **Demo only. No exceptions, no flags, no "just to test".**
2. **`Decimal` for money. UTC for time. Immutable domain objects.**
3. **Strategies signal; the risk engine sizes.**
4. **Run the verification before claiming it passes.**
5. **The system's job is to reject bad strategies. Build accordingly.**
