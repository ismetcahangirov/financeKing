# Evolution Engine

How strategies are born, promoted, demoted and killed — and the defences that stop the whole apparatus from producing confident nonsense.

`SCORING_ENGINE.md` defines the objective this engine optimises. `SURVIVAL_PROTOCOL.md` explains why that objective is not profit. Read §4 of this document before writing any mutation code.

---

## 1. The lifecycle

```
                    ┌──────────┐
                    │ proposed │
                    └────┬─────┘
                         │ contract gate
                         ▼
                   ┌────────────┐
                   │ backtested │◄──────────┐
                   └────┬───────┘           │
                        │ CPCV gate         │ (never; see §8)
                        ▼                   │
                   ┌───────────┐            │
                   │ validated │            │
                   └────┬──────┘            │
                        │ diversity + slot  │
                        ▼                   │
                   ┌────────┐               │
                   │ paper  │───────────────┤
                   └────┬───┘               │
                        │ forward gate      │
                        ▼                   │
                  ┌────────────┐            │
                  │ challenger │────────────┤
                  └────┬───────┘            │
                       │ beats champion     │
                       ▼                    │
                  ┌──────────┐              │
                  │ champion │──────────────┤
                  └──────────┘              │
                                            │
                   ┌─────────┐              │
                   │ retired │◄─────────────┘
                   └─────────┘   terminal
```

Every state except `retired` can transition to `retired`. Nothing transitions *out* of `retired`. Transitions are append-only rows in `strategy_lifecycle_events` with the score, the sample counts, the trial index and the reason.

### The states

| State | Capital | Data seen | What it means |
|---|---|---|---|
| `proposed` | none | none | A genome exists. It may not compile. |
| `backtested` | none | in-sample history | It ran. The number means very little. |
| `validated` | none | CPCV paths | It survived combinatorial purged cross-validation with deflation. |
| `paper` | notional only | live data, simulated fills | Real-time, real data, no venue interaction. |
| `challenger` | small real allocation | live data, demo fills | Trading on the demo venue in parallel with the champion. |
| `champion` | full allocation | live data, demo fills | Currently the best answer we have for its niche. |
| `retired` | none | — | Terminal. See §8. |

---

## 2. Transition conditions

Every threshold below is a configuration value with a compiled ceiling, in the same pattern as risk limits (`RISK_PHILOSOPHY.md` §9): config can only make the gates *stricter*.

### `proposed → backtested` — the contract gate

Deterministic checks, all mandatory, none score-based:

1. Imports and instantiates; type-checks under `mypy --strict`.
2. `strategy` has no import path to `execution` (structural, already enforced, re-checked per genome because genomes are generated).
3. Emits `Signal`, never constructs an order.
4. Pure: no I/O, no `datetime.now()`, no unseeded randomness. Verified by running the same bar sequence twice under different wall-clock times and asserting identical output.
5. Declares `invalidation` on ≥ 95% of non-flat signals.
6. Every feature it requests is declared available by the feature store (`ARCHITECTURE.md` §6). No silent substitution.
7. Passes the adversarial look-ahead test: the harness feeds it a series in which future bars are replaced with poison values, and the strategy's output must be bit-identical. If it changes, it read the future.
8. States a falsifiable thesis in `rationale`. For agent-authored genomes, the rationale must differ from the parent's when the logic differs — an unchanged rationale on changed logic is rejected. Cheap check; catches the most common LLM failure, which is mutating code and copying the justification.

Failure at this gate is not a low score. It is `retired` with reason class `defect`, immediately, and the genome is tombstoned.

### `backtested → validated` — the CPCV gate

| Requirement | Threshold |
|---|---|
| Deflated Sharpe (`SCORING_ENGINE.md` §2.1) | ≥ 0.95 |
| Trades aggregated across CPCV paths | ≥ 200 (effective), and ≥ 30 in every test fold |
| Median across CPCV path Sharpes | > 0 |
| Fraction of CPCV paths with positive Sharpe | ≥ 0.60 |
| `edge_multiple` (`c_edge` input) | ≥ 1.5 |
| Estimated capacity `Q*` | ≥ $10,000 and ≥ 10× venue minimum notional |
| Risk violations during backtest | 0 hard, 0 limit |
| `suspicious_oos` flag | clear, or cleared by the leak battery |

The *fraction of paths* requirement matters as much as the median. CPCV produces a distribution of Sharpes, and the distribution's shape is the actual result. A mean Sharpe of 1.1 assembled from paths spanning −0.9 to 3.0 is a strategy that works in one part of history, and the mean conceals that completely. Reporting only the mean or median throws away the single most useful thing CPCV gives you.

The inverse is also a rejection criterion, and it is the one nobody expects: a distribution that is *too tight* is a defect signal. If `p95 − p05` is very small across 28 paths, the folds are not independent — the same data is reaching every training set, which means purging or embargo is misconfigured. Suspiciously stable validation is a bug report, not a result.

### `validated → paper`

- Behavioural correlation to every existing `champion` and `challenger`: `|ρ| < 0.60` on daily attributed returns over the overlapping validation window.
- A paper slot is free (population caps in §7).
- The held-out vault has **not** been touched by this genome (§5.3).

### `paper → challenger` — the forward gate

| Requirement | Threshold |
|---|---|
| Forward trading days in paper | ≥ 30 |
| Forward closed trades | ≥ 60 |
| Forward survival score | ≥ 0.5 × validation survival score |
| Forward Sharpe | inside the 95% CI predicted by validation |
| Risk violations in paper | 0 |
| Slippage vs decision price | within 2× modelled |

The Sharpe-inside-CI condition is two-sided and that is deliberate. A strategy validated at Sharpe 1.0 that delivers 3.5 in paper fails this gate exactly as a strategy delivering −1.0 does. Massive forward outperformance means the validation was not modelling the same process — usually a cost model that is too pessimistic, a fill model that is too pessimistic, or paper trading that is quietly getting fills a real venue would not give. All three are bugs, and all three will reverse when the strategy reaches the demo venue.

The final row is the one that catches paper-trading self-deception. If realised slippage exceeds twice the model, the strategy is not being evaluated under conditions resembling execution.

### `challenger → champion`

The challenger must beat the incumbent by more than the joint noise:

```
S_challenger − S_champion  >  1.65 · √( SE(S_ch)² + SE(S_cp)² )
```

measured over ≥ 90 days of **concurrent** operation with ≥ 100 forward trades each. Concurrency is not optional: comparing a challenger's last 90 days to a champion's record from a different period compares two market regimes, not two strategies.

The 1.65 (one-sided 95%) is the incumbency margin. Ties go to the incumbent, because a promotion has costs the score does not see — transition slippage, a fresh unmeasured strategy at full allocation, and the loss of a track record that has already been paid for.

Immediately before the promotion is committed, and only then, the challenger gets its single held-out vault evaluation (§5.3). The vault can veto. It cannot promote.

### `* → retired`

Any of:

- Contract gate failure (`defect`)
- Strategy drawdown limit breach (`risk`)
- ≥ 2 hard violations or ≥ 4 limit violations in 12 months (`risk`)
- Survival score below the retention floor (0.30) with sufficient sample for 2 consecutive evaluation cycles (`decay`)
- Projected Sharpe from the alpha half-life fit falls below the retention floor within one cycle (`decay`)
- Beaten and replaced by a descendant (`superseded`)
- Its declared thesis is contradicted by a structural market change the research pipeline has confirmed (`environmental`)

---

## 3. Champion and challenger

There is not one champion. There is one champion **per niche**, where a niche is a behavioural cluster (§7). This matters: a single global champion collapses the population onto whatever works right now, and the population's job is to have something ready when "right now" ends.

Challengers run at reduced allocation — 25% of a champion's risk budget — for the duration of the comparison. This is the cost of the evaluation and it is deliberately non-zero: a challenger evaluated at zero allocation is a paper strategy with a different label, and the entire point of the challenger state is to measure behaviour under real venue interaction (partial fills, rejects, queue position, funding).

A demoted champion goes to `retired`, not back to `challenger`. Demotion means the evidence that promoted it has been superseded; keeping it in the pool means the search keeps re-examining a hypothesis it has already tested.

---

## 4. The central problem

**An automated search over strategy space is a machine for producing overfit results.** This is not a risk to be managed. It is the default output, and every other feature in this module exists to prevent it.

The mechanism is arithmetic. Run `K` configurations against fixed history. Under the null hypothesis that none has any edge, the best observed Sharpe is approximately `√(2 ln K) · SE(ŜR)`. With three years of daily data (`N ≈ 750`, `SE ≈ √(252/750) ≈ 0.58` annualised) and `K = 10,000` trials:

```
best-of-noise annualised Sharpe ≈ √(2 · ln 10000) · 0.58 ≈ 4.29 · 0.58 ≈ 2.5
```

A Sharpe of 2.5 from three years of data. Generated by strategies with **zero** edge. A researcher shown that result without the trial count will believe it, and every backtesting library on earth will print it without mentioning `K`.

The evolution engine will run far more than 10,000 trials. It runs them continuously, across restarts, across sessions, across months. Without correction, its top-ranked strategy is a coin-flipping champion, and the more effort we spend searching, the more confident and the more wrong the result.

Three properties of this problem make it worse than ordinary overfitting:

- **Effort makes it worse.** In most engineering, more search is better. Here, more search raises the noise floor. A search that runs overnight produces a *worse* answer than one that runs for an hour, unless the trial count is carried into the significance test.
- **It is invisible in-sample.** There is no in-sample diagnostic that distinguishes the overfit winner from a real edge. The winner looks exactly like what you were hoping to find, because it was selected for looking like it.
- **It compounds through the generations.** Selecting parents on an inflated statistic and breeding from them concentrates the population on the noise that happened to be selected. Generation 20 is not exploring strategy space; it is exploring the neighbourhood of generation 1's luckiest accident.

---

## 5. The defences, in priority order

Ordered by how much damage their absence causes. If you have to cut something, cut from the bottom.

### 5.1 Global trial counting — the one that cannot be skipped

`K` feeds `SR*` in the deflated Sharpe (`SCORING_ENGINE.md` §2.1). Everything else in this list is a refinement; this one is the difference between a significance test and a number.

**Scope.** The ledger is keyed by search context — `hash(symbol_universe, date_range, feature_set)` — because trials against different data do not contaminate each other. Within a context, two counts are maintained: the **global** figure, which is what feeds `SR*`, and a **per-lineage** figure, which feeds the additional family deflation in §5.6. The global figure is the primary one; a system that deflates only by lineage is treating each family as if it were the only search ever run.

**What counts as a trial:** every distinct strategy configuration evaluated against the context. That includes:

- every genome the evolution loop backtests, including ones discarded immediately
- every online refit of an adaptive strategy's parameters
- every parameter configuration in a sweep, individually
- **every ad-hoc `make backtest` run by a human**
- every re-run after a code change that could alter results

A CPCV evaluation counts **one trial per path**, registered as each path completes rather than batched at the end (`BACKTEST_ENGINE.md` §6.2). This is the conservative reading and it is the right one: a crashed or abandoned run must still have consumed its trials, or the ledger becomes a mechanism for laundering failed searches — run 28 paths, keep the good ones, report the count of what you kept.

**How it is enforced structurally.** `BacktestEngine.run()` refuses to execute any `spec_hash` that was not registered beforehand, and reports every execution it does perform to the ledger. There is no code path that produces a backtest result outside the ledger's view, in exactly the way there is no code path that constructs an `Order` without the risk engine. This is the same architectural move for the same reason: a counter the caller is responsible for maintaining is a counter that will be wrong.

The charge itself is `max(declared_grid_size, actual_executions)` per specification — the declaration prices optional stopping, and the execution report prices a grid that grew past what was declared. Neither number alone closes both evasions. The authoritative division of responsibility between `quant` (registration), `optimizer` (ledger mechanics) and the engine (enforcement) is in `docs/rules/overfitting-defences.md`, section "Where the charge happens".

**Persistence.** `K` lives in Postgres, is monotonic, and survives restarts, redeploys and database migrations. It is never reset. A reset would be indistinguishable from a claim that the previous six months of searching never happened.

The most common real-world failure here is counting trials *within a generation* and resetting between generations. That gives `K ≈ 200` when the true figure is `K ≈ 50,000`, and the difference in `SR*` is roughly `√(ln 50000 / ln 200) = √(10.8/5.3) = 1.43×` — the bar is 43% too low, permanently, for everything.

Note the flip side, which is why this discipline erodes: `SR*` grows as `√(2 ln K)`, so a 100× undercount only understates the bar by 41%. It never produces a dramatic, obviously-wrong number. It produces a slightly too-generous bar, forever, across the whole population. Slow, invisible, cumulative — which is the profile of every defect that actually kills a system.

### 5.2 Combinatorial purged cross-validation

A single train/test split is one path through history and therefore one draw from the distribution of possible results. It tells you nothing about variance, which is the quantity you actually need.

The scheme is `N = 8` contiguous groups with `k = 2` held out, giving `C(8,2) = 28` paths (`BACKTEST_ENGINE.md` §6.2 is authoritative on the mechanics). The output is a *distribution* of 28 Sharpes, not a number.

**Purging** removes from the training set any observation whose label horizon overlaps the test set. Without it, a strategy with a 5-day holding period trains on observations whose outcomes are determined by test-set price action — a leak that looks like nothing and inflates results substantially.

**Embargo** additionally drops observations immediately after each test block. The floor is `max_feature_lookback + max_holding_horizon`; the engine applies `1.5×` that floor, the margin being for horizon estimates that are wrong, which they usually are for strategies with state-dependent exits.

The evolution engine's specific interest here is that **the embargo length is a function of the genome**, so a mutation that lengthens a horizon or swaps in a longer-lookback feature silently changes the validation geometry. Horizon and feature-swap mutations therefore trigger an embargo recomputation, and a mutant whose recomputed embargo would leave any CPCV fold below its minimum trade count is rejected before evaluation rather than producing a technically-valid result on three usable folds.

### 5.3 The held-out vault

The most recent 12 months of data are sealed. Physically separated, SHA-256 manifested, reachable only through `fking.data.vault`, which logs every access with the requesting genome hash and increments a burn counter.

Three rules:

1. **One access per genome, ever.** Granted only immediately before a `challenger → champion` promotion.
2. **The vault can veto, never promote.** A strategy that performs badly on the vault is retired. A strategy that performs well on the vault gains nothing — the promotion decision was already made on the concurrent-forward comparison, and the vault only removes.
3. **Touched is burned.** Once a genome has been evaluated against the vault, that evaluation is final for that genome. There is no re-run after a fix. A fixed strategy is a new genome with a new hash and it does not get a second vault access on the same window.

Rule 2 is the non-obvious one and it is what makes the vault work. A held-out set that can *promote* is a selection criterion, and any selection criterion gets optimised against as soon as someone iterates on failures. Making it veto-only removes the gradient: there is no way to tune toward passing a test that gives you nothing for passing, so the only way to pass is to actually be robust.

Rule 3 is the one people try to negotiate. The answer is that the vault's value is entirely in its untouched-ness, and untouched-ness is not a renewable resource. You cannot buy more held-out data. You can only wait for calendar time to produce it, which is precisely the property that makes it informative and precisely why it must not be spent on iteration.

When the population epoch advances, a new vault window is sealed from the data accumulated since the last seal. If insufficient new data has accumulated, promotions to champion wait. Waiting is an acceptable outcome; burning the vault to avoid waiting is not.

### 5.4 Champion/challenger requiring forward performance

Everything above is a statistical correction applied to historical data. This is the only defence that uses information the search could not have seen at any price, because it did not exist yet.

It is fourth on the list rather than first only because it is slow — 90 days per comparison — and because by the time it fires, the cheaper defences should already have rejected the candidate. But it is the *final* authority. If a strategy clears every statistical gate and then fails forward, it fails.

### 5.5 Minimum sample sizes

See `SURVIVAL_PROTOCOL.md` §10. The engine returns `INSUFFICIENT_SAMPLE`, never a low score, and no gate accepts it.

### 5.6 Lineage tracking

Every genome records: parent hashes, generation number, the trial index at creation, the mutation operators applied, and the `scoring_version` in force.

Three uses:

- **Defect propagation.** When a look-ahead leak is found in a genome, every descendant is quarantined and re-tested automatically. Without lineage, you fix one strategy and leave nine of its children in production carrying the same bug.
- **Detecting lineage collapse.** Behavioural correlation is estimated over a finite window and can look healthy while the population is genealogically inbred. If more than 50% of live strategies share a common ancestor within 5 generations, diversity pressure is escalated and the proposal pipeline is forced toward the novelty archive (§7), regardless of what the measured correlations say.
- **Family-adjusted trials.** A genome that is the 40th mutation of the same parent is not an independent hypothesis. Its family trial count feeds an additional deflation term.

---

## 6. Mutation operators

Applied to a genome represented as a typed expression tree over declared features plus a numeric parameter vector.

| Operator | Rate | Detail |
|---|---|---|
| Parameter jitter | 0.40 | Multiplicative log-normal, `σ = 0.15`. Preserves sign and order of magnitude. |
| Parameter reset | 0.10 | Redraw from the parameter's declared prior. Escapes local optima. |
| Feature swap | 0.15 | Replace a feature node with another of the same type from the availability-declared set. |
| Operator swap | 0.10 | Swap a comparison, logical or aggregation node for a type-compatible sibling. |
| Rule deletion | 0.10 | Remove a subtree, reconnecting the parent to a constant or a sibling. |
| Rule addition | 0.08 | Insert a new type-valid subtree. |
| Horizon change | 0.05 | Adjust the declared holding horizon. Triggers embargo recomputation. |
| Invalidation change | 0.02 | Change the invalidation rule. Lowest rate deliberately — this is the strategy's falsifiability contract. |

**Deletion rate exceeds addition rate, and that is the point.** Absent selection pressure, the population drifts toward simplicity. Genetic-programming populations bloat: complexity accumulates because a slightly more complex genome fits the sample slightly better essentially always, and the fit is noise. An explicit asymmetric ratchet toward deletion is cheaper and more reliable than a parsimony penalty in the objective, because a penalty is a weight to be tuned and a ratchet is a structural bias that cannot be tuned away by accident.

Every mutant re-enters at `proposed` and must clear the full contract gate. A mutation that produces a genome violating the invalidation contract is discarded without consuming a trial — the trial counter increments on *evaluation*, not on generation.

Parameter priors are declared per parameter with units and bounds, and the bounds are economic, not numerical. A lookback window's prior is `LogUniform(4 bars, 500 bars)`, not `Uniform(1, 10000)`, because a 3-bar and a 7000-bar lookback are not strategies we have any reason to test and testing them burns trials that raise the bar for everything else. **Every trial you spend on a hypothesis you do not believe makes it harder to confirm the ones you do.** That is the strongest practical argument for tight priors and it is not the usual one.

---

## 7. Crossover and diversity pressure

### Crossover

Two operators, applied to tournament-selected parents (tournament size 3, on *shared* fitness):

- **Subtree crossover** with type constraints — a boolean node may only be exchanged with a boolean node. Untyped crossover produces mostly-invalid offspring and wastes the contract gate's time.
- **Blend crossover (BLX-α, α = 0.5)** on numeric parameters that both parents share, sampling uniformly from `[min − α·d, max + α·d]` where `d = |p₁ − p₂|`. The extension beyond the parents' range is what lets a population escape the convex hull of its founders.

Offspring inherit both parents' lineage and count as new trials.

### Diversity pressure

Fitness sharing. A strategy's effective fitness is divided by the crowding in its neighbourhood:

```
S_shared = S / ( 1 + Σ_{j≠i} sh(d_ij) )

d_ij  = 1 − |ρ_ij|                       ρ on daily attributed returns
sh(d) = 1 − d/σ_share   if d < σ_share,  else 0
σ_share = 0.40                            i.e. sharing kicks in above |ρ| = 0.60
```

Plus a hard constraint: no two strategies with `|ρ| > 0.85` may both hold `champion` status.

**Behavioural, not genotypic.** Diversity is measured on what strategies *do* — return correlation, trade-timing overlap, holding-period distribution, regime participation — not on how different their code is. Two genotypically unrelated genomes can produce nearly identical trade sequences, and a diversity metric based on tree edit distance will happily report a diverse population that is in fact one strategy expressed six ways. The risk engine will then refuse to fund five of them (`RISK_PHILOSOPHY.md` §5) while the scoring engine keeps rewarding all six, and the population quietly stops working while every dashboard stays green.

A **novelty archive** stores the behaviour descriptor of every strategy that ever reached `validated`. New proposals whose descriptor falls within `ε` of an archived one are rejected before evaluation, without burning a trial. The archive is the cheapest defence in the entire engine per unit of trial saved.

Population caps: 12 `paper`, 6 `challenger`, 6 `champion` (one per behavioural niche). Caps exist because attention and allocation are finite, and an uncapped population converges on the current regime by sheer numbers.

---

## 8. Retirement is permanent

`retired` has no outgoing edges. There is no `reactivate`, no `unretire`, no "give it another chance now that conditions changed".

Every retirement carries a reason class, and the class determines what happens to the lineage:

| Class | Trigger | Lineage consequence |
|---|---|---|
| `defect` | Contract gate failure, look-ahead leak, non-determinism | All descendants quarantined and re-tested |
| `risk` | Drawdown breach, accumulated violations | Descendants flagged; risk-relevant genes recorded |
| `decay` | Score below floor with sufficient sample, or projected below floor | Descendants unaffected |
| `superseded` | Beaten by a descendant | Descendant carries the lineage forward |
| `environmental` | Confirmed structural market change invalidating the thesis | Genome tombstoned as *revisitable* |

### Tombstones

Every retired genome's hash and behaviour descriptor go into a tombstone table. **Mutation and crossover are forbidden from producing a genome within `ε` of a tombstone**, except for `environmental` retirements, which may be revisited only through an explicit human-initiated proposal that starts at `proposed` and increments `K` like anything else.

Without tombstones the search rediscovers the same dead strategy every few generations. Each rediscovery costs a full evaluation, burns trials, raises `SR*` for the entire population, and produces exactly zero new information — the hypothesis was already tested. This is the most expensive avoidable failure in an evolutionary search and it is invisible unless you are specifically looking for it, because from the loop's perspective each rediscovery is a novel candidate.

### Why permanence, specifically

Three reasons, in increasing order of importance:

1. **Comparability.** A resurrected strategy's historical score was computed under a different `scoring_version`, a different trial count, and different data. It is not comparable to current scores and would have to be re-earned anyway.
2. **Oscillation.** A strategy near the retention floor with noisy scores would cycle in and out on sampling noise, consuming a slot and an allocation each time, and each cycle would look like a decision.
3. **Trial economics.** Re-testing a hypothesis you already rejected does not give you a second independent look. It gives you a second correlated look, which the deflation term treats as a new trial — you pay the full statistical cost and get almost none of the information. Repeated resurrection is the fastest way to inflate `K` while learning nothing, and inflating `K` makes every *genuine* discovery harder to confirm.

The third reason is why retirement is permanent even when it is probably wrong about a specific strategy. Some genuinely good strategies will be retired by noise. That cost is real and it is smaller than the cost of a search that spends its trials revisiting its own past.

---

## 9. What the engine cannot do

Stated plainly so nobody expects otherwise:

- It cannot find an edge that is not in the feature set. Mutation recombines; it does not discover new data. New edges come from the research pipeline and the data platform, not from here.
- It cannot distinguish a real edge from an overfit one within a single generation. Only forward time does that, which is why §5.4 exists and why the cycle is slow by design.
- It cannot compensate for a lying scoring engine. If `SCORING_ENGINE.md` §6 fires, this engine is amplifying an error at full speed and must be stopped, not tuned. Promotions freeze; the search does not get to keep running "just to collect data", because every trial it burns while the objective is wrong makes the recovery harder.
