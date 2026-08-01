# Scoring Engine

The objective function. This is the number the entire system optimises toward.

`SURVIVAL_PROTOCOL.md` argues for the shape of this function; this document specifies it. `EVOLUTION_ENGINE.md` describes what consumes it.

---

## 0. Safety classification

**The scoring engine is the second most safety-critical component in this repository, after the safety kernel.**

Not the risk engine. Not the execution layer. The scoring engine, because:

- The safety kernel bounds what the system *can* do.
- The scoring engine determines what the system *will try* to do, forever, without supervision.

A bug in the risk engine loses money once and is visible. A bug in the scoring engine causes the evolution engine to spend months breeding strategies that optimise the bug. Nothing alarms. The metrics look excellent — they are, by construction, the metrics being optimised. The failure surfaces as forward performance that never matches validation, months later, with an entire population that has to be discarded.

Consequently:

- Changes to `fking.evolution.scoring` require a PR labelled `safety:critical`, the same review level as changes to risk limits or the host allowlist.
- Every weight and every scale constant in this document has a comment in the source citing its justification. A magic number here with no provenance will be "cleaned up" by someone who does not know what it selects for.
- Component functions are pure, take an injected clock, and are covered by Hypothesis property tests asserting monotonicity (§7).
- Score changes are versioned. A score is stored with the `scoring_version` that produced it, and scores from different versions are never compared. Re-scoring the population after a change is a batch job, not an implicit consequence of deployment.

---

## 1. The top-level formula

```
S  =  S₀  +  w(n_eff) · ( P − S₀ )  −  V
```

where

| Term | Meaning | Range |
|---|---|---|
| `P` | weighted performance score | `[0, 1]` |
| `w(n_eff)` | confidence weight from effective sample size | `[0, 1)` |
| `S₀` | pessimistic prior for an unproven strategy | `0.35` (re-estimated quarterly, §5) |
| `V` | risk-violation penalty | `[0, ∞)` |
| `S` | survival score | `(−∞, 1]` |

The asymmetry between `P` (bounded above by 1) and `V` (unbounded) is the mechanism that makes the survival protocol's core guarantee true rather than aspirational: no amount of performance can outrun violations, because the reward term saturates and the penalty term does not.

If the sample gate in `SURVIVAL_PROTOCOL.md` §10 is not met, the engine returns `INSUFFICIENT_SAMPLE` — a distinct variant of the result type, not a number. It has no ordering relative to real scores and no promotion gate accepts it.

---

## 2. The performance term

```
P = 0.30·c_rar + 0.20·c_dd + 0.15·c_reg + 0.15·c_edge + 0.10·c_cap + 0.10·c_ada
```

Weights sum to 1.0, asserted at import time.

Every component is mapped into `(0, 1)` by a normal CDF with an explicit scale constant:

```
squash(x, x₀, s) = Φ( (x − x₀) / s )
```

This is a deliberate uniformity. Because every component uses the same squashing function, `0.5` means "neutral / no edge" for *all* of them, and the weights are directly comparable — a 0.1 improvement in any component moves `P` by `weight × 0.1` regardless of the component's native units. Ad-hoc per-component normalisation (min-max over the population, z-scores, percentile ranks) destroys this property and, worse, makes a component's contribution depend on the rest of the population, which means a strategy's score changes when an unrelated strategy is added. Scores must be absolute, not relative, or the lifecycle transitions in `EVOLUTION_ENGINE.md` §3 are not well-defined.

### 2.1 `c_rar` — risk-adjusted return, deflated

The input is the **deflated Sharpe ratio** (Bailey & López de Prado), which is already a probability in `[0, 1]`, so no squashing is applied:

```
                ( ŜR − SR* ) · √(N − 1)
DSR = Φ( ───────────────────────────────────── )
          √( 1 − γ₃·ŜR + ((γ₄ − 1)/4)·ŜR² )

c_rar = DSR
```

- `ŜR` — observed Sharpe in **per-observation** units (not annualised; the formula's variance term assumes per-observation scale).
- `N` — number of return observations on the daily grid (§4).
- `γ₃`, `γ₄` — skewness and kurtosis of the return series.
- `SR*` — the expected maximum Sharpe under the null of zero edge, given `K` trials:

```
SR* = √Var(ŜR_k) · [ (1 − γ)·Φ⁻¹(1 − 1/K)  +  γ·Φ⁻¹(1 − 1/(K·e)) ]
```

with `γ = 0.5772156649` (Euler–Mascheroni), `e = 2.71828…`, and `Var(ŜR_k)` the variance of Sharpe estimates across the `K` trials in the same search.

Two properties to internalise:

**The kurtosis and skew terms are in the denominator with signs that punish the §2 pathology.** Negative skew (`γ₃ < 0`) increases the denominator, lowering `DSR`. Excess kurtosis (`γ₄ > 3`) does the same. A strategy that earns steadily and loses catastrophically has exactly that moment signature, so the statistical correction and the economic objection agree. Uncorrected Sharpe does the opposite: it rewards the shape.

**`K` bites slowly and relentlessly.** `SR*` grows roughly as `√(2 ln K)`. Going from `K = 100` to `K = 10,000` raises the noise threshold by only `√(ln 10000 / ln 100) = √2 ≈ 1.41×`. This is why people underestimate the damage from trial inflation — a 100× increase in search effort raises the bar by only 41%, which feels survivable, and so the counter is allowed to drift. But it never comes back down, and it applies to every strategy in the population simultaneously. See `EVOLUTION_ENGINE.md` §5.1 on what counts as a trial.

### 2.2 `c_dd` — drawdown discipline

```
c_dd = 0.5·Φ( (MAR − 0.5)/0.5 )  +  0.3·Φ( (Martin − 2.0)/2.0 )  +  0.2·c_tuw

MAR    = CAGR / MaxDD
Ulcer  = √( mean_t( D_t² ) ),   D_t = 100 · (equity_t / peak_t − 1)
Martin = CAGR / Ulcer
c_tuw  = clip( 1 − TUW₉₅ / 90 days , 0, 1 )
```

`TUW₉₅` is the 95th percentile of drawdown recovery duration in days. The 90-day scale is chosen because it is roughly the point at which a human operator stops believing the strategy — an honest constant, sourced from human behaviour rather than from statistics, and commented as such in the source.

The blend weights (0.5/0.3/0.2) intentionally give the majority to MAR even though the ulcer index is the statistically better estimator, because MAR is what a human will check when auditing the score and a score whose components a human cannot reproduce by hand is a score nobody trusts.

### 2.3 `c_reg` — cross-regime consistency

```
c_reg = Φ( SR_q20 / 0.5 )
```

`SR_q20` is the trade-count-weighted 20th percentile of annualised per-regime Sharpe across the six regime buckets (vol tercile × trend sign, labelled point-in-time). Regimes with `n_eff < 10` are excluded from the percentile and their absence is recorded — a strategy evaluated on four of six regimes carries a `regime_coverage` flag that blocks promotion to champion.

Scale `s = 0.5`: a worst-quintile regime Sharpe of 0.5 maps to `Φ(1) = 0.841`; zero maps to 0.5; `−0.5` maps to 0.159.

### 2.4 `c_edge` — per-trade edge after costs

```
c_edge = Φ( ē_net_bps / c̄_bps )
```

The denominator is the strategy's own mean round-trip cost in basis points: exchange fees at the actual tier, half-spread at observed depth for the actual traded size, modelled impact, and funding accrued across holding periods.

The scale constant is therefore *the strategy's own cost*, not a global number. This makes the component read as "how many multiples of your trading cost do you keep per trade", which is the only form in which a per-trade edge is comparable across strategies with different holding periods, venues and size profiles. `Φ(1) = 0.841` for a strategy that nets exactly one round trip's worth of cost per trade; `Φ(0) = 0.5` for a strategy that exactly pays for itself.

Cost parameters come from production market data. Never testnet. See `CLAUDE.md` §2.

### 2.5 `c_cap` — capacity

```
Q* = ADV · ( ē_gross_bps / (2 · η · σ_bps) )²          USD notional
c_cap = clip( log₁₀( Q* / 1000 ) / 3 , 0, 1 )
```

`η` is the square-root-impact coefficient, calibrated per symbol from production trade and quote data. The log scale maps `Q* = $1,000 → 0` and `Q* = $1,000,000 → 1`.

If `Q*` falls below the venue's minimum notional for the symbol, `c_cap = 0` **and** a `capacity_below_min_notional` flag is set, which blocks promotion outright regardless of score. An edge that cannot be expressed as a legal order is not an edge.

### 2.6 `c_ada` — adaptability / out-of-sample decay

```
δ = SR_oos / SR_is                    (SR_is clipped to ≥ 0.1 to avoid a blow-up)
c_ada = clip( (δ + 0.5) / 2 , 0, 1 )
```

Linear, not squashed, because the quantity is already a ratio on a natural scale and the anchor points matter more than the shape:

| `δ` | `c_ada` | Interpretation |
|---|---|---|
| ≤ −0.5 | 0.00 | Edge reversed out of sample |
| 0.0 | 0.25 | Edge vanished |
| **0.5** | **0.50** | **Half the edge survived — the empirical norm, scored neutral** |
| 1.0 | 0.75 | Edge fully survived |
| ≥ 1.5 | 1.00 | Better out of sample than in |

Anchoring the *neutral* point at `δ = 0.5` rather than `δ = 1.0` is the deliberate part. Half the in-sample edge surviving is the expected outcome across the literature and across our own history; treating it as good would make the median strategy look above average, which is how a scoring engine starts lying to itself.

`δ > 1.2` sets a `suspicious_oos` flag. Out-of-sample outperforming in-sample by 20%+ is more often a validation leak — an embargo that is too short, a feature computed on the full series, a regime label with hindsight — than a genuinely robust strategy. The flag routes the strategy to the leak-detection test battery before promotion, not to rejection.

---

## 3. The violation penalty

```
V = Σ_classes  λ_class · ( 1 + ln(1 + m_class) )
```

where `m_class` is the count of violations of that class in the trailing 12 months, and

| Class | `λ` | `V` at 1 | at 2 | at 5 |
|---|---|---|---|---|
| Hard breach | 0.50 | 0.85 | 1.05 | 1.40 |
| Limit breach | 0.20 | 0.34 | 0.42 | 0.56 |
| Discipline | 0.05 | 0.085 | 0.105 | 0.14 |

Read the first row against the fact that `P ≤ 1`. **One hard breach costs more than the difference between a perfect strategy and a neutral one.** A strategy with one hard breach and perfect performance scores `1.0 − 0.85 = 0.15`, below the prior `S₀ = 0.35` that an unproven strategy starts with. It ranks below a strategy nobody has evaluated yet. That is the intended ordering.

The logarithmic growth in count is intentional. A strategy with 20 limit breaches is not 20× worse than one with a single breach; the first breach carries nearly all the information ("this strategy's behaviour is not bounded by its design"), and subsequent ones are largely correlated repeats of the same defect. Linear accumulation would let a single bad week permanently dominate a score whose other terms are measured over years.

A violation is recorded **whether or not the risk engine blocked it**. The portfolio was never exposed; the strategy still demonstrated intent, and intent predicts. See `SURVIVAL_PROTOCOL.md` §9.

---

## 4. Normalisation across strategies with different trade frequencies

This is the part that is usually done wrong, and doing it wrong makes the entire leaderboard incomparable.

**Rule: all path statistics are computed on a fixed daily mark-to-market grid, identical for every strategy, regardless of trade frequency.** Sharpe, drawdown, ulcer index, time under water, regime Sharpes and the deflation inputs `N`, `γ₃`, `γ₄` all use daily marks of attributed equity. Only `c_edge` and `c_cap` are per-trade quantities, because they are properties of an individual trade rather than of the equity path.

The alternative — computing Sharpe on per-trade returns and annualising by the strategy's own trade frequency — makes `√f` a free parameter that the strategy controls. A strategy that trades 10× more often annualises its per-trade Sharpe by `√10 ≈ 3.16×`. Two strategies with identical equity curves and different trade counts then receive different Sharpes, and the evolution engine learns to trade more often, which is not an edge, it is a fee.

### The consequence you must accept

Marking daily means a strategy that is in the market 10% of the time is penalised by roughly `√0.10 ≈ 0.316` on Sharpe relative to its active-period Sharpe. A selective strategy with an excellent active-period edge will score lower than a mediocre always-on one with the same average return.

**This is correct and it is deliberate.** Capital allocated is capital committed. A strategy holding a slot and a risk budget while flat is consuming both. Rewarding active-period performance would make "trade rarely and only in perfect conditions" the dominant evolutionary strategy, and the resulting population would be unable to deploy capital.

But it is a real distortion, so it is reported rather than hidden. Every score carries `time_in_market` alongside it. The correct reading of "high active-period edge, low participation" is *an allocation problem, not a strategy problem*: the answer is to run it alongside complementary strategies so the slot is not idle, which is a portfolio construction decision (`RISK_PHILOSOPHY.md` §5), not a reason to retire it. The evaluation workflow explicitly checks for this pattern before issuing a retire verdict.

### Effective sample size

Everywhere `n` appears, it is `n_eff`:

```
n_eff = n / ( 1 + 2·Σ_{k=1}^{K} ρ_k )
```

`ρ_k` is the lag-`k` autocorrelation of the daily attributed-return series, summed until the first non-significant lag. For a strategy holding 5-day positions this typically gives `n_eff ≈ n/5`. Skipping this correction is how overlapping-position strategies manufacture apparent significance, and the mutation operators produce overlapping-position designs by default.

---

## 5. Confidence weighting: how the score changes as the sample grows

```
w(n_eff) = n_eff / (n_eff + n₀),        n₀ = 100
```

Half weight at 100 effective observations, 75% at 300, 90% at 900.

The score shrinks toward `S₀`, **not toward zero and not toward the neutral 0.5**:

```
S = S₀ + w(n_eff)·(P − S₀) − V
```

`S₀ = 0.35` is a *pessimistic* prior, below the neutral value of 0.5 that a zero-edge strategy would score with an infinite sample. Three consequences:

1. A brand-new strategy with a spectacular 40-observation backtest (`P = 0.95`, `n_eff = 40`) scores `0.35 + 0.286·0.60 = 0.52`. It does not lead the board. It has to accumulate evidence.
2. A brand-new strategy with a *terrible* short record is pulled *up* toward 0.35. Small samples are uninformative in both directions, and a scoring engine that shrinks bad small samples but not good ones is not doing Bayesian shrinkage, it is doing wishful thinking with extra steps.
3. A mature strategy at `n_eff = 900` with `P = 0.62` scores `0.35 + 0.9·0.27 = 0.593` and beats the spectacular newcomer. Proven mediocrity outranks unproven brilliance, which is the correct preference for a system that must run unattended.

**`S₀` is measured, not assumed.** Every quarter, the engine computes the median realised forward score of strategies that reached `paper` in the preceding year and sets `S₀` to it, bounded to `[0.20, 0.50]`. This makes the prior an empirical statement about *this* population's track record rather than a constant someone picked. If `S₀` drifts downward over successive quarters, the validation pipeline is getting worse at predicting, and that is itself an alarm (§6).

---

## 6. Detecting that the score is lying

The score is a hypothesis about the future. It must be tested like one.

### 6.1 Rank correlation, validation versus forward

For every strategy that has reached `paper` or beyond, record the pair `(S_validation, S_forward)` where `S_forward` is computed on strictly out-of-sample live/paper data. Compute the Spearman rank correlation `ρ_fwd` across the population, requiring at least 20 pairs.

| `ρ_fwd` | Verdict | Action |
|---|---|---|
| ≥ 0.40 | Healthy | Normal operation |
| 0.15 – 0.40 | Weak | Alert; promotion margins widened by 50% |
| < 0.15 for 2 consecutive quarters | **The score is not predictive** | **Freeze all promotions.** Scoring engine investigation becomes the top priority in the repository, above feature work. |

`ARCHITECTURE.md` §13 names this as the assumption most likely to be wrong. This is the instrument that detects it.

### 6.2 The failure mode rank correlation cannot see

Rank correlation measures *ordering*. It is entirely possible for `ρ_fwd = 0.7` — the score ranks strategies beautifully — while every strategy underperforms its predicted score by a growing margin. Ordering is preserved; calibration has collapsed. The score is still lying, just consistently.

So the second instrument is the **haircut**:

```
h_t = mean( S_validation − S_forward )   over strategies promoted in quarter t
```

A stable non-zero `h` is expected and fine — it is the in-sample optimism we already know about, and it can simply be subtracted. A **widening** `h` is the alarm: it means overfitting is increasing over time, which is exactly what happens as the trial counter climbs and the mutation operators converge on whatever the current scoring implementation rewards.

Monitored as a linear regression of `h_t` on `t` over the trailing 8 quarters. A slope significantly greater than zero at `p < 0.05` triggers the same investigation as §6.1, even with healthy rank correlation. Watching only rank correlation is the standard mistake and it is invisible for a long time.

### 6.3 The third instrument: component drift

Track the population-mean of each component `c_i` over time. If the population's mean `c_rar` rises while mean `c_reg` and `c_edge` stay flat, the search has found a way to inflate the Sharpe term specifically — a proxy, a leak, or a degenerate trade pattern — rather than finding better strategies. Genuine improvement raises several components together, because real edges are good for several reasons at once. A single component rising alone is almost always the population learning to exploit the scoring implementation.

---

## 7. Property tests that define the engine

The formulas above are the current implementation. These properties are the actual contract, tested with Hypothesis over generated strategy records in `tests/evolution/test_scoring_property.py`:

1. **Bounded reward.** For all inputs with `V = 0`, `S ≤ 1.0`.
2. **Violations dominate.** For any two strategies `A`, `B` where `A` has ≥ 1 more hard breach than `B`, and `B`'s performance is at least `P_A − 0.5`, then `S_B > S_A`. This is the survival protocol's core guarantee, expressed executably.
3. **Monotone in every component.** Increasing any `c_i`, holding the rest fixed, strictly increases `S`.
4. **Monotone in sample size for above-prior performance.** For `P > S₀`, `S` is strictly increasing in `n_eff`; for `P < S₀`, strictly decreasing.
5. **Frequency invariance.** Two strategies with identical daily attributed-equity curves but different trade counts receive identical `c_rar`, `c_dd`, `c_reg`, `c_ada`.
6. **Population independence.** `S` for a strategy is unchanged by adding or removing any other strategy from the database, except through `K` (trial count) and `S₀` (quarterly re-estimate), both of which are explicit inputs rather than implicit lookups.
7. **Determinism.** Same inputs, same `scoring_version`, same score, bit for bit, across processes and machines. `Decimal` throughout; no float accumulation in the weighted sum.

Property 6 is the one most likely to be broken by an innocent-looking refactor that introduces a percentile-rank normalisation "so the scores spread out nicer". If a strategy's score can change because an unrelated strategy was added, then lifecycle thresholds are not thresholds and the transition conditions in `EVOLUTION_ENGINE.md` §3 stop meaning anything.
