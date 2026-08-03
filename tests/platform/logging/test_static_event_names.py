"""No log call in `src/fking` interpolates its event name.

`OBSERVABILITY.md` section 6: the static message *is* the event type, and it is what a
Loki selector matches on. `logger.info(f"submitted {qty} {symbol}")` produces a stream you
can grep and cannot aggregate -- you cannot ask for the p99 submitted quantity without a
regex over your own log format, and the regex breaks the first time somebody reorders the
sentence.

ruff's `G004` catches this for stdlib loggers and does not recognise a structlog logger,
which is every logger in this codebase. So it is checked here, over the AST, where a
logger the linter has never heard of is still a call on a name bound to `get_logger`.
"""

from __future__ import annotations

import ast
import pathlib
from typing import Final

import pytest

pytestmark = pytest.mark.unit

PACKAGE_ROOT: Final[pathlib.Path] = pathlib.Path(__file__).resolve().parents[3] / "src" / "fking"

_LOG_METHODS: Final[frozenset[str]] = frozenset(
    {"debug", "info", "warning", "error", "critical", "exception", "msg", "log"}
)

# The names modules bind their logger to. Deriving this from assignments would catch more
# and would also catch `self.info(...)` on unrelated objects; the convention in this
# repository is one module-level logger called `_LOG`, and a second spelling is itself
# worth a review comment.
_LOGGER_NAMES: Final[frozenset[str]] = frozenset({"_LOG", "log", "logger", "LOG"})


def _interpolated_event_names(tree: ast.AST) -> list[int]:
    offending: list[int] = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        func = node.func
        if not isinstance(func, ast.Attribute) or func.attr not in _LOG_METHODS:
            continue
        if not isinstance(func.value, ast.Name) or func.value.id not in _LOGGER_NAMES:
            continue
        if not node.args:
            continue
        first = node.args[0]
        # A constant string is the event type. An IfExp between two constants is two
        # event types chosen by a branch, which is still two selectable names.
        if isinstance(first, ast.Constant) and isinstance(first.value, str):
            continue
        if isinstance(first, ast.IfExp) and all(
            isinstance(branch, ast.Constant) and isinstance(branch.value, str)
            for branch in (first.body, first.orelse)
        ):
            continue
        offending.append(node.lineno)
    return offending


@pytest.mark.parametrize("path", sorted(PACKAGE_ROOT.rglob("*.py")), ids=str)
def test_no_log_call_interpolates_its_event_name(path: pathlib.Path) -> None:
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    lines = _interpolated_event_names(tree)
    assert not lines, (
        f"{path} interpolates a log event name at line(s) {lines}. The first argument is "
        f"a query key; put the values in fields."
    )


def test_the_check_catches_a_known_violation() -> None:
    """If this passes, the check above is decorative and every other result here is void."""
    source = 'def f(qty):\n    _LOG.info(f"submitted {qty}")\n'
    assert _interpolated_event_names(ast.parse(source)) == [2]
