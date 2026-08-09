---
number: 0004
title: Redis Streams as the event bus, with idempotent consumers, instead of Kafka
date: 2026-08-03
status: accepted
deciders: [ismetcahangirov, architect, cto]
supersedes: null
superseded_by: null
related_issues: ["#18", "#16"]
related_adrs: [ADR-0001, ADR-0003, ADR-0010]
---

## Context

Every arrow in the core data flow crosses a module boundary and emits an event: bar ingested, signal emitted, order approved or rejected, order submitted, fill received, strategy retired. Those events carry the correlation ID that makes a trade reconstructable months later (`ARCHITECTURE.md` §3, §11).

```
Forces:
- Events must survive a process restart. A fill event lost because the consumer
  was redeploying is a position the books do not know about.
- Consumers must be able to resume from where they stopped, and a consumer that
  died mid-message must have that message redelivered to a live one.
- The whole system is one process on one machine (ADR-0001), so the bus is
  mostly an in-process decoupling mechanism with durability, not a network
  transport between services.
- Throughput is small and bounded by decisions per second, not by ticks per
  second: a handful of symbols on minute bars, tens of events per minute.
- Redis is already in the stack for caching and locks, so it is a running
  daemon either way.
- Retention has to be finite on one disk, and the audit trail -- not the bus --
  is what has to survive for years (docs/rules/append-only-audit.md).

The constraint that forces a decision now:
#18 builds the bus, and every consumer in the project is written against its
delivery semantics. Idempotency is a design constraint on each consumer
(CLAUDE.md 2), so the semantics have to be fixed before any consumer exists.
```

## Decision

**We use Redis Streams as the event bus, with one consumer group per logical consumer, `XAUTOCLAIM` for reclaiming stranded messages, and `XACK` only after the effect and its `processed_events` row have committed in one Postgres transaction.** Delivery is at-least-once and every consumer is idempotent by design, keyed on a hash of the event's semantic content rather than on the stream message id (`docs/rules/idempotency.md`). The bus is a transport with bounded retention; durable history lives in the append-only audit tables (ADR-0003), not in the stream.

## Alternatives considered

### Alternative 1 — Kafka, or Redpanda as a drop-in (strongest rejected)

**What it would have given us.** Kafka is the reference answer for exactly this problem and the reasons are good ones. Retention is measured in weeks or months rather than in whatever fits in RAM, so replaying a month of events to rebuild a projection is a routine operation rather than a data-recovery exercise — and this system genuinely wants that, because the evolution engine re-derives scores from history. Consumer-group offset management is battle-tested against every failure mode a decade of production has found. Partitioning gives ordered parallelism per key, so per-symbol ordering would come free rather than being a property of having one partition. The ecosystem is enormous, and Redpanda removes the ZooKeeper/KRaft objection while keeping the protocol.

**Why it lost.** Retention is the strongest part of that case and it is answering a requirement this system does not have. `ARCHITECTURE.md` §11's guarantee is that a trade is reconstructable **from the audit log**, months later, with no access to application memory — and the audit log is a partitioned, hash-chained, append-only Postgres table with parquet cold archival (`docs/rules/append-only-audit.md`). If reconstruction depended on the bus, the audit tables would be redundant; since it does not, long bus retention buys a capability that is already owned elsewhere. Paying Kafka's operational cost for it is paying twice.

That cost is the decisive part. A broker on one machine is a JVM (or a second Rust daemon), its own disk budget competing with the archive and Postgres, its own memory reservation competing with the backtest process that ADR-0001 already flags as the memory risk, its own upgrade path, and its own failure mode to diagnose unattended at 03:00. Against that: throughput here is bounded by *decisions* per second across a handful of symbols on minute bars. Redis Streams is not close to its limit at that volume, and Redis is already running for caching and locks — so the marginal cost of using it as the bus is one library and zero daemons.

The partitioning argument is real but is not needed yet. Per-symbol ordered parallelism matters when one partition cannot keep up; here one consumer keeps up by orders of magnitude. Buying it now is speculative capacity with a permanent operational bill.

**What survives the rejection, and is adopted.** Kafka's consumer-group model — explicit acknowledgement, a pending-entries list, and reclaim of messages stranded by a dead consumer — is the part that actually matters, and Redis Streams implements the same shape: `XREADGROUP`, the PEL, `XAUTOCLAIM`, `XACK`. The design copies it deliberately rather than inventing a lighter protocol. The retention gap is closed on purpose by the audit substrate, not ignored.

### Alternative 2 — Postgres as the queue (`LISTEN`/`NOTIFY`, or `SELECT ... FOR UPDATE SKIP LOCKED`)

**What it would have given us.** One fewer moving part, and one enormous advantage the chosen design does not have: the effect and the acknowledgement would be **the same transaction**, in the same database, so the `processed_events` table and the `XACK`-after-commit ordering would both become unnecessary. The entire class of "committed the effect, crashed before acking" would not exist. Retention would be as long as disk allows, and replay would be a `SELECT`.

**Why it lost.** It puts a polling workload on the instance that also serves the live loop's as-of feature reads and holds the audit tables — the one component ADR-0003 deliberately protects from competing load. `SKIP LOCKED` polling at a low interval is wasted transactions all day; at a high interval it is latency in the order path. `LISTEN`/`NOTIFY` avoids the polling but its payloads are ephemeral: a notification issued while no listener is connected is simply gone, so durability has to be rebuilt with a table anyway, at which point the polling is back. Redis is already running, and moving queue churn off the transactional store keeps the store's tuning about one workload rather than two.

The transactional-acknowledgement advantage is genuine and is the sharpest objection to the chosen design. It is answered rather than dismissed: the `processed_events` row and the effect commit in **one** Postgres transaction, and `XACK` happens strictly after that commit — so the residual failure window degrades to a duplicate delivery, which every consumer is required to absorb, rather than to a lost event.

### Alternative 3 — do nothing (direct function calls, no bus)

```
Cost of the status quo: in-process calls have no durability boundary, so a
crash between "fill received" and "position updated" loses the event with no
redelivery. #18 is blocked, and with it correlation propagation across module
boundaries -- which is the mechanism ARCHITECTURE.md 11 rests on.
Why that is no longer payable: the system runs unattended and restarts are
routine. An event delivery model that assumes the process survives is an
assumption the deployment contradicts weekly.
```

## Consequences

**What becomes easier**
- No new daemon: the bus rides the Redis that the stack already runs, so the Compose file, the backup story and the supervision story are unchanged.
- Consumer-group mechanics are close enough to Kafka's that the code shape transfers if the trigger below ever fires — `XAUTOCLAIM` maps to a rebalance, the PEL maps to uncommitted offsets.
- `XAUTOCLAIM` makes duplicate delivery the *normal* case rather than an edge case, so the idempotency machinery is exercised on every restart instead of first running during an incident.

**What becomes harder**
- Every consumer must be idempotent, with a stable content-derived key and a `processed_events` claim in the same transaction as its effect. That is real work per consumer and it cannot be skipped, which is why it is a repository rule with its own replay harness rather than a convention.
- Ordering guarantees are per-stream only, and `XAUTOCLAIM` breaks even that — a reclaimed message arrives after messages published since. Any effect that overwrites state carries a monotone `venue_seq` and refuses to move backwards.
- Retention is bounded by memory, so the stream is not a replay log. Rebuilding a projection reads the audit tables, not the bus.

**What we now cannot do**
- Replay an arbitrary window of history from the bus. Reopening that means either Kafka's disk-backed retention or a durable event table in Postgres — and the latter is Alternative 2, whose cost is the transactional store's tuning budget.

## What would make us revisit this

```
Trigger:   Sustained stream memory above 2 GB, OR consumer lag on any group
           exceeding 60 s for three consecutive days, OR a demonstrated need
           to replay more than 24 h of events that the audit tables cannot
           serve.
Observed:  Grafana panels `redis.memory_used_bytes` and
           `bus.consumer_lag_seconds` by group.
Then:      Re-open the Kafka/Redpanda comparison in a superseding ADR, with
           the consumer-group code shape as the migration argument.
```

## Verification

```
Confirmed if:  the double-delivery replay harness passes for every recorded
               scenario on every merge, and zero incidents in
               docs/postmortems/ are attributed to a lost or double-applied
               event, measured by 2027-02-01
Refuted if:    any consumer is found without a processed_events claim, or any
               XACK is reachable inside an open transaction, or the lag or
               memory trigger fires
Checked by:    observability agent, via tests/platform/bus/test_replay.py and
               the AST check on XACK placement
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
- [x] Linked from #16 and from `.claude/knowledge/decisions-log.md` (D-011, D-017)
