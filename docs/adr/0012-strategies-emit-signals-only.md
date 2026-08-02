---
number: 0012
title: Strategies emit Signal and have no import path to order construction
date: 2026-08-03
status: accepted
deciders: [ismetcahangirov, architect, risk-manager, judge]
supersedes: null
superseded_by: null
related_issues: ["#46", "#55", "#16"]
related_adrs: [ADR-0001, ADR-0005]
---

## Context

Something has to decide how much to buy. In essentially every trading framework, that something is the strategy: it computes a signal and submits an order sized by its own logic.

```
Forces:
- A strategy that sizes its own positions can bankrupt the portfolio
  regardless of how good its signals are. Signal quality and sizing quality
  are independent, and only one of them is bounded by being wrong.
- Sizing needs information a single strategy does not have: total portfolio
  exposure, correlation with every other live strategy, remaining drawdown
  budget, and whether the kill switch is armed. A strategy sizing itself is
  sizing against a partial view.
- This system will write its own strategies via LLM agents. An LLM-authored
  strategy will size its own positions if the type system permits it, with a
  plausible comment explaining why this case is different, and it will pass a
  human skim.
- Every framework in this space does it the other way, so the convention will
  be proposed repeatedly by anyone with prior experience -- and by any model
  trained on their code.
- Strategy code must be pure and deterministically replayable, because the
  evolution engine replays and mutates it and a non-reproducible result scores
  noise.

The constraint that forces a decision now:
#46 defines the strategy contract and #55 defines RiskEngine.decide(). The
contract fixes what a strategy is allowed to say, and retrofitting the
restriction after strategies exist means rewriting all of them.
```

## Decision

**A strategy emits a `Signal` — `direction`, `conviction`, `horizon`, `invalidation`, `rationale`, `decided_at_utc` — and says nothing about size.** `RiskEngine.decide()` in `src/fking/risk/` is the only code that constructs an `Order`, and it owns position sizing, exposure limits, correlation-aware netting, drawdown limits and veto authority. `fking.strategy` has **no import path** to `fking.execution` or `fking.risk`, enforced by two `import-linter` contracts — the layers contract and a separately named forbidden contract — so the violation is a build failure with a line number rather than a review comment. `invalidation` is mandatory: a strategy must state in advance the price at which its thesis is wrong. This decision covers what a strategy may express; it does not fix the sizing algorithms, which are #48's subject and may change freely behind the boundary.

## Alternatives considered

### Alternative 1 — strategies construct orders; the risk engine vetoes them (strongest rejected)

**What it would have given us.** This is how NautilusTrader, Backtrader, QuantConnect, `bt` and nearly every framework in the space work, and the reasons are not weak ones. Sizing is frequently *part of the edge*: a volatility-breakout strategy that scales into a position as confirmation accumulates, a mean-reversion strategy that adds at intervals, a strategy whose whole thesis is about position accumulation shape — these express something in their sizing that a conviction scalar cannot carry. Under a veto model the strategy states its full intent, and risk trims or refuses it, which preserves both the expressiveness and the control. It is also the model every practitioner and every model trained on their code expects, so it is what a contributor writes by default and what a reviewer reads without friction. And a veto is genuinely a control: an order that risk refuses does not reach the venue.

**Why it lost.** A veto sees the order, not the reasoning, and by then the information needed to size correctly has already been discarded. When a strategy submits 0.5 BTC, the risk engine can only ask "is 0.5 BTC acceptable given everything else?" — it cannot ask "given this conviction, this horizon and this invalidation level, what size is right?", because conviction, horizon and invalidation were consumed inside the strategy and thrown away. So a veto model systematically produces the wrong question, and the answer to it is a clamp rather than a decision.

The second reason is that the veto is only as strong as the path being singular. Once a strategy holds an order type and a submission path, every other route to the venue must also be intercepted — and `ARCHITECTURE.md` §5 makes risk's authority structural precisely because interception is a property of code that gets refactored. Under the chosen design the guarantee is not "risk checks every order"; it is that **no other code can construct one**, which does not depend on anyone remembering to route through the checkpoint.

Third, and decisive for this project specifically: the author will frequently not be a human. An LLM-authored strategy under a veto model will submit an aggressively sized order with a comment about why this setup warrants it, and the veto will trim it to the limit — so the strategy learns nothing, the limit becomes the size, and every strategy converges on maximum exposure. Under the chosen design the same model cannot express the request at all, because `fking.strategy` has no import edge to reach it, and CI says so with a filename and a line number.

**What survives the rejection, and is adopted.** The expressiveness objection is real and is not answered by "conviction is enough". It is why `Signal` carries `horizon` and `invalidation` rather than a bare direction and confidence: the risk engine can size from the distance to invalidation, which is exactly the volatility-scaled sizing the accumulating-strategy argument wants, computed with portfolio state the strategy never had. Where a genuine sizing *shape* is needed — scaling in over time — the correct move is to enrich `Signal` so the risk engine can express it, not to move the authority. That is a change to a shared contract, reviewed once, rather than a capability granted to every strategy.

### Alternative 2 — strategies emit a target portfolio weight

**What it would have given us.** A middle position with real merit: the strategy says "I want 3% of the book in this", which is size-like enough to express accumulation shape while remaining unit-free and portfolio-relative, so it composes across symbols and does not require the strategy to know the account balance. Risk still scales the whole book. Several institutional systems work exactly this way and it is not a compromise for its own sake.

**Why it lost.** A weight is a size expressed in a different unit, and it carries the same defect: it presumes the strategy knows what fraction of the portfolio this position deserves, which is a question about *every other position* — their correlations, their current drawdown contribution, the remaining risk budget. A strategy that answers it is answering from a partial view, and two strategies that each want 3% of a book they cannot see have no mechanism to discover that they are the same trade. Correlation-aware netting (#51) exists because that case is common rather than exotic.

It also weakens the enforcement without removing the need for it. A weight is a number the risk engine must still reinterpret against portfolio state, so the sizing logic stays in `risk` regardless — and the strategy now carries a field it cannot compute correctly, which invites the reasoning that produced it back into strategy code.

### Alternative 3 — do nothing (convention, documented, not enforced)

```
Cost of the status quo: the rule holds until the first strategy that finds it
inconvenient, and that strategy will frequently be machine-authored with a
persuasive comment. The failure is silent: an order sized by a strategy is a
real position the risk engine never counted, so portfolio exposure,
correlation netting and the drawdown limit are all computed against an
incomplete book -- which means the kill switch, the one mechanism meant to
survive every other failure, is operating on wrong state.
Why that is no longer payable: the enforcement costs two import-linter
contracts and runs in two seconds. The failure it prevents is unbounded.
```

## Consequences

**What becomes easier**
- Sizing is one implementation in one module, so improving it improves every strategy at once, and a sizing defect has one place to be fixed.
- Strategies are trivially testable: `evaluate()` is pure, takes an injected clock, and returns a value. No venue, no portfolio, no mocking.
- Cross-strategy netting is possible at all, because the risk engine sees every intent before any of them becomes an order.
- The mandatory `invalidation` field forces each strategy to state what would falsify it, which is what the survival score's risk-limit accounting and the evolution engine's retirement logic both read.

**What becomes harder**
- Strategies that genuinely need a sizing *shape* must express it through `Signal`, and if the field does not exist yet the strategy is blocked on a change to a shared contract. That is deliberate friction and it will be felt by the first scale-in strategy.
- Practitioners and models with prior experience will write the veto pattern by reflex, and CI will reject it. Every such rejection costs a round trip and an explanation, which is why this ADR exists to be pointed at.
- The risk engine becomes a concentration of complexity and a single point of failure for the whole book — which is why `risk` carries a 95% coverage floor and mandatory Hypothesis properties.

**What we now cannot do**
- Import a strategy from the wider ecosystem and run it. Every third-party strategy assumes it can submit orders, so there is no adapter that does not reintroduce the capability. Reopening that would mean a shim through which foreign code submits orders, which is Alternative 1 wearing an integration layer.

## What would make us revisit this

```
Trigger:   Three or more distinct strategy specifications within any six
           months are rejected at review as unimplementable under the Signal
           contract, and in each case the missing expressiveness is the same
           field.
Observed:  Issues labelled area:strategy closed as `wontfix: contract`, and
           the strategy-generator agent's rejection log.
Then:      Enrich Signal with that field in a superseding ADR. Moving order
           construction into strategy is explicitly not the remedy: the
           trigger measures a missing field, not a missing authority.
```

## Verification

```
Confirmed if:  `lint-imports` reports the "Strategies never reach the order
               path" contract kept on every merge to main, and zero orders in
               the audit log lack a corresponding RiskEngine decision row,
               measured by 2027-02-01
Refuted if:    the contract is relaxed or removed, or any Order is constructed
               outside src/fking/risk/
Checked by:    risk-manager agent, via `make imports` and the audit-log
               reconstruction test (#95)
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
- [x] Linked from #16 and from `.claude/knowledge/decisions-log.md` (D-005)
