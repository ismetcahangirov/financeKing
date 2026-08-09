"""Reject ambiguous identifiers. The table in docs/rules/naming.md is the source of truth.

`size` is not vague, it is dangerous: in a trading system it plausibly means base
quantity, quote notional, contract count, or position value in USD -- four numbers
that differ by four or five orders of magnitude for BTC. A function taking
`size: Decimal` and called with a notional where it expected a base quantity produces
an order 60,000 times too large, and every annotation in the chain is correct.
mypy cannot help, because both are `Decimal`.

The check runs over src/fking only. Tests may bind `price` in a fixture body without
harm, and extending the ban there produces noise that gets the check disabled.
"""

from __future__ import annotations

import ast
import sys
from collections.abc import Sequence
from pathlib import Path
from typing import Final

BANNED: Final[frozenset[str]] = frozenset(
    {
        "amount",
        "balance",
        "count",
        "data",
        "fee",
        "info",
        "limit",
        "obj",
        "pct_change",
        "pnl",
        "price",
        "qty",
        "result",
        "ret",
        "size",
        "slippage",
        "time",
        "timeout",
        "timestamp",
        "tmp",
        "ts",
        "value",
    }
)

# Modules transcribing a published formula may use its symbols, provided the marker
# is present and the docstring defines every one of them. Grep-able on purpose: it
# shows up in review as a deliberate act.
MATH_ESCAPE: Final[str] = "# fking: allow-math-symbols"


def package_names(root: Path) -> frozenset[str]:
    """The top-level packages under `root`.

    One word in BANNED is also a module name -- `data`, at src/fking/data. The module
    map is the one context where these words are unambiguous, because the name is
    resolved by the architecture rather than by the reader: `data` there means exactly
    "ingestion, storage and the feature store", and a settings section or attribute
    that mirrors the package must be allowed to share its name. Renaming it to
    `market_data` would break the mirror between FKING_DATA__* and the package it
    configures, and would be less accurate besides.

    Derived from the tree rather than hardcoded, so this exemption cannot be widened by
    editing a list -- it can only be widened by creating a package, which is a change
    the layering contract already reviews.
    """
    return frozenset(
        entry.name
        for entry in root.iterdir()
        if entry.is_dir() and (entry / "__init__.py").exists()
    )


def bound_names(tree: ast.AST) -> list[tuple[str, int]]:
    """Every identifier the module binds, with its line number."""
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


def check_source(source: str, *, label: str, exempt: frozenset[str] = frozenset()) -> list[str]:
    if MATH_ESCAPE in source:
        return []
    failures: list[str] = []
    for name, lineno in bound_names(ast.parse(source, filename=label)):
        if name in BANNED and name not in exempt:
            failures.append(
                f"{label}:{lineno} ambiguous identifier '{name}' -- see docs/rules/naming.md"
            )
        elif name.endswith("_pct") and "fraction" in name:
            failures.append(f"{label}:{lineno} '{name}' claims both percent and fraction")
    return failures


def check_tree(root: Path) -> list[str]:
    exempt = package_names(root)
    failures: list[str] = []
    for path in sorted(root.rglob("*.py")):
        failures.extend(
            check_source(path.read_text(encoding="utf-8"), label=str(path), exempt=exempt)
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
