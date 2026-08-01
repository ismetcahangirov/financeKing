---
description: Profile and optimize a hot path with before/after numbers, without trading away correctness
argument-hint: <component or operation>
allowed-tools: Read, Grep, Glob, Bash, Write, Edit
---

Optimize: $ARGUMENTS

## 1. State the requirement before optimizing

What must it be fast enough for? This system trades on minute-to-hour timeframes and is explicitly **not built for latency arbitrage**. Execution-path microseconds are almost never the constraint. The two that genuinely are:

- **Backtest throughput** — a slow engine means fewer validation folds get run, and skipped folds are the real cost.
- **Ingestion and feature computation** — a slow feature store makes point-in-time recomputation expensive enough that people start caching it wrongly.

If the answer is "it feels slow", stop and measure whether it matters.

## 2. Measure before changing anything

```bash
python -m cProfile -o /tmp/prof.out -m fking.backtest.run --config configs/backtest/<pinned>.toml
python -c "import pstats; pstats.Stats('/tmp/prof.out').sort_stats('cumtime').print_stats(25)"
```

Record wall time, and for ingestion, rows/second and peak RSS.

Guessing the hot spot is reliably wrong here. In practice the time is usually in Postgres round-trips, `Decimal` construction inside inner loops, or pandas object-dtype columns — not the arithmetic people assume.

## 3. Optimize in this order

1. **Do less work.** Fewer queries, a narrower date range, columnar scans through DuckDB over Parquet instead of row-by-row Postgres reads for bulk history.
2. **Batch the round-trips.** N+1 queries per bar dominate everything else.
3. **Fix the data types.** Object-dtype pandas columns are usually 10–50x slower than the typed equivalent; parse once at ingestion rather than per access.
4. **Cache only what is safe to cache.** A cached feature must still be point-in-time correct. A cache keyed on symbol but not on as-of time is a look-ahead bug wearing a performance costume — this is the most dangerous optimization in the project.
5. **Only then** micro-optimize.

## 4. What you may not trade away

- `Decimal` for money. Switching to `float` for speed is not an optimization, it is a correctness regression that shows up as reconciliation drift. If `Decimal` construction is genuinely hot, hoist it out of the loop or construct once at the boundary — do not change the type.
- Point-in-time semantics.
- Backtest/live parity. An optimization that applies only to the backtest venue and changes fill semantics has broken the one property that makes backtests falsifiable.
- Determinism. A parallelized backtest must produce bit-identical results to the serial one; if it does not, the parallelization has a shared-state bug and the speedup is fictional.

## 5. Prove behaviour is unchanged

```bash
make backtest CONFIG=configs/backtest/<pinned>.toml > /tmp/after.json
diff /tmp/before.json /tmp/after.json
make check
```

Trade-for-trade identity. "Similar results" means you have changed behaviour and do not know how.

## 6. Measure after

Report before, after, and the ratio, on the same machine and the same data range. Then state what the new bottleneck is — there always is one, and naming it stops the next person re-profiling from scratch.

## 7. Report

Requirement, baseline, change, new number, proof of identical behaviour, and the next bottleneck. If the speedup was under ~20%, recommend reverting: complexity added for a marginal gain is a permanent cost for a temporary one.
