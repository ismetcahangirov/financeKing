---
number: 0005
title: Custom event-driven backtest engine instead of adopting NautilusTrader
date: 2026-08-03
status: accepted
deciders: [ismetcahangirov, architect, cto, backtesting]
supersedes: null
superseded_by: null
related_issues: ["#33", "#36", "#37", "#16"]
related_adrs: [ADR-0012, ADR-0001]
---

## Context

The single most important architectural property is that strategy code is **identical** in backtest, walk-forward, paper and demo-live, with only the `ExecutionVenue` swapped (`ARCHITECTURE.md` §4). Everything the project claims about a strategy rests on that: if a strategy could behave differently in backtest than in demo, no backtest result would be falsifiable, because "the strategy is bad" and "the harnesses differ" would be indistinguishable.

```
Forces:
- Writing an event-driven backtest engine is roughly six weeks of work that
  someone else has already done better. NautilusTrader is event-driven, has a
  Rust hot path, is actively maintained, and is used in production by people
  who do this full time.
- Adopting an engine means adopting its domain model and its lifecycle. Our
  risk engine holds sole authority to construct orders (ADR-0012) and our
  evolution engine owns the objective function; both are first-class
  components here, not plugins.
- Backtest/live parity has to be structural -- one code path -- rather than
  maintained by discipline across two engines.
- Fill simulation is where backtests lie. Queue position, latency, partial
  fills and the rejection taxonomy are the parts that decide whether a result
  is real, and they must be calibrated from production archives, never from
  testnet (VF-008).
- One developer. Six weeks of engine work is six weeks not spent on the
  validation machinery that is the actual product.

The constraint that forces a decision now:
#33 builds the event loop, and #36 the venue. Both are on P2's critical path,
and every strategy, every cost-model calibration and every validation run is
written against whichever engine this chooses.
```

## Decision

**We build a custom event-driven backtest engine in `src/fking/backtest/`, with a deterministic event loop over a total event ordering and an injected clock, and `BacktestVenue`, `PaperVenue` and `DemoVenue` as three implementations of one `ExecutionVenue` interface.** The strategy, the `RiskEngine` and the order path above the venue are the same objects in all four modes; only the venue changes. **This decision is recorded as open to revisit rather than closed** — the case for adopting NautilusTrader is strong and is preserved below in full, so that reopening it is an argument against a stated position rather than a rediscovery. Vectorised engines are rejected outright for the core and that part is not open.

## Alternatives considered

### Alternative 1 — adopt NautilusTrader (strongest rejected, and the reason this ADR stays open)

**What it would have given us.** An event-driven engine with a Rust core, correct nanosecond-resolution event ordering, a mature fill model, live and backtest adapters that already share a code path, and roughly six weeks of engine work we would not do or debug. It is maintained by people whose full-time job is this problem, so its edge cases — the ones that take a year of production to find — are already found. Its performance would relieve the constraint that ADR-0002 concedes is real and that #109 exists to manage: how many combinatorial purged CV folds we can afford directly determines how strong the overfitting defences are, and a faster engine is more validation per candidate. It also solves parity *for us*, in the same way we intend to solve it, which means the strongest argument for building is an argument Nautilus also satisfies.

**Why it lost.** Adopting an engine means adopting its domain model, and the two components this architecture is organised around would become plugins inside someone else's lifecycle.

The risk engine is the first. `ARCHITECTURE.md` §5 makes it the **sole** constructor of orders: a strategy emits a `Signal` carrying direction, conviction, horizon and invalidation, and has no import path to order construction at all (ADR-0012). Under Nautilus, a strategy *is* the thing that submits orders — that is its contract, and it is the contract of essentially every framework in this space. Preserving our invariant on top of it means either routing every submission through a risk shim the framework does not know about (so the framework's own order paths become bypasses that `import-linter` cannot see), or accepting that a strategy can size itself. The second is unacceptable and the first converts a structural guarantee into a convention — which is precisely the thing this project refuses, because its strategies will be written by language models that do not read documentation.

The evolution engine is the second. The survival score, the trial ledger charged at specification time, the held-out vault and the champion/challenger gate are not reporting layered on a backtest; they own when a backtest may run and what a result means (`ARCHITECTURE.md` §10). `BacktestEngine.run()` refuses a `spec_hash` that was never registered (`docs/rules/overfitting-defences.md`). That is an authority relationship, and inverting it — making our gates a callback inside a foreign `run()` — means the enforcement point moves into code we do not control.

Third, and least discussed: adopting a fill model means adopting *its* assumptions about queue position and latency, and those assumptions are the part of a backtest that decides whether the number is real. We need them calibrated from `data.binance.vision` production archives and structurally forbidden from testnet calibration (VF-008, ADR-0007). That is a small, well-understood piece of code that we must be able to defend line by line, and it is not where borrowed judgement helps.

**What survives the rejection, and is adopted.** Two things, deliberately. Nautilus's total-ordering discipline — every event carries a monotone sequence and ties break deterministically, so a replay is byte-identical — is copied rather than reinvented; #33 specifies it as the engine's first property. And the performance concern is not dismissed by rejecting the library: it is tracked as #109 with a measured budget, and the revisit trigger below is written against that budget.

**Why this ADR is `open to revisit` and 0014 is not.** The rejection turns on a claim about integration cost that has not been measured — nobody has attempted to preserve the risk engine's authority inside Nautilus's lifecycle and found it impossible. It is a strong prediction, not an observation. Reopening it legitimately requires two things together: a named, recurring defect in our engine that Nautilus's design would have prevented, **and** a demonstration that the risk engine retains sole order-construction authority inside its lifecycle. Absent both, reopening is speculation. Tracked as OQ-010.

### Alternative 2 — a vectorised engine (VectorBT, `bt`) for the core

**What it would have given us.** Orders of magnitude faster than any event loop, in Python, today. A parameter sweep that takes hours event-driven takes minutes vectorised, and for a project whose defences depend on how much validation is affordable that is not a small claim.

**Why it lost, and why this part is not open to revisit.** A vectorised engine computes a signal series over a whole price series and then computes returns from it. Path-dependent risk logic cannot be expressed in that shape: a trailing stop that reacts to intrabar state, a portfolio kill switch that fires mid-sequence and changes every subsequent decision, correlation-aware netting that depends on positions that exist only because earlier signals were accepted. Every one of those is a feedback loop from the portfolio back into the decision, and vectorisation exists by removing exactly that loop.

The failure mode is what makes it disqualifying rather than merely limiting: approximating the loop leaks look-ahead, and look-ahead does not fail — it makes bad strategies look excellent (`CLAUDE.md` §2). A vectorised core would produce faster, more confident, wrong answers, in a system whose entire purpose is to reject strategies convincingly. Vectorised tools remain fine for exploratory screening *outside* the validated path, and nothing about that route may produce a `BacktestResult`.

### Alternative 3 — do nothing (defer the engine, validate manually)

```
Cost of the status quo: P2 does not start, so P3 (strategy and risk) has
nothing to validate against and P6 (evolution) has no result type to score.
That is roughly half the roadmap blocked. Manual validation of even one
strategy is days of work and is not reproducible, which makes it evidence of
nothing under the overfitting defences.
Why that is no longer payable: the project's stated job is to reject bad
strategies convincingly (CLAUDE.md 1). Without an engine there is no
mechanism to reject anything, and generation is already cheap.
```

## Consequences

**What becomes easier**
- Parity is a property of the object graph rather than a claim about two implementations: the same `Strategy` instance and the same `RiskEngine` run in backtest, paper and demo, and the parity test asserts identical signals and identical risk decisions across venues (#37).
- The trial ledger can be enforced *inside* `BacktestEngine.run()` (#39), so an unregistered specification cannot produce a result at all. That enforcement point does not exist if the run loop is foreign code.
- Fill semantics, latency and the rejection taxonomy are ours to calibrate and ours to defend, and the cost model can be structurally refused testnet input (ADR-0007).
- Determinism is testable end to end: the look-ahead probe replays a poisoned future and requires byte-identical decisions before the cut (`docs/rules/no-lookahead.md`), which needs a loop we control.

**What becomes harder**
- Every edge case is ours: event-ordering ties, bar-boundary semantics, partial fills, self-trade prevention, rejection classification. Each is a defect we will find in production rather than inherit as already-fixed.
- Performance is a permanent budget item rather than a solved problem, and it bounds validation depth. #109 exists because of this decision.
- Roughly six weeks of engine work that produces no strategy, no signal and no result — pure infrastructure, paid up front.

**What we now cannot do**
- Borrow a community strategy or adapter and run it. Everything that runs here is written against our `Signal`/`RiskEngine`/`ExecutionVenue` contract, so there is no import path from the ecosystem. Reopening that means an adapter layer, and an adapter that lets foreign code submit orders is the exact bypass Alternative 1 was rejected for.

## What would make us revisit this

```
Trigger:   Three or more distinct defects in docs/postmortems/ within any six
           months are attributed to event-ordering or fill-simulation errors
           in src/fking/backtest/, AND a spike demonstrates the RiskEngine
           retaining sole order-construction authority inside NautilusTrader's
           lifecycle.
Observed:  The postmortem index, tagged `area:backtest`, plus the spike branch.
Then:      Open a superseding ADR. Both conditions are required: the defects
           alone argue for fixing our engine, and the spike alone argues
           nothing without a cost we are actually paying.
```

## Verification

```
Confirmed if:  the backtest/live parity test (#37) passes on every merge, with
               zero divergence in signals or risk decisions between
               BacktestVenue and PaperVenue over the standard window, through
               2027-02-01
Refuted if:    the parity test is ever weakened to pass, or the defect trigger
               above fires
Checked by:    backtesting agent, via `make test -k parity` and the postmortem
               index
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
- [x] Linked from #16 and from `.claude/knowledge/decisions-log.md` (D-007, open to revisit; D-008)
