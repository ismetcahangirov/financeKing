# Template — GitHub Issue

Paste the body below into `gh issue create`, or keep it as a draft at `.github/ISSUE_TEMPLATE/` if you are wiring it into the GitHub UI. The issue number it receives becomes the branch name: `<type>/<issue-number>-<kebab-slug>`.

**The rule that governs this template: an issue that cannot state its verification command is not ready to be worked.** Not "not ready to be merged" — not ready to be *started*. If nobody can say what command proves the work is done, then the acceptance criteria are opinions, the reviewer has nothing to check against, and the pull request will be graded on whether the diff looks reasonable. That is how a system that is supposed to reject things ends up accepting them. Write the verification command first if you find the acceptance criteria hard to phrase; the command usually tells you what the criterion actually was.

Related: `GIT_WORKFLOW.md`, `CLAUDE.md` §2, `pull-request.md`.

---

```yaml
---
type: <feat | fix | docs | chore | refactor | test | perf | research>
priority: <P0 blocks the critical path | P1 this milestone | P2 scheduled | P3 opportunistic>
milestone: <milestone name from ROADMAP.md>
modules: [<src/fking/... paths this touches — if the list has more than three entries, say why in "Estimated size">]
blocked_by: [<#N, #N — issues that must merge first, or empty>]
blocks: [<#N — issues waiting on this one, or empty>]
assignee: <human username or agent name>
labels: [<type label, plus any of safety:critical, needs-human, breaking-change>]
---
```

---

## Problem

*State what is observably wrong or missing. Evidence, not opinion: a log line, a failing command with its output, a metric with a timestamp, a row from an audit table. If the only support is "this feels wrong", you are proposing a preference and the issue should say so honestly rather than dressing it as a defect.*

```
Observed:  <what happens, with the evidence inline>
Expected:  <what should happen>
Evidence:  <command output, log excerpt with correlation_id, dashboard link, or audit row>
```

> Example: Observed — `parse_archive()` returns bars dated 1970-01-01 for `BTCUSDT` spot files after 2025-01-01. Evidence — `duckdb -c "select min(ts), max(ts) from 'data/spot/BTCUSDT/2025-03-*.parquet'"` returns `1970-01-01 00:28:44` to `1970-01-01 00:28:45`. Expected — March 2025 timestamps.

---

## Why it matters now

*Two things: the cost of the current state, and what happens if this is not done in this milestone. "It would be nice" is not a reason to schedule work. If the honest answer is that nothing happens, mark it P3 and say so — that is a legitimate outcome and it stops the backlog from being a list of things that all claim to be urgent.*

```
Cost today:            <measurable — hours per week, blocked issues, incidents, wrong numbers>
If not done this milestone: <the specific consequence, and when it lands>
```

---

## Proposed approach

*How you would do it, in enough detail that a reviewer can disagree with the approach before the code exists. Name the modules, the types, and the order of work. If there is a plausible alternative approach, name it in one line and say why it is not the plan — that single line prevents the same debate reappearing in review.*

```
1. <step, naming the module and the type>
2. <step>
3. <step>

Alternative considered: <approach> — not chosen because <reason>.
```

---

## Contracts affected

*Anything that other code, other agents, or stored data depend on. A change to any row here needs a migration, a version bump, or a coordinated deploy, and missing one of these is how a consumer breaks silently three days later. Write "none" where nothing changes, never leave a row blank.*

| Contract | Change | Consumers affected |
|---|---|---|
| Domain types | `<type name and field change, or none>` | `<modules>` |
| Event bus schemas | `<subject and payload change, or none>` | `<consumers>` |
| DB schema | `<table and migration, or none>` | `<readers>` |
| Module boundaries | `<new import edge, or none>` | `import-linter` contract `<name>` |
| Agent input/output models | `<model and field change, or none>` | `<agents>` |
| Public API routes | `<route and shape change, or none>` | dashboard |

---

## Acceptance criteria

*Checkable statements, each with the command that verifies it. One command per criterion — a criterion whose verification is "run make check" is not specific enough to fail informatively. Include at least one criterion that fails if the work is done wrongly, not merely absent, because absence is easy to notice and wrongness is not.*

- [ ] <criterion, stated as an observable behaviour>
      `<exact verification command>`
      Expected: `<what the output looks like when this passes>`
- [ ] <criterion>
      `<exact verification command>`
      Expected: `<expected output>`
- [ ] <criterion that catches a wrong implementation, not just a missing one>
      `<exact verification command>`
      Expected: `<expected output>`

> Example: Criterion — an archive whose `(market, date)` pair is not in the unit table fails to load rather than defaulting. Command — `pytest tests/data/test_normalize.py::test_unknown_market_date_raises -q`. Expected — `1 passed`, and the test asserts `UnknownUnitEpochError`.

---

## Non-goals

*What this issue explicitly does not do. Scope creep in this repository usually arrives as a reasonable-sounding adjacent improvement noticed mid-work. Naming the adjacent things here means the pull request can decline them by reference instead of by argument.*

- <adjacent thing this does not touch> — <the issue number that covers it, or "not planned">
- <adjacent thing this does not touch> — <where it lives instead>

---

## Risks and non-negotiables touched

*Tick every item this work comes near, whether or not you intend to change its behaviour. Coming near is the point: the checklist exists to make the reviewer look. An unticked list on an issue that touches money, time, or the order path is a sign the list was not read.*

`CLAUDE.md` §2 items in scope:

- [ ] Handles prices, quantities, or monetary amounts (`Decimal`, constructed from `str`, never `float`)
- [ ] Handles datetimes (timezone-aware UTC, naive rejected at construction)
- [ ] Adds or modifies a domain object (immutable, transitions return new objects)
- [ ] Touches the `Signal` -> `Order` path (strategies emit `Signal`; only the risk engine constructs orders)
- [ ] Adds or modifies an event bus consumer (must be idempotent — delivery is at-least-once)
- [ ] Writes to an audit table (append-only, enforced by the database)
- [ ] Affects backtest/live parity (one code path; only `ExecutionVenue` swaps)
- [ ] Touches cost model parameters (production-calibrated, never testnet)
- [ ] Computes or consumes features (point-in-time, no look-ahead)
- [ ] Constructs a network client (must go through `guarded_client()`)
- [ ] Touches `fking.platform.safety` (requires the `safety:critical` label and a human decision)

```
Named risks:
- <what could go wrong> -> <the mitigation, or the reason we accept it>
```

---

## Estimated size and why it is not larger

*A size, plus the reason this is one issue rather than three. Large pull requests are unreviewable and therefore unreviewed. If the estimate is over roughly 400 changed lines or touches more than three modules, split it here and link the parts — splitting at issue time is cheap, splitting at review time is not.*

```
Size:        <S: under ~150 lines | M: ~150-400 | L: over 400, must be justified or split>
Modules:     <count>
Why not larger: <the seam that keeps this self-contained and independently mergeable>
Why not smaller: <what would be left in a broken intermediate state if split further>
```

---

## Definition of done

- [ ] Problem is stated with evidence that someone else could reproduce
- [ ] "Why it matters now" names a cost with a number and a consequence with a date
- [ ] Contracts table has a row for every category, with "none" where nothing changes
- [ ] Every acceptance criterion has an exact verification command and an expected output
- [ ] At least one criterion fails on a wrong implementation, not only on a missing one
- [ ] Non-goals name the adjacent work this will be tempted into
- [ ] Every applicable `CLAUDE.md` §2 item is ticked, including ones only touched incidentally
- [ ] Size is stated, and an L-sized issue is either justified or split into linked children
- [ ] Type label, priority, milestone and assignee are set
