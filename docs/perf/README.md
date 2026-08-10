# Performance evidence

Profiling runs of the pinned reference workload, dated, so that the next investigation
starts from evidence rather than from an intuition about where the time goes.

Regenerate the machine-produced record with:

```bash
make bench ARGS="--profile docs/perf/$(date -u +%F)-cpcv-reference-profile.md"
```

The budget itself is in `tools/bench/_budget.py`, and `PERFORMANCE_GUIDE.md` §10 states it
with the machine it was measured on. Timings inside a profile are inflated by cProfile's
per-call overhead by roughly 2.5x on this workload; they rank call sites, they do not set
budgets.

| File | Workload | Note |
|---|---|---|
| [`2026-08-10-cpcv-reference-profile.md`](./2026-08-10-cpcv-reference-profile.md) | CPCV N=8 k=2, BTCUSDT 1m, 2024-01-01..2024-01-15, 143,651 events | Current baseline, after issue #109's optimisations |

## 2026-08-10 — where the time actually went (issue #109)

### The finding

Half the wall clock of a full CPCV run was in `fking.domain.codec.encode`, and none of it
was in `Decimal` arithmetic.

The engine records one `TraceEntry` per dispatched event, and the entry carries
`canonical_digest(encode(event))` — the digest two runs are compared on, which is the
property that makes a backtest evidence rather than an anecdote. It then encodes the whole
trace again at the end to produce the run digest. So every event is encoded twice, once as
itself and once as its trace entry, and each encode recurses through a `Bar`, an
`Instrument`, a `Venue` and six `Decimal`s.

Baseline profile, same workload, same session (top call sites by cumulative time, seconds
under cProfile):

```
        49,739,603 function calls in 62.634 seconds
3697556/143735   15.407   32.849  src/fking/domain/codec.py:56(encode)
      18326144    7.754    7.755  {built-in method builtins.isinstance}
        143735    0.723    8.195  src/fking/backtest/_config.py:62(canonical_digest)
        569584    3.266    5.264  Lib/dataclasses.py:1278(fields)
        143651    1.045    5.105  {built-in method _heapq.heappop}
```

Three things stand out, and only one of them was the suspect named in the issue:

1. **`isinstance`, 18.3 million calls.** `encode` walked a ten-branch `isinstance` chain
   per value. The chain's *order* is load-bearing — `Enum` must be checked before `str`,
   or a `StrEnum` member encodes as itself and decodes into something that is no longer an
   enum — but the *answer* depends only on the value's type.
2. **`dataclasses.fields()`, 569,584 calls.** Called once per encoded dataclass instance,
   and it rebuilds a tuple of `Field` objects every time. Field names cannot change after
   the class is defined.
3. **`heappop`, 5.1 s.** `QueuedEvent.__lt__` rebuilt its three-part ordering key on both
   sides of every comparison, and `heapq` performs O(log n) comparisons per push and pop.

Not on the list: `Decimal`. The reference strategy's rolling sums are two `Decimal`
additions, two subtractions and two multiplications per bar, and they do not appear in the
top ten at all. The issue's instruction holds in the strongest form — there was nothing to
gain by touching the money type, and the optimisation space was entirely in arithmetic and
allocation *around* it.

### What changed

- `fking.domain.codec`: dispatch resolved once per runtime type and cached
  (`_encoder_for`), with the field-name tuple resolved at the same moment. The chain of
  checks still states the supported shapes and their order; what is cached is which branch
  a *type* takes, never what a value encodes to. Encoded output is byte-identical, which
  is what the codec's round-trip property test asserts.
- `fking.backtest._queue`: `QueuedEvent` builds its ordering key once at insertion instead
  of twice per comparison. It is derived from the same three fields and is not a fourth
  ordering component.
- `tools/bench/_workload`: fold slicing by binary search rather than a scan per interval.
  Benchmark hygiene — that scan was 10% of the run, charged to the engine.

### The result

Under the profiler, back to back: **62.6 s → 34.9 s**, 49.7M → 24.3M function calls.
Unprofiled, on a quiet developer machine: **21.4 s → 11.6 s**, 6,697 → 12,403 events/s.

An interleaved A/B on the *same* machine later in the session, when it was heavily loaded,
measured a median of 42.2 s → 34.0 s. The two ratios disagree (1.85x versus 1.24x) because
contention compresses the difference, and that disagreement is the reason the committed
budget is a CI number rather than a laptop number — see `PERFORMANCE_GUIDE.md` §10.

### Considered and not done

- **Making the trace digest incremental** — a streaming SHA-256 over each entry as it is
  appended, instead of encoding the finished trace. It would remove the second encode of
  every event, which is the single largest remaining cost. It also changes the value of
  `RunTrace.digest`, and that digest is the determinism check's whole subject. Rejected:
  the saving is not worth invalidating every recorded run digest, and the change would have
  to be made once, deliberately, with a migration of what "the same run" means.
- **Making the trace optional** — a flag to skip trace entries in "fast" runs. Rejected on
  sight. It is a configuration flag that bypasses a gate (`CLAUDE.md` §11), and the gate it
  bypasses is the one that makes results comparable at all.
