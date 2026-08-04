"""The order path must not be able to reach the archive egress client, and vice versa.

Issue #22 refuses the obvious implementation -- adding `data.binance.vision` to
`PERMITTED_HOSTS` and reusing `guarded_client()` -- on the grounds that the trading
allowlist is not a permission list but a proof about which hosts a process holding
order-placement code can reach at all. The property bought instead is *two clients that
cannot see each other*, and a property nobody re-checks is a property that lasts one
afternoon.

So both directions are deliberately violated here on every run:

- `fking.execution` importing the archive client must fail the build. That is the
  refactor CLAUDE.md section 11 names: read paths become write paths, and the first
  step is always a shared helper that already had the host in its list.
- The archive path importing `fking.platform.config` must fail the build. Every
  `SecretStr` in this system lives there, and a client that cannot authenticate cannot
  place an order even if a future change points it at the wrong host.

Both directions are asserted against a passing baseline, because a harness that
analysed nothing would look exactly like a working guard.
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

ORDER_PATH_CONTRACT = "The order path cannot reach the archive egress client"
CREDENTIAL_CONTRACT = "The archive egress path holds no credentials"

# The exact shape each contract exists to stop.
EXECUTION_REACHES_ARCHIVE = (
    "\nfrom fking.platform.safety.archive import guarded_archive_client"
    "  # deliberate: the order path reaching a data-host client\n"
    "\n_ARCHIVE = guarded_archive_client\n"
)
ARCHIVE_REACHES_CREDENTIALS = (
    "\nfrom fking.platform.config import settings"
    "  # deliberate: attaching a credential source to the archive path\n"
    "\n_SETTINGS = settings\n"
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
    so the copy shadows the real package. Editing src/fking would leave a violating
    import behind if the run were interrupted.
    """
    shutil.copytree(REPO_ROOT / "src" / "fking", tmp_path / "src" / "fking")
    shutil.copy2(REPO_ROOT / "pyproject.toml", tmp_path / "pyproject.toml")
    return tmp_path


def _append(module: Path, text: str) -> None:
    module.write_text(module.read_text(encoding="utf-8") + text, encoding="utf-8")


@pytest.mark.parametrize("contract", [ORDER_PATH_CONTRACT, CREDENTIAL_CONTRACT])
def test_the_committed_tree_satisfies_the_archive_contracts(
    isolated_tree: Path, contract: str
) -> None:
    result = _lint_imports(isolated_tree, isolated_tree / "src")
    assert result.returncode == 0, f"the committed tree breaks its own contracts:\n{result.stdout}"
    assert contract in result.stdout, (
        f"lint-imports ran but never evaluated {contract!r}:\n{result.stdout}"
    )


def test_execution_importing_the_archive_client_fails_the_build(isolated_tree: Path) -> None:
    module = isolated_tree / "src" / "fking" / "execution" / "__init__.py"
    _append(module, EXECUTION_REACHES_ARCHIVE)

    result = _lint_imports(isolated_tree, isolated_tree / "src")

    assert result.returncode != 0, (
        f"the order path reached the archive client and lint-imports passed:\n{result.stdout}"
    )
    assert ORDER_PATH_CONTRACT in result.stdout
    assert "BROKEN" in result.stdout
    assert "fking.execution -> fking.platform.safety.archive" in result.stdout


def test_execution_importing_the_archive_fetcher_fails_the_build(isolated_tree: Path) -> None:
    """The fetcher holds an egress, so reaching it is reaching the client one hop later."""
    module = isolated_tree / "src" / "fking" / "execution" / "__init__.py"
    _append(module, "\nfrom fking.data.archive import ArchiveFetcher\n\n_F = ArchiveFetcher\n")

    result = _lint_imports(isolated_tree, isolated_tree / "src")

    assert result.returncode != 0
    assert ORDER_PATH_CONTRACT in result.stdout
    assert "fking.execution -> fking.data.archive" in result.stdout


def test_the_archive_client_importing_a_credential_source_fails_the_build(
    isolated_tree: Path,
) -> None:
    module = isolated_tree / "src" / "fking" / "platform" / "safety" / "archive.py"
    _append(module, ARCHIVE_REACHES_CREDENTIALS)

    result = _lint_imports(isolated_tree, isolated_tree / "src")

    assert result.returncode != 0, (
        f"the archive client reached the settings tree and lint-imports passed:\n{result.stdout}"
    )
    assert CREDENTIAL_CONTRACT in result.stdout
    assert "BROKEN" in result.stdout


def test_the_archive_fetcher_importing_a_credential_source_fails_the_build(
    isolated_tree: Path,
) -> None:
    module = isolated_tree / "src" / "fking" / "data" / "archive.py"
    _append(module, ARCHIVE_REACHES_CREDENTIALS)

    result = _lint_imports(isolated_tree, isolated_tree / "src")

    assert result.returncode != 0
    assert CREDENTIAL_CONTRACT in result.stdout
    assert "fking.data.archive -> fking.platform.config" in result.stdout


def test_the_archive_client_is_not_re_exported_from_the_safety_package() -> None:
    """The contract only has something to forbid while the import must be spelled out.

    A re-export from `fking.platform.safety.__init__` would put the archive client one
    attribute access away from every module that already imports the trading kernel,
    and no import edge would record it -- so the contract above would keep passing
    while the property it protects had gone.
    """
    package_init = (REPO_ROOT / "src" / "fking" / "platform" / "safety" / "__init__.py").read_text(
        encoding="utf-8"
    )
    assert "archive" not in package_init, (
        "fking.platform.safety.__init__ mentions the archive module; re-exporting it "
        "defeats the import contract in tests/adversarial/test_archive_egress_contract.py"
    )
