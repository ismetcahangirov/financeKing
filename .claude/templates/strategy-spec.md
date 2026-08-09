# Template — Strategy Specification

Copy this file to `docs/strategies/<strategy-id>-<kebab-slug>.md`, where `<strategy-id>` is the identifier the strategy will carry in the registry and its lineage records. Example: `docs/strategies/s-0042-funding-residual-reversal.md`.

**Read this before you write anything: the section order below is load-bearing and you must fill the sections in the order given.**

The falsifiable thesis comes first. The invalidation rule comes second. The entry rule is not permitted to appear until both are written and both are specific. This is not a formatting preference — it is the only mechanical defence this project has against a common and expensive failure. An author who cannot state what would prove the strategy wrong does not yet have a strategy, and writing the entry rule first is how a hope gets dressed as a thesis: once a rule exists, the thesis gets written backwards to justify it, and the invalidation gets set wherever it happens not to trigger. Sections 1 and 2 must be readable as a standalone claim about the world with no reference to any rule.

If you cannot complete section 1 with a sign, a horizon and a magnitude, stop. Go back to `../workflows/research.md`. There is nothing here to specify yet.

Related: `../../docs/rules/no-lookahead.md`, `../../docs/rules/overfitting-defences.md`, `../contexts/crypto-perpetuals.md`, `../contexts/backtest-pitfalls.md`, `ARCHITECTURE.md` §5, `SURVIVAL_PROTOCOL.md`.

---

```yaml
---
id: <s-NNNN, matching the filename>
name: <human-readable name, title case>
author: <agent name, e.g. strategy-generator, or a human username>
date: <yyyy-mm-dd>
lineage:
  parent: <parent strategy id, or null if founder generation>
  mutation: <what was changed from the parent in one clause, or null>
  generation: <integer, 0 for founder>
status: <specified | backtested | challenger | champion | retired>
hypothesis_correlation_id: <the correlation_id of the HypothesisRegistration this is built from>
trials_charged: <integer — the trial charge already booked for the founding hypothesis>
global_trials_at_spec: <the global trial counter at the moment this spec was written>
---
```

---

## 1. Falsifiable thesis

*One sentence, carrying a sign, a horizon and a magnitude, then a separate paragraph giving the economic mechanism. "Funding predicts returns" is not a thesis; it has no sign, no horizon and no magnitude, and it cannot fail. The mechanism paragraph must answer who is on the other side of this trade and why they keep taking it — a counterparty who is losing and knows it will stop, so a mechanism that names no willing loser is describing an effect that has already been competed away or never existed.*

```
Thesis: <instrument set> <input condition> precedes <sign> excess returns of at least
        <magnitude in bp, net of costs> over <horizon>.

Mechanism: <who is on the other side, what they are getting in exchange, and why that
           exchange is rational for them and therefore persistent>
```

> Example: In the top-8 USDT perpetuals, funding residual in its top 5 percent precedes negative perp excess returns of at least 8bp net of costs over the following 24-72 hours. The counterparty is a basis arbitrageur who is short the perp and long spot; they are not losing, they are collecting the carry, which is exactly why the effect persists rather than being arbitraged flat. What we are trading against is the leveraged directional demand that pays that carry and eventually cannot.

---

## 2. Invalidation

*Three distinct things, all required. Do not merge them. The first is a price. The second is the value that goes into the `Signal`. The third retires the strategy at population level and is not about any single trade.*

**Thesis invalidation (price or condition).** *The specific level or state at which the claim in section 1 is wrong — not the level at which the trade is uncomfortable. If price reaching this level would leave you still believing the thesis, it is a stop, not an invalidation, and you have not written this section.*

```
<condition, expressed in market terms, with the reasoning for the level>
```

**The `invalidation` field the `Signal` carries.** *A `Decimal` price, or `None` with an explicit written reason. Show how it is computed from market state at signal time. `None` is permitted only for a flat signal.*

```python
invalidation: Decimal | None = <expression over point-in-time features, as Decimal>
```

**Population-level kill criterion.** *The observable condition on the live or forward record that retires this strategy permanently. It must include a sample size, so it cannot fire on noise and cannot be argued away. This is the criterion `evolution` applies without asking anyone.*

```
Retire when: <metric> <comparator> <value> over <minimum sample>, evaluated <cadence>
```

> Example: Thesis invalidation — mark price 1.5x the 30d realised daily sigma beyond entry against the signal direction, because a move that large inside the funding horizon means the deleveraging the thesis predicts is not happening and something else is driving the tape. Kill criterion — rolling 60-trade net edge below 0 bp for two consecutive 60-trade windows, evaluated weekly.

---

## 3. Evidence

*Numbers from the `HypothesisResult`, copied, not recomputed. If any row is unknown, the strategy is not ready to be specified. A bare Sharpe with no deflated twin and no trial count is a defect (`../agents/quant.md`).*

| Metric | Value | Source |
|---|---|---|
| Hypothesis registration | `<artifacts/agents/quant/registered/<correlation_id>.json>` | quant |
| Observed Sharpe | `<value>` | quant |
| **Deflated Sharpe** | `<value>` | quant |
| **Global trial count at test** | `<integer>` | trial ledger |
| `n_observations` | `<integer>` | quant |
| **`n_independent_episodes`** | `<integer>` | quant |
| Net edge (bp, production-calibrated costs) | `<value>` | quant + market-research |
| CPCV fold sign consistency | `<value in 0..1>` | quant |
| Verdict | `<supported>` | quant |

*State in one line why this evidence is sufficient given the trial count, and one line naming the weakest number in the table.*

```
Sufficient because: <reasoning that references the global trial count>
Weakest number:     <which row, and what would strengthen it>
```

---

## 4. Data requirements

*Every feature, its earliest clean date, and the availability verdict from the feature store. The strategy inherits the shortest history in this table — state that date explicitly. Free full-depth L2 order book history does not exist; if a row here needs queue position or resting-liquidity dynamics, the verdict is unavailable and the strategy cannot be built.*

| Feature id | Granularity | Earliest clean date | Availability verdict |
|---|---|---|---|
| `<feature_id>` | `<1m / 8h / trade-level>` | `<yyyy-mm-dd>` | `<available / unavailable>` |

```
Effective start date (shortest history in the table): <yyyy-mm-dd>
Point-in-time confirmation: <how available_at is known for each feature, not just event_time>
```

---

## 5. Entry rule

*Only now. State the rule as a deterministic function of point-in-time features with no clock access and no I/O. Every constant gets a provenance comment saying where the number came from — a constant with no provenance in strategy code gets tuned by someone later who does not know what it encodes.*

```python
# entry condition, evaluated on each bar close
<condition expressed over feature values, with Decimal literals constructed from str>
```

```
Constants and their provenance:
- <name> = <value>  # <where this number came from; a mechanism, a registration, an ADR>
```

---

## 6. Exit rule

*Cover all four exit paths explicitly. A strategy with no time-based exit holds a position forever when the thesis quietly stops applying.*

```
Thesis realised:  <condition>
Thesis invalid:   <references section 2>
Time expiry:      <the horizon from section 1, stated as a timedelta>
Signal flip:      <what happens when the strategy would signal the opposite direction while in position>
```

---

## 7. Signal contract

*The exact `Signal` this strategy emits, as real Python that would typecheck under `mypy --strict`. `conviction` is a `Decimal` between 0 and 1 expressing belief, never a size and never a notional — the risk engine turns conviction into quantity and it is the only thing that may. `horizon` is a `timedelta`. `rationale` is written for a human reading an audit trail in a year, so it names the condition that fired and its value.*

```python
Signal(
    direction=<"long" | "short" | "flat">,
    conviction=Decimal("<0..1>"),          # belief, not size
    horizon=timedelta(hours=<n>),
    invalidation=Decimal("<price>"),        # from section 2
    rationale=f"<condition name>={<value>:.4f} beyond <threshold>; <what that means>",
)
```

*State the conviction mapping explicitly: what market state produces 0.2 and what produces 0.9, and why the mapping is not simply a rescaled signal strength if it is not.*

```
conviction = <function of feature state>   # <justification for the shape>
```

---

## 8. What this strategy must never do

*Restate the boundaries in terms specific to this strategy. Generic restatements of the rules are useless here; say what the temptation would look like in this particular code.*

- **Size itself.** It emits conviction. It never computes a quantity, a notional, or a leverage figure. <Name the place in this strategy where sizing would be tempting.>
- **Touch execution.** No import path from `fking.strategy` to `fking.execution`, enforced by `import-linter`. It does not know about order types, fees at order level, or venue state.
- **Read the clock.** No `datetime.now()`. The evaluation timestamp arrives as a parameter, or the strategy is neither replayable nor evolvable.
- **Reach for data outside the section 4 table.** <Name the adjacent feature this strategy would most plausibly try to sneak in, and why it is not in the table.>
- **Carry mutable state between evaluations** beyond what is passed in explicitly.

---

## 9. Cost and capacity assumptions

*Production-calibrated, never testnet. Binance futures testnet shows roughly a 7.5bp spread against production's 0.16bp and about 10x inflated volume; a cost model calibrated there is fiction that flatters every strategy uniformly. Cite the `market-research` artefact the numbers came from.*

| Assumption | Value | Source artefact |
|---|---|---|
| Fee tier (maker / taker) | `<bp / bp>` | `<artefact id>` |
| Assumed fill type | `<taker / maker>` | `<artefact id>` |
| Half-spread at signal time | `<bp>` | `<artefact id>` |
| Slippage model | `<function of participation rate>` | `<artefact id>` |
| Round-trip cost | `<bp>` | derived |
| Capacity ceiling | `<notional_usd>` before edge decays below `<bp>` | `<artefact id>` |

```
Why a maker assumption is or is not available here: <one or two sentences>
```

---

## 10. Regime dependence

*Name the regimes where the mechanism should hold and where it should break, before looking at per-regime results. A mechanism that predicts nothing about regimes is not a mechanism.*

| Regime | Expected behaviour | Reason from the mechanism |
|---|---|---|
| `<high realised vol>` | `<stronger / weaker / absent>` | `<why>` |
| `<trending>` | `<stronger / weaker / absent>` | `<why>` |
| `<low funding dispersion>` | `<stronger / weaker / absent>` | `<why>` |

```
Regime in which this strategy must be flat, and how that is detected: <condition>
```

---

## 11. Backtest results and validation design

*The design first, the numbers second, in that order, because the design is what makes the numbers mean anything. A single train/test split is not evidence.*

```
Validation design:
  CPCV groups / test groups per split / total splits: <n / n / n>
  Purge:   <duration, sized to the label horizon>
  Embargo: <duration>
  Walk-forward windows: <train length / test length / step>
  Bootstrap: <block length and why it matches the dependence structure>
  Holdout period: <range> — touched: <no | yes, with authorisation ref>
```

| Result | Value |
|---|---|
| Observed Sharpe | `<value>` |
| Deflated Sharpe (N = `<global trials>`) | `<value>` |
| Fold sign consistency | `<value>` |
| Max drawdown | `<percent>` |
| Net edge per trade (bp) | `<value>` |
| `n_independent_episodes` | `<integer>` |
| Risk-limit breaches in simulation | `<integer — any non-zero value is a hard negative>` |
| Survival score | `<value>` |

```
Reproduction command: <exact command, with seed and data snapshot id>
```

---

## 12. Promotion criteria and lifecycle

*The thresholds fixed in advance, and applied literally. State what happens at each transition and who or what applies the rule.*

```
specified  -> backtested:  <criteria>
backtested -> challenger:  <criteria, including minimum forward observation period>
challenger -> champion:    <criteria, including forward performance against the incumbent>
any        -> retired:     <the kill criterion from section 2>

Applied by: <evolution engine component>, evaluated <cadence>
Demotion:   <what sends a champion back to challenger rather than to retired>
```

---

## 13. Monitoring

*The metrics whose deviation means the thesis is decaying, not merely that the strategy is losing. These are different: a strategy can lose money with an intact thesis and it can make money with a dead one, and only the second is an emergency. Each row needs an alert threshold and a named owner.*

| Metric | Healthy range | Alert threshold | What a breach means | Owner |
|---|---|---|---|---|
| `<realised net edge bp, rolling 60 trades>` | `<range>` | `<value>` | `<thesis decay / cost regime change / execution problem>` | `<agent>` |
| `<hit rate>` | `<range>` | `<value>` | `<interpretation>` | `<agent>` |
| `<mechanism proxy, e.g. carry share of edge>` | `<range>` | `<value>` | `<interpretation>` | `<agent>` |
| `<slippage vs decision price>` | `<range>` | `<value>` | `<interpretation>` | `<agent>` |

```
The single metric that would most cheaply tell us the thesis is dead: <name it and say why>
```

---

## Definition of done

- [ ] Sections 1 and 2 were written before section 5 existed in any form, and read as a claim about the world with no reference to a rule
- [ ] The thesis carries a sign, a horizon and a magnitude in bp
- [ ] The mechanism names a counterparty and says why their side is rational for them
- [ ] The `invalidation` expression is a `Decimal` computable from point-in-time features at signal time
- [ ] The population-level kill criterion includes a minimum sample size
- [ ] Every evidence row is copied from a `HypothesisResult` with `spec_hash_matches: true`
- [ ] Every feature in section 4 has an availability verdict and an earliest clean date
- [ ] Every constant in sections 5 and 6 has a provenance comment
- [ ] The `Signal` snippet typechecks under `mypy --strict` and constructs every `Decimal` from `str`
- [ ] `conviction` is nowhere used, described, or scaled as a size
- [ ] Cost figures cite a production-calibrated `market-research` artefact, and no number in section 9 came from testnet
- [ ] Validation design was fixed before the results table was filled
- [ ] Promotion thresholds are numeric and were set before backtesting
- [ ] Holdout status is stated explicitly, with an authorisation reference if touched
- [ ] `make check` is green on the branch carrying this file
