---
number: 0015
title: Enforce append-only audit in PostgreSQL with revoked grants, immutability triggers and a hash chain
date: 2026-08-03
status: accepted
deciders: [ismetcahangirov, architect, database, compliance]
supersedes: null
superseded_by: null
related_issues: ["#17", "#94", "#95", "#106"]
related_adrs: [ADR-0003, ADR-0011]
---

## Context

`ARCHITECTURE.md` §11 requires that any trade be fully reconstructable from the audit log alone, months later, with no access to application memory. That requirement is only worth what the log's trustworthiness is worth, and the log has to stay trustworthy against code that does not exist yet: this project is written largely by AI across sessions with no shared memory, and it generates its own strategies and consumers.

```
Forces:
- The audit trail must survive the application being wrong. Application-layer
  discipline is a statement about the code that exists today.
- The threat is not a malicious operator. It is a migration that grants a broad
  role to a new service, an UPDATE run during an incident to fix a malformed
  payload, and a restore from a stale dump.
- Grants alone cannot stop a superuser, and triggers alone cannot stop TRUNCATE,
  which does not fire row triggers at all.
- Neither grants nor triggers can see a doctored pg_dump/restore.
- Every mechanism costs something on every write, and audit writes sit in the
  order path.

The constraint that forces a decision now:
#17 creates the audit tables. Whatever posture they are created with is the
posture every later phase inherits, and retrofitting immutability onto a table
that already holds rows is a data migration rather than a schema change.
```

## Decision

We enforce append-only in PostgreSQL, in four layers, defined by the migrations under `migrations/versions/` and classified in `src/fking/platform/persistence/schema.py` as `APPEND_ONLY_TABLES` and `HASH_CHAINED_TABLES`: revoked `UPDATE`/`DELETE`/`TRUNCATE` grants for `fking_app` on every append-only table, a `BEFORE UPDATE OR DELETE` row trigger calling `fking_append_only_guard()`, a per-row SHA-256 hash chain on `audit_log` and `trial_ledger` computed by a database trigger from a shared digest function, and an irreversible `downgrade()` on the migration that creates those two. The decision covers what the database refuses and what it can detect; it says nothing about which events get written, which is #94.

## Alternatives considered

### Alternative 1 — enforce append-only in the repository layer (strongest rejected)

**What it would have given us.** One `AuditRepository` with an `append()` method and no `update()` or `delete()` is simple, testable without a container, portable across engines, and imposes no per-write cost at all. It reads clearly, a reviewer can verify it in thirty seconds, and it makes the intent obvious to the next author in a way a `REVOKE` buried in a migration does not. Every write in the system would go through it, because there is nothing else to call.

**Why it lost.** "Every write goes through it" is true only of the code that exists when the sentence is written. The concrete failure is not hypothetical: during an incident someone opens `psql` and runs `UPDATE audit_log SET payload = ... WHERE seq = 41822` to make a dashboard render, and the repository is not in that path at all. The docstring saying the table is append-only is a comment. Six weeks later a reconciliation discrepancy is traced back to that row, which is now wrong with no record that it was ever changed. `CLAUDE.md` §2 states the rule as "enforced by the database" for exactly this reason: an audit log the application can rewrite is not an audit log, and the application is not the only thing with a connection string.

### Alternative 2 — grants and triggers, but no hash chain

**What it would have given us.** Two layers already stop every write path the application has, and they cost nothing measurable. The chain costs an advisory lock per audit insert, which serialises audit writes across the whole process, and it adds a digest recipe that has to be kept in step with the columns. Dropping it removes the serialisation and a class of maintenance error.

**Why it lost.** Forbidding a rewrite is not the same as detecting one. A superuser, a `pg_dump`/restore from a doctored dump, or direct file access all bypass both layers, and without the chain a missing row is indistinguishable from a row that never existed — so the audit log can only ever be believed, never demonstrated. The serialisation cost is affordable because audit write rate is bounded by decisions per second, which is small here; the alternative is a log whose completeness is an assertion. `test_a_superuser_rewrite_breaks_the_chain` is what converts that assertion into a check.

### Alternative 3 — do nothing

```
Cost of the status quo: #17 cannot close. Every table it creates is a table
whose immutability posture is decided by omission, and #94, #95, #90 and #102
all write to or read from those tables.
Why that is no longer payable: retrofitting a BEFORE UPDATE OR DELETE trigger
and a hash chain onto a populated audit table means back-filling prev_hash and
row_hash for rows that were never chained -- which is itself a bulk UPDATE of
audit rows, the exact operation the mechanism exists to forbid. The window in
which this is a schema change closes the first time a row is written.
```

## Consequences

**What becomes easier**
- A correction is unambiguous: a new row whose `causation_id` points at the row being corrected. There is no second, quieter way to do it, so there is no argument about which was used.
- The reconstruction requirement becomes checkable rather than aspirational — #95's verification job re-derives `row_hash` from each row's own contents using the same `fking_audit_log_digest()` the trigger used, so a discrepancy names an exact `seq`.
- A migration that later grants a broad role to a new service cannot silently widen audit access; the trigger fires regardless of who holds what.

**What becomes harder**
- Every audit insert takes `pg_advisory_xact_lock`, so audit writes are serialised process-wide. Any future write path that batches thousands of audit rows must be designed around that, not discovered by it.
- Adding a column to a hash-chained table changes the digest input, so it needs a versioned recipe rather than an `ALTER TABLE`. Adding a *nullable* column outside the digest is fine; anything in the digest is a new chain version.
- Test fixtures cannot clean up after themselves by deleting rows. Integration tests either use unique ids and assert relatively, or take a fresh database.

**What we now cannot do**
- Back-fill or correct any historical audit or trial-ledger row, including a value that is unambiguously derivable from another table. Reopening it would mean disabling a trigger as a superuser and accepting a permanent chain break that is indistinguishable from tampering — which means the next real tampering event gets dismissed as "probably that backfill".
- Roll a deployment back past `0002_audit_substrate`. Recovering from a bad schema state below that revision means rolling forward with a new migration, or dropping the database.

## What would make us revisit this

```
Trigger:   p99 latency of an audit INSERT exceeds 50ms, or audit write throughput
           saturates above 200 rows/second sustained for one hour -- either
           indicates the advisory lock has become the bottleneck rather than an
           affordable cost.
Observed:  Prometheus `fking_audit_append_duration_seconds` p99 and
           `fking_audit_append_total` rate, on the persistence Grafana panel (#98).
Then:      Open a superseding ADR comparing per-partition chains against a
           batched Merkle root written on a timer. Do not remove the chain.
```

## Verification

```
Confirmed if:  zero rows in audit_log or trial_ledger fail digest re-derivation,
               and zero audit-integrity incidents reach docs/postmortems/, in the
               six months to 2027-02-03
Refuted if:    any audit-log row is found to have been modified after insertion,
               or the chain-verification job is muted or disabled for more than
               24 hours
Checked by:    the compliance agent, via
               `make test ARGS="tests/platform/persistence/test_audit_append_only.py"`
               and the #95 chain-verification job
Review date:   2027-02-03
```
