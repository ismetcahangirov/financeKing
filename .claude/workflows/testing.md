# Workflow — Testing

---

## 1. Pick the level

| Level | Use for | Infrastructure |
|---|---|---|
| Unit | Pure functions in `domain`, `strategy`, `risk` | None — these modules are pure by requirement |
| Property (Hypothesis) | **All risk and position math — mandatory** | None |
| Integration | Persistence, event bus, feature store | Real Postgres+TimescaleDB and Redis in service containers |
| Contract | Exchange adapters | Recorded real responses |
| Adversarial | Look-ahead, safety kernel, reconciliation | Whatever it takes |

---

## 2. Unit and property tests

Test behaviour, not implementation. A test that breaks on a private method rename is a liability — it makes refactoring expensive and trains people to delete tests.

For risk and position arithmetic, property tests are not a preference. Example-based tests confirm the cases you thought of; position arithmetic fails on the ones you did not — partial closes, direction flips, zero-crossings, dust quantities.

Properties this codebase needs:

- A fully closed position has **exactly zero** quantity, not `1E-18`
- Direction flips pass through flat
- PnL is invariant across any decomposition of the same fill sequence
- No sequence of valid signals produces exposure above the configured limit
- Applying a fill twice equals applying it once (idempotence is a requirement, not luck)
- Every `Decimal` result is quantized to the instrument's tick or lot precision

---

## 3. Integration tests

**Do not mock the database.** Real Postgres in a service container, via testcontainers. A mocked database proves the mock works — and the bugs here live in constraints, transaction boundaries, and the append-only triggers on audit tables, none of which a mock has.

Include a test that an `UPDATE` against an audit table is **rejected by the database**. If that test passes only because the application never issues one, it is testing nothing.

For the event bus: test the redelivery path explicitly. Redis Streams is at-least-once, so deliver the same message twice and assert the resulting state is identical.

---

## 4. Exchange contract tests

Mock the exchange, against **recorded real responses**. Hand-written fixtures encode what you assume the API returns, so tests pass while production fails.

```bash
ls tests/fixtures/recorded/
```

Record from testnet; store the raw payload unedited. Then add the hostile-input case: a truncated response, a missing field, a string where a number was expected, an error body with a 200 status. Exchange responses are hostile input and must never be indexed into optimistically.

Cover both user-data mechanisms — they are genuinely different and are modelled as such: futures `listenKey`, and spot's WebSocket `session.logon` handshake with Ed25519 keys, since `POST /api/v3/userDataStream` returns 410 Gone.

---

## 5. The adversarial tests that matter most here

**Look-ahead.** A test that actively attempts to leak future data into a feature and must **fail closed**. This is the highest-value test in the repository, because look-ahead does not fail on its own — it makes bad strategies look excellent. Run it in every suite, not on a schedule.

**Safety kernel.** 100% coverage, no exceptions. `guarded_client()` rejects a non-allowlisted host on **every request**, including when the base URL is overridden per call — a construction-time-only check is no check. Test fixtures may inject a *narrower* host set, never a broader one; a fixture that widens the allowlist re-creates the hole it is testing for.

**Parity.** The same strategy over the same data through `BacktestVenue` and `PaperVenue` produces identical signals and identical risk decisions.

**Timestamp normalization.** Spot data on or after 2025-01-01 parses as microseconds; spot before it and all futures as milliseconds; keyed on `(market, date)`, never a global constant.

**Testnet wipe recovery.** Simulate balances and open orders gone with keys still valid, and assert the system rebuilds its whole view from the exchange rather than trusting local state.

**Quota exhaustion.** Provider returns quota-exceeded; assert the system degrades to deterministic-only operation rather than stalling.

---

## 6. Determinism

Seed all randomness. Inject all clocks. Never assert against `datetime.now()`.

A flaky test in a trading system trains you to ignore failures, and one of those failures will be real. A flaky test has an unmodelled dependency — find it. Never add a retry or a `sleep`.

---

## 7. Coverage

```bash
make test ARGS="--cov=src/fking/<module> --cov-report=term-missing"
```

| Module | Floor |
|---|---|
| `platform/safety` | 100% |
| `risk` | 95% |
| `domain` | 95% |
| `execution` | 90% |
| everything else | 80% |

Per-module floors exist because one global number lets well-tested utilities subsidize untested risk logic. Check the module, never the total.

Coverage is a floor, not a goal. The uncovered lines that matter are rarely the ones listed first — they are the branches that only fire under partial fills, redelivery, reconnects, and rejected orders.

---

## 8. Before every PR

```bash
make check
```

Green, run now, output read. Not remembered green.
