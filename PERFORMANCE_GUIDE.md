# Performance Guide

**This is not a latency-arbitrage system and must not pretend to be one.** It trades on minute-to-hour timeframes against a demo account. Execution-path microseconds are almost never the constraint, and effort spent shaving them is effort not spent on the thing that actually limits this project: **how many validation folds get run before a strategy is believed.**

`ARCHITECTURE.md` §13 says this plainly and it is worth repeating here, because "trading system" carries an implication of latency obsession that would be actively harmful to import. The bottleneck here is validation throughput, not order round-trip time.

---

## 1. What must be fast

### 1.1 Backtest throughput — the one that matters

Everything else is secondary to this.

The arithmetic that makes it the constraint: one symbol-year of 1m bars is **525,600 bars**. A combinatorial purged cross-validation over 8 groups produces 28 train/test splits. Walk-forward across 3 years, 5 symbols, with that CV, is roughly:

```
525,600 bars/symbol-year × 3 years × 5 symbols × 28 folds ≈ 220M bar-evaluations
```

per strategy candidate. The evolution engine evaluates many candidates per cycle.

**The requirement, stated as a decision rather than a number: a full validation suite for one candidate must complete inside an overnight window (~8h) on one machine.** From the figure above, that is roughly **8,000 bar-evaluations per second sustained**, including feature lookup, strategy evaluation, risk sizing, and simulated fill.

Why this specific framing matters more than any throughput number: **when the backtest is slow, people run fewer folds.** Nobody decides to reduce statistical rigour; they just pick 4 folds instead of 28 because 28 does not finish. The overfitting defences in `EVOLUTION_ENGINE.md` are the actual product of this repository, and backtest speed is what determines whether they are affordable. A slow engine does not produce slow answers — it produces *wrong* ones, arrived at faster.

### 1.2 Feature computation

Point-in-time recomputation must be cheap enough that nobody is tempted to cache it wrongly. That is the real requirement, and it is a correctness requirement wearing a performance costume — see §5.

Target: a full feature set for one symbol-year of 1m bars computes in under ~30s. Above that, the temptation to cache across as-of times becomes irresistible, and that temptation is how look-ahead bias enters.

### 1.3 Event bus latency — and where its budget comes from

The bus sits inside the signal → risk → order path in live operation. Its budget is not a preference; it is **derived from Binance's `recvWindow`**.

An order request carries a `timestamp`, and Binance rejects it with error `-1021` if the server's receipt time falls outside `recvWindow` (default 5000ms) of that timestamp. Our clock skew plus network round-trip plus every millisecond spent between stamping the request and sending it all consume that window.

The allocation:

| Segment | Budget |
|---|---|
| Clock skew allowance (measured, resynced on `-1021`) | 1000ms |
| Network round-trip to testnet | 500ms |
| **Signal → risk decision → order construction → send, including all bus hops** | **1500ms** |
| Safety margin | 2000ms |

So: **p99 end-to-end bus latency under 1500ms.** That is a generous budget by trading-system standards and a deliberate one — it is the number the exchange imposes, not a number chosen to feel fast. If a change pushes past it, the symptom is not "slow"; it is intermittent `-1021` rejections that look like an exchange problem.

Redis Streams on localhost delivers well inside this. If the bus is anywhere near the budget, something is wrong architecturally — most likely a consumer doing synchronous work on the event loop (§4.4).

---

## 2. What does not need to be fast

Naming these explicitly, because optimisation effort flows to whatever was measured most recently rather than to whatever matters:

- **Order placement microseconds.** We are not competing for queue position. A 50ms improvement in order construction buys nothing measurable in P&L on minute bars.
- **Dashboard API response time.** One user. 200ms and 20ms are indistinguishable.
- **Agent latency.** LLM calls take seconds and are inherently off the critical path. They propose; deterministic gates dispose, and the gates are not waiting.
- **Ingestion of live bars.** One bar per symbol per minute. This is not a throughput problem in any sense.
- **Startup time.** It happens once.

If you are optimising something on this list, stop and go read §1.1.

---

## 3. Profile before optimising

Guessing the hot spot is reliably wrong here.

```bash
python -m cProfile -o /tmp/prof.out -m fking.backtest.run \
  --config configs/backtest/<pinned>.toml
python -c "import pstats; pstats.Stats('/tmp/prof.out').sort_stats('cumtime').print_stats(25)"
```

For line-level attribution inside a known-hot function, `line_profiler`. For memory, `memray` — peak RSS matters during ingestion, where a naive load of a symbol-year of trades will not fit alongside the Postgres container.

Record before you touch anything: **wall time, and for ingestion rows/second and peak RSS**, on a named data range and a named machine. A speedup measured against an unrecorded baseline is a claim, not a result.

In practice, the time is almost never where people expect. The three actual culprits, in order:

1. **Postgres round-trips.** An N+1 query per bar dominates everything else by an order of magnitude, and it is invisible in a profile that only shows Python frames — it shows as time inside the driver.
2. **`Decimal` construction inside inner loops.** Not `Decimal` arithmetic — construction. `Decimal("123.45")` parses a string every time.
3. **pandas object-dtype columns.** Typically 10–50x slower than the typed equivalent, and they arise silently from a single `None` or a mixed-type read.

Nobody guesses these. They guess the arithmetic, which is fine.

---

## 4. The optimisation order

Do these in order. Each step is cheaper and safer than the one after it.

### 4.1 Do less work

Narrower date range, fewer symbols, fewer columns. The fastest query is the one not issued.

For bulk historical scans, read Parquet through DuckDB rather than row-by-row from Postgres. Columnar scan of the columns you need, no server round-trip, no ORM materialisation. This is what the Parquet + DuckDB half of the storage split exists for (`ARCHITECTURE.md` §6) — Postgres is the operational store, not the analytics scan path.

### 4.2 Batch the round-trips

One query per backtest run, not one per bar. Load the window, iterate in memory.

This is usually the single largest win available and it is usually available, because the natural way to write a bar loop is to fetch inside it.

### 4.3 Fix the data types

Parse once, at ingestion, into the right dtype. Never per access.

Object-dtype columns in pandas are the common case. `df.dtypes` before optimising anything else — an `object` column where you expected `float64` is a free 10x.

### 4.4 Keep the event loop free

Anything CPU-bound over ~10ms goes to `asyncio.to_thread` or a process pool. `CODING_STANDARDS.md` §10.2 has the specific consequence in this system: blocking the loop starves the Binance user-data WebSocket keepalive, the server closes the connection, fill events are missed, and the position divergence surfaces at reconciliation as an apparent exchange problem. The performance symptom and the correctness symptom are the same bug.

### 4.5 Cache — carefully, and read §5 first

### 4.6 Only then micro-optimise

Hoist `Decimal` construction out of loops. Use `__slots__` (already required on `domain/` dataclasses). Replace a comprehension with a generator where the intermediate list is large.

If you are here and the previous five steps did not help, the problem is probably algorithmic and micro-optimisation will buy you 15% of something that needs 10x.

---

## 5. Caching can silently introduce look-ahead bias

**This is the most dangerous optimisation in the project and it deserves its own section.**

The natural cache for a feature store is keyed on `(symbol, feature_name)`. It is also completely wrong, and it will not fail.

```python
# WRONG — a look-ahead bug wearing a performance costume
@lru_cache(maxsize=4096)
def get_feature(symbol: str, feature: str) -> Series:
    ...

# RIGHT
@lru_cache(maxsize=4096)
def get_feature(
    symbol: Symbol,
    feature: FeatureName,
    as_of: datetime,          # the point-in-time boundary
    data_vintage: VintageId,  # which ingestion produced the underlying bars
) -> Series:
    ...
```

**Why `as_of` is mandatory.** Without it, a value computed at a later as-of time is returned for an earlier one. In a walk-forward backtest, folds are frequently evaluated out of chronological order — so the cache is warmed by a *later* fold and then serves that warmed value to an *earlier* fold. The earlier fold now sees the future. Its Sharpe improves. Nothing raises. Nothing logs. The strategy is promoted.

**Why `data_vintage` is mandatory, and this is the less obvious half.** Historical bars get corrected. Binance republishes archives; a backfill repairs a gap; a timestamp normalisation bug is fixed and the data is reingested. A cache keyed only on `(symbol, feature, as_of)` will happily serve a value computed from the *old* bars after the data has been corrected — so two backtest runs over the same configuration produce different numbers, and neither is reproducible, and the difference is attributed to "randomness" because the alternative explanation is not visible.

The vintage id is the ingestion batch identifier. It changes whenever the underlying bars change. It is also what makes a cached result honestly comparable across the `results_epoch` boundary described in `RELEASE_PROCESS.md` §3.2.

### 5.1 The rule

**A cache key that does not contain the as-of time and the data vintage is a correctness bug, not a performance choice.**

Enforced by the adversarial leak test (`TESTING.md` §8.1), which is run **twice**: once against a cold cache and once against a cache deliberately warmed by later data. The warm-cache variant is the one that catches this, and it is the reason that test exists in both forms.

### 5.2 Caches that are safe

- Immutable reference data: symbol metadata, tick sizes, lot sizes, fee schedules. These have no as-of dimension within a run.
- Fully-computed, immutable artefacts keyed by content hash: a completed fold's result, keyed on `(strategy_hash, config_hash, data_vintage, results_epoch)`.
- Anything whose key already contains a timestamp that bounds the input.

---

## 6. Parallelism, and its exact limit

**Parallelise across folds. Never within a fold.**

Folds are independent by construction — different date ranges, no shared state. Running 28 folds across 8 processes is close to linear, and it is the single largest available speedup for the workload described in §1.1.

Within a fold, the simulation is **path-dependent and therefore inherently serial**. A trailing stop's level at bar *n* depends on every bar before it. A portfolio-level kill switch depends on cumulative drawdown. Position state depends on the fill sequence. Splitting a fold across workers and reassembling produces a result that is not merely different — it is a simulation of a different, incoherent strategy.

This is the same reason vectorized engines were rejected for the core (`ARCHITECTURE.md` §4): they cannot express path-dependent risk logic without leaking look-ahead. A parallel-within-fold backtest reintroduces exactly that defect from the other direction.

**Each worker gets its own seeded RNG derived deterministically from the run seed and the fold index** — not a shared global, and not a per-process default. `np.random.default_rng([run_seed, fold_index])`. A shared global RNG makes results depend on worker scheduling, which makes them irreproducible, which makes them worthless.

---

## 7. Determinism must survive every optimisation

**A parallelised backtest must produce bit-identical output to the serial one. If it does not, the parallelisation has a shared-state bug and the speedup is fictional.**

```bash
make backtest CONFIG=configs/backtest/<pinned>.toml > /tmp/before.json
# ... optimise ...
make backtest CONFIG=configs/backtest/<pinned>.toml > /tmp/after.json
diff /tmp/before.json /tmp/after.json
```

**An empty diff is the acceptance criterion. Trade-for-trade, timestamp-for-timestamp identity.** "Similar Sharpe" means you changed behaviour and do not know how — and the difference will be attributed to noise, because that is the available explanation.

If the diff is non-empty and you believe the new behaviour is better, that is a **separate change** with its own PR, its own justification, and a `Results-Invalidating:` trailer (`GIT_WORKFLOW.md` §3). It is not a performance change.

### 7.1 The ordering hazard

Reordering a summation changes the result even with `Decimal`, once precision limits are reached, and changes it reliably with `float`. So:

- Do not reorder accumulations "for cache locality".
- Do not replace an ordered `for` loop over fills with an unordered parallel reduce.
- Do not swap `sorted(x)` for `set(x)` anywhere a result depends on iteration order.
- `PYTHONHASHSEED` is pinned in CI; do not rely on it locally.

---

## 8. What you may not trade away

Four things, none negotiable for any speedup.

**`Decimal` for money.** Switching to `float` for speed is not an optimisation; it is a correctness regression that surfaces as reconciliation drift and looks like an exchange bug. If `Decimal` construction is genuinely hot, hoist it out of the loop or construct once at the boundary — do not change the type.

There is a legitimate boundary here, and it is worth stating precisely because it is the one place `float` is correct:

> **Money is `Decimal`. Indicator math may be `float64`.** Anything that becomes an order quantity, a price sent to the exchange, a realized or unrealized PnL figure, or a fee is `Decimal` end to end. A moving average, a z-score, a correlation coefficient, or an RSI — values that exist only to be compared against a threshold and never leave the strategy — may be `float64` numpy, which is 50–100x faster and vectorises.

The boundary rule: **the moment a float-derived value influences a quantity or a price, it must be converted through a documented, quantized step**, and that step is where the `Decimal` domain begins. A float indicator crossing a threshold to produce a *direction* is fine and remains deterministic (IEEE 754 is deterministic for a fixed operation order — see §7.1). A float indicator multiplied into a position size is not.

**Point-in-time semantics.** See §5.

**Backtest/live parity.** An optimisation applied only to `BacktestVenue` that changes fill semantics has broken the one property that makes backtests falsifiable (`ARCHITECTURE.md` §4). If the optimisation cannot be applied to all venues, it changes behaviour.

**Determinism.** See §7.

---

## 9. Reporting an optimisation

Every `perf` PR states, in this order:

1. **The requirement.** What must it be fast enough for, and why does that matter? "It felt slow" is not a requirement.
2. **The baseline.** Wall time, rows/sec, peak RSS as applicable. Named machine, named data range, named config.
3. **The change**, and which step of §4 it belongs to.
4. **The new number**, same machine, same data.
5. **The proof of identical behaviour** — the empty `diff`, pasted.
6. **The new bottleneck.** There always is one. Naming it stops the next person re-profiling from scratch, which is a real and repeated cost in a codebase written across sessions with no shared memory.

**If the speedup is under ~20%, recommend reverting.** Complexity added for a marginal gain is a permanent cost paid against a temporary benefit, and the next reader has to understand it forever.

---

## 10. Known hot spots and their current state

Kept here so that profiling starts from knowledge rather than from zero. Update it when you profile.

| Area | What dominates | The available fix |
|---|---|---|
| Backtest bar loop | Postgres round-trips per bar | Load the window once; DuckDB over Parquet for bulk history (§4.1, §4.2) |
| Feature computation | pandas object-dtype columns from mixed-type reads | Parse to typed dtypes at ingestion (§4.3) |
| Position arithmetic | `Decimal` *construction*, not arithmetic | Hoist construction out of the loop; construct at the boundary (§4.6) |
| Ingestion | Peak RSS on a full symbol-year of trades | Chunked read, write Parquet per partition, never hold a full year in memory |
| Walk-forward | Serial fold execution | Parallelise across folds only (§6), bounded by `WorkerMemoryBudget` (§11) |
| Live loop | Synchronous work on the event loop starving the WS keepalive | `asyncio.to_thread` (§4.4) |
| Event loop, per event | `fking.domain.codec.encode` for the trace digest — every event is encoded twice, once as itself and once as its trace entry | Already cut roughly in half by per-type dispatch caching (#109). What remains is inherent to the digest; see `docs/perf/README.md` for why an incremental digest was rejected |
| Event queue | `QueuedEvent` ordering-key construction under `heapq` | Already fixed (#109): the key is built once at insertion |

---

## 11. The reference workload and its budget

The methodology in §1.1 is expensive by design, and the risk is not that it is slow. It is that it becomes slow enough that somebody reduces `n_groups`, and the defence quietly weakens to fit the hardware. The budget exists to make that a conversation rather than a commit.

### The workload

`make bench` runs one pinned workload and nothing else: **CPCV N=8, k=2 — 28 paths — over a 20/60-minute mean-crossover strategy on BTCUSDT 1-minute bars, 2024-01-01 to 2024-01-15, seed 20240101.** 143,651 dispatched events. It is defined in `tools/bench/_workload.py`, and every one of those parameters is a module constant, so changing the workload is a diff somebody reviews rather than a flag somebody passes.

The bars are synthesised from a pinned seed rather than read from the Parquet corpus, because the corpus is not committed and a budget CI cannot assert is the terminal-only number this replaces. **The consequence is that the budget covers the engine — queue ordering, handler arithmetic, per-event encoding and digesting — and not the Parquet read path.** A regression in the read path will not be caught here.

The workload reports zero trades on every path by construction, so `path_distribution` refuses and no Sharpe can come out of it. That is deliberate: a benchmark that emitted a plausible-looking distribution would eventually have one of its numbers quoted.

### The budget, and the machine it was measured on

| | |
|---|---|
| **Budget** | **32.0 s wall clock**, single process |
| **Tolerance** | 20% over — the build fails above 38.4 s |
| **Machine** | GitHub-hosted `ubuntu-latest` runner, 4 vCPU / 16 GiB, CPython 3.12 |
| **Measured** | 2026-08-10, by the `bench` CI job (issue #109) |
| **Peak RSS** | ~170 MB, recorded rather than gated |
| **Where it lives** | `tools/bench/_budget.py` |

Wall clock, not CPU. A change that halves CPU time and triples I/O has not helped, and against a CPU budget it would read as a 50% win.

**The budget is a CI number, and there is deliberately no developer-machine budget.** The laptop this work was done on (i7-10870H, 16 GiB, Windows 11) produced 11.6 s and 43.4 s for the identical workload within a single session — a 3.7x spread from background load and thermal throttling, eighteen times the tolerance. A gate asserted there would fail on a busy afternoon and pass on a genuine 50% regression the next morning, and a gate that does that gets disabled within a month.

So locally, `make bench` is for comparing a change against the commit before it, back to back, in one sitting — which is the comparison a noisy machine can support. `make bench ARGS="--check"` will run anywhere, and its verdict means nothing off the reference machine.

### When it fails

The failure message carries the previous number, the current number and the overshoot. There are exactly two honest responses:

1. The change made the engine slower, and either it is worth it or it is not.
2. The budget is genuinely too small, in which case `tools/bench/_budget.py` gets a new measurement and the reason, in a reviewed diff.

Shrinking the workload is not a third option. If the honest conclusion is that a full CPCV run costs more than the budget allows, the budget goes up — `n_groups` does not go down.

### Bounding the fold pool

Fold parallelism (§6) is bounded by memory, never by cores. `os.cpu_count()` inside a container reports the host's cores rather than the cgroup's quota, which is how a 2-CPU container ends up running sixteen workers.

`fking.backtest.WorkerMemoryBudget` does the arithmetic — container limit, less a parent reserve, divided by a *measured* per-worker peak RSS — and `resolve_worker_total` **refuses** a request above what fits rather than reducing it. An oversubscribed pool does not fail: it swaps, produces every number a healthy run produces, and reports a wall clock that is an artefact of the swapping. That number then becomes a budget somebody defends.

```python
budget = WorkerMemoryBudget(
    memory_limit_bytes=container_memory_limit_bytes() or 4 * 1024**3,
    per_worker_peak_rss_bytes=170_000_000,   # make bench prints this
)
report = run_cpcv(partition, evaluate=..., charge=..., worker_total=4, memory_budget=budget)
```

`container_memory_limit_bytes()` returns `None` off a cgroup rather than falling back to physical RAM, so the caller has to state a limit — and stating it is what makes the number visible in review.

### The evidence

`docs/perf/` holds the dated profiling records and the analysis that produced this budget, including what was optimised, what was measured, and the two optimisations that were considered and rejected for weakening the determinism check.
