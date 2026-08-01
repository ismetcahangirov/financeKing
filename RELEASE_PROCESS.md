# Release Process

A release here is **a known-good, reproducible commit of the demo runtime** — the point you return to when the running system starts behaving strangely. It is not a shipment to users. There are no users.

That reframing changes what a version number has to communicate. In a normal product, a version tells a consumer whether their integration will break. Here it tells a future session whether **the numbers produced before this tag can be compared to the numbers produced after it**. That is the harder question, and semantic versioning alone cannot answer it — which is why §3 introduces a second, orthogonal number.

`.claude/commands/release.md` and `.claude/workflows/release.md` are the executable procedure. This document is the reasoning and the parts that require judgement.

---

## 1. When to cut a release

- A milestone closes.
- A load-bearing invariant changed (safety kernel, backtest/live parity, risk authority, audit schema) — cut immediately, even mid-milestone.
- Before any change that will make prior results incomparable, so there is a tag on the last comparable state.
- Before a period of unattended running.

Do **not** cut a release to mark progress. A tag with nothing behind it dilutes the meaning of every other tag, and the whole value of the tag list is that each entry is a state you can actually return to.

---

## 2. Preconditions

```bash
git checkout main && git pull origin main
git status --porcelain                 # must be empty
gh pr list --state open --base main    # announce the freeze; nothing merges during a cut
make check                             # green, run now, not remembered
uv lock --check                        # the lockfile must be authoritative
```

Then the part that is easy to skip and expensive to skip:

```bash
make down
docker compose build --no-cache
make up && make migrate
make check
```

`--no-cache` catches the dependency that only works because it is already in your image layer cache. On a single-machine, zero-budget project this is the failure that costs the most hours, because it is invisible until the day you rebuild from scratch — which is, reliably, the day you are trying to roll back.

**Exit condition**: green `make check` on a from-scratch build, output in the transcript.

---

## 3. Versioning

### 3.1 Semantic versioning, this project's reading

| Bump | Means |
|---|---|
| **major** | A load-bearing invariant changed: the safety kernel, backtest/live parity, risk's exclusive authority to construct orders, point-in-time feature semantics, or the audit schema. Also: any results-epoch bump (§3.2) |
| **minor** | New module, new venue, new strategy type, new agent, new feature in the registry. Also: any change to the survival score's weighting |
| **patch** | Fixes and internal changes with no contract change and no effect on produced numbers |

A change to the survival score's weighting is **at least minor and is called out by name**, because scores computed before and after are on different scales. Comparing them is meaningless, and the comparison is exactly what someone will do.

### 3.2 The results epoch: a second number, because SemVer cannot say this

SemVer expresses compatibility of *interfaces*. It has no way to express "every number this system has ever produced is now void". That statement is the most consequential thing a release in this project can make, so it gets its own counter.

**`results_epoch`** is a monotonically increasing integer, defined in `fking.platform.version`, and **stamped onto every persisted backtest result, every survival score, and every walk-forward fold record.**

It is incremented when a change makes prior results incomparable to new ones. That is:

- The cost model's structure or its calibrated parameters
- Any feature definition in the registry, including a bug fix to one
- The scoring engine's inputs or weights
- The backtest engine's fill, slippage, or clock semantics
- The validation methodology: fold construction, purge or embargo length, holdout policy

The point of making it a stored column rather than a convention: **the query layer refuses to aggregate across epochs.** A request for "best strategies by Sharpe" spanning an epoch boundary returns an error naming the boundary, not a silently mixed ranking. Enforcement in code, not in documentation, because documentation does not survive a session with no memory of reading it.

**An epoch bump is a major version bump.** The two are not independent in practice: if all prior numbers are void, the system's contract with its own history has changed, and that is exactly what major means here.

The epoch is *not* bumped for: performance work that produces trade-for-trade identical output (`PERFORMANCE_GUIDE.md` §7), new strategies, new venues, dashboard changes, or anything in `api/`.

### 3.3 Worked examples

| Change | Version | Epoch |
|---|---|---|
| Fix an off-by-one in a rolling window used by three features | major | **bump** — every backtest using those features is void |
| Add a maker/taker split to the cost model | major | **bump** |
| Add a capacity penalty to the survival score | minor + major (epoch) | **bump** |
| Reweight the existing survival score terms | minor + major (epoch) | **bump** |
| Add a Bybit testnet venue | minor | no |
| Replace Postgres bar scans with DuckDB, output identical | patch | no |
| Widen a `Decimal` quantize precision on order quantities | major | **bump** — fills change |
| Fix a dashboard chart axis | patch | no |
| Add a host to the safety allowlist | major | no |

The fourth row is the one people get wrong. "Just reweighting" feels like tuning. It makes every recorded survival score incomparable to every future one, which is precisely the definition of an epoch bump.

---

## 4. The changelog

Built from what actually merged, not from memory:

```bash
git log --oneline $(git describe --tags --abbrev=0)..HEAD
gh pr list --state merged \
  --search "merged:>$(git log -1 --format=%aI $(git describe --tags --abbrev=0))" \
  --json number,title,labels
```

Group by Conventional Commit type. Then add the two sections that matter more than the feature list.

### 4.1 Results-invalidating changes — mandatory section

Generated, not written from memory:

```bash
git log $(git describe --tags --abbrev=0)..HEAD --grep='^Results-Invalidating:' \
  --format='%h %s%n    %(trailers:key=Results-Invalidating,valueonly)'
```

This is why the `Results-Invalidating:` git trailer is mandatory on commits touching the cost model, feature definitions, the scoring engine, or the backtest engine (`GIT_WORKFLOW.md` §3). Prose in a PR body does not substitute — PR bodies are not in the git history, and the changelog is generated from the history.

The section states, explicitly:

- Which change caused it
- **Which results are affected, by date range and epoch** — "all backtests and survival scores produced under `results_epoch=3` (2026-05-14 to 2026-08-02)"
- Whether affected strategies must be re-scored before their next lifecycle decision

If this section is non-empty, the release bumps the epoch (§3.2) and the version is major.

### 4.2 Safety-relevant changes — mandatory section

Every PR labelled `safety:critical`, listed **individually**, with its diff to `src/fking/platform/safety/` inlined into the notes.

```bash
gh pr list --state merged --label safety:critical \
  --search "merged:>$(git log -1 --format=%aI $(git describe --tags --abbrev=0))" \
  --json number,title,url
```

Not summarised. The diff goes in the notes verbatim, because the release notes are what someone reads when they are trying to establish, months later, whether the allowlist ever changed and when. A summary requires trusting whoever wrote it; the diff does not.

If the section is empty, say "None" explicitly. An absent section is ambiguous between "nothing changed" and "nobody checked".

### 4.3 Migrations

```bash
ls migrations/versions/ | tail -20
```

For each migration in the range, state: whether it is forward-only, and whether it preserves append-only enforcement on audit tables.

**A release containing a migration that grants `UPDATE` or `DELETE` on an audit table, or drops a rejecting trigger, does not go out.** An audit log the application can rewrite is not an audit log, and this is the last checkpoint before it ships.

---

## 5. Capturing runtime state at tag time

The changelog says what the code does. This section says **what the system was doing**, and it is what makes a future rollback interpretable rather than a guess.

Captured at cut time and written into the release notes:

```bash
python -m fking.platform.snapshot --format=markdown >> CHANGELOG-v<version>.md
```

Contents, each with a specific reason for being there:

| Field | Why it is needed at rollback time |
|---|---|
| **Active strategies and their version hashes** | Rolling back code does not roll back the strategy population. You need to know which strategies were live under the old code to decide whether they are still valid |
| **`results_epoch`** | Determines whether scores recorded after the rollback can be compared to those recorded before it |
| **Global trial counter** | Feeds the deflated Sharpe. It is monotonic and **is not rolled back** — see §5.1 |
| **Cost model parameter set id and calibration date** | Tells you whether a slippage divergence after the rollback is the code or the parameters |
| **Held-out period status: intact or burned** | See §5.1. This is the one that cannot be undone |
| **Last successful reconciliation timestamp** | Tells you how much exchange drift is possible between the tag and now |
| **Open positions and open orders, with ids** | The reconciliation baseline. Without it, post-rollback reconciliation cannot distinguish "we lost a position" from "the exchange never had it" |
| **Kill switch state and, if tripped, the reason** | A release cut while degraded must say so, or the next reader assumes it was healthy |

### 5.1 What a rollback cannot undo

Two pieces of state are **monotonic and survive rollback**, and the release notes must say so plainly because the instinct at rollback time is to assume everything reverts.

**The global trial counter.** It counts how many configurations have been evaluated against history, and it feeds the deflated Sharpe ratio that defends against overfitting. Rolling it back would understate the multiple-testing burden and inflate every subsequent strategy's apparent significance. The counter only ever goes up. A rollback that "restores" it has quietly disabled the primary overfitting defence.

**The held-out period.** It is burned the first time it is touched. Rolling back to a tag from before it was touched does not un-burn it — the information has been observed and has influenced every decision made since. Restoring the "intact" flag would be a lie recorded in the system's own state.

Both are recorded at tag time precisely so that a future rollback knows not to touch them.

---

## 6. Tag and publish

```bash
git tag -a v<version> -m "v<version>"
git push origin v<version>
gh release create v<version> --title "v<version>" --notes-file CHANGELOG-v<version>.md
```

Then commit the `CHANGELOG.md` update to `main` as a `docs:` commit.

**Tags are immutable and are never moved.** A bad release gets `v0.4.1`, never a re-pointed `v0.4.0`. The runtime state snapshot in the notes describes what was running at a specific commit; moving the tag makes that description silently wrong, and it is read exactly when something is already going badly.

---

## 7. Rollback

### 7.1 The procedure

```bash
make down
git checkout v<previous>
make up && make migrate
python -m fking.execution.reconcile --full
```

Four things about it, in order of how badly they are misunderstood.

**Reconciliation is mandatory, not optional.** Rolling back code does not roll back orders already placed. Between the bad release and the rollback, the system placed orders, received fills, and updated positions. The rolled-back code's local state is from before all of that. Without a full reconciliation, the system resumes trading against a book it has hallucinated.

**Migrations are forward-only; `make migrate` moves forward, never back.** The rolled-back code must tolerate a schema that is newer than it is. That is why every migration is additive — new columns nullable, new tables unused by old code. A migration that renames or drops is a migration that makes rollback impossible, and it should have been rejected at review (`CODE_REVIEW.md` §1).

If the schema genuinely cannot support the old code, the rollback is a **forward fix**, not a checkout. Say so and cut a patch release.

**Do not roll back the trial counter or the held-out flag** (§5.1). The reconciliation tool does not touch them; do not touch them by hand either.

**If the rollback spans a testnet wipe, local state was fiction on both sides.** Binance spot testnet wipes roughly every 30 days: keys survive, balances and open orders vanish. Compare the wipe date against the tag's `last successful reconciliation` field. If a wipe fell in between, the full reconciliation will report a total divergence and trip the kill switch — that is correct behaviour, not a bug, and it requires a human to confirm the cause before the system resumes (`TESTING.md` §8.5).

### 7.2 Position reconciliation, specifically

`reconcile --full` treats the **exchange as the source of truth** and converges local state to it. Three outcomes, all of which must be audited individually rather than silently fixed:

| Divergence | Meaning | Action |
|---|---|---|
| Local has a position the exchange does not | Either the wipe, or we recorded a fill that never happened | Audit row per divergence; drop local; kill switch trips |
| The exchange has a position local does not | An orphan — **unmanaged risk**. Possibly opened by the bad release | Adopt it, audit it, and do not resume automated trading until a human has seen it |
| Quantities disagree | Almost always a double-applied or dropped fill | Adopt the exchange's; audit the delta; investigate the consumer's dedupe key |

Silent convergence is forbidden. A reconciler that quietly fixes divergences is indistinguishable from one that quietly loses real positions, and the audit rows are the only way to tell afterwards which happened.

### 7.3 After the rollback

```bash
make check
python -m fking.platform.snapshot --format=markdown     # capture the post-rollback state too
```

Compare the new snapshot to the one in the release notes. Every field that differs and is not explained by §5.1 is an open question, and open questions block resuming automated trading.

Open an issue with the snapshot diff attached. The rollback is not finished when the system is running again; it is finished when the reason for it is written down.

---

## 8. After a release

For the first full cycle, watch rather than walk away:

- Error-level logs
- **Realized slippage against modelled slippage.** A divergence here means the cost model shifted with the release — and if it did, the epoch should have been bumped and was not
- Reconciliation agreement
- Agent quota consumption against the free-tier limit

Then unfreeze: close the milestone, open the next, move any unfinished issue forward with a one-line reason, resume merging.

---

## 9. Release checklist

- [ ] `main` clean, `make check` green, `uv lock --check` passes
- [ ] Clean `--no-cache` rebuild green, output in the transcript
- [ ] Version determined against §3, including whether the epoch bumps
- [ ] Changelog generated from merged PRs, not written from memory
- [ ] **Results-invalidating** section generated from `Results-Invalidating:` trailers, with affected date range and epoch
- [ ] **Safety-relevant** section listing every `safety:critical` PR with its diff inlined — or "None"
- [ ] Every migration in the range confirmed forward-only and audit-preserving
- [ ] Runtime state snapshot captured into the notes (§5)
- [ ] Rollback path stated explicitly in the notes, including the reconciliation step
- [ ] Tag annotated and pushed; `CHANGELOG.md` committed to `main` as `docs:`
- [ ] Post-deploy reconciliation run and its output recorded
