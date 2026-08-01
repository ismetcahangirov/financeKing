---
description: Verify green, commit with Conventional Commits, push, and open a labelled PR linked to its issue
argument-hint: <issue-number>
allowed-tools: Read, Grep, Glob, Bash
---

Ship the current branch for issue #$1. Nothing here is optional and nothing here may be reported as done without its command output in the transcript.

## 1. Refuse to ship from main

```bash
git branch --show-current
```

If it is `main`, stop. Never commit to `main`, never force-push to it.

## 2. Read your own diff first

```bash
git diff main...HEAD --stat
git diff main...HEAD
```

Read it. Check, specifically:

- Any `float` touching a price, quantity or monetary amount → must be `Decimal`, constructed from `str`.
- Any naive `datetime`, any `datetime.now()` inside `strategy/` or `risk/` → both are defects here, not style.
- Any new `httpx`/`aiohttp`/`websockets`/`requests` construction in the execution path → must go through `fking.platform.safety.guarded_client()`.
- Any new mutable domain object, any `@dataclass` in `domain/` without `frozen=True`.
- Any leftover debug print, commented-out code, scratch file, `TODO`, or `raise NotImplementedError`.
- Any new bus consumer that is not idempotent. Redis Streams delivers at least once; a consumer that double-applies a fill is a position bug that reproduces only in production.
- Any magic constant in `risk/` without a provenance comment.

Fix what you find before continuing. This is cheaper than a review round.

## 3. `make check` must be green

```bash
make check
```

This runs ruff, format check, `mypy --strict`, `import-linter`, and the test suite.

**Do not proceed on a red or skipped run.** If it fails, fix the cause and run it again; paste the failing output if you cannot. A PR opened on an unverified build is worse than no PR, because the next reader will trust it.

If `import-linter` fails, do not relax the contract to make it pass. The two contracts that matter — `strategy` cannot import `execution`, and `execution` cannot import raw HTTP clients — are the architecture, not lint noise.

Capture the tail of the green output; it goes in the PR body verbatim.

## 4. Commit

One logical change per commit. A commit mixing a refactor with a behaviour change is unreviewable and unrevertable — split it with `git add -p` if that has happened.

Conventional Commits, type matching the branch type:

```bash
git add <specific paths, not -A>
git commit -m "feat(risk): net correlated exposure before sizing

Sizing previously summed gross notional across symbols, so two
correlated longs consumed one symbol's limit twice.

Refs #$1"
```

Subject imperative, lowercase, no trailing period, under 72 chars. Body explains *why*. Scope is the module: `domain`, `data`, `strategy`, `risk`, `execution`, `backtest`, `agents`, `evolution`, `platform`, `api`.

## 5. Push

```bash
git push -u origin "$(git branch --show-current)"
```

## 6. Open the pull request

Pull the issue's milestone and labels so the PR inherits them rather than being guessed:

```bash
gh issue view $1 --json title,labels,milestone
```

```bash
gh pr create \
  --title "<type>(<scope>): <what changed>" \
  --body "$(cat <<'EOF'
## What
<one paragraph>

## Why
<the problem, not the solution restated>

## Verification
```
<paste the actual tail of `make check` — the real output, not a claim>
```

Coverage on touched modules: <module> <pct> (floor <pct>)

## Risk
- Safety kernel touched: yes/no
- Backtest/live parity affected: yes/no
- Migration included: yes/no (append-only audit tables must stay append-only)

## Out of scope
<what was deliberately left out and why>

Closes #$1
EOF
)" \
  --assignee ismetcahangirov \
  --label "<labels from the issue>" \
  --milestone "<milestone from the issue>" \
  --base main
```

If the diff touches `src/fking/platform/safety/`, add `--label safety:critical` and say so in the first line of the PR body. That path changes the demo-only guarantee and needs a human decision, not a review rubber-stamp.

## 7. Size check

```bash
gh pr view --json additions,deletions,changedFiles
```

If the diff is over ~400 changed lines excluding lockfiles, fixtures and generated code, say so plainly and propose the split. An unreviewable PR is an unreviewed PR.

## 8. Report

Give the PR URL, the commit subjects, the `make check` result, and anything intentionally left out.
