"""Reject float annotations on money names, and float literals in money-free packages.

Why this cannot be left to mypy: `Decimal` and `float` are both perfectly valid types,
so `notional_usd: float` type-checks cleanly and only fails much later, as a
reconciliation drift that looks like an exchange bug. The error is baked in before the
code runs -- `Decimal(0.1)` is already
`Decimal('0.1000000000000000055511151231257827021181583404541015625')` because the
literal was rounded to the nearest double by the parser.

The one sanctioned exception is statistical and machine-learning computation in
`backtest` and `data`, where sampling error is many orders of magnitude larger than
2^-53. Those packages are exempt from the float-literal ban but NOT from the
money-name ban, so `sharpe: float` passes here and `notional_usd: float` does not.
See .claude/rules/decimal-and-money.md.
"""

from __future__ import annotations

import ast
import sys
from collections.abc import Sequence
from pathlib import Path
from typing import Final

MONEY_TOKENS: Final[frozenset[str]] = frozenset(
    {
        "balance",
        "collateral",
        "commission",
        "equity",
        "fee",
        "funding",
        "margin",
        "notional",
        "pnl",
        "price",
        "quantity",
    }
)
MONEY_SUFFIXES: Final[tuple[str, ...]] = ("_usd", "_bps", "_quote", "_base")

# Packages where no float literal may appear at all. `backtest` and `data` are
# deliberately absent: numpy float64 is correct for Sharpe ratios and covariance
# matrices, and forcing Decimal through a NumPy pipeline costs a hundredfold in
# runtime for precision the estimate does not have.
FLOAT_FREE_PACKAGES: Final[frozenset[str]] = frozenset({"domain", "execution", "risk"})


def is_money_name(name: str) -> bool:
    """True when an identifier names a monetary or size-bearing quantity."""
    lowered = name.lower()
    return any(token in lowered for token in MONEY_TOKENS) or lowered.endswith(MONEY_SUFFIXES)


def annotation_mentions_float(node: ast.expr | None) -> bool:
    """True when `float` appears anywhere in an annotation, including inside `X | None`."""
    if node is None:
        return False
    return any(isinstance(sub, ast.Name) and sub.id == "float" for sub in ast.walk(node))


def check_source(source: str, *, label: str, float_free: bool) -> list[str]:
    """Return one message per violation found in `source`."""
    tree = ast.parse(source, filename=label)
    failures: list[str] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name):
            if is_money_name(node.target.id) and annotation_mentions_float(node.annotation):
                failures.append(
                    f"{label}:{node.lineno} money field '{node.target.id}' annotated float"
                )
        elif isinstance(node, ast.arg):
            if is_money_name(node.arg) and annotation_mentions_float(node.annotation):
                failures.append(
                    f"{label}:{node.lineno} money parameter '{node.arg}' annotated float"
                )
        elif isinstance(node, ast.Call) and isinstance(node.func, ast.Name):
            if float_free and node.func.id == "float":
                failures.append(f"{label}:{node.lineno} float() call in a float-free package")
        # bool is a subclass of int, not float, so `True` does not reach this branch.
        elif float_free and isinstance(node, ast.Constant) and isinstance(node.value, float):
            failures.append(
                f"{label}:{node.lineno} float literal {node.value!r} in a float-free package"
            )
    return failures


def check_tree(root: Path) -> list[str]:
    failures: list[str] = []
    for path in sorted(root.rglob("*.py")):
        package = path.relative_to(root).parts[0]
        failures.extend(
            check_source(
                path.read_text(encoding="utf-8"),
                label=str(path),
                float_free=package in FLOAT_FREE_PACKAGES,
            )
        )
    return failures


def main(argv: Sequence[str]) -> int:
    failures: list[str] = []
    for root in argv:
        failures.extend(check_tree(Path(root)))
    for failure in failures:
        print(failure, file=sys.stderr)
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
