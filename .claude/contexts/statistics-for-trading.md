# Context — Statistics for Trading

## What you need to hold in your head

Financial returns violate every assumption the standard statistical toolkit is built on, and they violate them in the direction that flatters you: fat tails inflate apparent significance, volatility clustering makes independent-looking observations dependent, overlapping windows multiply one observation into a hundred, and a search over strategy space guarantees that the best result you find is the one whose noise happened to point up. None of this produces an error message. It produces a number that looks good. Your job is to know the size of the correction before you compute the statistic, to count effective sample size in **independent episodes rather than observations**, and to charge every trial against the deflation that makes the whole project's results honest. The single most useful habit in this document: whenever you see a sample size, immediately ask how many of those rows are actually independent draws — the gap between 41,208 hourly observations and 37 independent episodes is usually the entire story, and everything else you are about to compute is a rounding error next to it.

---

## 1. Returns are not IID normal, and each violation breaks something specific

| Property | What actually holds in crypto | What it breaks |
|---|---|---|
| **Fat tails** | Daily BTC log-return excess kurtosis is routinely in the 5–15 range against 0 for a Gaussian; single-day moves beyond 6σ under a fitted normal occur several times per year | Every t-statistic, every Gaussian confidence interval, every VaR computed from a standard deviation. The tail mass sits where the model says nothing happens, so a strategy short of tail risk shows a beautiful Sharpe until the day it does not |
| **Volatility clustering** | Absolute returns are strongly autocorrelated out to weeks; squared-return autocorrelation decays as a power law, not exponentially | The IID assumption behind the Sharpe standard error `sqrt((1 + SR²/2)/T)`. Drawdowns cluster, so the worst drawdown in a sample is far worse than an IID model predicts, and equity-curve smoothness in a calm sub-period tells you nothing about the next one |
| **Return autocorrelation** | Raw returns are near-uncorrelated at daily frequency but meaningfully autocorrelated at minute frequency (microstructure) and again at multi-week horizons (momentum/reversal) | The annualisation factor `sqrt(periods_per_year)`, which is only valid at zero autocorrelation. Positive autocorrelation makes it understate volatility and overstate Sharpe |
| **Non-stationarity** | Realised volatility has fallen structurally as the asset class matured; the funding-rate regime flips sign for months at a time | Any parameter fitted on the full sample. The "optimal" lookback is an average over regimes that never recurs |
| **Cross-sectional dependence** | Crypto majors carry a dominant common factor; correlations rise toward 1 in liquidation events | Any claim that testing 8 symbols gives 8 independent tests. It gives roughly one and a half |

The operational rule: **you may use a Gaussian statistic as a rough screen, never as evidence.** Anything that reaches a decision goes through a bootstrap that resamples the actual dependence structure, or through a test whose null distribution you generated yourself.

---

## 2. Overlapping windows: why your t-statistic is wrong

The most common way to manufacture significance in this domain, and it is almost always accidental.

You label each hourly bar with the forward 48-hour return. Consecutive labels share 47 of their 48 hours. You now have 41,208 rows and something close to 41,208/48 ≈ 858 non-overlapping label windows — and because the underlying returns are themselves clustered, far fewer independent *episodes*. A t-statistic computed on 41,208 rows divides by `sqrt(41208)`. It should be dividing by something closer to `sqrt(37)`. That is a factor of 33 on the standard error, which converts a t of 0.9 into a t of 30.

```python
# WRONG — treats overlapping labels as independent draws
t_stat = returns.mean() / (returns.std(ddof=1) / math.sqrt(len(returns)))
```

Three fixes, applied by dependence structure:

**Block bootstrap** — for overlapping windows and volatility clustering. Resample contiguous blocks long enough to contain the dependence, not individual observations. Block length must exceed the label horizon; the working default here is `max(2 * label_horizon_bars, 7 days in bars)`.

**Episode-level resampling** — for clustered events. An "episode" is one contiguous occurrence of the setup: a funding-extremity regime, a holding period, a signal cluster. Resample whole episodes with replacement. This is the correct unit for anything triggered by a condition that persists.

**Newey–West / HAC standard errors** — acceptable as a screen when the dependence is short and you need a number fast. Not acceptable as the reported significance for a decision, because the lag-truncation choice is a free parameter and free parameters get chosen to produce the desired answer.

```python
from __future__ import annotations

import math

import numpy as np
import numpy.typing as npt


def circular_block_bootstrap(
    returns: npt.NDArray[np.float64],
    block_length_bars: int,
    n_resamples: int,
    seed: int,
) -> npt.NDArray[np.float64]:
    """Resample a return series in contiguous circular blocks.

    Circular (wrap-around) rather than plain moving-block: every observation has
    equal probability of selection, so the resampled mean is unbiased for the
    sample mean. Plain moving-block under-weights the first and last
    `block_length_bars - 1` observations, which in a trending crypto series
    systematically discards the beginning of the sample.

    The seed is a required argument, not a default. CLAUDE.md section 5: every
    test is deterministic, and a bootstrap whose seed lives in module scope
    silently changes its answer when the import order changes.
    """
    if block_length_bars <= 0 or block_length_bars > returns.shape[0]:
        raise ValueError("block_length_bars must be in 1..len(returns)")

    rng = np.random.default_rng(seed)
    n_observations = returns.shape[0]
    n_blocks = -(-n_observations // block_length_bars)  # ceiling division

    starts = rng.integers(0, n_observations, size=(n_resamples, n_blocks))
    offsets = np.arange(block_length_bars)
    indices = (starts[:, :, None] + offsets[None, None, :]) % n_observations
    flat = indices.reshape(n_resamples, n_blocks * block_length_bars)
    return returns[flat[:, :n_observations]]


def bootstrap_sharpe_interval(
    returns: npt.NDArray[np.float64],
    block_length_bars: int,
    periods_per_year: int,
    seed: int,
    n_resamples: int = 10_000,
) -> tuple[float, float]:
    """Two-sided 95% interval for the annualised Sharpe under block resampling."""
    samples = circular_block_bootstrap(returns, block_length_bars, n_resamples, seed)
    means = samples.mean(axis=1)
    stds = samples.std(axis=1, ddof=1)
    sharpes = np.where(stds > 0.0, means / stds, 0.0) * math.sqrt(periods_per_year)
    low, high = np.percentile(sharpes, [2.5, 97.5])
    return float(low), float(high)
```

If the block-bootstrap interval contains zero and the naive t-test says `p < 0.001`, the block bootstrap is right and the t-test measured the overlap.

---

## 3. Effective sample size: episodes, not observations

Report both numbers, always, side by side. `HypothesisResult` carries `n_observations` and `n_independent_episodes` as separate fields precisely so that no one can quote the flattering one alone.

For an AR(1)-like dependence with lag-1 autocorrelation `rho`, the effective sample size is

```
n_eff ≈ n * (1 - rho) / (1 + rho)
```

At `rho = 0.9` that is `n/19`. At `rho = 0.98` — entirely normal for a slow-moving regime indicator sampled hourly — it is `n/99`.

For episode-based strategies, do not estimate `rho` at all. **Count the episodes.** One funding-extremity regime lasting 30 hours is one draw, not 30. One trend-following holding period is one draw regardless of how many bars it spans. The count that matters is how many times the world independently presented the setup.

Practical floors used in this project's decision rules:

| Test | Minimum independent episodes | Why |
|---|---|---|
| Any `supported` verdict | 30 | Below this the Sharpe standard error exceeds the effect you are claiming |
| Regime-conditional claim | 30 per regime | A claim about bear markets needs bear-market episodes, not sample-wide episodes |
| Tail/liquidation-event claim | Usually unreachable | There are fewer than ten genuinely independent crypto liquidation cascades in the entire history. State it as untestable rather than proxying |

A strategy that trades a rare setup may simply be **untestable with the history that exists**. That is a legitimate verdict and it is cheaper to reach it before the work than after.

---

## 4. Sharpe ratio mechanics

The Sharpe ratio is a t-statistic wearing a hat: `SR = mean / sd` over the sampling period, annualised by `sqrt(periods_per_year)`.

**The crypto annualisation factor is not 252.** Crypto trades continuously, so:

| Bar interval | `periods_per_year` | `sqrt(periods_per_year)` |
|---|---|---|
| 1 minute | 525,600 | 725.0 |
| 1 hour | 8,760 | 93.6 |
| 4 hours | 2,190 | 46.8 |
| 1 day | 365 | 19.1 |
| 1 day, equities convention (wrong here) | 252 | 15.9 |

Using 252 on daily crypto returns understates the annualised Sharpe by a factor of `sqrt(365/252) = 1.20`. Using 365 on a strategy that only trades weekdays overstates it by the same factor. State the convention next to the number or the number is not interpretable.

**The autocorrelation correction (Lo, 2002).** Annualising by `sqrt(q)` assumes zero autocorrelation. When it is not zero:

```
SR_q = SR_1 * q / sqrt(q + 2 * sum_{k=1}^{q-1} (q - k) * rho_k)
```

With positive autocorrelation the denominator grows and the naive `sqrt(q)` scaling overstates. Slow-moving strategies with `rho_1 ≈ 0.3` at daily frequency see annualised Sharpe drop roughly 20–25% under the correction. Apply it whenever the strategy holds across multiple bars, which is nearly always here.

**The Sharpe standard error** under IID normality is `sqrt((1 + SR²/2) / T)`. Use it only to see how hopeless the sample is, never to claim significance. At `T = 37` independent episodes and `SR = 1.0`, the standard error is `sqrt(1.5/37) = 0.20` — so a point estimate of 1.0 carries a 95% interval of roughly [0.6, 1.4] *before* accounting for fat tails, and the fat tails widen it further.

---

## 5. PSR, DSR, and the multiple-testing intuition

**The intuition first, because the formulas follow from it.** Draw `N` samples from a distribution with mean zero and take the maximum. The expected maximum grows with `N` — roughly like `sd * sqrt(2 * ln N)` for Gaussian draws. At `N = 2,000` that is about `3.9 * sd`. So if your backtest Sharpes have a cross-trial standard deviation of 0.15 and *none of the strategies has any edge whatsoever*, the best of 2,000 backtests will show an annualised Sharpe near 0.58 purely by construction. Reporting that 0.58 as a finding is not a subtle error. It is reporting the null.

**Probabilistic Sharpe Ratio (PSR)** answers: what is the probability the true Sharpe exceeds a benchmark `SR*`, given the observed Sharpe, the sample length, and the higher moments? It corrects for skew and kurtosis, which the plain t-statistic ignores. Negative skew and high kurtosis both *reduce* PSR for the same observed Sharpe — correctly, because a Sharpe earned by selling tails is less trustworthy than one earned symmetrically.

**Deflated Sharpe Ratio (DSR)** is PSR with `SR*` set to the expected maximum Sharpe under the null across `N` trials. It converts "this looked best out of 2,000" into "this is better than the best of 2,000 would look by chance, with probability p".

```python
from __future__ import annotations

import math
from typing import Final

from scipy.stats import norm

EULER_MASCHERONI: Final = 0.5772156649015329


def probabilistic_sharpe_ratio(
    sharpe_observed: float,
    sharpe_benchmark: float,
    n_observations: int,
    skew: float,
    kurtosis: float,
) -> float:
    """P(true Sharpe > benchmark), corrected for non-normality.

    All Sharpe arguments are PER-OBSERVATION, not annualised. Mixing an
    annualised observed Sharpe with a per-observation benchmark is the most
    common way this function is misused, and it produces a plausible-looking
    number rather than an error.

    `kurtosis` is the raw fourth standardised moment: 3.0 for a Gaussian, not 0.0.
    Both numpy and scipy default to EXCESS kurtosis; add 3.0 at the call site.
    """
    if n_observations < 2:
        raise ValueError("n_observations must be >= 2")

    variance_term = (
        1.0
        - skew * sharpe_observed
        + 0.25 * (kurtosis - 1.0) * sharpe_observed**2
    )
    if variance_term <= 0.0:
        # Occurs at extreme skew with a large observed Sharpe: the asymptotic
        # expansion has left its domain. Fail loudly rather than return a number.
        raise ValueError("PSR variance term non-positive; higher moments out of range")

    numerator = (sharpe_observed - sharpe_benchmark) * math.sqrt(n_observations - 1)
    return float(norm.cdf(numerator / math.sqrt(variance_term)))


def expected_max_sharpe_under_null(
    n_trials: int,
    sharpe_variance_across_trials: float,
) -> float:
    """Expected maximum Sharpe from `n_trials` draws of a zero-mean null.

    This is the threshold a result must beat to be more than a selection artefact.
    It grows without bound in `n_trials`, which is the entire point: a project
    that has tried 20,000 things needs a far better result to prove the same claim
    as one that has tried 20.
    """
    if n_trials < 2:
        raise ValueError("deflation is undefined below 2 trials")

    sharpe_sd = math.sqrt(sharpe_variance_across_trials)
    return sharpe_sd * (
        (1.0 - EULER_MASCHERONI) * norm.ppf(1.0 - 1.0 / n_trials)
        + EULER_MASCHERONI * norm.ppf(1.0 - 1.0 / (n_trials * math.e))
    )


def deflated_sharpe_probability(
    sharpe_observed: float,
    n_trials: int,
    n_observations: int,
    skew: float,
    kurtosis: float,
    sharpe_variance_across_trials: float,
) -> float:
    """Bailey & Lopez de Prado DSR: a PROBABILITY in [0, 1], not a Sharpe."""
    threshold = expected_max_sharpe_under_null(n_trials, sharpe_variance_across_trials)
    return probabilistic_sharpe_ratio(
        sharpe_observed, threshold, n_observations, skew, kurtosis
    )


def haircut_sharpe(
    sharpe_observed: float,
    n_trials: int,
    sharpe_variance_across_trials: float,
) -> float:
    """Observed Sharpe minus the selection threshold, on the Sharpe scale.

    This is what `HypothesisResult.sharpe_deflated` carries, so that it is
    directly comparable to a decision rule written as `deflated Sharpe >= 0.50`.
    Report it alongside `deflated_sharpe_probability`; they answer different
    questions and neither substitutes for the other.
    """
    return sharpe_observed - expected_max_sharpe_under_null(
        n_trials, sharpe_variance_across_trials
    )
```

**Worked arithmetic, illustrative, matching the `quant` agent's example.** At `N = 1,847` trials, `norm.ppf(1 - 1/1847) = 3.27` and `norm.ppf(1 - 1/(1847·e)) = 3.54`, so the bracket evaluates to `0.4228 * 3.27 + 0.5772 * 3.54 = 3.43`. With a cross-trial Sharpe standard deviation of 0.158 (annualised), the selection threshold is `SR* = 0.54`. An observed annualised Sharpe of 1.12 haircuts to **0.58** — which clears a 0.50 decision rule, and which would have been reported as "Sharpe 1.12" by anyone not doing this arithmetic.

Two things the formula makes obvious and people still get wrong:

- **`sharpe_variance_across_trials` is the variance of Sharpes across the trials you actually ran**, not the sampling variance of one Sharpe. If you did not record per-trial Sharpes, you cannot compute this, which is a reason to record them rather than a reason to substitute something.
- **Annualisation must be consistent.** `expected_max_sharpe_under_null` returns a threshold in whatever units `sharpe_variance_across_trials` was measured in. `probabilistic_sharpe_ratio` needs per-observation units because of the `sqrt(n_observations - 1)` term. Convert once, at a named boundary, and say which units you are in.

---

## 6. The global trial counter

The deflation is only as honest as `N`, and `N` is the most gameable number in the system. The full rule lives in [`../rules/overfitting-defences.md`](../rules/overfitting-defences.md); the statistical reason it takes that exact shape is here.

**Charged at specification time, not execution time.** You declare a 200-point grid and abandon it after 12 points because the first 12 looked bad. If you charge 12, you have understated `N` by 188 — but the selection event already happened at specification, because had one of those first 12 looked good you would have stopped there and reported it. The correct `N` is the size of the search you were willing to conduct.

**Global, not per-hypothesis.** `SR*` depends on how many results the *project* selected from, because the result being reported is the best-looking one to emerge from the project's entire history. Every test anyone runs raises the bar for every future result. This is the correct incentive and it is why the counter appears in every report.

**Monotone forever.** No expiry, no reset on refactor, no "those trials were on different data". A resettable counter is a decorative counter.

The corollary that changes how you design a study: **a large parameter grid is expensive to everyone, permanently.** A hypothesis with one or two parameters fixed a priori from a stated mechanism is cheap and provable. If you cannot fix a parameter from the mechanism, you do not have a mechanism — you have a search, and searches are priced accordingly.

---

## 7. Combinatorial purged cross-validation

Standard k-fold cross-validation leaks in time series in two directions, and both directions inflate every fold Sharpe.

**Purge.** A training observation whose *label* extends into the test period has seen the test period. With a 48-hour label horizon, every training row in the 48 hours before the test window must be dropped. Purge length = label horizon, exactly. Not "a few bars".

**Embargo.** Serial correlation means a training observation immediately *after* the test period is still informationally adjacent to it — features are computed on trailing windows that overlap the test data. Embargo the bars after each test window. The working default is `max(label_horizon, 1% of sample length)`.

**Combinatorial.** Rather than one path through the data, split into `G` groups and take all `C(G, k)` combinations of `k` test groups. With `G = 8, k = 2` you get 28 splits and 28 fold Sharpes — a *distribution* instead of a point estimate. What you read from it is not the mean but the **sign consistency**: the fraction of folds with positive Sharpe. A strategy at 22/28 positive is different in kind from one at 15/28 with the same mean, and the second one is one regime wearing a costume.

What leaks without each defence:

| Missing | Leak | Symptom |
|---|---|---|
| Purge | Training labels overlap test period | Fold Sharpes uniformly high, low dispersion, and they degrade sharply when purge is added |
| Embargo | Trailing-window features straddle the boundary | Folds adjacent to the test window outperform distant folds |
| Combinatorial | One path, one number | No dispersion estimate at all, so you cannot distinguish an edge from a regime |
| Any of them | — | Validation Sharpe far above live Sharpe. This is the failure `ARCHITECTURE.md` §13 names as the assumption most likely to be wrong in the entire system |

**Walk-forward** answers a different question and is not a substitute. CPCV asks "is there an effect in this data". Walk-forward asks "would a decision procedure that only ever saw the past have made money" — it retrains on an expanding or rolling window and trades the next window, mimicking deployment including the refit cadence. Run both. CPCV establishes the effect; walk-forward establishes that a causal procedure can capture it.

---

## 8. Statistical significance is not economic significance

A `p < 0.001` result on a 0.3bp edge is a precisely measured nothing. The relevant comparison is not to zero, it is to **cost**.

```
net_edge_bps = gross_edge_bps - fees_bps - half_spread_bps - impact_bps - funding_bps
```

Round-trip taker cost on liquid USDT perpetuals runs on the order of 9bp under this project's production-calibrated cost model. So:

- An edge below cost is `not_supported`, however significant.
- An edge at 1.2× cost is a cost-model artefact — the cost model's own uncertainty exceeds the margin.
- The working bar is **gross edge above roughly 2× modelled cost**, and the reported number is always net.

There is a genuinely useful third verdict between "works" and "does not work": *the effect is real and we cannot currently afford it.* That finding points at execution capability as the binding constraint rather than at more hypothesis generation, and it is only visible if you report gross and cost separately. See the `quant` agent's worked example, where a real 14.2bp carry effect fails a 6bp net floor at 9bp cost.

Never resolve this by switching to a maker-fill assumption. Passive fill probability is unmeasurable without L2 depth, which [`../../ARCHITECTURE.md`](../../ARCHITECTURE.md) §6 establishes we do not have. An unfalsifiable input chosen because it produces the desired answer is not an assumption, it is a decision.

---

## 9. Power and minimum detectable effect — compute this before the test

**A null result only means something if the test could have detected the effect.** Most negative findings in this domain are uninformative because the sample was never large enough, and you cannot discover that after the fact without HARKing.

The minimum detectable effect at 80% power and a 5% one-sided level is

```
MDE = (z_0.95 + z_0.80) * standard_error = 2.486 * sigma_episode / sqrt(n_episodes)
```

**Illustrative calculation.** Suppose episode-level returns have a standard deviation of 80bp and you have 37 independent episodes. Then `MDE = 2.486 * 80 / sqrt(37) = 32.7bp`. A hypothesis claiming a 6bp effect **cannot be tested on this sample** — there is no outcome of the experiment that would distinguish 6bp from zero. Registering it anyway charges trials, consumes a validation cycle, and produces an "inconclusive" that reads like a negative result to the next person.

Invert it to get the required sample before you commit:

```python
def episodes_required(
    effect_bps: float,
    episode_return_sd_bps: float,
    power: float = 0.80,
    alpha: float = 0.05,
) -> int:
    """Independent episodes needed to detect `effect_bps` one-sided."""
    z_alpha = norm.ppf(1.0 - alpha)
    z_power = norm.ppf(power)
    return math.ceil(((z_alpha + z_power) * episode_return_sd_bps / effect_bps) ** 2)
```

At `effect_bps = 6`, `episode_return_sd_bps = 80`: **1,073 episodes**. If the setup fires 40 times a year, that is 27 years of history in an asset class that has fewer than ten years of liquid perpetual data. Say "untestable with available data" and move on. That is a real finding and it saves the next investigator the same month.

---

## 10. Multiple hypothesis correction: Bonferroni vs deflation

| Method | Adjustment | Fits this project? |
|---|---|---|
| **Bonferroni** | `alpha / N` | No. Assumes independent tests. A parameter sweep produces highly correlated tests, so Bonferroni is severely conservative — and simultaneously it says nothing about *effect size*, only about a p-value threshold |
| **Benjamini–Hochberg (FDR)** | Rank p-values, control expected false-discovery proportion | Better for a genuinely multi-hypothesis screen, but it needs the full set of p-values from a single coherent family. Our trials accumulate across years and studies, so there is no "family" to rank |
| **Deflated Sharpe** | Raises the required *effect size* by the expected max under the null | Yes. It works on the quantity we actually care about (Sharpe), it handles correlated trials through the cross-trial variance term, and it composes across time — you can add trials to `N` years later and the correction remains coherent |

Deflation is the project's mechanism. Bonferroni is a sanity cross-check when you have a small number of genuinely independent tests, and it should be reported as such rather than as the headline.

---

## 11. Regime conditioning and aggregation errors

Break every result down by regime before believing it. Crypto's regimes are short and violent enough that a sample-wide statistic is frequently a weighted average of contradictory sub-statistics.

The Simpson's-paradox form specific to this domain: a strategy shows a positive pooled edge while losing money in *every* regime individually. The mechanism is that the strategy trades more in the regime with the higher unconditional drift. The pooled mean picks up the drift, not the edge.

```python
# WRONG — pooled mean over unequal regime exposure
edge_bps = trades["pnl_bps"].mean()

# RIGHT — condition first, then aggregate with declared weights
by_regime = trades.groupby("regime")["pnl_bps"].agg(["mean", "count"])
# and report the per-regime table, not only its aggregate
```

The corollary that decides verdicts: **an edge concentrated in one regime is a story about that regime**, and it should be evaluated as a bet on that regime recurring. Sometimes that is a legitimate position. It is never the same claim as "this strategy works".

Regimes to cut on, at minimum: realised-volatility tercile, trend versus chop, funding-rate sign, and calendar era (see [`./backtest-pitfalls.md`](./backtest-pitfalls.md) §6 for the specific eras).

---

## 12. Stationarity, cointegration, and spurious regression

Two independent random walks regressed on each other in **levels** produce R² near 1 and t-statistics in the tens. This is Granger–Newbold spurious regression, and it is not a subtle effect — it is the default outcome. Crypto is full of trending, near-unit-root series, so the setup arises constantly: regress ETH price on BTC price over 2020–2021 and you will get a beautiful relationship that means nothing.

Rules:

- **Regress returns (log differences), not levels**, unless you have specifically established cointegration.
- **Establish cointegration properly** — Engle–Granger on the residual, or Johansen for more than two series — and remember that ADF-family tests have low power in short samples, so failing to reject a unit root is not evidence of one.
- **Cointegration in crypto is unstable.** Pairs that cointegrate for a year decouple when one asset's supply schedule, staking mechanics, or listing venue changes. Test for cointegration *within* each walk-forward window, not once over the whole sample, and treat a relationship that only cointegrates in-sample as a fit rather than a structure.
- **Differencing is not free.** It destroys the level information that a mean-reversion strategy trades. If your edge is in the level, you need cointegration; if you cannot establish it, you do not have the edge.

---

## 13. HARKing, and why a modified hypothesis is a new hypothesis

Hypothesising After the Results are Known is the mechanism by which careful, honest people generate fake findings. The sequence is always the same: you register a 48-hour horizon, the result is flat, you notice 72 hours looks better, and you write it up as though 72 was the plan. Every step feels reasonable and the output is fiction.

The defence is mechanical, not attitudinal: **the registration is committed with a spec hash before data access, and `spec_hash_matches: False` voids the result automatically.** A modified hypothesis is a new registration with a new trial charge. Not a correction, not an amendment — a new charge, because the modification was informed by the data and that is exactly what the counter exists to price.

The related failure is threshold drift. If the pre-registered floor was 6bp and you measured 5.2bp, **it failed.** Record the margin. Do not run one more configuration to check; that is an additional trial and it is also precisely the behaviour deflation exists to punish.

---

## 14. Bayesian priors as a discipline

You do not need a full Bayesian pipeline to get the main benefit, which is this: **an implausible effect size demands stronger evidence than a plausible one.**

Take a prior over the true annualised Sharpe of a strategy that is (a) implementable at retail scale, (b) on liquid crypto perpetuals, (c) net of taker costs. Centre it at 0 with a standard deviation of about 0.5 — generous, if anything, given how many well-capitalised participants are looking at the same public data. Now observe a backtest Sharpe of 3.0 with a standard error of 0.6. The normal-normal posterior mean is

```
posterior_mean = (prior_mean / prior_var + observed / obs_var) / (1 / prior_var + 1 / obs_var)
               = (0 / 0.25 + 3.0 / 0.36) / (4.0 + 2.78)
               = 8.33 / 6.78
               = 1.23
```

The observed 3.0 shrinks to 1.23 before you have considered trial deflation at all. Run the deflation on top and it shrinks further. This arithmetic is illustrative, but the shape of the conclusion is robust: **a spectacular backtest is much more likely to be a spectacular error than a spectacular edge**, and the prior encodes that rather than leaving it to be argued about.

The practical use is as a triage rule. A result implying a Sharpe above roughly 2 on a horizon of hours-to-days, net of costs, on a public data source, is a bug report until proven otherwise. Go looking for the look-ahead before you go looking for the mechanism.

---

## 15. Where this shows up in the codebase

| Concept | Location |
|---|---|
| PSR, DSR, haircut Sharpe, expected max under null | `fking.backtest.validation` |
| Block bootstrap, episode-level resampling, seeded RNG | `fking.backtest.validation` |
| CPCV splitter, purge and embargo sizing | `fking.backtest.validation` |
| Walk-forward harness, refit cadence | `fking.backtest` engine; the `walk-forward` agent |
| Global trial counter, append-only ledger | `fking.evolution.scoring`, DB-enforced append-only table |
| Survival score, cross-regime consistency term, out-of-sample decay | `fking.evolution.scoring`, specified in `SCORING_ENGINE.md` |
| Effective sample size, episode counting | `fking.backtest.validation`, reported by the `quant` agent in `HypothesisResult` |
| Cost model parameters used in the net-edge calculation | `fking.backtest` cost model, calibrated per [`./binance-testnet.md`](./binance-testnet.md) §6 from production data only |
| Hypothesis registration, spec hash, trial charge | `.claude/agents/quant.md`, artefacts under `artifacts/agents/quant/` |
| The `float` exception that lets NumPy do this work at all | [`../rules/decimal-and-money.md`](../rules/decimal-and-money.md), "The one exception" |

The statistical functions live in `fking.backtest.validation` and return `float`, because they are estimates with sampling error many orders of magnitude larger than `2^-53`. They convert to `Decimal(str(result))` at the module boundary before anything in `evolution` or the database sees them.

Related: [`./backtest-pitfalls.md`](./backtest-pitfalls.md) for how these errors manifest operationally, [`../rules/no-lookahead.md`](../rules/no-lookahead.md) for the leak that makes all of this moot if violated, [`../rules/overfitting-defences.md`](../rules/overfitting-defences.md) for the enforcement mechanism, and [`../knowledge/verified-facts.md`](../knowledge/verified-facts.md) for the measured figures quoted above.

---

## 16. Traps

1. **Reporting a bare Sharpe.** Every Sharpe leaves with its deflated counterpart and the trial count. A bare Sharpe is a defect, not a style choice.
2. **`scipy.stats.kurtosis` returns excess kurtosis.** The PSR formula wants the raw fourth standardised moment. Add 3.0. Forgetting produces a number that is wrong in the optimistic direction.
3. **Mixing annualised and per-observation Sharpes** inside PSR. The `sqrt(n_observations - 1)` term only makes sense per-observation. This produces a plausible number, never an error.
4. **Annualising crypto with 252.** It is 365, or 8,760 hourly. Understates by 20%.
5. **Computing `sharpe_variance_across_trials` from the sampling variance of one Sharpe.** It is the dispersion *across* the trials you ran. If you did not record them, record them next time; do not substitute.
6. **Dropping a failed CPCV fold.** Degenerate folds get reported as failed. Silently dropping them biases the fold-Sharpe distribution upward, which is the one direction that matters.
7. **Purging by "a few bars" instead of by the label horizon.** The purge length is not a tuning parameter; it is determined by the label.
8. **Bootstrapping without a fixed seed**, or with the seed defaulted in module scope. Every test is deterministic (`CLAUDE.md` §5).
9. **Treating 8 symbols as 8 independent tests.** Crypto majors share a dominant factor; correlations rise toward 1 exactly when it matters.
10. **Running the test before computing the minimum detectable effect.** A null result from an underpowered test is not a null result.

## If you remember nothing else

**Count independent episodes, not observations. Charge every trial at specification time. Report the deflated Sharpe or report nothing. Compute the minimum detectable effect before you run the test. And treat a spectacular result as a bug report until you have failed to find the bug.**
