"""The type system is doing the reviewing. This is the test that proves it still is.

`Ceiling` and `Floor` exist so that the uniform-comparison version of the validator --
one mapping, one loop, one `>` -- cannot be written. That claim is about `mypy`, not
about runtime, so nothing at runtime can check it: the spellings asserted here are
exactly the spellings that fail to compile, so they cannot appear in an ordinary test
module without breaking `make types` for the whole repository.

So `mypy --strict` runs as a subprocess over generated snippets, and both directions
are asserted. A harness that analysed nothing would report every snippet as rejected
and look exactly like a working guard, which is why `baseline_ok.py` -- the correct
spelling of every construct below -- must come back clean in the same run.

No snippet carries a `# type: ignore`. An ignore would make the file compile and prove
the opposite of what is wanted.

`RISK_PHILOSOPHY.md` section 9 property 3.
"""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path
from typing import Final

import pytest

pytestmark = [pytest.mark.unit, pytest.mark.slow]

REPO_ROOT: Final[Path] = Path(__file__).resolve().parents[2]

_PREAMBLE: Final[str] = (
    "from decimal import Decimal\n"
    "\n"
    "from fking.risk.ceilings import (\n"
    "    HARD_CEILINGS,\n"
    "    HARD_FLOORS,\n"
    "    assert_above_floors,\n"
    "    assert_within_ceilings,\n"
    ")\n"
    "from fking.risk.limits import RiskLimits\n"
    "\n"
    "SUBMITTED = {'max_leverage': Decimal('2'), 'conviction_floor': Decimal('0.15')}\n"
    "\n"
)

# The correct spelling of every construct the rejected snippets get wrong. If this
# stops compiling, the module is unusable and the rejections below prove nothing.
BASELINE: Final[str] = (
    "OVER = HARD_CEILINGS['max_leverage'].is_exceeded_by(Decimal('9'))\n"
    "UNDER = HARD_FLOORS['conviction_floor'].is_undercut_by(Decimal('0'))\n"
    "assert_within_ceilings(SUBMITTED, HARD_CEILINGS, scope='risk')\n"
    "assert_above_floors(SUBMITTED, HARD_FLOORS, scope='risk')\n"
    "LIMITS = RiskLimits(kill_switch_enabled=True, require_invalidation_level=True)\n"
)

# Each entry is one way of writing the bug this design exists to prevent, paired with
# the mypy error code that must reject it. The code is asserted, not just the exit
# status: a snippet rejected for the wrong reason -- a typo, a missing import -- would
# otherwise count as a pass and the guard would be gone.
REJECTED: Final[dict[str, tuple[str, str]]] = {
    # The bug itself: a floor compared with the ceiling operator. This is the line a
    # reviewer is being asked to catch inside a loop over a dictionary, and the reason
    # Floor is a class rather than a NewType over Decimal -- a NewType inherits
    # Decimal's operators and this compiles.
    "floor_compared_with_gt.py": (
        "BREACHED = HARD_FLOORS['conviction_floor'] > Decimal('0.15')\n",
        "operator",
    ),
    # The mirror, for symmetry: a ceiling compared with the floor operator.
    "ceiling_compared_with_lt.py": (
        "BREACHED = HARD_CEILINGS['max_leverage'] < Decimal('2')\n",
        "operator",
    ),
    # One mapping fed to the other's validator -- the uniform-loop refactor, arriving
    # as a one-line edit that looks like a simplification.
    "floors_passed_to_the_ceiling_validator.py": (
        "assert_within_ceilings(SUBMITTED, HARD_FLOORS, scope='risk')\n",
        "arg-type",
    ),
    "ceilings_passed_to_the_floor_validator.py": (
        "assert_above_floors(SUBMITTED, HARD_CEILINGS, scope='risk')\n",
        "arg-type",
    ),
    # A gate switched off. CLAUDE.md section 11: a config flag that bypasses a gate.
    "kill_switch_disabled.py": (
        "LIMITS = RiskLimits(kill_switch_enabled=False)\n",
        "arg-type",
    ),
    "invalidation_requirement_disabled.py": (
        "LIMITS = RiskLimits(require_invalidation_level=False)\n",
        "arg-type",
    ),
}


@pytest.fixture(scope="module")
def mypy_report(tmp_path_factory: pytest.TempPathFactory) -> str:
    """One `mypy --strict` pass over every snippet, rejected and baseline alike.

    One invocation rather than one per snippet: mypy spends most of its time building
    the `fking` graph, and paying that once keeps the whole file under a few seconds.

    `--config-file=` with no argument disables config discovery entirely. Without it
    mypy would walk up from the snippet directory, and whether it found the repository's
    `pyproject.toml` would depend on where pytest put the temporary directory -- which
    is exactly the kind of environment-dependent gate that reports a phantom result.
    `--follow-imports=silent` keeps errors from inside `fking` itself out of the report,
    so this test fails for its own reason and not for someone else's.
    """
    workspace = tmp_path_factory.mktemp("ceiling_types")
    (workspace / "baseline_ok.py").write_text(_PREAMBLE + BASELINE, encoding="utf-8")
    for filename, (snippet, _code) in REJECTED.items():
        (workspace / filename).write_text(_PREAMBLE + snippet, encoding="utf-8")

    environment = dict(os.environ)
    environment["MYPYPATH"] = str(REPO_ROOT / "src")
    completed = subprocess.run(
        [
            sys.executable,
            "-m",
            "mypy",
            "--strict",
            "--config-file=",
            "--no-incremental",
            "--follow-imports=silent",
            "--no-error-summary",
            ".",
        ],
        cwd=workspace,
        env=environment,
        capture_output=True,
        text=True,
        # Explicit UTF-8 rather than the locale codec `text=True` would otherwise pick:
        # on a Windows console defaulting to cp1252 a non-ASCII byte in mypy's output
        # raises during decode, and the test then fails for a reason unrelated to types.
        encoding="utf-8",
        check=False,
    )
    assert completed.returncode != 0, (
        "mypy accepted every snippet, including the ones that must not compile; the guard is gone"
    )
    return completed.stdout


def _lines_for(report: str, filename: str) -> list[str]:
    return [line for line in report.splitlines() if line.startswith(filename)]


def test_the_correct_spelling_compiles(mypy_report: str) -> None:
    """Without this, every assertion below is satisfied by a harness that never ran."""
    assert _lines_for(mypy_report, "baseline_ok.py") == []


@pytest.mark.parametrize(("filename", "code"), [(k, v[1]) for k, v in REJECTED.items()])
def test_the_wrong_spelling_is_rejected(mypy_report: str, filename: str, code: str) -> None:
    reported = _lines_for(mypy_report, filename)
    assert reported, f"mypy --strict accepted {filename}, which must not compile"
    assert any(f"[{code}]" in line for line in reported), (
        f"{filename} was rejected, but not for the reason claimed (wanted [{code}], got {reported})"
    )


def test_no_snippet_silences_the_type_checker() -> None:
    """A `# type: ignore` anywhere here would make the file compile and prove the
    opposite of what this module asserts."""
    sources = [_PREAMBLE, BASELINE, *(snippet for snippet, _ in REJECTED.values())]
    assert not any("type: ignore" in source for source in sources)
