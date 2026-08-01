# Workflow — Bugfix

A bugfix that does not include a test which fails before the fix is not a bugfix. It is a change that happened to coincide with the symptom disappearing.

---

## 1. Contain, if it is live

If the bug is affecting the running system, the kill switch comes first and diagnosis second — go to `.claude/workflows/incident.md` and come back here afterwards.

---

## 2. Reproduce before anything else

Run `/debug <symptom>`.

Get the correlation ID and pull the chain:

```bash
psql "$DATABASE_URL" -c "select ts, module, event, payload from audit_events where correlation_id = '<id>' order by ts;"
```

Then replay. Nothing in `strategy` or `risk` reads the clock or uses unseeded randomness, so failures there reproduce exactly:

```bash
python -m fking.backtest.replay --correlation-id <id>
```

**If it does not reproduce, the reconstructed state is incomplete — find what is missing.** A "flaky" failure in a trading system is an unmodelled state, not noise, and treating it as noise is how it comes back during an incident.

**Exit condition**: you can make it fail on demand.

---

## 3. Check the usual causes before inventing a new one

| Symptom | First suspect |
|---|---|
| Position mismatch, sudden zero balance | Spot testnet wipe — roughly every 30 days, keys survive, balances and open orders vanish |
| Doubled fill or doubled position | Non-idempotent bus consumer; Redis Streams is at-least-once |
| Data shifted enormously in time | Spot timestamps are microseconds from 2025-01-01; futures are milliseconds |
| Column off by one row | Futures kline CSVs have a header row; spot ones do not |
| Boolean column is a string | Spot trade files serialize booleans Python-style |
| Backtest looks too good | Look-ahead — it does not fail, it flatters |
| Backtest and live differ | Parity broken; a second code path for strategies has appeared |
| Agents silent | Free-tier quota exhausted; the loop should have degraded to deterministic-only |
| Reconciliation drift in money | `float` in the money path, or `Decimal` built from a float literal |

---

## 4. Branch

```bash
git checkout main && git pull origin main
git checkout -b fix/<issue-number>-<kebab-slug>
```

---

## 5. Write the failing test

At the level the bug actually lives at. For position or risk arithmetic, write it as a Hypothesis property rather than pinning the one input that failed — the specific failing input is rarely the only one, and the property will find its neighbours.

Watch it fail.

**Exit condition**: a red test that describes the defect.

---

## 6. Fix the class, not the instance

```bash
grep -rn "<the defective pattern>" src/fking/ --include=*.py
```

If the same pattern exists in three places, fixing one is a partial fix and the issue should say so.

Do **not** fix it by catching an exception to keep the loop alive. That converts a visible failure into silent wrong behaviour with positions open — and it is usually why the bug was found late rather than early.

---

## 7. Verify

```bash
make check
```

Then confirm the original reproduction no longer reproduces, using the same command from step 2. Both pieces of evidence go in the PR.

---

## 8. Ship

Run `/ship <issue-number>`.

The PR body must answer **why this was not caught before**. That answer is the actual deliverable of a bugfix: a missing property test, an unvalidated boundary, a type that permitted an invalid state, an alert on a symptom rather than a leading indicator. If the answer is "nobody thought of it", file the follow-up issue that makes the class impossible rather than merely fixed.

---

## 9. If it caused an incident

Link the PR from the post-mortem's action list and close the action. An action item closed without a linked commit is not closed.
