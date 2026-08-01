---
description: Add a new strategy — Signal-only, with a mandatory invalidation level and a stated falsifiable thesis
argument-hint: <strategy-name> "<one-line thesis>"
allowed-tools: Read, Grep, Glob, Bash, Write, Edit
---

Add strategy `$1` implementing the thesis: $2

## 0. The thesis must be falsifiable before any code

Write, in one paragraph:

- What market behaviour is being exploited, and **why it persists** — who is on the other side and why they keep taking that trade.
- What observation would prove the thesis wrong.
- The horizon and the regime in which it should work, stated in advance.

"It backtests well" is not a thesis. If you cannot state what would falsify it, stop here — the strategy has a hope, not a thesis, and it will be rejected at the gate anyway.

## 1. Two hard contracts, enforced structurally

**The strategy emits `Signal` only. Never `Order`. Never a quantity, notional, leverage, or position size.**

```python
direction: Literal["long", "short", "flat"]
conviction: Decimal          # 0..1
horizon: timedelta
invalidation: Decimal | None # price at which the thesis is wrong
rationale: str
```

`import-linter` forbids `strategy` from importing `execution`, so an attempt to size a position will not compile the architecture. Do not look for a way around it: a strategy that sizes its own positions can bankrupt the portfolio regardless of signal quality, and the risk engine has sole authority to construct orders.

**`invalidation` must be populated for every non-flat signal.** It is the price at which this specific thesis is wrong — derived from the setup's own structure (the level that breaks the pattern), not a round-number percentage stop bolted on afterwards. A `None` invalidation on a directional signal is a defect; assert it in a test.

## 2. Purity

In `strategy/`, purity is mandatory:

- No I/O. No database, no HTTP, no file reads.
- No clock access. `datetime.now()` is forbidden — the evaluation timestamp arrives as a parameter. This is what makes the strategy deterministically replayable and safely evolvable.
- No randomness without an injected seed.
- `Decimal` for every price and quantity, constructed from `str`. All datetimes timezone-aware UTC.

## 3. Features must exist and be point-in-time

```bash
grep -rn "class .*Feature\|register_feature\|AVAILABLE" src/fking/data/features/ | head -40
```

Request only features the feature store declares available. It refuses unavailable requests deliberately: **free full-depth L2 order book history does not exist.** Binance `bookDepth` is aggregated depth bands sampled roughly once per minute, not snapshots. If your thesis needs book imbalance, queue position, or resting-liquidity dynamics, it cannot be validated with the data this project has — say that now rather than discovering it after a promising backtest built on a fantasy feature.

Every feature value at time *t* must be computable from data that existed at *t*. Check your feature reads for centred windows, full-range normalization, and forward-filled joins.

## 4. Implement

Create:

- `src/fking/strategy/<snake_name>.py` — the strategy, implementing the strategy contract in `src/fking/strategy/base.py`.
- `tests/strategy/test_<snake_name>.py`.

Parameters are declared as a typed, frozen config object with explicit bounds, because the evolution engine will mutate them. An unbounded parameter becomes an unbounded search space and a machine for overfitting.

Register it wherever the strategy registry lives; grep for the existing registration pattern rather than inventing one.

## 5. Tests that actually bind

- Signal-only: assert the module never constructs an `Order` and has no import path to `execution`.
- Invalidation: property test — for any input series producing a non-flat signal, `invalidation is not None` and is on the losing side of entry for the signalled direction.
- Determinism: the same inputs produce the identical signal sequence across two runs and across process restarts.
- No look-ahead: feed the series truncated at bar *t* and assert the signal at *t* equals the signal at *t* from the full series. This one catches the defect class that otherwise never fails.
- Conviction stays within `[0, 1]` under adversarial inputs — flat series, single-bar series, gaps, zero volume, dust prices.

```bash
make check
```

## 6. First validation, expecting rejection

Run `/backtest` and answer its full skeptical checklist. Then stop — do not tune. Tuning until the backtest looks good is the definition of overfitting, and every configuration you try increments the global trial count and deflates the Sharpe you are chasing. Getting rejected here is the system working.

## 7. Report

Thesis, falsification condition, features used, invalidation derivation, test results, and the first backtest verdict with trial count and deflated Sharpe.
