"""The read loop. It contains no `try` and no `except`, and that is the whole module.

`DATA_PIPELINE.md` section 5: **the stream is never restarted by catching an exception
inside the read loop. The supervisor restarts the session; the loop does not defend
itself.** `CLAUDE.md` section 4 states the reason in general terms -- a swallowed
exception converts a visible failure into silent wrong data -- and the live path is
where that costs the most, because the process keeps looking healthy while the system's
view of the market stops moving.

The loop is a module of its own so the rule is checkable rather than reviewed:
`tests/data/test_live_read_loop.py` parses this file's AST and asserts it holds no
handler at all. A loop that lives beside its own reconnect logic will eventually acquire
a `continue`, and nobody reviewing that diff will be looking for it.

`WebSocketConnection` is a `Protocol` with one method rather than an import of
`websockets.asyncio.client.ClientConnection`, for two reasons. `fking.data` may not
import `websockets` -- only the safety kernel constructs transports, and an
`import-linter` contract enforces it. And a test replaying recorded frames needs
something to be, which is exactly the "mock the exchange, against recorded real
responses" seam `.claude/rules/testing-rules.md` requires.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from typing import Protocol, runtime_checkable

__all__ = ["WebSocketConnection", "read_frames"]


@runtime_checkable
class WebSocketConnection(Protocol):
    """The one thing this module needs from a socket: the next frame, or an exception."""

    async def recv(self) -> str | bytes: ...


async def read_frames(connection: WebSocketConnection) -> AsyncIterator[str]:
    """Yield decoded frames until the connection raises.

    Every failure -- a closed connection, a reset, a timeout, a decode error -- leaves
    this generator by propagating. There is deliberately no place here to put a retry, a
    `continue`, or a log-and-carry-on.

    Frames are decoded as UTF-8 with no error handling, so a frame that is not valid
    UTF-8 raises `UnicodeDecodeError` rather than arriving at the parser with replacement
    characters where a price used to be.
    """
    while True:
        raw = await connection.recv()
        yield raw if isinstance(raw, str) else raw.decode("utf-8")
