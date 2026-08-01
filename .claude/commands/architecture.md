---
description: Evaluate a proposed architectural change against the load-bearing invariants, ending in an ADR or a rejection
argument-hint: <proposed change>
allowed-tools: Read, Grep, Glob, Bash, Write
---

Evaluate the architectural change: $ARGUMENTS

Most proposed architecture changes should be rejected. Do the evaluation honestly and expect that outcome.

## 1. Read what is already decided

```bash
cat ARCHITECTURE.md
ls docs/adr/ && grep -rln "<subject>" docs/adr/
```

ADRs are immutable once accepted. If this change contradicts an accepted ADR, the output of this command is a **superseding ADR**, leaving both in place — the record of rejected paths is the valuable part.

## 2. Test it against the load-bearing invariants

Any of these being weakened is a rejection unless the user explicitly decides otherwise:

1. **Demo-only.** No path, flag, config, or environment variable enables a production venue. The allowlist is a compiled-in `frozenset`. If the change makes real trading reachable — even conditionally, even read-only — stop and ask the user.
2. **Backtest/live parity.** Strategy code is identical across backtest, walk-forward, paper and demo; only `ExecutionVenue` swaps. If the change creates a second code path for strategies, every backtest result becomes unfalsifiable — you could no longer distinguish "the strategy is bad" from "the harness differs".
3. **Risk is structural.** The risk engine alone constructs orders; `strategy` has no import path to `execution`. This must stay enforced by `import-linter` rather than by convention, because the strategy author will eventually be an LLM that never read this document.
4. **Point-in-time features.** No change may make it possible to compute a feature at *t* from data that did not exist at *t*.
5. **Append-only audit.** Any trade must remain fully reconstructable from the audit log alone, months later, with no access to application memory.
6. **Dependencies point inward.** `domain` imports nothing but stdlib; `platform` is importable by anyone.

## 3. Cost the change honestly

- What operational surface does it add? This is a single developer, single machine, zero budget. A component needing its own server has to justify the server.
- Does it add a network partition between components that must agree about position state? That is the specific cost that ruled out microservices.
- Does it add a deployment target, a second datastore, or a paid tier? Postgres+TimescaleDB is deliberately one engine for relational and time-series; Parquet+DuckDB is deliberately serverless.
- What does it make harder to reverse? Prefer the reversible option even when it is slightly worse.

## 4. Argue the strongest case against

Write the best version of the opposing argument, then answer it. If you cannot state the counter-argument convincingly, you do not understand the decision well enough to make it.

Include the "do nothing" option explicitly. It is frequently the right answer and it is the one nobody writes down.

## 5. Check the assumptions register

`ARCHITECTURE.md` §13 lists what the architecture assumes: single node is enough, free tiers hold, testnet remains available and free, and the evolution engine's defences are sufficient. State which of these the change depends on or invalidates. The last one is the assumption most likely to be wrong — if evolved strategies consistently outperform in validation and underperform forward, the scoring engine is lying, and that takes priority over every other architectural concern.

## 6. Produce the output

**Reject**: one paragraph in the evaluation saying why, so the next person does not re-propose it. Add it to the relevant ADR's alternatives-considered section only if a new ADR is being written; never edit an accepted one.

**Accept**: write `docs/adr/NNNN-<kebab-slug>.md` with Status, Context, Decision, Consequences, Alternatives considered (including the strongest rejected one and why), and — if superseding — an explicit `Supersedes: ADR-NNNN` plus a `Superseded by` line added at the top of the old ADR without altering its body.

Then state the migration path and which `import-linter` contracts must change. A contract change is part of the ADR, never a quiet edit.

## 7. Report

Verdict, the invariant most at risk, and the concrete next step.
