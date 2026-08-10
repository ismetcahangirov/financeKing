"""There is one sanctioned blind except, and this is what keeps it at one.

`docs/rules/error-handling.md` grants `fking.platform.supervisor.run` the only
`except Exception` in this codebase. Ruff's BLE001 enforces that everywhere else, which
means the rule survives exactly as long as nobody suppresses BLE001 in a second place --
and a suppression is one comment, added by someone who has a loop they want to keep
alive and a deadline. Counting them is the check that a linter cannot perform on itself.

The count is asserted over `src/` because that is where the rule bites. The three
suppressions in the test harness are enumerated below with the reason each exists; a
fourth, anywhere, fails this test.
"""

from __future__ import annotations

from pathlib import Path
from typing import Final

import pytest

pytestmark = pytest.mark.unit

REPO_ROOT: Final[Path] = Path(__file__).resolve().parents[2]
SCANNED_ROOTS: Final[tuple[str, ...]] = ("src", "tests", "tools")

# Assembled rather than written, so this file does not count itself.
MARKER: Final[str] = "# noqa: " + "BLE" + "001"

# The one sanctioned site. `docs/rules/error-handling.md` names the module and the
# function; the handler trips the kill switch, flattens the book, writes the fatal audit
# row and exits non-zero.
SANCTIONED: Final[Path] = REPO_ROOT / "src" / "fking" / "platform" / "supervisor.py"

# The test harness, where a broad catch is bootstrap rather than control flow: starting a
# Docker container and opening a first connection fail through docker-py, requests, the
# driver and the socket layer alike, and every one of them means the same thing -- there
# is no server, say so and skip. Narrowing these would convert an explained skip into an
# unexplained crash on a developer machine with Docker stopped. None of them is in the
# order path, and none of them keeps a loop alive.
HARNESS_EXEMPTIONS: Final[frozenset[Path]] = frozenset(
    {
        REPO_ROOT / "tests" / "conftest.py",
        REPO_ROOT / "tests" / "platform" / "bus" / "conftest.py",
    }
)


def _python_files() -> list[Path]:
    return [
        path
        for root in SCANNED_ROOTS
        for path in sorted((REPO_ROOT / root).rglob("*.py"))
        if path != Path(__file__).resolve()
    ]


def _suppressions() -> dict[Path, int]:
    found: dict[Path, int] = {}
    for path in _python_files():
        occurrences = path.read_text(encoding="utf-8").count(MARKER)
        if occurrences:
            found[path] = occurrences
    return found


def test_src_holds_exactly_one_blind_except_and_it_is_the_supervisor() -> None:
    """The acceptance criterion of issue #110, asserted as a count rather than a review."""
    in_src = {path: count for path, count in _suppressions().items() if "src" in path.parts}

    assert in_src == {SANCTIONED: 1}


def test_no_blind_except_is_suppressed_outside_the_two_known_places() -> None:
    """A fourth suppression anywhere in the tree fails here, with its path named.

    Adding one is a decision, not an oversight, and the correct place to argue for it is
    a pull request that also edits this list.
    """
    unexpected = {
        str(path.relative_to(REPO_ROOT))
        for path in _suppressions()
        if path != SANCTIONED and path not in HARNESS_EXEMPTIONS
    }

    assert unexpected == set()


def test_the_sanctioned_suppression_sits_on_the_handler_it_documents() -> None:
    """A suppression that drifts onto a different statement suppresses a different rule.

    Asserting the text of the line rather than only its presence: `# noqa` is line-scoped,
    so moving the comment down one line during a refactor silently re-arms BLE001 on the
    handler and disarms it on whatever it landed on.
    """
    lines = SANCTIONED.read_text(encoding="utf-8").splitlines()
    (line,) = [text for text in lines if MARKER in text]

    assert line.strip().startswith("except Exception as err:")
