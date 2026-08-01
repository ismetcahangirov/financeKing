---
description: Reconcile ROADMAP.md against real repository state and report the true critical path
argument-hint: [phase-or-milestone]
allowed-tools: Read, Grep, Glob, Bash, Edit
---

Reconcile the roadmap with reality for: $ARGUMENTS (default: the current milestone).

A roadmap that is edited but never checked against the repository becomes fiction within a month. This command's job is to find the divergence.

## 1. Read the stated plan

```bash
cat ROADMAP.md
gh issue list --milestone "<current>" --state all --json number,title,state,labels --limit 100
```

## 2. Verify each "done" item against the code, not the checkbox

For every item marked complete, find the evidence:

```bash
gh pr list --state merged --limit 50 --json number,title,mergedAt,labels
```

For each, confirm the module actually exists and is covered:

```bash
ls src/fking/<module>/
make test ARGS="tests/<module> --cov=src/fking/<module> --cov-report=term-missing"
```

An item is complete when the code exists, `make check` is green on `main`, and the module clears its coverage floor (`platform/safety` 100%, `risk` 95%, `domain` 95%, `execution` 90%, else 80%). A merged PR is not by itself evidence of completion — check for stubs left behind:

```bash
grep -rn "NotImplementedError\|TODO\|FIXME\|pass  #" src/fking/ --include=*.py
```

Any hit inside a module marked done is a roadmap correction, not a nit.

## 3. Re-derive the critical path

The dependency order is structural, not preference:

- `domain` blocks everything. Nothing above it can be trusted until the types are frozen and property-tested.
- `platform/safety` blocks any code that touches the network. It is P0 and non-deferrable.
- Correlation IDs and append-only audit tables are P0 work, not final polish — instrumentation deferred to the end never gets added properly and is missing from exactly the history an investigation needs.
- Point-in-time semantics in `data` block every backtest result. A backtest run before the leakage test exists produces numbers nobody can defend.
- The cost model must be calibrated from production data before any strategy is promoted; testnet calibration invalidates everything downstream.
- `evolution` scoring blocks strategy generation. Generating strategies before the rejection machinery exists produces confident nonsense at scale.

Report which of these is currently the binding constraint. There is one; name it.

## 4. Look for the drift that matters

- Items that have been "in progress" across two milestones — either they are blocked and nobody said so, or they are underspecified.
- Any phase where generation capability is ahead of validation capability. That inversion is the single most dangerous state this project can be in, because it produces results that look like progress.
- Work items with no verification defined. Add one or drop the item.

## 5. Update

Edit `ROADMAP.md` to match what is actually true. Move items, do not delete their history — a struck-through item with a reason is more useful than a clean list. Where an item was dropped, say why in one line.

Then reconcile GitHub:

```bash
gh issue edit <n> --milestone "<correct>"
```

## 6. Report

Current phase, the binding constraint, items whose "done" status was wrong and why, and the next three issues in dependency order.
