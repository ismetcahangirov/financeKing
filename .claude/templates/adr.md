# Template — Architecture Decision Record

Copy this file to `docs/adr/NNNN-<kebab-slug>.md`, where `NNNN` is the next unused four-digit number in `docs/adr/` (zero-padded, never reused). Example: `docs/adr/0014-funding-residual-feature-store-keying.md`.

**An accepted ADR is immutable.** You do not edit it to reflect a change of mind, and you do not delete it when it turns out to be wrong. You write a new ADR that supersedes it, set `status: superseded by ADR-NNNN` on the old one — that status line is the single permitted post-acceptance edit — and leave both in the tree. The record of paths this project rejected, and why, is worth more than the record of paths it took, because the rejected ones are the ones someone will propose again next quarter.

Related: `../rules/module-boundaries.md`, `../knowledge/decisions-log.md`, `CLAUDE.md` §13.

---

```yaml
---
number: NNNN
title: <Imperative noun phrase naming the thing decided, not the problem. "Key feature-store normalization on (market, date)", not "Timestamp problems".>
date: <yyyy-mm-dd, the date of the status transition to accepted>
status: <proposed | accepted | superseded by ADR-NNNN>
deciders: [<human usernames and/or agent names that held authority over this call>]
supersedes: <ADR-NNNN, or null>
superseded_by: <ADR-NNNN, or null>
related_issues: [<#N, #N — the issues this decision unblocks or closes>]
related_adrs: [<ADR-NNNN — decisions this one leans on but does not replace>]
---
```

---

## Context

*Describe the forces in tension and name the single constraint that made a decision necessary rather than optional. A good Context lets a reader who has never seen the codebase predict the shape of the answer before they read it. Do not describe the solution here. If nothing in this section forces a choice, you are documenting a preference, not a decision, and you should close the ADR.*

```
Forces:
- <force 1: a requirement, cost, or property we want>
- <force 2: a requirement, cost, or property we want that conflicts with force 1>
- <force 3>

The constraint that forces a decision now:
<one sentence — what breaks, or what stays blocked, if we do not choose>
```

> Example: Spot archives switched to microsecond timestamps on 2025-01-01 while futures stayed in milliseconds. A single global unit constant parses one of the two markets into 1970 or into the year 57000, and neither raises. Ingestion cannot proceed until unit resolution has an owner.

---

## Decision

*One paragraph, active voice, present tense, stated as a thing this project does. "We normalize timestamps keyed on `(market, date)`" — not "it was decided that timestamps should probably be normalized". Name the module that owns the behaviour and the type or function that expresses it. If the paragraph needs an "and also", you probably have two decisions and should split the ADR.*

```
We <verb> <object>, implemented in `src/fking/<module>/<file>.py` as <type or function name>.
<One sentence on the scope of the decision — what it covers and what it explicitly leaves alone.>
```

> Example: We resolve timestamp units per `(market, date)` from a declared table in `src/fking/data/normalize.py`, and `parse_archive()` raises `UnknownUnitEpochError` for any pair not in the table rather than guessing. Ingestion of an unlisted market fails at load rather than producing plausible garbage downstream.

---

## Alternatives considered

*One subsection per alternative. The strongest rejected alternative goes first and must be argued at its full strength before you reject it — write the paragraph its most capable advocate would write, then say the specific thing that beats it. If your strongest alternative reads as obviously bad, you have written a straw man and this section is worthless; the whole point of the ADR is that a future reader can tell whether the rejected path was rejected for a reason that still holds.*

### Alternative 1 — <name> (strongest rejected)

**What it would have given us.** *<Two to four sentences, stated as an advocate. Concrete benefits, named.>*

**Why it lost.** *<The specific, checkable reason. A measurement, a constraint from CLAUDE.md §2, a cost we cannot pay. Not "it felt heavyweight".>*

> Example — rejected alternative "adopt NautilusTrader as the backtest core": it would have given us an event-driven engine with a Rust hot path, maintained by people who do this full time, and roughly six weeks of engine work we would not have to do or debug. It lost because adopting it means adopting its domain model: the risk engine becomes a plugin to its lifecycle rather than the sole authority over order construction, and `ARCHITECTURE.md` §5 makes that authority structural.

### Alternative 2 — <name>

**What it would have given us.** *<Same shape.>*

**Why it lost.** *<Same shape.>*

### Alternative 3 — do nothing

*Always argue this one. State what the status quo costs per week or per incident, and why that cost is no longer payable. If "do nothing" is cheap, say so and reconsider the ADR.*

```
Cost of the status quo: <measurable — hours per week, incidents per month, blocked issues>
Why that is no longer payable: <the trigger>
```

---

## Consequences

*Three lists, all non-empty. If "what becomes harder" is empty you have not understood the decision yet — every real architectural choice trades something away. Be specific enough that a reader can check each claim against the code six months from now.*

**What becomes easier**
- <concrete capability we now have, or work we no longer do>
- <second>

**What becomes harder**
- <concrete cost we now pay on every change of a named kind>
- <second>

**What we now cannot do**
- <a capability this decision forecloses, and what reopening it would cost>

> Example — under per-`(market, date)` unit resolution, onboarding a new venue now requires a declared unit row before the first byte is ingested, and there is no way to bulk-load an unrecognized archive "just to look at it".

---

## What would make us revisit this

*A named, observable trigger with a threshold and a place it is observed. "If requirements change" is not a trigger. A trigger a monitoring system or a scheduled review could evaluate without judgement is a trigger; anything else is a hope.*

```
Trigger:   <observable condition with a number>
Observed:  <the dashboard, metric name, alert, or scheduled review that surfaces it>
Then:      <the action — open a superseding ADR, re-benchmark, escalate>
```

> Example: Trigger — median ingestion latency for a full day of futures klines exceeds 90s for three consecutive days. Observed — Grafana panel `data.ingest.duration_seconds` p50. Then — re-open the DuckDB-versus-Postgres-hypertable comparison in a superseding ADR.

---

## Verification

*How we will know the decision was right, by when, and who checks. A decision without a verification date is a decision that never gets graded, and ungraded decisions accumulate. Name the metric and the value that counts as confirmation, plus the value that counts as refutation — they are not simply complements.*

```
Confirmed if:  <metric> <comparator> <value>, measured by <date>
Refuted if:    <metric> <comparator> <value>, measured by <date>
Checked by:    <human or agent>, via <command or dashboard>
Review date:   <yyyy-mm-dd>
```

> Example: Confirmed if zero timestamp-unit defects reach `docs/postmortems/` in the six months to 2027-02-01. Refuted if any archive parses without error into a year outside 2017-2030. Checked by the `data-engineer` agent via `make test -k test_normalize_units`. Review date 2027-02-01.

---

## Definition of done

- [ ] `number` is the next unused value in `docs/adr/` and the filename matches `NNNN-<kebab-slug>.md`
- [ ] Context names one constraint that forces a decision, not a list of preferences
- [ ] Decision is one paragraph, active voice, and names the owning module
- [ ] The strongest rejected alternative is argued at its strongest, and a reader could disagree with the rejection on its merits
- [ ] "Do nothing" is costed with a number
- [ ] All three Consequences lists are non-empty, including what we now cannot do
- [ ] The revisit trigger is observable without judgement and names where it is observed
- [ ] Verification states both a confirming and a refuting value, with a date and an owner
- [ ] If this supersedes an earlier ADR, that ADR's `status` and `superseded_by` are updated and nothing else in it is touched
- [ ] Linked from the issue it resolves, and from `../knowledge/decisions-log.md`
- [ ] `make check` is green on the branch carrying this file
