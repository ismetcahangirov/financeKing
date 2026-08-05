# Error Recovery

A taxonomy of failures and the correct response to each.

`FAILSAFE.md` covers what happens when the system does not know what is wrong. This document covers the cases where it does.

---

## 1. The taxonomy

Six classes. The class determines the response; the specific error determines only the details. Getting the classification right matters more than handling any individual error well, because the wrong class produces a confidently wrong response.

| Class | Signature | Correct response | Wrong response that looks right |
|---|---|---|---|
| **Transient network** | Timeout, connection reset, 5xx, WebSocket drop | Retry with backoff — **reads only** | Retrying a write |
| **Exchange rejection** | 4xx with a business error code | Do not retry. Classify, record, fix the input or halt | Retry, because "it might work this time" |
| **Data gap** | Missing bars, missing trades, sequence discontinuity | Mark the gap, backfill from archive, refuse features spanning it | Interpolate |
| **State divergence** | Local ≠ exchange | Reconcile. Exchange is truth | Trust the more recent write |
| **Process crash** | Restart with unknown in-flight work | Replay the write-ahead intent log, then reconcile | Assume in-flight work did not happen |
| **Testnet wipe** | Keys valid, world gone | Advance the venue epoch (§6) | Book it as trading losses |

The last column is the point of the table. Every one of those wrong responses is the natural implementation.

---

## 2. Transient network

Timeouts, connection resets, HTTP 5xx, WebSocket disconnects, and the ccxt/Binance transport-level codes `-1001 DISCONNECTED`, `-1006 UNEXPECTED_RESP`, `-1007 TIMEOUT`.

Retry with backoff, **for idempotent reads only**. Reads: account state, open orders, trade history, klines, exchange info, server time. Writes get §3.

The distinguishing property of this class is that the request has an unknown outcome and the operation has no side effect, so repeating it is free. The moment either half of that stops being true, it is not this class.

---

## 3. Retry policy

```
delay_n = uniform(0, min(cap, base · 2ⁿ))     full jitter
base = 250 ms      cap = 30 s      max_attempts = 6      total budget ≈ 60 s
```

Full jitter, not exponential-with-fixed-delay and not decorrelated jitter. With several consumers reconnecting after the same outage, fixed backoff synchronises them into a thundering herd that reproduces the outage; full jitter spreads them uniformly and is the variant that measures best under contention.

The retry budget is wall-clock bounded, not attempt bounded. Six attempts that take four minutes is not a retry policy, it is an outage with extra steps — after 60 seconds the operation fails to its caller, which for market data means `DATA_STALE` and for the venue means `EXCHANGE_UNREACHABLE` (`FAILSAFE.md` §3).

### When not to retry

| Condition | Code | Why not |
|---|---|---|
| Authentication / permission | `-2015`, HTTP 401 | Retrying an invalid key produces an IP ban, not a success |
| Filter violation | `-1013` (`LOT_SIZE`, `MIN_NOTIONAL`, `PRICE_FILTER`), futures `-4131` | Deterministic. The same invalid order will be invalid every time. Fix the sizing, or halt |
| Insufficient balance | `-2010` | Retrying does not create balance. Reconcile — our balance record is wrong or a position is not what we think |
| Unknown order | `-2011`, `-2013` | The venue is answering a question, not failing. Treat the answer as data |
| IP banned | HTTP 418 | Retrying extends the ban. Back off until the stated expiry, then resume at reduced weight |
| **Any order placement** | — | §5 |

### Rate limits are a throttle problem, not a retry problem

Binance rate limits are weight-based and every response carries `X-MBX-USED-WEIGHT-1M`. The system tracks used weight and **throttles proactively at 80% of the limit**, shedding low-priority requests (backfills, research queries) before high-priority ones (order state, reconciliation).

By the time you receive a `429`/`-1003` it is already too late to be graceful: repeated 429s escalate to an HTTP 418 IP ban, and Binance ban durations escalate from 2 minutes to 3 days. A three-day ban on the only venue is an outage that no retry policy recovers from. Handling 429 correctly is therefore not the interesting part of rate limiting; never generating one is.

When a `429` does arrive, honour `Retry-After` exactly and treat it as an incident — it means the throttle is miscalibrated.

---

## 4. Reconciliation: the universal recovery primitive

Every recovery path in this document ends in reconciliation. It is the only operation that restores certainty, because it is the only one that asks the authority.

**Exchange state is truth. Local state converges to it. Never the reverse.** Not "merge", not "take the most recent write", not "prefer local because we have more context". If the local record and the venue disagree, the local record is wrong — even when we are sure it is not, because the alternative rule has no fixed point.

### The algorithm

1. Fetch, in one logical snapshot: balances, open orders, positions (futures), and `myTrades` since the last known trade id per symbol.
2. Build the venue view.
3. Diff against the local view, three-way across `intents`, `fills` and current positions.
4. Classify every divergence and apply the resolution.
5. Emit a reconciliation report event, clean or not.

### Divergence classes

| Class | Meaning | Resolution |
|---|---|---|
| `MISSING_LOCAL` | Venue has a fill/order we do not | Ingest it. Attribute (see below) |
| `MISSING_REMOTE` | We believe an order is resting; venue disagrees | Query by `clientOrderId`. Filled → ingest. `-2013` → mark `VOID_UNCONFIRMED` |
| `QUANTITY_MISMATCH` | Position sizes differ beyond tolerance | Adopt venue quantity; record the delta to `unattributed` |
| `PRICE_MISMATCH` | Average entry differs | Adopt venue; the delta is a fill we mis-recorded — investigate, do not silently absorb |
| `BALANCE_MISMATCH` | Free/locked balance differs | Adopt venue. Repeated occurrences indicate a fee model error |

### Dust tolerance is the venue's quantum, not an epsilon

The comparison tolerance for a symbol's quantity is that symbol's `LOT_SIZE.stepSize`, and for price its `PRICE_FILTER.tickSize`. Not `1e-8`, not a relative tolerance, not a configurable float.

Two reasons. First, step sizes span eight orders of magnitude across symbols; a single global epsilon is simultaneously too tight for one symbol and too loose for another, and the too-loose direction hides real divergence. Second, this codebase uses `Decimal` specifically so that quantities are exact — introducing a relative tolerance re-imports the floating-point reasoning that `Decimal` was adopted to eliminate. The only legitimate slack is the granularity the venue itself cannot represent.

### Attribution of unexpected fills

A fill discovered during reconciliation is attributed to the strategy whose recorded intent matches its `clientOrderId`. If no intent matches — the fill came from somewhere we have no record of — it goes to an `unattributed` account.

**It is never distributed pro-rata across strategies.** Spreading an unexplained fill across the population injects noise into every strategy's attributed PnL, which propagates into every survival score, which propagates into breeding decisions. An unattributed account with a non-zero balance is an obvious, visible, investigable defect. Pro-rata distribution is the same defect made invisible and made to contaminate the objective function. This is the recurring shape of the worst bugs in this system: the tidy-looking option is the one that corrupts the thing being optimised.

---

## 5. Order placement: reconcile, never retry

**A timeout on an order placement is not a failure. It is an unknown outcome, and "unknown" is not "no".**

The order may have reached the matching engine and filled before the response was lost. Re-sending is not a retry, it is a second order, and the failure mode is a double position discovered later by reconciliation — if you are lucky, or by a margin call, if you are not.

The rule: **never blindly retry an order placement. Reconcile first, then decide.**

### Write-ahead intent log

Every order goes through three durable steps:

```
1. INTENT   append-only row written to Postgres, including the derived clientOrderId
            — before any network call
2. SEND     request issued
3. OUTCOME  ACK | REJECT | UNKNOWN recorded against the intent
```

A crash at any point leaves a durable intent whose outcome is discoverable. Recovery walks every intent in `UNKNOWN` and resolves it by query, never by re-send. This is the same write-ahead discipline a database uses, for the same reason: the only way to survive a crash between "decided" and "did" is to have written down that you decided.

### Deterministic client order IDs

```
client_order_id = "fk-" + base32(blake2s(correlation_id ‖ strategy_id ‖ intent_seq, digest_size=12))
```

23 characters, within Binance's 36-character `newClientOrderId` limit and its `^[\.A-Z\:/a-z0-9_-]{1,36}$` character class.

The ID is derived from the *intent*, not from the attempt. The same logical order always produces the same ID. Consequences:

- The venue becomes the deduplication authority. If a retry ever does escape, Binance rejects it with `-2010 "Duplicate order sent."` — and **that rejection is a success signal**, not an error. It tells us the first attempt reached the engine. The handler for `-2010 Duplicate` queries the order and ingests its state.
- Recovery can query by ID without having received a response containing the exchange's `orderId`. `GET /api/v3/order?origClientOrderId=...` resolves the unknown. This is the property that makes crash recovery possible at all; a randomly generated ID recorded only on success gives you nothing to ask about.

### The resolution procedure for an `UNKNOWN` outcome

1. Query by `origClientOrderId`.
2. Order exists → ingest its true state (`NEW`, `PARTIALLY_FILLED`, `FILLED`, `CANCELED`). Done.
3. `-2013 Order does not exist` → check `myTrades` for the symbol over the window, in case the order filled and aged out of the open-orders view.
4. No trace in either → and only then may the intent be re-sent, reusing the same `clientOrderId`.
5. Query itself fails → stay in `UNKNOWN`, do not re-send, enter `EXCHANGE_UNREACHABLE`.

Step 3 exists because "not in open orders" and "never existed" are different states that the naive check conflates, and the difference between them is an entire position.

---

## 6. Data gaps

Detection: expected versus actual bar counts per `(symbol, interval, day)`; trade-id sequence discontinuities; a checksum mismatch against the archive manifest.

Response:

1. Record the gap in `data_gaps` with its exact bounds. The gap is a first-class fact, not an absence.
2. Backfill from the Binance data archive where available, re-verifying the checksum.
3. If it cannot be filled, **features whose lookback window spans the gap are marked unavailable for that window.** They are not computed across it. A 20-period moving average computed over 20 bars that straddle a four-hour hole is not a 20-period moving average, and nothing downstream can tell.
4. Backtests overlapping a known gap either exclude the window explicitly or fail. There is no interpolation path.

The awkward consequence, stated because it will come up: a gap discovered *after* backtests have run against that period invalidates those results. The affected runs are flagged and their conclusions withdrawn — but **the trial counter does not decrease**. `K` counts hypotheses examined, not hypotheses examined correctly. Trials spent on corrupted data are still trials spent, and pretending otherwise would understate `SR*` for everything that follows (`EVOLUTION_ENGINE.md` §5.1). Bad data costs you twice, and the second cost is permanent.

---

## 7. Process crash

Redis Streams delivers at-least-once and every consumer is idempotent by design (`CLAUDE.md` §2), so message-level recovery is mechanical: on restart, claim the pending-entries list for the consumer group (`XPENDING` → `XCLAIM` for entries idle > 60 s) and reprocess. Reprocessing a message that was already handled is a no-op by construction.

Idempotency is achieved by natural keys, not by a "seen messages" set: a fill is keyed by `(venue, trade_id)`, an intent by `clientOrderId`, a signal by `(strategy_id, bar_timestamp)`. Insert with `ON CONFLICT DO NOTHING`. A deduplication cache is a cache, and caches have eviction, and eviction of a dedup entry is a duplicate.

After message replay, the sequence in `FAILSAFE.md` §4 runs in full. Note in particular that risk state — high-water marks, loss counters, de-risking scalars — is restored from persistence and never recomputed from a fresh in-memory series. A restart that recomputes the high-water mark from current equity silently widens the drawdown limit, which is the failure mode described in `FAILSAFE.md` §4 and is invisible in every metric.

---

## 8. Recovering from the Binance spot testnet wipe

The spot testnet resets roughly every 30 days with no notice. **API keys survive. Balances, open orders and trade history do not.** This happens about twelve times a year, so it is not an edge case — it is a scheduled operation that arrives unscheduled, and it must be tested rather than documented. A chaos test replays a recorded post-wipe response set against the recovery path monthly.

### Detecting it, and distinguishing it from a liquidation

A wipe and a catastrophic loss look similar from a balance query and require opposite responses. The signature requires **at least three** of:

1. API key still authenticates — no `-2015`.
2. Balances are at default faucet values, or zero, across *all* assets simultaneously.
3. Open orders empty while local records show resting orders.
4. `myTrades` returns empty for symbols with known local trade history.
5. An order for which we hold a venue-signed ACK returns `-2013 Order does not exist`.

Signals 4 and 5 are decisive. **A liquidation cannot erase trade history.** If we lost everything through trading, `myTrades` is full of the trades that lost it, and the orders we hold ACKs for are still queryable. History disappearing is the venue's state being reset, not ours being destroyed.

### The procedure

```
1. Kill switch trips              (automatic — reconciliation divergence, FAILSAFE §2.1 #5)
2. Assert the wipe signature      ≥ 3 signals, including at least one of #4/#5
3. Advance venue_epoch            N → N+1, recorded append-only with the detection evidence
4. Void the book                  open orders and positions closed as VOID_VENUE_RESET,
                                  PnL to the `venue_reset` account — NOT to any strategy
5. Re-baseline risk state         starting equity from the venue; high-water mark = new equity;
                                  daily and rolling loss counters zeroed; drawdown scalars reset
6. Reconcile                      must come back clean against the new epoch
7. Human resume                   FAILSAFE §2.6, root cause "spot testnet wipe, epoch N+1"
```

### Step 4 is the one that matters

**The wipe must never be booked as trading losses.**

The naive implementation marks positions to zero and lets the equity curve record the difference. If the account was at 12,300 and the faucet resets it to 10,000, that books a −18.7% return that no strategy caused and no market produced. Follow it through:

- The portfolio drawdown trigger fires at 10%, so this clears it outright: the system halts on a loss that never happened, and a human has to diagnose an incident that has no cause.
- Every live strategy's drawdown-discipline component (`SCORING_ENGINE.md` §2.2) is destroyed, because `MaxDD` and the ulcer index both take the hit.
- `c_rar` collapses, since one −18.7% observation dominates the volatility and the skewness of a daily series.
- Strategies drop below the retention floor and are retired with reason class `risk` or `decay`.
- Retirement is permanent and tombstoned (`EVOLUTION_ENGINE.md` §8), so they are gone for good and the search is forbidden from re-proposing them.

The venue's monthly housekeeping would have wiped the strategy population, not just the balance — and it would have done so through a chain of individually correct-looking mechanisms, each doing exactly its job.

### Performance is stitched by returns, never by equity

Consequently: **all performance statistics are computed within a venue epoch and chained across epochs by compounding returns.**

```
equity_curve = Π_epochs Π_t (1 + r_t)          not      concat(equity_series)
```

Absolute balance is discontinuous at an epoch boundary; returns are not. Trade history from previous epochs is retained and remains valid — those trades really happened and their fills were real — but the balance series has a cut in it, and every statistic that reads the equity path has to respect that cut. The epoch id is a column on every performance row so that a query which forgets to partition by it fails loudly at the join rather than quietly averaging across a discontinuity.

### What does not need recovering

API keys and Ed25519 keys survive the wipe. Do not re-register, do not rotate, do not treat the wipe as an authentication problem — the first symptom people react to is "everything returns nothing", and re-issuing credentials is the instinctive response. It changes nothing, invalidates the audit trail's key fingerprints, and costs an afternoon.

---

## 9. Reverting a commit that squashed a migration

`GIT_WORKFLOW.md` §7 requires a pull request containing an Alembic migration to be merged with a merge commit, so that the migration survives as its own revertable commit. That rule has been violated once, silently: **`da9c42b` (#147) was merged with `gh pr merge --merge` and squashed anyway**, so `migrations/versions/0012_gap_resolution.py` lives inside a commit that also carries the code using it. The same is true of `de44c17` (#145). The tree is correct and both migrations apply cleanly; what is broken is revertability.

`main` is not rewritten to fix this. Force-push is blocked and would be wrong: `main`'s history is what the release tags point into. The defect is repaired at the moment somebody tries to revert, and this is the procedure.

### What goes wrong if you revert it plainly

```bash
git revert da9c42b     # deletes migrations/versions/0012_gap_resolution.py from the tree
```

Every database that has been upgraded still holds `0012_gap_resolution` in `alembic_version`. Alembic resolves the current revision by looking up that id in the migration directory, finds nothing, and refuses **every** operation — `upgrade`, `downgrade`, and `current` — with an error about a revision it cannot locate rather than about a file you deleted. The application will not start. Recovery from there is hand-editing `alembic_version` on a live database under time pressure, which is precisely what the rule exists to prevent.

### The procedure

1. **Revert, then restore the migration file in the same commit.** The revert is of the *code*, never of the schema.

   ```bash
   git revert --no-commit da9c42b
   git checkout da9c42b -- migrations/versions/0012_gap_resolution.py
   git commit -m "revert(data): back out the REST gap backfill, keeping migration 0012"
   ```

2. **Confirm the revision chain is intact.** `0013`'s `down_revision` points at `0012`; if the file were gone, the chain would have a hole rather than an obvious error.

   ```bash
   uv run alembic heads          # exactly one head
   uv run alembic current        # matches what the database holds
   ```

3. **Leave the schema alone.** A migration whose code is reverted leaves tables and columns nothing reads. That is harmless and is the intended end state — migrations are forward-only (`GIT_WORKFLOW.md` §7), and dropping the objects means writing a *new* forward migration, reviewed on its own, never a `downgrade()` run against a database holding audit history.

4. **State it in the revert's body**, naming the commit and the migration kept. The next reader's question is "why does this revert restore a file?", and the answer has to be in the commit rather than in this document.

### Why this is not automated

A check could detect that a `main` commit both touches `migrations/versions/` and has one parent. It would fire on two historical commits, forever, with no action available — `main` is not being rewritten — which is how a check becomes noise that people learn to ignore. The enforceable point is *before* the merge, and that is the `merge:commit` label the `PR metadata` check requires.
