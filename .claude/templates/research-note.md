# Template — Research Note

Copy this file to `docs/research/<yyyy-mm-dd>-<kebab-slug>.md`, dated by the day the investigation started. Example: `docs/research/2026-07-19-funding-residual-reversal.md`.

Research ends in a falsifiable hypothesis with a data availability verdict, or in a written "no". Both are successful outcomes; a summary of what was interesting is not (`../workflows/research.md`).

**Negative results get committed.** A "no effect" note is the cheapest thing this project owns and the most frequently re-purchased: without the note, the same question gets re-investigated in four months by someone who has no way of knowing it was already answered, and the trials spent the first time still count against every result in the repository. Write the note, commit it on a `research/<n>-<slug>` branch, and open a pull request — research notes go through review like code, because the trial count they report is load-bearing downstream.

Related: `../workflows/research.md`, `../agents/quant.md`, `../rules/no-lookahead.md`, `../contexts/statistics-for-trading.md`, `DATA_PIPELINE.md`.

---

```yaml
---
date: <yyyy-mm-dd, investigation start>
author: <agent name or human username>
question: <the sharpened question in one line, naming instrument, horizon, input and success criterion>
verdict: <testable-hypothesis | untestable-with-available-data | no-effect>
trials_charged: <integer — every variant tried, including abandoned ones>
global_trials_after: <the global counter after this note's charge>
correlation_id: <the id threading this investigation through artefacts and the audit trail>
data_snapshot: <snapshot id or archive checksum set the analysis ran against>
seed: <integer>
---
```

---

## 1. The sharpened question

*Rewrite the original question until it has a measurable answer, and show both forms so a reader can see what was vague about the first one. The sharpened version must name the instrument, the horizon, the input, and the success criterion. If you cannot name a success criterion, the question is not yet researchable and nothing below this section will fix that.*

```
As asked:    <the original, vague form>
Sharpened:   <instrument> | <input> | <horizon> | <success criterion in bp, net of costs>
```

> Example: as asked — "does order flow predict returns?". Sharpened — does signed taker volume imbalance over 5m predict the sign of the next 15m return on BTCUSDT perpetual, out of sample, with at least 3bp of net edge after production-calibrated taker costs?

---

## 2. Data availability verdict

*Before any analysis. This step kills most research here, and being killed here is far cheaper than being killed after a promising result. Every input gets a row; the investigation inherits the shortest history in the table. The hard ceiling is real: free full-depth L2 order book history does not exist — Binance `bookDepth` is aggregated depth bands sampled roughly once per minute, not snapshots. If the question needs queue position or resting-liquidity dynamics, the verdict is `untestable-with-available-data`, you stop here, and you still write and commit this note.*

| Input | Feature id | Granularity | Earliest clean date | Verdict |
|---|---|---|---|---|
| `<input>` | `<feature_id>` | `<1m / 8h / trade-level>` | `<yyyy-mm-dd>` | `<available / unavailable>` |

```
Command used to check:  <the availability query or grep against the feature store>
Effective start date:   <yyyy-mm-dd — the shortest history above>
Verdict:                <available for all inputs | untestable, because <input> requires <data we do not have>>
```

---

## 3. Data traps checked

*The three verified ingestion traps plus checksum verification, each with the check you actually ran and what it returned. These are not hypothetical: they are defects this project has already been bitten by, and each one produces plausible numbers rather than an error. Show the raw values you looked at, not the summary statistics — a mean is unchanged by a timestamp unit bug in a way that a printed first row is not.*

| Trap | Check run | Result |
|---|---|---|
| Archive checksum | `<command>` | `<verified / mismatch on <file>>` |
| Timestamp units (spot switched to microseconds from 2025-01-01; futures stayed in milliseconds) | `<command printing raw integers and rendered UTC for first and last rows, per (market, date)>` | `<result>` |
| Header rows (futures kline CSVs have one; spot ones do not) | `<command>` | `<result>` |
| Boolean serialization (spot trade files serialize Python-style `True`/`False`) | `<command>` | `<result>` |
| Series sanity | `<duplicate timestamps, gaps at known outages, zero prices, volume discontinuities that are unit changes>` | `<result>` |

```
Raw values inspected: <paste the first and last few raw rows you actually looked at>
```

---

## 4. The split, declared before looking

*Written before any analysis touched any data. Three ranges. Explore is iterated on freely. Confirm is looked at once. Held out is not touched, and is burned the moment it is read — including for a plot, including "just to check". Research does not get to spend it.*

```
Explore:   <start> to <end>
Confirm:   <start> to <end>   — looked at <n> times (1 is the only acceptable value)
Held out:  <start> to <end>   — touched: <no | yes, authorisation ref <id>>

Declared at: <commit sha or artefact timestamp proving this preceded the analysis>
```

---

## 5. Method

*What was computed, over what, with what controls. Name the boring alternative explanation and how it was controlled — if the boring answer is not controlled, the analysis cannot distinguish it from the interesting one. State the dependence structure explicitly (overlapping windows, clustered episodes) and how significance accounted for it; a t-statistic on overlapping observations is not a significance figure.*

```
Computed:            <the statistic or model>
Controls:            <trailing return, volatility, and the specific boring alternative>
Dependence structure: <overlapping windows / clustered episodes / both>
Handled by:          <block bootstrap with <n>-day blocks / episode-level resampling / CPCV
                      with purge <duration> and embargo <duration>>
Cost assumption:     <bp, production-calibrated, artefact <id>> — never testnet
Reproduction:        <exact command, with seed and data snapshot id>
```

---

## 6. Results

*Effect size in basis points first, significance second, and always against the trivial baseline. A significant 0.3bp edge is noise after costs. Break the result down by sub-period and regime — an effect concentrated in one quarter is a story about that quarter, and saying so here is worth more than the headline number.*

| Measure | This effect | Trivial baseline | Baseline used |
|---|---|---|---|
| Gross edge (bp) | `<value>` | `<value>` | `<buy-and-hold / previous-return sign>` |
| Cost (bp, production-calibrated) | `<value>` | `<value>` | `<artefact id>` |
| **Net edge (bp)** | `<value>` | `<value>` | |
| Observed Sharpe | `<value>` | `<value>` | |
| `n_observations` | `<integer>` | | |
| **`n_independent_episodes`** | `<integer>` | | |

**By sub-period**

| Period | Net edge (bp) | Episodes | Sign |
|---|---|---|---|
| `<range>` | `<value>` | `<n>` | `<+ / ->` |

```
Concentration check: <is the effect carried by one period or regime, and by how much>
```

---

## 7. Variant count

*Every variant tried, including the ones abandoned after two minutes and the ones that looked bad immediately. This number feeds the global trial counter and it is the single most gameable figure in the project. Twenty quiet variants make a two-sigma result unremarkable, and the counter is monotone — there is no expiry and no reset.*

| Variant | What was changed | Kept or abandoned |
|---|---|---|
| `<n>` | `<the change>` | `<kept / abandoned after <what>>` |

```
Total variants tried:   <integer>
Trials charged:         <integer>
Global counter: <before> -> <after>
```

---

## 8. Mechanism

*Who is on the other side of this trade, and why do they keep taking it? An effect with no mechanism is a coincidence with good manners. Mechanisms that hold up here are structural: a fee schedule, a funding-rate cycle, a liquidation cascade, a market-maker inventory constraint. "The model found it" is not a mechanism, and neither is a story that would equally explain the opposite sign.*

```
Counterparty:        <who>
What they receive:   <why their side is rational for them>
Why it persists:     <what stops this being competed to zero>
What would break it: <the structural change that would end the effect>
```

---

## 9. The one non-obvious thing

*Mandatory. State one thing here that a competent engineer would not have guessed before reading this note. It does not have to be the headline result — it is often a data property, a cost asymmetry, a failed assumption, or a reframing of what the binding constraint actually is. If you cannot write this section, the honest verdict is `no-effect`, and you write that instead. A note with nothing surprising in it trains everyone to skim the next one.*

```
<The non-obvious thing, in two to four sentences, with the evidence for it.>
```

> Example: the effect is real at 14bp gross and our taker cost is 9bp, so the finding is not "funding predicts returns" but "this edge is real and we cannot currently afford it". That points at execution capability as the binding constraint rather than at more hypothesis generation, which is a different roadmap.

---

## 10. Verdict and hand-off

*The verdict, applied against the success criterion from section 1 literally, then the next step. If it failed by a hair, it failed; record the margin. Adjusting the criterion now is the failure mode the criterion exists to prevent.*

```
Verdict: <testable-hypothesis | untestable-with-available-data | no-effect>
Margin:  <how far from the criterion, in the criterion's own units>
```

| Verdict | Hand-off |
|---|---|
| `testable-hypothesis` | File a `feat` issue; register with `quant` carrying `correlation_id` `<id>`; proceed to strategy specification |
| `untestable-with-available-data` | Close with this note; link it from the `DATA_PIPELINE.md` availability section so the ceiling is documented where the next person will hit it |
| `no-effect` | Commit this note. It is the record that stops this question being bought again |

```
Next step:      <the concrete action, with the issue number if one was filed>
What would make this worth re-opening: <a named, observable change — new data, a cost
                                        regime change, a structural change to the venue>
```

---

## Definition of done

- [ ] The sharpened question names instrument, horizon, input and success criterion
- [ ] The availability verdict was written before any data was read
- [ ] All four data traps were checked with a command, and raw values were inspected rather than summary statistics
- [ ] The three-way split was declared before analysis, with a commit sha proving it
- [ ] Holdout status is stated, and untouched unless a human authorisation reference is given
- [ ] Results state effect size in bp against a named trivial baseline, and a sub-period breakdown
- [ ] `n_independent_episodes` is reported next to `n_observations`
- [ ] Every variant tried is listed, including abandoned ones, and the trial charge matches the count
- [ ] The mechanism names a counterparty and says why their side is rational
- [ ] Section 9 contains something genuinely non-obvious, or the verdict is `no-effect`
- [ ] The verdict was applied against the pre-declared criterion literally, with the margin recorded
- [ ] Committed on a `research/<n>-<slug>` branch and opened as a pull request, including when the verdict is negative
