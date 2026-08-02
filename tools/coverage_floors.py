"""Enforce per-module coverage floors against the data pytest-cov just wrote.

coverage.py has exactly one `fail_under`, so the floors are enforced as separate
report passes over one data file.

Per-module floors exist because a single global number lets well-tested utilities
subsidise untested risk logic: a change that raises the aggregate while dropping
`risk` from 96% to 94% must fail, and against one number it does not.

A floor whose module does not exist yet is reported as SKIPPED rather than passing
silently. A floor whose module exists but produced no coverage data is a failure --
that is the case where a module was added and never exercised, which is precisely
what a floor is for.
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path
from typing import Final

REPO_ROOT: Final[Path] = Path(__file__).resolve().parents[1]
PACKAGE_ROOT: Final[Path] = REPO_ROOT / "src" / "fking"

# CLAUDE.md section 5. platform/safety is 100% because every uncovered line there is a
# line that might widen the host allowlist. execution is 90% rather than 95% because
# venue-error enumeration has genuinely unreachable arms.
FLOORS: Final[tuple[tuple[str, int], ...]] = (
    ("platform/safety", 100),
    ("risk", 95),
    ("domain", 95),
    ("execution", 90),
)
REMAINDER_FLOOR: Final[int] = 80


def _report(*, include: str | None, omit: str | None, floor: int) -> int:
    command = [sys.executable, "-m", "coverage", "report", f"--fail-under={floor}"]
    if include is not None:
        command.append(f"--include={include}")
    if omit is not None:
        command.append(f"--omit={omit}")
    completed = subprocess.run(command, cwd=REPO_ROOT, check=False)  # noqa: S603
    return completed.returncode


def main() -> int:
    if not (REPO_ROOT / ".coverage").exists():
        print(
            "no coverage data found; run `make test` before `make cover`",
            file=sys.stderr,
        )
        return 1

    breaches: list[str] = []
    for module, floor in FLOORS:
        if not (PACKAGE_ROOT / module).is_dir():
            # Loudly, not silently: a floor that quietly evaporates reads as
            # "covered" when the module simply has not been written yet.
            print(
                f"== {module} (floor {floor}%) SKIPPED -- src/fking/{module} does not exist yet",
                flush=True,
            )
            continue
        # flush before handing stdout to a subprocess: Python block-buffers when piped,
        # so without this every header prints after the table it labels.
        print(f"== {module} (floor {floor}%)", flush=True)
        if _report(include=f"src/fking/{module}/*", omit=None, floor=floor) != 0:
            breaches.append(f"src/fking/{module} below {floor}%")

    print(f"== everything else (floor {REMAINDER_FLOOR}%)", flush=True)
    remainder_omit = ",".join(f"src/fking/{module}/*" for module, _ in FLOORS)
    if _report(include=None, omit=remainder_omit, floor=REMAINDER_FLOOR) != 0:
        breaches.append(f"the remainder is below {REMAINDER_FLOOR}%")

    for breach in breaches:
        print(f"FLOOR BREACH: {breach}", file=sys.stderr)
    return 1 if breaches else 0


if __name__ == "__main__":
    raise SystemExit(main())
