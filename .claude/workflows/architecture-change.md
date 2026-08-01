# Workflow — Architecture Change

Most proposed architecture changes should be rejected. Run this workflow honestly and expect that outcome; a rejection recorded with its reasoning is a real deliverable.

---

## 1. Write the change down in one paragraph

If it cannot be stated in a paragraph, it is several changes and each needs its own pass through this workflow.

---

## 2. Read what is already decided

```bash
cat ARCHITECTURE.md
ls docs/adr/ && grep -rln "<subject>" docs/adr/
```

ADRs are immutable once accepted. Contradicting one means writing a **superseding** ADR with both left in place — the record of rejected paths is the valuable part.

---

## 3. Test against the load-bearing invariants

Run `/architecture <change>`. Any of these being weakened stops the workflow and goes to the user:

1. **Demo-only.** No path, flag, config, or environment variable reaches a production venue. The allowlist is a compiled-in `frozenset`.
2. **Backtest/live parity.** Strategy code identical across backtest, walk-forward, paper and demo; only `ExecutionVenue` swaps. Break this and no backtest result can be defended, because "the strategy is bad" and "the harness differs" become indistinguishable.
3. **Risk authority.** The risk engine alone constructs orders; `strategy` has no import path to `execution`, enforced by `import-linter` — because the strategy author will eventually be an LLM that never read this document.
4. **Point-in-time features.**
5. **Append-only audit**, sufficient to reconstruct any trade months later with no access to application memory.
6. **Dependencies point inward.** `domain` imports nothing but stdlib.

**Exit condition**: a written statement of which invariants the change touches and how it preserves each.

---

## 4. Cost it against this project's actual constraints

One developer, one machine, zero budget.

- Does it add a server, a paid tier, or a deployment target?
- Does it introduce a network partition between components that must agree about position state? That specific cost is what ruled out microservices, and it has not changed.
- What does it make irreversible? Prefer the reversible option even when it is slightly worse.
- Does it move the system toward latency sensitivity? This system is explicitly not built for latency arbitrage and should not pretend to be.

---

## 5. Argue the other side

Write the strongest case against the change, and the "do nothing" case explicitly. If you cannot make the opposing argument convincingly, you do not yet understand the decision well enough to make it.

---

## 6. Decide

**Reject** — write the reasoning into the issue so it is not re-proposed. Done.

**Accept** — continue.

---

## 7. Write the ADR before writing code

Run `/adr <title>`. Status stays draft until the user agrees if any invariant is affected.

The alternatives section carries the weight. ADR 0005 is the model: `NautilusTrader` was genuinely strong and lost for a specific structural reason — adopting it means adopting its domain model, so risk and evolution become plugins to its lifecycle rather than components with authority over it — recorded as open to revisit rather than closed.

---

## 8. Change the contracts explicitly

If module boundaries move, the `import-linter` contracts move **in the same PR as the ADR**, never quietly and never later:

```bash
make check
```

A contract silently relaxed is an architecture silently abandoned, and it is invisible in a diff unless someone is looking for it.

---

## 9. Migrate incrementally

- Land the new structure alongside the old.
- Move callers one at a time, each its own commit, each independently revertable.
- Delete the old path last, in its own PR.
- Never leave both paths live across a merge to `main` without a comment saying which one is authoritative and when the other dies.

If the change touches the backtest engine, cost model, or feature definitions, capture a golden backtest run before and after and state explicitly that prior results are or are not comparable.

---

## 10. Update the map

`ARCHITECTURE.md` gets the structural change. `CLAUDE.md` §14 gets a row if a new document was created. Cross-link; do not duplicate — duplicated documentation diverges and then both copies claim authority.

`ARCHITECTURE.md` §13 lists what the architecture assumes and when to revisit. If this change alters an assumption or adds one, update that section. The assumption most likely to be wrong is that the evolution engine's defences are sufficient; if a change touches validation, say what it does to that assumption.
