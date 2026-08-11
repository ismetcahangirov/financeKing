"""Every feature computation is registered, and no consumer reaches one directly.

The adversarial look-ahead probe is parametrised over `FEATURES`, so a feature in the
registry inherits it with no test-file edit. That leaves exactly one route by which a
computation reaches a strategy unprobed: not being in the registry at all -- written next
to the code that consumes it, never declaring a lookback, never declaring an availability
lag, and never covered by anything (`DATA_PIPELINE.md` section 7, issue #31).

Two rules, closing that route from both ends:

1. **Every compute function under `data/features/` is registered.** A function is a
   compute function if its second parameter is annotated `FeatureWindow`, which is the
   shape `FeatureCompute` names. Keying on the signature rather than on the module keeps
   the check honest when the functions are eventually split across files, and keying on
   it rather than on a naming convention keeps it honest when somebody picks a different
   convention.

2. **Nothing outside `fking.data` imports a compute module.** A strategy that imports
   `trailing_return_fraction` and calls it has a feature value that never passed through
   `evaluate`, never got an `available_at_utc` derived from a declared lag, and never
   reached the store -- so every mechanism in `docs/rules/no-lookahead.md` is bypassed
   by an import statement.

Static, like every check in this directory: the registry is read as a syntax tree rather
than imported. A check that imports the thing it checks fails on an unrelated import error
and reports it as a violation, and the reader believes the report.
"""

from __future__ import annotations

import ast
import sys
from collections.abc import Sequence
from pathlib import Path
from typing import Final

# Relative to the `fking` package root passed on the command line.
FEATURES_PACKAGE: Final[Path] = Path("data") / "features"
REGISTRY_MODULE: Final[Path] = FEATURES_PACKAGE / "registry.py"

# The annotation that identifies the second parameter of a `FeatureCompute`.
WINDOW_ANNOTATION: Final[str] = "FeatureWindow"

# The keywords `FeatureSpec` accepts a computation under, one per observation kind. A
# feature declares exactly one; this check does not care which.
COMPUTE_KEYWORDS: Final[frozenset[str]] = frozenset({"compute", "settlement_rate_compute"})

# Packages that may reach a compute function directly. `fking.data` owns them; everything
# else goes through the registry and the store.
COMPUTE_IMPORTERS: Final[frozenset[str]] = frozenset({"data"})

# The modules under `data/features/` that a consumer is meant to import: the types it
# holds, the reader it reads through, the contract that refuses, and the label alignment.
# Everything else in that package is a computation, and importing one bypasses `evaluate`.
PUBLIC_FEATURE_MODULES: Final[tuple[str, ...]] = (
    ".availability",
    ".labels",
    ".spec",
    ".store",
)


def annotation_name(node: ast.expr | None) -> str | None:
    """The bare name of an annotation, ignoring any module qualification."""
    if isinstance(node, ast.Name):
        return node.id
    if isinstance(node, ast.Attribute):
        return node.attr
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return node.value.rsplit(".", maxsplit=1)[-1]
    return None


def compute_functions(tree: ast.AST) -> list[tuple[str, int]]:
    """Module-level functions whose shape is `FeatureCompute`, with their line numbers."""
    found: list[tuple[str, int]] = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef):
            continue
        positional = node.args.posonlyargs + node.args.args
        window_position = 2
        if len(positional) != window_position:
            continue
        if annotation_name(positional[1].annotation) == WINDOW_ANNOTATION:
            found.append((node.name, node.lineno))
    return found


def registered_compute_names(tree: ast.AST) -> set[str]:
    """Names passed to either computation keyword anywhere in the registry module.

    Keywords rather than positions, because `FeatureSpec` is keyword-only. A value that is
    not a plain name -- a lambda, a partial, an attribute -- is not collected, and the
    function it wraps is then reported as unregistered. That is the correct outcome:
    `definition_digest` refuses anything but a plain function for the same reason.

    Both keywords are collected because a feature declares exactly one of them, and
    checking only `compute=` would report every settlement-rate feature as unregistered --
    which trains the reflex of adding names to an exemption list.
    """
    names: set[str] = set()
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        for keyword in node.keywords:
            if keyword.arg in COMPUTE_KEYWORDS and isinstance(keyword.value, ast.Name):
                names.add(keyword.value.id)
    return names


def unregistered_computations(package_root: Path) -> list[str]:
    registry_path = package_root / REGISTRY_MODULE
    if not registry_path.is_file():
        return [f"{registry_path}: the feature registry is missing; nothing can be probed"]
    registered = registered_compute_names(
        ast.parse(registry_path.read_text(encoding="utf-8"), filename=str(registry_path))
    )
    failures: list[str] = []
    for path in sorted((package_root / FEATURES_PACKAGE).rglob("*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        failures.extend(
            f"{path}:{lineno} '{name}' has the shape of a feature computation and is not in "
            f"FEATURES; an unregistered computation is one the look-ahead probe never runs"
            for name, lineno in compute_functions(tree)
            if name not in registered
        )
    return failures


def direct_compute_imports(package_root: Path) -> list[str]:
    """Imports of a feature-computation module from outside the packages that may."""
    failures: list[str] = []
    module_prefix = ".".join(("fking", *FEATURES_PACKAGE.parts))
    for path in sorted(package_root.rglob("*.py")):
        relative = path.relative_to(package_root)
        if relative.parts[0] in COMPUTE_IMPORTERS:
            continue
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for node in ast.walk(tree):
            if not isinstance(node, ast.Import | ast.ImportFrom):
                continue
            failures.extend(
                f"{path}:{node.lineno} imports '{module}'; a computation called outside "
                f"`evaluate` derives no availability stamp and reaches no store, so every "
                f"point-in-time mechanism is bypassed by this line"
                for module in _imported_modules(node)
                if module.startswith(f"{module_prefix}.")
                and not module.endswith(PUBLIC_FEATURE_MODULES)
            )
    return failures


def _imported_modules(node: ast.Import | ast.ImportFrom) -> list[str]:
    if isinstance(node, ast.Import):
        return [alias.name for alias in node.names]
    if node.module is not None and node.level == 0:
        return [node.module]
    return []


def check_package(package_root: Path) -> list[str]:
    return unregistered_computations(package_root) + direct_compute_imports(package_root)


def main(argv: Sequence[str]) -> int:
    failures: list[str] = []
    for root in argv:
        failures.extend(check_package(Path(root)))
    for failure in failures:
        print(failure, file=sys.stderr)
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
