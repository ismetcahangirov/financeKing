# Git Workflow

`CLAUDE.md` §6 states the rules. This document states the mechanics, the edge cases, and the reasons the mechanics are shaped the way they are.

The governing idea: **`main` must be a sequence of points you can return to.** Every rule below exists because some failure mode makes a commit unreturnable — unrevertable, unrebuildable, or uninterpretable six months later.

---

## 1. Every task starts by pulling `main`

```bash
git status --porcelain     # must be empty
git checkout main
git pull origin main
```

Non-negotiable, and not for tidiness. Two specific failures:

**Branching from a stale `main` hides merge conflicts until review.** A conflict discovered at merge time is resolved by whoever is in the least good position to resolve it — usually the author, days later, having forgotten the reasoning.

**Branching from a stale `main` reruns migrations out of order.** Alembic's `down_revision` chain is linear. If you branch from a `main` that predates someone else's migration and write your own, both migrations claim the same parent and Alembic refuses to resolve the head. The fix is a manual rebase of the revision chain, which is fiddly and easy to get wrong in a way that only fails on a fresh database — that is, in CI, in the release rebuild, and never on your machine.

If `git pull` is not a fast-forward, **stop**. A divergence on `main` is a separate incident and resolving it as a side effect of starting a task is how the divergence becomes permanent. Report it.

If the tree is dirty, stop and report rather than stashing. Uncommitted work in this repository is usually a half-written migration or a recorded exchange fixture; both are easy to lose in a stash and expensive to recreate — a recorded fixture may require provoking a specific exchange error condition that does not occur on demand.

---

## 2. Branch naming

```
<type>/<issue-number>-<kebab-slug>
```

| Type | For | Ends in |
|---|---|---|
| `feat` | New capability | Code + tests + docs |
| `fix` | A defect in existing behaviour | A regression test that fails on `main` |
| `docs` | Documentation only | No `src/` changes at all |
| `chore` | Dependencies, CI, tooling, lockfiles | Green rebuild proof |
| `refactor` | Structure changes, behaviour identical | Preservation evidence (`/refactor` §5) |
| `test` | Tests for existing code | Coverage delta against the floor |
| `perf` | Optimisation | Before/after numbers on the same data |
| `research` | Investigation, no production code | A falsifiable hypothesis or a negative result |

Real examples from this project's shape:

```
feat/12-binance-testnet-adapter
fix/47-spot-microsecond-timestamp-normalization
refactor/58-extract-cost-model-from-backtest-venue
perf/71-duckdb-parquet-scan-for-backtest-bars
research/83-does-funding-rate-predict-perp-basis
```

Pick the type from the **issue's labels**, not from how the title is phrased. "Improve the sizing logic" is a `feat` if it changes behaviour and a `refactor` if it does not, and the label is where that was decided.

Slug rules: lowercase, non-alphanumerics collapsed to `-`, roughly five words. It is a human handle, not a description; the issue holds the description.

### `research` branches still end in a pull request

A research branch usually produces no production code. It still opens a PR, and the PR body is the artifact: the hypothesis, the data availability verdict, the result, and — most importantly — the negative result if there was one.

This is not ceremony. This system's job is to reject bad strategies (`CLAUDE.md` §1), and a recorded negative result is what stops the same hypothesis being re-investigated in six months by a session with no memory of this one. Merge it with the notebook or the analysis script under `research/`, or close it with the finding in the PR body. Do not delete the branch and leave the knowledge nowhere.

### Branch from `main`, not from another branch

The single exception is a deliberate **stacked PR**: branch B genuinely requires branch A's contract to exist. Then:

- Label both PRs `stacked`.
- Set B's base to A in GitHub (`gh pr create --base <A-branch>`), so the diff shown is B's alone.
- Merge bottom-up. GitHub retargets B to `main` when A merges.

Never stack more than two deep. A three-deep stack means the bottom PR is being reviewed while two others depend on its shape, and review feedback that changes the bottom invalidates both.

---

## 3. Commits

Conventional Commits. **One logical change per commit.**

```
<type>(<scope>): <imperative subject, lowercase, no period, ≤72 chars>

<body: why, wrapped at 72>

<trailers>
```

`<scope>` is the module: `domain`, `data`, `strategy`, `risk`, `execution`, `backtest`, `agents`, `evolution`, `platform`, `api`. Not a file name, not a feature name. The scope is machine-read (§4), so it must be one of those ten.

### Real examples

```
feat(risk): net correlated exposure before sizing

Sizing summed gross notional across symbols, so two correlated
longs each consumed the full per-symbol limit and the portfolio
carried roughly 2x the intended exposure to a single factor.

Correlation is computed over the trailing 30d of the same bars
the strategy saw, so the netting is point-in-time and replays
identically in backtest.

Refs #34
```

```
fix(data): key timestamp unit on (market, date), not a global constant

Binance spot switched to microsecond timestamps on 2025-01-01;
futures stayed in milliseconds. The global MILLIS constant parsed
2025+ spot trades as timestamps in the year 57000, which the
ingestion range filter silently dropped rather than rejecting.

Regression test asserts all four quadrants: spot before/after the
cutover, futures before/after.

Refs #47
```

```
perf(backtest): scan bars via DuckDB over Parquet instead of Postgres

Row-by-row Postgres reads were 78% of backtest wall time on a
3-symbol-year run. Columnar scan of the same partitions is 11x
faster and produces a trade-for-trade identical result (diff of
both run outputs is empty, pasted in the PR).

Refs #71
```

```
refactor(execution): split venue fill simulation from order routing

No behaviour change. BacktestVenue.submit() did two unrelated
jobs; the fill simulator is now callable from PaperVenue without
dragging in the routing table.

Characterization suite unchanged and green; golden backtest diff
empty.

Refs #58
```

### Why one logical change per commit

Because `git revert` is the rollback mechanism (see `RELEASE_PROCESS.md` §7). A commit that mixes a refactor with a behaviour change cannot be reverted — reverting it undoes structure that later commits depend on, and not reverting it leaves the defect. You discover this at the worst possible time, which is while something is misbehaving in the running demo.

If you have already mixed them, split with `git add -p` before pushing. If you have already pushed, split with an interactive rebase — the branch is yours and force-pushing your own feature branch is fine (§7).

### Required trailers

**`Refs #<n>`** on every commit. The issue thread is where scope was actually negotiated (see `/new-task` §3), and a commit without a link to it loses that.

**`Results-Invalidating: <one line>`** on any commit touching the cost model, a feature definition, the scoring engine, or the backtest engine.

This trailer is not documentation — it is parsed. `RELEASE_PROCESS.md` §4 builds the "results-invalidating changes" changelog section from `git log --grep='^Results-Invalidating:'` over the tag range. If the trailer is missing, the change silently disappears from the release notes and someone later compares a pre-change Sharpe to a post-change Sharpe as though they were on the same scale. Prose in the PR body does not substitute; PR bodies are not in the git history.

```
feat(backtest): model maker/taker split from realized fill side

Results-Invalidating: taker share now inferred per fill; all
Sharpe figures produced before this commit assumed 100% taker
and are systematically pessimistic by roughly 0.1-0.3.

Refs #66
```

**`Co-Authored-By:`** where applicable.

### What never goes in a commit

Debug output, commented-out code, scratch files, `TODO` markers, `raise NotImplementedError`. `CLAUDE.md` §9 covers why. The relevant git-specific point: these survive by accident. Nobody deletes someone else's `# print(pos)` because they cannot be sure it was not load-bearing.

---

## 4. What CI derives from your commit metadata

Worth knowing, because it makes the conventions load-bearing rather than cosmetic:

- **Scope → coverage floor.** A PR whose commits are scoped `risk` is checked against 95%, `platform` touching `safety/` against 100%. A wrong scope means the wrong floor is applied and a weakly tested change passes.
- **Type → required checks.** A `refactor` PR additionally requires the golden-backtest diff job. A `perf` PR requires before/after numbers in the body. A `docs` PR that touches `src/` fails, because a docs branch that quietly changed code is the one nobody reads carefully.
- **`Results-Invalidating:` trailer → changelog section and the results-epoch check.** See `RELEASE_PROCESS.md` §3.

---

## 5. Pull requests

Every branch ends in one. Requirements, all of them mandatory:

| Requirement | Why it is not optional |
|---|---|
| **Labels** inherited from the issue | The label set drives the required-checks matrix and the changelog grouping |
| **Milestone** inherited from the issue | An unmilestoned PR is invisible to `ROADMAP.md` reconciliation |
| **Assignee `ismetcahangirov`** | One person owns getting it merged; an unassigned PR is nobody's |
| **Linked issue** via `Closes #<n>` | The issue closes automatically and the scope negotiation stays attached |
| **Verification evidence** — actual command output | `CLAUDE.md` §7. A claim is not evidence |

Pull the metadata rather than guessing it:

```bash
gh issue view <n> --json title,labels,milestone
```

### Body structure

```markdown
## What
One paragraph. What the code now does that it did not before.

## Why
The problem. Not the solution restated in past tense.

## Verification
```
<the actual tail of `make check`, pasted>
```
Coverage on touched modules: risk 96.2% (floor 95%), domain 97.1% (floor 95%)

## Risk
- Safety kernel touched: no
- Backtest/live parity affected: no
- Results-invalidating: no
- Migration included: yes — forward-only, adds an index, no grants changed

## Out of scope
What was deliberately left out, and why.

Closes #34
```

The **Verification** block is the part reviewers actually use, and the part most often faked. Paste real output. `CLAUDE.md` §7 is unusually blunt about this because much of this repository's output is consumed by automated processes that cannot independently check a claim — a fabricated green build propagates.

The **Out of scope** section is what stops silent scope creep in both directions. `CLAUDE.md` §9: scaling work down is the user's decision, and this is where you record having made it visible.

### The `safety:critical` label

**Any PR whose diff touches `src/fking/platform/safety/` carries the `safety:critical` label**, stated in the first line of the PR body, regardless of how trivial the change looks.

This includes: adding a host, removing a host, reformatting the allowlist, changing a type annotation on the allowlist, editing `guarded_client()`, editing the startup endpoint check, touching the `import-linter` contracts that forbid raw HTTP clients in `execution`, and editing the safety module's tests.

It especially includes changes that look like cleanups. A `frozenset` reformatted into a multi-line literal is exactly the diff in which one entry changes and nobody notices, because the whole block shows as changed.

The label has teeth:

- Branch protection requires a review from the repository owner on `safety:critical` PRs. No self-approval, no automation approval.
- The `safety-kernel-diff` CI check **fails by design** whenever that path changes. It is not a bug to be fixed; it is a forced stop. Merging requires an explicit human override with a written reason on the PR.
- The PR is listed individually, with its diff, in the release notes (`RELEASE_PROCESS.md` §4).

If you find yourself widening the allowlist or adding an override "so it can be tested more easily" — stop and ask the user. `CLAUDE.md` §0. The friction is the feature.

### Size

Soft limit **~400 substantive changed lines** — excluding lockfiles, recorded fixtures, generated code, and pure moves.

```bash
gh pr view --json additions,deletions,changedFiles
```

An unreviewable PR is an unreviewed PR (`CLAUDE.md` §11). Not "reviewed less well" — unreviewed. Past a few hundred lines, review degrades into skimming, and skimming is exactly how the mutable domain object and the deleted assertion get in.

---

## 6. When a PR has grown too large

It will happen. The handling matters, because the obvious fix destroys the review history.

**Do not** `git reset` the branch and re-commit in smaller pieces. Force-pushing a rewritten history orphans every existing review comment — GitHub marks them "outdated" and collapses them, and the reasoning in them is effectively lost. If review has already started, that reasoning is the most valuable thing on the PR.

Instead, **extract downward into a stack**:

1. Identify the piece with the fewest dependencies — usually a pure type in `domain/`, a helper, or a migration.
2. Branch it off `main` fresh:
   ```bash
   git checkout main && git pull origin main
   git checkout -b feat/34-a-signal-envelope-type
   git cherry-pick <the commits that belong to this piece>
   ```
   Cherry-pick works cleanly here precisely because of the one-logical-change-per-commit rule. This is the payoff for that discipline.
3. Open the small PR. Get it reviewed and merged.
4. Rebase the original branch onto the new `main`:
   ```bash
   git checkout feat/34-original
   git rebase main
   ```
   The cherry-picked commits drop out as already-applied, and the original PR shrinks by exactly that much. Existing review comments on the *remaining* commits survive, because those commits' content did not change.
5. Repeat until under the limit.

If the work genuinely cannot be split — a single coherent algorithm, a mechanical rename across many files — say so explicitly in the PR body and state what makes it atomic. A stated exception that a reviewer accepts is fine. An unstated 900-line PR is not.

**Prevention is cheaper.** Check size at the halfway point, not at the end:

```bash
git diff main...HEAD --stat | tail -1
```

If that is already near 400 lines and the work is half done, split now, while the pieces are still separable in your head.

---

## 7. Branch protection on `main`

Configured on the repository, not by convention:

- **No direct pushes.** All changes arrive via pull request.
- **No force-push. Ever.** Not by anyone, not with `--force-with-lease`. `main`'s history is the reference the release tags and the runtime state snapshots point into (`RELEASE_PROCESS.md` §6); rewriting it makes every prior release note wrong about what was running.
- **No branch deletion.**
- **Linear history required.** No merge commits from `main` into feature branches; rebase your branch instead. A tangled history makes `git log --oneline <tag>..HEAD` — the input to the changelog — unreadable.
- **Required status checks, all green**: `ruff`, `ruff format --check`, `mypy --strict`, `lint-imports`, the test suite, per-module coverage floors, and `safety-kernel-diff`.
- **Required review**: one approval. Two, one of which is the repository owner, for `safety:critical`.
- **Conversation resolution required.** An unresolved review thread blocks merge. This is what stops "will fix in a follow-up" from becoming "was never fixed".
- **Stale approvals dismissed on new commits.** An approval is of a specific diff.

Force-pushing **your own feature branch** is fine and often correct — rebasing onto `main`, fixing up a commit message, splitting a mixed commit. The prohibition is on `main` and on any branch someone else has based work on.

### Merge method: squash by default, merge commit for migrations

Squash-merge is the default. It keeps `main` linear and makes each `main` commit revertable as a unit.

**Exception: a PR containing an Alembic migration is merged with a merge commit, preserving the migration in its own commit.**

The reason is specific and bites hard. If a migration is squashed together with the code that uses it, `git revert` of that squashed commit deletes the migration *file* from the tree — but the migration has already run against the database and its revision id is still sitting in `alembic_version`. Alembic then starts up, reads a current revision id that has no corresponding file in the tree, and refuses to do anything at all: not upgrade, not downgrade, not tell you what is wrong in a useful way. You are now hand-editing `alembic_version` on a live database to recover, which is precisely the operation nobody wants to be doing under time pressure.

With the migration preserved as its own commit, you revert the code commit and leave the migration in place. A migration that adds an unused column is harmless. A missing migration file is not.

Corollary: **migrations are forward-only.** Write the `downgrade()` because Alembic requires it, but the rollback path is a new forward migration, never a downgrade run against a database holding real audit history.

---

## 8. After merge

```bash
git checkout main && git pull origin main
git branch -d <type>/<n>-<slug>
```

Delete the remote branch too — GitHub does this automatically if configured, otherwise `gh pr merge --delete-branch`.

If the merge touched a feature definition, the cost model, the scoring engine, or the backtest engine, confirm the `Results-Invalidating:` trailer made it into `main`:

```bash
git log --grep='^Results-Invalidating:' -1 --format='%H %s'
```

If it did not, the change is now invisible to the release process. Open a `docs:` PR adding it to `CHANGELOG.md` by hand and say why the trailer was missed — do not rewrite `main` to insert it.

---

## 9. Tags and releases

Tags are annotated, immutable, and never moved:

```bash
git tag -a v0.4.0 -m "v0.4.0"
git push origin v0.4.0
```

A bad release gets `v0.4.1`, never a moved `v0.4.0`. Release notes record the runtime state at cut time and rollback instructions reference the tag by name; moving a tag makes both silently wrong, and both are read exactly when something is already going badly.

Full process in `RELEASE_PROCESS.md`.

---

## 10. Quick reference

```bash
# Start
git status --porcelain && git checkout main && git pull origin main
git checkout -b feat/<n>-<slug>

# Work
git add <specific paths>          # not -A; -A is how scratch files get committed
git commit                        # Conventional Commits + Refs #<n>

# Check size before you are committed to the shape
git diff main...HEAD --stat | tail -1

# Ship
make check                        # must be green, run now, output pasted
git push -u origin "$(git branch --show-current)"
gh pr create --assignee ismetcahangirov --base main \
  --label "<from the issue>" --milestone "<from the issue>"

# After merge
git checkout main && git pull origin main && git branch -d feat/<n>-<slug>
```

`/new-task`, `/build`, `/ship` and `/review` in `.claude/commands/` automate these steps with the checks attached.
