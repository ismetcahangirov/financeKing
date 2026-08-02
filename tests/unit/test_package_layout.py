"""The package layout is a contract, not a directory listing.

Every module is listed literally rather than discovered, so that deleting one fails a
test instead of quietly shrinking the expectation.
"""

from __future__ import annotations

import importlib
import pkgutil
from pathlib import Path

import pytest

import fking

pytestmark = pytest.mark.unit

# ARCHITECTURE.md section 3.
DECLARED_MODULES: tuple[str, ...] = (
    "fking.agents",
    "fking.api",
    "fking.backtest",
    "fking.data",
    "fking.domain",
    "fking.evolution",
    "fking.execution",
    "fking.platform",
    "fking.risk",
    "fking.strategy",
)


@pytest.mark.parametrize("module_name", DECLARED_MODULES)
def test_declared_module_imports(module_name: str) -> None:
    assert importlib.import_module(module_name).__name__ == module_name


@pytest.mark.parametrize("module_name", DECLARED_MODULES)
def test_declared_module_states_what_it_knows_about(module_name: str) -> None:
    """Where code goes is decided by asking what it knows about.

    A module whose docstring does not answer that leaves the question to whoever is in
    a hurry, and the answer they pick is "wherever is convenient".
    """
    docstring = importlib.import_module(module_name).__doc__
    assert docstring is not None, f"{module_name} has no docstring"
    assert docstring.strip(), f"{module_name} has an empty docstring"


def test_no_undeclared_top_level_package() -> None:
    """A new package must be placed in the layering deliberately.

    import-linter's `exhaustive` flag also catches this; the redundancy is deliberate,
    because that failure names a layer ordering while this one names the package.
    """
    discovered = {f"fking.{module.name}" for module in pkgutil.iter_modules(fking.__path__)}
    assert discovered == set(DECLARED_MODULES)


def test_package_ships_type_information() -> None:
    """Without py.typed, `mypy --strict` in a consuming project silently sees Any."""
    package_dir = Path(next(iter(fking.__path__)))
    assert (package_dir / "py.typed").is_file()
