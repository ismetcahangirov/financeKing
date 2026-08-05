"""Every CI gate must reject its own violation.

Issue #15 requires that "a deliberately broken commit is proven to fail the correct
gate". Proving it once by pushing a broken branch proves it for that afternoon. This
asserts it on every run, and it asserts the pairing as well as the failure: a gate
that rejects everything is as useless as one that rejects nothing, so each case
checks that the *clean* form of the same input passes.

The commands here are the ones the workflow runs, not approximations of them. A test
that exercises a paraphrase of the gate tells you nothing about the gate.
"""

from __future__ import annotations

import shutil
import subprocess
import sys
from collections.abc import Sequence
from pathlib import Path

import pytest

pytestmark = [pytest.mark.integration, pytest.mark.slow]

REPO_ROOT = Path(__file__).resolve().parents[2]


def _run(command: Sequence[str], cwd: Path) -> subprocess.CompletedProcess[str]:
    # encoding="utf-8" rather than the locale codec `text=True` would pick: these
    # subprocesses print non-ASCII (ruff's arrows, import-linter's banner), and on a
    # Windows console defaulting to cp1252 the decode raises before the assertion runs
    # -- so the gate reports a failure the gate itself caused (#141).
    return subprocess.run(
        list(command), cwd=cwd, capture_output=True, text=True, encoding="utf-8", check=False
    )


@pytest.fixture
def sandbox(tmp_path: Path) -> Path:
    """A copy of the package the gates can be pointed at without touching the tree."""
    shutil.copytree(REPO_ROOT / "src" / "fking", tmp_path / "src" / "fking")
    return tmp_path


# --------------------------------------------------------------------------
# `checks` gate -- the AST checks for rules no linter can express
# --------------------------------------------------------------------------

# (check script, package to poison, violating source, why it is a violation)
AST_GATE_CASES: tuple[tuple[str, str, str, str], ...] = (
    (
        "money_types.py",
        "domain",
        "def settle(notional_usd: float) -> None: ...\n",
        "a money-shaped name annotated float",
    ),
    (
        "clock_isolation.py",
        "risk",
        "from datetime import UTC, datetime\n\nas_of = datetime.now(UTC)\n",
        "a wall-clock read inside risk",
    ),
    (
        "clock_isolation.py",
        "backtest",
        "from datetime import UTC, datetime\n\nas_of = datetime.now(UTC)\n",
        "a wall-clock read inside backtest",
    ),
    (
        "no_catch_safety.py",
        "execution",
        "try:\n    pass\nexcept SafetyViolation:\n    pass\n",
        "catching SafetyViolation",
    ),
    (
        "naming.py",
        "execution",
        "def submit(size) -> None: ...\n",
        "an ambiguous trading noun as an identifier",
    ),
)


@pytest.mark.parametrize(
    ("script", "package", "source", "description"),
    AST_GATE_CASES,
    ids=[case[3] for case in AST_GATE_CASES],
)
def test_ast_gate_rejects_its_violation(
    sandbox: Path, script: str, package: str, source: str, description: str
) -> None:
    target = sandbox / "src" / "fking" / package / "poisoned.py"
    command = [sys.executable, str(REPO_ROOT / "tools" / "checks" / script), "src/fking"]

    clean = _run(command, cwd=sandbox)
    assert clean.returncode == 0, f"the unmodified tree already fails {script}:\n{clean.stderr}"

    target.write_text(source, encoding="utf-8")
    poisoned = _run(command, cwd=sandbox)

    assert poisoned.returncode == 1, f"{script} accepted {description}"
    assert "poisoned.py" in poisoned.stderr, (
        f"{script} failed but did not name the offending file:\n{poisoned.stderr}"
    )


# --------------------------------------------------------------------------
# `fast` gate -- ruff
# --------------------------------------------------------------------------


def test_lint_gate_rejects_an_unformatted_file(sandbox: Path) -> None:
    (sandbox / "src" / "fking" / "domain" / "ugly.py").write_text("x    =    1\n", encoding="utf-8")
    result = _run(
        [sys.executable, "-m", "ruff", "format", "--check", "--isolated", "src"], cwd=sandbox
    )
    assert result.returncode != 0, "ruff format --check accepted an unformatted file"


def test_lint_gate_rejects_a_bare_except(sandbox: Path) -> None:
    """BLE001. The rule that stops a visible failure becoming silent wrong behaviour."""
    (sandbox / "src" / "fking" / "execution" / "swallow.py").write_text(
        "def go() -> None:\n    try:\n        pass\n    except Exception:\n        pass\n",
        encoding="utf-8",
    )
    result = _run(
        [sys.executable, "-m", "ruff", "check", "--isolated", "--select", "BLE", "src"],
        cwd=sandbox,
    )
    assert result.returncode != 0, "ruff accepted a blind `except Exception`"
    assert "BLE001" in result.stdout


# --------------------------------------------------------------------------
# `types` gate
# --------------------------------------------------------------------------


def test_type_gate_rejects_an_unannotated_function(sandbox: Path) -> None:
    (sandbox / "src" / "fking" / "domain" / "untyped.py").write_text(
        "def compute(a, b):\n    return a + b\n", encoding="utf-8"
    )
    result = _run(
        [sys.executable, "-m", "mypy", "--strict", "--no-incremental", "src"], cwd=sandbox
    )
    assert result.returncode != 0, "mypy --strict accepted an unannotated function"
    assert "untyped.py" in result.stdout


# --------------------------------------------------------------------------
# `test` gate -- the per-module coverage floors
# --------------------------------------------------------------------------


def test_coverage_floor_gate_rejects_an_uncovered_risk_module(tmp_path: Path) -> None:
    """A module that exists but is never exercised must breach its floor.

    This is the case a global coverage number hides: `risk` can sit at 0% while the
    aggregate stays comfortable because well-tested utilities carry it.
    """
    package = tmp_path / "src" / "fking" / "risk"
    package.mkdir(parents=True)
    (package / "__init__.py").write_text("", encoding="utf-8")
    # Ten uncovered statements, so the floor cannot be met by accident.
    (package / "sizing.py").write_text(
        "".join(f"CONSTANT_{index} = {index}\n" for index in range(10)), encoding="utf-8"
    )
    (tmp_path / ".coveragerc").write_text(
        "[run]\nbranch = True\nsource = src/fking\n", encoding="utf-8"
    )
    # Measure a process that imports nothing: every statement stays unexecuted.
    _run([sys.executable, "-m", "coverage", "run", "-c", "pass"], cwd=tmp_path)

    result = _run(
        [
            sys.executable,
            "-m",
            "coverage",
            "report",
            "--include=src/fking/risk/*",
            "--fail-under=95",
        ],
        cwd=tmp_path,
    )
    assert result.returncode != 0, "a 0%-covered risk module satisfied the 95% floor"
