---
description: Write or update documentation that carries at least one non-obvious constraint or trade-off
argument-hint: <document or subject>
allowed-tools: Read, Grep, Glob, Bash, Write, Edit
---

Document: $ARGUMENTS

Documentation that restates the obvious is worse than none, because it trains readers to skim — and then they skim the line that mattered.

## 1. Find the right home; do not create a new one

```bash
ls *.md docs/ docs/adr/
```

The map of the operating system is in `CLAUDE.md` §14. Add to the existing document that owns the subject. A new top-level document needs a distinct owner concept, not just a distinct topic.

**Cross-link rather than duplicate.** Duplicated documentation diverges, and the reader has no way to know which copy is stale. If you find yourself restating a rule from another file, link to it and state only what is new here.

## 2. Architecture decisions go to `docs/adr/`, not to prose

If the content is "we chose X over Y", it is an ADR — `/adr` writes it. ADRs are immutable once accepted; a decision changes by writing a new ADR that supersedes the old one, leaving both in place. The record of rejected paths is the valuable part.

## 3. The bar for every document

Every document contains **at least one decision, constraint, or trade-off a competent engineer would not have guessed.** If you cannot point to that sentence in what you wrote, delete the document and say the subject did not need one.

Examples of the register to aim for, drawn from this repository:

- "Binance futures testnet shows a 7.5bp spread against production's 0.16bp and roughly 10x inflated volume, so cost parameters are calibrated on production data only."
- "Spot timestamps switched to microseconds from 2025-01-01 while futures stayed in milliseconds; normalization is keyed on `(market, date)`, never a global constant."
- "Spot `listenKey` returns 410 Gone; spot user data now requires a WebSocket `session.logon` with Ed25519 keys, while futures `listenKey` still works — two mechanisms behind one interface."
- "Spot testnet wipes roughly every 30 days: keys survive, balances and open orders vanish."

## 4. Where a rule exists, state the reason

A rule without a reason gets discarded the first time it is inconvenient — usually by someone in a hurry, usually correctly from their point of view given what they knew. The reason is the load-bearing half of the sentence.

## 5. Say what is not true

Document the limits: what the component does not do, what data does not exist, what the assumption is and when to revisit it. "Free full-depth L2 order book history does not exist" has saved more time than any positive statement in `DATA_PIPELINE.md`.

## 6. Verify the examples

Every command in a document must have been run. Every code snippet must reflect the current signature:

```bash
grep -rn "<symbol from the doc>" src/fking/ --include=*.py
```

A stale example is worse than no example because it is trusted.

## 7. No forward promises

No "this could be implemented later", no "TODO", no describing an unimplemented feature in the present tense. If it does not exist, either build it or say plainly that it does not exist.

## 8. Report

What was written, which existing document it links to instead of duplicating, and the specific non-obvious sentence that justifies its existence.
