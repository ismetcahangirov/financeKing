# Rule — Decimal and Money

## The rule

Every price, quantity, notional, fee, funding payment, balance and PnL is a `decimal.Decimal` constructed from a `str`, from the exchange's raw response text through to the `NUMERIC(38, 18)` column it lands in. `float` never appears in the same expression as money, and never enters the system through a JSON parse that has already turned digits into a binary double.

## Why

`Decimal(0.1)` is `Decimal('0.1000000000000000055511151231257827021181583404541015625')`. The error is 5.5e-18 and it is already baked in before your code runs, because the literal `0.1` was rounded to the nearest double by the parser. `Decimal("0.1")` is exactly one tenth. Same-looking constructor, different number.

Three failure modes follow, and none of them crash:

**Reconciliation drift.** Every fill carries a rounding residue. Positions are built by summing fills. After a few thousand fills the local position and the exchange's position differ in the fifteenth decimal, the reconciler declares a mismatch, and you spend a day reading `ccxt` source looking for an exchange bug that does not exist. `ARCHITECTURE.md` §7 makes exchange state the source of truth precisely so that reconciliation runs constantly — which means a float residue becomes a recurring alert rather than a one-off.

**Silent comparison failure.** `Decimal("0.1") == 0.1` is `False`. Not an error, not a warning — a risk limit check that quietly takes the wrong branch. Arithmetic between the two types raises `TypeError`, so `Decimal + float` is caught immediately; comparison is the hole, and comparison is exactly what limit checks are made of.

**Exchange rejection.** Binance enforces per-symbol step size and tick size filters. A quantity carrying float noise in the eighteenth decimal fails the filter, and the order is rejected in a hot path with a message about a value that looks correct when printed.

`CLAUDE.md` §2 states this as non-negotiable #1. This file is the mechanism.

## Incorrect

```python
from decimal import Decimal

import ccxt.async_support as ccxt


async def fetch_position_value(client: ccxt.Exchange, symbol: str) -> Decimal:
    ticker = await client.fetch_ticker(symbol)
    balance = await client.fetch_balance()

    price = ticker["last"]                          # float
    quantity = balance["BTC"]["free"]               # float
    notional = price * quantity                     # float * float
    fee = notional * 0.001                          # float, and a magic number

    return Decimal(notional - fee)                  # Decimal built from a float
```

At runtime this returns a `Decimal` that looks authoritative and carries a value like `Decimal('43127.899999999997817212715744972229003906250')`. It is stored, summed with other such values, compared against a drawdown limit, and written to an append-only audit table that can never be corrected. The `Decimal` annotation on the return type makes `mypy --strict` happy and makes a reviewer stop reading. Nothing in this function fails.

## Correct

```python
from decimal import ROUND_DOWN, ROUND_HALF_EVEN, Decimal
from typing import Final

TAKER_FEE_RATE: Final = Decimal("0.001")  # Binance futures testnet VIP0 taker, docs/adr/0009


def position_notional_quote(
    quote_price: Decimal,
    base_quantity: Decimal,
    fee_rate: Decimal = TAKER_FEE_RATE,
) -> Decimal:
    """Notional net of the taker fee, quantized for reporting.

    All inputs are exact decimals originating from the venue's raw response text.
    """
    notional_quote = quote_price * base_quantity
    fee_quote = notional_quote * fee_rate
    return (notional_quote - fee_quote).quantize(Decimal("0.00000001"), rounding=ROUND_HALF_EVEN)


def quantize_base_quantity(base_quantity: Decimal, step_size: Decimal) -> Decimal:
    """Snap a quantity down to the venue's step size.

    ROUND_DOWN truncates toward zero, so a short's magnitude also shrinks. ROUND_FLOOR
    would round -1.005 to -1.01 and increase the short by one step.
    """
    return base_quantity.quantize(step_size, rounding=ROUND_DOWN)
```

Money enters the process exactly once, in the venue adapter, and it enters from text:

```python
import json
from decimal import Decimal
from typing import Any


def parse_venue_payload(body: str) -> dict[str, Any]:
    """Decode an exchange response without ever materialising a float.

    json's `parse_float` hook receives the original source substring, not a parsed
    double, so `Decimal` is constructed from the exact characters the venue sent.
    """
    return json.loads(body, parse_float=Decimal)
```

Any value that has already passed through a `float` is not repairable — it must be re-parsed from the response text or discarded. Rounding cannot be undone by widening the type afterwards.

## Rounding mode by quantity kind

The mode is a risk decision, not a formatting preference.

| Quantity | Mode | Reason |
|---|---|---|
| Order `base_quantity` | `ROUND_DOWN` | Truncation toward zero can only ask for less than you can afford. `ROUND_HALF_UP` can round a quantity above free balance and produce an insufficient-margin rejection on exactly the fills that matter most — the large ones. |
| Order `quote_price` (buy) | `ROUND_DOWN` | Snapping a bid down to the tick never crosses further into the book than intended. |
| Order `quote_price` (sell) | `ROUND_UP` | Symmetric: snapping an ask up never crosses down into the book. |
| Reported `realised_pnl_usd`, fees, equity | `ROUND_HALF_EVEN` | Banker's rounding is unbiased over many roundings. `ROUND_HALF_UP` adds half a tick of expected value per rounded half, which across a year of per-fill PnL becomes a visible upward drift in reported performance — a systematic overstatement of edge in the number the evolution engine optimizes (`ARCHITECTURE.md` §10). |
| Risk limit thresholds | no quantization | Compare at full precision. Quantizing a threshold moves it, always in one direction, and always the wrong one. |

## Enforcement

**Process-wide decimal context**, set once at bootstrap in `src/fking/platform/numeric.py` and nowhere else. Decimal arithmetic — including addition — is rounded to context precision, so the precision is part of the money contract, not a detail:

```python
from decimal import ROUND_HALF_EVEN, Clamped, DivisionByZero, FloatOperation, InvalidOperation, Overflow, Underflow, getcontext


def configure_decimal_context() -> None:
    """Call exactly once, first thing in the process entrypoint.

    FloatOperation is the load-bearing trap: it turns `Decimal(0.1)` and
    `Decimal("0.1") < 0.1` — two failures that otherwise pass silently — into
    exceptions at the point of the mistake.

    It does not turn `Decimal("0.1") == 0.1` into one. CPython leaves equality
    comparisons and explicit conversions silent even when the signal is trapped,
    so that one answers False and says nothing; the AST check below is what keeps
    a float away from the comparison. Measured against CPython 3.12 while
    implementing this function (#110) — the earlier text here claimed otherwise.
    """
    context = getcontext()
    context.prec = 38
    context.rounding = ROUND_HALF_EVEN
    context.traps[FloatOperation] = True
    context.traps[InvalidOperation] = True
    context.traps[DivisionByZero] = True
    context.traps[Overflow] = True
    context.traps[Underflow] = True
    context.traps[Clamped] = True
```

`prec = 38` matches `NUMERIC(38, 18)`, so a value that is representable in the database is representable in memory and the round trip cannot lose digits. The trap set makes every ignorable numeric anomaly loud, per `CLAUDE.md` §4.

**AST check** at `tools/checks/money_types.py`, run by `make check`:

```python
"""Reject float annotations and float literals in money-typed code."""

from __future__ import annotations

import ast
import sys
from pathlib import Path

MONEY_TOKENS: frozenset[str] = frozenset(
    {"price", "quantity", "notional", "balance", "equity", "pnl",
     "fee", "commission", "margin", "collateral", "funding"}
)
MONEY_SUFFIXES: tuple[str, ...] = ("_usd", "_bps", "_quote", "_base")
FLOAT_FREE_PACKAGES: tuple[str, ...] = ("domain", "risk", "execution")


def is_money_name(name: str) -> bool:
    lowered = name.lower()
    return any(token in lowered for token in MONEY_TOKENS) or lowered.endswith(MONEY_SUFFIXES)


def annotation_mentions_float(node: ast.expr | None) -> bool:
    if node is None:
        return False
    return any(isinstance(sub, ast.Name) and sub.id == "float" for sub in ast.walk(node))


def check_file(path: Path, float_free: bool) -> list[str]:
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    failures: list[str] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name):
            if is_money_name(node.target.id) and annotation_mentions_float(node.annotation):
                failures.append(f"{path}:{node.lineno} money field '{node.target.id}' annotated float")
        elif isinstance(node, ast.arg):
            if is_money_name(node.arg) and annotation_mentions_float(node.annotation):
                failures.append(f"{path}:{node.lineno} money parameter '{node.arg}' annotated float")
        elif isinstance(node, ast.Call) and isinstance(node.func, ast.Name) and node.func.id == "float":
            if float_free:
                failures.append(f"{path}:{node.lineno} float() call in a float-free package")
        elif isinstance(node, ast.Constant) and isinstance(node.value, float):
            if float_free:
                failures.append(f"{path}:{node.lineno} float literal {node.value!r} in a float-free package")
    return failures


def main(root: Path) -> int:
    failures: list[str] = []
    for path in sorted(root.rglob("*.py")):
        package = path.relative_to(root).parts[0]
        failures.extend(check_file(path, float_free=package in FLOAT_FREE_PACKAGES))
    for failure in failures:
        print(failure, file=sys.stderr)
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(main(Path(sys.argv[1])))
```

```make
checks:
	uv run python tools/checks/money_types.py src/fking
	uv run python tools/checks/clock_isolation.py src/fking
	uv run python tools/checks/no_catch_safety.py src/fking
	uv run python tools/checks/naming.py src/fking

check: lint types imports checks test
```

**ruff**, in `pyproject.toml`:

```toml
[tool.ruff.lint]
select = ["E", "F", "B", "N", "UP", "RUF", "TRY", "DTZ", "BLE", "FURB", "PL", "SIM", "ANN"]
ignore = [
    # verbose-decimal-constructor rewrites Decimal("0") to Decimal(0); string
    # construction is mandatory here even where the value is integral, because the
    # spelling is what the reviewer and the AST check key on.
    "FURB157",
]

[tool.ruff.lint.flake8-annotations]
mangle-confusables = false
allow-star-arg-any = false
```

**mypy --strict** catches the arithmetic half for free: typeshed types `Decimal.__add__` and friends as accepting `Decimal | int`, so `quote_price * 1.05` is `Unsupported operand types for * ("Decimal" and "float")` at type-check time rather than `TypeError` at 03:00.

**Hypothesis round-trip property**, in `tests/domain/test_money_properties.py`:

```python
import json
from decimal import ROUND_DOWN, Decimal

from hypothesis import given
from hypothesis import strategies as st

money = st.decimals(
    min_value=Decimal("-1000000"),
    max_value=Decimal("1000000"),
    places=18,
    allow_nan=False,
    allow_infinity=False,
)
step = st.sampled_from([Decimal("1"), Decimal("0.1"), Decimal("0.001"), Decimal("0.00000001")])


@given(value=money)
def test_money_survives_the_json_wire_round_trip(value: Decimal) -> None:
    encoded = json.dumps({"realised_pnl_usd": str(value)})
    decoded = json.loads(encoded, parse_float=Decimal)
    assert Decimal(decoded["realised_pnl_usd"]) == value


@given(value=money, step_size=step)
def test_quantize_down_never_increases_magnitude(value: Decimal, step_size: Decimal) -> None:
    quantized = value.quantize(step_size, rounding=ROUND_DOWN)
    assert abs(quantized) <= abs(value)
    assert quantized.quantize(step_size, rounding=ROUND_DOWN) == quantized
```

The second property is the one that matters: quantization must be idempotent and must never hand the venue a quantity larger than the balance backing it.

**Schema**, in the Alembic migration:

```sql
CREATE TABLE fill (
    fill_id           UUID        PRIMARY KEY,
    order_id          UUID        NOT NULL REFERENCES "order" (order_id),
    event_time_utc    TIMESTAMPTZ NOT NULL,
    quote_price       NUMERIC(38, 18) NOT NULL CHECK (quote_price > 0),
    base_quantity     NUMERIC(38, 18) NOT NULL CHECK (base_quantity <> 0),
    fee_quote_usd     NUMERIC(38, 18) NOT NULL CHECK (fee_quote_usd >= 0),
    realised_pnl_usd  NUMERIC(38, 18) NOT NULL
);
```

`DOUBLE PRECISION` is banned in this schema, and the ban is tested rather than reviewed:

```python
MONEY_COLUMN_SUFFIXES = ("_price", "_quantity", "_usd", "_bps", "_pnl", "_fee")


def test_every_money_column_is_numeric_38_18(migrated_connection) -> None:
    rows = migrated_connection.execute(
        """
        SELECT table_name, column_name, data_type, numeric_precision, numeric_scale
        FROM information_schema.columns
        WHERE table_schema = 'public'
        """
    ).fetchall()

    offenders = [
        (table, column, data_type)
        for table, column, data_type, precision, scale in rows
        if column.endswith(MONEY_COLUMN_SUFFIXES)
        and (data_type != "numeric" or precision != 38 or scale != 18)
    ]
    assert offenders == []
```

It runs against real Postgres in a container, never a mock — `CLAUDE.md` §5.

**Pydantic v2** at the wire boundary, in `src/fking/platform/money.py`. The boundary models live in `platform`, `api` and `agents`; `domain` imports nothing but stdlib and therefore has no Pydantic types at all (see [`./module-boundaries.md`](./module-boundaries.md)):

```python
from decimal import Decimal
from typing import Annotated, Any

from pydantic import BaseModel, BeforeValidator, ConfigDict, PlainSerializer


def _reject_float(candidate: Any) -> Any:
    if isinstance(candidate, float):
        raise ValueError(
            "float is not an accepted money input; send the number as a JSON string"
        )
    return candidate


Money = Annotated[
    Decimal,
    BeforeValidator(_reject_float),
    PlainSerializer(str, return_type=str, when_used="json"),
]


class FillPayload(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    quote_price: Money
    base_quantity: Money
    fee_quote_usd: Money
```

`extra="forbid"` matters as much as the type: an exchange that adds a field is a change we want to see, not absorb. The `PlainSerializer` guarantees the JSON representation is a string in both directions, so a value cannot lose precision by being re-read by a consumer whose JSON parser has no `parse_float` hook.

## The one exception

`float` — specifically `numpy.float64` — is permitted inside statistical and machine-learning computation in `fking.backtest` and in feature math in `fking.data`. Sharpe ratios, covariance matrices, regression coefficients, indicator values and model inputs are estimates with sampling error many orders of magnitude larger than 2^-53; forcing `Decimal` through a NumPy pipeline buys nothing and costs a hundredfold in runtime.

The exception is bounded on all sides:

- The conversion happens at a named boundary function, one direction at a time — `Decimal` → `float` on the way into the computation, `Decimal(str(result))` on the way out — never implicitly mid-expression.
- It never touches a balance, a fill, an order quantity, a fee, or a PnL of record. A backtest may compute a Sharpe ratio in float64; the equity curve it is computed *from* is `Decimal`.
- It never crosses a module boundary as `float`. What leaves `backtest` or `data` for `risk`, `execution` or the database is `Decimal`.
- `tools/checks/money_types.py` excludes `backtest` and `data` from the float-literal ban but still enforces the money-name annotation ban there, so `sharpe: float` passes and `notional_usd: float` does not.

There is no exception for "just this once to compare against the reference implementation", and none for reading a `float` out of a Parquet column into a fill. See [`./naming.md`](./naming.md) for why a variable named `size` is what lets this exception leak in the first place, and `CLAUDE.md` §9 for the self-review question that catches it.
