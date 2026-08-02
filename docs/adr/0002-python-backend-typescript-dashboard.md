---
number: 0002
title: Python for the backend, TypeScript confined to the dashboard
date: 2026-08-03
status: accepted
deciders: [ismetcahangirov, architect, cto]
supersedes: null
superseded_by: null
related_issues: ["#10", "#16", "#103"]
related_adrs: [ADR-0001]
---

## Context

The system has two audiences with incompatible requirements: a trading and research core that needs numerical libraries and exchange clients, and a dashboard that needs a browser.

```
Forces:
- The quant and ML ecosystem is Python and effectively only Python. pandas,
  NumPy, SciPy, Hypothesis, ccxt, statsmodels, DuckDB's Python API, pgvector
  clients -- none has a TypeScript equivalent worth the swap, and several have
  none at all.
- Python is slow at exactly the thing this system does most: iterating a
  multi-year event stream bar by bar. The backtest engine is the hot path and
  it is not vectorisable (ADR-0005).
- A dashboard is a browser application, and browsers run JavaScript. Server-side
  rendering a research UI from Python is possible and is uniformly worse than
  the React ecosystem at it.
- Two languages means two toolchains, two dependency managers, two type
  checkers, two CI paths and two places a Decimal can silently become a float.
- There is one developer. Every language boundary is a context switch paid on
  every change that crosses it.

The constraint that forces a decision now:
#10 lays down the project skeleton -- interpreter version, lockfile, type
checker, linter -- and every later issue is written against it.
```

## Decision

**We write the entire backend in Python 3.12 — domain, data, strategy, risk, execution, backtest, agents, evolution, platform and the FastAPI surface — and confine TypeScript to the Next.js dashboard, which holds no trading logic and no authority.** The dashboard reads through the control-plane API and its one write path is the audited kill switch (#102). Every monetary value crosses that boundary as a JSON **string**, never a JSON number, so the language boundary cannot silently become a `Decimal`-to-`double` boundary. This decision fixes the languages and where the line between them sits; it does not preclude a compiled extension behind a Python interface if profiling ever demands one.

## Alternatives considered

### Alternative 1 — Rust or Go for the execution and backtest hot paths, Python for research (strongest rejected)

**What it would have given us.** The honest problem with Python here is real and measurable: the backtest engine walks years of bars in a Python loop that cannot be vectorised without reintroducing look-ahead (`ARCHITECTURE.md` §4), and combinatorial purged cross-validation multiplies that loop by the fold count. #109 exists as a performance issue precisely because this is the binding constraint on how much validation is affordable. A Rust event loop would plausibly be one to two orders of magnitude faster, which converts "we can afford one CPCV run per candidate" into "we can afford twenty" — and the number of validation runs we can afford is directly the strength of the overfitting defences (`ARCHITECTURE.md` §10). Rust would also give real types over money instead of a `Decimal` class the language does not know is special, and no GIL, so a sweep and the live loop would not contend (ADR-0001's revisit trigger).

**Why it lost.** Two languages inside the backend would break backtest/live parity, which `ARCHITECTURE.md` §4 calls the single most important architectural property. Parity is guaranteed structurally by there being exactly one code path from `Signal` through `RiskEngine` to `ExecutionVenue`, with only the venue swapped. A Rust backtest engine and a Python live path are two implementations of the fill semantics, the ordering rules and the cost model — and the moment they can disagree, every backtest result becomes unfalsifiable, because "the strategy is bad" and "the harnesses differ" stop being distinguishable. The performance win would be bought with the property the whole architecture is organised around.

The second reason is that the split is on the wrong axis. The research layer is not separable from the execution layer here: the strategy code that runs in a backtest is the *same object* that runs against the demo venue. There is no clean seam to put a language boundary on, so the boundary would have to be drawn through the middle of the parity guarantee.

Third, one developer maintaining a Rust core and a Python surface is a real ongoing cost against a speed problem that has cheaper answers: `polars` and DuckDB for the data path, process-level parallelism for sweeps, and profiling before rewriting. The escape hatch stays open — a compiled extension behind a Python interface preserves the single code path in a way a second engine does not.

**What survives the rejection, and is adopted.** The performance concern is legitimate and is not dismissed by rejecting the language. It is tracked as its own issue (#109, "make CPCV affordable on one machine") with a measured budget, and the revisit trigger below is written in terms of that budget rather than in terms of language preference.

### Alternative 2 — TypeScript end to end, Python called out to for numerics

**What it would have given us.** One language, one type system, one package manager, and no serialisation boundary between the API and the dashboard. TypeScript's structural types are genuinely good, and a single toolchain for a single developer is a real saving.

**Why it lost.** `ccxt` has a TypeScript build, so the exchange client is not the blocker — the research stack is. Hypothesis has no TypeScript equivalent, and property-based tests are mandatory for all risk and position math (`CLAUDE.md` §5); `decimal.js` is a library rather than a language-integrated type with a process-wide context, so the `FloatOperation` trap that turns `Decimal("0.1") == 0.1` into an exception at the point of the mistake (`.claude/rules/decimal-and-money.md`) has no counterpart. Deflated Sharpe ratios, purged CV, `pgvector` and the archive parsers would all be written from scratch or shelled out to Python — at which point there are two languages anyway, split across the parity boundary, which is Alternative 1's failure with the languages swapped.

### Alternative 3 — do nothing (defer, prototype in both)

```
Cost of the status quo: #10 is blocked, and with it every issue that imports
from src/fking. A prototype in both languages is roughly two weeks that
produces no shippable component and answers a question the ecosystem has
already answered.
Why that is no longer payable: the answer is not close. No other language has
the quant stack, and the parity requirement forbids splitting the backend.
```

## Consequences

**What becomes easier**
- One dependency resolver (`uv`), one lockfile, one `mypy --strict` configuration covering `src`, `tests` and `tools`, one `ruff` rule set. `make check` is a single gate.
- The AST checks in `tools/checks/` — money never typed `float`, strategy and risk never reading the wall clock, `SafetyViolation` never caught — work because there is one language to parse. A polyglot backend would need each check written twice or dropped.
- Strategy code is genuinely identical across backtest, paper and demo, so parity is a property of the code rather than a claim about two implementations.

**What becomes harder**
- The backtest loop is Python-slow, permanently. Every validation method has to be affordable in Python or it does not get used, which is a constraint on methodology and not only on runtime.
- The dashboard boundary needs explicit care on every payload: money is serialised as a string in both directions, and `Decimal` reconstruction on the way back in is not optional (`.claude/rules/decimal-and-money.md`). A `number` in a JSON schema is a defect, not a style choice.
- Two toolchains still exist for the dashboard. The saving is that only one of them can touch a position.

**What we now cannot do**
- Put trading logic in the dashboard, even trivially — no client-side position sizing, no "just compute the notional in the browser to save a round trip". The dashboard renders what the backend decided. Reopening that would mean a second implementation of sizing in a language whose numeric type is a double, which is the failure `.claude/rules/decimal-and-money.md` exists to prevent.

## What would make us revisit this

```
Trigger:   A single combinatorial purged CV run over the standard 3-year,
           1-minute BTCUSDT window exceeds 30 minutes wall clock on the
           development machine after the #109 optimisation work is complete.
Observed:  The `backtest.cpcv.duration_seconds` metric, and the timing
           recorded in each BacktestResult.
Then:      Profile first; if the hot path is the event loop rather than the
           data layer, open a superseding ADR for a compiled extension behind
           the existing Python interface -- not a second engine.
```

## Verification

```
Confirmed if:  zero defects in docs/postmortems/ are attributed to a value
               changing type or precision across the Python/TypeScript
               boundary, measured by 2027-02-01
Refuted if:    any monetary value reaches the dashboard as a JSON number, or
               any trading decision is computed in TypeScript
Checked by:    api-engineer agent, via the API contract tests and the
               serialisation assertions in tests/
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
- [x] Linked from #16 and from `.claude/knowledge/decisions-log.md`
