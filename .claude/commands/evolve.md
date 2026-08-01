---
description: Run or inspect an evolution cycle with the overfitting defences verified before any promotion
argument-hint: [population-id or generation]
allowed-tools: Read, Grep, Glob, Bash
---

Run/inspect the evolution cycle for: $ARGUMENTS

An automated search over strategy space is a machine for producing overfit results. Run enough configurations against fixed history and some will look excellent by chance alone. The mutation operators are the easy part; the defences are the feature. Verify the defences before looking at any winner.

## 1. Defences, checked before results

```bash
grep -rn "trial\|deflat\|embargo\|holdout\|held_out" src/fking/evolution/ src/fking/backtest/ | head -40
```

**A. Global trial counting.** The counter is *global across the whole project's history*, not per-run and not per-population. Every configuration ever evaluated against history counts, including abandoned ones. Report the counter before and after this cycle.

**B. Deflated Sharpe.** Every candidate's Sharpe is deflated by the global trial count before ranking. Ranking on raw Sharpe and deflating only the winner is the same as not deflating — selection already happened.

**C. Combinatorial purged CV with embargo.** Confirm the embargo period exceeds the maximum feature lookback plus the maximum holding horizon. An embargo shorter than the feature window leaks across the fold boundary and every fold is contaminated.

**D. Held-out period untouched.** Print the date ranges the cycle read. Cross-check against the reserved window. It is burned once touched, by anything, including a diagnostic plot.

**E. Minimum sample size.** Trades, not bars. Candidates below the floor are not ranked at all — not ranked with a caveat.

**F. Forward performance required for promotion.** Champion/challenger: a challenger is promoted on forward paper/demo performance, never on backtest rank alone.

If any of A–F is missing or disabled, stop. The cycle's output is not interpretable and reporting a winner from it is actively harmful.

## 2. Run

```bash
make up
python -m fking.evolution.cycle --population $1
```

## 3. Read the population, not the winner

The winner is the most overfit member by construction — it is the maximum of a noisy sample. Look at the distribution:

- Median and dispersion of deflated Sharpe across the population. If the median is at zero and only the tail is positive, you are looking at noise and the tail is the luckiest draw.
- How much better is the best than the 90th percentile? A large gap is a red flag, not a discovery.
- Do the survivors share a parameter region, or are they scattered? Scattered survivors mean the objective is fitting noise.

## 4. Score integrity

The survival score is deliberately **not profit**. Confirm the weighting still reflects: risk-adjusted return, drawdown discipline, cross-regime consistency, per-trade edge after costs, capacity, out-of-sample decay — and that **risk-limit violations are a hard negative**. A strategy that made money by breaching limits must score worse than one that made less within them.

```bash
grep -rn "violation\|breach" src/fking/evolution/scoring* src/fking/evolution/
```

If someone has softened the violation penalty to "let good strategies through", that is the bug: the system optimizes what it measures.

## 5. Lineage and memory

- Every candidate has a parent lineage and a version hash. An unattributable candidate cannot be retired intelligently later.
- Evolution memory is append-only. Confirm no cycle rewrites or deletes prior generations' records — a population that can edit its own history will flatter itself.

## 6. The assumption to watch hardest

Compare validation rank against subsequent forward performance for previously promoted strategies:

```bash
python -m fking.evolution.report --forward-vs-validation
```

If validation rank does not predict forward performance, **the scoring engine is lying** and fixing it takes priority over every other piece of work in the project. Report the rank correlation explicitly every cycle; it is the single number that tells you whether the whole apparatus is working.

## 7. Report

Trial counter delta, population deflated-Sharpe distribution, promotions and retirements with reasons, held-out status, and the validation-vs-forward rank correlation.
