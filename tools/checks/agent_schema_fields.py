"""No agent model may declare a field the AI system is forbidden to decide.

`AI_MANIFEST.md` section 3 item 2 says the AI system may never determine a position
size, notional or leverage, and item 5 says it may never widen, bypass or query around
the host allowlist. Both are enforced *by absence*: there is no such field on any agent
schema, so the prohibition holds regardless of what a prompt asks for or what a model
decides to emit.

Enforcement by absence is stronger than a guard inside a field that exists. A guard is
defeated by a refactor that does not understand it, by an exception handler keeping a
loop alive, or by a config flag added for testing. A field that does not exist is
defeated by none of those -- the only way to reach it is to add it, in a reviewed pull
request, which is exactly the moment this check fires.

The scan is a two-pass closure over the whole package rather than a per-file base-name
match, because `class ThesisProposal(AgentOutput)` names a base defined in another
module. A single-pass check would see an unknown base and skip the class -- which is
the failure mode where the check is present, green, and inspecting nothing.

Matching is by substring on the lowercased field name, so `notional_usd`,
`max_leverage` and `venue_base_url` are all caught. That is deliberately blunt: a field
whose name merely *contains* a forbidden token is a field whose name a reviewer would
have to think about, and thinking about it is the point.
"""

from __future__ import annotations

import ast
import sys
from collections.abc import Sequence
from pathlib import Path
from typing import Final

# Bases that make a class an agent-facing schema. A class reachable from any of these
# by inheritance is scanned; anything else in the package is not, because an internal
# record is not a decision surface.
SCHEMA_ROOTS: Final[frozenset[str]] = frozenset({"AgentInput", "AgentOutput", "BaseModel"})

# Sizing authority belongs to the risk engine (RISK_PHILOSOPHY.md). A schema that can
# express a quantity is a schema the deterministic core might one day read.
SIZING_TOKENS: Final[frozenset[str]] = frozenset(
    {
        "size",
        "notional",
        "leverage",
        "quantity",
        "margin",
        "collateral",
        "position_usd",
    }
)

# Anything that names where a request could go. The host allowlist is compiled in and
# an agent may not propose a change to it "in any form, for any reason, including
# read-only" -- so it may not name a host at all.
EGRESS_TOKENS: Final[frozenset[str]] = frozenset(
    {
        "host",
        "hostname",
        "base_url",
        "endpoint",
        "api_key",
        "secret",
    }
)

FORBIDDEN_TOKENS: Final[frozenset[str]] = SIZING_TOKENS | EGRESS_TOKENS


def _base_names(node: ast.ClassDef) -> list[str]:
    """The simple names of every base, dotted or not."""
    names: list[str] = []
    for base in node.bases:
        if isinstance(base, ast.Name):
            names.append(base.id)
        elif isinstance(base, ast.Attribute):
            names.append(base.attr)
    return names


def _annotated_field_names(node: ast.ClassDef) -> list[tuple[str, int]]:
    """`name: Annotation` entries in the class body, with their line numbers.

    Only `AnnAssign` counts. A bare `x = 1` in a Pydantic model body is a default for a
    field declared elsewhere or a class constant, and neither is a schema field.
    """
    return [
        (statement.target.id, statement.lineno)
        for statement in node.body
        if isinstance(statement, ast.AnnAssign) and isinstance(statement.target, ast.Name)
    ]


def forbidden_tokens_in(field_name: str) -> list[str]:
    lowered = field_name.lower()
    return sorted(token for token in FORBIDDEN_TOKENS if token in lowered)


def schema_classes(package_root: Path) -> dict[str, tuple[Path, ast.ClassDef]]:
    """Every class under `package_root` that inherits, transitively, from a schema root.

    Two passes: collect all classes with their declared base names, then grow the set of
    schema classes until it stops changing. Base names are matched simply, so an alias
    import (`from x import AgentOutput as Base`) would be missed -- there is no such
    alias in this package, and adding one to dodge this check would be a deliberate act
    visible in the diff.
    """
    declared: dict[str, tuple[Path, ast.ClassDef, list[str]]] = {}
    for path in sorted(package_root.rglob("*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for node in ast.walk(tree):
            if isinstance(node, ast.ClassDef):
                declared[node.name] = (path, node, _base_names(node))

    schema_names = set(SCHEMA_ROOTS)
    changed = True
    while changed:
        changed = False
        for name, (_path, _node, bases) in declared.items():
            if name not in schema_names and schema_names.intersection(bases):
                schema_names.add(name)
                changed = True

    return {
        name: (path, node)
        for name, (path, node, _bases) in declared.items()
        if name in schema_names
    }


def check_package(package_root: Path) -> list[str]:
    failures: list[str] = []
    for name, (path, node) in sorted(schema_classes(package_root).items()):
        for field_name, lineno in _annotated_field_names(node):
            tokens = forbidden_tokens_in(field_name)
            if tokens:
                failures.append(
                    f"{path}:{lineno} {name}.{field_name} names {tokens}; agent schemas "
                    f"carry no sizing or egress field -- AI_MANIFEST.md section 3"
                )
    return failures


def main(argv: Sequence[str]) -> int:
    failures: list[str] = []
    for root in argv:
        path = Path(root)
        if not path.is_dir():
            print(f"{root}: not a directory", file=sys.stderr)
            return 1
        failures.extend(check_package(path))
    for failure in failures:
        print(failure, file=sys.stderr)
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
