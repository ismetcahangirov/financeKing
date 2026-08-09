"""Risk and position math carries a Hypothesis property test, or the build fails.

`CLAUDE.md` section 5 makes property tests mandatory for all risk and position math, on
the stated grounds that example-based tests confirm the cases somebody thought of while
position arithmetic fails on the ones they did not -- partial closes, direction flips,
zero-crossings, dust quantities. `docs/rules/testing-rules.md` clause 2 restates it and
quotes this checker's source. Until issue #170 the checker did not exist: `risk/ceilings.py`
and `risk/limits.py` both shipped with no property test and passed every gate, and a
reviewer going looking is what caught it.

Two rules:

1. **Every module under `risk/` has `tests/property/test_<stem>_properties.py`.** The whole
   package, because risk holds veto authority over every order and clause 2 says *every
   function* in it.
2. **Every module named in `POSITION_MATH_MODULES` has the same.** `domain` is not swept
   wholesale: clause 2 asks for *position arithmetic* there, not for a property test on
   every enum and identifier type, and a check that demands one for `enums.py` is a check
   somebody deletes. The declaration is the price of that narrowing -- and it fails closed
   in the other direction too, because a declared module that no longer exists is reported
   rather than silently satisfied. Renaming `position.py` breaks this gate instead of
   emptying it.

**A named file with nothing behind it is a failure, not a pass.** That is precisely how
this decays: `test_x_properties.py` gets created alongside a stub, the stub never grows a
`@given`, and `ls tests/property/` reads as complete. So the decorator is located in the
syntax tree, not by substring -- `"@given"` in a docstring, in a comment, or in a skipped
module's prose satisfies a `grep` and proves nothing.

The naming convention is load-bearing rather than cosmetic, and it is deliberately not an
import-graph check: an import-graph check is defeated by a test that imports the module and
never exercises it, and a human reading `ls tests/property/` can see the gap without
running anything.
"""

from __future__ import annotations

import ast
import sys
from collections.abc import Sequence
from pathlib import Path
from typing import Final

# Relative to the `fking` package root passed on the command line.
SWEPT_PACKAGES: Final[tuple[str, ...]] = ("risk",)

# Modules outside a swept package that carry position arithmetic. Relative to the same
# package root, without the `.py`. Adding one is a one-line diff; removing one without
# removing the module is not possible, because an unmatched declaration is a failure.
POSITION_MATH_MODULES: Final[tuple[str, ...]] = ("domain/position",)

# The decorator that makes a file a property test. `hypothesis.given`, `st.given` and a
# bare `@given` all reduce to this name.
PROPERTY_DECORATOR: Final[str] = "given"

# The package root and the property-test root. Unlike the other checks in this directory
# this one compares two trees, so neither can be inferred from the other.
REQUIRED_ARGUMENT_COUNT: Final[int] = 2

# Distinct from 1, so a misconfigured invocation is never read as "the tree is dirty".
USAGE_EXIT_CODE: Final[int] = 2


def expected_test_name(module_stem: str) -> str:
    return f"test_{module_stem}_properties.py"


def decorator_name(node: ast.expr) -> str | None:
    """The bare name of a decorator expression, ignoring call and module qualification.

    `@given(...)` is an `ast.Call`; `@hypothesis.given(...)` is a call on an `ast.Attribute`;
    `@given` is a plain `ast.Name`. All three are the decorator this check looks for.
    """
    current = node
    if isinstance(current, ast.Call):
        current = current.func
    if isinstance(current, ast.Attribute):
        return current.attr
    if isinstance(current, ast.Name):
        return current.id
    return None


def declares_a_property(tree: ast.AST) -> bool:
    """True when some function in `tree` is decorated with Hypothesis' `given`.

    `ast.walk` rather than a scan of module-level statements, so a property defined inside
    a class or a helper still counts -- the question is whether the file exercises the
    module under generated input, not where the author put the function.
    """
    return any(
        decorator_name(decorator) == PROPERTY_DECORATOR
        for node in ast.walk(tree)
        if isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef)
        for decorator in node.decorator_list
    )


def check_module(module_path: Path, property_tests_root: Path, *, label: str) -> list[str]:
    """Failures for one module that is required to have a property test."""
    expected = property_tests_root / expected_test_name(module_path.stem)
    if not expected.is_file():
        return [
            f"{label}: expected {expected}, which does not exist; "
            f"property tests are mandatory for risk and position math (CLAUDE.md section 5)"
        ]
    tree = ast.parse(expected.read_text(encoding="utf-8"), filename=str(expected))
    if not declares_a_property(tree):
        return [
            f"{expected}: exists but declares no @given; a named file with nothing behind "
            f"it reads as coverage in `ls tests/property/` and asserts nothing"
        ]
    return []


def swept_modules(package_root: Path) -> list[Path]:
    """Every module under a swept package that requires a property test."""
    return [
        path
        for package in SWEPT_PACKAGES
        for path in sorted((package_root / package).rglob("*.py"))
        # A leading underscore means the package does not promise the module exists, so it
        # has no public surface to state a property about.
        if not path.stem.startswith("_")
    ]


def check_package(package_root: Path, property_tests_root: Path) -> list[str]:
    failures: list[str] = []
    for module_path in swept_modules(package_root):
        failures.extend(check_module(module_path, property_tests_root, label=str(module_path)))
    for declared in POSITION_MATH_MODULES:
        module_path = package_root / f"{declared}.py"
        if not module_path.is_file():
            # The declaration outlived the module. Reported rather than skipped: a rename
            # that emptied this list would otherwise turn the domain half of the gate off
            # while leaving it looking configured.
            failures.append(
                f"{module_path}: declared in POSITION_MATH_MODULES and does not exist; "
                f"the declaration is stale and the module it named is now ungated"
            )
            continue
        failures.extend(check_module(module_path, property_tests_root, label=str(module_path)))
    return failures


def main(argv: Sequence[str]) -> int:
    if not argv:
        return 0
    if len(argv) != REQUIRED_ARGUMENT_COUNT:
        print(
            "usage: property_coverage.py <package-root> <property-tests-root>",
            file=sys.stderr,
        )
        return USAGE_EXIT_CODE
    package_root, property_tests_root = argv
    failures = check_package(Path(package_root), Path(property_tests_root))
    for failure in failures:
        print(failure, file=sys.stderr)
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
