"""A module constructing its own HTTP client must fail the build.

Issue #11 requires that `import-linter` blocks direct HTTP library imports in
`execution`, "verified by a deliberate violation". Verified once by hand means
verified for that afternoon, so it is asserted here on every run.

The threat is not malice. It is an agent generating `import httpx` because that is
what the training data does, in a module whose author never read CLAUDE.md. Such a
client has left the safety kernel entirely: no host validation, no `trust_env=False`,
no boot-time allowlist check, no audit.

Both directions are asserted. Without the passing case, a harness that analysed
nothing would look exactly like a working guard -- which is not hypothetical in this
repository, where `python -m importlinter.cli` was found to exit 0 having evaluated no
contracts at all.
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
CONTRACT_NAME = "Only the safety kernel constructs network clients"

# The exact shape the contract exists to stop: a venue adapter reaching for the
# transport directly instead of going through guarded_client().
VIOLATION = (
    "\nimport httpx  # deliberate: bypasses fking.platform.safety.guarded_client\n"
    "\n_UNGUARDED = httpx.AsyncClient\n"
)


def _lint_imports_executable() -> str:
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
        # Explicit UTF-8, not the locale codec `text=True` would otherwise pick. The
        # subprocess is `lint-imports`, whose rich banner contains box-drawing glyphs;
        # on a Windows console that defaults to cp1252 the decode raises and the test
        # fails while the contracts it is checking all passed. Same trap as the one
        # documented on the Makefile's `imports` target (#141), one layer out.
        encoding="utf-8",
        check=False,
    )


@pytest.fixture
def isolated_tree(tmp_path: Path) -> Path:
    """A standalone copy of the package plus the real contract configuration.

    Copied rather than edited in place: the editable install is a plain .pth file,
    whose paths are appended during site processing and therefore lose to PYTHONPATH,
    so the copy shadows the real package. Editing src/fking would leave an unguarded
    import behind if the run were interrupted.
    """
    shutil.copytree(REPO_ROOT / "src" / "fking", tmp_path / "src" / "fking")
    shutil.copy2(REPO_ROOT / "pyproject.toml", tmp_path / "pyproject.toml")
    return tmp_path


def test_the_committed_tree_satisfies_the_network_contract(isolated_tree: Path) -> None:
    result = _lint_imports(isolated_tree, isolated_tree / "src")
    assert result.returncode == 0, f"the committed tree breaks its own contracts:\n{result.stdout}"
    assert CONTRACT_NAME in result.stdout, (
        f"lint-imports ran but never evaluated the contract under test:\n{result.stdout}"
    )


def test_execution_importing_httpx_directly_fails_the_build(isolated_tree: Path) -> None:
    module = isolated_tree / "src" / "fking" / "execution" / "__init__.py"
    module.write_text(module.read_text(encoding="utf-8") + VIOLATION, encoding="utf-8")

    result = _lint_imports(isolated_tree, isolated_tree / "src")

    assert result.returncode != 0, (
        f"execution imported httpx and lint-imports passed:\n{result.stdout}"
    )
    assert CONTRACT_NAME in result.stdout
    assert "BROKEN" in result.stdout
    assert "fking.execution -> httpx" in result.stdout


def test_the_safety_kernel_itself_may_still_import_httpx(isolated_tree: Path) -> None:
    """The contract must permit the one path that is supposed to exist.

    `fking.platform` is not a source module precisely so the kernel can hold the
    transport. A contract that also forbade this would forbid the correct
    architecture, and the pressure to delete it would become irresistible.
    """
    kernel = isolated_tree / "src" / "fking" / "platform" / "safety" / "client.py"
    assert "import httpx" in kernel.read_text(encoding="utf-8")

    result = _lint_imports(isolated_tree, isolated_tree / "src")
    assert result.returncode == 0
