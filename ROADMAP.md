# Roadmap

Eight phases, P0 through P7, tracked as GitHub milestones. Each phase has an epic issue; each epic has task issues.

This document states the *plan and its reasoning*. For actual state, trust the issues — a roadmap that disagrees with the repository is a roadmap nobody reads.

---

## Sequencing principle

The ordering is not arbitrary and is not "easiest first". It follows one rule:

**Nothing that produces a number is built before the thing that can tell whether the number is real.**

The tempting order is data → strategy → execution → "and then we'll add validation". That order produces a system that trades within weeks and has no way to know whether any of it works. Validation built afterwards always gets weakened to accommodate results that already exist.

So: contracts and guardrails first (P0), then data with point-in-time integrity (P1), then the validation engine (P2), and only then strategies (P3) — which is the first phase that produces a result worth having, precisely because P2 already exists to doubt it.

---

## Critical path

```
P0 Foundation ──► P1 Data ──► P2 Backtest ──► P3 Strategy+Risk ──► P4 Demo Execution
                    │              │                 │
                    │              └────────────────►└──────────► P6 Evolution
                    │                                                  ▲
                    └──────────────────────────────────────────────────┘
                                                    P5 Agents ─────────┘

P7 Observability ── instrumented incrementally across all phases, assembled last
```

**The binding constraint is P2.** It is the largest and highest-risk component, it cannot be delegated to a library without surrendering backtest/live parity, and both P3 and P6 are blocked on it. Any schedule pressure should be absorbed elsewhere.

**P5 (Agents) is deliberately off the critical path.** The system must be able to trade and evolve without LLM agents at all. Agents accelerate hypothesis generation; they are not load-bearing. If free-tier quotas collapse or an LLM provider withdraws, the system degrades to deterministic operation rather than stopping. Building P5 early would invert that relationship.

**P7 is not a final phase in the usual sense.** Instrumentation is part of each earlier phase's definition of done, because observability deferred to the end never gets built properly and is missing from exactly the history an investigation needs. P7 assembles and completes what earlier phases installed.

---

## Phases

### P0 — Foundation & Operating System
*Epic [#1](https://github.com/ismetcahangirov/financeKing/issues/1) · nothing trades*

Repository operating system, domain contracts, safety kernel, config, Compose stack, CI, ADRs, database schema, event bus and telemetry foundation.

Exists first because every later phase writes code touching money-shaped objects. A guardrail retrofitted into a running system is advisory; one that predates the first exchange adapter is structural.

| Task | Issue | Size |
|---|---|---|
| Claude Code operating system | [#9](https://github.com/ismetcahangirov/financeKing/issues/9) | XL |
| Python skeleton, enforced boundaries | [#10](https://github.com/ismetcahangirov/financeKing/issues/10) | M |
| Safety kernel | [#11](https://github.com/ismetcahangirov/financeKing/issues/11) | M |
| Domain contracts | [#12](https://github.com/ismetcahangirov/financeKing/issues/12) | M |
| Configuration and secrets | [#13](https://github.com/ismetcahangirov/financeKing/issues/13) | M |
| Docker Compose baseline | [#14](https://github.com/ismetcahangirov/financeKing/issues/14) | M |
| CI pipeline | [#15](https://github.com/ismetcahangirov/financeKing/issues/15) | M |
| ADR process, first twelve ADRs | [#16](https://github.com/ismetcahangirov/financeKing/issues/16) | M |
| Schema, migrations, audit substrate | [#17](https://github.com/ismetcahangirov/financeKing/issues/17) | L |
| Event bus, logging, telemetry | [#18](https://github.com/ismetcahangirov/financeKing/issues/18) | L |
| Re-verify free-tier landscape | [#19](https://github.com/ismetcahangirov/financeKing/issues/19) | M |
| Database roles, least-privilege grants | [#106](https://github.com/ismetcahangirov/financeKing/issues/106) | M |
| Container and network hardening | [#107](https://github.com/ismetcahangirov/financeKing/issues/107) | M |

**Exit:** `docker compose up` healthy · `make check` green · a test proves the safety kernel rejects mainnet and cannot be overridden by config, and that an outbound connection to a production host fails at the network layer with the in-process kernel bypassed.

---

### P1 — Data Platform
*Epic [#2](https://github.com/ismetcahangirov/financeKing/issues/2) · depends on P0*

**18 task issues** — [P1 milestone](https://github.com/ismetcahangirov/financeKing/milestone/2)

Bulk historical ingestion, live streaming, normalization, feature store with point-in-time semantics, data-quality gates, alternative data.

**Exit:** ingesting BTCUSDT and ETHUSDT 1m from 2017 to present is one reproducible command · quality gate fails on injected corruption · the look-ahead test passes.

**Watch item:** three verified format traps (spot microsecond timestamps from 2025-01-01 while futures stayed milliseconds; futures kline header rows; Python-style booleans) will silently corrupt the dataset if normalization is keyed on anything other than `(market, date)`.

---

### P2 — Backtest & Validation Engine
*Epic [#3](https://github.com/ismetcahangirov/financeKing/issues/3) · depends on P1 · **critical path***

**14 task issues** — [P2 milestone](https://github.com/ismetcahangirov/financeKing/milestone/3)

Event-driven engine, venue abstraction, cost model, walk-forward, CPCV, Monte Carlo, deflated Sharpe, tearsheets.

**Exit:** same strategy + data + seed produces bit-identical results · a deliberately overfit strategy is rejected by the deflated-Sharpe gate · a deliberate look-ahead bug is caught rather than producing good numbers.

**Watch item:** this is where the project most plausibly fails by taking twice as long as expected. It is also the phase where cutting scope is most expensive, because everything downstream inherits its credibility.

---

### P3 — Strategy & Risk Core
*Epic [#4](https://github.com/ismetcahangirov/financeKing/issues/4) · depends on P2*

**14 task issues** — [P3 milestone](https://github.com/ismetcahangirov/financeKing/milestone/4)

Strategy contract, three baseline strategies, risk engine, sizing, correlation-aware exposure, drawdown and daily loss limits, kill switch.

The baselines (trend, mean reversion, funding carry) are **not expected to be profitable.** They are expected to be *correct*, and to serve as the control group an evolved strategy must beat. Without a control, "the evolution engine produced something good" is unfalsifiable.

**Exit:** risk engine rejects limit-breaching signals with a logged reason · kill switch flattens and blocks within one event loop tick · Hypothesis property tests prove sizing never exceeds bounds for any input.

---

### P4 — Demo Execution
*Epic [#5](https://github.com/ismetcahangirov/financeKing/issues/5) · depends on P3 · `safety:critical`*

**13 task issues** — [P4 milestone](https://github.com/ismetcahangirov/financeKing/milestone/5)

Binance testnet adapters (two user-data paths), OMS, reconciliation, clock-skew monitoring, rate limiting, promotion gate, execution quality analysis.

**Exit:** a baseline strategy completes a logged round trip on testnet · killing the process mid-order and restarting reconciles with no duplicate or orphaned orders · a simulated testnet wipe is detected and recovered from · the safety kernel suite passes including mainnet rejection.

**Watch item:** the roughly 30-day spot testnet wipe destroys balances and open orders while keeping API keys. This makes reconciliation a first-class feature rather than a nicety — and a wipe looks identical to catastrophic loss unless the system can tell them apart.

---

### P5 — Multi-Agent Intelligence
*Epic [#6](https://github.com/ismetcahangirov/financeKing/issues/6) · depends on P0, P2 · **off critical path***

**12 task issues** — [P5 milestone](https://github.com/ismetcahangirov/financeKing/milestone/6)

LLM gateway with quota-aware routing and failover, agent runtime, versioned prompt library, three-tier memory with pgvector, agent evaluation harness.

**Exit:** a research cycle produces a typed, validated hypothesis · every LLM call logged with prompt, response, model, latency, tokens · quota exhaustion degrades to deterministic operation · a prompt regression suite catches a deliberately degraded prompt.

**Blocked on:** [#19](https://github.com/ismetcahangirov/financeKing/issues/19). Current free-tier quota assumptions are **unverified** — the research was cut short. If real limits are an order of magnitude below assumption, agent scheduling changes materially.

---

### P6 — Evolution Engine
*Epic [#7](https://github.com/ismetcahangirov/financeKing/issues/7) · depends on P2, P3*

**13 task issues** — [P6 milestone](https://github.com/ismetcahangirov/financeKing/milestone/7)

Lifecycle state machine, survival scoring, mutation and crossover, global trial registry, held-out period manager, champion/challenger promotion, population diversity, lineage store.

**Exit:** a full generation runs autonomously · a deliberately overfit strategy is rejected by the promotion gate with a recorded reason · trial counts survive process restarts and generation boundaries · diversity is measured per generation.

**Watch item:** the assumption most likely to be wrong in this entire project is that P6's overfitting defences are sufficient. The signal that they are not: evolved strategies consistently outperform in validation and underperform forward. If that appears, the scoring engine is lying and takes priority over all other work.

---

### P7 — Observability & Control Plane
*Epic [#8](https://github.com/ismetcahangirov/financeKing/issues/8) · instrumented throughout, assembled last*

**14 task issues** — [P7 milestone](https://github.com/ismetcahangirov/financeKing/milestone/8)

OpenTelemetry coverage, provisioned dashboards, correlation ID propagation, append-only audit log, Next.js dashboard, alert rules, runbooks.

**Exit:** any trade fully reconstructable from the audit log alone with no access to application memory · one trace follows a decision end to end · kill switch reachable in one action from the dashboard · every alert links to a runbook a tired human can follow at 3am.

---

## Risk register

| Risk | Impact | Mitigation |
|---|---|---|
| **Evolution engine manufactures overfit strategies and ranks them highly** | Fatal to the project's purpose | Global trial counting, CPCV, burned holdout, forward-only promotion, minimum sample sizes. Monitored by validation-vs-forward rank correlation. |
| **Look-ahead bias in the feature store** | Every downstream result invalid, silently | Point-in-time semantics, `available_at` separate from `event_time`, adversarial leak test that must fail closed |
| **P2 takes far longer than estimated** | Schedule slips across four phases | Accept it; absorb pressure elsewhere. Do not cut validation scope to recover time. |
| **Free-tier LLM quotas below assumption** | P5 scheduling redesign | [#19](https://github.com/ismetcahangirov/financeKing/issues/19) verifies before implementation; gateway abstraction makes provider swap a config change |
| **Testnet withdrawn or changed** | Execution phase blocked | Bybit testnet as fallback behind the same abstraction; venue profile is data, not code |
| **Cost model calibrated on testnet** | Backtests become fiction | Standing rule: calibrate from production data only. Encoded in `market-research` agent and the backtest checklist. |
| **Silent data corruption from format changes** | Backtests skew without failing | Checksum verification, `(market, date)`-keyed normalization, nightly quality job |
| **Scope: building the OS instead of the system** | Nothing ever trades | P0 ships skeleton and guardrails only. Abstractions require two concrete callers. |

---

## What "done" means for this project

There is no version at which this is finished, but there is a point at which it is *real*:

A strategy proposed by the system, validated through CPCV and deflated Sharpe against the full project trial count, promoted only after forward paper performance, executing on testnet under a risk engine that can veto it, fully reconstructable from the audit log — and retired automatically when its edge decays.

Everything in this roadmap exists to make that sentence true rather than aspirational.
