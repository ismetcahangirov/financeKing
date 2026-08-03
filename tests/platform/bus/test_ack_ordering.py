"""`XACK` is never reachable inside an open transaction, anywhere in `src/fking`.

The fault-injection test in `test_stream.py` proves the ordering for the path that exists.
This proves it for the paths not yet written -- every consumer #21 onwards adds -- because
the mistake is a two-line edit that no type checker sees and whose symptom is a fill that
the exchange recorded and our books do not have, permanently.
"""

from __future__ import annotations

import ast
import pathlib
from typing import Final

import pytest

pytestmark = pytest.mark.unit

PACKAGE_ROOT: Final[pathlib.Path] = pathlib.Path(__file__).resolve().parents[3] / "src" / "fking"

_TRANSACTION_OPENERS: Final[frozenset[str]] = frozenset({"begin", "begin_nested"})


def _acks_inside_a_transaction(tree: ast.AST) -> list[int]:
    offending: list[int] = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.AsyncWith | ast.With):
            continue
        opens_transaction = any(
            isinstance(item.context_expr, ast.Call)
            and isinstance(item.context_expr.func, ast.Attribute)
            and item.context_expr.func.attr in _TRANSACTION_OPENERS
            for item in node.items
        )
        if not opens_transaction:
            continue
        offending.extend(
            inner.lineno
            for inner in ast.walk(node)
            if isinstance(inner, ast.Attribute) and inner.attr == "xack"
        )
    return offending


@pytest.mark.parametrize("path", sorted(PACKAGE_ROOT.rglob("*.py")), ids=str)
def test_no_module_acknowledges_a_message_inside_an_open_transaction(
    path: pathlib.Path,
) -> None:
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    lines = _acks_inside_a_transaction(tree)
    assert not lines, (
        f"{path}: xack inside an open transaction at line(s) {lines}. A crash after the "
        f"ack and before the commit removes the message from the PEL and rolls back the "
        f"effect -- the event is then gone, and no retry brings it back."
    )


def test_the_check_catches_a_known_violation() -> None:
    """If this passes, the check above is decorative and its result means nothing."""
    source = (
        "async def f(session, redis, stream, group, message_id):\n"
        "    async with session.begin():\n"
        "        await redis.xack(stream, group, message_id)\n"
    )
    assert _acks_inside_a_transaction(ast.parse(source)) == [3]
