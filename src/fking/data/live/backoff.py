"""Reconnect backoff: exponential, full jitter, base 1 s, cap 60 s, unbounded retries.

`DATA_PIPELINE.md` section 5 fixes all four numbers, and each of them is load-bearing:

**Full jitter, not "exponential plus a bit of noise".** Sleeping the full capped
exponential is what synchronises every reconnecting client onto one schedule, so a venue
that dropped every connection at once gets them all back at once -- a thundering herd
against the endpoint that is already struggling. `uniform(0, ceiling)` spreads the
returns across the whole window, and it is the variant AWS measured as strictly better
than equal jitter for total completion time.

**Cap 60 s, not "keep doubling".** Binance closes a healthy connection every 24 hours by
design, so reconnecting is the normal case; an uncapped schedule reaches hours of delay
after a dozen failures and turns a transient outage into a day of missing data.

**Unbounded retries.** There is no attempt count at which giving up is better than
trying again: a market-data session that stops reconnecting produces a system that looks
alive with a frozen view of the world, which is the failure `FAILSAFE.md` calls the
worst available. The gap registry is what makes the outage visible while it lasts.

The RNG is injected. A backoff schedule that reads a module-level `random` cannot be
replayed, and the property test below could not assert anything about a specific
sequence.
"""

from __future__ import annotations

import random
from typing import Final

__all__ = [
    "RECONNECT_BASE_SECONDS",
    "RECONNECT_CAP_SECONDS",
    "reconnect_delay_seconds",
]

RECONNECT_BASE_SECONDS: Final[float] = 1.0
RECONNECT_CAP_SECONDS: Final[float] = 60.0

# 2**63 overflows nothing in Python, but `base * 2**attempt` for a session that has been
# reconnecting for weeks builds a pointlessly large float before min() throws it away.
# The ceiling is already saturated by this attempt for any sane base.
_SATURATED_ATTEMPT: Final[int] = 64


def reconnect_delay_seconds(
    attempt: int,
    *,
    rng: random.Random,
    base_seconds: float = RECONNECT_BASE_SECONDS,
    cap_seconds: float = RECONNECT_CAP_SECONDS,
) -> float:
    """Seconds to wait before reconnect attempt `attempt`, counting from zero.

    Raises:
        ValueError: `attempt` is negative. Counting from zero is the contract, and a
            negative attempt would produce a sub-base ceiling that silently retries
            faster than the schedule allows.
    """
    if attempt < 0:
        raise ValueError(f"attempt counts from zero, got {attempt}")
    exponent = min(attempt, _SATURATED_ATTEMPT)
    ceiling = min(cap_seconds, base_seconds * float(2**exponent))
    return rng.uniform(0.0, ceiling)
