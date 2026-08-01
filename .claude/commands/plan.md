---
description: Produce an implementation plan with contracts, verification, and named failure modes before any code is written
argument-hint: <task description or issue number>
allowed-tools: Read, Grep, Glob, Bash
---

Plan the implementation of: $ARGUMENTS

Output a plan. Write no implementation code in this command.

## 1. Understand what exists before proposing what should

```bash
ls src/fking/
grep -rn "<key symbol>" src/fking/ --include=*.py | head -30
ls docs/adr/
```

An accepted ADR is binding. If the plan contradicts one, the plan is to write a superseding ADR first — ADRs are immutable once accepted and are superseded, never edited.

## 2. Place the code

Ask *what does this code know about?* Code that knows about order types belongs in `execution`. Code that knows about order types **and** feature engineering belongs in neither — it is two pieces of code that have not been separated yet, and the plan should separate them.

Then check the plan against the dependency rules:

- Dependencies point inward toward `domain`. `domain` imports nothing but stdlib.
- `strategy` cannot import `execution`. If the plan needs that edge, the plan is wrong.
- `platform` is importable by anyone.

## 3. Resist the abstraction

An abstraction requires **two concrete callers before it exists**. One caller plus an anticipated future caller is speculation. If the plan introduces a base class, protocol, or registry with a single implementation, remove it — write the second implementation first, then extract. Say so in the plan if you removed one.

## 4. Specify the contracts

For each new function or type:

- Full signature with units in the names: `notional_usd: Decimal`, `timeout_seconds: float`, `base_quantity: Decimal`. `size` is ambiguous in a trading system to the point of being dangerous.
- `Decimal` for every price, quantity and monetary amount, constructed from `str`.
- Timezone-aware UTC datetimes; naive datetimes rejected at construction.
- Domain objects frozen; state transitions return new objects.
- Anything that reads the clock takes it as a parameter.
- Errors: which specific exception is raised, and at which boundary validation happens. Never a bare `except Exception` to keep going.

## 5. Verification plan, written before the code

- Exact commands to run.
- For `risk`, `domain`, and any position math: the **Hypothesis properties** to assert, not just example cases. Position arithmetic fails on the cases you did not think of — partial closes, direction flips, zero-crossings, dust quantities.
- Database tests use the real Postgres service container, never a mock. A mocked database proves the mock works.
- Exchange tests use recorded real responses, never hand-written fixtures. Hand-written fixtures encode your assumptions, so tests pass while production fails.
- Coverage floor for each touched module.

## 6. Name the failure modes

State explicitly which of these the change could introduce, and how the plan prevents it:

- Look-ahead bias (does not fail — makes bad strategies look excellent)
- Non-idempotent bus consumer (Redis Streams is at-least-once)
- `float` contamination in money math
- A network call bypassing `guarded_client()`
- A timestamp unit assumption (spot is microseconds from 2025-01-01; futures are milliseconds)
- State that does not survive a testnet wipe (spot testnet wipes roughly every 30 days; keys survive, balances and open orders vanish)
- Free-tier LLM quota exhaustion stalling the loop instead of degrading to deterministic-only

## 7. Sequence it

Order the work so each step is independently verifiable and independently revertable. Each step should be one commit. If any step is over ~400 lines, split it — an unreviewable PR is an unreviewed PR.

## 8. State the open question

If exactly one assumption would waste substantial work if wrong, ask about it now, with a recommendation attached: "I recommend A because X; say so if you would rather have B." Do not present a menu of options you are not going to pursue.
