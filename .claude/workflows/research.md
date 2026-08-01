# Workflow — Research

Research ends in a **falsifiable hypothesis with a data availability verdict**, or in a written "no". Both are successful outcomes. A summary of what was interesting is not.

---

## 1. Sharpen the question

Rewrite it until it has a measurable answer. "Does order flow predict returns?" is not researchable. "Does signed taker volume imbalance over 5m predict the sign of the next 15m return on BTCUSDT perpetual, out of sample, net of costs?" is.

**Exit condition**: the question names the instrument, the horizon, the input, and the success criterion.

---

## 2. Data availability verdict — before any analysis

This kills most research here, and it is far cheaper to be killed at this step than after a promising result.

```bash
grep -rn "AVAILABLE\|declare_feature" src/fking/data/ | head -30
```

The hard ceiling: **free full-depth L2 order book history does not exist.** Binance `bookDepth` is aggregated depth bands sampled roughly once per minute, not snapshots. What exists is tick trades, top-of-book on futures, and coarse depth bands. The feature store refuses unavailable requests deliberately, so that a strategy cannot silently assume richer data.

If the question needs queue position or resting-liquidity dynamics, record it as **untestable with current data** and stop. That is a real finding and it saves the next person the same week.

**Exit condition**: every input is declared available, with its earliest clean date. The hypothesis inherits the shortest history.

---

## 3. Load and distrust the data

Checksum-verify every archive before trusting it. Then check the three verified ingestion traps:

- **Timestamp units** — spot switched to microseconds from 2025-01-01; futures stayed in milliseconds. Normalize keyed on `(market, date)`, never a global constant. Print raw integers and confirm the first/last timestamps render as sane UTC.
- **Header rows** — futures kline CSVs have one; spot ones do not.
- **Booleans** — spot trade files serialize Python-style `True`/`False`.

Then look at the series itself: duplicate timestamps, gaps at known outages, zero prices, volume discontinuities that are unit changes rather than events.

**Exit condition**: you have looked at the raw values, not just the summary statistics.

---

## 4. Split the data before looking at it

Decide the split first, in writing:

- **Explore** — the earliest portion. Iterate here freely.
- **Confirm** — a later portion. Look at it **once**.
- **Held out** — the reserved period. **Do not touch it.** It is burned the moment it is read, including for a plot, including "just to check". Research does not get to spend it.

**Exit condition**: the ranges are written down before analysis starts.

---

## 5. Analyse

- Report effect size in basis points, not just significance. A significant 0.3bp edge is noise after costs.
- Compare against the trivial baseline — buy and hold, previous-return sign. Most "edges" lose to it.
- Break the result down by sub-period and regime. An effect concentrated in one quarter is a story about that quarter.
- **Count every variant you try.** That number is real and it feeds the global trial count if this becomes a strategy. Twenty quiet variants make a 2-sigma result unremarkable.

---

## 6. Demand a mechanism

Before writing it up, answer: **who is on the other side of this trade, and why do they keep taking it?**

An effect with no mechanism is a coincidence with good manners. Mechanisms that hold up here are structural — a fee schedule, a funding-rate cycle, a liquidation cascade, a market-maker inventory constraint. "The model found it" is not a mechanism.

---

## 7. Write the note

`docs/research/<yyyy-mm-dd>-<kebab-slug>.md` — run `/research` for the required structure.

It must contain at least one thing a competent engineer would not have guessed. If it does not, the honest note is "no effect found", and that note still gets written and committed — otherwise the same question gets re-investigated in four months.

---

## 8. Hand off

| Verdict | Next step |
|---|---|
| Testable hypothesis with a mechanism | File a `feat` issue; proceed to `strategy-lifecycle.md` |
| Untestable with available data | Close with the note; link it from `DATA_PIPELINE.md` availability section |
| No effect | Commit the note. Negative results are the cheapest thing this project owns and the most frequently re-purchased. |

Commit on a `research/<n>-<slug>` branch and open a PR — research notes go through review like code, because the trial count they report is load-bearing for everything downstream.
