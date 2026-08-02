"""SafetyViolation is never caught, and neither is BaseException.

`SafetyViolation` inherits `BaseException` precisely so that the ordinary defensive
handlers in this codebase -- and in third-party libraries we do not control -- cannot
absorb it and retry against a host outside the allowlist. That guarantee survives only
as long as nobody writes `except BaseException` on purpose, which ruff's BLE001 does
not cover.

The check runs over tests/ as well as src/. A test that catches SafetyViolation to
assert it was raised is the first step toward code that does; use
`pytest.raises(SafetyViolation)`, which is a call rather than an except clause and so
does not appear here.

See .claude/rules/error-handling.md and .claude/rules/safety-kernel.md.
"""

from __future__ import annotations

import ast
import sys
from collections.abc import Sequence
from pathlib import Path
from typing import Final

FORBIDDEN_HANDLERS: Final[frozenset[str]] = frozenset({"BaseException", "SafetyViolation"})


def handler_names(handler: ast.ExceptHandler) -> list[str]:
    """Return the exception names an except clause catches; ['<bare>'] for `except:`."""
    node = handler.type
    if node is None:
        return ["<bare>"]
    parts = list(node.elts) if isinstance(node, ast.Tuple) else [node]
    names: list[str] = []
    for part in parts:
        if isinstance(part, ast.Name):
            names.append(part.id)
        elif isinstance(part, ast.Attribute):
            # `errors.SafetyViolation` catches the same class as `SafetyViolation`.
            names.append(part.attr)
    return names


def check_source(source: str, *, label: str) -> list[str]:
    tree = ast.parse(source, filename=label)
    failures: list[str] = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.ExceptHandler):
            continue
        caught = sorted(set(handler_names(node)) & FORBIDDEN_HANDLERS)
        if caught:
            failures.append(
                f"{label}:{node.lineno} catches {caught}; the safety kernel has no handler"
            )
    return failures


def check_tree(root: Path) -> list[str]:
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
