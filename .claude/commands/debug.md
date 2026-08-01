---
description: Diagnose a failure from the audit trail rather than by guessing, and reproduce it deterministically before fixing
argument-hint: <symptom, error, or correlation id>
allowed-tools: Read, Grep, Glob, Bash
---

Debug: $ARGUMENTS

Do not propose a fix before you can reproduce the failure. A fix for an unreproduced bug is a guess with a commit hash.

## 1. Get the correlation ID

Every arrow between modules carries a correlation ID that originated at the top of the flow. That ID is the whole investigation.

```bash
make logs | grep -i "error\|traceback" | tail -40
```

With an ID in hand, pull the full chain from the audit log — what data existed, what features were computed, which strategy version and lineage fired, what risk decided and why, which agent reasoning contributed, what was sent, what came back, and the slippage against decision price:

```bash
psql "$DATABASE_URL" -c "select ts, module, event, payload from audit_events where correlation_id = '<id>' order by ts;"
```

Any trade must be fully reconstructable from the audit log alone. If it is not, that gap is itself a finding and probably the highest-value fix available.

## 2. Classify before investigating

The class determines where to look, and guessing the class wastes the most time here:

| Symptom | First suspect |
|---|---|
| Positions disagree with the exchange | Testnet wipe. Spot testnet wipes roughly every 30 days — keys survive, balances and open orders vanish. Run reconciliation before assuming a code bug. |
| A drawdown that appears instantaneously | Same. A wipe looks identical to a catastrophic loss on the equity curve. |
| Duplicated fills or doubled position | Non-idempotent bus consumer. Redis Streams is at-least-once; the redelivery is not the bug, the missing dedupe is. |
| Data shifted by orders of magnitude in time | Timestamp units. Spot switched to **microseconds from 2025-01-01**; futures stayed in **milliseconds**. Print raw values, not formatted dates. |
| A column offset by one row | Futures kline CSVs have a header row; spot ones do not. |
| A boolean column parsed as a string | Spot trade files serialize booleans Python-style (`True`/`False`). |
| Backtest results too good | Look-ahead. It does not raise; it flatters. |
| Backtest and live diverge | Something broke parity — check whether a second code path for strategies has appeared. |
| Agents stopped producing | Free-tier quota exhaustion. The correct behaviour is degrading to deterministic-only, not stalling; if it stalled, the degradation path is the bug. |
| Spot user-data stream dead | `listenKey` returns 410 Gone. Spot needs the WebSocket `session.logon` handshake with Ed25519 keys. |
| `Decimal`/`float` reconciliation drift | A `float` crept into money math, or a `Decimal` was constructed from a float literal. |

## 3. Reproduce deterministically

Nothing in `strategy` or `risk` reads the clock or uses unseeded randomness, so a failure in either is replayable exactly. Rebuild the input from the audit log and re-run it:

```bash
python -m fking.backtest.replay --correlation-id <id>
```

If it does not reproduce, the state you reconstructed is incomplete — find what is missing rather than concluding it is flaky. A "flaky" failure in a trading system is an unmodelled state, not noise.

## 4. Write the failing test before the fix

The test goes at the level the bug lives at. For position or risk arithmetic, make it a Hypothesis property and let it find the neighbouring cases — partial closes, direction flips, zero-crossings, dust quantities. The specific input that failed is rarely the only one.

Watch it fail. Then fix. Then watch it pass.

## 5. Fix the class, not the instance

Ask whether the same defect exists elsewhere:

```bash
grep -rn "<the defective pattern>" src/fking/ --include=*.py
```

Then do **not** fix it by catching an exception to keep the loop alive — that converts a visible failure into silent wrong behaviour with real positions open. Fail loudly at the boundary instead.

## 6. Verify

```bash
make check
```

## 7. Report

Symptom, correlation ID, root cause in one sentence, why it was not caught before, the test that now catches it, and whether the same class exists elsewhere. If the audit log was insufficient to diagnose it, say so — that is a separate issue worth filing.
