---
number: 0019
title: The system beat is built here, on an anchored fire-time lattice and a Postgres run ledger, rather than on APScheduler
date: 2026-08-05
status: accepted
deciders: [ismetcahangirov, architect, scheduler, cto]
supersedes: null
superseded_by: null
related_issues: ["#108", "#66", "#95", "#71"]
related_adrs: [ADR-0001, ADR-0003, ADR-0010]
---

## Context

`ARCHITECTURE.md` §12 names **APScheduler + GitHub Actions cron** in the technology table, chosen against Temporal on the grounds that Temporal needs its own server and database. That comparison was between orchestration platforms and it is still right. What it did not consider is whether APScheduler's *scheduling semantics* match what this system's jobs need, because at the time no job existed to check them against.

#108 is where they get checked. It names three recurring jobs and three different answers to the same question — what happens to the fires that elapsed while the process was down:

- **Gap detection** re-scans a window that ends now. Six catch-up runs produce five duplicate findings. Run **once**.
- **Hourly ingestion** covers a distinct window per fire. Skipping five leaves five holes that nothing later will notice, because "the last run succeeded" is the only state most catch-up logic checks. Run **all six, in ascending order**.
- **Reconciliation** converges to current exchange state. Run **once, stamped now** — a run labelled 04:00 that read the exchange at 10:00 is a record that will be misread later.

```
Forces:
- APScheduler's misfire model is `coalesce` plus `misfire_grace_time`. It can
  express "one run" and "every fire inside a grace window", and it cannot
  express "one execution per missed window, in order, however far behind we
  are" -- grace is a duration, not a count, and coalesce=False plus a wide
  grace fires them without a defined order and requires max_instances > 1,
  which is the opposite of the overlap property #108 also requires.
- Its persistent job stores are synchronous SQLAlchemy. This project has one
  PostgreSQL driver, asyncpg, and platform/persistence/engine.py says why: the
  migration path and the application path must agree about numeric and
  timestamptz, and a second driver is a second set of type coercions on the
  one path whose whole job is to agree.
- APScheduler 4.x is async-native and asyncpg-capable, and has been
  pre-release for years. The process that will drive reconciliation (#66) and
  audit chain verification (#95) is not where a pre-release dependency
  belongs.
- Its job store persists job *definitions*. Here the schedule is code. A
  stored row naming a callable that no longer exists is a startup failure with
  no diff to review, recovered by hand-editing a database row.
- Whichever library runs the timer, the catch-up ledger has to exist: "which
  fire times has this job already covered" is not derivable from a next-run
  timestamp, and it is the substantial half of the work.

The constraint that forces a decision now:
#108 cannot register its first job until missed-run behaviour is expressible
per job, and #66 and #95 are both blocked behind it. Whatever shape that takes
is the shape every later job copies.
```

## Decision

We build the beat in `fking.platform.scheduler`: an anchored interval schedule (`anchor_utc + k * period`, integer arithmetic), a per-job `MisfirePolicy` with no default, and a `scheduler_job_run` table whose primary key `(job_id, scheduled_fire_utc)` is the idempotency key for every execution. `SchedulerBeat.tick` resolves what is due, claims it with `INSERT … ON CONFLICT DO NOTHING`, and starts one task per job; `run_forever` holds a session-level advisory lock so exactly one beat runs against a database, and sweeps unfinished runs to `abandoned` at boot. **APScheduler is not adopted**, and `ARCHITECTURE.md` §12's row is amended to point here. The decision covers in-process scheduling only; the GitHub Actions cron tier for daily housekeeping is untouched and unbuilt, and LLM admission control stays with the quota ledger (#71) rather than moving into the beat.

## Alternatives considered

### Alternative 1 — APScheduler's `AsyncIOScheduler` with a memory job store, plus our own catch-up ledger (strongest rejected)

**What it would have given us.** This is the strongest option and it is close. It keeps the technology table honest, it needs no synchronous driver because the memory job store has no database, and it sidesteps the persisted-definitions problem entirely — jobs are registered from code at startup, which is what we want anyway. APScheduler then owns the part that is genuinely fiddly and well-tested elsewhere: the timer, `max_instances=1`, `CronTrigger`'s calendar arithmetic including month lengths and leap years, and a job store API we would not have to design. Our catch-up ledger would sit beside it and run the outage replay at startup, and steady-state scheduling would be somebody else's tested code. Roughly two hundred lines of this package would not exist.

**Why it lost.** The steady state and the outage would be two different code paths, and only one of them runs every fifteen seconds. APScheduler's timer would drive the normal case and our ledger would drive the catch-up, which means the path that has to work at 03:00 after a restart is the path that is exercised only by tests — the exact split `.claude/rules/testing-rules.md` warns about, in the component whose job is to keep reconciliation running. Building the tick as one function that computes "what is due since the cursor" makes the six-hour outage a single call with a different `now_utc`, and normal operation the case where that call returns one fire time. The second reason is smaller and still decided it: with a memory job store and a ledger cursor, APScheduler's `next_run_time` and our `max(scheduled_fire_utc)` are two answers to the same question that can disagree after a misfire, and the disagreement is silent. What we would have kept — a tested timer and calendar arithmetic — is worth less than it looks, because every job named in #108 is a fixed interval and `asyncio.sleep` plus integer floor division is the whole timer.

### Alternative 2 — APScheduler 4.x with its async asyncpg data store

**What it would have given us.** Async all the way down, one driver, a persistent store designed for exactly this, and schedules with richer trigger types than we are building. If it were stable this would be the answer.

**Why it lost.** It has been pre-release for years, and its API has changed shape across alphas. Pinning a pre-release into the process that drives reconciliation and chain verification makes an upgrade a rewrite and a security advisory a crisis. `SECURITY.md` §7's dependency posture and `CONTRIBUTING.md`'s ADR requirement for significant adoptions both point the same way: not this one, not here.

### Alternative 3 — APScheduler 3.x with `SQLAlchemyJobStore` on a synchronous driver

**What it would have given us.** The documented, supported configuration, with durability out of the box.

**Why it lost.** It requires psycopg alongside asyncpg. `platform/persistence/engine.py` states the cost: two drivers means two sets of type coercions for `numeric` and `timestamptz`, on a schema where `NUMERIC(38, 18)` and `TIMESTAMPTZ` are load-bearing and where a coercion difference surfaces as a value that is subtly wrong rather than as an error. Paying that to store a `next_run_time` we would then override with our own cursor is the worst trade on this list.

### Alternative 4 — do nothing

```
Cost of the status quo: #108 stays open, and #66 (reconciliation) and #95
                        (chain verification) stay blocked behind it, which is
                        P4 and P7's critical path. Meanwhile the two jobs that
                        already exist -- the live gap detector and the bulk
                        backfill's resume pass -- keep being started by hand,
                        so a machine that reboots overnight silently stops
                        ingesting until somebody notices.
Why that is no longer payable: #27 shipped the live path and #28 the repair
                        path. Two concrete callers exist, which is the bar
                        CLAUDE.md 3 sets for an abstraction to be built at
                        all, and the third and fourth are the two blocked
                        issues.
```

## Consequences

**What becomes easier**
- Missed-run behaviour is a declaration a reviewer reads, not a pair of tuning parameters whose interaction has to be reasoned about. `misfire_policy=RUN_EVERY_MISSED, max_catch_up_runs=24` states both the intent and its bound on one line.
- A six-hour outage is a unit test. `due_fire_times` is pure, so all three policies are asserted in milliseconds without a clock, a database or a restart.
- Restart safety is a unique index. "A restart does not re-fire a window that already ran" needs no code path to stay correct, and a second writer racing us loses at the primary key rather than by convention.
- Job identity survives a rename of anything but the job. `(job_id, scheduled_fire_utc)` is stable across processes because the fire times are anchored, so a run in the ledger means the same window it meant last year.

**What becomes harder**
- We own the timer. `asyncio.sleep` between ticks means a fire time is honoured within one tick interval rather than exactly, and a job that needs sub-second precision has nothing here to reach for.
- No cron expressions. A schedule like "the first Monday of each month" is not expressible on an interval lattice, and adding it means either a dependency or a calendar implementation with its own leap-year tests.
- Two more things to keep correct that a library would have owned: the advisory-lock lifecycle, and the reap-and-re-raise that turns a job's unexpected exception into a process exit rather than an unretrieved task exception at interpreter shutdown.
- `ARCHITECTURE.md` §12 now disagrees with its own original reasoning unless a reader follows the pointer to this file, which is one more hop for anybody asking "what schedules things".

**What we now cannot do**
- Schedule anything with a deadline tighter than the tick interval. The beat's resolution is a floor, and lowering it costs one `max(scheduled_fire_utc)` per registered job per tick.
- Run two scheduler processes. The advisory lock refuses the second outright, so horizontal scale of the beat is not available and would need the boot sweep replaced by per-run leases first.
- Retry a failed window automatically. A fire time is consumed once whatever its outcome, so recovering a failed run is a deliberate backfill. That is the same trade `.claude/rules/error-handling.md` makes everywhere else and it is still a real limitation.

## What would make us revisit this

```
Trigger:   a job is proposed whose schedule cannot be written as
           anchor + k * period, or the beat's own tick latency (the p95 of the
           scheduler.job_run span's start against its scheduled_fire_utc)
           exceeds two tick intervals
Observed:  the span contracts landing in #97, and the job catalogue at review
           time
Then:      Reconsider APScheduler's triggers as a pure library -- its
           CronTrigger is a calendar implementation, not a scheduler, and
           importing it for get_next_fire_time alone would leave this
           package's ledger and policy untouched. That is a smaller change
           than it looks and it is deliberately left available.
```

## Verification

```
Claim:         A per-job misfire policy plus a claim keyed on
               (job_id, scheduled_fire_utc) gives correct outage, restart and
               overlap behaviour with no scheduling library, at a cost of
               roughly 400 lines and one table
Confirmed if:  by 2027-02-01 every job registered by #66, #95 and the data
               issues has been expressible as an IntervalSchedule, and
               scheduler_job_run holds no duplicate execution of any window --
               that is, count(*) equals count(distinct (job_id,
               scheduled_fire_utc)), which the primary key guarantees, with no
               job having worked around it by stamping fire times from a clock
Refuted if:    a job is registered whose `run` reads the wall clock instead of
               its fire time, or `max_catch_up_runs` is raised on any job to
               silence a CatchUpBacklogTooLargeError rather than to state a
               real tolerance, or a second scheduler process is introduced and
               the advisory lock is removed to allow it
Checked by:    the scheduler and cto agents, via `make check` --
               tests/platform/scheduler/ asserts the three misfire policies,
               the overlap refusal, the restart, the boot sweep and the
               single-instance lock against a real PostgreSQL
Review date:   2027-02-01
```

## Definition of done

- [x] `number` is the next unused value in `docs/adr/` and the filename matches `NNNN-<kebab-slug>.md`
- [x] Context names one constraint that forces a decision
- [x] Decision is one paragraph, active voice, and names the owning module
- [x] The strongest rejected alternative is argued at its strongest, and the part of it that was correct — that job definitions belong in code and not in a job store — is adopted
- [x] "Do nothing" is costed
- [x] All three Consequences lists are non-empty, including what we now cannot do
- [x] The revisit trigger is observable without judgement and names where it is observed
- [x] Verification states both a confirming and a refuting value, with a date and an owner
- [x] Linked from #108
