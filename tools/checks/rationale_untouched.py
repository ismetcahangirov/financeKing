"""`.rationale` is stored and displayed. Nothing in `src/fking/**` reads it.

`rationale` is the one channel through which arbitrary model-authored text travels
intact through this system -- including anything an injected news headline persuaded a
model to echo back. Every other guarantee in `docs/rules/llm-output-handling.md`
survives that only as long as nothing downstream ever *acts* on it.

The moment any code branches on its contents, the free-text field has become an untyped
control channel from the model into the deterministic core, and it is one that no
schema constrains, no reviewer reads and no test covers. `conviction` is the field that
carries confidence; it is a bounded `Decimal`, and it is the only one the risk engine
sees.

An AST check rather than a grep: the word "rationale" appears in docstrings, in field
declarations and in comments throughout this repository, and a grep that flags those is
a grep somebody switches off. This names the exact node -- an attribute *load* -- and
ignores everything else.

Permitted, and nothing else:

- **A field declaration.** `rationale: RationaleText` is an `AnnAssign`, not an
  attribute access, so it never reaches this check.
- **A keyword argument.** `ThesisProposal(rationale=...)` is an `ast.keyword`, also not
  an attribute access.
- **`self.rationale` inside `__post_init__`.** Construction-time validation is the
  third of the four permitted operations -- length-bounding -- and the only thing a
  `__post_init__` can do with the value is refuse to build the object. It cannot reach
  a decision, because there is no object yet to decide about. `fking.domain.Signal`
  does exactly this, and rejecting it would push the emptiness guard somewhere with
  less authority than the constructor.
- **An allowlisted serializer module.** Rendering it for a human is the second
  permitted operation, and the renderer must read the field to render it.

Storing it needs no exemption: an audit writer takes the whole model, and the field
goes to the database with the rest of it.
"""

from __future__ import annotations

import ast
import sys
from collections.abc import Sequence
from pathlib import Path
from typing import Final

GUARDED_FIELD: Final[str] = "rationale"

# Modules that may read the field, as POSIX-style paths relative to the scan root.
# Display is permitted; the serializer is where display happens. Kept as an explicit
# tuple rather than a directory glob so that adding a second reader is a diff someone
# has to justify.
PERMITTED_READERS: Final[tuple[str, ...]] = ("api/serializers.py",)

# The one method that may read the field off `self`. Named exactly, not by prefix: a
# method called `_post_init_validate` is ordinary code that happens to run at
# construction, and the exemption rests on the constructor's inability to decide
# anything rather than on a naming convention.
CONSTRUCTION_VALIDATOR: Final[str] = "__post_init__"


def _is_permitted_reader(relative_path: str) -> bool:
    return relative_path in PERMITTED_READERS


def _is_self_access(node: ast.Attribute) -> bool:
    return isinstance(node.value, ast.Name) and node.value.id == "self"


def _guarded_accesses(tree: ast.AST) -> list[ast.Attribute]:
    """Every `.rationale` attribute node that is not a construction-time validation.

    `ast.Store` and `ast.Del` contexts are reported too. `thesis.rationale = x` cannot
    succeed on a frozen model, but a module that attempts it is a module that believes
    the field is writable, and that belief is the thing worth catching early.
    """
    exempt: set[int] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.FunctionDef) and node.name == CONSTRUCTION_VALIDATOR:
            exempt.update(
                id(inner)
                for inner in ast.walk(node)
                if isinstance(inner, ast.Attribute)
                and inner.attr == GUARDED_FIELD
                and _is_self_access(inner)
            )
    return [
        node
        for node in ast.walk(tree)
        if isinstance(node, ast.Attribute) and node.attr == GUARDED_FIELD and id(node) not in exempt
    ]


def check_source(source: str, *, label: str, permitted: bool = False) -> list[str]:
    """Every forbidden `.rationale` access in `source`, with its line number."""
    if permitted:
        return []
    return [
        f"{label}:{node.lineno} reads .{GUARDED_FIELD}; it is stored and displayed, "
        f"never parsed, matched, branched on or used as a cache key "
        f"-- docs/rules/llm-output-handling.md"
        for node in _guarded_accesses(ast.parse(source, filename=label))
    ]


def check_tree(package_root: Path) -> list[str]:
    failures: list[str] = []
    for path in sorted(package_root.rglob("*.py")):
        relative = path.relative_to(package_root).as_posix()
        failures.extend(
            check_source(
                path.read_text(encoding="utf-8"),
                label=str(path),
                permitted=_is_permitted_reader(relative),
            )
        )
    return failures


def main(argv: Sequence[str]) -> int:
    failures: list[str] = []
    for root in argv:
        path = Path(root)
        if not path.is_dir():
            print(f"{root}: not a directory", file=sys.stderr)
            return 1
        failures.extend(check_tree(path))
    for failure in failures:
        print(failure, file=sys.stderr)
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
