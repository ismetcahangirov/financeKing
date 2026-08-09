# Context — Backtest Pitfalls

## What you need to hold in your head

Every one of the seven failures below shares a property that makes it uniquely dangerous: **none of them throws an exception.** They produce a backtest that runs cleanly, reports plausible numbers, and is wrong in the optimistic direction. There is no test suite that catches a good-looking lie, so the defence has to be structural — the backtest and live paths must be one code path, features must be point-in-time by construction, trials must be counted whether or not you liked the result, and costs must come from production data. Crypto sharpens all seven: there is no session boundary to make a timestamp error visible, the tradable universe has been culled by exchange collapses rather than by orderly delistings, liquid perpetual history is short enough to be exhausted by a single parameter sweep, and the regimes are violent and brief enough that a five-year sample contains perhaps four independent macro states. Assume every promising result is one of these seven until you have ruled each one out by name.

---

## 1. Look-ahead bias

**The defect class that does not fail — it makes bad strategies look excellent.** `CLAUDE.md` §2 names it as the most dangerous in the project.

### How it manifests in crypto specifically

Equities have a session boundary. A look-ahead bug there often surfaces as a fill at a price that did not trade during the session, or a signal timestamped 16:05 on a market that closed at 16:00 — the calendar itself is a tripwire. **Crypto has no such landmark.** The tape runs continuously, every timestamp is valid, and a one-bar shift produces a perfectly well-formed equity curve. The only signal that something is wrong is that the result is too good, which is exactly the signal you are least motivated to act on.

Four specific vectors:

**The resample label trap.** `pandas.resample("1h")` labels bins by their **left** edge by default. The row timestamped `10:00` contains everything through `10:59:59` and its close is not knowable until `11:00`. Consume it as a feature at `10:00` and you have a clean one-hour look-ahead with no visible artefact.

**The bar-close trap.** A signal computed from a bar's close is actionable at the *next* bar's open, not at that bar's close. Multiplying `signal[t] * return[t]` where `return[t]` is the return *into* `t` uses the bar's own move to decide the trade that captured it.

**The daily-close-for-intraday trap.** Joining a daily bar onto an intraday index keyed on the calendar date makes the day's close available at 00:00 of that day. This is the most common form in cross-frequency features and it is worth 24 hours of foresight.

**Indicators computed on the full series, then sliced.** Any full-sample statistic — a z-score mean and standard deviation, a `StandardScaler.fit`, a quantile threshold, a volatility normalisation — leaks the whole sample into every point. Slicing afterwards does not undo it.

**Funding rates known only at settlement.** The realised funding rate for an 8-hour interval is final at settlement. Anything published before that is a prediction that moves. Joining the settled rate back onto bars inside its own interval hands the strategy the outcome of the interval it is trading. See [`./crypto-perpetuals.md`](./crypto-perpetuals.md) for the settlement mechanics.

### The symptom you would actually observe

Sharpe above 3 on an hours-to-days horizon. Equity curve with almost no drawdown. Win rate above 65%. Performance that *degrades monotonically* as you add bars of delay between signal and execution — which is the diagnostic: a real edge decays gracefully with latency; a leak collapses to zero at exactly one bar.

### Wrong

```python
df["sma_50"] = df["close"].rolling(50).mean()
df["zscore"] = (df["close"] - df["close"].mean()) / df["close"].std()  # full-sample
df["signal"] = (df["close"] > df["sma_50"]).astype(int)
df["ret"] = df["close"].pct_change()

pnl = df["signal"] * df["ret"]   # signal[t] earns the return INTO t
```

Three leaks in five lines: the full-sample z-score, the signal consumed on its own bar, and the implicit assumption that `close[t]` is knowable at `t`.

### Right

```python
import pandas as pd

# Bars are labelled by their CLOSE time and are available AT that time, never before.
bars = trades.resample("1h", label="right", closed="right").agg(
    {"price": "ohlc", "quantity": "sum"}
)

# Expanding, not full-sample: every statistic uses only data that existed at t.
rolling_mean = bars["close"].rolling(50, min_periods=50).mean()
rolling_std = bars["close"].expanding(min_periods=500).std()
zscore = (bars["close"] - bars["close"].expanding(min_periods=500).mean()) / rolling_std

signal = (bars["close"] > rolling_mean).astype(int)

# Execution is one bar after the decision, and the return is FORWARD from that point.
forward_return = bars["close"].pct_change().shift(-1)
pnl = signal.shift(1) * forward_return.shift(1)
```

Better still: do not compute PnL by multiplying aligned series at all. Run the strategy through the same event loop the live system uses, where the engine physically cannot hand a strategy a bar that has not closed. That is the point of backtest/live parity ([`../../ARCHITECTURE.md`](../../ARCHITECTURE.md) §4) — the leak becomes structurally impossible rather than something a reviewer has to spot.

### The project's mechanical defence

- **One code path.** Strategy code is identical in backtest, walk-forward, paper and demo-live; only the `ExecutionVenue` swaps. A strategy cannot see a future bar because the engine does not have one to give.
- **Point-in-time feature store.** A feature value at `t` must be reproducible from data that existed at `t`, and the store enforces it.
- **An adversarial leakage test** that actively attempts to inject future data and **must fail closed**. It runs in `make check`.
- **Latency sensitivity as a standing diagnostic.** Re-run with one extra bar of execution delay. A result that dies is a leak.
- Full rules: [`../../docs/rules/no-lookahead.md`](../../docs/rules/no-lookahead.md).

---

## 2. Survivorship bias

### How it manifests in crypto specifically

Crypto's version is not the mild equity-index version where a handful of constituents get replaced each year. It is **severe, and it is the norm**:

- **Delisted pairs.** Binance delists dozens of spot pairs a year for low liquidity. Their price histories are still in the archive; their delisting is not.
- **Dead tokens.** LUNA and UST went to approximately zero in May 2022. A universe built from today's listed symbols contains neither, so a backtest over 2021–2022 never has the chance to hold the thing that went to zero.
- **Exchange collapses.** FTX failed in November 2022, taking FTT and everything quoted against it. A dataset assembled from surviving venues has silently conditioned on venue survival as well as token survival.
- **Stablecoin depegs that removed a quote asset entirely.** UST's collapse eliminated a quote leg. BUSD's wind-down removed another. Pairs quoted in a dead stablecoin vanish from `exchangeInfo`, and so does every strategy's exposure to the depeg itself.

The compound effect: **a universe built from today's `exchangeInfo` has conditioned on surviving every one of these events.** A mean-reversion strategy backtested on that universe learns that dips recover, because in this sample they always did — the ones that did not are not in the sample.

### The symptom you would actually observe

Mean-reversion and "buy the dip" strategies with implausibly high win rates and no fat left tail. Altcoin baskets that outperform BTC over any window. A maximum drawdown that is suspiciously close to BTC's when the portfolio holds long-tail assets — real long-tail crypto drawdowns are much worse, and the ones that were worst are missing.

### Wrong

```python
markets = client.fetch_markets()
symbols = [m["symbol"] for m in markets if m["quote"] == "USDT" and m["active"]]
bars = load_bars(symbols, start="2020-01-01")   # today's survivors, five years ago
```

### Right

```python
from datetime import datetime


def universe_as_of(conn, as_of_utc: datetime) -> frozenset[str]:
    """Symbols that were listed and tradable at `as_of_utc`.

    The symbol_universe table is built from historical exchangeInfo snapshots and
    delisting announcements, with listed_at/delisted_at as timestamptz. It is the
    only permitted source of a backtest symbol set. A universe derived from a live
    exchangeInfo call has conditioned on survival and cannot be repaired by
    filtering afterwards.
    """
    rows = conn.execute(
        """
        SELECT symbol FROM symbol_universe
        WHERE listed_at <= %(as_of)s
          AND (delisted_at IS NULL OR delisted_at > %(as_of)s)
        """,
        {"as_of": as_of_utc},
    ).fetchall()
    return frozenset(row[0] for row in rows)
```

Delisted symbols must carry their **terminal outcome**, not just an end date. A token that went to zero and a token that was delisted for low volume at a normal price are different events, and a backtest that treats both as "series ends here" gets the first one free.

### The project's mechanical defence

- **Point-in-time symbol universe** stored as a table with `listed_at` / `delisted_at`, built from historical `exchangeInfo` snapshots. The backtest engine resolves its universe per bar timestamp, never once at configuration time.
- **Delisting outcome recorded** as a typed terminal event so that a position in a delisted asset resolves at the correct terminal price rather than silently disappearing.
- The `/backtest` command's item K asks the question directly: is the symbol set the set that existed at the start of the window, or the set that exists today?
- Note the interaction with [`./binance-testnet.md`](./binance-testnet.md) fact 4: testnet is missing 79 spot and 189 futures symbols relative to production, so a universe taken from testnet is survivorship-biased *and* arbitrarily truncated.

---

## 3. Overfitting and multiple testing

### How it manifests in crypto specifically

The binding constraint is history length. **Liquid USDT perpetual data begins around 2019–2020**, so roughly six years exist. At daily frequency that is about 2,200 observations and — after accounting for autocorrelation and regime clustering — perhaps four genuinely independent macro states. Against that, a single 4×5×5×3 parameter grid over 8 symbols is 2,400 trials. **The search space routinely exceeds the information content of the data by orders of magnitude.**

The specific traps:

**The parameter sweep.** Any grid. The result you report is the maximum over the grid, and the expected maximum of `N` zero-mean draws grows like `sd * sqrt(2 ln N)` — see [`./statistics-for-trading.md`](./statistics-for-trading.md) §5. At 2,000 trials and a cross-trial Sharpe dispersion of 0.15, the best backtest shows a Sharpe near 0.58 with no edge present at all.

**The abandoned grid that still charges.** You declare 200 points, run 12, they look bad, you stop. The honest charge is 200, because the selection event happened at specification: had one of the first 12 looked good you would have stopped there and reported it. Charging 12 understates `N` by 188.

**The invisible trials.** Reruns with a "tiny" parameter tweak. Variants explored during research before the hypothesis was registered. A feature engineered, tested informally, and discarded. All of these are trials and all of them charge.

**Feature-selection overfitting.** Choosing which of 40 candidate features to include *is* a search over 2^40 subsets, even if you only evaluated 40 models.

### The symptom you would actually observe

A performance surface with a sharp peak — the neighbours of the optimal parameters perform far worse. A real edge has a **plateau**: nearby parameters work nearly as well, because the effect is a property of the market rather than of a coincidence at one setting. Also: high in-sample Sharpe with fold Sharpes that scatter around zero, and a large gap between validation and forward performance.

### Wrong

```python
best = None
for fast in range(5, 50, 5):
    for slow in range(50, 300, 25):
        for threshold in [0.5, 1.0, 1.5, 2.0]:
            result = backtest(fast=fast, slow=slow, threshold=threshold)
            if best is None or result.sharpe > best.sharpe:
                best = result
print(f"Best Sharpe: {best.sharpe}")   # 360 trials, reported as one number
```

### Right

```python
# The grid is declared and CHARGED before any data is read.
parameter_grid = {"funding_horizon_h": [24, 48, 72]}   # multiples of the 8h funding
                                                       # interval, fixed by mechanism
trials = math.prod(len(v) for v in parameter_grid.values()) * n_symbols * n_variants
charge_global_trials(correlation_id, trials)           # append-only, DB-enforced

# ... run ...

sharpe_deflated = haircut_sharpe(
    sharpe_observed=result.sharpe,
    n_trials=read_global_trial_count(),                # the PROJECT total, not this study's
    sharpe_variance_across_trials=result.cross_trial_sharpe_variance,
)
```

The design lesson is in the grid, not the code: `funding_horizon_h` has three values rather than thirty, and they are multiples of eight hours **because the mechanism says funding settles every eight hours**. If you cannot fix a parameter a priori from a mechanism, you do not have a mechanism — you have a search, and it is priced accordingly.

### The project's mechanical defence

- **Global, monotone trial counter charged at specification time**, feeding the deflated Sharpe. Never reset, never expired, never reduced. [`../../docs/rules/overfitting-defences.md`](../../docs/rules/overfitting-defences.md).
- **Combinatorial purged cross-validation** with purge and embargo, reporting a fold Sharpe *distribution* and a sign-consistency figure rather than a point estimate.
- **A permanently held-out period, burned once touched**, tracked in a holdout ledger and requiring human authorisation.
- **Champion/challenger promotion requiring forward performance**, not validation performance.
- **Parameter-plateau check**: a result whose neighbours in parameter space collapse is reported as a peak and treated as overfit.

---

## 4. Ignoring costs

### How it manifests in crypto specifically

Crypto invites high turnover — 24/7 markets, no session, cheap-looking taker fees, and strategies whose signal decays in minutes. That is exactly the regime where costs dominate.

**The arithmetic that ends most intraday ideas.** Round-trip taker cost on liquid USDT perpetuals runs on the order of **9bp** under production-calibrated parameters (fees + half-spread + slippage). A strategy doing **40 round trips a day** therefore pays `40 × 9 = 360bp = 3.6% per day` in costs. To break even it needs a gross edge of 3.6% per day, which compounded is not a number anyone should say out loud. The point is not that the strategy is marginal. It is that **the required gross edge is implausible by two orders of magnitude**, and this can be established in one line of arithmetic before any backtest is run.

The four cost components, each with a crypto-specific wrinkle:

| Component | Crypto wrinkle |
|---|---|
| **Fees** | Tiered by 30-day volume and by BNB discount. A backtest at VIP0 taker rates is right for this project and wrong for anyone assuming a market-maker schedule. Maker rebates on some venues are negative fees, which makes a maker assumption tempting and unverifiable |
| **Half-spread** | 0.16bp on production BTC perpetual is not typical of the long tail. Altcoin perpetual spreads are 5–50× wider, and a universe-wide backtest that applies a BTC spread to everything is fiction across most of its symbols |
| **Impact** | Perpetual books are thin outside the top few symbols. Impact is convex in size and it widens exactly during the volatility that generates most signals |
| **Funding** | A perpetual position pays or receives funding every 8 hours. A strategy holding through settlement has a cost or a subsidy that is **not** in the price series and is frequently larger than the edge. See [`./crypto-perpetuals.md`](./crypto-perpetuals.md) |

**The testnet-calibration trap.** Binance futures testnet showed a **7.5bp** spread against production's **0.16bp**, with roughly 10× inflated volume ([`./binance-testnet.md`](./binance-testnet.md) fact 6). Calibrating on testnet is wrong in both directions at once: pessimistic on spread and wildly optimistic on fill probability and capacity, because a thin bot-populated book has no real queue and no adverse selection.

**Maker fills are unfalsifiable without L2.** Assuming passive fills cuts modelled cost roughly in half and is the single most effective way to make a failing strategy pass. It is also unverifiable: passive fill probability requires queue position, which requires full-depth order book history, which [`../../ARCHITECTURE.md`](../../ARCHITECTURE.md) §6 establishes does not exist for free. `bookDepth` is aggregated depth bands sampled about once a minute, not snapshots. An unfalsifiable input chosen because it produces the desired answer is a decision, not an assumption.

### The symptom you would actually observe

A strategy whose Sharpe is barely sensitive to the cost parameter — which usually means costs are being applied to too few trades, or per-notional rather than per-round-trip. Also: net PnL that scales linearly with turnover in the *positive* direction. And a per-trade edge of 1–3bp presented as significant.

### Wrong

```python
FEE = 0.0004                              # one number for everything
pnl = position.diff().abs() * FEE
net = gross - pnl                         # no spread, no impact, no funding
```

### Right

```python
from decimal import Decimal


def round_trip_cost_bps(
    symbol: str,
    notional_usd: Decimal,
    cost_params: CostParameterSet,
) -> Decimal:
    """Per-round-trip cost in basis points, from production-calibrated parameters.

    cost_params carries a provenance id naming its calibration source. A parameter
    set with testnet provenance raises at load time (CLAUDE.md section 2), because
    the failure is silent otherwise and the resulting backtest is fiction.
    """
    taker_fee_bps = cost_params.taker_fee_bps                      # both legs
    half_spread_bps = cost_params.half_spread_bps[symbol]          # PER SYMBOL
    impact_bps = cost_params.impact_coefficient[symbol] * (
        notional_usd / cost_params.reference_notional_usd
    ).sqrt()
    return Decimal(2) * (taker_fee_bps + half_spread_bps + impact_bps)


# Funding is a separate accrual, not part of the round trip.
funding_cost_usd = sum(
    position_notional_usd_at(settlement) * funding_rate_at(settlement)
    for settlement in settlements_during_holding_period
)
```

And run the **cost sensitivity sweep** as a standing check: recompute the result at 0.5×, 1×, 2× and 3× the modelled cost. A strategy that survives 3× is robust. A strategy insensitive to the multiplier is not applying costs correctly.

### The project's mechanical defence

- **Cost parameters carry a provenance id** and testnet provenance is refused at load.
- **Per-symbol spread and impact**, never a universe-wide constant.
- **Funding accrual modelled as a separate cash flow** at each settlement, not folded into the price.
- **`/backtest` item F**: gross edge under roughly 2× modelled cost is treated as a cost-model artefact.
- **Fill assumptions default to taker.** A maker assumption requires an explicit, justified override and is flagged in the result.

---

## 5. In-sample optimisation

### How it manifests in crypto specifically

The failure is universal, but crypto's short history makes it acute: with six years of data, a 70/30 split leaves under two years of test data, spanning roughly one regime. And because the same six years are all anyone has, **every strategy in the project is tested on the same test set** — which converts it into training data after the second use.

Four specific forms:

**A single train/test split is non-evidence.** `CLAUDE.md` §11 names it directly. One split gives one number with no dispersion, so you cannot distinguish an edge from a regime.

**Tuning until it looks good.** The literal definition of overfitting. Every adjustment after seeing a result is a trial, and the sequence of adjustments is itself a search whose size nobody records.

**The held-out period burned by a "quick look".** It is burned the moment it is read — including for a plot, including "just to sanity check". There is no partial read. The holdout's entire value is that it has never influenced any decision, and looking at it influences the next decision whether or not you intend it to.

**Reusing the confirm set across ten strategies.** Each use is a selection event. After ten strategies, the confirm set has an effective trial count of ten and the best performer on it is the expected maximum of ten draws. It is now in-sample and nobody wrote that down.

### The symptom you would actually observe

Validation Sharpe far above forward Sharpe. Fold Sharpes that are excellent in the training folds and scatter around zero out of sample. A "final" parameter set that differs from the initial one in every dimension. And — the tell that is easiest to spot in a diff — a config file whose parameters have been edited more times than the strategy's logic has.

### Wrong

```python
train, test = df[: int(0.7 * len(df))], df[int(0.7 * len(df)) :]
for params in candidate_params:
    if backtest(train, params).sharpe > best_sharpe:
        best_params = params
print(backtest(test, best_params).sharpe)   # reported as out-of-sample. It is not.
```

It is not out-of-sample, because `test` was used to decide whether to publish. And if the number had been bad, `candidate_params` would have been widened — which makes `test` part of the search whether or not it was queried.

### Right

```python
# CPCV: a distribution of fold Sharpes, with purge and embargo sized to the label.
splitter = CombinatorialPurgedCV(
    n_groups=8,
    n_test_groups=2,                  # 28 splits
    purge=timedelta(hours=48),        # == the label horizon, exactly
    embargo=timedelta(hours=24),
)
fold_sharpes = [backtest(train_idx, test_idx, params).sharpe
                for train_idx, test_idx in splitter.split(df)]

sign_consistency = sum(s > 0 for s in fold_sharpes) / len(fold_sharpes)
# Read the CONSISTENCY, not the mean. 22/28 positive is a different claim from
# 15/28 positive at the same mean, and the second one is one regime in costume.
```

Plus walk-forward, which answers the different question of whether a causal procedure — retraining only on the past, at the real refit cadence — would have captured it.

### The project's mechanical defence

- **CPCV and walk-forward, both, or it is not evidence.** A single split is rejected at review.
- **Holdout ledger** with human authorisation required and one read per milestone maximum.
- **Every configuration ever evaluated increments the trial counter**, including abandoned ones, which prices the tuning loop.
- **Champion/challenger promotion on forward performance**, so validation performance alone cannot promote anything.

---

## 6. Regime dependence

### How it manifests in crypto specifically

Crypto's regimes are shorter and more violent than any other liquid asset class, and a five-year backtest may contain only three or four independent ones.

| Era | Character | What it does to a backtest |
|---|---|---|
| 2017 bull | Retail mania, extreme trend persistence | Any long-biased trend follower looks like genius |
| 2018 bear | Grinding decline, falling volume | Trend followers survive; mean reversion is destroyed |
| **March 2020** | COVID liquidation cascade, BTC roughly halving in a day, exchanges degraded | A single day that dominates tail statistics. Include or exclude it and the max drawdown changes by a factor of two |
| 2021 bull | Two distinct legs with a violent interruption | Sub-period dependence within one "regime" |
| **May 2021** | Leverage flush plus the China mining ban | Second dominant tail event; funding flipped hard negative |
| 2022 bear | LUNA/UST in May, FTX in November | Survivorship interacts here (§2): the assets that defined the regime are missing from a modern universe |
| 2023–present | Structurally falling realised volatility as the asset class matures, spot ETF flows, institutional participation | A volatility-scaled strategy fitted on 2017–2021 volatility is mis-sized by a factor of two or more |

Two crypto-specific regime axes that people forget:

**Funding regimes flip sign.** Perpetual funding is persistently positive in bull phases and persistently negative in stressed ones. A carry strategy that collects positive funding is a bull-regime strategy wearing a market-neutral label, and it pays in exactly the environment it was supposed to hedge.

**Volatility is structurally falling.** This is not a regime that alternates, it is a trend. Parameters expressed in absolute price or absolute volatility terms — not in units of current realised volatility — are silently mis-calibrated for the most recent and most relevant part of the sample.

### The symptom you would actually observe

Performance concentrated in the first fold or in one calendar year. Cumulative PnL with a single steep segment and flat stretches either side. Fold sign-consistency near 0.5 with a high mean. And the Simpson's-paradox form: a positive pooled edge with negative per-regime edges, which happens when the strategy simply trades more in the higher-drift regime — see [`./statistics-for-trading.md`](./statistics-for-trading.md) §11.

### Wrong

```python
print(f"Sharpe: {returns.mean() / returns.std() * math.sqrt(365)}")
# One number over five years and four regimes.
```

### Right

```python
regime_report = (
    trades.assign(
        vol_tercile=pd.qcut(trades["rvol_30d"], 3, labels=["low", "mid", "high"]),
        funding_sign=np.sign(trades["funding_8h"]),
        era=pd.cut(trades["ts"], bins=REGIME_BOUNDARIES, labels=REGIME_NAMES),
    )
    .groupby(["era", "vol_tercile", "funding_sign"])["pnl_bps"]
    .agg(["mean", "count", "std"])
)
# Report the TABLE. An aggregate that hides a negative cell is not a summary,
# it is a redaction.
```

### The project's mechanical defence

- **Cross-regime consistency is a term in the survival score**, weighted, in `fking.evolution.scoring` — not a note in a document. The system optimises what it measures.
- **Regime breakdown is a mandatory reported artefact** (`/backtest` item I), split by volatility tercile, trend/chop, funding sign and era.
- **Minimum episodes per regime**, so a regime-conditional claim needs episodes from that regime rather than sample-wide episodes.
- A verdict may legitimately be "this is a bet on regime X recurring". That is a different and much smaller claim than "this strategy works", and it is stated as such.

---

## 7. Insufficient sample size

### How it manifests in crypto specifically

Two forces compound. Crypto's usable liquid history is short — roughly six years of deep perpetual data — and its returns are strongly autocorrelated in volatility, which destroys the effective count. The result is that a backtest with tens of thousands of rows may carry the statistical weight of a few dozen.

**Episodes, not observations.** One holding period is one draw regardless of how many bars it spans. One funding-extremity regime lasting 30 hours is one draw, not 30. The `quant` agent's worked example makes the gap concrete: **41,208 hourly observations, 37 independent episodes.** The naive standard error is understated by `sqrt(41208/37) ≈ 33×`.

**Autocorrelation destroys the effective count.** For AR(1)-like dependence, `n_eff ≈ n (1-ρ)/(1+ρ)`. At `ρ = 0.98` — normal for a slow regime indicator sampled hourly — that is `n/99`.

**Rare setups may be untestable, permanently.** Compute the minimum detectable effect before the test. With episode-return dispersion of 80bp and 37 episodes, the MDE at 80% power is roughly 33bp ([`./statistics-for-trading.md`](./statistics-for-trading.md) §9). A hypothesis claiming a 6bp effect cannot be tested on that sample — no outcome distinguishes 6bp from zero. Detecting 6bp would need over a thousand episodes; at 40 firings a year, that is 27 years of history in an asset class that has fewer than ten. **"Untestable with available data" is the correct verdict** and reaching it before the work is the whole value of computing it first.

### The symptom you would actually observe

A headline trade count in the thousands alongside a handful of distinct market episodes. Confidence intervals that are never reported. A Sharpe that swings wildly when one month is excluded. And the giveaway: total PnL dominated by the largest five trades.

### Wrong

```python
print(f"n = {len(df)}")                    # bar count
print(f"Sharpe = {sharpe:.2f}")            # no interval, no episode count
```

### Right

```python
n_observations = len(df)
n_episodes = count_independent_episodes(positions)   # contiguous holdings / setups
sharpe_low, sharpe_high = bootstrap_sharpe_interval(
    returns, block_length_bars=7 * 24, periods_per_year=8760, seed=20260801
)
mde_bps = 2.486 * episode_return_sd_bps / math.sqrt(n_episodes)

print(f"n_observations={n_observations} n_episodes={n_episodes} "
      f"sharpe={sharpe:.2f} [{sharpe_low:.2f}, {sharpe_high:.2f}] mde={mde_bps:.1f}bp")
```

### The project's mechanical defence

- **`HypothesisResult` carries `n_observations` and `n_independent_episodes` as separate required fields**, so the flattering one cannot be quoted alone.
- **Minimum episode floors in the decision rule** — 30 for any `supported` verdict, 30 per regime for a regime-conditional claim.
- **Block bootstrap and episode-level resampling** rather than IID standard errors.
- **Minimum trade count enforced before a Sharpe is reported at all** (`/backtest` item G).

---

## Pre-flight checklist

Run before believing any backtest result. This is the pitfall-facing complement to the `/backtest` command's A–K interrogation; run that too, and answer each item with the evidence you looked at rather than a summary.

**Data and universe**
1. Was the symbol universe resolved **point-in-time**, or from today's `exchangeInfo`? Name the source table.
2. Do delisted symbols carry a terminal outcome, or do their series just end?
3. Did timestamp normalisation run keyed on `(market, date)`? Do the first and last bars render as sane UTC — not year 56000, not 1970?
4. Are bars labelled by **close** time, and is every feature computed only from data available at that close?

**Leakage**
5. Every feature: available at bar close, or does it read the bar it predicts?
6. Any centred or forward rolling window, any `shift` with the wrong sign, any `merge_asof` with the wrong direction?
7. Any normalisation, scaler, quantile or z-score fitted on the full range rather than expanding?
8. Any daily or funding series joined onto an intraday index inside its own interval?
9. Did the adversarial leakage test run in this suite and **fail closed**?
10. Re-run with one extra bar of execution delay. Did the result survive, or collapse?

**Trials and validation**
11. Did the global trial counter move, and by how much? Does the charge include the grid you declared but did not fully run?
12. Deflated Sharpe reported with the trial count `N` next to the raw Sharpe?
13. CPCV **and** walk-forward, with purge equal to the label horizon and a stated embargo? Fold sign consistency reported?
14. Was any held-out period touched, including for a plot? Name the exact date ranges used.
15. Do the neighbours of the chosen parameters perform comparably — a plateau — or is this a peak?

**Costs**
16. Cost parameter provenance: production calibration, named. **Not testnet.**
17. Per-symbol spread and impact, or one constant applied universe-wide?
18. Funding modelled as a separate accrual at each settlement?
19. Cost sensitivity at 0.5×, 1×, 2×, 3×. Does the result survive 3×? Is it *suspiciously* insensitive?
20. Taker fills assumed, or is there an unfalsifiable maker assumption doing the work?

**Sample and regime**
21. `n_independent_episodes` reported next to `n_observations`. Does it clear the floor?
22. Minimum detectable effect computed — could this test have detected the claimed effect at all?
23. Regime breakdown by era, volatility tercile and funding sign. Any negative cell hidden inside a positive aggregate?
24. Is PnL dominated by the largest few trades, or by one calendar quarter?

Any unanswered item makes the result **blocked**, not marginal. Rejecting is the expected outcome and a successful use of the checklist — `CLAUDE.md` §1.

---

## Results that should make you suspicious

| Observation | Most likely cause | First thing to check |
|---|---|---|
| **Equity curve is too smooth** | Look-ahead, or costs not applied per round trip | Add one bar of execution delay; recount trades against cost applications |
| **Sharpe above ~2 on an hours-to-days horizon, net of costs** | Look-ahead. This is a bug report until proven otherwise ([`./statistics-for-trading.md`](./statistics-for-trading.md) §14) | Feature-by-feature point-in-time audit |
| **Zero losing months** | Survivorship, or a full-sample normalisation leaking the whole period | Universe source; every `mean()`/`std()`/`fit()` call in the feature path |
| **Performance concentrated in the first fold** | Regime dependence, or a purge/embargo leak into the earliest folds | Fold-by-fold Sharpes with dates; purge length against label horizon |
| **Results insensitive to cost assumptions** | Costs applied per notional instead of per round trip, or applied to too few trades | `sum(cost_events)` against `2 × round_trip_count` |
| **Sharp peak in parameter space** | Overfitting | Neighbour performance; trial count charged |
| **Thousands of trades, tens of episodes** | Autocorrelated observations counted as independent draws | Episode counter; block bootstrap interval |
| **Backtest beats forward performance by more than ~40%** | In-sample optimisation, or a burned holdout | Holdout ledger; count of configurations evaluated |
| **Maker fills assumed** | An unfalsifiable input chosen for its answer | Whether L2 depth exists to justify it. It does not |
| **Win rate above 65% with positive expectancy** | Look-ahead, or a hidden short-volatility payoff with an unmodelled tail | Loss distribution shape; behaviour in March 2020 and May 2021 |
| **Altcoin basket outperforms BTC over every window** | Survivorship | `listed_at` / `delisted_at` coverage in the universe table |

---

## If you remember nothing else

**None of these seven fails loudly — they all fail flatteringly.** A backtest is a claim, not evidence, and the default assumption is that a good-looking result is wrong. Count trials at specification time, resolve the universe point-in-time, calibrate costs from production, count episodes rather than observations, and re-run with one extra bar of delay before you believe anything. Related: [`../../docs/rules/no-lookahead.md`](../../docs/rules/no-lookahead.md), [`../../docs/rules/overfitting-defences.md`](../../docs/rules/overfitting-defences.md), [`./statistics-for-trading.md`](./statistics-for-trading.md), [`./binance-testnet.md`](./binance-testnet.md), [`../knowledge/verified-facts.md`](../knowledge/verified-facts.md), and [`../../ARCHITECTURE.md`](../../ARCHITECTURE.md) §4 for why parity is the defence that makes the rest enforceable.
