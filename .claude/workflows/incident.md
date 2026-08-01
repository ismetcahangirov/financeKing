# Incident Response

**Read this top to bottom. Do the numbered steps in order. Do not skip to diagnosis.**

You are probably tired and it is probably late. That is what this document is written for.

---

## 0. ACTIVATE THE KILL SWITCH FIRST. DIAGNOSE SECOND.

```bash
curl -X POST localhost:8000/admin/kill-switch -H "Authorization: Bearer $ADMIN_TOKEN"
```

If the API is not responding:

```bash
make down
```

If Docker is not responding, kill the process directly:

```bash
docker kill $(docker ps -q --filter name=fking) 2>/dev/null || pkill -f "fking"
```

**Do not investigate before the switch is on.** You will not lose anything by stopping — this is a demo account and there is no opportunity cost. You can lose a great deal by spending twenty minutes reading logs while a broken loop keeps sending orders.

Confirm it took effect:

```bash
curl -s localhost:8000/admin/status | grep -i kill
make logs | tail -20
```

Then write down the wall-clock UTC time. Everything after this is timed against it.

---

## 1. Classify severity — 60 seconds, not more

| Sev | Definition | Response |
|---|---|---|
| **SEV-1** | The demo-only guarantee is in question. Any sign of a request to a non-testnet host, an allowlist change, or credentials for a real account present anywhere. | Kill switch stays on. Wake the user **now**, whatever the hour. Do not attempt a fix. |
| **SEV-2** | Positions or balances disagree with the exchange; orders sent that the system cannot account for; the risk engine emitted an order past a limit. | Kill switch stays on. Reconcile. Notify the user within the hour. |
| **SEV-3** | Trading halted or degraded: data pipeline down, agents stalled, bus backed up, telemetry blind. No position uncertainty. | Kill switch stays on until data integrity is confirmed. Notify at next working hour. |
| **SEV-4** | Cosmetic or contained: dashboard error, one metric missing, a non-critical job failing. | Kill switch may be released. File an issue. |

**When unsure between two levels, pick the higher one.** The cost of over-classifying is a message the user reads in the morning. The cost of under-classifying is discovering on Tuesday that Saturday's event was serious.

**SEV-1 is not something you fix.** If there is any question about the demo-only guarantee, stop, preserve everything, and hand it to the user. That guarantee is the single most important property of this system and it is not yours to restore alone at 3am.

---

## 2. Preserve evidence before touching anything

Do this before restarting, before reconfiguring, before "just trying" anything.

```bash
mkdir -p incidents/$(date -u +%Y%m%dT%H%M%SZ) && cd incidents/$(date -u +%Y%m%dT%H%M%SZ)
docker compose logs --no-color --timestamps > docker.log
git rev-parse HEAD > deployed-commit.txt
git status --porcelain > working-tree.txt
curl -s localhost:8000/admin/status > status.json 2>/dev/null
psql "$DATABASE_URL" -c "\copy (select * from audit_events where ts > now() - interval '6 hours' order by ts) to 'audit.csv' csv header"
redis-cli XINFO STREAM fking.events > redis-stream.txt 2>/dev/null
```

A restart destroys in-memory state, and the audit log's usefulness depends on being read against what the process actually believed at the time.

---

## 3. Answer the two questions that determine everything else

**Q1: Do we know what our real position is?**

```bash
python -m fking.execution.reconcile --report-only
```

`--report-only` compares and prints; it does not write. Read the comparison before letting anything converge.

**Q2: Is this a testnet wipe?**

Binance spot testnet wipes roughly every **30 days without notice**: API keys keep working, balances and open orders vanish. On the equity curve this is indistinguishable from a catastrophic loss, and it is the single most common cause of a 3am alert in this system.

Check the signature:
- Balances at or near zero, keys still authenticating, open orders gone, no corresponding fills in the audit log.
- If fills are missing from the exchange side but present locally with no counterpart — wipe, not loss.

If it is a wipe: this is **SEV-3**, not SEV-2. Reconcile from the exchange (exchange state is the source of truth, local converges to it), record the wipe date, and stop treating it as a loss. Do not "recover" the missing balance; there was never a real balance.

---

## 4. Diagnose

Start from the correlation ID of the first anomalous event. Every module boundary carries one, propagated from the top, which is what makes a trade reconstructable end to end months later.

```bash
psql "$DATABASE_URL" -c "select ts, module, event, payload from audit_events where correlation_id = '<id>' order by ts;"
```

The usual causes here, in rough order of frequency:

1. **Testnet wipe** — see step 3.
2. **Non-idempotent bus consumer.** Redis Streams delivers at least once. A doubled position after a reconnect is a missing dedupe key, not a Redis bug.
3. **Timestamp unit change.** Spot switched to **microseconds from 2025-01-01**; futures stayed in **milliseconds**. Symptoms: bars in the wrong order, features computed over the wrong window, a strategy trading on data from 1970 or the year 56000. Print raw integers, not formatted dates — formatting hides it.
4. **Free-tier LLM quota exhausted.** Agents stop producing. Correct behaviour is degrading to deterministic-only; if the loop stalled instead, the degradation path itself is the defect.
5. **Spot user-data stream dead.** `POST /api/v3/userDataStream` returns 410 Gone — spot needs the WebSocket `session.logon` handshake with Ed25519 keys. Futures `listenKey` still works, so "futures fine, spot silent" points straight here.
6. **Swallowed exception keeping a loop alive.** Look for a caught exception that continued. That converts a visible failure into silent wrong behaviour with positions open, and it is why the alert came late.
7. **Data pipeline fed something malformed** — header row consumed as data on spot klines (futures have a header, spot does not), or Python-style booleans in spot trade files parsed as strings.

Do not stop at the first plausible cause. Confirm it against the audit trail; a plausible cause you did not verify will send you back here next week.

---

## 5. Communication expectations

**SEV-1**: wake the user immediately, by whatever channel reaches them. Message states: what is in question, what you stopped, what you have NOT touched. Nothing else. No speculation.

**SEV-2**: notify within one hour with: what happened, current position certainty, kill-switch state, what you need from them.

**SEV-3**: notify at the next working hour. File the issue immediately regardless.

**SEV-4**: file the issue. No notification.

For every severity, update the incident record at least every 30 minutes while the kill switch is on, even if the update is "still investigating, no change". Silence during an incident is read as either resolution or abandonment, and both readings are wrong.

State facts and their confidence separately. "Positions do not match; I do not yet know why" is a good update. "Probably just a wipe" during an unresolved SEV-2 is not.

---

## 6. Recovery — in this order, no reordering

1. Fix the root cause. Not the symptom. If you cannot fix it, leave the switch on and say so.
2. `make check` — green, run now, output read.
3. `python -m fking.execution.reconcile --full`. Exchange is truth; local converges.
4. Restart with **strategies disabled** and the kill switch still armed.
5. Watch one full data cycle. Confirm features compute, signals emit, and the risk engine's decisions appear in the audit log with correlation IDs.
6. Enable strategies **one at a time**, watching each.
7. Release the kill switch **last**, deliberately, and record who released it and when. Re-arming and releasing is audited; it is never automatic and never on a timer.

If any step is ambiguous, stop and leave the switch on. A system that stays down overnight costs nothing here.

---

## 7. Post-mortem — required, within 48 hours, blameless

Required for every SEV-1 and SEV-2, and for any SEV-3 that recurred.

**Blameless means: the question is never "who did this", it is "what made this possible".** Every incident in this system was made possible by a missing guardrail, an absent test, an unlogged decision, or a document that stated a rule without its reason. Those are the findings. A post-mortem that concludes someone should have been more careful has found nothing, because the next person will be exactly as careful as this one was.

Write `docs/incidents/<yyyy-mm-dd>-<kebab-slug>.md`:

1. **Timeline** in UTC — first bad event, first detection, kill switch on, root cause identified, kill switch off. The gap between the first bad event and first detection is usually the most important number in the document.
2. **Impact** — what actually happened, in numbers.
3. **Root cause** — one sentence, mechanical, no adverbs.
4. **Why it was not caught** — which test, gate, alert, or type would have caught it and did not exist. This is the section that produces the work.
5. **Why detection took as long as it did.**
6. **What went right.** Genuinely. Guardrails that held are evidence about which ones to build more of.
7. **Actions** — each a filed issue with a number, an owner, and a milestone. An action without an issue number is a wish.

Actions worth preferring, in order: a type or structural constraint that makes the failure unrepresentable; a test that fails closed; an alert on the leading indicator rather than the symptom; a document with the reason attached. Adding a config flag to bypass the gate that caught it is never the action — gates exist because someone will be in a hurry later, and that someone is you.

Link the post-mortem from the incident issue and close it.
