---
description: Write an architecture decision record, including the strongest rejected alternative
argument-hint: <decision title>
allowed-tools: Read, Grep, Glob, Bash, Write
---

Write an ADR for: $ARGUMENTS

## 1. Check whether this is already decided

```bash
ls docs/adr/
grep -rln "<subject keywords>" docs/adr/
```

ADRs are **immutable once accepted**. If an existing ADR covers this, you are writing a **superseding** ADR, not editing the old one. The old ADR stays exactly as written — the record of rejected paths is the valuable part, and editing it destroys the reason the record exists.

## 2. Number it

```bash
ls docs/adr/ | sort | tail -1
```

Next sequential number, zero-padded to four digits: `docs/adr/0014-<kebab-slug>.md`.

## 3. Write it

```markdown
# ADR 0014 — <Title>

- **Status**: Accepted
- **Date**: <YYYY-MM-DD>
- **Supersedes**: ADR-000X   (omit if none)

## Context

The forces in play. What is true about the world that makes this a decision
rather than an obvious choice. Include the measured facts — numbers with
provenance, not impressions.

## Decision

What we are doing, stated so that someone can tell whether a future pull
request complies with it.

## Consequences

What becomes easier. What becomes harder. What we are now committed to that
we were not before. What we will have to undo if this turns out wrong.

## Alternatives considered

For each: what it was, its strongest argument, and the specific reason it lost.
The strongest rejected alternative gets the most space — an ADR whose
alternatives are all obviously bad is an ADR that did not consider any.

## Revisit when

The condition under which this decision should be reopened. A decision with no
revisit condition becomes permanent by accident.
```

## 4. The bar

**The alternatives section is the load-bearing part.** ADR 0005 in this repository is the model: `NautilusTrader` was a genuinely strong option — event-driven, Rust core, well maintained — and it was rejected for a specific structural reason (adopting it means adopting its domain model, so the risk and evolution engines become plugins to its lifecycle rather than first-class components with authority over it), and that trade-off is recorded as open to revisit rather than closed. Aim for that register.

If you cannot write a convincing case *for* the option you rejected, you have not understood the decision well enough to record it.

Include the "do nothing" option explicitly. It is frequently the right answer and it is the one nobody writes down.

## 5. Check it against the invariants

An ADR cannot decide to weaken: demo-only execution, backtest/live parity, risk's exclusive authority to construct orders, point-in-time features, or append-only audit. If the decision touches any of these, it needs the user's explicit agreement before the status becomes Accepted — say so in the report rather than writing "Accepted" yourself.

## 6. Link, do not duplicate

Reference `ARCHITECTURE.md` and the sibling documents rather than restating them. Duplicated documentation diverges, and then two documents both claim to be authoritative.

## 7. If superseding

Add a single line at the top of the old ADR — `> Superseded by ADR-0014` — and change nothing else in its body. Not a word.

## 8. Report

ADR number, decision in one sentence, the strongest rejected alternative and why it lost, and whether the status is genuinely Accepted or awaiting the user's decision.
