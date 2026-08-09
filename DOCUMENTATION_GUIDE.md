# Documentation Guide

Documentation in this repository has an unusual primary audience: **AI sessions with no memory of each other.** That is not a quirk to work around — it determines what a useful document looks like, and it is why several of the rules below would be over-strict in a normal codebase and are exactly right here.

`CLAUDE.md` §13 states the bar. This document is how to hit it.

---

## 1. The bar: one non-obvious fact per document

**Every document contains at least one decision, constraint, or trade-off a competent engineer would not have guessed.** If you cannot point at that sentence in what you wrote, delete the document and say the subject did not need one.

This is not a stylistic preference. Documentation that restates the obvious is *worse than none*, because it trains readers to skim — and then they skim the line that mattered. A reader who has learned that a document is 90% filler will miss the 10% that would have saved them a day.

The register to aim for, drawn from this repository's own facts:

> Binance futures testnet shows a 7.5bp spread against production's 0.16bp and roughly 10x inflated volume, so cost parameters are calibrated on production data only.

> Spot timestamps switched to microseconds from 2025-01-01 while futures stayed in milliseconds; normalization is keyed on `(market, date)`, never a global constant.

> Spot `listenKey` returns 410 Gone. Spot user data now requires a WebSocket `session.logon` with Ed25519 keys, while futures `listenKey` still works — two genuinely different mechanisms behind one interface.

> Free full-depth L2 order book history does not exist. Binance `bookDepth` is aggregated depth bands sampled about once a minute, not snapshots.

> Spot testnet wipes roughly every 30 days without notice: keys survive, balances and open orders vanish.

Each of these is checkable, surprising, and changes what someone builds. That is the test.

### 1.1 The self-check before you commit a document

Ask, of each paragraph: **would a competent engineer who had never seen this repository have written this paragraph themselves, from first principles?** If yes, delete it. "Use meaningful variable names" fails. "`size` is banned as a parameter name because in a trading system it means base quantity, notional, contract count, or leverage depending on who wrote the line, and every one of those differs by orders of magnitude" passes.

### 1.2 State the reason with the rule, always

A rule without a reason gets discarded the first time it is inconvenient — usually by someone in a hurry, and usually correctly from their point of view given what they knew. The reason is the load-bearing half of the sentence.

```markdown
<!-- WRONG -->
Always use `Decimal` for money.

<!-- RIGHT -->
Always use `Decimal` for money, constructed from `str`. `Decimal(0.1)` is
not `Decimal("0.1")`, and float error accumulates across thousands of fills
into a position notional that disagrees with the exchange's — which presents
as a reconciliation failure and looks like an exchange bug for about a day
before anyone suspects arithmetic.
```

### 1.3 Say what is not true

The limits are frequently the most valuable content: what a component does not do, what data does not exist, what the assumption is and when to revisit it.

"Free full-depth L2 order book history does not exist" has saved more time than any positive statement in `DATA_PIPELINE.md`, because it stops an entire class of strategy design before someone spends a week on it.

---

## 2. Cross-link, do not duplicate

Duplicated documentation diverges, and the reader has no way to tell which copy is stale. Two documents both claiming to be authoritative is strictly worse than one document plus a link.

```markdown
<!-- WRONG -->
Coverage floors: platform/safety 100%, risk 95%, domain 95%, execution 90%,
everything else 80%.

<!-- RIGHT -->
Coverage floors are in `TESTING.md` §6. The one that matters for this module
is `platform/safety` at 100% *plus* a mutation gate — line coverage there is
achievable without ever testing a rejection.
```

The right version links for the shared fact and states only what is new. When the floors change, one file changes.

### 2.1 Where a fact lives

Each fact has exactly one owner. Everything else links.

| Fact | Owner |
|---|---|
| The non-negotiables | `CLAUDE.md` §2 |
| Module boundaries and why | `ARCHITECTURE.md` §2, §5 |
| Language-level rules with examples | `CODING_STANDARDS.md` |
| Coverage floors, test tiers, fixtures | `TESTING.md` |
| Branch, commit, PR mechanics | `GIT_WORKFLOW.md` |
| What blocks a merge | `CODE_REVIEW.md` §1 |
| Setup and definition of done | `CONTRIBUTING.md` |
| A specific decision and its rejected alternatives | `docs/adr/NNNN-*.md` |

If you are about to state one of these somewhere else, link instead.

### 2.2 New top-level documents need a distinct owner concept

Not just a distinct topic. "Redis usage" is a topic; it belongs in `ARCHITECTURE.md` and `OBSERVABILITY.md`. A new file needs to own something no existing file owns.

Check before creating:

```bash
ls *.md docs/ docs/adr/
grep -rln "<subject keywords>" *.md docs/
```

### 2.3 "We chose X over Y" is an ADR, not prose

If the content is a decision with rejected alternatives, it goes in `docs/adr/` via `/adr`, and the prose document links to it. ADRs are immutable once accepted; a decision changes by writing a superseding ADR and adding one line to the old one. The record of rejected paths is the valuable part.

---

## 3. Writing for readers with no shared memory

Most sessions reading this repository will have read **fragments** of it, retrieved by search, with no memory of previous sessions and no ability to ask a follow-up question. That has concrete consequences for how a document must be written.

### 3.1 Every section must be self-locating

A section may be read entirely out of context. Never write:

- "As mentioned above" — there is no above.
- "See the previous section" — sections are retrieved individually.
- "This" with no antecedent in the same paragraph.
- "The engine" where there are three engines (backtest, risk, evolution). Say which.

Repeat the subject rather than pronominalising across a paragraph boundary. Slight redundancy inside a document is fine; it is duplication *across* documents that is forbidden (§2).

### 3.2 Use absolute, greppable identifiers

```markdown
<!-- WRONG -->
The safety module's client wrapper validates the host.

<!-- RIGHT -->
`fking.platform.safety.guarded_client()` validates the host on every
request — see `src/fking/platform/safety/`.
```

A session that cannot find the symbol will reimplement it. Fully-qualified names and repo-relative paths are what make a document searchable rather than merely readable.

### 3.3 Prohibitions beat aspirations

An instruction gets partially applied. A prohibition is checkable — a reviewer or a linter can look for the forbidden thing.

```markdown
<!-- WEAK -->
Prefer immutable domain objects where practical.

<!-- STRONG -->
Never add a `domain/` dataclass without `frozen=True`, and never give a frozen
class a `list` or `dict` field — `frozen=True` prevents rebinding the attribute
and does nothing to the object it points at.
```

"Where practical" is an escape hatch that will be taken.

### 3.4 Banish temporal and relative language

These words rot silently, and a rotted document is trusted exactly as much as a fresh one:

- "currently", "recently", "for now", "at the moment", "the new X"
- "soon", "will be", "is being migrated"
- "the old approach", "legacy" — legacy relative to what, as of when?

Write dated facts instead: "As of 2026-08, `ccxt` 4.5.70 is the only client correct on the spot `session.logon` handshake." A dated statement can be checked and refuted. "Currently the only client" cannot.

### 3.5 Front-load the load-bearing content

Retrieval systems weight the beginning of a document heavily, and a session that pulls a fragment usually gets the first section. Put the constraint that changes behaviour in the first 200 words. Do not build up to it.

### 3.6 Give the failure, not just the rule

A session that knows the consequence can generalise to cases the rule did not enumerate. A session that knows only the rule cannot.

```markdown
<!-- WEAK -->
Bus consumers must be idempotent.

<!-- STRONG -->
Bus consumers must be idempotent. Redis Streams delivers at least once by
design, and redelivery is out of order after a consumer group rebalance — so
a "last seen id" dedupe is not enough. A consumer that double-applies a fill
produces a position that is wrong by exactly one fill, which reconciles as an
apparent exchange discrepancy rather than as a bug in our code.
```

The second version lets a session correctly handle a case nobody wrote down.

---

## 4. False documentation is deleted, not flagged

**A document that has become false is deleted or corrected in the same change that made it false. It is never annotated.**

Do not write `> ⚠️ This section may be out of date`. Do not add `(deprecated)`. Do not leave it with a note at the top.

Three reasons, and the third is the one specific to this project:

**A warning is not read by whoever needs it.** The reader who most needs the warning is the one skimming for the answer to one question. They will find the paragraph, take it, and never see the banner.

**A flagged document stays flagged forever.** "Someone should update this" is nobody's task. The flag becomes furniture, and after the third flag the reader stops seeing them.

**Retrieval strips the context.** Fragments are surfaced without the document's header. A section retrieved from a file whose top says "possibly outdated" arrives with no such qualifier attached, and is read as fact. This is the decisive argument: the mechanism by which most sessions read this repository *cannot carry the flag*.

So:

- If the fact changed, **change the sentence** in the same PR that changed the behaviour.
- If the section is now wrong and you do not know what is right, **delete it** and, if it matters, open an issue. An empty space is honest. A wrong paragraph is not.
- If a whole document describes something that no longer exists, delete the file. Git has it.

The same applies to code documentation and to `docs/adr/` with one exception: **ADRs are never edited or deleted**, because their value is the historical record. A superseded ADR gets exactly one added line — `> Superseded by ADR-00XX` — and not a word else changes. The ADR is a record of what was decided at a time, not a claim about the present.

### 4.1 Corollary: no forward promises

No "this could be implemented later". No `TODO`. No describing an unimplemented feature in the present tense. If it does not exist, either build it or say plainly that it does not exist and why.

A document that describes an aspiration in the present tense is the worst class of false documentation, because it is not detectably stale — it was never true.

---

## 5. Every command example must have been executed

Not "should work". Executed, by you, in this repository, with the output you are about to describe.

```bash
# Before writing this into a document, run it:
make test ARGS="--cov=src/fking/risk --cov-branch --cov-report=term-missing"
```

A stale example is worse than no example, because it is trusted. A session that copies a broken command spends its next twenty minutes debugging your documentation instead of its actual task, and it has no way to know that is what it is doing.

The same applies to code snippets:

```bash
# Confirm every symbol in a snippet still exists with that signature
grep -rn "def guarded_client" src/fking/platform/safety/
grep -rn "class Position" src/fking/domain/
```

If a snippet is illustrative rather than runnable — a "wrong/right" pair, or pseudocode — say so on the line above it. `CODING_STANDARDS.md` marks these with `# WRONG` / `# RIGHT` comments so there is no ambiguity about which one is meant to be copied.

**Placeholders are marked unmistakably.** `<issue-number>`, `<kebab-slug>`, `<pinned>` in angle brackets. Never a plausible-looking fake value, because a plausible fake gets copied verbatim.

---

## 6. Structure

### 6.1 Length is a consequence, not a target

Write the non-obvious content and stop. A 40-line document with three surprising facts is better than a 400-line document with the same three facts and 360 lines of scaffolding. There is no minimum.

### 6.2 Tables for rules, prose for reasoning

A rule with a reason fits a two-column table and is scannable. An argument does not fit a table and should not be forced into one.

### 6.3 Headings are search targets

Write them as the question a reader is asking. "Why the database is never mocked" beats "Database testing". "What blocks a merge" beats "Review policy". This matters more than it sounds, because headings dominate retrieval matching.

### 6.4 Number your sections

`## 4. False documentation is deleted, not flagged`. Numbered sections are stable cross-link targets, and every document in this repository links to others by number. A renamed heading breaks links; a numbered one at least fails visibly.

---

## 7. Which document, for which change

| You changed | Update |
|---|---|
| A language-level rule | `CODING_STANDARDS.md` — with a correct/incorrect pair and the enforcement mechanism |
| A test convention or a floor | `TESTING.md` |
| Anything about branching, commits, PRs | `GIT_WORKFLOW.md` |
| What blocks a merge | `CODE_REVIEW.md` §1 |
| A module boundary or the data flow | `ARCHITECTURE.md`, plus an ADR |
| A decision with rejected alternatives | `docs/adr/` via `/adr` — nothing else |
| An invariant, its failure mode and its enforcement | `docs/rules/<rule>.md`, and the index in `CLAUDE.md` §14 if the file is new |
| A non-negotiable | `CLAUDE.md`, by pull request, never in passing |
| A verified fact about an exchange or a data source | `DATA_PIPELINE.md` and, if it changes the cost model, an ADR |

If a change touches behaviour and no document needed updating, check again — either the behaviour was undocumented (fix that) or the change was smaller than it looked (fine).

---

## 8. The report you owe when you write a document

When `/document` finishes, it states three things. Hold yourself to the same standard writing by hand:

1. **What was written**, and where.
2. **Which existing document it links to instead of duplicating.**
3. **The specific non-obvious sentence that justifies its existence** — quoted.

If you cannot produce the third, the document should not be committed.
