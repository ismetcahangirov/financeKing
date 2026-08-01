---
description: Start work on a GitHub issue — pull main, branch correctly, read the issue, state a plan before touching code
argument-hint: <issue-number>
allowed-tools: Read, Grep, Glob, Bash
---

Start work on issue #$1. Do **not** write any implementation code in this command. The output is a branch and a plan.

## 1. Confirm the tree is clean

```bash
git status --porcelain
```

If anything is uncommitted, stop and report it. Do not stash silently — uncommitted work in this repo is usually a half-finished migration or a recorded exchange fixture, and both are easy to lose.

## 2. Pull main

```bash
git checkout main
git pull origin main
```

If the pull is not a fast-forward, stop and report. Never resolve a `main` divergence as part of starting a task.

## 3. Read the issue

```bash
gh issue view $1 --json number,title,body,labels,milestone,assignees,comments
```

Read the comments as well as the body. In this repo the body is usually the original ask and the comments are where the scope was actually settled.

## 4. Derive the branch name

`<type>/<issue-number>-<kebab-slug>` where `<type>` is one of `feat`, `fix`, `docs`, `chore`, `refactor`, `test`, `perf`, `research`.

Pick the type from the issue labels, not from the title's phrasing. Slug is the issue title, lowercased, non-alphanumerics collapsed to `-`, trimmed to roughly 5 words.

```bash
git checkout -b <type>/$1-<kebab-slug>
git branch --show-current
```

Print the branch name so it is in the transcript.

## 5. Locate the blast radius

Before planning, find what already exists. Use Grep/Glob over `src/fking/` and `tests/`:

- Which module does this belong in? Decide by asking *what does this code know about?* Code that knows about order types goes in `execution`; code that knows about order types **and** feature engineering is two pieces of code that have not been separated yet.
- Does it cross the `strategy` → `execution` boundary? If yes, the design is wrong — re-read `RISK_PHILOSOPHY.md` before planning.
- Is there an existing ADR? `ls docs/adr/` and grep for the subject. An accepted ADR is binding; superseding it means writing a new ADR, not editing the old one.
- Does it touch `src/fking/platform/safety/`? If yes, the plan must say so explicitly and the PR will need the `safety:critical` label and a human decision.

## 6. State the plan

Write the plan into the issue thread so it survives this session:

```bash
gh issue comment $1 --body "<plan>"
```

The plan must state, in this order:

1. **Files to be created or changed**, with the module each lands in.
2. **The contract**: types in and out. Name units — `notional_usd: Decimal`, `timeout_seconds: float`. `size` and `price` alone are not acceptable names here.
3. **How it will be verified**: the exact commands, and for `risk`/`domain` work, which Hypothesis properties will be asserted (not just example cases).
4. **Coverage floor that applies** to the touched modules: `platform/safety` 100%, `risk` 95%, `domain` 95%, `execution` 90%, everything else 80%.
5. **What is explicitly out of scope**, so the PR does not quietly widen.
6. **Any assumption that would waste the work if wrong** — ask the user about that one now, before building.

## 7. Stop

Report the branch name, the plan summary, and anything that needs the user's decision. Then stop. Implementation happens in `/build`.
