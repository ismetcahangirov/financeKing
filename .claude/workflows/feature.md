# Workflow — Feature

End to end, from an issue to a merged pull request. Each step has an exit condition; do not advance past a step whose exit condition is unmet.

---

## 1. Take the issue

Run `/new-task <issue-number>`.

```bash
git checkout main && git pull origin main
gh issue view <n>
git checkout -b feat/<n>-<kebab-slug>
```

**Exit condition**: the branch exists, the issue's scope is stated in your own words, and any assumption that would waste the work if wrong has been asked about.

---

## 2. Plan before code

Run `/plan`.

Decide which module the code belongs in by asking *what does this code know about?* Confirm the plan does not require `strategy` to import `execution` — if it does, the design is wrong, not the contract.

Resist the abstraction: two concrete callers before it exists. If the plan introduces a protocol with one implementation, remove it.

Post the plan as an issue comment so it survives the session.

**Exit condition**: contracts written with units in the names, verification commands chosen, coverage floors identified, out-of-scope stated.

---

## 3. Test first

Write the failing test. For anything in `risk`, `domain`, or position arithmetic, a Hypothesis property test is required — example tests confirm the cases you thought of, and position math fails on the ones you did not.

```bash
make test ARGS="tests/<path> -x -v"
```

**Exit condition**: the test fails, and it fails for the reason you expect. A test that passes before the implementation exists is testing nothing.

---

## 4. Implement

Run `/build`.

While writing, the non-negotiables are not style: `Decimal` from `str` for money, timezone-aware UTC, frozen domain objects, injected clock, `guarded_client()` for anything on the network, idempotent bus consumers, `mypy --strict` clean.

Instrument as you go — event, correlation ID, audit row. Deferred instrumentation never gets added properly and is missing from exactly the history the next investigation needs.

**Exit condition**: the test passes, no stub or `TODO` remains, and nothing was quietly widened beyond the plan.

---

## 5. Verify

```bash
make check
make test ARGS="--cov=src/fking/<module> --cov-report=term-missing"
```

Floors: `platform/safety` 100%, `risk` 95%, `domain` 95%, `execution` 90%, else 80%.

**Exit condition**: green output in the transcript. Not remembered green — run now.

---

## 6. Self-review

```bash
git diff main...HEAD
```

Debug output? `float` in money math? A network call bypassing `guarded_client()`? A mutable domain object? Tests that only execute the code? Would a reader in six months know *why*?

Then run `/review` on your own diff. Reviewing your own work with the checklist finds roughly half of what a reviewer would.

**Exit condition**: nothing on the blocking list survives.

---

## 7. Ship

Run `/ship <issue-number>`.

Conventional Commits, one logical change per commit, push, PR with labels, milestone, assignee `ismetcahangirov`, `Closes #<n>`, and the real `make check` output pasted into the body.

If the diff exceeds ~400 substantive lines, split it before opening. An unreviewable PR is an unreviewed PR.

**Exit condition**: PR open, CI green, issue linked.

---

## 8. After merge

```bash
git checkout main && git pull origin main
git branch -d feat/<n>-<kebab-slug>
```

If the feature changed a feature definition, the cost model, or the scoring engine, say so in the PR and note that backtest results produced before this merge are no longer comparable to those after it. That statement is the difference between a comparable result history and a meaningless one.
