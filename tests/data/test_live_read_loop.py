"""The read loop does not defend itself, proved against the AST rather than by review.

`DATA_PIPELINE.md` section 5 and `CLAUDE.md` section 4 both state the rule: the
supervisor restarts the session; the loop does not catch its way out of a failure. A
swallowed exception in a market-data reader converts a visible failure into silent wrong
data while the process keeps reporting health, and the specific shape it takes is
`except ...: continue` inside the frame loop.

`tools/checks/no_catch_safety.py` already forbids catching `BaseException` and
`SafetyViolation` anywhere. It cannot express this rule, because `except
ConnectionClosed: continue` is legitimate in plenty of code and fatal here.

Two assertions, and the second is the one with a future:

1. `session.py` holds no handler at all, so the loop has nowhere to acquire one.
2. No handler *anywhere* in `fking.data.live` resumes iteration. That is what keeps the
   rule true when someone later adds a second loop somewhere in the package -- which is
   exactly the diff nobody will be reading with this rule in mind.
"""

from __future__ import annotations

import ast
from pathlib import Path
from typing import Final

import pytest

import fking.data.live
from fking.data.live import session

pytestmark = pytest.mark.unit

LIVE_PACKAGE_ROOT: Final[Path] = Path(fking.data.live.__file__).parent
LIVE_MODULES: Final[tuple[Path, ...]] = tuple(sorted(LIVE_PACKAGE_ROOT.rglob("*.py")))
SESSION_MODULE: Final[Path] = Path(session.__file__)

# `continue` resumes the loop the handler sits inside; `pass` falls through to the next
# iteration of an enclosing loop, which is the same thing spelled more quietly.
RESUMING_STATEMENTS: Final[tuple[type[ast.stmt], ...]] = (ast.Continue, ast.Pass)


def _parse(path: Path) -> ast.Module:
    return ast.parse(path.read_text(encoding="utf-8"), filename=str(path))


def _handlers(tree: ast.Module) -> list[ast.ExceptHandler]:
    return [node for node in ast.walk(tree) if isinstance(node, ast.ExceptHandler)]


# frames, detectors, router, session, supervisor, writer, streams, backoff, __init__.
MINIMUM_LIVE_MODULES = 9


def test_the_live_package_has_modules_to_check() -> None:
    """A rglob that matched nothing would make every assertion below vacuous."""
    assert len(LIVE_MODULES) >= MINIMUM_LIVE_MODULES
    assert SESSION_MODULE in LIVE_MODULES


def test_the_read_loop_module_contains_no_exception_handler_at_all() -> None:
    handlers = _handlers(_parse(SESSION_MODULE))
    assert handlers == [], (
        f"{SESSION_MODULE.name} holds {len(handlers)} except clause(s) at lines "
        f"{[handler.lineno for handler in handlers]}; the read loop must propagate every "
        f"failure to the supervisor"
    )


def test_the_read_loop_module_contains_no_try_statement_at_all() -> None:
    """A bare `try/finally` is legitimate elsewhere and is still a place to put a
    handler later, so the read loop is kept free of the statement entirely."""
    tree = _parse(SESSION_MODULE)
    tries = [node for node in ast.walk(tree) if isinstance(node, ast.Try)]
    assert tries == [], f"{SESSION_MODULE.name} holds a try at lines {[t.lineno for t in tries]}"


@pytest.mark.parametrize("module", LIVE_MODULES, ids=lambda path: path.name)
def test_no_handler_in_the_live_package_resumes_iteration(module: Path) -> None:
    offending = [
        handler.lineno
        for handler in _handlers(_parse(module))
        if any(isinstance(node, RESUMING_STATEMENTS) for node in ast.walk(handler))
    ]
    assert offending == [], (
        f"{module.name} resumes a loop from inside an except clause at lines {offending}. "
        f"A stream reader that carries on after an exception it did not understand is "
        f"producing silent wrong data with the process reporting health"
    )


def test_read_frames_is_the_only_frame_loop_in_the_package() -> None:
    """If a second `async for` over a connection appears, it needs this rule applied to
    it explicitly rather than inherited by accident."""
    loops = 0
    for module in LIVE_MODULES:
        for node in ast.walk(_parse(module)):
            if isinstance(node, ast.AsyncFor) and isinstance(node.iter, ast.Call):
                function = node.iter.func
                name = (
                    function.id if isinstance(function, ast.Name) else getattr(function, "attr", "")
                )
                loops += 1 if name == "read_frames" else 0
    assert loops == 1
