# Backtest Engine

Event-driven design, the venue abstraction, the cost model, and the validation methodology.

This expands `ARCHITECTURE.md` §4 and §10. The operating posture, stated once and meant literally:

> **A good backtest result is a bug report until proven otherwise.**

The engine's purpose is not to find out whether a strategy works. It is to make it as hard as possible for a strategy that does not work to look like one that does. Almost every design choice below trades convenience for that.

---

## 1. Why the engine is custom

Recorded in ADR 0005 and summarised in `ARCHITECTURE.md` §4. `NautilusTrader` is a genuinely strong alternative and was seriously considered; it was rejected because adopting it means adopting its domain model, and the risk engine and evolution engine would become plugins to its lifecycle rather than components with authority over it. Vectorized engines (VectorBT, `bt`) were rejected outright for the core: they cannot express path-dependent risk logic — trailing stops reacting to intrabar state, portfolio-level kill switches — without leaking look-ahead.

The decision is open to revisit, not closed. What is *not* open to revisit is the property the custom engine exists to guarantee: one code path, four venues.

---

## 2. Event-driven design

### The event loop

A single-threaded, deterministic loop over a merged, time-ordered event stream. There is no wall clock anywhere inside it.

```
                 ┌─────────────────────────────────────────┐
                 │  EventQueue (heap, ordered by           │
                 │  (timestamp, priority, sequence))       │
                 └───────────────┬─────────────────────────┘
                                 │ pop
                                 ▼
   MarketEvent ──► FeatureStore.update(t) ──► point-in-time snapshot
                                 │
                                 ▼
                        Strategy.on_event(snapshot, clock)
                                 │  emits Signal | None
                                 ▼
                        RiskEngine.decide(signal, portfolio)
                                 │  emits Order | Rejection (both recorded)
                                 ▼
                        ExecutionVenue.submit(order)
                                 │  schedules future events:
                                 │    OrderAck   at t + latency_ack
                                 │    Fill       at t + latency_fill (0..n partials)
                                 │    Reject     at t + latency_ack
                                 ▼
                        Portfolio.apply(fill) ──► new immutable Portfolio
                                 │
                                 ▼
                        Metrics/AuditRecorder.record(...)
```

### Event types and their ordering

| Event | Priority | Source |
|---|---|---|
| `MarketDataEvent` (bar close, trade, quote) | 0 | Parquet/DuckDB scan |
| `FundingEvent` (perpetual funding settlement) | 1 | Synthesised at 00:00/08:00/16:00 UTC |
| `OrderAckEvent` | 2 | Venue simulation |
| `FillEvent` (may be partial) | 2 | Venue simulation |
| `RejectEvent` | 2 | Venue simulation |
| `TimerEvent` (strategy-scheduled) | 3 | Strategy |
| `ReconciliationEvent` | 4 | Venue (live venues only) |

Ties on `(timestamp, priority)` break on a monotone sequence number assigned at insertion. There is no other tiebreaker and none may be added: a comparison that falls through to object identity or dict ordering is a non-determinism source that will surface as a result that differs between two runs of the same config hash.

**Market data has priority 0 and venue events have priority 2** on purpose. If a fill and a bar carry the same timestamp, the bar is processed first. The alternative — a fill arriving before the market data that would have justified it — lets a strategy observe its own fill before the price that caused it, which is a look-ahead channel that looks like an ordering detail.

### The two hard rules of the loop

1. **No component reads the clock.** The clock is injected, always. `datetime.now()` inside `strategy`, `risk` or the engine makes the run non-reproducible, and `CLAUDE.md` §4 makes clock injection mandatory in exactly those modules.
2. **A strategy sees only what a snapshot at *t* contains.** The `FeatureStore` is advanced to *t* before the strategy is called and exposes no method to read beyond it. Look-ahead is prevented by the shape of the interface, not by the strategy author's care — because the strategy author will frequently be an LLM.

### What a handler may do, and the two things the loop refuses

A handler receives a `RunContext` exposing exactly three operations: read the clock, schedule a future event, and derive a seed for a named source of randomness. It cannot advance the clock, reorder the queue, or read an event that has not been dispatched — because a handler that could look ahead would have look-ahead available *through an interface*, and no amount of care in the strategy author prevents that when the strategy author is frequently an LLM.

Two refusals, both raising rather than repairing:

**An event scheduled before the instant being dispatched.** Scheduling *at* the current instant is ordinary — a bar, the fill it caused and the timer it woke share one timestamp, and priority and sequence order them. Scheduling before it is a causality violation and is never clamped to `now`: a clamped fill happens at a plausible-looking time and produces an excellent equity curve with no error anywhere.

**A run that exceeds its declared event budget.** The failure this catches is a handler that schedules at its own timestamp on every call, so simulated time never advances and the queue never drains. Under an unattended evolution cycle that is a machine occupied indefinitely rather than a crash — nothing times out, nothing alerts, and the generation never completes.

Events scheduled *past* the window's end are neither an error nor invisible: a fill acknowledged 180 ms after the final bar is ordinary, so those are dropped and **counted**, and the count is reported on the trace. A run that dropped thousands of them ended mid-flight, and a count of zero where a venue was active is itself a finding.

`RunConfig` carries only what can change a run's output — strategy, parameters, symbols, window, seed, budget — and is content-hashed into `config_hash` over canonical JSON, never over `repr`, `pickle` or Python's `hash()`, which is salted per process by `PYTHONHASHSEED`. Anything that can change without changing the result stays out, or two runs producing identical numbers carry different identities and the determinism check below passes vacuously by never comparing anything.

### What the engine does not do

It does not vectorize. It does not batch bars for speed. A full-universe 18-month 1m backtest runs in minutes, not seconds, and that is an accepted cost: every optimisation that processes a window of bars at once is an opportunity for the window's later bars to influence its earlier decisions.

---

## 3. The venue abstraction

```
Strategy ──► Signal ──► RiskEngine ──► Order ──► ExecutionVenue
                                                   ├── BacktestVenue   simulated fills, historical clock
                                                   ├── PaperVenue      live data, simulated fills, live clock
                                                   ├── DemoVenue       Binance testnet, real order lifecycle
                                                   └── ReplayVenue     recorded venue responses, for tests
```

One interface:

```python
class ExecutionVenue(Protocol):
    async def submit(self, order: Order) -> VenueAck: ...
    async def cancel(self, client_order_id: str) -> VenueAck: ...
    async def reconcile(self) -> VenueState: ...
    @property
    def clock(self) -> Clock: ...
```

### Parity is architectural, not disciplinary

This is the single most important property of the engine, and the distinction in the heading is the whole point.

**Disciplinary parity** means "we are careful to keep the backtest and live paths in sync". It survives about three months. Someone adds a live-only guard, someone adds a backtest-only shortcut for speed, and the two paths drift. The drift is invisible because both paths still work.

**Architectural parity** means there is exactly one code path and the difference is a constructor argument. Strategy code, feature computation, signal generation, risk sizing, order construction, portfolio arithmetic and metric computation are **byte-identical** across all four venues. There is nothing to keep in sync because there is nothing that differs.

What this buys: when a backtest and a paper run disagree, the disagreement is necessarily about the *venue* — fills, latency, costs — and never about the strategy. That narrows an investigation from "somewhere in the system" to a bounded surface. Without it, every backtest result is unfalsifiable, because you can never distinguish "the strategy is bad" from "the harness differs".

Enforcement:

- `import-linter` forbids `strategy` from importing `execution` at all, so a strategy cannot detect which venue it is running against.
- No `if isinstance(venue, BacktestVenue)` anywhere in the codebase. A CI grep asserts it.
- No `is_backtest` flag on any config object reachable from `strategy` or `risk`.
- A **parity test** runs the same strategy over the same window through `BacktestVenue` and `ReplayVenue` with recorded fills injected, and asserts the emitted `Signal` sequence is identical. Signals, not fills — fills legitimately differ; signals must not.

`CLAUDE.md` §11 names the corresponding anti-pattern: writing a backtest-only code path for a strategy. If a strategy needs different code to run in backtest, fix the venue abstraction. Do not fix the strategy.

---

## 4. The cost model

The cost model is where honest backtests are separated from marketing. Most strategies that die in this system die here, and they die correctly.

### The standing rule

> **Cost parameters are calibrated from PRODUCTION market data. Never from testnet.**

Measured on Binance USDⓈ-M futures testnet against production over the same window:

| Metric | Testnet | Production | Ratio |
|---|---|---|---|
| Median spread, BTCUSDT | 7.5 bp | 0.16 bp | **~47x** |
| Reported volume | inflated | reference | **~10x** |

A cost model fitted to testnet is fiction. Whichever direction the error runs, the result is void — and note that it can run either way: a 47x overstated spread makes every strategy look unprofitable, and an inverted config (0.075bp instead of 7.5bp) makes every strategy look brilliant. Provenance is disqualifying regardless of which way the number pushed the result.

Enforcement is structural, not a review checklist:

```python
class CostModel(BaseModel):
    version: str
    calibration_source: str          # e.g. "binance_um_production_2026-03..2026-05"
    calibration_method: str
    calibrated_at: date

    @field_validator("calibration_source")
    @classmethod
    def _no_testnet(cls, v: str) -> str:
        if "testnet" in v.lower():
            raise ValueError("cost model calibration source must not be testnet")
        return v
```

`BacktestResult.cost_model_calibration_source` is recorded on every run. A run whose provenance mentions testnet is voided, and so is every result that used that cost model version. Testnet-measured slippage feeds exactly one thing: the **divergence monitor** in `execution`, which compares realised testnet cost against the production model to detect that the harness is behaving as documented. It never feeds calibration.

### Components

Total round-trip cost is the sum of six terms, reported separately, never as one number.

#### 4.1 Fees

Maker and taker, per market, as basis points of notional. Production VIP-0 reference values:

| Market | Maker | Taker |
|---|---|---|
| Spot | 10.0 bp | 10.0 bp |
| USDⓈ-M futures | 2.0 bp | 5.0 bp |

Fee tier is configuration (`CONFIGURATION.md` §6) with the VIP-0 rates as the default, because assuming a better tier than you have is a way to manufacture edge. Fee is applied on notional at the fill price, per fill — a partially filled order pays fees on each partial, which matters for sliced execution.

#### 4.2 Spread

Half-spread on a marketable order, zero on a passive fill that is not adversely selected. Calibrated per symbol from production `bookTicker` as a distribution, not a scalar: **p50 and p99, with an hour-of-day profile**.

The hour-of-day profile is not a refinement. Spread on BTCUSDT roughly doubles in the hour around 00:00/08:00/16:00 UTC funding settlement, and a strategy that concentrates its entries there against a flat median spread is being subsidised by the cost model. Backtests run against the p50 profile and are **re-run against p99** as a robustness check; a strategy whose edge disappears at p99 spread is a strategy that dies during the only conditions that matter.

#### 4.3 Slippage as a function of order size against depth

Slippage is not a constant. It is a function of how much of the available depth an order consumes.

```
slippage_bp(q) = half_spread_bp + impact_coefficient * (q / depth_at_touch) ** impact_exponent
```

with `impact_exponent` defaulting to **0.5** — the square-root law, which is the standard empirical finding and is used here as a prior, not as a measurement.

And here is the constraint that makes this section honest: **we do not have the depth data to fit this properly.** `DATA_PIPELINE.md` §9 sets the ceiling — top-of-book on futures and ~1/minute coarse depth bands at ±1–5%. There is no per-level book history, so `depth_at_touch` is the `bookTicker` quantity and nothing behind it is observable.

The consequence is a deliberate conservatism rule:

> **Assume the quoted top-of-book quantity is all the liquidity there is, until a fill proves otherwise.**

An order larger than the touch quantity is modelled as walking into the ±1% depth band at a linearly interpolated price, and an order larger than the ±1% band notional is **rejected by the backtest venue as unfillable** rather than filled at an invented price. That rejection is recorded and appears in the result. A strategy whose backtest is full of size rejections has discovered a capacity limit, which is a genuine finding, and is far better than the alternative — filling arbitrary size at the touch, which is how a strategy with no capacity gets promoted.

#### 4.4 Latency

Three latencies, modelled separately because they have different causes and different fixes:

| Latency | Default | Source |
|---|---|---|
| Decision → send | measured per-stage from production traces | Feature computation, agent call, risk sizing |
| Send → venue ack | 180 ms | Measured network + venue ack |
| Ack → fill | 95 ms + queue time | Venue |

`decision_to_arrival` is the one that matters and the one usually omitted. This system computes features, may consult an LLM agent whose latency is seconds against a free-tier quota, applies risk sizing, and only then sends. `OBSERVABILITY.md` §5 requires the per-stage decomposition, and the backtest consumes the measured distribution rather than a guessed constant.

The backtest applies latency by **scheduling** the ack and fill events at `t + latency`, so the market moves during the interval exactly as it would live. It does not apply a post-hoc basis-point penalty; a penalty cannot reproduce the case where the market moved *through* the limit price during the latency window and the order never filled at all.

#### 4.5 Partial fills

Orders fill against the modelled depth, and a marketable order for more than the touch quantity produces **multiple fill events at successively worse prices**, each with its own timestamp, fee and audit row.

Passive limit orders use a queue-position model that is deliberately pessimistic, because queue data does not exist: a resting order is assumed to be at the **back** of the visible quantity at its price, and fills only after cumulative traded volume at that price exceeds the quantity that was resting when it arrived. Any strategy whose backtest fills 100% of its limit orders is trading against a market that does not exist, and this model makes that impossible.

Passive fills additionally carry an **adverse-selection markout**: a passive fill is disproportionately one that filled because the market came to you. Measured on production BTCUSDT, passive limits inside 1bp filled ~71% of the time with a 5-minute post-fill markout of −3.2bp against a captured spread of 0.8bp. The model applies the measured markout as a cost on passive fills. Without it, passive execution appears free and every strategy discovers that being passive is optimal.

#### 4.6 Funding, for perpetuals

USDⓈ-M perpetuals settle funding every 8 hours at **00:00, 08:00 and 16:00 UTC** (some symbols 4-hourly). A `FundingEvent` is synthesised at each settlement and applies:

```
funding_payment = position_notional_usd * funding_rate      # signed; longs pay when positive
```

Funding is charged on the position held **at the settlement instant**, not on average exposure. That discreteness is exploitable and must be modelled exactly: a strategy that flattens 30 seconds before settlement pays nothing, and a strategy that holds through pays in full. Historical funding rates come from the futures archive; they are real data, not a modelled constant.

For any strategy holding perpetual positions for more than a few hours, funding is frequently the largest single cost term and dominates fees. A carry strategy's entire P&L *is* the funding term.

#### 4.7 Rejections

Modelled explicitly, not assumed away:

- **Size rejection** — order exceeds modelled available depth (§4.3).
- **Notional/lot filters** — below `MIN_NOTIONAL`, or not on the symbol's `LOT_SIZE` step. Real filters from the exchange's `exchangeInfo`, applied in backtest exactly as live.
- **Price band rejection** — limit price outside the exchange's `PERCENT_PRICE_BY_SIDE` band.
- **Rate-limit rejection** — order rate exceeding the venue's stated budget.

Every rejection is an event, is recorded, and appears in the result as a count. A backtest with zero rejections against a venue that rejects in reality is optimistic in a way that no cost parameter captures.

### Reporting

Gross edge, total cost and net are always reported **separately**. A net number alone is uninterpretable — it cannot distinguish a large edge eaten by costs from a small edge that survived, and those two have completely different futures.

```
edge_to_cost_ratio = gross_edge_per_trade_bp / round_trip_cost_bp
```

**Below 2.0 is a rejection, regardless of net return.** A strategy whose gross edge is 1.5x its costs is one cost-model revision, one fee-tier change, or one volatility regime away from being unprofitable, and it will spend its life oscillating across the line.

---

## 5. Determinism and seeding

A backtest that is not bit-reproducible is not evidence.

- The engine is single-threaded. Concurrency is available only for running *independent* backtests in parallel, never inside one.
- Every source of randomness takes an explicitly injected seed derived from `run_seed`. There is no implicit global RNG use anywhere in `backtest`, `strategy` or `risk`.
- Iteration over sets and dicts is never load-bearing; anywhere ordering matters, an explicit sort key exists.
- Floating-point is banned for money (`CLAUDE.md` §2). `Decimal` arithmetic is deterministic across platforms in a way float summation is not — a float portfolio value can differ in the last bits between an x86 CI runner and an ARM laptop, and once a threshold comparison sits downstream, that difference becomes a different trade.
- The full config is **content-hashed** and stored with the result. Any run is reproducible from its `run_id` alone.

**A result that differs between two runs of the same `config_hash` is a determinism failure and outranks everything else on the queue.** It is not a flake to be retried. Find the unseeded randomness or the clock read; until you do, no result the engine has ever produced can be trusted.

CI runs a determinism test: the same config twice, asserting identical `run_id`-independent output hashes.

---

## 6. Validation

A single-window backtest is not evidence. It is one draw with one boundary, and the boundary was chosen by someone who had already seen the data. Everything in this section exists because `ARCHITECTURE.md` §10 is right that an automated search over strategy space is a machine for producing overfit results.

### 6.1 Walk-forward

| Scheme | When |
|---|---|
| **Anchored** — training window grows, test window rolls forward | Strategies that accumulate history |
| **Rolling** — fixed-length training window rolls forward | Strategies with a bounded memory |

Choose the scheme from the strategy's **re-fit story**, not from habit. A strategy with fixed parameters wants CPCV (§6.2), which uses the data more efficiently. A strategy that re-fits periodically wants a walk-forward whose step mirrors its real re-fit cadence — otherwise you are validating a strategy that will never exist in production.

Report **out-of-sample decay**: performance as a function of distance between the test window and the end of its training window, expressed as a slope. This predicts forward failure better than the mean does. A strategy at −0.09 Sharpe per 30 days of distance is not wrong, it is *short-lived*: its usable life is measured in weeks and its re-fit cadence must be set accordingly, or it should not be promoted.

### 6.2 Combinatorial purged cross-validation

Standard k-fold on financial time series **leaks by default**. It is not conservative-but-fine; it is actively misleading, and it produces the cleanest-looking wrong answers in the project.

CPCV splits the series into `N` contiguous groups and tests on every combination of `k` groups, training on the rest. With `N=8, k=2` that is `C(8,2) = 28` paths, each yielding a full performance path.

#### Purging

Remove from the **training** set every sample whose label horizon overlaps the test window. A label that resolves inside the test window was partly determined by test-period information; training on it is training on the answer.

#### Embargo

Remove training samples immediately **after** the test window. This is the half people skip, and skipping it is invisible.

The reasoning is not symmetric with purging and is worth stating: serial correlation leaks *backwards*. A training sample beginning shortly after the test window ends is computed from features whose lookback windows still reach into the test period. Without an embargo, the model is trained on data that contains the test period's information, filtered through a feature lookback.

> **Embargo floor = `max_feature_lookback` + `max_holding_horizon`.**

Not a suggestion. A strategy with a 4-hour feature lookback and a 6-hour maximum hold needs at least a 10-hour embargo. Using one bar because "it's crypto, it's fast" reintroduces exactly the leak the embargo exists to close. `DATA_PIPELINE.md` §7 explains why an understated `FeatureSpec.lookback` silently weakens this — the validation layer cannot verify the lookback independently and takes it on trust.

Purge and embargo lengths appear in **three places**: the validation plan, the output schema, and the log line. Three, because this is the number that is silently wrong most often and one place is one place to overlook.

#### Reporting the distribution, not the mean

> **The distribution is the finding.**

A mean Sharpe of 1.1 built from paths ranging −0.9 to 3.0 is a strategy with no stable edge, and the mean hides that completely. Every CPCV result reports:

- `sharpe_mean`, `sharpe_p05`, `sharpe_p95`
- `fraction_of_paths_positive`
- per-path trade counts, so a "28-path summary" where 14 paths had 3 trades each is visible as what it is

A result that is suspiciously *stable* across all paths (p95 − p05 very small) is treated as a **defect signal, not a triumph**. Either the folds are not independent or the same data is in every training set.

#### Every path is a trial

`N=8, k=2` is 28 paths and therefore **28 trials against the global counter, permanently**. Trials are registered as each path completes, not batched at the end — a crashed run must still have consumed its trials, or the trial ledger becomes an instrument for laundering failed searches.

### 6.3 The permanently held-out period

A period of history that no backtest, no validation run and no diagnostic may touch. It is **burned on read**: once looked at, it is no longer out-of-sample and can never be again.

Burning it is the user's decision, taken once, for a strategy that is otherwise ready to promote. No agent and no automated process may burn it. Not for a sanity check, not read-only.

### 6.4 Monte Carlo

Three distinct resamplings, answering three different questions:

| Method | Question |
|---|---|
| **Trade-sequence bootstrap** — resample realised trades with replacement | How much of the equity curve's shape is luck of ordering? |
| **Block bootstrap on returns** (block ≈ max holding horizon) | Does the edge survive when autocorrelation structure is preserved but the specific path is not? |
| **Parameter perturbation** — jitter every parameter ±10% | Is this a plateau or a spike? |

Parameter perturbation is the most informative and the least run. A strategy whose performance collapses under a 10% parameter jitter has found a spike in the fitness landscape, which is the geometric signature of overfitting. A robust strategy sits on a plateau. This test costs almost nothing and rejects a large fraction of candidates.

Block bootstrap uses a block length of at least the maximum holding horizon. An i.i.d. bootstrap destroys the autocorrelation that a momentum or mean-reversion strategy trades, and will report that essentially every such strategy is indistinguishable from noise — a result that is technically true of the resampled series and irrelevant to the real one.

### 6.5 Deflated Sharpe ratio

A raw Sharpe with no trial context is a marketing number. Run enough configurations against fixed history and some will look excellent by chance alone.

The Deflated Sharpe Ratio (Bailey & López de Prado) adjusts the observed Sharpe for the number of trials, the variance of the trial Sharpes, and the non-normality (skew and kurtosis) of the return series, yielding the probability that the observed Sharpe exceeds what the best of `N` random trials would produce.

Inputs the engine must supply honestly:

- **`n_trials`** — the global count for this lineage, from the optimizer's ledger. This includes failed runs, crashed runs, abandoned walk-forward reconfigurations, and every CPCV path. A trial count that only counts successes is a trial count designed to flatter.
- **Skew and kurtosis** of the realised return series. Crypto minute returns are strongly fat-tailed; ignoring kurtosis inflates the deflated figure precisely for the strategies most likely to blow up.

**No Sharpe is ever reported without its trial count.** `BacktestResult` makes `trials_at_time_of_run` a required field so it cannot be dropped by omission.

### 6.6 Probability of backtest overfitting

PBO is the fraction of CPCV path-splits in which the configuration that was best **in sample** underperforms the **median** out of sample. It measures the search process, not the strategy.

- Threshold: **PBO > 0.30 fails validation**, regardless of how good the mean looks.
- High PBO with a good mean is the classic signature of a search that found noise.
- PBO above threshold on a strategy the population depends on indicts the *search*, not just that strategy, and escalates.

### 6.7 Sanity thresholds

On crypto minute bars with realistic costs, these are presumed defective until a leak has been actively searched for and not found:

| Observation | Prior |
|---|---|
| Sharpe > 2.0 | Presumed defective |
| Win rate > 65% | Presumed defective |
| Max drawdown < 5% on a high-Sharpe result | Presumed defective |
| 100% of limit orders filled | Certainly defective |
| Zero venue rejections | Certainly defective |

Do not start by explaining why the result might be real. Start by finding the leak. The audit order below reflects real prior probabilities:

1. **Look-ahead** — most dangerous, highest prior
2. **Cost model error** — including calibration provenance
3. **Timestamp misalignment** — `DATA_PIPELINE.md` §3, trap 1
4. **Fill optimism** — queue position, partial fills, size against depth
5. **Survivorship / selection** — symbol set chosen after knowing which symbols did well
6. **Sample size** — below the minimum trade count, nothing is credible

**Minimum credible sample: 200 trades**, and at least 30 trades in every CPCV test fold. Below that the Sharpe's own standard error swamps the estimate and the result is not evidence either way.

---

## 7. Metrics produced

Reported in this order, always. **Lead with credibility, then economics, then statistics. Never lead with the Sharpe.**

### Credibility

| Metric | Note |
|---|---|
| `credibility` | `credible` / `not_credible` / `unaudited` |
| `audit_findings` | Seven checks, each with evidence that is real command output |
| `cost_model_calibration_source` | Must not contain `testnet` |
| `trade_count` | Below 200 → not credible |
| `rejections_by_reason` | Zero rejections is itself a finding |

### Economics

| Metric | Note |
|---|---|
| `gross_return`, `total_cost`, `net_return` | Always separate |
| `gross_edge_per_trade_bp` | Before costs |
| `round_trip_cost_bp` | Decomposed into fees / spread / slippage / funding |
| `edge_to_cost_ratio` | **< 2.0 rejects** |
| `capacity_notional_usd` | Size at which modelled slippage consumes half the edge |
| `funding_pnl` | Separate from price P&L for perpetuals |

### Risk

| Metric | Note |
|---|---|
| `max_drawdown`, `max_drawdown_duration_days` | Duration is the one that ends strategies in practice |
| `ulcer_index` | Drawdown depth × persistence |
| `var_95`, `cvar_95` | Daily, empirical, not Gaussian |
| `worst_day`, `worst_week` | |
| `risk_limit_breaches` | **Any non-zero is a hard negative** in the survival score |
| `time_in_market_pct` | A strategy in the market 2% of the time has a very different Sharpe interpretation |

### Statistics

| Metric | Note |
|---|---|
| `sharpe`, `sortino`, `calmar` | |
| `trials_at_time_of_run` | Required, not optional |
| `deflated_sharpe` | The one that counts |
| `pbo` | > 0.30 fails |
| `sharpe_p05`, `sharpe_p95`, `fraction_of_paths_positive` | Distribution over CPCV paths |
| `oos_decay_slope` | Sharpe per 30 days of distance from the fit window |

### Regime breakdown

Every metric above is additionally reported per regime (trend / chop, high-vol / low-vol, funding-positive / funding-negative). A strategy with an aggregate Sharpe of 1.2 that is 3.0 in one regime and −0.4 in another is a regime bet wearing a strategy's clothes, and the aggregate conceals that completely.

---

## 8. The tearsheet

One HTML artefact per run, self-contained, written to `reports/backtest/<run_id>/tearsheet.html`, and reproducible from `run_id` alone.

Layout, top to bottom — deliberately ordered so that the numbers most likely to invalidate the result appear before the numbers most likely to excite:

1. **Header** — `run_id`, `config_hash`, strategy id and version, window, cost model version **and its calibration source**, `trials_at_time_of_run`, engine git SHA.
2. **Credibility banner** — a full-width band, green or red. A `not_credible` run renders red with the failing checks listed and **the equity curve suppressed**. You cannot look at the pretty picture of a result that has already failed an audit; anchoring is real and the suppression is deliberate.
3. **Audit findings table** — seven checks with pass/fail/inconclusive and the evidence.
4. **Economics** — gross / cost / net waterfall, cost decomposed into fees, spread, slippage, funding, and the `edge_to_cost_ratio` against its 2.0 line.
5. **Equity curve** — with drawdown underneath on a shared time axis, and the CPCV path envelope (p05–p95) shaded behind it. The envelope is the point: a single equity curve invites belief, and the same curve inside a band spanning −0.9 to 3.0 Sharpe does not.
6. **Per-regime breakdown.**
7. **Trade distribution** — P&L histogram, holding-period histogram, entry-hour histogram. The entry-hour histogram is where funding-window concentration becomes visible.
8. **Validation** — CPCV path table, PBO, decay slope, Monte Carlo distributions, parameter-perturbation surface.
9. **Rejections and unfilled orders** — count by reason. Capacity limits show up here first.
10. **Provenance footer** — data coverage per symbol with gap ranges, feature versions used, exact parameter set as `Decimal` strings, held-out period status.

The tearsheet is generated by the same code path in every environment and is checked into the run's artefact directory, not regenerated on demand. A tearsheet that regenerates against current code is a document about today, not about the run.

---

## 9. Failure handling

| Failure | Response |
|---|---|
| Engine crash mid-run | The trial still counts. Record the failed run in the ledger with the traceback |
| Missing bars in the window | Do not interpolate. Report coverage; narrow the window or refuse |
| Same `config_hash`, different result | Determinism failure. Outranks everything |
| `edge_to_cost_ratio` infinite or negative | The cost model did not run. Void the result |
| A CPCV path crashes | Record as a consumed trial with the error; never silently reduce `path_count` |
| Insufficient trades in a test fold | Mark `insufficient`, exclude from statistics, **report how many were excluded** |
| Purge/embargo produces overlapping train and test ranges | Hard failure. Do not clamp and continue |
| Backtest and paper diverge beyond the cost model's error bars | Parity failure. Escalate immediately |
| Result is credible *and* the edge is unusually large | Escalate rather than celebrate. Large clean edges here have so far always been leaks |

---

## 10. Cross-references

| For | See |
|---|---|
| Why parity is the load-bearing architectural property | `ARCHITECTURE.md` §4 |
| Why risk sits structurally in the order path | `RISK_PHILOSOPHY.md` |
| Feature lookback, gaps, and point-in-time guarantees | `DATA_PIPELINE.md` §7 |
| Why testnet statistics are void | `DATA_PIPELINE.md` §2 |
| The survival score and why it is not profit | `SURVIVAL_PROTOCOL.md`, `SCORING_ENGINE.md` |
| Trial ledger, promotion gates, champion/challenger | `EVOLUTION_ENGINE.md` |
| Backtest configuration surface | `CONFIGURATION.md` §7 |
| Why the engine is custom rather than NautilusTrader | `docs/adr/0005` |
