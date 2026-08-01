# Survival Protocol

What it means for a strategy to survive here, and why survival is not the same thing as profit.

The formulas live in `SCORING_ENGINE.md`. This document is the argument for why the formulas have the shape they do. The lifecycle that consumes the score lives in `EVOLUTION_ENGINE.md`.

---

## 1. The premise

Strategies in this system compete for capital and for existence. The population is finite, slots are scarce, and every evaluation cycle some strategies are promoted and some are retired. That is the survival instinct: **performance-driven, but bounded by safety.**

The bound is not decoration. It is stated as a hard requirement:

> A strategy that made money by breaching risk limits must score **worse** than a strategy that made less money within them.

Not "should be looked at". Not "flagged for review". Strictly worse, in the number the selection process actually uses. If that inequality does not hold in the objective function, it does not hold at all, because the system optimises what it measures and nothing else.

Everything below is downstream of that sentence.

---

## 2. Why profit alone selects for hidden tail risk

This is the central result and it deserves to be stated precisely, because "don't just look at returns" is usually said as a platitude and it is not one.

Consider a selection process that ranks strategies by realised return (or Sharpe, or any statistic of the observed sample) over a window of length `T`. Consider two strategies with identical expected return:

- **A** earns its return smoothly, with losses distributed throughout the window.
- **B** earns the same expected return by collecting a premium continuously and paying it back in a rare, large loss whose expected inter-arrival time exceeds `T`.

Over any given window, B's observed mean is higher than its true mean by exactly the amount of the tail it did not pay, and its observed volatility is lower because the variance-contributing event is absent. B's observed Sharpe is therefore higher than A's — usually much higher — while its true Sharpe is identical or worse.

The selection process does not merely *tolerate* B. It **prefers** B, and the preference is monotone in how far the tail sits outside the window. For a fixed expected return, moving risk further into the unobserved tail strictly increases the observed statistic. A profit-ranked search does not accidentally pick up tail risk; it is a tail-risk-seeking optimiser with the sign flipped.

Concretely, in crypto: a short-volatility strategy — selling into a range, mean-reverting with no stop, funding-rate harvesting into leverage — earns 0.4–0.8% a month with a 96% win rate. After 30 months its Sharpe is above 3 and its maximum drawdown is 4%. It is the best strategy in the population by every profit-derived measure, right up to the March 2020 / May 2021 / November 2022-shaped day when it gives back 18 months of gains in six hours.

An evolution engine ranking on profit will find that strategy. It will find it *quickly*, because it is easy to construct and it dominates the leaderboard from month two. It will then breed from it.

**The defence is not to look at drawdown.** B's realised drawdown is small — that is the whole problem. The defences are structural: cross-regime consistency (§5) forces the strategy to be evaluated in periods where the tail *did* occur; capacity (§6) asks whether the edge is economic or an artifact; risk violations (§8) catch the leverage the strategy used to manufacture the premium; and confidence weighting (§9) refuses to believe a 30-month record about a 60-month event.

---

## 3. Component one: risk-adjusted return

Return per unit of risk, discounted for the number of times we looked.

The raw Sharpe ratio is not usable as a selection statistic in a search process — see §9 and `EVOLUTION_ENGINE.md` §4. What enters the score is the **deflated Sharpe ratio**, which asks: given that we ran `K` trials, what is the probability that this strategy's Sharpe exceeds what the best of `K` pure-noise strategies would have produced, correcting for the non-normality of the return distribution?

Two things about this that are easy to miss:

- **Skew and kurtosis enter with the sign you would not expect.** Negative skew *inflates* the naive Sharpe's significance if uncorrected — the deflation formula penalises exactly the return shape that strategy B in §2 produces. This is one of the few places where a statistical correction and an economic intuition point the same direction, and it is not a coincidence: both are detecting the same missing tail.
- **`K` includes trials you did not think of as trials.** Every backtest ever run against this dataset counts, including a human's ad-hoc exploration. See `EVOLUTION_ENGINE.md` §5.1.

Weight: 0.30. It is the largest single component and it is deliberately less than half.

---

## 4. Component two: drawdown discipline

Maximum drawdown is a single order statistic of a single path. It is the least stable number on the report and people treat it as the most reliable. A strategy's realised max drawdown is an estimate with an enormous standard error, and the estimate is biased low in short samples for the same reason as §2.

So drawdown discipline is measured with three numbers, not one:

- **MAR ratio** (`CAGR / MaxDD`) — the familiar one, included because it is what a human will check.
- **Ulcer index** (`√(mean(D_t²))`, where `D_t` is the percentage drawdown at time `t`) and the derived Martin ratio. Unlike max drawdown, the ulcer index uses the *entire* drawdown path, so it is a statistic over thousands of observations rather than one. It distinguishes a strategy that touched −12% once from one that spent eight months between −8% and −12%. Those have similar max drawdowns and are not similar strategies.
- **Time under water**, specifically the 95th percentile of recovery duration. This is the component that maps to whether the system will still be running. A strategy with a 10% drawdown that recovers in three weeks and one with a 10% drawdown that takes fourteen months are not equivalent risks, because during month nine a human turns it off.

Weight: 0.20.

---

## 5. Component three: cross-regime consistency

History is partitioned into regimes and the strategy is scored on its **worst** regimes, not its average.

Partitioning is by realised-volatility tercile × trend sign (six buckets), computed from a 30-day rolling window on the benchmark, with regime labels assigned point-in-time — a bar's regime label may only use data available at that bar. Labelling regimes with hindsight is a look-ahead bug that produces beautiful and completely false regime-conditional performance, and it is easy to write by accident because the natural implementation computes regimes over the full series first.

The score uses the trade-weighted 20th percentile of per-regime Sharpe, not the mean and not the minimum. The mean lets one spectacular regime hide three bad ones. The minimum is too noisy — with six buckets, the worst bucket is often the one with 14 trades in it. The 20th percentile is a CVaR-style lower-tail statistic that is stable enough to optimise against.

**Why this catches strategy B.** A short-vol strategy that earns steadily in low-vol regimes and bleeds in high-vol regimes has a fine mean and a terrible 20th percentile. It cannot hide, because the partition guarantees that the high-vol periods are evaluated on their own rather than being diluted by the 80% of history in which the strategy works.

A strategy that is *only* profitable in one regime is not automatically retired — a regime-specialist with an honest regime gate is a legitimate strategy. But it must declare the gate in its signal logic, so that in the wrong regime it emits `flat` and accumulates no trades in that bucket rather than accumulating losses. The distinction the score makes is between "profitable in one regime and flat in the others" (fine) and "profitable in one regime and bleeding in the others" (not fine).

Weight: 0.15.

---

## 6. Component four: per-trade edge after costs

The number that matters is not the edge. It is the edge **divided by the cost of capturing it**.

```
edge_multiple = mean net PnL per trade (bps of notional) / mean round-trip cost (bps)
```

An edge of 3 bps per trade is excellent for a strategy paying 1 bp round trip and fatal for one paying 8 bp. Reported in isolation, "3 bps of alpha per trade" is not information. This is the most common way a backtest lies to a competent person: the edge is real, the cost model is optimistic by 2 bps, and the strategy is underwater in production while the researcher checks their signal logic for the third time.

Two hard rules follow:

- **Cost parameters are calibrated from production market data, never from testnet.** Measured: Binance futures testnet shows roughly a 7.5 bp spread against production's 0.16 bp, with volume inflated by about 10×. A strategy calibrated on testnet costs is calibrated against fiction in both directions — the spread is 45× too wide and the depth is 10× too deep. See `CLAUDE.md` §2 and `BACKTEST_ENGINE.md`.
- **The cost denominator includes everything**: taker/maker fees at the actual tier, half-spread at the observed depth for the actual size, modelled market impact, and funding for anything held across a funding timestamp. Funding is the one people drop, and for a perpetuals strategy holding through three funding events a day it is frequently larger than the fee.

An `edge_multiple` below 1.0 means the strategy is a cost-generating machine that happens to also trade. Below 1.5 it is not robust to a cost-model error of the size we routinely make.

Weight: 0.15.

---

## 7. Component five: capacity

This is the component whose inclusion surprises people, because the system trades a demo account with no capital constraint. Capacity is included anyway, and not because we anticipate scaling.

**Capacity is a proxy for whether the edge is economically real.**

An edge that exists at $500 of notional and vanishes at $50,000 is, with high probability, not an economic phenomenon. It is one of: a fill-model artifact (the backtest assumed you got the touch), a microstructure effect too small to survive its own costs, or curve-fitting to individual prints. Real edges come from something structural — a flow imbalance, a funding dislocation, a persistent behavioural pattern — and structural things have depth behind them.

Capacity is estimated by solving for the notional at which modelled impact consumes half the gross per-trade edge, using a square-root impact model:

```
impact_bps(Q) = η · σ_bps · √(Q / ADV)      ⟹      Q* = ADV · ( edge_gross_bps / (2 · η · σ_bps) )²
```

`η` is calibrated per symbol from production data. `Q*` is expressed in USD and enters the score on a log scale.

The diagnostic value is in the *ratio* `Q* / Q_traded`. A strategy operating at 1% of its estimated capacity is fine. A strategy whose estimated capacity is below the venue's minimum notional has an "edge" that cannot be expressed as a trade, and its backtest is describing something that did not happen.

Weight: 0.10.

---

## 8. Component six: adaptability and out-of-sample decay

Every edge decays. The question is only how fast, and whether the decay is faster than the evaluation cycle that is supposed to catch it.

Two measurements:

- **Decay ratio** `δ = SR_out_of_sample / SR_in_sample`. The empirically honest prior across the published literature and our own history is `δ ≈ 0.5` — half the in-sample edge survives contact with new data. The score is calibrated so that `δ = 0.5` maps to the *neutral* value, not to a good one. A strategy that keeps half its edge is normal, not impressive. A strategy that keeps 90% is either exceptional or has a leak somewhere in its validation, and the second is more likely.
- **Alpha half-life.** Regress the rolling 60-day Sharpe on time since validation; from the slope, estimate the half-life `h = ln2 / λ`. If the projected Sharpe falls below the retention floor within one evaluation cycle, the strategy is retired now rather than after the cycle confirms it. A strategy on a visible decay trajectory is not a strategy with a good current score; it is a strategy with a stale one.

A subtlety on the word "adaptability": a strategy that *adapts* — refits parameters online — is not automatically better. Online refitting is an additional degree of freedom, which means additional trials, which means the deflation term rises. Adaptive strategies must clear a higher bar, and the trial counter must be incremented for every online refit. Most implementations forget this and adaptive strategies then dominate the population for reasons that are purely statistical.

Weight: 0.10.

---

## 9. Risk violations: the hard negative

Every other component is bounded in `[0, 1]` and multiplied by a weight, and the weights sum to 1. The maximum achievable performance score is therefore exactly 1.0.

**The violation penalty is unbounded above.** That asymmetry is the mechanism, and it is the whole answer to "how do you guarantee a strategy cannot profit its way past the limits". It cannot, because there is no amount of profit that reaches 1.5.

A single hard-limit breach costs 0.5 — half of everything a perfect strategy could ever earn. Two breaches produce a negative score regardless of performance, which ranks the strategy below a strategy that never traded. This is intended. A strategy that breached twice is worse than nothing: it consumed a slot, it consumed trials, and it demonstrated that its behaviour is not bounded by its stated design.

What counts as a violation, in descending severity:

| Class | Examples | Penalty |
|---|---|---|
| **Hard breach** | Exceeded a compiled ceiling; attempted to construct an order; bypassed the invalidation contract; caused a kill-switch trip attributable to this strategy | 0.50 each |
| **Limit breach** | Strategy drawdown limit hit; daily loss contribution over budget; exposure limit hit at portfolio level with this strategy as the marginal cause | 0.20 each |
| **Discipline** | Signal emitted with `invalidation=None` in more than 5% of signals; conviction miscalibration beyond tolerance; signal rate exceeding declared frequency by >2× | 0.05 each |

Two properties worth stating explicitly:

1. **A violation is recorded even if it was blocked.** The risk engine rejects the order — the portfolio is never actually exposed. The violation is still counted, because the strategy demonstrated intent, and intent is the thing that predicts future behaviour. A strategy whose signals are routinely clipped by risk limits is being *defined* by the risk engine rather than by its own thesis, and its backtest — where the same limits applied — is not describing what it would do unconstrained. This is the single most useful signal for finding a strategy that has learned to exploit the sizing rules.

2. **Violations never expire, but they decay in *weight*.** The count is permanent in the audit log. The penalty applied to the current score uses violations within the trailing 12 months, so a strategy can, in principle, recover. It cannot recover by being profitable, only by being clean for a long time.

---

## 10. Minimum sample sizes, and why a 20-trade Sharpe is meaningless

Below the minimum sample, the scoring engine does not return a low score. It returns `INSUFFICIENT_SAMPLE`, which is a distinct outcome that no promotion gate accepts. This matters: a low number is comparable and can be ranked, and a rankable number for an unmeasured strategy will eventually win a comparison against a well-measured mediocre one.

### The arithmetic

For `n` observations, the standard error of an estimated Sharpe ratio is approximately

```
SE(ŜR) ≈ √( (1 + ŜR²/2) / n )
```

in per-observation units. Annualising by `√f` (with `f` observations per year) scales the standard error identically.

Take a strategy with 20 trades over 60 days, evaluated on daily returns (`n = 60`, `f = 252`):

```
SE(SR_annual) ≈ √(252/60) ≈ 2.05
```

An observed annualised Sharpe of **2.0** therefore carries a 95% confidence interval of roughly **[−2.0, +6.0]**. It is not merely imprecise. It is statistically indistinguishable from a strategy with no edge whatsoever, and equally indistinguishable from one of the best strategies ever recorded. Reporting it to three decimal places, as every backtesting library does, is an act of false precision that has cost people real money.

### Now add the search

The previous paragraph assumed one strategy. We run thousands.

The expected maximum of `K` draws from a standard normal is approximately `√(2 ln K)`. So the best of `K` *pure-noise* strategies, each measured with standard error `SE`, will show an apparent Sharpe of about `√(2 ln K) · SE`.

With per-trade Sharpe measured over `n = 20` trades, `SE ≈ 1/√20 ≈ 0.224`. With `K = 1000` trials:

```
best-of-noise per-trade Sharpe ≈ √(2 · ln 1000) · 0.224 ≈ 3.72 · 0.224 ≈ 0.83
```

Annualised at 100 trades per year, that is a Sharpe of **8.3**, produced by a search over strategies with *exactly zero* edge. If a 20-trade evaluation window ever shows you a Sharpe of 8, the correct inference is not "we found something".

This is why the sample minimums are not negotiable and why the deflated Sharpe (which formalises exactly this correction) is the input to the score rather than the raw one.

### The thresholds

| Gate | Minimum |
|---|---|
| Any numeric score at all | 30 closed trades **and** 90 calendar days **and** ≥ 2 regimes with ≥ 10 trades each |
| Validation (CPCV) | 200 trades aggregated across CV paths |
| Promotion to challenger | 60 forward trades, 30 forward days, zero risk violations |
| Promotion to champion | 100 forward trades, 90 days concurrent with the incumbent |
| Kelly sizing enabled | 100 closed trades (`RISK_PHILOSOPHY.md` §3.3) |
| Conviction calibration enabled | 100 closed trades (`RISK_PHILOSOPHY.md` §2) |

All of the above use **effective** sample size, not raw count:

```
n_eff = n / (1 + 2·Σ_{k=1}^{K} ρ_k)
```

where `ρ_k` is the lag-`k` autocorrelation of the trade-return series. This is not pedantry. A strategy holding 5-day positions and re-signalling daily produces returns with ~0.8 first-order autocorrelation, and 100 such trades carry roughly the information of 20 independent ones. Overlapping positions are the standard way a strategy manufactures an impressive-looking trade count, and they are common in exactly the momentum and carry designs the mutation operators generate most readily.

The calendar-days requirement exists independently of the trade count for a reason the trade count cannot capture: a high-frequency strategy can accumulate 500 trades in a week, all of them inside one regime, one funding cycle, and one market structure. Sample size in trades and sample size in *conditions* are different quantities, and only the second one tells you whether the edge generalises.

---

## 11. What survival does not mean

- **Not "made the most money."** A strategy can be the top earner and be retired for a drawdown breach in the same cycle.
- **Not "never loses."** A strategy that never loses over a 90-day window with 200 trades is a red flag, not an achievement — see §2. The scoring engine treats an implausibly high win rate combined with a negative payoff ratio as a tail-risk signature and it does not score well.
- **Not permanent.** A champion is re-scored every cycle against the same bar it cleared. Incumbency confers a promotion margin (`EVOLUTION_ENGINE.md` §3), not immunity.
- **Not recoverable after retirement.** Retirement is terminal for that genome. See `EVOLUTION_ENGINE.md` §8 for why, including the tombstone mechanism that stops the search from rediscovering the same corpse every generation.
