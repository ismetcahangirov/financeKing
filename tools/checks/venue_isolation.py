"""Reject any code that asks which venue it is holding.

`BACKTEST_ENGINE.md` section 3 promises that a strategy, a feature computation, a risk
sizing and a portfolio update are byte-identical across every venue. `import-linter`
already stops `strategy` from importing `execution`, so a strategy cannot name a venue
class at all -- but the engine, the portfolio and the OMS all legitimately hold one, and
in those modules a single `isinstance(venue, BacktestVenue)` is enough to make the promise
false.

It is a plausible-looking line, which is the problem. "Backtest venues have no
reconciliation, so skip it here" is true and is the first step; the second is a paper-only
guard for a venue quirk; the third is a backtest-only shortcut for speed. Each is defended
on its own, and afterwards a backtest result is unfalsifiable, because "the strategy is
bad" and "the harness differs" have become indistinguishable.

Three spellings are rejected, because banning only the first teaches the next author the
second:

- `isinstance(venue, PaperVenue)`, including a tuple of venue types
- `type(venue) is ReplayVenue`
- `venue.__class__ is BacktestVenue`

The bare domain enum `Venue` is exempt. It is a symbol's exchange, not an implementation
-- `isinstance(x, Venue)` is a type check on an enum and carries no branch on how fills
are produced. Everything whose name *ends* in `Venue` and is longer than it is a venue
implementation or its Protocol, and is refused.

What is deliberately not rejected: constructing a venue, importing one, or annotating a
parameter with one. Naming the type is how the object gets built; branching on it is the
thing that breaks parity.
"""

from __future__ import annotations

import ast
import sys
from collections.abc import Sequence
from pathlib import Path
from typing import Final

#: `fking.domain.Venue` names an exchange, not a fill model -- see the module docstring.
EXEMPT: Final[frozenset[str]] = frozenset({"Venue"})

_CLASS_ATTRIBUTE: Final = "__class__"


def _is_venue_type(node: ast.expr) -> str | None:
    """The venue implementation `node` names, if it names one."""
    if isinstance(node, ast.Name):
        named = node.id
    elif isinstance(node, ast.Attribute):
        named = node.attr
    else:
        return None
    if named in EXEMPT or not named.endswith("Venue"):
        return None
    return named


def _venue_types_in(node: ast.expr) -> list[str]:
    """Every venue implementation named by `node`, which may be a tuple of them."""
    if isinstance(node, ast.Tuple):
        return [named for element in node.elts if (named := _is_venue_type(element)) is not None]
    named = _is_venue_type(node)
    return [] if named is None else [named]


def _is_runtime_class_of(node: ast.expr) -> bool:
    """Whether `node` is `type(x)` or `x.__class__` -- the two ways to get a class object."""
    if isinstance(node, ast.Call) and isinstance(node.func, ast.Name) and node.func.id == "type":
        return len(node.args) == 1
    return isinstance(node, ast.Attribute) and node.attr == _CLASS_ATTRIBUTE


def check_source(source: str, *, label: str) -> list[str]:
    """Every venue-conditional branch in one module, as human-readable failures."""
    failures: list[str] = []
    for node in ast.walk(ast.parse(source, filename=label)):
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Name):
            if node.func.id in {"isinstance", "issubclass"} and len(node.args) == 2:  # noqa: PLR2004
                # Exactly two arguments: the builtin's signature. Anything else is a
                # shadowed name, and guessing at its meaning would produce a failure
                # nobody can act on.
                failures.extend(
                    f"{label}:{node.lineno} {node.func.id}(..., {named}) branches on which "
                    f"venue is held -- see BACKTEST_ENGINE.md section 3"
                    for named in _venue_types_in(node.args[1])
                )
            continue
        if isinstance(node, ast.Compare):
            operands = [node.left, *node.comparators]
            if not any(_is_runtime_class_of(operand) for operand in operands):
                continue
            for operand in operands:
                failures.extend(
                    f"{label}:{node.lineno} comparing a runtime class against {named} "
                    f"branches on which venue is held -- see BACKTEST_ENGINE.md section 3"
                    for named in _venue_types_in(operand)
                )
    return failures


def check_tree(root: Path) -> list[str]:
    """Every venue-conditional branch under `root`."""
    failures: list[str] = []
    for path in sorted(root.rglob("*.py")):
        failures.extend(check_source(path.read_text(encoding="utf-8"), label=str(path)))
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
