# Risk Philosophy

Why the risk engine sits *in* the order path rather than beside it, and how it decides size.

Read `ARCHITECTURE.md` §5 first — this document expands it. `FAILSAFE.md` covers the kill switch's operational side; here we cover why it exists and what trips it.

---

## 1. Risk is a structural position, not a service

The data flow is `Signal → risk → Order`. The risk engine is not a library that strategies call. It is a component strategies *cannot* call, because it sits downstream of them and nothing flows around it.

The distinction matters more than it looks. Three designs are commonly confused:

| Design | What a strategy can do | Failure mode |
|---|---|---|
| Risk as a library (`size = risk.size_for(...)`) | Call it, ignore it, call it with the wrong arguments, cache the result and reuse it | The check is only as good as the caller's discipline |
| Risk as a middleware/filter | Reject an order after it was constructed | The order already exists as an object with a quantity someone chose; rejection is a veto over a decision already made |
| Risk as the sole constructor of `Order` | Emit a `Signal` and nothing else | A strategy that wants to size has no type to express it in |

We take the third. `Order` has no public constructor reachable from `strategy`; the only path to one is `RiskEngine.decide(signal, portfolio_state, market_state) -> Order | Rejection`. `import-linter` proves that `strategy` has no import path to `execution`, and the risk module owns the only factory.

**The reason this rigidity is worth it is not human discipline.** A careful human developer would be fine with a library. This system will eventually generate its own strategies via LLM agents, and an agent asked to "improve returns" will size its own positions the moment the type system permits it — not maliciously, but because increasing size is the shortest path to the metric it was given. The constraint must hold against an author who has not read this file and never will.

### The second-order reason: attribution

There is a quieter benefit. Because exactly one component constructs orders, exactly one component knows the full portfolio at decision time. Per-strategy sizing makes portfolio-level reasoning impossible — you cannot compute a correlation-aware exposure limit from inside a strategy that only sees its own book. The centralisation is not just a safety property; it is what makes §5 computable at all.

---

## 2. What a strategy is allowed to say

```python
direction: Literal["long", "short", "flat"]
conviction: Decimal          # 0..1
horizon: timedelta
invalidation: Decimal | None # price at which the thesis is wrong
rationale: str
```

Conviction and invalidation are the two channels through which a strategy influences size. Both are deliberately indirect.

**Invalidation is mandatory in practice even though the type allows `None`.** A `Signal` with `invalidation=None` is accepted by the type system but sized under the "no stop" branch, which is materially smaller (see §3.1) and flagged in the audit trail. A strategy that cannot say what would prove it wrong has a hope, not a thesis, and it gets hope-sized.

**Conviction is not trusted as reported.** This is the part people get wrong. If a strategy can emit `conviction=1.0` and that number multiplies notional, you have reinvented strategy-side sizing with extra steps. Conviction is therefore passed through a per-strategy calibration map before it is used:

```
r_used = r_min + calibrated(conviction) · (r_max − r_min)
```

with `r_min = 0.25%` and `r_max = 1.00%` of equity risked per trade. `calibrated()` is a monotone isotonic map fitted from that strategy's own history: bucket historical signals by reported conviction decile, measure realised hit rate and mean edge per bucket, and fit the map so that reported conviction is monotonically related to realised outcome. Until the strategy has ≥ 100 closed trades, `calibrated()` returns the constant 0.5 — every signal is sized identically regardless of what the strategy claims.

The effect: a strategy that reports high conviction indiscriminately finds its conviction channel flattened by its own record. It cannot inflate size by asserting confidence; it has to earn the gradient.

Below `conviction_floor` (0.15, `CONFIGURATION.md` §8) the signal is discarded rather than sized down. A near-zero conviction is not a small opinion, it is the absence of one, and the correct response to the absence of an opinion is no position — not a tiny position whose expected edge cannot cover its own round-trip cost.

---

## 3. Position sizing

Three methods are computed independently. **The final size is the minimum of all of them, never the average.**

```
q = min(q_fixed_fractional, q_vol_target, q_kelly, q_exposure, q_venue_filters)
```

Averaging is the intuitive choice and it is wrong: it lets an overconfident method be rescued by a conservative one, which means the overconfident method's errors survive into production at reduced amplitude. Taking the minimum gives every method veto power, so a bug in any one of them can only make the system smaller. Every input to a risk calculation should have that property.

### 3.1 Fixed fractional

Risk a fixed fraction of equity per trade, sized to the distance to invalidation:

```
q = (r_used · E) / |P_entry − P_invalidation|
```

This is where the mandatory invalidation level pays for itself. Without it there is no denominator and "risk 0.5% per trade" is not a computable statement — you end up risking 0.5% of equity *in notional*, which is a completely different and much weaker claim.

If `invalidation is None`, the denominator falls back to `k · ATR_14` with `k = 2.5`, and `r_used` is additionally halved. A strategy that will not name its stop pays for the estimate.

Note what this does to the relationship between conviction and notional: a high-conviction signal with a distant stop gets a *smaller* position than a low-conviction signal with a tight stop. That is correct and frequently surprises people. The budget is risk, not exposure.

### 3.2 Volatility targeting

Size so that each position contributes a target volatility:

```
q = (σ_target / σ_asset) · E / P
```

`σ_target` defaults to 15% annualised per position, 12% at portfolio level.

The estimator matters more than the formula. We use

```
σ_used = max(σ_ewma(λ=0.94), σ_60d, σ_floor)
```

EWMA with λ = 0.94 is the RiskMetrics daily parameter (≈ 33-day effective half-life). Taking the **maximum** of a fast and a slow estimate, never a blend, encodes an asymmetry: underestimating volatility is expensive and overestimating it is merely suboptimal. A fast estimator in a calm regime is exactly the configuration that maximises size immediately before a volatility expansion — the estimator's error and the market's move are positively correlated in the direction that hurts.

`σ_floor` is set per symbol at the 10th percentile of its 3-year realised volatility distribution. Crypto majors go quiet for weeks and then do not; the floor prevents the 3 a.m. quiet period from producing a 6× position.

### 3.3 Kelly, and why we use a quarter of it

For a continuously rebalanced position with excess return `μ` and volatility `σ`, the growth-optimal fraction is

```
f* = μ / σ²
```

and the expected log-growth rate at fraction `f` is

```
g(f) = f·μ − f²σ²/2      with      g(f*) = μ² / (2σ²)
```

Full Kelly is genuinely optimal: no other fixed fraction produces higher long-run geometric growth, and betting `f*` almost surely outperforms any other fixed fraction as `T → ∞`. Every word of that is true and every word of it is conditional on knowing `μ` and `σ`. We do not.

**The estimation problem, quantified.** `f*` is a ratio of two estimated quantities, and the numerator is the badly estimated one. With `T` years of data,

```
SE(μ̂) = σ / √T      ⟹      SE(μ̂)/μ = 1 / (SR · √T)
```

So the *fractional* standard error on your Kelly numerator is `1/(SR·√T)`. At a Sharpe ratio of 1.0 with one year of data, the standard error on `μ̂` is 100% of `μ̂` itself. To pin `μ` down to ±25% at SR = 1 you need **16 years** of data. We will never have that for a crypto strategy whose regime lifetime is measured in months. Adding data does not rescue this: the error falls as `√T` while the alpha decays on a schedule that is usually faster.

**The asymmetry, quantified.** Substituting into `g(f)`:

- `g(f*/2) = μ²/(2σ²) − μ²/(8σ²) = 0.75 · g(f*)` — half Kelly keeps **75% of the growth for half the volatility**.
- `g(2f*) = 2μ²/σ² − 2μ²/σ² = 0` — double Kelly has **exactly zero** expected log growth.
- Beyond `2f*`, expected log growth is negative: ruin is not a tail event, it is the expectation.

Now combine the two results. Your estimate of `f*` carries a 100% standard error at one year and SR = 1. A 2× overestimate is a one-sigma event. A one-sigma error on the upside takes you from maximum growth to zero growth; a one-sigma error on the downside costs you 25% of growth. That asymmetry — not squeamishness — is the entire argument for fractional Kelly.

**The drawdown result seals it.** Under full Kelly, the probability of the equity curve ever touching a fraction `x` of its starting value is exactly `x`. Under fractional Kelly `f = c·f*`, it is `x^(2/c − 1)`:

| Kelly fraction `c` | P(ever down 20%) | P(ever down 50%) | P(ever down 80%) |
|---|---|---|---|
| 1.00 (full) | 0.80 | 0.50 | 0.20 |
| 0.50 (half) | 0.51 | 0.125 | 0.008 |
| 0.25 (quarter) | 0.21 | 0.008 | 2.6e-5 |

A full-Kelly bettor with a *perfectly known* edge has a coin-flip chance of halving their capital at some point. With an unknown edge it is worse than that. We cap at `max_kelly_fraction = 0.25`, with a compiled ceiling of 0.50.

**One more thing Kelly assumes that we do not have:** stationarity. `f*` is derived for a repeated gamble with a fixed distribution. A strategy whose edge decays — which is all of them, see `SURVIVAL_PROTOCOL.md` §8 — is betting a fraction computed from a distribution that no longer exists. Fractional Kelly is partly a stationarity haircut, not only an estimation haircut.

The Kelly estimate itself uses the strategy's own realised per-trade distribution, not the backtest's, and returns `q = 0` below 100 closed trades. Before that, `q_kelly` is simply not one of the terms in the `min()`.

---

## 4. Portfolio construction: the min() has a portfolio term

`q_exposure` is the binding constraint most of the time, and it is computed at portfolio level.

| Limit | Default | Hard ceiling |
|---|---|---|
| Max notional in one position | 5% of equity | 10% |
| Max gross exposure | 200% of equity | 300% |
| Max net directional exposure | 100% of equity | 150% |
| Max exposure to one asset (all strategies) | 15% of equity | 25% |
| Max risk contribution from one correlation cluster | 25% of portfolio σ | 40% |
| Min free margin | 40% of equity | (floor, 25%) |

These are the *relative* limits, expressed as fractions of equity because that is the form in which portfolio risk arithmetic is meaningful. They are applied jointly with the **absolute** caps declared in `CONFIGURATION.md` §8 — `max_position_notional_usd`, `max_portfolio_notional_usd`, `max_single_order_notional_usd`, `max_open_positions`, `max_orders_per_minute`. Both sets enter the same `min()`, and whichever binds first wins.

Keeping both is not redundancy. A percentage limit scales with equity, which is correct for risk but means a runaway equity calculation (a bad mark, a duplicated fill, a currency conversion error) silently authorises a larger position. An absolute notional cap does not scale with anything and therefore does not participate in that failure. Each covers the other's blind spot.

---

## 5. Correlation-aware exposure, and why per-strategy limits are a fiction

The naive design gives each strategy a budget: "no strategy may exceed 5% of equity". Five strategies, each within its limit, each long a different altcoin, is presented by that design as a diversified 25% book. In a crypto drawdown it is one 25% position, because pairwise correlations among majors and large alts run 0.80–0.95 in stress even when they run 0.4 in calm.

Per-strategy limits do not constrain concentration. They constrain *bookkeeping*.

### The correct object

The risk engine holds a portfolio weight vector `w` (signed, in units of equity) and a covariance matrix `Σ`, and computes:

```
σ_p       = √(wᵀ Σ w)                 portfolio volatility
MCR_i     = (Σ w)_i / σ_p             marginal contribution to risk
CTR_i     = w_i · MCR_i               component contribution;  Σ_i CTR_i = σ_p
```

Limits are expressed on `CTR_i / σ_p`, the *share of portfolio risk*, not on notional. A 3% notional position in a high-beta alt can carry more risk share than a 10% position in BTC, and the notional limit alone would have the ranking backwards.

### Clustering

Assets are clustered each night by hierarchical clustering on the correlation distance

```
d_ij = √(0.5 · (1 − ρ_ij))
```

(the standard metric — it is a proper distance, unlike `1 − ρ`), cut at the level corresponding to `ρ = 0.7`. Cluster risk share is the sum of member `CTR`s. The cluster limit binds before the single-asset limit in almost every real portfolio, which is the point.

### The estimate you must use is not the current one

**Correlations are estimated on a 60-day EWMA and then floored:**

```
ρ_used_ij = max(ρ_ewma_ij, ρ_p95_ij)
```

where `ρ_p95_ij` is the 95th percentile of the rolling 60-day correlation between `i` and `j` over the last three years.

This is deliberately pessimistic and it is the single most important line in the risk engine's portfolio maths. Correlations rise toward 1 precisely during the events the limits exist to survive. Sizing on today's calm-market correlation matrix means the diversification you were counting on evaporates in the same hour your positions start losing. Sizing on the stress matrix means you are permanently slightly under-diversified in calm markets — a real, small, ongoing cost paid to avoid a large, rare, correlated one. That trade is worth making every time.

A consequence worth stating: with stress correlations, adding a sixth correlated altcoin strategy to the portfolio increases the portfolio's capacity for it by nearly nothing. New strategies earn allocation by being *different*, not by being good. That is also why the evolution engine applies explicit diversity pressure (`EVOLUTION_ENGINE.md` §7) — without it, the population converges on one idea wearing six hats, and the risk engine silently refuses to fund five of them while the scoring engine keeps rewarding all six.

### Netting

Two strategies with opposing signals on the same symbol net before the order goes out. One net order, not two offsetting ones — otherwise you pay the spread twice for a zero net position.

The non-obvious part is attribution. When strategies cross internally, a strategy's "fill" does not correspond to any venue trade, so a naive implementation assigns it a synthetic price and total attributed PnL no longer equals venue PnL. The rule: **the crossed portion is booked at the same VWAP as the venue portion of the same net order.** If there is no venue portion (perfect internal cross), it is booked at the prevailing mid at decision time, and the difference versus the next observed trade is charged to a `crossing_residual` account, not to either strategy. Attribution must sum to reality or every survival score downstream is measuring fiction.

---

## 6. Drawdown limits

Two levels, both measured from a **persisted** high-water mark.

### Strategy level — default 15%, ceiling 25%

Measured on the strategy's attributed equity curve. On breach: open positions closed, strategy moved to `suspended`, a risk event recorded against it in the scoring engine, and no automatic resumption. It re-enters through evaluation like anything else.

**De-risking begins well before the limit.** A hard cliff at 15% means a strategy runs at full size at 14.9% drawdown and dies at 15.0%, which is the worst possible size schedule — maximum exposure at the moment of maximum evidence that something is wrong. Instead, size is scaled linearly across the last 40% of the drawdown budget:

```
m = clip( 1 − (DD/DD_max − 0.6) / 0.4 , 0, 1 )
```

At 60% of the budget (9% drawdown on a 15% limit) `m = 1` and scaling begins; at 100% it has already reached zero. The limit is then rarely hit at full size — it is approached asymptotically, which is what you want, because most drawdowns that reach 12% do not reach 15%, and a strategy that survives one at reduced size is still available afterwards.

### Portfolio level — default 10%, ceiling 20%

Breach trips the kill switch. See `FAILSAFE.md`.

The strategy limit (15%) being larger than the portfolio limit (10%) is not an inconsistency: the strategy limit is measured on that strategy's *attributed* equity, which is a fraction of the portfolio. A strategy running 20% of capital can lose 15% of its own allocation while contributing 3% to portfolio drawdown. The two limits bind on different quantities and both are needed — the portfolio limit stops the aggregate, the strategy limit stops one component from bleeding indefinitely under cover of others' profits.

The high-water mark is persisted in Postgres and restored before trading resumes on any restart. A restarted process that recomputes its high-water mark from an empty in-memory equity curve has silently reset its drawdown limit to "20% below wherever we are now", which converts a hard limit into a ratchet that never binds. This has been the failure in more than one real system and it looks like nothing in the logs. See `FAILSAFE.md` §4 on recovery ordering, and `ERROR_RECOVERY.md` §7 on why the restore is from persistence rather than recomputation.

---

## 7. Daily loss limit

Default 3% of start-of-day (00:00 UTC) equity, ceiling 5% (`max_daily_drawdown_ratio` in `CONFIGURATION.md` §8). Measured **mark-to-market including unrealised**, evaluated on every fill and every mark update, not on a schedule. A limit that is only checked hourly is a 3% limit with an hour of slack in it.

Breach halts new position opening for the remainder of the UTC day; existing positions are managed to their invalidation levels but not added to.

**The reset boundary is an exploitable seam.** Crypto has no session close, so 00:00 UTC is an arbitrary line. A strategy that loses 2.9% by 23:50 has its full budget back eleven minutes later, which means the real limit is closer to 6% over a 12-hour window than 3% over a day. This is not hypothetical — trend strategies cluster their losses in exactly this way during a reversal.

Mitigation: a second, rolling limit of **4.5% over any trailing 24 hours** (1.5× the fixed-window limit), evaluated continuously. The fixed-window limit exists for reporting and human intuition; the rolling one is the limit that actually binds during a bad night.

Also note the interaction with `σ_target`: a 3% daily loss limit against a 12% annualised portfolio volatility target (≈ 0.76% daily σ) is a 3.9-sigma day. Under a normal distribution that is a once-per-decade event; in crypto it happens a few times a year, which is itself the argument against using a normal distribution anywhere in this system. If the daily limit is being hit more than about once a quarter, the volatility estimate is wrong, not the market. Frequency of limit breaches is monitored as a calibration signal on the estimator, not just as an incident count.

---

## 8. The kill switch, from the risk side

Operational detail is in `FAILSAFE.md`. What matters here:

The kill switch check is **the first statement in `RiskEngine.decide()`**, guarded by the same lock that guards order construction. It is not a subscriber to an event, it is not a background task, it is not a check in the execution layer. Because it shares the lock with the only code path that can construct an `Order`, there is no window in which the switch is tripped and an order is nonetheless constructed. Latency is not a design parameter for the *blocking* half of the kill switch; the blocking half is a memory read on the critical path.

Latency is a design parameter for the two halves that involve the outside world: trip decision within 100 ms p99 of the triggering event reaching the bus, and cancellation of all resting orders submitted within 2 s p99.

`FAILSAFE.md` §2.4 and [ADR 0014](docs/adr/0014-kill-switch-flattens-on-trip.md) explain why the trip **flattens the book**, and why it sizes the exit from venue state rather than from local position records. Briefly: an unhandled exception already flattens (`.claude/rules/error-handling.md`), so a kill switch that did not would make the response to uncertainty depend on which code path noticed it; and "let a human decide" needs a human, which an unattended system does not have at 03:00. The cost — flattening is a market order under the conditions that produce the worst fills — is real, tracked as `killswitch.flatten_slippage_bps`, and is the ADR's stated revisit trigger.

Resume requires a human command with an incident ID, a non-empty root cause, and a clean reconciliation within the preceding five minutes. There is no automatic resume, including when the drawdown recovers. A system that unhalts itself has a kill switch in name only.

---

## 9. Limits are configuration; ceilings are compiled in

Every limit in this document is configurable. Every limit also has a hard ceiling — or, for the limits where *smaller* is riskier, a hard floor — that is a compiled-in constant in `fking.risk.ceilings`, not read from config, environment, database or file. The declaration lives in `CONFIGURATION.md` §8; what follows is why it has the shape it does.

Three properties are load-bearing:

**1. Configuration can only make the system more conservative.** There is no config value, no environment variable and no API call that widens a limit past its ceiling. Widening requires a source edit and a PR labelled `safety:critical`, exactly like the host allowlist. The direction of friction matches the direction of risk: tightening is free, loosening is expensive.

**2. Overreach is rejected at startup, never silently clamped.**

Clamping is the tempting implementation — take `min(configured, ceiling)` and carry on, and the system is provably never out of bounds. It is the wrong choice, for the reason developed in `DECISION_FRAMEWORK.md` §8: a clamp is a silent substitution of a default, which is the second-worst failure mode available. Someone wrote `max_position_notional_usd: 50000` because they believed they were running at $50k. Clamping to $25k means the system is safe *and* the person is wrong *and* nobody finds out. They will make the next decision on the belief they still hold.

Rejecting at startup makes the misunderstanding visible at the only moment it is cheap to fix, and costs a restart. A validator raising `ValueError` with both the requested and the ceiling value in the message is the entire mechanism.

**3. The floors are where the bug lives.** Most limits are "larger is riskier" and are bounded above. A few — `min_free_margin_ratio`, `min_trades_for_kelly`, `conviction_floor` — are "smaller is riskier" and must be bounded *below*. A single validation loop that compares every configured value against a single `HARD_CEILINGS` mapping with `>` handles the first group correctly and the second group backwards: it would happily accept `min_free_margin_ratio = 0`.

The two sets are therefore separate mappings with separate validators, and — the part that actually prevents the mistake — separate *types*. A `Ceiling(Decimal)` and a `Floor(Decimal)` newtype means the uniform-comparison version does not typecheck. Relying on a reviewer to notice a `>` that should be a `<` in a loop over a dictionary is relying on the least reliable review capability there is.

A Hypothesis property test in `tests/risk/test_limits_property.py` asserts, over arbitrary generated configs, that every accepted `RiskSettings` satisfies every ceiling and every floor, and that every rejected one violates at least one. That test is the actual guarantee; the validator is just how it is currently satisfied. `risk` carries a 95% coverage floor, and the ceilings module specifically is held at 100%.

---

## 10. What this philosophy costs

Being explicit about the downside, because a risk document that only lists benefits is marketing.

- **Stress correlations and `max()` volatility estimates mean the system is systematically under-levered in calm markets.** Expect measurably lower returns than a naive vol-targeted book during quiet quarters. That is the premium being paid.
- **Quarter Kelly retains only 43.75% of theoretically available log growth** (`g(c·f*)/g(f*) = 2c − c²`, which is 0.4375 at `c = 0.25` and 0.75 at `c = 0.5`). We give up more than half the theoretical growth rate to buy a drawdown distribution a human can actually hold through.
- **The `min()` across sizing methods means the most conservative method dominates**, so improving the other two has no effect until the binding one is improved. Anyone tuning sizing must first check which term is binding; tuning a non-binding term is the most common wasted afternoon in this codebase.
- **Conviction calibration means a genuinely well-calibrated new strategy is under-sized for its first 100 trades.** Real cost, accepted, because the alternative is trusting a number a strategy assigned to itself.
