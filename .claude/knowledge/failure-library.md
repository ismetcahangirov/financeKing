# Failure Library

The catalogue of ways this system breaks, indexed by **the symptom you will actually observe** rather than by the cause.

**Why indexed by symptom.** When you are debugging, you do not have the cause — that is the thing you are looking for. You have a weird number, a stalled consumer, or a backtest that looks too good. This file is the lookup from what you see to what it usually is. Read the symptom column first.

**Why this file exists at all.** This project is built mostly by AI across sessions with no shared memory. Without a durable record, every failure is a first-time failure forever, and the expensive part of debugging — recognising the shape — never accumulates. A post-mortem tells the story of one incident; this file is what the next session actually reads.

## Status values

| Status | Meaning |
|---|---|
| **observed** | It happened here. There is an incident record or a commit that fixed it. |
| **verified-in-research** | Confirmed by direct measurement against the real system, before it had a chance to bite. Traceable to a `VF-NNN` in [`./verified-facts.md`](./verified-facts.md). |
| **class-known** | A well-established failure mode of this kind of system that our design specifically defends against. Recorded so the defence is not "cleaned up" by someone who does not know what it protects against. |

A `class-known` entry is not speculation. It is the reason a specific line of defensive code exists, and deleting that line without reading the entry is how it becomes `observed`.

---

## Index by symptom

| Symptom you observe | Likely entry |
|---|---|
| Balances reconcile to a tiny nonzero residue that grows over weeks | [F-001](#f-001) |
| Backtest and live disagree on P&L by a few cents per trade | [F-001](#f-001) |
| Timestamps render in the year 55000, or in 1970 | [F-002](#f-002) |
| A resampled series has correct endpoints and wrong spacing in the middle | [F-002](#f-002) |
| Backtest results shift when unrelated code runs first | [F-003](#f-003) |
| A one-bar gap at the start of every futures file | [F-004](#f-004) |
| A CSV column that should be numeric has dtype `object` | [F-004](#f-004) |
| JSON decode error on a spot trades archive | [F-005](#f-005) |
| A statistic changed and no code changed | [F-006](#f-006) |
| HTTP 410 on the very first spot user-data call | [F-007](#f-007) |
| Spot auth fails with an apparently correct key | [F-008](#f-008) |
| User data events stop arriving, no error, orders still work | [F-009](#f-009) |
| Auth succeeds, requests succeed, account is empty | [F-010](#f-010) |
| Order rejected: invalid symbol, on a symbol that exists in production | [F-011](#f-011) |
| `UnicodeEncodeError` while logging, or a crash parsing `exchangeInfo` | [F-012](#f-012) |
| Order rejected for `minNotional` or lot size on a small signal | [F-013](#f-013) |
| Position doubles after a network blip | [F-014](#f-014) |
| A message is processed twice after a consumer restart | [F-015](#f-015) |
| Events reprocessed forever after a crash between commit and ack | [F-016](#f-016) |
| A backtest looks excellent and dies immediately in forward testing | [F-017](#f-017), [F-018](#f-018), [F-019](#f-019) |
| Equity curve is implausibly smooth; no losing months | [F-017](#f-017) |
| Sharpe drops by half when the trial count is applied | [F-018](#f-018) |
| Every symbol in the universe has a long, successful history | [F-019](#f-019) |
| A strategy passes with an edge of 3bp and loses money live | [F-020](#f-020) |
| The system keeps running with a position it cannot explain | [F-021](#f-021) |
| An error appears once in the logs and the loop continues | [F-021](#f-021) |
| Agents stop responding; the system stalls rather than degrading | [F-022](#f-022) |
| Log volume explodes; Loki queries time out | [F-023](#f-023) |
| An audit row's content differs from what was written | [F-024](#f-024) |
| A strategy's live behaviour differs from its backtest, same code | [F-025](#f-025) |
| An agent's proposal is acted on and the response was never valid | [F-026](#f-026) |

---

## Money and numerics

### F-001 — Float error accumulating across fills {#f-001}
**Status**: class-known · **Defended by**: [`../../docs/rules/decimal-and-money.md`](../../docs/rules/decimal-and-money.md)

**Symptom**: reconciliation against the exchange leaves a residue that is tiny per trade and grows monotonically over weeks. It looks like an exchange bug. It is not.

**Mechanism**: `Decimal(0.1)` is not `Decimal("0.1")` — the float has already lost the value before `Decimal` sees it. Every fill adds a rounding error of order 1e-17, and thousands of fills turn that into a discrepancy large enough to notice and small enough to be dismissed as a fee-rounding artefact. Mixing `Decimal` and `float` in a single expression silently promotes the whole expression to float.

**Why it is hard to spot**: nothing raises. The number is *almost* right, and "almost right" reads as a rounding convention rather than a defect.

**Detection**: a reconciliation residue that never returns to zero. Zero drift is the only acceptable value.

**Prevention**: `Decimal` constructed from `str`, always; take the raw string from the exchange response before any JSON parser has made it a float; `NUMERIC(38, 18)` in Postgres, never `DOUBLE PRECISION`; serialize as string on the wire.

### F-002 — Timestamp unit mismatch {#f-002}
**Status**: verified-in-research (VF-015) · **Defended by**: [`../../docs/rules/time-and-timezones.md`](../../docs/rules/time-and-timezones.md), ADR 0013

**Symptom**: two variants. The loud one — dates in the year 55000, or in 1970. The quiet one — a resampled series whose first and last timestamps look correct while the spacing in the middle is wrong.

**Mechanism**: Binance spot timestamps became microseconds from 2025-01-01; futures stayed milliseconds. A global unit constant is wrong for at least one half of the corpus; a per-market constant is wrong for spot before 2025.

**Why it is hard to spot**: the quiet variant produces a series that passes every summary check. Row counts are right, min and max are right, and only the intervals are wrong — which no default validation looks at.

**Detection**: print the first and last **raw integers** and confirm they render as sane UTC. Assert the modal inter-bar interval equals the expected bar width.

**Prevention**: normalization keyed on `(market, date)`, never a constant.

### F-003 — Decimal context mutated globally {#f-003}
**Status**: class-known · **Defended by**: [`../../docs/rules/decimal-and-money.md`](../../docs/rules/decimal-and-money.md)

**Symptom**: results change depending on what ran first in the process. A test passes alone and fails in the suite, or vice versa.

**Mechanism**: `decimal.getcontext()` is thread-local global state. Any code that sets precision or a rounding mode for its own convenience changes it for everything that runs afterwards.

**Detection**: run the suite in a randomised order — a stable seed with a shuffled order is exactly what surfaces this.

**Prevention**: the context is configured once at process start in `fking.platform`; anything needing different behaviour uses `decimal.localcontext()`.

---

## Data ingestion

### F-004 — Futures CSV header ingested as data {#f-004}
**Status**: verified-in-research (VF-016) · **Defended by**: [`../templates/data-source.md`](../templates/data-source.md)

**Symptom**: a one-bar gap at the start of every futures file, or a numeric column whose dtype is `object`.

**Mechanism**: futures kline CSVs have a header row; spot ones do not. A shared loader with `header=None` reads the header as a data row.

**Why it is hard to spot**: one bad bar per file per day is invisible in a summary and quietly present in every window that starts at a file boundary. The dtype variant is worse, because it makes downstream arithmetic silently wrong rather than absent.

**Prevention**: detect the header per file keyed on `(market, date)`, and assert the resulting dtypes after load.

### F-005 — Python-style booleans in spot trade archives {#f-005}
**Status**: verified-in-research · **Defended by**: [`../templates/data-source.md`](../templates/data-source.md)

**Symptom**: a JSON or CSV parse error on a spot trades file, mentioning `True` where `true` was expected.

**Mechanism**: spot trade files serialize booleans Python-style. This is loud and easy to fix; it is catalogued because it is the third of the three verified ingestion traps and someone will hit it, spend twenty minutes on it, and not write it down.

**Prevention**: an explicit boolean converter in the spot loader.

### F-006 — Truncated archive trusted without checksum {#f-006}
**Status**: class-known (VF-014) · **Defended by**: ingestion checksum verification

**Symptom**: a statistic changed and no code changed. Or a backtest window that used to have 43,200 bars now has 41,000 and nobody notices, because nobody asserts the count.

**Mechanism**: an interrupted download produces a short file that unzips and parses cleanly into a shorter series. Nothing raises. Every statistic computed from it is quietly different.

**Why it is the worst shape available**: silent, plausible, and it changes conclusions rather than crashing.

**Prevention**: every `data.binance.vision` archive has a `.zip.CHECKSUM` sibling. Verify before extract, unconditionally — not as an optional integrity mode.

---

## Exchange integration

### F-007 — Building spot user data on `listenKey` {#f-007}
**Status**: verified-in-research (VF-002)

**Symptom**: HTTP 410 Gone on `POST /api/v3/userDataStream`, on testnet and production alike.

**Mechanism**: the endpoint is retired for spot. Every tutorial, blog post, Stack Overflow answer and pre-2025 library describing spot user data is wrong.

**Cost when hit**: the 410 itself is instant and unambiguous. The expensive version is spending a day designing around the `listenKey` model before the first call is made.

**Prevention**: read [`../contexts/binance-testnet.md`](../contexts/binance-testnet.md) before writing spot user-data code. Do not re-test the 410 as a debugging step; it is not a flake.

### F-008 — HMAC key used for spot `session.logon` {#f-008}
**Status**: verified-in-research (VF-003)

**Symptom**: spot WebSocket API authentication fails with an apparently correct, working API key.

**Mechanism**: `session.logon` requires **Ed25519**. An HMAC-SHA256 key cannot perform it, and the signing scheme is different — Ed25519 over the canonical payload, not HMAC over a query string. The account therefore holds two credentials of different types, and config that treats "the API key" as one thing conflates them.

**Prevention**: model the two key types as distinct config entries with distinct types, so passing the wrong one is a type error rather than an auth failure.

### F-009 — Silent user-data stream death {#f-009}
**Status**: class-known · **Related**: [`./open-questions.md`](./open-questions.md) OQ-003

**Symptom**: user-data events stop arriving. No error, no disconnect. Orders still place successfully.

**Mechanism**: a futures `listenKey` expiring without keepalive, or a spot WebSocket-API session ending server-side. Position state goes stale while the system keeps trading against it — the worst available combination, because everything that would normally alert you still works.

**Detection**: a liveness watchdog. If no user-data event and no heartbeat has arrived within a bounded window, tear down and reconnect rather than waiting for an error that will not come.

**Prevention**: reconciliation on every reconnect makes an unnecessary reconnect cheap and a missed one survivable.

### F-010 — Testnet wipe mistaken for a bug {#f-010}
**Status**: verified-in-research (VF-005)

**Symptom**: authentication succeeds, requests succeed, and the account is simply empty with no open orders.

**Mechanism**: Binance spot testnet is wiped roughly every 30 days. Keys survive; balances and open orders do not.

**Why it wastes time**: the signature looks like a permissions problem or a wrong-account problem, and both are more familiar than "the exchange deleted everything on schedule".

**Prevention**: check for the wipe **before** debugging anything else with this signature. Reconciliation treats it as a normal event, not an incident — the exchange is the source of truth and local state converges to it.

### F-011 — Symbol universe assumed from production {#f-011}
**Status**: verified-in-research (VF-006)

**Symptom**: order rejected as an invalid symbol, on a symbol that plainly exists on Binance.

**Mechanism**: testnet is not a subset of production — spot testnet is missing 79 production symbols and futures is missing 189. The rejection reason talks about the symbol, so it sends you to check the symbol string rather than the environment.

**Prevention**: intersect the universe at startup and log the result. Never hardcode the counts; the durable fact is that the sets differ, not by how much.

### F-012 — Unicode symbol crashes the parser {#f-012}
**Status**: verified-in-research (VF-009)

**Symptom**: a crash parsing `exchangeInfo`, or a `UnicodeEncodeError` when logging a symbol — the latter especially on Windows, where the default encoding is not UTF-8.

**Mechanism**: a deliberate Unicode symbol exists in testnet `exchangeInfo`. It is there to break naive parsers.

**Prevention**: never gate symbol validity on `str.isalnum()` or `[A-Z0-9]+`; open files with an explicit `encoding="utf-8"`; sanitise before using a symbol as a path component. The correct behaviour is to parse it successfully and exclude it via the universe intersection, not to crash and not to special-case it by name.

### F-013 — Filter rejection treated as an edge case {#f-013}
**Status**: class-known · **Defended by**: [`../../docs/rules/exchange-integration.md`](../../docs/rules/exchange-integration.md)

**Symptom**: orders rejected for `minNotional` or lot-size violations, disproportionately on low-conviction signals.

**Mechanism**: tick size, step size and `minNotional` are per-symbol and are not suggestions. A quantity rounded the wrong way exceeds available balance; a small-conviction signal produces a notional below the floor. This is not an edge case — it is a routine outcome of correct sizing on a small account.

**Prevention**: quantities quantize `ROUND_DOWN` against step size; a rejected order is a first-class typed outcome that the risk engine handles, not an exception that aborts the cycle.

---

## Event bus and idempotency

### F-014 — Retried order placement double-fills {#f-014}
**Status**: class-known · **Defended by**: [`../../docs/rules/idempotency.md`](../../docs/rules/idempotency.md)

**Symptom**: the position is exactly twice the intended size after a network blip.

**Mechanism**: the placement request succeeded at the exchange and the response was lost. The retry created a second order. The client never saw the first acknowledgement, so from its perspective nothing happened.

**Prevention**: a deterministically derived `clientOrderId`, so the retry is the *same* order to the exchange and is rejected as a duplicate rather than accepted as a new one.

### F-015 — Non-idempotent consumer on at-least-once delivery {#f-015}
**Status**: class-known · **Defended by**: [`../../docs/rules/idempotency.md`](../../docs/rules/idempotency.md)

**Symptom**: a duplicated fill in the ledger, or a counter that is too high, after a consumer restart or an `XAUTOCLAIM`.

**Mechanism**: Redis Streams delivery is at-least-once. This is a **design constraint, not a discovery** — reclaiming a stuck message from a dead consumer's pending list makes duplicates the normal case, not the exceptional one. Any effect that is not naturally idempotent (appending a fill, incrementing, sending an order) doubles.

**Prevention**: an idempotency key derived from the event's semantic content, written to `processed_events` in the **same transaction** as the effect, with `ON CONFLICT DO NOTHING`.

**Test**: the replay harness feeds every consumed event twice and asserts byte-identical final state.

### F-016 — `XACK` before the transaction commits {#f-016}
**Status**: class-known · **Defended by**: [`../../docs/rules/idempotency.md`](../../docs/rules/idempotency.md)

**Symptom**: either an event whose effect never happened and which is never redelivered, or — if the ordering is inverted the other way and the dedupe row commits without the effect — an event that is permanently skipped.

**Mechanism**: acknowledging before committing means a crash in between loses the work with no redelivery. This is the one ordering mistake that turns at-least-once into at-most-once, silently.

**Prevention**: commit first, `XACK` second, always. The duplicate that this ordering can produce is handled by the dedupe table; the loss that the other ordering produces is not handled by anything.

---

## Validation and research

### F-017 — Look-ahead through full-sample statistics {#f-017}
**Status**: class-known · **Defended by**: [`../../docs/rules/no-lookahead.md`](../../docs/rules/no-lookahead.md), [`../contexts/backtest-pitfalls.md`](../contexts/backtest-pitfalls.md)

**Symptom**: an implausibly smooth equity curve, a Sharpe far above what the horizon can plausibly support, no losing months, and immediate death in forward testing.

**Mechanism**: normalising a feature with the mean and standard deviation of the whole series; computing an indicator on the full array and then slicing; using a bar's close to decide a trade executed at that same close; a label whose horizon overlaps the training window without a purge.

**Why it is the most dangerous class in the project**: it does not fail. It makes bad strategies look excellent, and every downstream process — validation, promotion, evolution — then works correctly on a false input.

**Prevention**: the feature store takes a non-optional `as_of` and physically cannot return rows with `available_at > as_of`; the adversarial `LookaheadProbe` perturbs future bars and asserts every past value and decision is byte-identical, and must fail closed.

### F-018 — Overfitting via an abandoned parameter grid {#f-018}
**Status**: class-known · **Defended by**: [`../../docs/rules/overfitting-defences.md`](../../docs/rules/overfitting-defences.md)

**Symptom**: a headline Sharpe that halves when the global trial count is applied, and a result that does not reproduce on a later window.

**Mechanism**: a 200-point grid is declared, 12 points are run, one looks good, and the search stops. Charging on execution charges 12 — but the selection pressure was applied at *specification*, because the remaining 188 would have run had the first 12 looked worse.

**Prevention**: charge at specification time, for the full declared grid; keep the counter global and monotone; re-check the `spec_hash` at test time, and void the result on a mismatch.

### F-019 — Survivorship in the symbol universe {#f-019}
**Status**: class-known · **Defended by**: [`../contexts/backtest-pitfalls.md`](../contexts/backtest-pitfalls.md)

**Symptom**: every symbol in the study has a long and successful history, and the strategy works on all of them.

**Mechanism**: the universe was built from today's listed symbols. In crypto this is severe rather than academic — delistings, dead tokens, collapsed exchanges and depegged quote assets remove exactly the losers, and the surviving set has been conditioned on success.

**Prevention**: a point-in-time symbol universe. If the listing history is not available for a market, that is an availability finding, not a detail to work around.

### F-020 — An edge inside the cost band {#f-020}
**Status**: class-known (VF-008, VF-017) · **Defended by**: [`../contexts/market-microstructure.md`](../contexts/market-microstructure.md)

**Symptom**: a strategy with a statistically solid few-basis-point edge loses money in live demo.

**Mechanism**: the edge was measured gross, or against a maker-fill assumption that cannot be evidenced without L2 data, or against costs calibrated on testnet where the spread is 7.5bp against production's 0.16bp.

**Prevention**: costs calibrated from production archives only; taker execution assumed unless a maker assumption is separately evidenced; the edge reported gross and net separately, so "real but uneconomic" is distinguishable from "not real" — a genuinely different and useful finding.

---

## Runtime and operations

### F-021 — Exception caught to keep the loop alive {#f-021}
**Status**: class-known · **Defended by**: [`../../docs/rules/error-handling.md`](../../docs/rules/error-handling.md)

**Symptom**: one error line in the logs, then normal-looking operation, then a position nobody can explain.

**Mechanism**: `except Exception: log.error(...); continue` converts a visible failure into silent wrong behaviour **with real positions open**. The system continues past a state it does not understand, and every subsequent decision is computed from that state.

**Prevention**: catch the specific exception you can actually handle; unexpected state trips the kill switch rather than continuing. The one permitted broad catch is the top-level supervisor, and it exists to flatten the book, write the audit record and exit non-zero — never to continue.

### F-022 — Quota exhaustion stalls instead of degrading {#f-022}
**Status**: class-known · **Defended by**: [`../../docs/rules/quota-management.md`](../../docs/rules/quota-management.md) · **Related**: OQ-001

**Symptom**: agents stop responding, retries pile up, and the system waits rather than doing the work it can still do deterministically.

**Mechanism**: an in-memory quota counter resets exactly when you are being rate limited and restarting; a retry loop that does not honour `Retry-After` retries into the same exhausted window; and exhaustion raises rather than returning a status.

**Why it is undertested**: the free-tier limits themselves are **unverified** (OQ-001, issue #19), so the degradation path is designed against numbers nobody has checked.

**Prevention**: a persistent quota ledger that survives restarts; reservation before the call and reconciliation after; exhaustion returns a `degraded` status object and the system continues deterministic-only.

### F-023 — Prompt text in the log stream {#f-023}
**Status**: class-known · **Defended by**: [`../../docs/rules/logging-rules.md`](../../docs/rules/logging-rules.md)

**Symptom**: log volume explodes, Loki queries time out, and the one investigation that needed the logs is the one that cannot run.

**Mechanism**: full LLM prompts and responses logged at INFO. Each is thousands of tokens, emitted many times per cycle.

**Prevention**: prompts and responses go to the append-only audit table; the log carries the audit row id. A size cap on log records is asserted in tests.

### F-024 — Audit row rewritten through the ORM {#f-024}
**Status**: class-known · **Defended by**: [`../../docs/rules/append-only-audit.md`](../../docs/rules/append-only-audit.md)

**Symptom**: an audit row whose content does not match what was written, or a hash-chain verification failure.

**Mechanism**: an audit log the application can rewrite is not an audit log. A well-meaning "backfill this missing field" migration is the usual vector, and it looks like data hygiene.

**Prevention**: enforcement in the database, not the ORM — `REVOKE UPDATE, DELETE` from the application role **plus** a trigger that raises, so a later migration granting a broad role does not silently reopen it. A per-row hash chain makes a superuser rewrite detectable rather than merely forbidden.

### F-025 — Backtest/live divergence {#f-025}
**Status**: class-known · **Defended by**: [`../../ARCHITECTURE.md`](../../ARCHITECTURE.md) §4

**Symptom**: identical strategy code behaves differently in backtest and in demo.

**Mechanism**: a clock read directly instead of injected; randomness without an injected seed; an I/O call inside strategy or risk logic; a code path that branches on venue type above the `ExecutionVenue` boundary.

**Why it is fatal rather than annoying**: if a strategy can behave differently in backtest than in demo, **every backtest result is unfalsifiable** — you can never distinguish "the strategy is bad" from "the harness differs".

**Prevention**: purity is mandatory in `strategy` and `risk` — no I/O, no clock access, no unseeded randomness. Only the venue implementation differs.

### F-026 — Unvalidated agent output acted upon {#f-026}
**Status**: class-known · **Defended by**: [`../../docs/rules/llm-output-handling.md`](../../docs/rules/llm-output-handling.md)

**Symptom**: a decision traced back to a model response that never conformed to its schema, or a field interpreted charitably by a lenient parser.

**Mechanism**: a permissive parse — regex-extracting a JSON block, coercing types, defaulting a missing field. Each individually looks like robustness. Together they mean the deterministic gate is validating something the model never actually said.

**Prevention**: parse into a Pydantic v2 model with `extra="forbid"` and strict types; an unparseable response is a failure, not something to interpret charitably; zero re-asks at runtime — the call fails, the raw response is audited, and the caller takes its deterministic path. A retry loop over a stochastic generator searches for output that passes validation, not output that is correct, and it suppresses the parse-failure rate that is the signal the prompt is broken. See [`../../docs/rules/llm-output-handling.md`](../../docs/rules/llm-output-handling.md).

---

## Adding an entry

Append with the next `F-NNN`, add a **symptom** row to the index — phrased as what you would actually observe, not as the cause — and give: status, the rule or document that defends against it, the mechanism, why it is hard to spot, how it is detected, and how it is prevented.

Promote `class-known` to `observed` when it happens, and link the post-mortem ([`../templates/post-mortem.md`](../templates/post-mortem.md)). The promotion is worth doing carefully: an entry that has actually bitten us carries more weight with the next reader than one that has not, and the difference should be visible.
