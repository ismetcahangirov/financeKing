# Release Process

A release here is **a known-good, reproducible commit of the demo runtime** — the point you return to when the running system starts behaving strangely. It is not a shipment to users. There are no users.

That reframing changes what a version number has to communicate. In a normal product, a version tells a consumer whether their integration will break. Here it tells a future session whether **the numbers produced before this tag can be compared to the numbers produced after it**. That is the harder question, and semantic versioning alone cannot answer it — which is why §3 introduces a second, orthogonal number.

`.claude/commands/release.md` and `.claude/workflows/release.md` are the executable procedure. This document is the reasoning and the parts that require judgement.

**What is automated, and what deliberately is not.** `tools/release/` runs the preflight refusals (§2, §3), derives the changelog (§4), classifies every migration in the range, and selects the rollback procedure (§7). It does **not** create the tag unless asked twice, and it never pushes one:

```bash
make release VERSION=0.4.0                    # refusals + notes. Creates nothing.
make release VERSION=0.4.0 IRREVERSIBLE=1     # same, for a range with an irreversible migration
make release-tag VERSION=0.4.0                # re-runs the refusals, then tags
git push origin v0.4.0                        # publishes; triggers .github/workflows/release.yml
```

The split exists because the tag is the one irreversible act in this process — tags here are immutable and never moved (§6) — and the notes it will be quoted from contain the rollback procedure someone will follow under pressure. Reading that procedure *before* the object that quotes it exists costs one command; discovering it says the wrong thing afterwards costs a release.

`CHANGELOG-v<version>.md` is a build artifact, not a tracked file: the copy of record is the one attached to the GitHub release, and the workflow re-derives it from the tagged commit rather than uploading yours. Delete it after the push. It is the one untracked path the dirty-tree refusal ignores — otherwise `make release` would write the file that makes `make release-tag` refuse — and the exclusion is exactly that one filename, so nothing else hides behind it.

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

Built from what actually merged, not from memory. `make release` does this; the rest of this section is what it does and why.

### 4.0 The range is ancestry, never a timestamp

The obvious recipe is a time-bounded search:

```bash
# DO NOT. Kept here as the rejected alternative, because it is what everyone reaches for.
gh pr list --state merged --search "merged:>$(git log -1 --format=%aI $(git describe --tags --abbrev=0))"
```

It asks GitHub's search index a question about *time*, and fails in two directions. The index is eventually consistent, so a pull request merged shortly before the cut can simply be absent from the answer — and a changelog that silently omits an entry is indistinguishable from one that is complete. The boundary is also an instant, so a pull request merged in the same second as the previous tag falls into both ranges or neither, depending on rounding.

`git log <previous-tag>..HEAD` is a question about *ancestry*: exact, local, offline, and identical on every machine. Squash merges leave `(#158)` on the subject line and merge commits leave `Merge pull request #158`; both are parsed, and each number's labels are then fetched by id rather than by search. This is the derivation `tools/release/changelog.py` implements.

### 4.1 Grouping, and why an ambiguous label fails the release

Entries are grouped by the pull request's `type:` label, in behaviour-first order: Added, Fixed, Changed, Performance, Architecture decisions, Research, Tests, Documentation, Housekeeping.

Three rules make the taxonomy load-bearing rather than decorative:

- **Two `type:` labels is a refusal, not a tie-break.** Picking one means the section a change is announced in was decided by a sort order. The fix is one label edit on the pull request.
- **A `type:` label with no section is a refusal.** Adding a label to the repository without adding it to `SECTION_FOR_TYPE` would otherwise silently bucket every future change under it as "Uncategorised".
- **No `type:` label is listed under "Uncategorised", never dropped.** A changelog that quietly omits what it could not classify is worse than one that admits it could not, because the omission is invisible exactly where people look for completeness.

Then the two sections that matter more than the feature list.

### 4.2 Results-invalidating changes — mandatory section

Generated, not written from memory, from the commit trailers over the same ancestry range:

```bash
git log $(git describe --tags --abbrev=0)..HEAD --grep='^Results-Invalidating:' \
  --format='%h %s%n    %(trailers:key=Results-Invalidating,valueonly)'
```

The generator uses `%x1f` and `%x1e` — unit and record separators — rather than the readable format above, because a trailer's value is prose that routinely contains every delimiter a person would otherwise choose. One trap in that, worth knowing before you touch the parser: **Python counts U+001C–U+001F as whitespace**, so `record.strip()` eats the trailing field separator of a commit whose trailer is empty, and every ordinary commit in the range then reports as malformed. `tests/infra/test_release_changelog.py` carries the regression case.

This is why the `Results-Invalidating:` git trailer is mandatory on commits touching the cost model, feature definitions, the scoring engine, or the backtest engine (`GIT_WORKFLOW.md` §3). Prose in a PR body does not substitute — PR bodies are not in the git history, and the changelog is generated from the history.

The section states, explicitly:

- Which change caused it
- **Which results are affected, by date range and epoch** — "all backtests and survival scores produced under `results_epoch=3` (2026-05-14 to 2026-08-02)"
- Whether affected strategies must be re-scored before their next lifecycle decision

If this section is non-empty, the release bumps the epoch (§3.2) and the version is major.

### 4.3 Safety-relevant changes — mandatory, and first

Every PR labelled `safety:critical`, listed **individually**, in the **leading** section, with `git diff <previous-tag>..HEAD -- src/fking/platform/safety/` inlined into the notes.

Not summarised. The diff goes in verbatim, because the release notes are what someone reads when they are trying to establish, months later, whether the allowlist ever changed and when. A summary requires trusting whoever wrote it; the diff does not.

Leading, because burying an allowlist change among forty entries defeats the review the label exists to trigger. And the entry appears **twice** — once at the top, once in its `type:` section — which looks like a bug and is not: listing it only at the top makes the feature list a lie by omission, and listing it only in its type section is the burial. Duplication costs a reader four seconds; either omission costs them the review.

If the section is empty, it says "None." explicitly. An absent section is ambiguous between "nothing changed" and "nobody checked".

### 4.4 Migrations

Every migration added or modified in the range, each classified by what its `downgrade()` actually does. *Modified*, not only added: a `downgrade()` edited after its revision shipped changes this release's rollback story exactly as much as a new migration does, and it is the change a reviewer is least likely to look at.

| Classification | Means |
|---|---|
| `reversible` | No `raise` in `downgrade()` |
| `conditionally irreversible` | A `raise` under an `if`, `try`, loop or `with` — e.g. `0012_gap_resolution`, which refuses when resolved gaps exist |
| `irreversible` | A `raise` directly in the body — e.g. `0002_audit_substrate` |

**`conditionally irreversible` is treated exactly as `irreversible`.** Whether such a migration raises is a property of the *database at rollback time*, not of the file, so at tag time it is unknowable — and the only safe reading of an unknowable rollback is that it will not work. The classification is AST-based rather than a grep for `raise`, because a grep matches the word inside the docstring of every migration explaining why it does *not* raise, which is wrong in the direction that reports safety.

**A release containing a migration that grants `UPDATE` or `DELETE` on an audit table, or drops a rejecting trigger, does not go out.** An audit log the application can rewrite is not an audit log, and this is the last checkpoint before it ships. No tool checks this one; a reviewer does.

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
make release-tag VERSION=<version>            # re-runs every refusal, then tags
git push origin v<version>                    # the publish
```

Nothing else. The push triggers `.github/workflows/release.yml`, which re-derives everything from the tagged commit and creates the GitHub release; `gh release create` by hand is not part of the procedure.

**The tag message carries a machine-readable assertion, and the workflow checks it.** `make release-tag` writes:

```
v0.4.0

Irreversible-Migration: yes
Migrations: 0015_release_stamp.py (reversible), 0016_agent_memory.py (irreversible)
```

The workflow re-derives the classification from `migrations/versions/` over the same range and refuses to publish when the trailer disagrees — or when there is no trailer at all, which means the tag was created by hand rather than by the tool. A missing trailer is a refusal rather than a default of "no", because the assertion selects which of the two §7 procedures the published notes carry, and a release whose runbook describes the wrong one is worse than a release with no runbook: it will be followed.

The workflow additionally refuses a lightweight tag, a tag whose commit is not an ancestor of `main`, and a commit whose check runs are anything but green — **including absent**, which is the normal state of a commit pushed a minute ago and is not a pass.

Then commit the `CHANGELOG.md` index update to `main` as a `docs:` commit.

**Tags are immutable and are never moved.** A bad release gets `v0.4.1`, never a re-pointed `v0.4.0`. The runtime state snapshot in the notes describes what was running at a specific commit; moving the tag makes that description silently wrong, and it is read exactly when something is already going badly.

**Release versions are three integers.** No `-rc1`, no `+build`. Not grammar pedantry: §1 defines a release as a state you return to, so a pre-release tag is by construction a state nobody has agreed to return to — and a tag list containing them answers "what can I roll back to?" with a list that must be filtered by hand at the moment hand-filtering is least reliable. `make release` refuses a version that does not exceed the last one, and refuses outright if any release-shaped-but-unparseable tag (`v0.4.0-rc1`) exists, because such a tag is invisible to that ordering check.

---

## 7. Rollback

### 7.0 The asymmetry: code back, schema forward

**A rollback here is not "deploy the previous tag", and the reason is that the two halves of a release move in opposite directions.**

For a stateless service, code and schema are one thing. Here the previous tag may predate a migration, and `downgrade()` on the audit substrate and the trial ledger **raises by design** — rolling back a schema that holds the audit trail is a data-destruction operation dressed as a schema operation (`.claude/rules/append-only-audit.md`). So there are two procedures, and which one applies is a fact about the range, computed at tag time by `make release` and written into the notes:

| Range contains | Procedure | Schema |
|---|---|---|
| Only `reversible` migrations | §7.1 | May move back, but should not — see below |
| Any `irreversible` or `conditionally irreversible` migration | §7.2 | Stays forward. `alembic downgrade` is not run |

The reason this is decided at *tag* time rather than at rollback time is the constraint it makes explicit. The schema-forward procedure is safe **only if every migration in the range was additive** — new columns nullable, new tables unread by the old code. That is already a review requirement (`CODE_REVIEW.md` §1), but it is a requirement nobody re-checks; marking the release with `IRREVERSIBLE=1` is the moment somebody is made to confirm it, while the change is still fresh, rather than at 03:00 with positions open.

Over-declaring is refused for the same reason under-declaring is: the marking selects the runbook, so a release wrongly marked irreversible tells a future operator not to touch a schema they safely could — and, worse, teaches them the marker means nothing.

### 7.1 Symmetric rollback — every migration in the range reverses

```bash
make down
git checkout v<previous>
make up && make migrate
python -m fking.execution.reconcile --full
```

`make migrate` moves forward only; against the previous tag it is a no-op, because the schema is already at that revision or beyond. **Prefer leaving the schema forward even here.** `alembic downgrade` is available in this range and buys nothing, and a habit of reaching for it survives into the next release, where it is not available and where reaching for it does the damage described in §7.3.

### 7.2 Asymmetric rollback — the range contains an irreversible migration

```bash
make down
git checkout v<previous>
make up            # NOT `make migrate`. The schema stays where it is.
python -m fking.execution.reconcile --full
```

**`alembic downgrade` is not part of this procedure and must not be run.** §7.3 is the evidence for that sentence.

If the old code cannot run against the forward schema, the rollback is a **forward fix**, not a checkout: cut a patch release. Say that out loud rather than reaching for `downgrade` — the temptation arrives precisely when the forward fix looks slow.

### 7.3 The drill, and what it found

`make rollback-drill` executes the schema half against a real PostgreSQL rather than reasoning about it: it migrates a fresh database to `head`, attempts `alembic downgrade base`, and asserts what happens. It runs in CI on every pull request as `tests/infra/test_release_rollback_drill.py`.

**Executed 2026-08-05, against `timescale/timescaledb-ha:pg16` via testcontainers, at `dd7295d`:**

```
tests/infra/test_release_rollback_drill.py::test_the_schema_refuses_to_roll_back_past_the_audit_substrate PASSED [ 50%]
tests/infra/test_release_rollback_drill.py::test_the_audit_tables_survive_the_refused_rollback PASSED [100%]

============================= 2 passed in 21.90s ==============================
```

Two findings, and the second is the one that changed the procedure above:

**The refusal holds, and it protects the rows.** `downgrade` cannot walk past `0002_audit_substrate`, and after the refusal `audit_log` and `trial_ledger` are both still present. A refusal that raised *after* dropping the table would be a refusal with no subject, and nothing else in this repository would have noticed.

**The refusal is not a clean abort — it is a partial teardown.** `migrations/env.py` sets `transaction_per_migration=True`, so each revision commits on its own. `downgrade base` therefore *succeeds* through every revision above 0002 — dropping hypertables, functions, triggers, grants and the feature store as it goes — and only then raises. The database is left pinned at `0002` with the audit tables intact and **everything above them gone**. Recovery is `alembic upgrade head`, which the drill also asserts, but the intervening state is a running system with no `bar`, no `feature_values` and no `order` table.

That is why §7.2 says `alembic downgrade` must not be *run*, rather than the weaker and more natural "it will refuse anyway". The refusal is not the protection you would infer from reading the migration; it is a floor you hit after the damage above it is already committed.

### 7.4 In every case

**Reconciliation is mandatory, not optional.** Rolling back code does not roll back orders already placed. Between the bad release and the rollback, the system placed orders, received fills, and updated positions. The rolled-back code's local state is from before all of that. Without a full reconciliation, the system resumes trading against a book it has hallucinated.

**Do not roll back the trial counter or the held-out flag** (§5.1). The reconciliation tool does not touch them; do not touch them by hand either.

**Never move a tag and never force-push `main`.** A bad release gets the next patch version. This section is quoted from during an incident, and a moved tag makes it silently describe a commit that is no longer there.

**If the rollback spans a testnet wipe, local state was fiction on both sides.** Binance spot testnet wipes roughly every 30 days: keys survive, balances and open orders vanish. Compare the wipe date against the tag's `last successful reconciliation` field. If a wipe fell in between, the full reconciliation will report a total divergence and trip the kill switch — that is correct behaviour, not a bug, and it requires a human to confirm the cause before the system resumes (`TESTING.md` §8.5).

**The first release has no rollback target at all.** Its range is the whole history, so it contains `0002` and must be marked `IRREVERSIBLE=1`; and there is no previous tag to check out. Its only recovery is forward. The generated notes say so rather than emitting a `git checkout` against a tag that does not exist.

### 7.5 Position reconciliation, specifically

`reconcile --full` treats the **exchange as the source of truth** and converges local state to it. Three outcomes, all of which must be audited individually rather than silently fixed:

| Divergence | Meaning | Action |
|---|---|---|
| Local has a position the exchange does not | Either the wipe, or we recorded a fill that never happened | Audit row per divergence; drop local; kill switch trips |
| The exchange has a position local does not | An orphan — **unmanaged risk**. Possibly opened by the bad release | Adopt it, audit it, and do not resume automated trading until a human has seen it |
| Quantities disagree | Almost always a double-applied or dropped fill | Adopt the exchange's; audit the delta; investigate the consumer's dedupe key |

Silent convergence is forbidden. A reconciler that quietly fixes divergences is indistinguishable from one that quietly loses real positions, and the audit rows are the only way to tell afterwards which happened.

### 7.6 After the rollback

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

Items marked **(auto)** are enforced by `make release` and fail the cut rather than the review. They are still listed, because a checklist that hides what the machine is doing trains people to stop reading it.

- [ ] `main` clean, `make check` green, `uv lock --check` passes — **(auto: clean tree, on `main`, in step with `origin/main`, green CI including "absent is not green")**
- [ ] Clean `--no-cache` rebuild green, output in the transcript
- [ ] Version determined against §3, including whether the epoch bumps — **(auto: three integers, exceeds the last release, does not already exist)**
- [ ] Changelog generated from merged PRs, not written from memory — **(auto, from ancestry rather than a search query; §4.0)**
- [ ] **Results-invalidating** section generated from `Results-Invalidating:` trailers — **(auto)**; affected date range and epoch stated by hand
- [ ] **Safety-relevant** section listing every `safety:critical` PR with its diff inlined — or "None" — **(auto)**
- [ ] Every migration in the range classified — **(auto)** — and **confirmed additive and audit-preserving by a human**, which is what `IRREVERSIBLE=1` asserts
- [ ] Runtime state snapshot captured into the notes (§5)
- [ ] Rollback path stated explicitly in the notes, including the reconciliation step — **(auto: §7.1 or §7.2, selected by the migration classification)**
- [ ] Notes read *before* tagging — the tag is immutable and the rollback section is what an incident will be run from
- [ ] `make release-tag`, then `git push origin v<version>`; `CHANGELOG.md` index committed to `main` as `docs:`
- [ ] Post-deploy reconciliation run and its output recorded
