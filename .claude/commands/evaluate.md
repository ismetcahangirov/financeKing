---
description: Evaluate a strategy against the survival score and issue a lifecycle verdict
argument-hint: <strategy-id-or-version-hash>
allowed-tools: Read, Grep, Glob, Bash
---

Evaluate: $ARGUMENTS

The survival score is deliberately **not profit**. Judging a strategy on returns alone selects for hidden tail risk — the strategy that never breached a limit because it was lucky and the one that breached limits profitably look the same on a P&L chart.

## 1. Identify precisely

```bash
python -m fking.evolution.inspect --strategy $1
```

Record: version hash, lineage (parents and mutation operators applied), promotion date, current lifecycle stage, cumulative trial count attributable to this lineage.

An evaluation without a version hash is unattributable and worthless three months later.

## 2. Compute the survival score components

Report each separately. The aggregate hides exactly what you need to see.

| Component | What to report |
|---|---|
| Risk-adjusted return | Deflated Sharpe with the trial count used to deflate it |
| Drawdown discipline | Max DD, time to recovery, DD relative to the modelled worst case |
| Cross-regime consistency | Per-regime return dispersion — trend/chop, high/low vol |
| Per-trade edge after costs | bp gross, bp net, and the ratio |
| Capacity | Notional at which modelled slippage consumes half the edge |
| Out-of-sample decay | In-sample vs out-of-sample performance ratio |
| **Risk-limit violations** | **Count and severity — a hard negative** |

A strategy that made money by breaching limits scores worse than one that made less within them. If the score being reported does not reflect that, the scoring engine has been softened and that is the finding.

## 3. Ask the questions the score cannot

- **Is the invalidation level being respected?** Compare exits against the declared invalidation. A strategy whose losers routinely run past their stated invalidation does not have the thesis it claims — it has a different, unstated one.
- **Is the edge in a few trades?** Remove the best 5 trades and recompute. If the edge vanishes, it is a story about five events.
- **Has capacity changed?** An edge measured at low volume that is now sized up may have already eaten itself.
- **Is realized slippage tracking the model?** If realized slippage exceeds modelled slippage systematically, the cost model is wrong and every result downstream of it is wrong too. Remember the cost model must be calibrated from **production** data — testnet shows ~7.5bp spread against production's ~0.16bp and ~10x inflated volume.

## 4. Decay detection

Compare the most recent forward window against the validation-period expectation:

```bash
python -m fking.evolution.report --strategy $1 --forward
```

Decay is expected — the question is whether it exceeds the out-of-sample decay allowance the strategy was promoted under. Do not extend the allowance to keep a strategy alive. Retiring strategies is the system working, not the system failing.

## 5. Verdict

One of:

- **Promote** — forward performance confirms validation, all components within bounds, zero hard risk violations.
- **Hold** — performing within expectation; state the next review date and the specific number that would change the verdict.
- **Reduce** — capacity or decay concern; size down rather than retire, with the threshold for the next step stated.
- **Retire** — decay past allowance, or any hard risk violation. Retirement is permanent for this version hash; a fixed variant is a new lineage entry with its own trial count, not a resurrection.

## 6. Record

Append the evaluation to the append-only strategy record: version hash, date, every component value, the verdict, and the reasoning. Never edit a prior evaluation — the sequence of evaluations is how you later detect that the scoring engine itself was drifting.

## 7. The meta-check

State whether this strategy's validation rank predicted its forward performance. If validation rank consistently fails to predict forward results across the population, the scoring engine is lying, and fixing that outranks everything else in the project.
