# Workflow — AI Review

Reviewing code written by an AI agent, or having an AI agent review code. Both directions are covered here because both are normal in this repository.

The governing fact: **this codebase is written mostly by AI across sessions with no shared memory.** Types, contracts, and this operating system are the only durable memory between them. Review accordingly — the reviewer is not checking whether the author was careless, it is checking whether the durable contracts held.

---

## 1. Know which failure modes are AI-specific

These appear far more often in AI-authored diffs than human ones, and general review checklists miss them:

| Pattern | Why it happens | What to do |
|---|---|---|
| A plausible-looking implementation of something that already exists elsewhere | The author could not see the rest of the repo | `grep` for the concept before accepting the new code |
| A speculative abstraction with one implementation | Producing "good design" is a trained reflex | Delete it. Two concrete callers before an abstraction exists |
| `float` in money math | Overwhelmingly common in the training distribution | Blocking. `Decimal` from `str` |
| `datetime.now()` inside `strategy` or `risk` | Idiomatic almost everywhere else | Blocking. The clock is injected |
| A directly constructed `httpx`/`aiohttp` client | The obvious way to make an HTTP call | Blocking. `guarded_client()` only |
| `except Exception: log; continue` | Trained as defensive programming | Blocking. It converts a visible failure into silent wrong behaviour with positions open |
| A stub, `TODO`, or `NotImplementedError` presented as complete | Task pressure | Blocking. No fake implementations |
| **A claimed green test run that never happened** | The single most damaging failure mode here | Verify independently, always |
| A strategy that computes a quantity | LLM-authored strategies will size their own positions if the types permit it | Blocking — and check the type made it possible |
| Confident agreement with a flawed suggestion | Models converge easily | See §4 |

---

## 2. Never trust the verification claim

The PR body says `make check` is green. Run it yourself:

```bash
gh pr checks <n>
gh pr checkout <n>
make check
make test ARGS="--cov=src/fking/<touched> --cov-report=term-missing"
```

A false completion claim makes everything else in the PR worthless, because you now have to verify all of it by hand. Treat an unverified green claim as a blocking finding in its own right, separate from whatever the code does.

---

## 3. Run the structured review

Run `/review <pr>` for the blocking checklist, and `/security` if the diff touches `platform/safety`, `execution`, migrations, or anything that constructs a client.

Read the diff yourself as well. A checklist finds known classes; reading finds the thing that is subtly the wrong shape for this codebase — code that is individually correct but knows about two things that should have been separated.

---

## 4. When an AI reviews, force it to be adversarial

The same rule that governs the runtime Judge and Critic agents applies here: **an agent panel that converges easily is worthless, and language models converge easily by default.**

- Give the reviewer the diff **without** the author's rationale. The rationale is persuasive and it is not evidence.
- Score the review on defects found and confirmed, not on whether it agreed with the author.
- Ask for the strongest argument that the change is wrong, explicitly, even when the reviewer's verdict is approve.
- Track agreement rate over time. A reviewer approving above ~80% of diffs unchanged is not reviewing.

---

## 5. When receiving AI review feedback

Verify before implementing. A suggestion is a hypothesis about the code, and roughly a third of them are wrong in a codebase with rules this specific — particularly suggestions to "simplify" a `Decimal` construction, remove a "redundant" clock parameter, broaden an exception handler, or relax an `import-linter` contract to make a build pass.

Push back with reasoning when a suggestion conflicts with a non-negotiable. Do not implement a change you cannot justify; performative agreement produces worse code than disagreement does.

---

## 6. Runtime agent output is reviewed differently

Code written by an agent goes through this workflow. **Output produced by a runtime agent — a proposed strategy, thesis, or parameter change — is never reviewed by a human as a gate.** It goes through the deterministic gate: the validation gate decides whether a strategy lives, the risk engine decides the position, the promotion gate decides whether a parameter applies.

If you find yourself hand-approving an agent's trading proposal, the gate is missing and that is the issue to file.

---

## 7. Verdict

Approve / approve with comments / request changes — plus one sentence stating what you **verified by running**, distinguished from what you read. That distinction is the whole point of this workflow.
