# Template — Experiment Log

Copy this file to `docs/experiments/<yyyy-mm-dd>-<experiment-id>-<kebab-slug>.md`. One file per experiment, one experiment per file, and the file is written in two passes: everything above the results table before the run, everything below it after. Example: `docs/experiments/2026-07-22-exp-0031-funding-horizon-grid.md`.

An automated search over strategy space is a machine for producing overfit results: run enough configurations against fixed history and some will look excellent by chance alone. This document is one of the defences. It works only if the declaration genuinely precedes the run — a decision rule written after seeing the results is not a decision rule, it is a description of the result you liked.

Related: `../rules/overfitting-defences.md`, `../agents/quant.md`, `EVOLUTION_ENGINE.md`, `SCORING_ENGINE.md`, `BACKTEST_ENGINE.md`.

---

```yaml
---
experiment_id: <exp-NNNN>
date: <yyyy-mm-dd>
hypothesis_ref: <artifacts/agents/quant/registered/<correlation_id>.json>
correlation_id: <id>
global_trials_before: <integer>
trials_charged: <integer — the full declared grid, not the executed subset>
global_trials_after: <integer — must equal before + charged>
seed: <integer>
code_commit_sha: <full sha of the commit the run executed>
data_snapshot_id: <snapshot or checksum set the run read>
declared_at: <commit sha of the commit that added this file with sections 1-4 filled and the results table empty>
---
```

---

## 1. What is being varied and what is held fixed

*Two lists. The held-fixed list is the more useful one and gets read more often, because the first question anyone asks a surprising result is "what else changed?". Anything not in either list is an uncontrolled variable, and naming it here as such is better than discovering it later.*

**Varied**

| Parameter | Values | Why these values and not others |
|---|---|---|
| `<name>` | `<list>` | `<from the mechanism, an ADR, or a prior result — not "these looked reasonable">` |

**Held fixed**

| Thing | Value | Where it is pinned |
|---|---|---|
| Code | `<commit sha>` | frontmatter |
| Data snapshot | `<id>` | frontmatter |
| Seed | `<integer>` | frontmatter |
| Cost model | `<bp, production-calibrated>` | `<artefact id>` |
| Validation design | `<CPCV config>` | section 2 |
| Universe | `<symbols>` | `<config path>` |
| `<other>` | `<value>` | `<where>` |

```
Known uncontrolled variables: <name them, or "none identified">
```

---

## 2. The declared parameter grid, in full

*The complete grid, written before the run. **The charge is for the declared grid, not the executed one.** If you declare a 200-point grid and abandon it after 12 points because the first 12 looked bad, the selection still happened at declaration: you would have run the remaining 188 had the first 12 looked promising, and charging 12 understates the deflation by 188. Charge at specification time, every time, for the whole grid.*

*The corollary is the reason this section is worth taking seriously: a large grid is expensive to everyone in this project, forever, because the global counter is monotone and feeds the deflated Sharpe of every future result. A grid with two parameters fixed a priori from a mechanism is worth more than a grid with six chosen because they were available. If you cannot fix a parameter a priori, that is evidence you do not have a mechanism, and an experiment without a mechanism is a search.*

```python
parameter_grid = {
    "<name>": [<values>],
    "<name>": [<values>],
}
n_symbols  = <integer>
n_variants = <integer>

trials_charged = <product of grid lengths> * n_symbols * n_variants  # = <integer>
```

```
Charged to the global ledger at: <timestamp, before first data access>
Ledger entry: <id>
```

---

## 3. Decision rule, fixed in advance

*The exact thresholds that will be applied, with comparators and values, written before the run and applied literally afterwards. Include a rule for what happens on a near miss, because that is the case where the rule will be tested. State the promotion consequence, so the rule has teeth: a rule with no consequence is a preference.*

```
Promote iff ALL of:
  deflated Sharpe        >= <value>   (at the global trial count at test time)
  fold sign consistency  >= <value>
  net edge (bp)          >= <value>
  n_independent_episodes >= <integer>
  risk-limit breaches     = 0         (any breach is a hard negative, not a deduction)
  max drawdown           <= <value>

On a near miss: it failed. Record the margin. No additional configuration is run to check.
Consequence of a pass:  <the lifecycle transition this authorises>
Consequence of a fail:  <what happens to the parent strategy and to this line of enquiry>
```

---

## 4. Reproduction command

*The exact command, including seed, snapshot and config path, such that someone else on the same commit gets the same numbers. Determinism is not optional here — an experiment that cannot be re-run is an anecdote.*

```console
$ <exact command with all arguments>
```

```
Expected runtime:   <duration>
Deterministic:      <yes — every source of randomness is seeded; list any that are not>
Output artefacts:   <paths the run writes>
```

---

## 5. Results

*Written after the run. One row per configuration in the declared grid, including the configurations that were not executed — mark those `not run` rather than omitting them, because an omitted row reads as a grid that was smaller than it was.*

| Config | Observed Sharpe | Deflated Sharpe | Fold sign consistency | `n_independent_episodes` | Net edge (bp) | Max drawdown | Risk-limit breaches |
|---|---|---|---|---|---|---|---|
| `<params>` | `<value>` | `<value>` | `<value>` | `<integer>` | `<value>` | `<value>` | `<integer>` |
| `<params>` | `<value>` | `<value>` | `<value>` | `<integer>` | `<value>` | `<value>` | `<integer>` |
| `<params>` | `not run` | | | | | | `<reason>` |

```
Global trial count at test: <integer — the value used in the deflation, not the value at declaration>
Configurations executed:    <n> of <declared n>
Any breach above zero:      <which config, which limit, and by how much>
```

---

## 6. Verdict against the pre-declared rule

*Apply the rule from section 3 literally, criterion by criterion, and show the comparison for the best configuration whether it passes or fails. Naming the temptation is part of the record: if a small change to an assumption would have flipped the result, say what it was and why it is not available.*

| Criterion | Threshold | Best config value | Pass |
|---|---|---|---|
| Deflated Sharpe | `<value>` | `<value>` | `<yes / no>` |
| Fold sign consistency | `<value>` | `<value>` | `<yes / no>` |
| Net edge (bp) | `<value>` | `<value>` | `<yes / no>` |
| `n_independent_episodes` | `<integer>` | `<integer>` | `<yes / no>` |
| Risk-limit breaches | `0` | `<integer>` | `<yes / no>` |
| Max drawdown | `<value>` | `<value>` | `<yes / no>` |

```
Verdict: <promote | reject>
Margin on the binding criterion: <value, in that criterion's units>
The change that would have flipped this: <name it> — not available because <reason>
```

---

## 7. What was learned that is not about this experiment

*The transferable finding. Most experiments produce a verdict that stops mattering within a month and an observation about method, data, or cost structure that keeps mattering for a year. This section is for the second kind. "The parameter did not help" is about this experiment; "the deflation at our current trial count makes any single-symbol result unprovable below a 1.4 observed Sharpe, so single-symbol experiments are not worth declaring" is not.*

```
<Two to four sentences, with the evidence that supports generalising it.>
```

```
Where this belongs: <../knowledge/verified-facts.md | ../knowledge/failure-library.md |
                     a semantic memory entry | an ADR>
```

---

## 8. Holdout status

*Was the permanently held-out period touched? It is burned the moment it is read, including for a plot. If the answer is yes, the human authorisation reference is mandatory and its absence makes this experiment's holdout results void rather than merely questionable.*

```
Holdout touched:        <no | yes>
Authorisation ref:      <issue #N and the human who approved it — required if yes>
Holdout period read:    <range>
Reads of this holdout to date: <integer>
Remaining budget:       <per the milestone's holdout ledger>
```

---

## Definition of done

- [ ] Sections 1 through 4 were committed before the run, and `declared_at` points at that commit
- [ ] The grid is stated in full, and `trials_charged` covers the declared grid rather than the executed subset
- [ ] `global_trials_after` equals `global_trials_before` plus `trials_charged`
- [ ] Every varied value has a justification from a mechanism, an ADR, or a prior result
- [ ] The held-fixed list names the code sha, data snapshot, seed, and cost artefact
- [ ] The decision rule has numeric thresholds, a near-miss clause, and a stated consequence
- [ ] The reproduction command runs deterministically on the recorded commit
- [ ] The results table includes unexecuted configurations, marked `not run`
- [ ] The deflated Sharpe uses the global trial count at test time
- [ ] The verdict applies the pre-declared rule literally, including on a near miss
- [ ] The change that would have flipped the result is named and its unavailability explained
- [ ] Section 7 says something that outlives this experiment
- [ ] Holdout status is stated, with an authorisation reference if it was touched
