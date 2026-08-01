---
description: Write or repair tests to this project's standard — behaviour-level, property-based for risk math, real Postgres, recorded exchange responses
argument-hint: <module or file>
allowed-tools: Read, Grep, Glob, Bash, Write, Edit
---

Test: $ARGUMENTS

## 1. Find the real gap

```bash
make test ARGS="--cov=src/fking/<module> --cov-report=term-missing"
```

Floors: `platform/safety` 100%, `risk` 95%, `domain` 95%, `execution` 90%, everything else 80%. Per-module floors exist because a single global number lets well-tested utilities subsidize untested risk logic — so check the module, never the total.

The uncovered lines that matter are rarely the ones the report lists first. They are the branches that only fire under partial fills, redelivery, reconnects, and rejected orders.

## 2. Test behaviour, not implementation

A test that breaks when you rename a private method is a liability — it makes refactoring expensive and trains people to delete tests. Assert on outputs and observable state transitions.

## 3. Property-based tests are mandatory for risk and position math

Example-based tests confirm the cases you thought of. Position arithmetic fails on the cases you did not.

```python
from hypothesis import given, strategies as st

@given(
    entry=st.decimals(min_value="1", max_value="100000", places=2),
    qty=st.decimals(min_value="0.00000001", max_value="1000", places=8),
    close_frac=st.decimals(min_value="0", max_value="1", places=4),
)
def test_partial_close_preserves_cost_basis(entry, qty, close_frac): ...
```

Properties worth asserting in this codebase:

- A fully closed position has **exactly zero** quantity, not `1E-18`.
- Direction flips go through flat; a long never becomes a short without passing zero.
- Realized + unrealized PnL is invariant across any decomposition of the same fill sequence.
- No sequence of valid signals produces exposure above the configured limit.
- Applying the same fill twice produces the same state as applying it once (bus consumers are idempotent by requirement, not by luck).
- Every `Decimal` result is quantized to the instrument's tick or lot precision.

## 4. What not to mock

**Do not mock the database.** Use the real Postgres+TimescaleDB in a service container via testcontainers. A mocked database proves the mock works, and the bugs here are in constraints, transaction boundaries, and the append-only triggers on audit tables — none of which a mock has.

**Do mock the exchange, against recorded real responses.** Hand-written fixtures encode what you assume the API returns, so tests pass while production fails. Record from testnet and store the raw payload:

```bash
ls tests/fixtures/recorded/
```

When adding a new endpoint, record it rather than writing it. Then verify the parser rejects a malformed variant — exchange responses are hostile input and must never be indexed into optimistically.

## 5. Determinism

Every test is deterministic. Seed all randomness, inject all clocks, never assert on `datetime.now()`. A flaky test in a trading system trains you to ignore failures, and one of those failures will be real.

```bash
make test ARGS="-p no:randomly --count=3" 2>/dev/null || make test
```

If a test is flaky, it has an unmodelled dependency — find it. Do not add a retry or a `sleep`.

## 6. The tests this project specifically needs

- **Leakage test**: an adversarial test that attempts to leak future data into a feature and must **fail closed**. This is the highest-value test in the repository, because look-ahead does not fail on its own — it makes bad strategies look excellent.
- **Parity test**: the same strategy over the same data through `BacktestVenue` and `PaperVenue` produces identical signals and identical risk decisions.
- **Safety kernel test**: `guarded_client()` rejects a non-allowlisted host on **every request**, including when the base URL is overridden per call. 100% coverage, no exceptions.
- **Timestamp normalization test**: spot data dated on or after 2025-01-01 parses as microseconds, spot before it and all futures as milliseconds, keyed on `(market, date)`.
- **Reconciliation test**: after a simulated testnet wipe (balances and open orders gone, keys valid), the system rebuilds its entire view from the exchange rather than trusting local state.

## 7. Verify

```bash
make check
```

## 8. Report

Coverage before and after against the module's floor, which properties were added, and — honestly — which uncovered branches remain and why they are acceptable.
