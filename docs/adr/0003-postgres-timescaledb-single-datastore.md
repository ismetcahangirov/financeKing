---
number: 0003
title: PostgreSQL 16 with TimescaleDB as the single operational datastore, Parquet for bulk scans
date: 2026-08-03
status: accepted
deciders: [ismetcahangirov, architect, database]
supersedes: null
superseded_by: null
related_issues: ["#17", "#24", "#16"]
related_adrs: [ADR-0001, ADR-0010]
---

## Context

The system stores four things with different access shapes: relational state (strategies, orders, positions, lifecycle), append-only audit rows that must survive years, high-cardinality time series (bars, features, funding), and years of historical archive scanned end to end by every backtest.

```
Forces:
- Audit rows must be append-only, enforced by the database rather than by the
  application (CLAUDE.md 2). That requires triggers, revoked grants and row-level
  constraints -- a relational engine, not a document store or a log.
- The operational time series is small and query-shaped: "the last 500 bars for
  this symbol as of this instant", asked constantly by the live loop.
- The archive is the opposite: 3+ years of 1-minute bars per symbol, scanned
  in full, repeatedly, by every walk-forward and CPCV run. Row-store overhead
  dominates there.
- Feature reads are point-in-time and are enforced by a SECURITY DEFINER
  function the application role can execute but cannot bypass
  (.claude/rules/no-lookahead.md). That mechanism is Postgres-specific.
- One machine, zero budget, unattended operation. Every additional server is a
  process to supervise, a backup to rehearse and a version to upgrade.

The constraint that forces a decision now:
#17 writes the schema, the migrations and the audit substrate. Every table in
the project is created against whatever this decides, and migrating an
append-only audit table to a different engine later is not a migration -- it
is a data-provenance break.
```

## Decision

**We run one PostgreSQL 16 instance with the TimescaleDB extension as the single operational datastore, and additionally write bulk historical bars to partitioned Parquet on local disk, queried in-process by DuckDB.** Relational state, audit tables and operational time series are Postgres — hypertables where the access pattern is time-ranged, plain tables otherwise. Parquet is a derived, regenerable cache for backtest scans and is never a source of truth: nothing writes to Parquet that did not first pass the data-quality gate, and no operational read path depends on it. Neither component requires a server we do not already run in the Compose stack (ADR-0010).

## Alternatives considered

### Alternative 1 — Postgres for relational state plus ClickHouse for time series (strongest rejected)

**What it would have given us.** ClickHouse is the right tool for this shape of data by a wide margin. Columnar storage with per-column codecs typically compresses OHLCV five to ten times better than a Postgres heap; `DoubleDelta` on timestamps and `Gorilla` on prices are built for exactly this. Full scans over years of minute bars — the operation every walk-forward run performs dozens of times — are where it is fastest, and it does aggregations across symbols without the planner gymnastics a hypertable needs. It is genuinely free, self-hostable, and would remove the pressure that #109 exists to relieve. Splitting by access shape rather than forcing one engine to do both is sound engineering, not premature optimisation.

**Why it lost.** The split does not fall where the argument assumes. The time series that has to be *fast* is the archive, and the archive is immutable, regenerable, and read by exactly one consumer — the backtest engine, in-process. That is a file-format problem, not a database problem, and Parquet plus DuckDB solves it with **no server at all**: no second daemon to supervise, no second backup to rehearse (#114), no second connection pool, no second failure mode at 03:00. Taking ClickHouse means taking its operational cost to solve a problem an embedded reader already solves.

The time series that has to be *correct* is the operational one — feature values with `event_time` and `available_at`, read as-of. Its correctness rests on machinery ClickHouse does not have: a `SECURITY DEFINER` function that the application role can execute while holding no `SELECT` on the underlying table, so a look-ahead leak is `permission denied` rather than a review miss (`.claude/rules/no-lookahead.md`). That is the strongest guarantee in the data platform and it is Postgres-specific. Moving features to ClickHouse would trade an enforced invariant for read throughput on a query that returns 500 rows.

Third, the audit substrate settles it. Append-only is enforced by revoked grants, a `BEFORE UPDATE OR DELETE` trigger, and a per-row hash chain computed inside the database at insert (`.claude/rules/append-only-audit.md`). ClickHouse has no row triggers and no per-row grant model of that kind. An audit log the application layer promises not to rewrite is not an audit log.

**What survives the rejection, and is adopted.** The columnar argument is correct about the archive, and it is adopted in full — that is what the Parquet plus DuckDB half of this decision is. The rejection is of the *second server*, not of columnar storage.

### Alternative 2 — Postgres alone, archive in hypertables too

**What it would have given us.** One storage technology, one query language, one backup, one place to look. Timescale compression on old chunks is real and would shrink the archive substantially. Backtests would read through the same connection as everything else, and the "regenerate Parquet" step in the pipeline would not exist.

**Why it lost.** It puts the heaviest scan in the project through the process that must stay responsive to the live loop. A CPCV run scanning three years of minute bars for a symbol competes for the same buffer cache, the same connection pool and the same CPU as the order path, on one machine. The scan also crosses a network socket and the wire protocol to reach data that is on the same disk — DuckDB reads the same bytes with no serialisation. And decompressing Timescale chunks for a full scan is slower than reading columnar files that were laid out for it. The operational cost is not zero either: chunk and compression policy for a 40-million-row-per-symbol table is real tuning work, against a workload that never updates a row.

### Alternative 3 — do nothing (SQLite, or files, for now)

```
Cost of the status quo: #17 blocked, and with it #18, #29 and every issue that
persists anything. SQLite specifically cannot express the audit guarantee --
no roles, no per-table grants -- so choosing it would mean rewriting the audit
substrate before the first real trade rather than after.
Why that is no longer payable: the audit substrate is P0 work precisely
because retrofitting provenance onto rows that were written without it is
impossible, not merely expensive.
```

## Consequences

**What becomes easier**
- One connection string, one migration tool, one backup and restore drill (#114), one thing to supervise. `make up` brings the whole data layer online.
- Audit immutability is enforceable at the strongest available level: revoked grants, row triggers and a hash chain the application cannot compute for itself.
- Point-in-time feature reads are enforced by grants rather than by review, so a look-ahead leak fails as a permissions error in CI.
- Backtest scans do not touch the operational database at all, so a sweep cannot slow the order path through the datastore.

**What becomes harder**
- Parquet is a second copy of the same data and must be kept derived rather than authoritative. The regeneration path has to exist, be tested, and be run whenever normalisation changes — otherwise the archive silently encodes an old parser's beliefs.
- Cross-store queries are the developer's problem: joining a feature series to an order history means pulling both into the process. There is no engine that sees both.
- Timescale is an extension, so the Postgres version we can run is bounded by what Timescale supports. Postgres upgrades are gated on that compatibility.

**What we now cannot do**
- Ask an ad-hoc analytical question across the full archive *and* live operational state in one SQL statement. Reopening that means either loading the archive into Postgres (Alternative 2's cost) or replicating operational state into DuckDB (a synchronisation problem with no owner).

## What would make us revisit this

```
Trigger:   The operational Postgres instance exceeds 200 GB on disk, OR p99
           latency of the as-of feature read exceeds 50 ms for three
           consecutive days.
Observed:  Grafana panels `postgres.database_size_bytes` and
           `data.feature_as_of.duration_seconds` p99.
Then:      Re-open the columnar-store comparison in a superseding ADR, with
           the audit tables explicitly excluded from any migration.
```

## Verification

```
Confirmed if:  the operational store needs no manual tuning intervention and
               no schema-level rescue between now and 2027-02-01, and the
               restore drill (#114) succeeds on every rehearsal
Refuted if:    the size or latency trigger above fires, or any operational
               read path is found depending on Parquet
Checked by:    database agent, via the #114 restore rehearsal and the
               `make check` integration tests against real Postgres
Review date:   2027-02-01
```

## Definition of done

- [x] `number` is the next unused value in `docs/adr/` and the filename matches `NNNN-<kebab-slug>.md`
- [x] Context names one constraint that forces a decision
- [x] Decision is one paragraph, active voice, and names the owning module
- [x] The strongest rejected alternative is argued at its strongest, and the part of it that was correct is adopted rather than discarded
- [x] "Do nothing" is costed
- [x] All three Consequences lists are non-empty, including what we now cannot do
- [x] The revisit trigger is observable without judgement and names where it is observed
- [x] Verification states both a confirming and a refuting value, with a date and an owner
- [x] Linked from #16 and from `.claude/knowledge/decisions-log.md` (D-009, D-010)
