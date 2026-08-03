"""The correlation id: one per causal chain, minted once, propagated everywhere.

`OBSERVABILITY.md` section 3 states the rule this module exists to make mechanical:

> The correlation ID originates at the top -- the market data event -- and propagates
> unchanged through feature computation, signal, risk decision, order, fill and
> evolution scoring. **You may not generate a new correlation ID mid-flow.**

Regenerating it because the context was not available here does not produce a missing
link. It produces two chains that each look complete, which is strictly worse, because
nothing about either one looks broken.

It lives in a leaf module -- importing nothing from `fking` -- because both the logging
pipeline and the tracing helpers need it, and neither may import the other.

A `ContextVar` rather than a parameter threaded through call frames: the binding survives
`await` boundaries and is copied into `asyncio.TaskGroup` children, and a parameter that
thirty call sites must remember to pass will be forgotten by exactly the call site an
investigation needs. It does *not* cross a Redis boundary -- there is no ambient context
on the other side of a stream, which is why the event envelope carries the id as a field.
"""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from contextvars import ContextVar
from typing import Final, Literal
from uuid import UUID

# The one sanctioned literal, for records emitted before any flow exists: configuration
# loading, the allowlist dump, migration status. There is no "unknown", no "n/a" and no
# empty string -- those would make the mandatory field satisfiable everywhere, which is
# the same as not having it. `.claude/rules/logging-rules.md`, the one exception.
BOOT: Final[Literal["boot"]] = "boot"

type CorrelationId = UUID | Literal["boot"]

_CURRENT: Final[ContextVar[str | None]] = ContextVar("fking_correlation_id", default=None)


class MissingCorrelationIdError(RuntimeError):
    """Work was performed with no correlation scope active.

    Raised rather than defaulted. A default would let a decision be recorded under an id
    that ties nothing to anything, and the log would look complete.
    """


@contextmanager
def correlation_scope(correlation_id: CorrelationId) -> Iterator[str]:
    """Bind `correlation_id` for the duration of the block, then restore the previous one.

    Restoring rather than clearing matters for nested scopes: a scheduled job that opens
    its own root scope inside a request must not leave the request's frames unlabelled
    when it returns.
    """
    rendered = str(correlation_id)
    if not rendered:
        raise MissingCorrelationIdError("correlation_scope was given an empty id")
    token = _CURRENT.set(rendered)
    try:
        yield rendered
    finally:
        _CURRENT.reset(token)


def current_correlation_id() -> str | None:
    """The id bound by the innermost active scope, or None."""
    return _CURRENT.get()


def require_current_correlation_id(*, at: str) -> str:
    """The active id, or `MissingCorrelationId` naming the operation that wanted one."""
    active = _CURRENT.get()
    if active is None:
        raise MissingCorrelationIdError(
            f"{at} ran with no correlation scope active. Open one at the top of the "
            f"flow with correlation_scope(); never mint an id here"
        )
    return active
