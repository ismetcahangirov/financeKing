---
description: Run a research investigation and produce a falsifiable hypothesis with a stated data availability verdict
argument-hint: <research question>
allowed-tools: Read, Grep, Glob, Bash, Write, WebSearch, WebFetch
---

Research: $ARGUMENTS

Research in this project ends in a **falsifiable hypothesis with a data availability verdict**, not a summary of what is interesting. A research note that cannot be turned into a test is not finished.

## 1. State the question precisely

Rewrite the question so it has a yes/no or a measurable answer. "Does order flow predict returns?" is not researchable. "Does signed taker volume imbalance over 5m predict the sign of the next 15m return on BTCUSDT perpetual, out of sample?" is.

## 2. Data availability first — before any analysis

This is the step that kills most research here, and it is cheaper to be killed now.

```bash
grep -rn "AVAILABLE\|declare_feature\|register_feature" src/fking/data/ | head -40
ls data/parquet/ 2>/dev/null
```

Answer explicitly:

- Does the feature store declare the required data available?
- **Free full-depth L2 order book history does not exist.** Binance `bookDepth` is aggregated depth bands sampled roughly once per minute, not snapshots. The zero-budget ceiling is tick trades, top-of-book on futures, and coarse depth bands. If the hypothesis needs resting-liquidity dynamics or queue position, stop and record it as untestable with current data — that is a valid, useful research outcome.
- What is the earliest date with clean data for every input? The hypothesis inherits the shortest history.

## 3. Investigate

For external sources, prefer primary documentation and actual exchange responses over blog posts. When citing an exchange behaviour, cite the endpoint and the observed response, not folklore.

If the research involves pulling data, keep the pull read-only against public archive endpoints and never against a trading endpoint — and note that even read-only production access to trading hosts is forbidden by the allowlist. Read paths become write paths during refactors; there are no exceptions, including read-only ones.

## 4. Check your data before you believe it

Every historical archive is guilty until checksum-verified. Then check the three verified ingestion traps:

- **Timestamp units**: spot switched to **microseconds from 2025-01-01**; futures stayed in **milliseconds**. Normalize keyed on `(market, date)`, never a global constant. Print the first and last timestamps as UTC datetimes and confirm they are sane.
- **Header rows**: futures kline CSVs have a header row; spot ones do not. A silently consumed header shifts every row by one.
- **Booleans**: spot trade files serialize booleans Python-style (`True`/`False`), which most CSV readers parse as strings.

Then sanity-check the series itself: duplicate timestamps, gaps at known exchange outages, prices that are exactly zero, volume spikes that are unit changes rather than events.

## 5. Analyse without fooling yourself

- Compute the effect on the earliest portion of data, then confirm on a later portion. Do not iterate on the later portion — that converts it into training data.
- **Do not touch the permanently held-out period.** It is burned the moment it is read, including for a plot.
- Report effect size in basis points, not just significance. A statistically significant 0.3bp edge is noise once costs are applied.
- Compare against the trivial baseline (buy and hold, previous-return sign). Most "edges" lose to it.
- Count how many variants you tried. That number belongs in the write-up and feeds the trial count if this becomes a strategy.

## 6. Write the note

Create `docs/research/<yyyy-mm-dd>-<kebab-slug>.md` containing:

1. The precise question
2. Data used, with exact ranges and the availability verdict
3. Method, including every variant tried and the count
4. Result with effect size and dispersion across sub-periods
5. **The falsifiable hypothesis**, phrased as a strategy thesis with a candidate invalidation level
6. **Why the effect should persist** — who is on the other side and why they keep taking that trade. An effect with no mechanism is a coincidence with good manners.
7. What would change the conclusion

The note must contain at least one thing a competent engineer would not have guessed. If it does not, the research did not find anything, and saying that plainly is the correct output.

## 7. Report

Verdict: **testable now** / **untestable with available data** / **no effect found**. Then the next concrete action.
