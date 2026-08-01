# Code Review

What blocks a merge, what is a comment, and how to review code that was mostly written by a language model with no memory of writing it.

The organising principle: **review effort is allocated by how silently a defect fails, not by how hard it was to write.** A subtle three-line change to a rolling window can invalidate a year of backtest results without ever raising an exception. A 200-line FastAPI router that is wrong will 500 on first use and cost nothing. Review the first one carefully and the second one quickly.

`.claude/agents/code-reviewer.md` runs the line-level pass. This document is the policy that pass implements, plus the parts that are human judgement.

---

## 1. Blocking versus comment

### Blocking — the merge does not happen

These are not negotiated in PR threads. If one is present, the verdict is **Request changes**, regardless of how good the rest of the PR is.

1. **A change to `src/fking/platform/safety/` without the `safety:critical` label and an owner review.** Not "the change looks safe" — the label and the human decision are the process, and the process is the guarantee.
2. **Look-ahead.** Centred rolling windows, `shift(-n)`, normalization computed over the full range, a join that forward-fills from the future, a label leaking into a feature, a cache key that omits as-of time.
3. **Money as `float`.** Including intermediates, including inside a comprehension, including "just for the log line". Including `Decimal(str(x))` where `x` was ever a float.
4. **Naive datetime, or `datetime.utcnow()`, or a clock read inside `strategy/` or `risk/`.**
5. **A mutable domain object**, or a state transition that mutates `self` and returns `None`.
6. **A bus consumer with no dedupe key.**
7. **A migration granting `UPDATE` or `DELETE` on an audit table, or dropping a rejecting trigger.**
8. **Direct HTTP/WebSocket client construction in the execution path**, or an `import-linter` contract moved into `ignore_imports`, or a function-level import that routes around a contract.
9. **A strategy computing a quantity, notional, or leverage**, or importing `execution`.
10. **A swallowed error** — `except Exception` followed by `continue`, `pass`, or log-and-proceed.
11. **A fake implementation** — `NotImplementedError`, a `TODO` standing in for the work, a stub that looks finished, documentation describing something that does not exist.
12. **A verification claim in the PR body that CI did not actually run**, or that you re-ran and could not reproduce. This is blocking on its own, independent of whether the code is correct. See §4.
13. **Coverage below the module's floor** without an explicit, accepted justification in the PR body.

### Should fix — usually before merge, negotiable with a reason

- Speculative abstraction: a protocol, base class, or registry with one implementation. Two concrete callers before the abstraction exists.
- Ambiguous names without units: `size`, `price`, `amount`, `qty`, `timeout`.
- An unsourced constant in `risk/` or the cost model.
- A test that asserts the code ran rather than what it did.
- Missing Hypothesis properties on new position or risk arithmetic.
- Hand-written exchange fixtures instead of recorded responses; a mocked database.
- A new cross-module hop with no event, no correlation ID propagation, or no audit row.
- A commit mixing a refactor with a behaviour change.
- A PR over ~400 substantive lines with no stated reason for being atomic.

### Comment — say it, do not block on it

- Style preferences the linter does not encode.
- "There is a simpler way to write this" where the current way is correct and clear.
- Suggestions for future work — as long as they are framed as suggestions and are not left as `TODO`s in the code.
- Naming that is fine but could be better.
- Questions. A question is not a blocking finding until it is answered badly.

**Do not blend the categories.** A review that mixes a `float` in the money path with a preference about tuple unpacking, both unlabelled, gets both treated with the same weight — which in practice means both get treated with the weight of the smaller one.

---

## 2. Reviewer checklist, ordered by silence

Work the list top-down. The order is the whole point: the earliest items fail without any signal at all, and once you are twenty minutes into a review your attention is worse than it was at the start.

**1. Look-ahead.** Does any feature, label, or backtest computation touch data from after its as-of time?

Where it hides: `df.rolling(20, center=True)`, `.shift(-1)`, `StandardScaler().fit(full_df)`, `merge_asof(direction="forward")`, a `groupby().transform("mean")` over the entire frame, a cache lookup keyed on symbol alone. Also, subtler: a `dropna()` that removes rows based on a future column's availability.

Why first: it never fails. It produces better numbers. Every incentive in the system points toward not noticing it.

**2. Safety kernel and network construction.** Does the diff widen the allowlist, add an override, construct a client outside `guarded_client()`, weaken an import contract, or reach the network from a function-level import?

Why second: also silent, and the failure mode is trading real money.

**3. Money types.** Trace every price, quantity, and monetary value back to where it entered the process. The question is never "is this line a `Decimal`" — it is "was this value ever a `float`?"

Why third: silent, cumulative, and presents as an exchange bug.

**4. Time.** Every `datetime` construction, comparison, and serialisation boundary. `utcnow()` is naive and always wrong here. An aware datetime in a non-UTC zone passes a `tzinfo is not None` check and is still wrong.

**5. Mutability.** Every new `domain/` type: `frozen=True`? Every field: is the field *type* immutable, or is it a `list` inside a frozen class? Every transition: does it return a new object?

**6. Idempotency.** Every new bus consumer: what is the dedupe key, and is it derived from the event or from our own processing? A dedupe key we generate on receipt deduplicates nothing.

**7. Error handling.** Every `except`. Is the exception specific? Is it handled, or logged and stepped over? Is the retryable/terminal distinction made by type or by parsing a message string?

**8. Boundaries.** Exchange responses, agent output, config, API input. Parsed and validated, or indexed optimistically? `response["result"][0]["price"]` is a crash waiting for a bad day.

**9. Audit and instrumentation.** New cross-module hop: does the correlation ID propagate? Is there an audit row? Deferred instrumentation is never added properly and is missing from exactly the history the next investigation needs.

**10. Tests.** Do they assert behaviour? Is there a property test for position arithmetic? Would they pass against a deliberately broken implementation? (See §4.3.)

**11. Naming, constants, comments.** Units in names. Provenance on constants. Comments explaining *why*.

**12. Structure.** Is the abstraction earning its place? Does the code belong in this module — what does it know about?

### Read the deletions as carefully as the additions

Most review interfaces collapse removed lines by default, and reviewers scroll past them. A deleted assertion, a deleted provenance comment, a deleted `frozen=True`, a deleted validation branch — all of them are invisible in a casual read and all of them are exactly as dangerous as a bad addition.

```bash
git diff main...HEAD | grep '^-' | grep -vE '^---' | less
```

Run that. It takes thirty seconds and it is the highest-yield mechanical step in the whole review.

---

## 3. Reviewing AI-authored code

Most of this codebase is written by language models across sessions with no shared memory. That changes what review is for, because AI-authored code fails differently from human-authored code.

**It is locally plausible and globally inconsistent.** Every function reads well. The problem is that the same concept now exists under three names in three modules, because each session invented it fresh. Human review catches "this is wrong"; here you also need "this already exists, and is called `NotionalLimit` in `risk/limits.py`".

```bash
# Before accepting any new type, protocol, or helper:
grep -rn "class <SimilarName>" src/fking/
grep -rn "def <similar_verb>" src/fking/
```

**It follows the letter of a rule and misses its purpose.** A session told "use `Decimal` for money" will write `Decimal(str(computed_float))` and consider the rule satisfied. A session told "tests must be property-based" will write a Hypothesis test whose property is trivially true for any implementation. The rule is present; the protection is not. Ask what defect the rule exists to prevent, then check whether *that* is prevented.

**It produces tautological tests.** This is the most consistent failure mode and the most dangerous, because it makes the coverage number lie. A model that writes an implementation and then writes tests for it will write tests that pass against that implementation, including against its bugs. The test is derived from the code, so it cannot disagree with the code.

**It is confident about verification it did not perform.** See §4.

### 3.1 Never trust a stated verification claim. Re-run it.

The PR body says `make check` is green. Run it.

```bash
gh pr checks <n>                       # did CI actually run, and on this head SHA?
gh pr view <n> --json headRefOid
git fetch origin pull/<n>/head && git checkout FETCH_HEAD
make check
make test ARGS="--cov=src/fking/<touched module> --cov-branch --cov-report=term-missing"
```

Three distinct things to check, and they fail independently:

1. **Did CI run at all?** A PR with no checks, or checks on a stale SHA, is unverified regardless of what the body says.
2. **Does it pass on your machine?** A pass that depends on someone's warm cache or local state will fail in the release rebuild.
3. **Does the pasted output correspond to this diff?** Output pasted from an earlier run, before the last three commits, is a false claim even though every character of it is real.

`CLAUDE.md` §7 is blunt about this because much of this system's output is consumed by automated processes that cannot independently check a claim. A fabricated green build does not just mislead one reviewer — it propagates into the release notes, the runtime state snapshot, and the next session's assumptions.

**A PR body claiming verification that did not happen is itself a blocking finding**, and it is blocking even if the code turns out to be perfect. The claim is the defect.

### 3.2 Check the reasoning, not just the result

AI-authored PRs usually include an explanation. The explanation is a hypothesis about the code, not a description of it, and the two diverge exactly where the bug is.

If the body says "correlation is computed over the trailing 30 days", find the line and check that the window is trailing, that it is 30 days rather than 30 bars, and that the data it reads is point-in-time. Where the prose and the code disagree, the prose is usually the intention and the code is usually what will run.

### 3.3 The tautological-test check: break it on purpose

The cheapest way to find out whether a test suite tests anything:

```bash
# Introduce a deliberate defect in the code under review
#   - flip a comparison operator in a limit check
#   - remove a quantize() call
#   - change a rolling window from 20 to 21
make test ARGS="tests/<the new tests>"
```

If the tests still pass, they were derived from the implementation and assert nothing. Revert your change and request real tests.

This takes two minutes and it is the only reliable defence against a 96% coverage number that means nothing. For `platform/safety` this is automated as a mutation gate (`TESTING.md` §6.1); everywhere else it is a manual spot check, and the place to spend it is on any new risk or position arithmetic.

### 3.4 Ask "what does this not handle?"

AI-authored code covers the described case well and adjacent cases not at all. The productive review question is not "is this correct?" but "what input makes this wrong?" — partial fills, zero quantities, a redelivered event, an exchange returning a field as a string this time, a position that is already flat, a symbol delisted mid-run.

---

## 4. Giving review feedback

### 4.1 Explain the defect, not the rule

"Use `Decimal`" is not a review comment. It tells the author what to type and nothing about why, so the next diff has the same problem.

```
This is a float, so after roughly 3,000 fills the running position
notional drifts from the exchange's by enough to trip reconciliation —
and it will present as an exchange bug for a day before anyone suspects
this line.

    price = Decimal(row["price"])   # row["price"] is already str from the
                                    # CSV reader; do not go via float
```

Every finding: quote the line, name the concrete defect it produces, give the actual replacement. The author should be able to fix it from the comment alone without asking a follow-up question.

### 4.2 Label blocking and non-blocking explicitly

Prefix every comment: `[blocking]`, `[should fix]`, or `[note]`. Unlabelled feedback is negotiated by tone, and tone is a terrible protocol.

### 4.3 Order findings blocking-first, then by file

Authors fix top-down. Interleaving a blocking finding among eight style notes buries it.

### 4.4 If you find the same defect twice, stop and look for the template

A repeated defect in one PR is usually a copy of an existing pattern in `main`. Grep for it:

```bash
grep -rn "<the defect pattern>" src/fking/
```

If it exists elsewhere, that is a separate finding and a separate issue — and it is more important than the PR you are reviewing, because it is already running.

### 4.5 State what you actually verified

An approval ends with one sentence on what you *ran*, not what you read:

> Approve — ran `make check` on FETCH_HEAD (green, output matches the body), re-ran the risk coverage at 96.2% branch, and confirmed the new property test fails when I flip the `>=` in `_check_gross_limit` to `>`.

An approval with no such sentence is a statement that the diff looked fine, which is a much weaker claim and should be phrased as one.

---

## 5. Receiving review feedback

The failure mode here is not defensiveness. It is **performative agreement** — "Good catch, fixed!" applied to feedback that was wrong, or that was right for the wrong reason, or that was not understood.

That is worse than arguing, because it produces a change nobody has actually reasoned about, approved by a reviewer who now believes the issue was understood.

### 5.1 Before implementing any suggestion, decide whether it is correct

Three outcomes, all legitimate:

**It is correct.** Fix it, and say what the defect actually was — not "fixed", but "fixed: the window was inclusive of the current bar, so the feature saw its own close". That confirms you understood it, which is the thing the reviewer needs to know.

**It is incorrect.** Say so, with the specific reason:

> This is deliberate. `avg_entry` is intentionally not recomputed on a closing fill — recomputing it there inflates the basis on every partial close. The property test `test_partial_close_preserves_avg_entry` pins this; it fails with the change you are suggesting. Happy to add a comment above the branch making it explicit.

**You do not understand it.** Say that. "I do not follow — which future bar do you think this reads?" is a good comment. Implementing something you do not understand is how a correct implementation becomes an incorrect one during review.

### 5.2 Verify before agreeing

If the reviewer says a test fails, run it. If they say a defect exists elsewhere too, grep for it. Roughly one review comment in ten is wrong on the facts, and a comment accepted without checking becomes a change accepted without checking.

### 5.3 A disputed non-negotiable is escalated, not argued

The rules in `CLAUDE.md` §2 are not negotiated in PR threads. If a reviewer blocks on one and you believe it does not apply, state the reason once, cite the location, and escalate to the user. Two rounds of PR comments on whether `Decimal` is really necessary here is two rounds nobody gets back.

If the rule genuinely should change, that is an amendment to `CLAUDE.md`, which happens by pull request, never in passing (`CLAUDE.md`, preamble).

---

## 6. Review size

**Soft limit: ~400 substantive changed lines**, excluding lockfiles, recorded fixtures, generated code, and pure file moves.

```bash
gh pr view <n> --json additions,deletions,changedFiles
```

Past that, review quality collapses. It does not decline gradually — attention runs out and the remainder gets skimmed, and skimming is precisely how the mutable domain object, the deleted assertion, and the naive datetime get through. An unreviewable PR is an unreviewed PR.

**As a reviewer, refuse rather than skim.** A partial line-level review that ends in an approval is a false assurance, and it is worse than no review because the merge now carries a signature. Say: "This is 940 lines; I will review the `domain/` types and the migration, and I need the rest split." Then propose the split (`GIT_WORKFLOW.md` §6).

**Time-box it.** Sixty minutes is the outer limit for one sitting. Past that, stop and finish tomorrow, or hand the remainder to another reviewer. A review completed in one heroic three-hour session is a review whose last hour found nothing.

**Read the whole file, not just the hunks.** Context is where the mutation is. A diff showing a new method on `Position` does not show you that `Position` lost its `frozen=True` two commits earlier in the same branch.

---

## 7. Verdicts

**Approve** — no blocking findings, and you ran the verification yourself. One sentence on what you ran.

**Approve with comments** — no blocking findings; the should-fix and note items are the author's call. Use this when the remaining items genuinely do not need re-review; if they do, request changes instead.

**Request changes** — one or more blocking findings, or the size limit is exceeded, or the verification claim did not hold.

There is no "LGTM". It means nothing and it is what people write when they have not read the diff.

---

## 8. What review does not do

Review is not the primary defence. `make check` is: `ruff`, `ruff format`, `mypy --strict`, `import-linter`, the test suite, the coverage floors, and the safety mutation gate. Anything a machine can check should be checked by a machine, and every time review catches something mechanical, the correct follow-up is a lint rule rather than a better checklist.

What review is uniquely for, and cannot be automated:

- **Does this belong here?** Module placement, whether the abstraction earns its existence, whether the concept already exists under another name.
- **Is the reasoning sound?** Does the change do what its rationale claims, and is the rationale correct about the market or the exchange?
- **Do the tests test anything?** A machine can measure coverage; only a human notices that the property asserted is trivially true.
- **What is missing?** The unhandled partial fill, the absent audit row, the error path with no test. Nothing in `make check` can flag an absence.

If you spend a review on things `ruff` would have caught, you have spent it on the cheap half.
