---
number: 0010
title: Docker Compose on a single node, not Kubernetes
date: 2026-08-03
status: accepted
deciders: [ismetcahangirov, architect, devops, infrastructure]
supersedes: null
superseded_by: null
related_issues: ["#14", "#16", "#107"]
related_adrs: [ADR-0001, ADR-0003]
---

## Context

The stack is one Python process plus Postgres/TimescaleDB, Redis, the OpenTelemetry Collector, Prometheus, Loki, Tempo, Grafana and the Next.js dashboard. Something has to start them in the right order, keep them running, and be the same on the development machine as in the demo runtime.

```
Forces:
- One machine, one developer, zero budget. There is no cluster to schedule
  onto and no second node to fail over to.
- The system runs unattended for long periods, so containers must restart on
  failure and start in dependency order -- the application must not open a
  connection to a Postgres that is accepting TCP but has not finished
  recovery.
- Local development and the demo runtime must be the same topology, or a
  defect found in one is not reproducible in the other.
- Postgres holds the audit tables and the archive holds hours of
  checksum-verified data. Both live in volumes whose accidental deletion is
  unrecoverable.
- The mode that places orders should not be the mode you get by accident.

The constraint that forces a decision now:
#14 lays down the compose baseline, and every service added afterwards --
observability (#99), the API, the dashboard -- is written against whatever
orchestrator this chooses. Migrating a stateful stack later is a data move,
not a config change.
```

## Decision

**We orchestrate the stack with Docker Compose on a single node, with health-gated startup ordering (`depends_on: condition: service_healthy`), explicit restart policies, and named volumes for every stateful service.** `docker-compose.yml` plus `docker-compose.override.yml` is the developer stack and is what `make up` starts; the demo runtime — the mode that can place orders — must be asked for by name with `-f docker-compose.yml -f docker-compose.demo.yml`, so it is never what you get by accident. No `make` target removes volumes, and there deliberately is not one.

## Alternatives considered

### Alternative 1 — Kubernetes, single-node via k3s or kind (strongest rejected)

**What it would have given us.** More than the usual summary admits. Liveness and readiness probes are a genuinely better model than Compose's healthchecks — readiness gates traffic while liveness restarts, and Compose conflates them. Resource requests and limits are declarative and enforced per container, which is the exact mechanism ADR-0001's revisit trigger cares about: a backtest sweep that would OOM the box gets killed as a pod instead of taking the order manager with it, and #109's memory budget becomes a manifest line rather than a hope. Secrets are first-class objects rather than files and environment variables. CronJobs would replace part of the scheduler. And it is the industry default, so the operational knowledge transfers.

**Why it lost.** The costs land on the two things this project has least of: one developer's attention, and one machine's memory. k3s reserves a meaningful slice of RAM for the control plane before a single workload starts, on a host that already runs Postgres, Redis, four observability services and a Python process that ADR-0001 identifies as the memory risk. That is capacity spent on scheduling for a scheduler with exactly one node to schedule onto.

The attention cost is the decisive one. Kubernetes replaces one file with a set of manifests, a package manager to template them, a `PersistentVolumeClaim` and `StorageClass` for each stateful service, an ingress for the dashboard, and its own upgrade path. Every one of those is a thing that can break at 03:00 while nobody is watching, in a system whose failure modes are supposed to be about markets. The debugging surface widens in the same motion: a container that will not start is now a pod that will not schedule, which is a different and larger question.

The scheduling benefits also have no claimant. There is no horizontal scaling (one process, ADR-0001), no rolling deployment worth the machinery (one node, brief downtime is acceptable for a demo system), and no service mesh. What remains is the resource-limit argument, which is real — and which Compose answers adequately with `deploy.resources.limits` and `mem_limit`, at a fraction of the cost.

**What survives the rejection, and is adopted.** Two things are taken deliberately. Health-gated ordering is kept and is not optional: `depends_on: condition: service_healthy` with real healthchecks, because a Postgres accepting TCP while still recovering is the failure Compose's default `depends_on` walks straight into. And per-service memory limits are set explicitly, so the sweep-versus-live-loop contention that motivated the Kubernetes case is bounded here too — the mechanism is weaker, but the property is not abandoned.

### Alternative 2 — no containers: systemd units and locally installed services

**What it would have given us.** The lowest possible overhead — no container runtime, no image layers, no volume indirection, and Postgres running on the host filesystem at full speed with no storage driver in the path. systemd is a mature supervisor with restart policies, dependency ordering and journal integration, and it is already running.

**Why it lost.** Reproducibility. `uv.lock` pins the Python dependencies, but Postgres 16 with the exact TimescaleDB extension version, the Collector, Loki and Tempo would all become host state — installed once, upgraded by the distribution on its own schedule, and different on CI's runner than on the development machine. CI already runs `uv sync --frozen` to guarantee the same interpreter and packages; leaving the services unpinned would mean a defect reproducible on one machine and not the other, and the first suspect would be the code. Containers make the service versions part of the repository, which is the same argument the lockfile already won.

### Alternative 3 — do nothing (start services by hand)

```
Cost of the status quo: every developer-machine start is a manual sequence
with an ordering constraint that is invisible until it is violated -- the
application connecting to a Postgres mid-recovery fails in a way that looks
like a code defect. #14 is blocked, and with it #99, #103 and the demo
runtime. Unattended operation is impossible: nothing restarts anything.
Why that is no longer payable: unattended operation is the point of the
system, and "restarts on failure" is the minimum property that makes it
possible.
```

## Consequences

**What becomes easier**
- `make up` is the whole stack, in the right order, with the same service versions everywhere including CI.
- Startup ordering is a correctness property rather than a habit: nothing connects to a dependency that has not reported healthy.
- Adding a service is a compose block, not a manifest set — which matters because the observability stack (#99) adds four at once.
- The demo runtime requires typing its file explicitly, so the order-placing mode cannot be entered by muscle memory.

**What becomes harder**
- Resource isolation is advisory rather than scheduled. Compose limits bound a container, but nothing rebalances, and a runaway backtest degrades the whole host rather than being evicted.
- There is no rolling deployment: updating the application means brief downtime, so any migration must be backward-compatible across the restart window or the restart is an outage.
- Single node means single point of failure for everything at once — one disk, one kernel, one power supply. #114 (backup, restore, rehearsed drill) exists because this decision makes recovery the only redundancy.

**What we now cannot do**
- Run any component on a second machine without changing orchestrator. Reopening that means a real cluster and, more importantly, a network between components that currently share a process and a memory space (ADR-0001) — the orchestration change is the smaller half of that move.

## What would make us revisit this

```
Trigger:   The host is saturated -- sustained memory above 85% or load average
           above core count for three consecutive days -- with the workload
           already tuned, OR any component must run on a second machine.
Observed:  Grafana panels `node.memory_used_ratio` and `node.load1`.
Then:      Open a superseding ADR. Note that the honest first move is a bigger
           machine, not a scheduler: a cluster of one node solves nothing that
           more RAM does not solve more cheaply.
```

## Verification

```
Confirmed if:  `make up` brings the full stack healthy on a clean checkout, on
               both the development machine and CI, with zero manual
               intervention, on every attempt through 2027-02-01
Refuted if:    the saturation trigger fires, or any service is started outside
               Compose to work around an ordering or resource problem, or a
               volume is lost to an orchestration command
Checked by:    devops agent, via `make up` in CI and the compose contract test
               in tests/infra/
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
- [x] Linked from #16 and from `.claude/knowledge/decisions-log.md` (D-026)
