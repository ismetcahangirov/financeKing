# Failsafe

The kill switch, degraded operating modes, and the ordering of recovery.

`ERROR_RECOVERY.md` covers what to do about specific failures. This document covers what the system does when it does not know what is happening.

---

## 1. Fail closed

**When the system is uncertain, it stops trading. It never continues on an assumption.**

This is the single governing principle and it is not the intuitive one. The intuitive design keeps trading through problems, because stopping has an obvious cost — missed opportunity, an open position left unmanaged, a research cycle interrupted — while continuing has a cost that is invisible until it is large.

The asymmetry that settles it:

- A system that halts when it should have continued loses the expected value of the trades it did not take. That is bounded, measurable, and usually small: over any short window the expected value of a marginal trade is close to zero, because that is what a Sharpe of 1 means in units of hours.
- A system that continues when it should have halted takes positions using state it cannot verify. That is unbounded, and it compounds — bad state produces bad orders, bad orders produce bad fills, bad fills produce bad attributed PnL, and bad attributed PnL feeds the survival score and breeds.

"Continue and log a warning" is the worst option in the list in `DECISION_FRAMEWORK.md` §8 wearing a responsible-looking hat.

Two consequences that people push back on and should not:

- **The absence of a signal is not a safe state.** No data does not mean no change. A silent market data feed and a market moving 8% look identical from inside the process.
- **Failing to *read* the failsafe state fails closed too.** If the kill-switch table is unreadable at boot, the system boots halted. A safety mechanism whose unavailability means "no restriction" is not a safety mechanism.

---

## 2. The kill switch

### 2.1 Triggers

Every trigger is evaluated continuously, not on a schedule. Each carries a default and a compiled ceiling in the pattern of `RISK_PHILOSOPHY.md` §9 — config can tighten, never loosen.

| # | Trigger | Default threshold |
|---|---|---|
| 1 | Portfolio drawdown from persisted high-water mark | ≥ 10% |
| 2 | Daily loss (mark-to-market, incl. unrealised) vs 00:00 UTC equity | ≥ 3% |
| 3 | Rolling 24-hour loss | ≥ 4.5% |
| 4 | Loss velocity | ≥ 1.5% of equity within any 5-minute window |
| 5 | Reconciliation divergence beyond dust tolerance, persisting after 2 attempts | any |
| 6 | Order rejection rate | > 20% over the last 20 orders |
| 7 | Market data staleness for a symbol with an open position | > 10× the symbol's 99th-percentile inter-tick gap |
| 8 | Clock skew versus exchange server time | > 1000 ms |
| 9 | Unhandled exception raised inside `risk` or `execution` | any |
| 10 | Gross exposure or position count exceeding a **hard ceiling** | any |
| 11 | Audit write failure | any |
| 12 | Manual trip (CLI, API, dashboard) | — |

Notes on the less obvious ones:

**#4, loss velocity, is the trigger that catches what the daily limit cannot.** A 3% daily limit permits losing 2.9% over 20 hours, which is a bad day, and losing 2.9% in four minutes, which is a broken system or a market event. Those require different responses and the same threshold cannot distinguish them. Velocity fires first in the second case, which is the case where speed matters.

**#6, rejection rate, is a proxy for "our model of the venue is wrong".** A sustained rejection rate means the exchange's symbol filters, minimum notionals, tick sizes or margin requirements are not what we believe them to be. Every one of those is a condition under which our sizing arithmetic is producing invalid orders, and the invalid ones we notice are unlikely to be the only ones that are wrong.

**#7 uses a per-symbol threshold derived from measured data**, not a constant. Crypto trades continuously, so a gap is always anomalous, but "always" differs by four orders of magnitude between BTCUSDT and a thin alt. The 99th-percentile inter-tick gap is recomputed nightly over a trailing 30 days.

**#10 should be impossible.** Hard ceilings are enforced at order construction; exceeding one means the enforcement itself failed. It is a trigger precisely because it indicates a defect in the mechanism that is supposed to prevent it, and at that point nothing else in the process can be trusted either.

**#11 exists because trading without an audit trail is not permitted.** See §3.4.

### 2.2 What tripping does

In order:

1. **Append a `TRIP` event** to `kill_switch_events` with the trigger id, the measured value, the threshold, the correlation ID of the causing event, and a full snapshot of portfolio state at the moment of the trip. This is written *first*, before any remediation, so that a crash during remediation still leaves the system halted on restart.
2. **Block all new order construction.** The check is the first statement in `RiskEngine.decide()`, under the same lock that guards order construction. It returns a `Rejection`, not an exception — the caller is not required to handle an unexpected error, and the rejection is auditable.
3. **Cancel all resting orders**, best effort, in parallel, with a 2-second deadline. Failures to cancel are recorded and retried by the recovery path, not by the trip path.
4. **Disable strategy signal consumption.** Strategies keep running and keep emitting signals; the signals are recorded and dropped. Keeping them running means the post-incident audit shows what the system *would* have done, which is often the most useful artefact in the investigation.
5. **Emit alerts** across every configured channel, with the incident ID.
6. **Flatten open positions, sized from venue state.** See §2.4. If venue state cannot be read, do not flatten — stay halted with positions open and page.

### 2.3 Latency

Split into three requirements, because they have genuinely different characters:

| Path | Requirement | Why it is achievable |
|---|---|---|
| Trip → new orders blocked | **Zero windows**, not a time budget | The flag check shares the lock with order construction. There is no interleaving in which the switch is tripped and an order is nonetheless built. This is a structural guarantee, not a latency target. |
| Triggering event → trip decision | ≤ 100 ms p99 | Trigger evaluation runs in the same process as the position and equity updates that feed it. No bus round trip. |
| Trip → cancellations submitted | ≤ 2 s p99 | Bounded by venue API latency, which is why it is a target rather than a guarantee. |

The first row is the important one and it is the reason the kill switch is not implemented as an event-bus subscriber. A subscriber has a queue, and a queue has a window in which the switch is tripped and the order path has not yet heard about it. That window is exactly when orders are most dangerous.

### 2.4 Why the default is flatten, and why it reads from the venue

`on_trip_flatten` defaults to **`true`**. The trip closes the book. The full argument, including the rejected alternative at its strongest, is [ADR 0014](docs/adr/0014-kill-switch-flattens-on-trip.md); this section states the operational shape.

The decisive reason is internal consistency. `.claude/rules/error-handling.md` gives the supervisor exactly one sanctioned `except Exception`, and that handler trips the switch, calls `execution.flatten_all()`, audits and exits. An unhandled exception is the least-understood state this system can reach, and it is already answered by flattening. A kill switch that instead left positions open would make the response to uncertainty depend on which code path happened to notice it, which is not a safety design.

The second reason is that **this system runs unattended.** "Stop making it worse and let a human decide" needs a human inside the window over which an open crypto position can move, and between an 03:00 trip and someone waking up there is no such human. The cost of a flatten you did not need is slippage on one exit. The cost of a position you could not supervise is not bounded by anything.

**The objection that survives, and shapes the implementation.** Several triggers indicate that our view of positions may be *wrong* — a reconciliation divergence most obviously. Closing orders sized from a position record we have just established we do not trust can open a position rather than close one. That is a real bug and it is not answered by arguing about slippage.

So the flatten never reads local position records:

1. Snapshot positions, open orders and the last reconciliation result **to the audit log first**. This is the post-mortem artefact, and it is the thing the old cancel-only default gave us for free (see §2.4.1).
2. Query the venue for current positions. This is the same source-of-truth principle as reconciliation (`ARCHITECTURE.md` §7).
3. Close **what the venue says we hold**, not what we believe we hold.
4. If the venue cannot be read — network failure, `SafetyViolation`, auth rejection — **do not flatten.** Stay halted with positions open, emit `killswitch.flatten_blocked` at `CRITICAL`, and page. Flattening on a guess is the failure this ordering exists to prevent.

Invalidation-level protective orders are the one exception to step 3 of §2.2 and are not cancelled until the flatten supersedes them, so there is no window in which the book is both unprotected and unclosed.

`on_trip_flatten = false` remains a legitimate configuration for a supervised deployment where a human is genuinely on call within minutes. It is not the default here, and it is bounded by the same compiled-in ceiling pattern as every other risk parameter.

#### 2.4.1 What flattening costs us, stated plainly

You can no longer trip the switch to freeze the book and inspect it — by the time an investigator looks, the positions are gone. That is a real loss, and step 1 above exists specifically to replace it. If a genuine freeze-and-inspect mode is ever needed, it is a separate halt mode and a superseding ADR, not a flag on this one.

Slippage on kill-switch flattens is therefore a tracked metric with an alert (`killswitch.flatten_slippage_bps`), because a switch that is expensive to trip creates pressure to raise its thresholds, and that pressure is how kill switches quietly stop working.

### 2.5 Persistence

State lives in two tables:

- `kill_switch_events` — append-only, the authority. Every `TRIP`, `ARM`, `RESUME`, with actor, reason and timestamp.
- `kill_switch_state` — a materialised current view, derived, rebuildable from the event log.

On boot, the system reads the latest event. If it is a `TRIP` without a subsequent `RESUME`, **the system boots halted**, regardless of how much time has passed, whether the process crashed, or whether the underlying condition has cleared.

If the table cannot be read — database down, migration mid-flight, permissions wrong — **the system boots halted**. Unknown is treated as tripped. This is the concrete instance of §1 that people most often get backwards, because the natural implementation wraps the read in a try/except and defaults to "not halted" so that startup does not break.

A restart is not a reset. That has to be stated because "turn it off and on again" is the universal instinct, and in a system with a kill switch it is an attempt to bypass the kill switch — usually not consciously.

### 2.6 Resume requires a human

There is no automatic resume. Not on a timer, not when the drawdown recovers, not when the data feed comes back, not when reconciliation goes clean. A system that unhalts itself has a kill switch in name only, because every trigger condition is transient by nature and waiting is always sufficient to clear it.

The procedure is two-step and deliberately awkward:

```bash
fking safety arm    --incident INC-2026-0114-03
fking safety resume --incident INC-2026-0114-03 \
                    --root-cause "Testnet spot wipe; venue epoch advanced to 7" \
                    --acknowledge
```

`arm` is valid for 120 seconds. `resume` will refuse unless **all** of:

1. The incident record exists and its `root_cause` field is non-empty and at least 20 characters. A resume with an empty cause is a resume with no diagnosis.
2. A full reconciliation has completed cleanly within the previous 5 minutes (`ERROR_RECOVERY.md` §4).
3. The originally triggering condition currently evaluates false.
4. The operator token is present and the operator identity is recorded on the `RESUME` row.
5. The recovery sequence in §4 has completed through step 7.

Condition 1 is the one that matters. The others are mechanical checks a script could satisfy. Requiring a written root cause is the point at which a person has to have understood the incident, and it is the only part of the procedure that cannot be automated by someone in a hurry. The text is stored on the append-only event row and appears in the incident report.

Resume restores trading globally. It does **not** un-suspend individual strategies that were suspended for their own drawdown breaches — those re-enter through evaluation (`EVOLUTION_ENGINE.md` §2).

---

## 3. Degraded operating modes

Each mode is an explicit named state, entered and exited with an audit event, visible on the dashboard, and exposed as a metric. There is no unnamed degraded state — if the system's behaviour differs from normal, it has a name and someone can see it.

### 3.1 `DATA_STALE`

**Entry.** Any subscribed symbol's last tick is older than 5× its 99th-percentile inter-tick gap.

**Behaviour.** No new positions in the affected symbols. Existing positions keep their resting invalidation orders at the venue. Strategies depending on the stale symbol receive an explicit `FeatureUnavailable` rather than a forward-filled value — forward-filling a price during an outage produces a flat series, which every volatility estimator reads as calm and every mean-reversion strategy reads as an opportunity. Both are exactly wrong.

**Escalation.** Staleness beyond 10× the p99 gap with an open position trips the kill switch (trigger #7).

**Exit.** Two consecutive fresh ticks. One tick could be a stale replay from a reconnecting feed.

### 3.2 `EXCHANGE_UNREACHABLE`

**Entry.** Two consecutive failed REST calls or a WebSocket disconnect that does not recover within 30 seconds.

**Behaviour.** No new orders. Reconnection with backoff (`ERROR_RECOVERY.md` §3). **On reconnect, reconciliation runs before anything else** — before resuming market data, before resuming strategies, before processing anything queued. The gap is exactly the interval in which positions can have changed without us: stop-outs, liquidations, partial fills of orders we thought were resting.

**The critical rule: never assume an in-flight order was not placed.** A request that timed out has an unknown outcome, and "unknown" is not "no". See `ERROR_RECOVERY.md` §5.

**Escalation.** Unreachable for more than 10 minutes with open positions trips the kill switch. The threshold is deliberately generous — brief testnet outages are routine and tripping on every one trains people to resume without reading.

### 3.3 `LLM_QUOTA_EXHAUSTED`

**Entry.** Primary provider (Gemini free tier) quota exhausted and the fallback (Groq free tier) also unavailable.

**Behaviour.** **Trading continues unaffected.** Research, hypothesis generation, strategy proposal and agent-authored evolution pause and queue. Deterministic evolution operators — mutation, crossover, scoring, promotion — continue, because none of them require an LLM.

**This mode is a non-event for trading, and that is a design assertion worth checking.** If quota exhaustion ever affects order flow, an LLM has crept into the order path, which `ARCHITECTURE.md` §9 forbids. The failure mode is therefore also a test: a chaos check that force-exhausts the quota and asserts that trading metrics are unchanged runs weekly. It is the cheapest available verification that the agent layer is genuinely on top of the deterministic core rather than inside it.

**Exit.** Quota window resets, or the fallback recovers. Queued work resumes oldest-first, subject to a queue age limit — a hypothesis generated against six-day-old market conditions is discarded rather than executed late.

### 3.4 `DATABASE_UNAVAILABLE`

**Entry.** Postgres unreachable, or an audit write fails.

**Behaviour.** **Trip the kill switch immediately.** This is the least negotiable entry in the table.

The reasoning: the audit log is not a record of trading, it is a *precondition* for trading. `ARCHITECTURE.md` §11 requires that any trade be fully reconstructable from the audit log alone. A trade executed while the audit is down is a trade that permanently cannot be reconstructed — not "harder to reconstruct", not "reconstructable from logs", permanently not. The database is also where the kill-switch state, the high-water marks, the trial counter and the position record live; without it the system cannot even establish whether it is supposed to be trading.

The tempting alternative is to buffer audit writes in memory and continue. That converts a bounded outage into a permanent hole in the record the moment the process restarts, and process restarts are correlated with database problems.

Redis being unavailable is a different and lesser matter: the event bus is at-least-once and every consumer is idempotent, so a Redis outage degrades to a stall, not to a correctness problem. Redis unavailable for more than 60 seconds enters `EXCHANGE_UNREACHABLE`-equivalent behaviour (no new orders) but does not trip.

### 3.5 `FEATURE_STORE_PARTIAL`

**Entry.** A declared feature cannot be computed for a symbol — upstream data gap, failed nightly job, schema mismatch.

**Behaviour.** Strategies depending on that feature emit no signal. They do not receive a substituted value, an imputation, a forward fill, or a zero. This is the same argument as §3.1 and it is worth repeating because imputation is the single most tempting shortcut in the entire data path: it always produces a number, the number always looks reasonable, and the resulting strategy behaviour is unfalsifiable.

---

## 4. Recovery ordering

Recovery is a fixed sequence. Steps do not run in parallel and none may be skipped. Each writes an audit event on entry and exit, so a stalled recovery is visible at the step it stalled on.

```
1. Safety kernel        allowlist loaded, hosts resolved and verified, allowlist logged
2. Persistence          Postgres reachable, migrations current, audit write proven with a probe row
3. Kill-switch state    read; if TRIP without RESUME, remain halted (this is the normal case)
4. Connectivity         exchange reachable, clock skew < 1000 ms, API keys valid
5. Reconciliation       exchange → local. Positions, balances, open orders. Must be clean.
6. Risk state           high-water marks, daily/rolling loss counters, drawdown scalars, venue epoch
7. Market data          subscriptions live, staleness cleared for every traded symbol
8. Strategies           re-armed, state rebuilt from persisted signal history
9. Human resume         §2.6
```

### Why the order is this order

**Safety kernel first**, before anything touches the network. The allowlist has to be established before a single HTTP client exists, or the first thing a compromised config does is talk to somewhere it should not.

**Persistence second, with a proven write.** Reachability is not the same as writability — a read-only replica, an exhausted disk or a revoked grant all answer `SELECT 1` cheerfully. The probe writes and reads back an audit row.

**Step 6 before step 7, and both before step 8.** This is the ordering constraint people get wrong and it is worth stating as its own rule:

> **A restarted system that has forgotten its high-water mark has silently reset its drawdown limit.**

Consider: equity peaked at 100, drawdown limit 20%, current equity 85. The system restarts. If it initialises its high-water mark from current equity, the limit is now 20% below 85, i.e. 68 — it has quietly granted itself an extra 15% of drawdown, at exactly the moment when the evidence says it should have less. Nothing in the logs is wrong. The metric reads "drawdown: 0.0%". The limit will not bind until the account has lost nearly a third of its peak.

The same applies to the daily loss counter (a restart at 15:00 UTC must not reset the day's loss), the rolling 24-hour window, and the drawdown de-risking scalar from `RISK_PHILOSOPHY.md` §6. All of these are persisted and restored, never recomputed from a fresh in-memory series. This is also the class of bug that a testnet wipe creates by other means — see `ERROR_RECOVERY.md` §8.

**Step 8 before step 9.** Strategies are re-armed and their internal state rebuilt *before* the human resumes, so that resume is instantaneous and the first post-resume signal comes from a strategy with correct state rather than one that is still warming up.

### Partial recovery is not recovery

If any step fails, the sequence stops at that step and the system stays halted. It does not proceed with the steps that would have succeeded. A system that recovered connectivity and market data but not reconciliation is a system trading with an unverified position record, which is worse than a system that is simply off — it looks operational on every dashboard.
