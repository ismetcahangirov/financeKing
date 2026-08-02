"""A boundary contract that cannot fail proves nothing.

Issue #10 requires that "a deliberate violating import makes CI fail". Demonstrating
that by hand once proves it for the afternoon it was demonstrated. This asserts it on
every run, in both directions: an unmodified copy of the tree must pass, and the same
copy with one forbidden import must fail *and name the contract it broke*.

Both directions are load-bearing. Without the passing case, a harness that silently
fails to analyse anything at all would look like a working guard.

The tree is copied to tmp_path and put on PYTHONPATH rather than being edited in
place: the editable install is a plain .pth file, whose paths are appended during
site processing and therefore lose to PYTHONPATH entries, so the copy shadows the
real package. Editing src/fking in place would leave a violating import behind if the
run were interrupted.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

pytestmark = [pytest.mark.integration, pytest.mark.slow]

REPO_ROOT = Path(__file__).resolve().parents[2]

# The exact violation the contract exists to stop: a strategy reaching the order path.
# A strategy that can construct an Order can bypass the risk engine entirely.
VIOLATING_IMPORT = "\nfrom fking.execution import __all__ as _forbidden  # boundary violation\n"

CONTRACT_NAME = "Strategies never reach the order path"


def _lint_imports_executable() -> str:
    """Resolve the console script, and fail loudly rather than skip if it is absent.

    Deliberately not `python -m importlinter.cli`: that import succeeds, runs no
    contracts and exits 0, which is indistinguishable from every contract passing.
    """
    executable = shutil.which("lint-imports", path=str(Path(sys.executable).parent))
    if executable is None:
        executable = shutil.which("lint-imports")
    assert executable is not None, "lint-imports is not installed; run `uv sync`"
    return executable


def _lint_imports(cwd: Path, package_parent: Path) -> subprocess.CompletedProcess[str]:
    environment = dict(os.environ)
    environment["PYTHONPATH"] = str(package_parent)
    return subprocess.run(
        [_lint_imports_executable()],
        cwd=cwd,
        env=environment,
        capture_output=True,
        text=True,
        check=False,
    )


@pytest.fixture
def isolated_tree(tmp_path: Path) -> Path:
    """A standalone copy of the package plus the real contract configuration."""
    shutil.copytree(REPO_ROOT / "src" / "fking", tmp_path / "src" / "fking")
    shutil.copy2(REPO_ROOT / "pyproject.toml", tmp_path / "pyproject.toml")
    return tmp_path


def test_the_unmodified_tree_satisfies_every_contract(isolated_tree: Path) -> None:
    result = _lint_imports(isolated_tree, isolated_tree / "src")
    assert result.returncode == 0, (
        f"the committed tree breaks its own contracts:\n{result.stdout}\n{result.stderr}"
    )
    assert CONTRACT_NAME in result.stdout, (
        "lint-imports ran but never evaluated the contract under test; "
        f"stdout was:\n{result.stdout}"
    )


def test_a_strategy_importing_execution_fails_the_build(isolated_tree: Path) -> None:
    strategy_init = isolated_tree / "src" / "fking" / "strategy" / "__init__.py"
    strategy_init.write_text(
        strategy_init.read_text(encoding="utf-8") + VIOLATING_IMPORT, encoding="utf-8"
    )

    result = _lint_imports(isolated_tree, isolated_tree / "src")

    assert result.returncode != 0, (
        f"a strategy imported execution and lint-imports passed:\n{result.stdout}"
    )
    assert CONTRACT_NAME in result.stdout
    assert "BROKEN" in result.stdout
    assert "fking.strategy -> fking.execution" in result.stdout
