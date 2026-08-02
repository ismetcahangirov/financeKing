"""The safety kernel's exception.

It lives here rather than in a shared taxonomy module so that the kernel needs no
import from outside itself, and so that it sits inside the 100% coverage boundary
instead of an 80% one. When `fking.platform.errors` arrives it should re-export this
name, not redefine it -- two classes with one name is how an `except` clause stops
catching what its author believed.
"""

from __future__ import annotations


class SafetyViolation(BaseException):
    """A request was addressed to a host outside the compiled-in allowlist.

    Inherits `BaseException` rather than `Exception`, and this is the single most
    important line in the module.

    CLAUDE.md section 4 forbids catching bare `Exception` to keep a loop alive. That
    rule will be violated eventually, somewhere, in a retry loop written at 2am -- and
    `ccxt`, `httpx` and the async machinery already contain such handlers, written by
    people with no knowledge of this project's prime directive. A `SafetyViolation`
    derived from `Exception` could be caught inside one of them, logged as a network
    problem, and retried against the non-allowlisted host.

    Deriving from `BaseException` makes that impossible without somebody writing
    `except BaseException` on purpose, which `tools/checks/no_catch_safety.py` rejects
    outright. Nothing in `src/fking/` catches this. The process dies.
    """
