# Risk Vocabulary

## What you need to hold in your head

Almost every serious risk bug in a trading system is a units bug wearing a plausible name. "Size" can mean base quantity, notional, margin posted, or fraction of equity risked, and those four differ by factors of a hundred. "Leverage 3x" is not a statement until you say against what. "Drawdown 15%" is not a statement until you say from which high-water mark, measured on which price, over which equity curve. This document fixes the vocabulary the risk engine and the survival score actually use, with units attached, so that a number crossing a module boundary carries its meaning with it. The rule that follows from all of it: **a risk check never returns a boolean.** A boolean throws away which limit, by how much, in what unit — which is precisely the information the audit trail, the survival score and the next investigation need.

The *reasoning* behind the sizing methods and the limit values lives in `../../RISK_PHILOSOPHY.md`. This is the vocabulary; that is the argument. Do not duplicate one into the other.

---

## 1. The size ladder

Four quantities, ascending in abstraction. Confusing any adjacent pair is a real production bug.

```
base_quantity  ──×price──►  notional  ──÷leverage──►  margin  ──÷equity──►  fraction
```

| Term | Symbol | Unit | Meaning |
|---|---|---|---|
| Base quantity | `q` | base asset (BTC) | How much of the thing you hold. Signed: positive long, negative short. |
| Notional | `N = q · P` | quote (USDT) | Economic exposure. Signed. **This is what funding and fees are charged on.** |
| Margin | `M = |N| / L` | quote (USDT) | Collateral posted to hold the position |
| Equity | `E` | quote (USDT) | Account balance plus unrealised P&L, marked on **mark price** |
| Risk fraction | `r` | dimensionless, 0..1 | Fraction of equity at risk if the invalidation level is reached |

`equity` is marked on mark price, not last (`./crypto-perpetuals.md` §3). Every drawdown, every limit and every survival-score input derives from that equity series, so getting the price source wrong corrupts everything downstream silently.

Naming discipline from `../../CLAUDE.md` §4: never write `size`. Write `base_quantity` or `notional_usd`. `size` is ambiguous in a trading system to the point of being dangerous.

---

## 2. Exposure and the leverage ambiguity

| Term | Formula | Unit | What it answers |
|---|---|---|---|
| Gross exposure | `Σ |N_i| / E` | multiple of equity | How much total position is on |
| Net exposure | `Σ N_i / E` | signed multiple | How directional you are |
| Long exposure | `Σ max(N_i, 0) / E` | multiple | |
| Short exposure | `Σ min(N_i, 0) / E` | signed multiple | |

A book that is long 100% and short 100% has gross 200% and net 0%. It is market-neutral on paper and carries every bit of the execution cost, funding cost and correlation risk of a 200% book. Gross and net are both limits here (`../../RISK_PHILOSOPHY.md` §4) because either one alone is gameable.

**"3x leverage" is ambiguous until you say against what.** At least four things are called leverage:

| Usage | Definition | Typical value |
|---|---|---|
| Venue leverage setting | The multiplier you set per symbol; determines initial margin and liquidation distance | 1x–125x |
| Position leverage | `|N| / M` for one position | Equals the setting, roughly |
| Account leverage | `Σ|N| / E` — gross exposure | The one that matters for solvency |
| Effective risk leverage | Portfolio volatility relative to the underlying's | The one that matters for returns |

The venue setting is the least informative of the four and the one people quote. A 20x venue setting on a position worth 2% of equity is a 0.4x account. A 3x setting across ten positions is a 30x account. **Always name which.** In this codebase the field name carries it: `venue_leverage`, `gross_exposure_pct_equity`.

---

## 3. Position sizing, and where it is allowed to happen

Sizing happens in `fking.risk` and nowhere else. A strategy emits a `Signal` with direction, conviction, horizon, invalidation and rationale, and has no import path to order construction (`../../ARCHITECTURE.md` §5). This is structural because an LLM-authored strategy asked to improve returns will size its own positions the moment the type system permits it.

| Method | Formula | What it needs | Failure mode |
|---|---|---|---|
| Fixed fractional | `q = r·E / |P_entry − P_invalidation|` | An invalidation level | Without a stop distance there is no denominator and "risk 0.5%" is not computable |
| Volatility targeting | `q = (σ_target/σ_asset)·E/P` | A volatility estimate | The estimator's error is correlated with the regime change that hurts you |
| Kelly | `f* = μ/σ²`, then a fraction of it | An edge estimate | `μ` is the badly estimated quantity; at SR 1.0 with one year of data its standard error is 100% of itself |
| Exposure cap | portfolio limits | The whole book | Binds most of the time in practice |

**The final quantity is the `min()` of all methods, never the average.** Averaging lets an overconfident method be rescued by a conservative one, so its errors survive into production at reduced amplitude. `min()` gives every method veto power, so a bug anywhere can only make the system smaller. The full argument, including the quantified case for quarter Kelly and the drawdown table behind it, is `../../RISK_PHILOSOPHY.md` §3.

**Fractional Kelly at most, never full.** Full Kelly is provably growth-optimal *conditional on knowing `μ` and `σ`*, which we do not. A 2× overestimate of `f*` is a one-sigma error at realistic sample sizes and takes expected log growth to exactly zero. Under full Kelly with a perfectly known edge, the probability of ever halving your capital is 0.5.

---

## 4. Drawdown, and the number that actually decides survival

| Term | Definition | Unit |
|---|---|---|
| High-water mark | Running max of the equity curve, **persisted** | quote |
| Current drawdown | `(HWM − E_t) / HWM` | fraction |
| Maximum drawdown (MDD) | `max_t` of current drawdown over the window | fraction |
| Drawdown duration | Time from a peak until that peak is exceeded again | days |
| Time under water | Fraction of the sample spent below a prior peak | fraction |
| Underwater curve | Current drawdown as a series | fraction over time |

**Drawdown duration is the number that decides whether a strategy survives contact with a human.** Depth is what people quote and duration is what they experience. A strategy with a 12% max drawdown that recovers in three weeks is comfortable. A strategy with a 12% max drawdown that stays underwater for fourteen months is one that every operator, human or automated, turns off in month nine — and the backtest showing its full-sample Sharpe never mentions that it was abandoned. Report max drawdown, longest drawdown duration, and time under water together, always.

**The high-water mark is persisted in Postgres and restored before trading resumes.** A restarted process that rebuilds its HWM from an empty in-memory equity curve has silently redefined its limit as "20% below wherever we are now", converting a hard limit into a ratchet that never binds. It looks like nothing in the logs. This has killed real systems (`../../RISK_PHILOSOPHY.md` §6).

**De-risking is continuous, not a cliff.** Size scales down across the last 40% of the drawdown budget, so the limit is approached asymptotically rather than hit at full size. A hard cliff means maximum exposure at the moment of maximum evidence that something is wrong.

---

## 5. Ratios, and the annualisation trap

| Ratio | Formula | Penalises | Blind to |
|---|---|---|---|
| Sharpe | `(μ − r_f) / σ` | All volatility | Skew, kurtosis, path, drawdown duration |
| Sortino | `(μ − r_f) / σ_downside` | Downside volatility only | Same tails, still a second-moment measure |
| Calmar | annualised return / max drawdown | Worst path outcome | Everything except one point of the path |

**The annualisation trap.** The textbook scaling is `SR_annual = SR_period · sqrt(periods_per_year)`. On crypto minute bars that factor is `sqrt(365 · 24 · 60) = sqrt(525,600) ≈ 725`. Multiplying by 725 amplifies everything, including the error, and the scaling is only valid if returns are i.i.d.

They are not. Minute-bar crypto returns are autocorrelated (microstructure effects at short lags, momentum and mean-reversion at longer ones), volatility is strongly clustered, and if your strategy holds positions across many bars its returns are overlapping. Positive autocorrelation makes `sqrt(T)` scaling **overstate** the annualised Sharpe, sometimes by a large factor. The corrections — Newey-West standard errors, block bootstrap, episode-level resampling — are in `./statistics-for-trading.md`.

Two habits that prevent most of the damage:

- **Report the sampling frequency and the annualisation factor next to every annualised number.** A bare "Sharpe 2.4" is not a number, it is a claim about an unstated procedure.
- **Report effective sample size in independent episodes, not observations.** 41,208 hourly observations containing 37 independent episodes is a sample of 37. The gap between the two is usually the whole story (`../agents/quant.md`).

**Deflated Sharpe (DSR) and probabilistic Sharpe (PSR).** A search over many strategy configurations selects the best-looking one, and the best of `N` random configurations has a high Sharpe by construction. DSR adjusts the observed Sharpe for the number of trials, the sample length, and the return distribution's skew and kurtosis. PSR gives the probability that the true Sharpe exceeds a benchmark. In this project the trial count `N` is **global, monotone and charged at specification time** — see `../agents/quant.md` and `./statistics-for-trading.md`. **A Sharpe reported without its deflated counterpart and the trial count used is a defect, not an omission.**

---

## 6. Tail measures: VaR, Expected Shortfall, and why VaR is the wrong primary limit

| Measure | Definition | Unit |
|---|---|---|
| VaR(α, h) | The loss not exceeded with probability α over horizon h | quote or fraction |
| Expected Shortfall / CVaR(α, h) | The mean loss **given** that VaR is exceeded | quote or fraction |

VaR(99%, 1d) = 3% says "on 99 days out of 100 you lose less than 3%". It says **nothing whatsoever** about the hundredth day. A position that loses 3.1% on that day and one that loses 80% have identical VaR. In an asset class whose defining risk is a liquidation cascade with a fat left tail (`./crypto-perpetuals.md` §5), a limit that is deliberately blind to the tail is measuring the wrong thing.

VaR is also **not subadditive**: the VaR of a combined book can exceed the sum of its parts, so it can penalise genuine diversification and reward tail-concentrated positions. Expected Shortfall is coherent and, more importantly, is a statement about the region that actually kills you.

This system therefore uses **drawdown and Expected Shortfall as binding constraints** and treats VaR as a reporting number for continuity with external convention. Do not add a VaR limit and consider the tail handled.

---

## 7. Volatility: which estimators we can actually compute

| Estimator | Inputs | Property |
|---|---|---|
| Realised (close-to-close) | Returns over a window | Simple, noisy, equal-weighted so it drops shocks abruptly when they exit the window |
| EWMA | Returns, decay `λ` | `λ = 0.94` is the RiskMetrics daily parameter, ≈ 33-day effective half-life. Responsive, no window cliff. |
| Parkinson | High, low | ~5x more efficient than close-to-close; assumes no drift, and underestimates when there are jumps between bars |
| Garman-Klass | OHLC | More efficient still; assumes continuous trading, which crypto actually satisfies better than equities |
| Implied | Option prices | **We do not have this.** No options data at this budget. |

All four computable estimators need only OHLCV, which we have. Implied volatility we do not have, so any hypothesis needing a forward-looking volatility measure is untestable here and the feature store refuses rather than substituting realised volatility (`../../DATA_PIPELINE.md` §8).

Two rules with reasons:

- **Take the `max()` of a fast and a slow estimate, never a blend.** Underestimating volatility is expensive; overestimating it is merely suboptimal. A fast estimator in a calm regime is exactly the configuration that maximises position size immediately before a volatility expansion — the estimator's error and the market's move are positively correlated in the direction that hurts.
- **Apply a per-symbol floor** at roughly the 10th percentile of its multi-year realised distribution. Crypto majors go quiet for weeks and then do not; without a floor the 3 a.m. quiet period produces a 6x position.

---

## 8. Correlation, netting, and beta

| Term | Definition | Unit |
|---|---|---|
| Correlation `ρ_ij` | Pearson correlation of returns | −1..1 |
| Correlation distance | `d_ij = sqrt(0.5·(1 − ρ_ij))` | A proper metric, unlike `1 − ρ` |
| Portfolio volatility | `σ_p = sqrt(wᵀΣw)` | fraction, annualised |
| Marginal contribution to risk | `MCR_i = (Σw)_i / σ_p` | |
| Component contribution | `CTR_i = w_i · MCR_i`, and `Σ CTR_i = σ_p` | fraction of `σ_p` |
| Beta to BTC | `cov(r_i, r_btc) / var(r_btc)` | dimensionless |

**Per-strategy notional limits do not constrain concentration; they constrain bookkeeping.** Five strategies each within a 5% limit, each long a different large-cap alt, is one 25% position in a drawdown, because pairwise correlations among crypto majors run 0.80–0.95 in stress even when they run 0.4 in calm. Limits are therefore expressed on **risk share** `CTR_i / σ_p`, not on notional, and assets are clustered nightly on correlation distance with the cluster limit binding before the single-asset limit.

**The correlation you must use is not today's.** The engine floors the EWMA estimate at the 95th percentile of its own trailing history: `ρ_used = max(ρ_ewma, ρ_p95)`. Correlations go to 1 exactly when netting matters, so sizing on a calm-market matrix means the diversification you counted on evaporates in the same hour your positions start losing. The cost is being permanently slightly under-diversified in calm markets. That trade is worth making every time (`../../RISK_PHILOSOPHY.md` §5).

**Beta to BTC** is the single most useful one-number summary of a crypto book's directional risk, because almost everything in the asset class is a levered BTC position with a story attached. A "market-neutral" book with net exposure near zero but a BTC beta of 0.6 is not neutral.

**Netting** happens inside the risk engine before an order goes out: two strategies with opposing signals on one symbol produce one net order, not two offsetting ones, or you pay the spread twice for a zero net position. The subtle part is attribution — the crossed portion books at the same VWAP as the venue portion of the same net order, and any residual goes to a `crossing_residual` account rather than to either strategy. Attribution must sum to reality or every survival score downstream measures fiction.

---

## 9. The limit taxonomy, and what a breach means

Every limit here is configurable, and every limit has a **hard ceiling compiled into `fking.risk.limits`** — not read from config, environment, database or file. Configuration can only make the system more conservative. Widening past a ceiling requires a source edit and a PR labelled `safety:critical`, exactly like the host allowlist.

| Limit | Scope | Unit | Binds on |
|---|---|---|---|
| Max position notional | per position | % of equity | Order construction |
| Max exposure to one asset | across strategies | % of equity | Order construction |
| Max gross exposure | portfolio | % of equity | Order construction |
| Max net directional exposure | portfolio | % of equity | Order construction |
| Max correlation-cluster risk share | portfolio | % of `σ_p` | Order construction |
| Min free margin | account | % of equity | Order construction |
| Daily loss limit | portfolio | % of 00:00 UTC equity | Every fill and every mark update |
| Rolling 24h loss limit | portfolio | % of equity | Continuously |
| Strategy max drawdown | per strategy | % from persisted HWM | Continuously |
| Portfolio max drawdown | portfolio | % from persisted HWM | Continuously, trips the kill switch |
| Order rate | per strategy, per account | orders/minute | Order submission |

Two details that are not obvious:

**The daily loss limit is evaluated on every fill and every mark update, not on a schedule.** A limit checked hourly is a limit with an hour of slack in it. It is measured mark-to-market including unrealised.

**The 00:00 UTC reset is an exploitable seam.** Crypto has no session close, so a strategy that loses 3.9% by 23:50 gets its full budget back eleven minutes later — the real limit over a bad night is closer to double the stated one. The rolling 24-hour limit exists because it is the one that actually binds; the fixed-window limit exists for reporting and human intuition.

**A breach is a hard negative in the survival score, not a warning.** The survival score deliberately is not profit: it weighs risk-adjusted return, drawdown discipline, cross-regime consistency, per-trade edge after costs, capacity and out-of-sample decay, and treats risk-limit violations as a hard negative. **A strategy that made money by breaching limits scores worse than one that made less within them.** That has to be encoded in the objective function rather than in documentation, because the system optimizes what it measures (`../../SURVIVAL_PROTOCOL.md`, `../../SCORING_ENGINE.md`, `../../ARCHITECTURE.md` §10).

---

## 10. Kill switch and degraded modes

The kill switch check is **the first statement in `RiskEngine.decide()`**, guarded by the same lock that guards order construction. Not an event subscriber, not a background task, not a check in the execution layer. Sharing the lock with the only code path that can construct an `Order` means there is no window in which the switch is tripped and an order is nonetheless constructed.

| Property | Value |
|---|---|
| Blocking half | A memory read on the critical path. Latency is not a design parameter. |
| Trip decision | Within 100 ms p99 of the triggering event reaching the bus |
| Cancellation of resting orders | Within 2 s p99 |
| Default action on trip | **Cancel, not flatten** |
| Resume | Human command with incident ID, non-empty root cause, and a clean reconciliation in the preceding five minutes |

**Cancel, not flatten**, because flattening is itself a trading decision and the conditions that trip a kill switch are exactly the conditions in which market orders execute worst.

**No automatic resume**, including when the drawdown recovers. A system that unhalts itself has a kill switch in name only.

**Degraded modes** are the states between "running" and "halted": quota exhaustion degrades the agent layer to deterministic-only operation rather than stalling; a stale market data feed blocks new position opening while existing positions are still managed to their invalidation levels; a reconciliation mismatch blocks all order flow for the affected symbol until it is resolved. Operational detail is in `../../FAILSAFE.md` and `../../ERROR_RECOVERY.md`.

---

## 11. Invalidation as a first-class field

```python
invalidation: Decimal | None   # price at which the thesis is wrong
```

It is a field on `Signal`, not a comment in a docstring, for two reasons.

**It is the denominator of fixed-fractional sizing.** `q = r·E / |P_entry − P_invalidation|`. Without it, "risk 0.5% per trade" is not a computable statement — you end up risking 0.5% of equity *in notional*, which is a completely different and much weaker claim. When it is `None` the engine falls back to `k · ATR_14` and additionally halves `r`: a strategy that will not name its stop pays for the estimate.

**It forces every strategy to state in advance what would falsify it.** A strategy that cannot answer "what price proves me wrong" has a hope, not a thesis, and it gets hope-sized. The type permits `None` and the sizing branch makes `None` expensive, which is the right combination — a prohibition would be evaded by emitting an absurd level, and a penalty cannot be.

Note the consequence that surprises people: **a high-conviction signal with a distant stop gets a smaller position than a low-conviction signal with a tight stop.** That is correct. The budget is risk, not exposure.

Related: **conviction is not trusted as reported.** It passes through a per-strategy isotonic calibration fitted on that strategy's own realised outcomes, and returns a constant until the strategy has enough closed trades to have a record. Otherwise you have reinvented strategy-side sizing with extra steps (`../../RISK_PHILOSOPHY.md` §2).

---

## 12. Capacity and turnover

**Capacity** is the maximum notional at which the strategy's net edge stays above zero. It is finite because market impact grows with participation rate, roughly as `sqrt(Q/V)` (`./market-microstructure.md` §5). Every strategy has a capacity curve: edge in bps as a function of deployed notional, decreasing.

**An edge that dies at $50k notional is not an edge for anyone.** It is a backtest artefact of trading in sizes the venue's filters may not even accept, and it is the single most common way a genuinely-positive-looking result turns out to be worthless. Capacity is an explicit term in the survival score for exactly this reason. Estimate it by re-running the backtest at escalating participation rates and finding where net edge crosses zero, and report the curve rather than a single number.

**Turnover** is traded notional over a period divided by average equity — how many times you turn the book over. It is a cost multiplier, directly:

```
annual_cost_drag ≈ turnover_per_year · round_trip_cost_bps
```

A strategy turning over 200x per year at a 9 bp round-trip cost pays 1,800 bp — 18% of equity annually — before it has been right about anything. That number, computed early, kills more candidate strategies than any statistical test, and it is cheap to compute before running a backtest at all. Do it first.

---

## 13. Every term, with unit and location

| Term | Symbol | Unit | Computed in |
|---|---|---|---|
| Base quantity | `q` | base asset, signed | `fking.domain`, sized in `fking.risk` |
| Notional | `N` | quote, signed | `fking.domain` |
| Margin | `M` | quote | `fking.risk`, reconciled in `fking.execution` |
| Equity | `E` | quote, marked on mark price | `fking.risk` |
| Risk fraction per trade | `r` | 0..1 | `fking.risk` |
| Gross exposure | | multiple of equity | `fking.risk` |
| Net exposure | | signed multiple of equity | `fking.risk` |
| Venue leverage | `L` | multiple | `fking.execution` |
| Account leverage | | multiple of equity | `fking.risk` |
| High-water mark | HWM | quote, persisted | `fking.risk`, Postgres |
| Current drawdown | DD | fraction | `fking.risk` |
| Max drawdown | MDD | fraction | `fking.risk`, `fking.evolution` |
| Drawdown duration | | days | `fking.evolution` |
| Time under water | | fraction | `fking.evolution` |
| Sharpe | SR | dimensionless, annualised | `fking.evolution` |
| Deflated Sharpe | DSR | dimensionless | `fking.evolution`, trial count from `fking.agents` ledger |
| Probabilistic Sharpe | PSR | probability | `fking.evolution` |
| Sortino | | dimensionless, annualised | `fking.evolution` |
| Calmar | | dimensionless | `fking.evolution` |
| VaR(α, h) | | fraction | `fking.risk`, reporting only |
| Expected Shortfall | ES | fraction | `fking.risk` |
| Realised volatility | `σ` | annualised fraction | `fking.data` |
| EWMA volatility | `σ_ewma` | annualised fraction | `fking.data` |
| Correlation | `ρ` | −1..1 | `fking.data`, floored in `fking.risk` |
| Portfolio volatility | `σ_p` | annualised fraction | `fking.risk` |
| Component risk contribution | `CTR_i` | fraction of `σ_p` | `fking.risk` |
| Beta to BTC | `β` | dimensionless | `fking.data` |
| Invalidation level | | quote price | emitted by `fking.strategy`, consumed by `fking.risk` |
| Conviction | | 0..1, calibrated | emitted by `fking.strategy`, calibrated in `fking.risk` |
| Capacity | | quote notional | `fking.backtest`, scored in `fking.evolution` |
| Turnover | | multiples per year | `fking.evolution` |
| Kelly fraction | `f*`, `c` | dimensionless | `fking.risk` |

---

## 14. A risk check returns a typed rejection, not a boolean

```python
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
from enum import Enum


class LimitCode(str, Enum):
    POSITION_NOTIONAL = "position_notional"
    SINGLE_ASSET_EXPOSURE = "single_asset_exposure"
    GROSS_EXPOSURE = "gross_exposure"
    NET_EXPOSURE = "net_exposure"
    CLUSTER_RISK_SHARE = "cluster_risk_share"
    MIN_FREE_MARGIN = "min_free_margin"
    DAILY_LOSS = "daily_loss"
    ROLLING_24H_LOSS = "rolling_24h_loss"
    STRATEGY_DRAWDOWN = "strategy_drawdown"
    PORTFOLIO_DRAWDOWN = "portfolio_drawdown"
    ORDER_RATE = "order_rate"
    KILL_SWITCH = "kill_switch"
    VENUE_FILTER_MIN_NOTIONAL = "venue_filter_min_notional"


class Unit(str, Enum):
    QUOTE = "quote_ccy"
    FRACTION_OF_EQUITY = "fraction_of_equity"
    FRACTION_OF_PORTFOLIO_SIGMA = "fraction_of_portfolio_sigma"
    ORDERS_PER_MINUTE = "orders_per_minute"


@dataclass(frozen=True, slots=True)
class Rejection:
    """Why no Order exists. Emitted to the audit trail and scored against the
    strategy. A bare False would discard every field below, which is exactly
    the information the next investigation needs six months from now.
    """

    code: LimitCode
    unit: Unit
    observed: Decimal
    limit: Decimal
    strategy_id: str
    symbol: str
    correlation_id: str          # originated at the Signal
    evaluated_at: datetime       # timezone-aware UTC, injected clock
    detail: str

    @property
    def breach_ratio(self) -> Decimal:
        """How far past the limit. 1.0 is exactly at it."""
        return self.observed / self.limit if self.limit else Decimal("Infinity")


def check_gross_exposure(
    *,
    proposed_notional: Decimal,
    current_gross_notional: Decimal,
    equity: Decimal,
    limit_pct_equity: Decimal,
    strategy_id: str,
    symbol: str,
    correlation_id: str,
    evaluated_at: datetime,
) -> Rejection | None:
    """Return None when the check passes. Pure: no clock, no I/O."""
    if evaluated_at.tzinfo is None:
        raise ValueError("evaluated_at must be timezone-aware UTC")
    projected = (current_gross_notional + abs(proposed_notional)) / equity
    if projected <= limit_pct_equity:
        return None
    return Rejection(
        code=LimitCode.GROSS_EXPOSURE,
        unit=Unit.FRACTION_OF_EQUITY,
        observed=projected,
        limit=limit_pct_equity,
        strategy_id=strategy_id,
        symbol=symbol,
        correlation_id=correlation_id,
        evaluated_at=evaluated_at,
        detail=(
            f"projected gross {projected:.4f}x equity exceeds {limit_pct_equity:.4f}x; "
            f"proposed notional {proposed_notional} on top of {current_gross_notional}"
        ),
    )
```

The engine's public signature follows the same shape:

```python
def decide(
    signal: Signal, portfolio: PortfolioState, market: MarketState, now: datetime
) -> Order | Rejection: ...
```

Four properties this buys, none of which a boolean gives you:

1. **The audit trail can answer "why was there no trade at 03:14 UTC" months later**, which is the governing observability requirement (`../../ARCHITECTURE.md` §11).
2. **The survival score can count breaches by code**, which is how a risk violation becomes a hard negative rather than an invisible non-event.
3. **`breach_ratio` distinguishes a 1.01x miss from a 40x one**, and those mean completely different things about the strategy.
4. **`Order | Rejection` forces the caller to handle both.** `mypy --strict` will not let you ignore the rejection branch, which is a stronger guarantee than any code review.

`now` is a parameter because anything reading the clock inside `risk` or `strategy` is untestable and non-reproducible (`../../CLAUDE.md` §4). Risk math is covered by Hypothesis property tests at a 95% floor, with `fking.risk.limits` held at 100%, because position arithmetic fails on the cases you did not think of: partial closes, direction flips, zero-crossings, dust quantities.

---

## 15. Where this shows up in the codebase

| Concept | Location |
|---|---|
| Sizing, limits, netting, kill switch, `Rejection` | `fking.risk` |
| Compiled hard ceilings and floors, direction-aware clamping | `fking.risk.limits` |
| Survival score, lifecycle verdicts, capacity and turnover scoring | `fking.evolution` |
| Volatility, correlation, beta estimators | `fking.data` |
| `Signal` with `invalidation` and `conviction` | `fking.domain`, emitted by `fking.strategy` |
| Equity, margin and position reconciliation against the venue | `fking.execution` |
| Why risk sits in the order path; every limit's argument and value | `../../RISK_PHILOSOPHY.md` |
| What "performance" means and how breaches are weighted | `../../SURVIVAL_PROTOCOL.md` |
| The objective function in detail | `../../SCORING_ENGINE.md` |
| Kill switch operations, degraded modes, recovery | `../../FAILSAFE.md`, `../../ERROR_RECOVERY.md` |

Related contexts: `./market-microstructure.md` (cost, capacity, impact), `./crypto-perpetuals.md` (mark price, funding, cascades), `./statistics-for-trading.md` (deflation, autocorrelation, effective sample size), `./backtest-pitfalls.md`, `../../docs/rules/no-lookahead.md`, `../../docs/rules/decimal-and-money.md`, `../knowledge/glossary.md`, `../../ARCHITECTURE.md` §6.

---

## 16. If you remember nothing else

1. **Every number carries a unit and a denominator.** "3x", "15%", "size" are not numbers until you say against what.
2. **Sizing lives in `fking.risk` and nowhere else.** Strategies emit `Signal`. The type system enforces it because the next strategy author will be an LLM.
3. **Take `min()` across sizing methods and `max()` across volatility estimators.** Both directions are chosen so that a bug can only make the system smaller.
4. **Floor correlations at their stress values.** They go to 1 exactly when netting matters.
5. **Drawdown duration, not depth, is what ends strategies.** Report both, always.
6. **Never report a Sharpe without its deflated twin and the global trial count.**
7. **VaR is not a tail limit.** Drawdown and Expected Shortfall bind; VaR reports.
8. **A limit breach is a hard negative in the survival score.** Money made by breaching a limit scores worse than less money made within it.
9. **The kill switch cancels, does not flatten, and never resumes itself.**
10. **A risk check returns a typed `Rejection`, never `False`.** The fields are the point.
11. **Compute turnover times round-trip cost before you run the backtest.** It kills more candidates than any statistical test, for a thousandth of the effort.
