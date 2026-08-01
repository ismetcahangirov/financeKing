# Rule — Testing

## The rule

Seven clauses. All are enforced mechanically; none are advisory.

1. **Test behaviour, not implementation.** Assert on returned values and observable state transitions. A test that names a private method, patches an internal attribute, or counts calls to a collaborator is a liability — it makes refactoring expensive and trains people to delete tests instead of fixing them.
2. **Property-based tests with Hypothesis are mandatory for every function in `fking.risk` and for all position arithmetic in `fking.domain`.** Example-based tests confirm the cases you thought of. Position math fails on the ones you did not.
3. **Never mock the database.** Real PostgreSQL 16 + TimescaleDB through `testcontainers`, with Alembic migrations applied.
4. **Do mock the exchange — only against recorded real responses** captured by `scripts/record_exchange.py` into `tests/fixtures/recorded/`. Hand-written fixtures are banned outright.
5. **Coverage floors are per module** and enforced separately, never as one global number.
6. **Every test is deterministic.** Pinned seeds, injected `FrozenClock`, zero `sleep`, zero wall-clock assertions.
7. **Every test carries exactly one of the markers `unit`, `integration`, `property`, `slow`** (plus `slow` as an optional second). `--strict-markers` makes an unregistered marker a collection error.

## Why

The system's stated job is to reject bad strategies (`../../CLAUDE.md` §1). A test suite that passes on mocks rejects nothing — it proves the mocks agree with themselves.

Each clause exists because of a specific way this codebase lies to you:

- **Mocked database.** The interesting failures here live in `CHECK` constraints, transaction boundaries, `ON CONFLICT DO NOTHING` semantics for idempotent consumers (`./idempotency.md`), and the `BEFORE UPDATE OR DELETE` triggers that make audit tables append-only (`./append-only-audit.md`). A mock has none of those. The mock will happily let you `UPDATE` an audit row.
- **Hand-written fixtures.** They encode what you believe Binance returns. The two things that actually bite — a `Decimal` field arriving as a JSON string with 8 decimal places, and a symbol containing a non-ASCII code point that testnet deliberately serves in `exchangeInfo` — are exactly the things you would never hand-write. See `./exchange-integration.md`.
- **A single global coverage number.** `platform/config` is trivially coverable and large. It will subsidize `risk/sizing.py` forever, and the aggregate will read 87% while the kill switch has three untested branches.

| Module | Floor | Why this number |
|---|---|---|
| `platform/safety` | 100% | Every uncovered line in the safety kernel is a line that might widen the allowlist. There is no acceptable uncovered branch (`../../CLAUDE.md` §0). |
| `risk` | 95% | Risk has veto authority over every order. An untested branch here is an unbounded loss with a green build. |
| `domain` | 95% | Cheap to cover — pure functions, no I/O. A gap here means someone wrote a domain object with a hidden dependency. |
| `execution` | 90% | The reconnect, partial-fill and rejection branches are the ones that go uncovered, and they are the ones that fire at 03:00. 90% rather than 95% because venue-error enumeration has genuinely unreachable arms. |
| everything else | 80% | A floor low enough that nobody games it and high enough that a new module cannot arrive naked. |

- **Non-determinism.** A flaky test in a trading system teaches you to re-run CI rather than read the failure. One of those failures will be a real reconciliation bug that only manifests under a particular event ordering.

## Incorrect

```python
# tests/test_position.py
from unittest.mock import MagicMock, patch

def test_close_position() -> None:
    repo = MagicMock()
    engine = PositionEngine(repo=repo, clock=datetime.now)
    engine.open("BTCUSDT", 0.5, 50000.0)
    engine.close("BTCUSDT", 0.5, 51000.0)
    repo.save.assert_called_once()
    assert engine._positions["BTCUSDT"].quantity == 0.0
```

Five defects, and the test passes:

`0.5` and `50000.0` are `float`, so this never exercises the `Decimal` path the production code takes and cannot detect the accumulation drift `Decimal` exists to prevent. `repo` is a `MagicMock`, so `save` returning a constraint violation is untested and `assert_called_once` couples the test to the current number of writes — split one write into two and a correct refactor goes red. `engine._positions` is private, so renaming it breaks the test without breaking anything. `clock=datetime.now` makes the fill timestamps unreproducible. And the assertion `quantity == 0.0` is satisfied by `-1e-18`, which is the actual bug: a residual dust quantity that later fails a `LOT_SIZE` filter with `-1013` and looks like an exchange fault.

## Correct

```python
# tests/property/test_position_properties.py
from __future__ import annotations

from decimal import ROUND_HALF_EVEN, Decimal

import pytest
from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st

from fking.domain.fill import Fill, Side
from fking.domain.instrument import Instrument
from fking.domain.position import Direction, Position, PositionTransition, apply_fill

pytestmark = [pytest.mark.property, pytest.mark.unit]

# BTCUSDT spot filters, captured from tests/fixtures/recorded/binance-spot-testnet/exchange_info/
BTCUSDT = Instrument(
    symbol="BTCUSDT",
    lot_step=Decimal("0.00001"),
    tick_size=Decimal("0.01"),
    min_notional=Decimal("10.00"),
)


def stepped(step: Decimal, *, max_value: str) -> st.SearchStrategy[Decimal]:
    """Decimals that are exact multiples of an exchange step size.

    `places` alone is insufficient: Binance step sizes are not always powers of
    ten (0.005 occurs), so the generated value is snapped onto the step lattice.
    Anything off the lattice is rejected by the venue with -1013 and would make
    the property test assert against inputs the exchange cannot produce.
    """
    places = -step.as_tuple().exponent
    return (
        st.decimals(
            min_value="0",
            max_value=max_value,
            places=places,
            allow_nan=False,
            allow_infinity=False,
        )
        .map(lambda d: (d / step).quantize(Decimal("1"), rounding=ROUND_HALF_EVEN) * step)
        .filter(lambda d: d > 0)
    )


fills = st.builds(
    Fill,
    side=st.sampled_from(Side),
    quantity=stepped(BTCUSDT.lot_step, max_value="5"),
    price=stepped(BTCUSDT.tick_size, max_value="150000"),
    fee_quote=st.just(Decimal("0")),
)


@given(sequence=st.lists(fills, min_size=1, max_size=12))
@settings(max_examples=1000, deadline=None, suppress_health_check=[HealthCheck.too_slow])
def test_position_arithmetic_invariants(sequence: list[Fill]) -> None:
    position = Position.flat(BTCUSDT)
    realized_total = Decimal("0")

    for fill in sequence:
        before = position
        transition: PositionTransition = apply_fill(before, fill)
        position = transition.after
        realized_total += transition.realized_pnl_quote

        # 1. Dust: quantity is always an exact multiple of the lot step. A residual
        #    1E-18 passes `== 0` for a float and is rejected by the venue as -1013.
        assert position.quantity % BTCUSDT.lot_step == 0

        # 2. Zero-crossing: flat is exactly zero and is a distinct state, never a
        #    tiny long. `Direction.FLAT` and a non-zero quantity is unrepresentable.
        assert (position.quantity == 0) == (position.direction is Direction.FLAT)

        # 3. Direction flip: a long never becomes a short without the transition
        #    reporting the flat crossing, because the close and the open are two
        #    separate realized-PnL events and netting them loses one of them.
        flipped = (
            before.direction is not Direction.FLAT
            and position.direction is not Direction.FLAT
            and before.direction is not position.direction
        )
        assert flipped == transition.crossed_flat
        if flipped:
            assert transition.closed_quantity == before.quantity
            assert transition.opened_quantity == fill.quantity - before.quantity

        # 4. Partial close preserves cost basis. Recomputing entry price on a close
        #    is the classic bug: it silently rewrites the basis of the remainder.
        partial_close = (
            transition.closed_quantity > 0
            and not transition.crossed_flat
            and position.direction is before.direction
        )
        if partial_close:
            assert position.entry_price == before.entry_price

    # 5. Path independence of realized PnL: the running sum equals the position's
    #    own accumulator regardless of how the sequence decomposed.
    assert realized_total == position.realized_pnl_quote
```

Database-touching tests take the real thing:

```python
# tests/integration/test_audit_append_only.py
import pytest
from psycopg.errors import RaiseException

pytestmark = pytest.mark.integration


def test_audit_rows_cannot_be_updated(pg_session) -> None:
    row_id = insert_agent_call(pg_session, correlation_id="c-1", model="gemini-2.5-flash")
    with pytest.raises(RaiseException, match="agent_calls is append-only"):
        pg_session.execute(
            "UPDATE agent_calls SET response_text = 'edited' WHERE id = %s", (row_id,)
        )
```

`pg_session` comes from a session-scoped `PostgresContainer("timescale/timescaledb:2.17.2-pg16")` fixture with `alembic upgrade head` applied once and a per-test transaction rolled back at teardown. The trigger under test is defined in `./append-only-audit.md`; a mock would have reported success.

## Enforcement

**`pyproject.toml`** — markers, strict config, pinned seed:

```toml
[tool.pytest.ini_options]
minversion = "8.0"
testpaths = ["tests"]
addopts = [
  "--strict-markers",
  "--strict-config",
  "--randomly-seed=20260801",
  "--cov=src/fking",
  "--cov-report=term-missing:skip-covered",
  "--cov-report=xml",
]
markers = [
  "unit: in-process, no container, no network, under 100ms",
  "integration: requires a testcontainer (Postgres/TimescaleDB or Redis)",
  "property: Hypothesis-driven; profile selected by HYPOTHESIS_PROFILE",
  "slow: over 5s wall clock; excluded from the local default profile",
]
filterwarnings = ["error"]
xfail_strict = true
asyncio_mode = "strict"

[tool.coverage.run]
branch = true
parallel = true
source = ["src/fking"]

[tool.coverage.report]
show_missing = true
skip_covered = true
exclude_also = [
  "if TYPE_CHECKING:",
  "@overload",
  "class .*\\(Protocol\\):",
]
```

**Per-module floors.** `coverage.py` has one `fail_under`, so the floors are enforced as separate report passes over one combined data file. In the `Makefile`:

```make
COVERAGE_FLOORS := platform/safety:100 risk:95 domain:95 execution:90
CORE_MODULES    := src/fking/platform/safety/*,src/fking/risk/*,src/fking/domain/*,src/fking/execution/*

cover:
	uv run coverage combine
	@for spec in $(COVERAGE_FLOORS); do \
	  mod=$${spec%%:*}; floor=$${spec##*:}; \
	  echo "== $$mod (floor $$floor%)"; \
	  uv run coverage report --include="src/fking/$$mod/*" --fail-under=$$floor \
	    || { echo "FLOOR BREACH: src/fking/$$mod below $$floor%"; exit 1; }; \
	done
	@echo "== everything else (floor 80%)"
	uv run coverage report --omit="$(CORE_MODULES)" --fail-under=80
```

`make check` runs `test` then `cover`. A PR that raises the aggregate while dropping `risk` from 96% to 94% fails, which is the entire point.

**Seed pinning.** `pytest-randomly` shuffles test order and reseeds `random` and `numpy.random` per test; the seed is pinned in `addopts` so a shuffle-order-dependent failure reproduces exactly from the CI log. Hypothesis gets explicit profiles in `tests/conftest.py`:

```python
import os
from datetime import timedelta

from hypothesis import HealthCheck, Verbosity, settings

settings.register_profile(
    "dev", max_examples=100, deadline=timedelta(milliseconds=500), print_blob=True
)
settings.register_profile(
    "ci",
    max_examples=1000,
    deadline=None,
    derandomize=True,      # a PR gate that fails on a new random draw is a flaky gate
    database=None,         # no .hypothesis cache in CI: the run must be self-contained
    print_blob=True,
    suppress_health_check=[HealthCheck.function_scoped_fixture],
)
settings.register_profile(
    "nightly", max_examples=20_000, deadline=None, derandomize=False, verbosity=Verbosity.verbose
)
settings.load_profile(os.environ.get("HYPOTHESIS_PROFILE", "dev"))
```

`derandomize=True` on the PR gate and `False` nightly is deliberate: the gate must be reproducible, and the search for genuinely new counterexamples belongs in a job whose failure opens an issue rather than blocking a merge.

**Risk modules must have a property test.** `scripts/check_property_coverage.py`, run as a CI step and by `make check`:

```python
#!/usr/bin/env python
"""Fail if any module under src/fking/risk lacks a property test with @given."""
from __future__ import annotations

import sys
from pathlib import Path

SRC = Path("src/fking/risk")
TESTS = Path("tests/property")


def main() -> int:
    problems: list[str] = []
    for module in sorted(SRC.rglob("*.py")):
        if module.stem.startswith("_"):
            continue
        expected = TESTS / f"test_{module.stem}_properties.py"
        if not expected.exists():
            problems.append(f"{module}: expected {expected}, which does not exist")
        elif "@given(" not in expected.read_text(encoding="utf-8"):
            problems.append(f"{expected}: exists but contains no @given")
    if problems:
        print("Property-test gate failed:", *problems, sep="\n  ", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
```

Naming convention over import analysis, on purpose: an import-graph check is defeated by a test that imports the module and never exercises it, and a human reading `tests/property/` should be able to see the gap without running anything.

**Recorded fixtures.** `scripts/record_exchange.py` drives a real testnet session through `guarded_client()` and writes YAML — YAML rather than JSON specifically so a hand edit can carry a `#` comment, which is what the exception below depends on:

```yaml
# tests/fixtures/recorded/binance-spot-testnet/create_order/2026-07-14T09-02-11Z.yaml
_recording:
  venue: binance-spot-testnet
  endpoint: POST /api/v3/order
  captured_at: "2026-07-14T09:02:11.418293Z"
  ccxt_version: "4.5.70"
  sha256: "3f1a...c9"          # over the canonical serialization of `response`
response:
  symbol: "BTCUSDT"
  orderId: "9124773"
  clientOrderId: "fk7c1d9a3e5b02"
  transactTime: "1752483731418"
  price: "0.00000000"
  origQty: "0.00050000"
  status: "FILLED"
```

`tests/test_fixture_integrity.py` walks every file under `tests/fixtures/recorded/`, recomputes the digest, and requires either a match or the exception below. It also asserts the fixture set contains at least one symbol with a non-ASCII code point, because testnet `exchangeInfo` deliberately serves one and a parser that has never seen it is a parser that will crash on it (`./exchange-integration.md`).

**ruff** carries the rest: `PT` (flake8-pytest-style) forbids bare `assert` misuse and enforces fixture form, `ARG` catches fixtures requested and never used, `S101` stays disabled under `tests/` only.

## Test marker taxonomy and what CI runs

| Marker | Means | Container | Budget |
|---|---|---|---|
| `unit` | Pure, in-process, no I/O of any kind | none | < 100 ms each |
| `integration` | Real Postgres/TimescaleDB or Redis via `testcontainers` | yes | < 5 s each |
| `property` | Hypothesis-driven; combines with `unit` or `integration` | either | profile-dependent |
| `slow` | Over 5 s; walk-forward runs, full-archive scans, the `LookaheadProbe` (`./no-lookahead.md`), the nightly injection probe (`./llm-output-handling.md`) | either | unbounded |

| Job | Selection | Profile | Blocks merge |
|---|---|---|---|
| local `make test` | `-m "not slow"` | `dev` | no |
| PR gate `make check` | all markers | `ci` | **yes** — plus `make cover`, `lint-imports`, `mypy --strict` |
| nightly | all markers | `nightly` | no — opens a `needs-human` issue |
| nightly re-record | `scripts/record_exchange.py --diff` | n/a | no — opens an issue if any response *shape* changed |

The nightly re-record is how you find out Binance changed a payload, and it is also how you find out testnet was wiped: fixtures still parse, but balances come back empty and open orders are gone. That signal is worth more than the test run around it.

## The one exception

**A recorded response may be hand-edited to synthesize an error path the exchange will not produce on demand.** You cannot ask testnet for `-1021 Timestamp for this request is outside of the recvWindow`, a partial fill that stalls mid-book, or a `410 Gone` on a futures endpoint. Those paths still need tests.

The edited file must:

1. Be derived from a real recording — you edit a captured response, you do not author one.
2. Carry a `# SYNTHETIC:` comment naming the reason and the exact source recording it came from.
3. Keep the original `sha256` under `_recording.derived_from_sha256` so the provenance chain survives.

```yaml
# tests/fixtures/recorded/binance-spot-testnet/create_order/synthetic-recv-window.yaml
# SYNTHETIC: -1021 recvWindow rejection. Testnet will not emit this on demand without
# deliberately skewing the client clock, which is not reproducible in CI. Derived from
# create_order/2026-07-14T09-02-11Z.yaml (sha256 3f1a...c9) by replacing the success
# body with the documented error envelope and preserving the transport-level shape.
_recording:
  venue: binance-spot-testnet
  endpoint: POST /api/v3/order
  synthetic: true
  derived_from: "create_order/2026-07-14T09-02-11Z.yaml"
  derived_from_sha256: "3f1a...c9"
response:
  code: "-1021"
  msg: "Timestamp for this request is outside of the recvWindow."
```

`tests/test_fixture_integrity.py` enforces all three clauses: a fixture whose digest does not match and which lacks a `# SYNTHETIC:` line, a `synthetic: true` flag, and a resolvable `derived_from` fails the suite. There is no fourth allowance — in particular, "the recording was stale so I updated the numbers by hand" is not a synthetic fixture, it is a hand-written fixture, and you re-record instead.
