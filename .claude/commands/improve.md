---
description: Improve an existing component along one named axis, with a before/after measurement
argument-hint: <component> [axis: correctness|clarity|coverage|cost|latency]
allowed-tools: Read, Grep, Glob, Bash, Write, Edit
---

Improve: $ARGUMENTS

"Improve" without a named axis and a measurement is a licence to churn. Pick one axis, measure before, change, measure after.

## 1. Choose the axis and the metric

| Axis | Metric | Command |
|---|---|---|
| correctness | failing property test that now passes | `make test ARGS="tests/<area> -v"` |
| clarity | reviewer can state what the module knows about in one sentence | reading |
| coverage | module coverage vs its floor | `make test ARGS="--cov=src/fking/<m> --cov-report=term-missing"` |
| cost | LLM tokens or free-tier quota consumed per cycle | agent gateway quota report |
| latency | p95 of the hot path | `make test ARGS="tests/perf -v"` |

If the metric cannot be stated, the improvement cannot be verified and should not be attempted.

## 2. Measure before

Record the number now, in this transcript. An improvement without a baseline is an assertion.

## 3. Find the real limiter

Do not improve what is convenient. For coverage, the gap is almost never in the module with the lowest percentage — it is in the branches that only fire under partial fills, redelivery, and reconnects. For latency, profile before guessing; the hot path in this system is usually feature computation or a Postgres round-trip, not the Python arithmetic people assume.

For cost: the free-tier LLM quota is an architectural constraint, not a budget line. If an agent is burning quota, the improvement is usually fewer calls with better-scoped context or a cache hit, not a smaller model — and quota exhaustion must degrade to deterministic-only operation rather than stalling the loop.

## 4. Change one thing

Do not bundle. A commit that improves coverage and refactors structure cannot be reverted when one half turns out wrong.

Do not weaken a test to make coverage look better, and do not weaken the survival score's risk-violation penalty to make a strategy look better. Both are ways of improving the measurement instead of the thing.

## 5. Measure after and check for collateral damage

```bash
make check
```

Then re-measure the chosen metric, and check the axes you were *not* optimizing:

- Did coverage drop elsewhere?
- Did a backtest golden run change? If behaviour moved during a "clarity" improvement, that is a bug you just introduced.
- Did `mypy --strict` gain a new `# type: ignore`? Each one needs an inline comment saying why it is unavoidable.

## 6. Report

Axis, before, after, what changed, and what you confirmed did not change. If the improvement was smaller than expected, say the number rather than the adjective.
