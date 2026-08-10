"""Only `fking.risk` may construct an `Order`.

`import-linter` cannot enforce this. `Order` lives in `fking.domain`, and every layer above
`domain` is *supposed* to import it -- `execution` submits orders, `backtest` fills them,
`api` serialises them. The forbidden thing is not the import, it is the call. A contract
expressed as an import edge would either forbid nothing or forbid the whole order path from
naming the type it works with.

So the rule is checked in the syntax tree instead: an `Order(...)` call outside
`src/fking/risk/` is a second place that decides how large a position is, and issue #55 is
the argument for why exactly one place may. `dataclasses.replace(order, ...)` is caught
alongside it, because `replace` is a constructor with a different spelling -- it runs
`__post_init__` and produces an `Order` whose quantity somebody outside the risk engine
chose, which is the same failure with an extra hop.

The narrowest legitimate way past this check is to move the code into `fking.risk`. That is
the intended outcome: if a module needs to construct an order, either it is risk logic and
belongs there, or it does not and should be handed the order the risk engine built.

Tests are not swept. A test constructing an `Order` is constructing a fixture, not sizing a
position, and a check that forbade it would be a check somebody deletes -- the same
reasoning `tools/checks/property_coverage.py` gives for not sweeping `domain` wholesale.
`domain` itself is exempt because that is where the class is defined.
"""

from __future__ import annotations

import ast
import sys
from collections.abc import Sequence
from pathlib import Path
from typing import Final

# The contract's name, printed on every failure. A checker whose output does not name the
# rule it enforces sends the reader to the wrong file: the message has to say which
# invariant broke, not only where.
CONTRACT_NAME: Final[str] = "Only fking.risk constructs an Order"

CONSTRUCTED_TYPE: Final[str] = "Order"

# Relative to the package root passed on the command line. `risk` is the sole constructor;
# `domain` holds the definition and its own `__post_init__` validation.
PERMITTED_PACKAGES: Final[frozenset[str]] = frozenset({"risk", "domain"})

REQUIRED_ARGUMENT_COUNT: Final[int] = 1


def _is_order_call(node: ast.Call) -> bool:
    """`Order(...)` or `order.Order(...)`, however the name was imported."""
    func = node.func
    if isinstance(func, ast.Name):
        return func.id == CONSTRUCTED_TYPE
    return isinstance(func, ast.Attribute) and func.attr == CONSTRUCTED_TYPE


def _replaced_identifier(node: ast.Call) -> str | None:
    """The name `replace()` is being applied to, or `None` when this is not a `replace`.

    Whether the target is really an `Order` cannot be proved from the syntax tree without
    type inference this checker does not have, so the subject is identified by name:
    `replace(order, ...)`, `replace(self.order, ...)`, `replace(net_order, ...)`. That is a
    heuristic and it is stated as one -- it will miss `replace(candidate, ...)` where
    `candidate` happens to hold an order. It is paired with the exact `Order(...)` rule
    above, which is not a heuristic, and `fking.domain.order` refuses a malformed order at
    construction whichever route produced it. Widening it to every `replace()` was tried and
    reverted: it fires on `replace(resting, ...)` in `backtest.venue`, where the subject is a
    `RestingOrder`, and a check with false positives is a check that gets a blanket ignore.
    """
    func = node.func
    is_replace = (isinstance(func, ast.Name) and func.id == "replace") or (
        isinstance(func, ast.Attribute) and func.attr == "replace"
    )
    if not is_replace or not node.args:
        return None
    subject = node.args[0]
    if isinstance(subject, ast.Name):
        return subject.id
    if isinstance(subject, ast.Attribute):
        return subject.attr
    return None


def _is_order_replace(node: ast.Call) -> bool:
    """Whether this `replace()` names an order as its subject."""
    identifier = _replaced_identifier(node)
    return identifier is not None and (identifier == "order" or identifier.endswith("_order"))


def check_source(source: str, *, label: str, order_is_imported: bool) -> list[str]:
    """Every forbidden construction in one module.

    `order_is_imported` gates the `replace` rule: a module that never names `Order` cannot
    be replacing one, and sweeping every `replace()` call in the repository would flag the
    dozens of legitimate frozen-dataclass transitions this codebase is built on.
    """
    failures: list[str] = []
    for node in ast.walk(ast.parse(source)):
        if not isinstance(node, ast.Call):
            continue
        if _is_order_call(node):
            failures.append(
                f"{label}:{node.lineno} constructs {CONSTRUCTED_TYPE}(...) outside "
                f"fking.risk -- contract '{CONTRACT_NAME}'"
            )
        elif order_is_imported and _is_order_replace(node):
            failures.append(
                f"{label}:{node.lineno} calls replace() in a module holding "
                f"{CONSTRUCTED_TYPE}; replace is a constructor with another spelling -- "
                f"contract '{CONTRACT_NAME}'"
            )
    return failures


def imports_order(source: str) -> bool:
    """Whether the module brings the `Order` name into scope at all."""
    for node in ast.walk(ast.parse(source)):
        if isinstance(node, ast.ImportFrom) and any(
            alias.name == CONSTRUCTED_TYPE for alias in node.names
        ):
            return True
    return False


def check_tree(root: Path) -> list[str]:
    failures: list[str] = []
    for path in sorted(root.rglob("*.py")):
        if path.relative_to(root).parts[0] in PERMITTED_PACKAGES:
            continue
        source = path.read_text(encoding="utf-8")
        failures.extend(
            check_source(source, label=str(path), order_is_imported=imports_order(source))
        )
    return failures


def main(argv: Sequence[str]) -> int:
    if len(argv) != REQUIRED_ARGUMENT_COUNT:
        print("usage: order_construction.py <package-root>", file=sys.stderr)
        return 2
    failures = check_tree(Path(argv[0]))
    for failure in failures:
        print(failure, file=sys.stderr)
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
