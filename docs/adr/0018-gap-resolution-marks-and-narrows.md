---
number: 0018
title: A filled gap is marked resolved and a partial fill narrows by insertion, never by rewrite or delete
date: 2026-08-04
status: accepted
deciders: [ismetcahangirov, architect, data-engineer]
supersedes: null
superseded_by: null
related_issues: ["#28", "#27", "#26", "#30"]
related_adrs: [ADR-0015, ADR-0013]
---

## Context

`coverage_gap` has been append-only since `0009_ingest_registry`, and nothing needed more than that: the archive backfill discovers holes, live ingestion discovers holes, and neither of them fills anything. #28 adds the first writer that closes one — REST gap repair — and closing a gap is where the table stops being append-only in the obvious sense.

Two columns in that table cannot be reproduced by rebuilding the corpus. `discovered_at_utc` answers *which completed backtests consumed this range before anybody knew there was a hole in it* (`DATA_PIPELINE.md` §11), and the bounds are what makes that question answerable about a specific window. Every remaining column is derivable.

```
Forces:
- A gap that has been filled must stop refusing windows. The availability
  contract (#30) reads coverage_gap and refuses any backtest window that
  intersects a gap, so a repaired range that keeps its row blocks work over
  data the corpus now holds.
- A gap that was filled is MORE interesting afterwards, not less. Every
  backtest that ran over the hole is still wrong, and discovered_at_utc plus
  the bounds are the only way to find those runs.
- REST does not return everything. Binance retention, a 1000-bar page size and
  a symbol delisted mid-gap all produce partial recovery, so "closed" and
  "open" are not the only two outcomes.
- A partially backfilled gap recorded as closed is strictly worse than an open
  one: the coverage report then tells backtest it may run over a range that is
  still holed, and nothing downstream can tell.
- CLAUDE.md 2 and .claude/rules/append-only-audit.md: an audit record the
  application can rewrite is not an audit record, and the control has to be in
  the database rather than in the writer.

The constraint that forces a decision now:
#28 cannot write a single row until the table can express "this range was
missing, then it was not" -- and whichever shape that takes is the shape every
later repair path, including the trade tape and the archive re-fetch, copies.
```

## Decision

We mark a filled gap resolved rather than deleting it: `coverage_gap` gains `resolved_at_utc` and `resolution ∈ {backfilled, superseded}`, written by `IngestRegistry.resolve_gap` in `src/fking/data/backfill/registry.py`, and `data_coverage` and every registry read now filter on `resolved_at_utc IS NULL`. A partial recovery inserts narrower rows carrying the **original** `discovered_at_utc` and marks the original `superseded`; the bounds, the kind and the discovery instant of an existing row are never rewritten, and no row is ever deleted. `0012_gap_resolution` enforces both halves in the database — a `BEFORE UPDATE` row trigger that admits a change to those two columns and nothing else, a `BEFORE DELETE` trigger that always raises, and the `DELETE` grant revoked from `fking_ingest`, which moves `coverage_gap` into a new `INGEST_RESOLVABLE` privilege class. The decision covers gap rows only; it says nothing about how a repair decides what to fetch, which lives in `fking.data.backfill.gaps`.

## Alternatives considered

### Alternative 1 — narrow the gap by updating its bounds in place (strongest rejected)

**What it would have given us.** One row per hole, forever, with the bounds always describing exactly what is missing right now. `UPDATE coverage_gap SET gap_start_utc = :recovered_through` after a partial backfill is a single statement, needs no new columns, needs no trigger, and leaves `data_coverage` untouched — `sum(gap_end_utc - gap_start_utc)` stays a truthful total with no predicate to remember. The registry's own docstring already argues against *merging* rows on exactly the grounds that a merged row destroys the earlier discovery instant, and an update that only ever shrinks a row keeps that instant intact. Every reader of the table gets simpler, and a query for "what is missing" needs no `WHERE resolved_at_utc IS NULL` that somebody will forget.

**Why it lost.** The bounds are half of the reconstruction key, not merely a description. `discovered_at_utc` alone answers "when did we learn something was missing"; it takes the bounds with it to answer "was the range this backtest consumed one of them". A run that consumed 10:00–11:00 on 2026-08-04 is invalidated by a gap that covered 10:00–10:40 at the time, and after an in-place narrow to 10:40–11:00 there is nothing in the database that says 10:00–10:40 was ever holed — the row is now truthful about the present and silent about the only period anyone would investigate. Worse, it is *silently* silent: the row still exists, still carries an old discovery instant, and reads as a complete account. The insert-plus-mark form costs one extra row per partial repair and one predicate on three queries, and keeps both spans on record with the instant that links them.

### Alternative 2 — delete the row when the range is fully recovered

**What it would have given us.** The simplest possible semantics: a gap exists while the range is missing and does not exist afterwards. No new columns, no new privilege class, no `resolved_at_utc IS NULL` on any query, and `data_coverage` needs no change at all. It is also the reading most people have of the word "gap".

**Why it lost.** It is the same loss as Alternative 1 with no partial case to soften it, and it removes the row that ADR-0015's reasoning exists to protect. It also cannot be constrained: once `fking_ingest` holds `DELETE` on `coverage_gap` for the legitimate case, nothing distinguishes a repair that deleted a filled gap from an operator who deleted an inconvenient one during an incident, and `TRUNCATE` fires no row trigger at all. The correct fix for a bad gap row is a new row, which is also the fix that leaves evidence.

### Alternative 3 — do nothing

```
Cost of the status quo: #28 blocked outright, and with it P1's last critical-path
                        issue; every gap live ingestion has recorded since #27
                        stays permanently open, so the availability contract
                        refuses every window intersecting a 400ms reconnect
                        forever. On a 24-hour venue-initiated disconnect cycle
                        that is at least one permanent refusal per symbol per day.
Why that is no longer payable: #27 shipped the detectors. A system that records
                        gaps and can never close one converges on refusing every
                        window it holds, at which point the contract that exists
                        to protect backtests instead prevents them, and the
                        pressure to disable it becomes the real risk.
```

## Consequences

**What becomes easier**
- A repair can honestly report partial success. "Forty of sixty minutes recovered, twenty still missing" is expressible as data rather than as a log line, and `BackfillOutcome.minutes_still_missing` is derived from the rows rather than from what the venue claimed to send.
- "Which backtests are suspect" survives the repair. The resolved row keeps its bounds and its discovery instant, so a dispute months later is answerable from the table with no application memory (`ARCHITECTURE.md` §11).
- The unresolved set is the whole working set. `ix_coverage_gap_unresolved` is a partial index on the predicate every reader now uses, so a corpus with years of repaired history costs nothing to query.

**What becomes harder**
- Every new reader of `coverage_gap` must remember `WHERE resolved_at_utc IS NULL`, and forgetting it fails in the *pessimistic* direction — a window refused over a hole that has been filled. That is the safer direction and it is still wrong.
- A repair path can no longer clean up after itself. A backfill that writes a residual it should not have cannot delete it; it can only insert a correcting row, which means a mistake in the repair logic leaves permanent rows and is found by reading them.
- `coverage_gap` needed its own privilege class. `INGEST_OWNED` grants `DELETE`, so the classification table grew a sixth member for one table, and the next ingest-owned table's author now has one more question to answer.

**What we now cannot do**
- Compact the gap table. There is no retention policy, no merge and no delete, so a symbol with a chronically unstable feed accumulates rows without bound. Reopening that means an archival path with the same shape as the audit partitions' — detach, verify, and keep the terminal state linked — not a `DELETE`.

## What would make us revisit this

```
Trigger:   coverage_gap exceeds 5,000,000 rows, or the p95 of
           IngestRegistry.open_gaps exceeds 250ms
Observed:  the standing data-quality job's row-count report, and the
           `data.registry.open_gaps` span duration once #97 lands span contracts
Then:      Open a superseding ADR for gap archival, following the audit
           partition-detach pattern rather than adding a delete path -- the
           terminal bounds of an archived segment stay in the live table, or
           archiving becomes indistinguishable from truncation
```

## Verification

```
Claim:         Marking and narrowing preserves the ability to identify a
               backtest that consumed a range while it was holed, at a cost of
               one extra row per partial repair and one predicate on three reads
Confirmed if:  by 2027-02-01, every coverage_gap row that has ever existed is
               still present, `SELECT count(*) FROM coverage_gap WHERE
               resolved_at_utc IS NOT NULL` is non-zero, and no row's
               discovered_at_utc has changed since it was inserted
Refuted if:    a residual row is found carrying a discovery instant later than
               the gap it narrowed, or any query in src/fking reads coverage_gap
               without the unresolved predicate and is used to decide
               availability, or the DELETE grant is restored to fking_ingest
Checked by:    the data-engineer and compliance agents, via `make check` --
               tests/data/test_seam_reconciliation.py asserts the inherited
               discovery instant, the delete refusal under both the grant and
               the trigger, and that a resolution is written once
Review date:   2027-02-01
```

## Definition of done

- [x] `number` is the next unused value in `docs/adr/` and the filename matches `NNNN-<kebab-slug>.md`
- [x] Context names one constraint that forces a decision
- [x] Decision is one paragraph, active voice, and names the owning module
- [x] The strongest rejected alternative is argued at its strongest, and the part of it that was correct — that a shrinking update preserves the discovery instant — is adopted, by carrying that instant onto the residual rows
- [x] "Do nothing" is costed
- [x] All three Consequences lists are non-empty, including what we now cannot do
- [x] The revisit trigger is observable without judgement and names where it is observed
- [x] Verification states both a confirming and a refuting value, with a date and an owner
- [x] Linked from #28
