---
description: Run a backtest and interrogate the result skeptically before believing any of it
argument-hint: <config-path-or-strategy-id> [start] [end]
allowed-tools: Read, Grep, Glob, Bash, Write
---

Run and then attack a backtest for: $ARGUMENTS

A backtest result is a claim, not evidence. The default assumption is that a good-looking result is wrong. Your job in this command is to find out how.

## 1. Establish the run

```bash
make up                      # Postgres/Timescale + Redis must be live
make backtest CONFIG=$1
```

Record, before looking at any performance number:

- Strategy id and **version hash**. A result without a version hash is unattributable.
- Data range, symbol set, bar interval.
- Cost model parameter set id and where it was calibrated.
- The global trial counter value **before** this run.

## 2. The skeptical checklist — every item answered explicitly

Do not summarize. Answer each with the evidence you looked at.

**A. Was the trial count incremented?**
This run must have incremented the global trial counter. Every configuration ever evaluated against history counts — including the ones you abandoned after ten seconds, including reruns with a "tiny" parameter tweak. Confirm the counter moved and by how much:

```bash
grep -rn "trial" src/fking/evolution/ src/fking/backtest/ | head
```

If the counter did not move, the run is uncountable and the Sharpe below is meaningless. Fix that first.

**B. Was the deflated Sharpe computed?**
Report the raw Sharpe, the trial count *N*, and the deflated Sharpe. If deflated Sharpe is not above zero with the stated confidence, the strategy has not beaten selection bias and there is nothing to discuss. Quote the number; do not paraphrase it as "still looks good".

**C. Was any held-out period touched?**
Name the exact date ranges used. Cross-check against the reserved held-out window. The held-out period is burned the moment it is touched — including by an exploratory plot, including "just to sanity check". If it was touched, say so loudly and treat it as consumed; do not quietly proceed.

**D. Is there look-ahead?**
This is the defect class that does not fail — it makes bad strategies look excellent.
- Every feature used: was it available at bar close, or does it read the bar it predicts?
- Any resample, rolling window, `shift`, or join — does it use a centred or forward window anywhere?
- Any label, target, or normalization computed over the full range rather than expanding?
- Did the adversarial point-in-time leakage test run in this suite, and did it fail closed?

```bash
make test ARGS="-k leakage -v"
```

**E. Were costs modelled from production data?**
Cost parameters must be calibrated from **production** market data, never testnet. Measured: Binance futures testnet shows ~7.5bp spread against production's ~0.16bp, and roughly 10x inflated volume. A backtest calibrated on testnet is fiction — usually pessimistic on spread and wildly optimistic on fill probability. Confirm the parameter provenance in the config.

**F. Is the edge bigger than the costs?**
Report per-trade edge in bp gross, then net of fees, spread, and slippage. If gross edge is under ~2x modelled cost, the result is a cost-model artefact.

**G. Is the sample large enough?**
Trade count, not bar count. Under the minimum trade count the Sharpe confidence interval spans zero and the point estimate is decoration.

**H. Validation methodology.**
A single train/test split is not evidence. Confirm walk-forward and combinatorial purged CV with embargo were used, and report cross-fold dispersion. A strategy that is excellent in two folds and flat in six is one regime, not an edge.

**I. Regime breakdown.**
Split returns by regime (trend/chop, high/low vol). A result driven by one quarter is a story about that quarter.

**J. Timestamp sanity.**
Confirm the run's data passed unit normalization: spot timestamps switched to **microseconds from 2025-01-01** while futures stayed in **milliseconds**. A silent unit change shifts bars by orders of magnitude and produces spectacular fake alpha. Verify the first and last bar timestamps render as sane UTC datetimes, not as year 56000 or 1970.

**K. Survivorship and delisting.**
Is the symbol set the set that existed at the start of the window, or the set that exists today?

## 3. Verdict

State one of:

- **Reject** — with the specific failing item. This is the expected outcome and is a successful use of the command.
- **Advance to walk-forward / paper** — only with A–K all answered and the deflated Sharpe cleared.
- **Blocked** — evidence missing; name what is missing.

Do not report a Sharpe ratio as a headline without the trial count and deflated value next to it. Ever.

## 4. Record it

Append the run — config hash, strategy version, ranges, trial counter, raw and deflated Sharpe, verdict — to the append-only backtest audit record. Never overwrite a prior run's row; the record of rejected attempts is what makes the trial count honest.
