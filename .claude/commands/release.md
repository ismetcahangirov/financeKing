---
description: Cut a tagged release from main with a changelog derived from merged PRs and a stated rollback path
argument-hint: <version, e.g. 0.4.0>
allowed-tools: Read, Grep, Glob, Bash, Write, Edit
---

Cut release `v$1`.

A release here is a **known-good, reproducible commit of the demo runtime**. It is not a shipment to users; it is a point you can return to when the running system starts behaving strangely.

## 1. Preconditions

```bash
git checkout main && git pull origin main
git status --porcelain            # must be empty
make check                        # must be green on main, run now, not remembered
gh run list --branch main --limit 5
```

Also confirm the lockfile is authoritative — a release that cannot be rebuilt is not a release:

```bash
uv lock --check
```

## 2. Determine the version

Semantic versioning, with this project's reading of the parts:

- **major** — a change to a load-bearing invariant: safety kernel, backtest/live parity, risk authority, or the audit schema.
- **minor** — new module, new venue, new strategy type, new agent.
- **patch** — fixes and internal changes with no contract change.

A change to the survival score's weighting is at least **minor** and must be called out by name in the changelog, because every score before and after it is on a different scale and comparing them is meaningless.

## 3. Build the changelog from what actually merged

```bash
git log --oneline $(git describe --tags --abbrev=0)..HEAD
gh pr list --state merged --search "merged:>$(git log -1 --format=%aI $(git describe --tags --abbrev=0))" --json number,title,labels
```

Group by Conventional Commit type. Then add the two sections that matter more than the feature list:

**Behaviour changes that invalidate prior results.** Anything touching the cost model, feature definitions, the scoring engine, or the backtest engine means backtest numbers from before this release are not comparable to numbers after it. Say so explicitly with the affected date range of results.

**Safety-relevant changes.** Any PR labelled `safety:critical`, listed individually with its diff to `src/fking/platform/safety/`.

## 4. Migrations

```bash
ls migrations/ | tail -10
```

List every migration in the range and state, for each, whether it is forward-only and whether it preserves append-only enforcement on audit tables. A release containing a migration that grants UPDATE or DELETE on an audit table does not go out.

## 5. Verify a clean rebuild, not just a clean tree

```bash
make down
docker compose build --no-cache
make up && make migrate
make check
```

This catches the dependency that only works because it is already in someone's cache — which, on a single-machine zero-budget project, is the failure that costs the most hours.

## 6. Tag and publish

```bash
git tag -a v$1 -m "v$1"
git push origin v$1
gh release create v$1 --title "v$1" --notes-file CHANGELOG-v$1.md
```

Update `CHANGELOG.md` on `main` in a `docs:` commit.

## 7. Record the runtime state at the tag

In the release notes, record what the running system's state was at cut time, because this is what makes a rollback interpretable later:

- Active strategies and their version hashes
- Global trial counter value
- Cost model parameter set id and its calibration date
- Whether the held-out period is still intact
- Last successful reconciliation timestamp

## 8. Rollback path

State it explicitly in the notes:

```bash
make down && git checkout v<previous> && make up && make migrate
python -m fking.execution.reconcile --full
```

Reconciliation after rollback is mandatory. Rolling back code does not roll back orders already placed, and if the rollback spans a testnet wipe the local state is fiction either way.

## 9. Report

Tag, changelog summary, migrations, results-invalidating changes, safety-critical changes, and the rebuild verification output.
