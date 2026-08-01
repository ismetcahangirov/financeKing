# Template — Pull Request

Paste the body below into `gh pr create --body-file`, or keep it at `.github/pull_request_template.md` if you are wiring it into the GitHub UI. The branch is already `<type>/<issue-number>-<kebab-slug>`; the PR needs labels, a milestone, and an assignee before it is ready for review, and a PR without all three does not get looked at.

**The verification section takes pasted command output, not a claim.** "Tests pass" is not evidence, it is an assertion about evidence, and this project runs largely on output that other automated processes cannot independently check (`CLAUDE.md` §7). If you did not run `make check`, say you did not run it and say why. That is a useful pull request. A false green is worse than a red one because it costs the reviewer their calibration on everything else you write.

Related: `../workflows/`, `CODE_REVIEW.md`, `GIT_WORKFLOW.md`, `issue.md`.

---

```yaml
---
closes: <#N>
labels: [<type label, plus any of safety:critical, breaking-change, needs-human>]
milestone: <milestone name>
assignee: <human username or agent name>
reviewers: [<who or what reviews this>]
---
```

---

## What changed

*One paragraph, past tense, describing the change as a reader of the diff would recognise it. Name the modules and the types. Do not restate the issue — the reader can follow `Closes #N`. If the paragraph needs a bulleted list of unrelated items, this is more than one pull request and should be split before review, not after.*

```
<One paragraph. Modules touched, types added or changed, behaviour that is now different.>
```

---

## Why

*The linked issue plus one or two sentences of context the issue does not carry — typically a decision made during implementation that a reviewer would otherwise have to reverse-engineer from the diff.*

```
Closes #<N>

<Anything decided during implementation that is not in the issue, and why.>
```

---

## How it was verified

*Paste the real terminal output. Not a summary of it, not a screenshot of a green tick, the output. Include the command, the trailing summary lines, and the timestamp or duration. Trim the middle if it is long; never trim the result line.*

```console
$ make check
<paste actual output — ruff, format check, mypy --strict, import-linter, pytest summary>
```

```console
$ <the specific verification command from the issue's acceptance criteria>
<paste actual output>
```

**Coverage against the floors** *(`platform/safety` 100%, `risk` 95%, `domain` 95%, `execution` 90%, everything else 80%)*

```console
$ <coverage command>
<paste actual per-module output for the modules this PR touches>
```

**Not run, and why.** *Every check that was skipped, with the reason and what that leaves unverified. An empty entry here means every check ran; write "nothing was skipped" rather than leaving it blank, so the reviewer can tell the difference between "all green" and "the author did not fill this in".*

```
<check that was not run> — <why> — <what remains unverified as a result>
```

> Example: `testcontainers` Postgres suite not run locally (Docker unavailable on this machine); CI ran it on the branch, run link `<url>`. The migration's `down` path is therefore verified only by CI, not by me.

---

## Non-negotiables self-review

*Tick each after actually looking at the diff for it, not from memory of having written it. These are the defect classes that are silent, expensive, and found late (`CLAUDE.md` §2, §9).*

- [ ] **No money as `float`.** Every price, quantity and monetary amount is `Decimal`, constructed from `str`.
- [ ] **No naive datetimes.** Every datetime is timezone-aware UTC and naive values are rejected at construction, not coerced.
- [ ] **No mutable domain objects.** New or modified domain types are frozen; state transitions return new objects.
- [ ] **No network call bypassing `guarded_client()`.** No direct `httpx`, `aiohttp`, `websockets` or `requests` client construction in the execution path.
- [ ] **`strategy` does not import `execution`,** directly or transitively. `import-linter` output above confirms it.
- [ ] **No look-ahead.** Every feature this PR computes or consumes uses only data available at its own timestamp, and the adversarial leak test still fails closed.
- [ ] **New event bus consumers are idempotent.** Redelivery of the same message produces the same end state. Name the idempotency key below.
- [ ] **Audit writes are append-only.** No `UPDATE` or `DELETE` against an audit table, and the DB constraint still enforces it.
- [ ] **Cost model parameters, if touched, are production-calibrated,** with the artefact cited. No testnet-derived numbers.
- [ ] **No debug output, commented-out code, or scratch files** left in the diff.
- [ ] **Every `# type: ignore` carries an inline reason** explaining why it is unavoidable.
- [ ] **Every new non-obvious constant carries a provenance comment** naming its source.

```
Idempotency key for any new consumer: <field or composite, and where it is enforced>
```

---

## Contracts and migrations changed

*Every consumer-visible change, with what a consumer must do about it. Write "none" per row rather than deleting rows — a missing row reads as "not considered", which is exactly the state that breaks a downstream reader quietly.*

| Contract | Change | What consumers must do |
|---|---|---|
| Domain types | `<change, or none>` | `<action, or nothing>` |
| Event bus schemas | `<subject and payload, or none>` | `<action>` |
| DB schema | `<migration id and effect, or none>` | `<action>` |
| Agent input/output models | `<change, or none>` | `<action>` |
| API routes | `<change, or none>` | `<action>` |
| `import-linter` contracts | `<change, or none>` | `<action>` |

```
Migration: <id> — forward: <what it does> — backward: <what `down` restores, and what it
           cannot restore>
```

---

## Rollback plan

*What to do if this is wrong in production-equivalent conditions. A revert is not automatically a rollback: if the migration dropped a column or a consumer already read the new event shape, reverting the code leaves the system in a state neither version expects. Say which case this is.*

```
Revert safe:      <yes | no>
If no, because:   <the irreversible step>
Rollback steps:   1. <command> 2. <command> 3. <verification>
Data written under the new behaviour: <what happens to it on rollback>
Runbook:          <link to ../templates/runbook.md-derived procedure if one applies>
```

---

## Screenshots and artefacts

*Where the change produces something a reviewer should look at rather than read: dashboard panels, backtest equity curves, agent transcripts, generated reports. Attach the artefact path or the image. Write "not applicable" for pure backend changes with no visual or artefact output — do not attach a screenshot of passing tests, the output above already covers that.*

```
<artefact path or embedded image, with one line saying what the reviewer should notice in it>
```

---

## Reviewer attention

*Name the one part of this diff most likely to be wrong, and say why you think so. Not the hardest part and not the part you are proudest of — the part where you had the least confidence, made an assumption you could not check, or wrote something that works for reasons you cannot fully articulate. A reviewer given a specific place to look finds things; a reviewer given a whole diff skims it.*

```
File and lines: <path:line-range>
Why it is the risky part: <the assumption you could not verify, or the case you are unsure about>
What would prove it wrong: <the test, the input, or the observation>
```

> Example: `src/fking/risk/netting.py:88-121`. The correlation-aware netting path assumes the exposure matrix is positive semi-definite after the shrinkage step; I could not construct a counterexample but I also could not prove it. A Hypothesis run over degenerate correlation inputs would settle it, and I did not write one.

---

## Definition of done

- [ ] Branch matches `<type>/<issue-number>-<kebab-slug>` and commits follow Conventional Commits
- [ ] `Closes #N` is present and links to a real issue
- [ ] `make check` output is pasted, real, and green — or explicitly marked as not run with a reason
- [ ] Coverage output for touched modules is pasted and clears the per-module floors
- [ ] "Not run, and why" is filled in, including the words "nothing was skipped" when that is the case
- [ ] Every non-negotiable box was ticked after looking at the diff, not from memory
- [ ] Contracts table has every row filled, "none" included
- [ ] Rollback plan states whether a plain revert is safe, and what happens to data written under the new behaviour
- [ ] Reviewer attention names a specific file and line range and admits an actual uncertainty
- [ ] Labels, milestone and assignee are set
- [ ] Diff is under roughly 400 lines, or the PR body explains why it could not be split
