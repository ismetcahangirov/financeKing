# Rule — Naming

## The rule

Every name states its unit and its intent. A quantity carries the unit in its suffix (`_usd`, `_bps`, `_seconds`, `_ms`, `_utc`, `_pct`), a boolean carries `is_`/`has_`/`should_`, and the ambiguous trading words — `size`, `price`, `amount`, `qty`, `pnl`, `fee`, `time`, `timeout`, `slippage` — are banned identifiers enforced by a denylist check. Database columns mirror the Python field name character for character, suffix included.

## Why

`size` is not vague, it is *dangerous*. In a trading system it plausibly means base quantity, quote notional, contract count, or position value in USD — four numbers that differ by four or five orders of magnitude for BTC. A function that accepts `size: Decimal` and is called with a notional where it expected a base quantity produces an order 60,000 times too large, and every type annotation in the chain is correct. `mypy --strict` cannot help: both are `Decimal`. The property tests cannot help: both are valid `Decimal`s. The risk engine's exposure limit catches it — which is the system working — but only after the mistake has been made, and only if the limit is expressed in the same unit as the mistake.

The percentage case is the one that has cost other people real money. `max_drawdown_pct = 0.02` reads as 2% to the author and as 0.02% to the next reader, and both readings produce working code — one halts trading at a 2% drawdown, the other halts immediately and permanently. Neither raises. The convention that `_pct` is 0–100 and a 0–1 ratio is `_fraction` or `_ratio` removes the ambiguity from the name, which is the only place it can be removed, because the value cannot carry it.

The third reason is the one specific to this codebase. `CLAUDE.md` §4 notes that this system is written mostly by AI across sessions with no shared memory. Names are the only context an agent has when it opens a file in a fresh session. `price` in a file it has not read tells it nothing; `decision_price` tells it this is the price the signal was formed at, not the price the fill happened at, and that the difference between them is slippage — the entire measurement `ARCHITECTURE.md` §11 requires for every trade.

## Banned names and their replacements

Every identifier on the left fails `tools/checks/naming.py` in `src/fking/`.

| Banned | Use | Why the banned form is ambiguous |
|---|---|---|
| `size` | `base_quantity`, `notional_usd`, `contract_count` | Four different numbers, orders of magnitude apart. |
| `amount`, `qty` | `base_quantity`, `quote_amount_usd` | Same ambiguity, plus `amount` means quote on some venues and base on others. |
| `price` | `quote_price`, `mark_price`, `index_price`, `decision_price`, `fill_price` | Mark vs index vs last vs decision drives liquidation, PnL and slippage differently. |
| `pnl` | `realised_pnl_usd`, `unrealised_pnl_usd` | Realised and unrealised are different claims about the world; summing them is a bug. |
| `fee` | `fee_quote_usd`, `funding_fee_usd`, `fee_rate_bps` | A rate and a charge are not the same quantity and differ by a factor of the notional. |
| `slippage` | `slippage_bps`, `slippage_quote_usd` | bps or currency; the cost model calibration in `BACKTEST_ENGINE.md` needs bps. |
| `timeout` | `timeout_seconds` | Seconds or milliseconds — a 1000x error that looks like a hang or like no timeout at all. |
| `time`, `timestamp`, `ts` | `event_time_utc`, `available_at_utc`, `decided_at_utc`, `received_at_utc` | Event time vs ingestion time vs decision time is the difference between a point-in-time feature and look-ahead. |
| `value` | the thing it actually is | Carries no information whatsoever. |
| `data`, `info`, `result`, `obj`, `tmp` | the thing it actually is | Same. |
| `count` (bare) | `fill_count`, `retry_count`, `trial_count` | Counts of different things get summed. |
| `limit` (bare) | `exposure_limit_usd`, `rate_limit_per_minute`, `row_limit` | Risk limit, API limit and SQL limit in one word. |
| `balance` (bare) | `free_balance_usd`, `total_balance_usd`, `margin_balance_usd` | Free vs total is the difference between a valid order and a rejection. |
| `ret`, `pct_change` | `return_fraction`, `return_bps` | See the `_pct` rule below. |

## Suffix conventions

| Suffix | Means | Type |
|---|---|---|
| `_usd` | US dollars, or a USD-pegged stablecoin quote treated as 1:1 for reporting | `Decimal` |
| `_bps` | basis points, 1 bp = 0.0001 | `Decimal` |
| `_pct` | percent, 0–100 | `Decimal` |
| `_fraction`, `_ratio` | dimensionless, 0–1 | `Decimal` |
| `_seconds` | seconds | `float` for durations, `int` for whole-second config |
| `_ms` | milliseconds, only where a venue's own units are being carried unconverted | `int` |
| `_utc` | an aware UTC `datetime` | `datetime` |
| `_count`, `_id`, `_ids` | cardinal, identifier, collection of identifiers | `int`, `UUID`/`str`, `frozenset` |

Three rules about these:

**A bare ratio is never `_pct`.** `0.02` in a `_pct` field is 0.02%, not 2%, and the check treats a `_pct` field whose literal default is below 1 as a failure worth a manual look.

**`_usd` asserts an assumption.** Binance USDT pairs are quoted in USDT, not dollars. Treating them 1:1 is correct for reporting and wrong during a depeg, and that assumption is stated once, in the docstring of the reporting module, rather than being implied silently by every field name. If a name says `_usd`, someone has decided the peg holds; make sure that decision is written down where it can be revisited.

**`_ms` is for carrying, not computing.** A venue epoch arrives in milliseconds and is named `open_time_ms` for exactly as long as it takes to reach `epoch_to_utc`, after which it is `open_time_utc` and the `_ms` name does not exist. Arithmetic on `_ms` values inside the system is how the microsecond/millisecond split in [`./time-and-timezones.md`](./time-and-timezones.md) escapes the ingestion layer.

## Incorrect

```python
def calc(price: float, size: float, fee: float, timeout: int = 30) -> float:
    """Calculate the result."""
    pnl = (price - 42000) * size - fee
    return pnl


async def check(pos, limit, t):
    if pos.value > limit:
        await close(pos, timeout=t)
```

Nothing here is answerable without opening another file. Is `price` the mark, the last trade, or the price the decision was made at? Is `size` base or notional? Is `fee` a rate or a charge — if a rate, the subtraction is off by the notional; if a charge, correct. Is `timeout` 30 seconds or 30 milliseconds? Is `42000` an entry price, a strike, or a threshold someone typed once? Is `limit` in USD or a fraction of equity? The docstring restates the function name and `CLAUDE.md` §13 is explicit that documentation which restates the obvious is worse than none.

Every one of these compiles, type-checks under `mypy --strict` once annotated, and passes tests written by the same person who wrote the function — because that person holds the missing context in their head, for about a week.

## Correct

```python
from datetime import datetime
from decimal import Decimal
from typing import Final

BTC_ENTRY_REFERENCE_USD: Final = Decimal("42000")  # 2026-Q1 cohort entry, docs/adr/0021


def realised_pnl_usd(
    fill_price: Decimal,
    base_quantity: Decimal,
    fee_quote_usd: Decimal,
    entry_price: Decimal = BTC_ENTRY_REFERENCE_USD,
) -> Decimal:
    """Realised PnL on a closed base quantity, net of the fee actually charged.

    `fee_quote_usd` is a charge, not a rate; multiply a rate by notional before calling.
    """
    return (fill_price - entry_price) * base_quantity - fee_quote_usd


async def enforce_exposure_limit(
    position: Position,
    exposure_limit_usd: Decimal,
    timeout_seconds: float,
    clock: Clock,
) -> None:
    if position.notional_usd > exposure_limit_usd:
        await flatten(position, timeout_seconds=timeout_seconds, decided_at_utc=clock.now())
```

The signature now answers the questions the previous one raised, and the docstring carries the one thing a reader could not have guessed — that `fee_quote_usd` is a charge rather than a rate.

**Event bus streams** are `fking.<module>.<noun>.<verb>`, verb in the past tense because an event is a fact that already happened:

```
fking.data.bar.ingested
fking.strategy.signal.emitted
fking.risk.order.approved
fking.risk.order.rejected
fking.execution.order.submitted
fking.execution.fill.received
fking.evolution.strategy.retired
```

The module segment makes the producer readable from a Grafana panel with no lookup, and it makes the boundary crossings of `ARCHITECTURE.md` §3 visible as a naming pattern — `fking.strategy.order.submitted` is a name that could not legitimately exist, and is obvious as such in a stream listing.

**Database columns mirror the Python field name exactly**, unit suffix included:

```sql
CREATE TABLE fill (
    fill_id           UUID            PRIMARY KEY,
    event_time_utc    TIMESTAMPTZ     NOT NULL,
    fill_price        NUMERIC(38, 18) NOT NULL,
    base_quantity     NUMERIC(38, 18) NOT NULL,
    fee_quote_usd     NUMERIC(38, 18) NOT NULL,
    realised_pnl_usd  NUMERIC(38, 18) NOT NULL
);
```

Not `price`, not `qty`, not `amount`. A row can then be splatted into the dataclass with no translation layer, so a rename becomes a migration that fails loudly rather than a mapping dictionary that silently pairs `qty` with `notional_usd`. It also makes the column-type assertions in [`./decimal-and-money.md`](./decimal-and-money.md) and [`./time-and-timezones.md`](./time-and-timezones.md) possible at all: those tests key on the suffix, so a column named `price` is invisible to the check that would have caught it being `DOUBLE PRECISION`.

**Test names state the behaviour, not the method under test.** `TESTING.md` expands this; the short form:

```python
def test_with_fill_flipping_direction_realises_pnl_only_on_the_closed_portion() -> None: ...
def test_naive_event_time_is_rejected_at_construction() -> None: ...
def test_unknown_venue_code_classifies_as_permanent() -> None: ...
```

not `test_with_fill`, `test_init`, `test_parse_2`. A failing test name should tell you what broke before you read a line of the diff, and a test named after a method is a test that must be renamed when the method is (`CLAUDE.md` §5: a test that breaks on a rename is a liability).

## Enforcement

**ruff `N`** (pep8-naming) for the mechanical layer — `N801` class `CapWords`, `N802`/`N803` function and argument `snake_case`, `N806` no `CAPS` for locals, `N815` no `mixedCase` in class scope, `N818` error classes end in `Error`:

```toml
[tool.ruff.lint]
select = ["E", "F", "B", "N", "UP", "RUF", "TRY", "DTZ", "BLE", "FURB", "PL", "SIM", "ANN"]
```

`N` enforces casing. It has no opinion about meaning, which is the whole problem — `size` is impeccable `snake_case`.

**Denylist AST check** at `tools/checks/naming.py`, run over `src/fking/` by the `checks` target described in [`./decimal-and-money.md`](./decimal-and-money.md):

```python
"""Reject ambiguous identifiers. The table in .claude/rules/naming.md is the source of truth."""

from __future__ import annotations

import ast
import sys
from pathlib import Path

BANNED: frozenset[str] = frozenset(
    {"size", "amount", "qty", "price", "pnl", "fee", "slippage", "timeout", "time",
     "timestamp", "ts", "value", "data", "info", "result", "obj", "tmp", "count",
     "limit", "balance", "ret", "pct_change"}
)
UNIT_SUFFIXES: tuple[str, ...] = (
    "_usd", "_bps", "_pct", "_fraction", "_ratio", "_seconds", "_ms", "_utc", "_count", "_id", "_ids"
)
MATH_ESCAPE = "# fking: allow-math-symbols"


def bound_names(tree: ast.AST) -> list[tuple[str, int]]:
    found: list[tuple[str, int]] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.arg):
            found.append((node.arg, node.lineno))
        elif isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name):
            found.append((node.target.id, node.lineno))
        elif isinstance(node, ast.Assign):
            found.extend(
                (target.id, node.lineno) for target in node.targets if isinstance(target, ast.Name)
            )
        elif isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef):
            found.append((node.name, node.lineno))
    return found


def main(root: Path) -> int:
    failures: list[str] = []
    for path in sorted(root.rglob("*.py")):
        source = path.read_text(encoding="utf-8")
        if MATH_ESCAPE in source:
            continue
        for name, lineno in bound_names(ast.parse(source, filename=str(path))):
            if name in BANNED:
                failures.append(f"{path}:{lineno} ambiguous identifier '{name}' — see .claude/rules/naming.md")
            elif name.endswith("_pct") and "fraction" in name:
                failures.append(f"{path}:{lineno} '{name}' claims both percent and fraction")
    for failure in failures:
        print(failure, file=sys.stderr)
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(main(Path(sys.argv[1])))
```

The check runs on `src/fking/` only. Tests may bind `price` in a fixture body without harm, and extending the ban there produces noise that gets the check disabled.

**Code review.** `CODE_REVIEW.md` carries the naming items as blocking, because the two things the check cannot do are judge whether a name is *accurate* — `mark_price` holding the last trade price passes every mechanical check — and catch a name that is merely uninformative without being on the list. A reviewer who cannot state the unit of every numeric field in the diff from its name alone should block the merge and say which field.

## The one exception

Conventional single-letter symbols inside a clearly documented mathematical formula, where the docstring defines every symbol and the function is a transcription of a published expression.

```python
# fking: allow-math-symbols
from decimal import Decimal


def deflated_sharpe_ratio(sr: Decimal, n: int, t: int, g3: Decimal, g4: Decimal, v: Decimal) -> Decimal:
    """Bailey & Lopez de Prado deflated Sharpe ratio.

    Symbols follow the source paper so the transcription is checkable against it:
      sr  observed Sharpe ratio, per-observation
      n   number of independent trials charged against the global counter
      t   number of observations in the sample
      g3  skewness of the return series
      g4  kurtosis of the return series
      v   variance of Sharpe ratios across the n trials

    Callers pass named arguments; nothing outside this function uses these names.
    """
```

The exception is narrow on four axes and all four must hold:

1. The module carries the `# fking: allow-math-symbols` marker, which is grep-able and shows up in review as a deliberate act.
2. The docstring defines every symbol, in a block, next to the citation.
3. The symbols are confined to the function body and its parameters. The moment a value leaves, it is renamed — `deflated_sharpe_ratio` returns into `sharpe_deflated`, never into `dsr`.
4. It is a transcription of an external formula, not a local computation. Naming your own intermediate `x` because the expression got long is not this exception; it is a signal the expression needs a named sub-result.

There is no exception for loop indices in trading logic (`for i, o in enumerate(orders)` is not one), none for "it is obvious in context", and none for matching a venue's field names — a venue calling it `origQty` is a reason to translate at the adapter boundary, which is where translation belongs (see [`./module-boundaries.md`](./module-boundaries.md)).
