# Workflow — Release

A release is a **known-good, reproducible commit of the demo runtime** — the point you return to when the running system starts behaving strangely. It is not a shipment to users.

---

## 1. Freeze

Stop merging to `main`. Announce the freeze in the milestone.

```bash
git checkout main && git pull origin main
git status --porcelain          # must be empty
gh pr list --state open --base main
```

Anything in flight either lands before the freeze or waits. A release cut mid-merge cannot be rebuilt from its tag.

---

## 2. Verify on a clean rebuild, not a warm one

```bash
make down
docker compose build --no-cache
make up && make migrate
make check
uv lock --check
```

The `--no-cache` rebuild catches the dependency that only works because it is already in your image layer cache — on a single-machine project that is the failure that costs the most hours, and it never shows up any other way.

**Exit condition**: green `make check` on a from-scratch build, in this transcript.

---

## 3. Decide the version

- **major** — a load-bearing invariant changed: safety kernel, backtest/live parity, risk authority, audit schema.
- **minor** — new module, venue, strategy type, or agent.
- **patch** — fixes with no contract change.

A change to the survival score's weighting is **at least minor** and gets called out by name, because scores before and after are on different scales and comparing them is meaningless.

---

## 4. Build the changelog from what merged

Run `/release <version>`.

```bash
git log --oneline $(git describe --tags --abbrev=0)..HEAD
gh pr list --state merged --json number,title,labels
```

Two sections matter more than the feature list:

**Results-invalidating changes.** Anything touching the cost model, feature definitions, the scoring engine, or the backtest engine. State that backtest numbers from before this release are not comparable to numbers after it, and name the affected range.

**Safety-relevant changes.** Every PR labelled `safety:critical`, listed individually with its diff to `src/fking/platform/safety/`.

---

## 5. Audit the migrations

```bash
ls migrations/ | tail -10
```

Forward-only. No migration may grant UPDATE or DELETE on an audit table or drop a rejecting trigger — an audit log the application can rewrite is not an audit log, and a release containing such a migration does not go out.

---

## 6. Record the runtime state at cut time

Into the release notes, because this is what makes a future rollback interpretable:

- Active strategies and their version hashes
- Global trial counter value
- Cost model parameter set id and calibration date
- Whether the held-out period is intact
- Last successful reconciliation timestamp

---

## 7. Tag and publish

```bash
git tag -a v<version> -m "v<version>"
git push origin v<version>
gh release create v<version> --title "v<version>" --notes-file CHANGELOG-v<version>.md
```

Commit the `CHANGELOG.md` update to `main` as `docs:`.

---

## 8. Deploy and confirm

Follow `.claude/workflows/deployment.md`. It is not optional to reconcile afterwards: `python -m fking.execution.reconcile --full`. Deploying across a testnet wipe with stale local state means trading against a phantom book.

---

## 9. Watch

For the first full cycle after a release, watch rather than walk away:

- Error-level logs
- Realized slippage against modelled slippage — a divergence here means the cost model shifted with the release
- Reconciliation agreement
- Agent quota consumption against the free-tier limit

---

## 10. Unfreeze

Close the milestone, open the next, and move any unfinished issue forward with a one-line reason. Then resume merging.

---

## Rollback

```bash
make down
git checkout v<previous>
make up && make migrate
python -m fking.execution.reconcile --full
```

Always reconcile. Rolling back code does not roll back orders already placed.
