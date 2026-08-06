"""Validation shared by the declarative types in this package.

Deliberately not `fking.domain._guards`. That module is private to `domain` for the
reason `.claude/rules/module-boundaries.md` gives -- a leading underscore means the
package promises nothing about it -- and reaching across a package boundary into one
would make a `domain` refactor break `strategy` silently. The duplication is four
functions; the coupling it avoids is permanent.

They raise `StrategyContractError` rather than `DomainError` because what they are
checking is a *declaration*, not a domain value. A malformed spec and a malformed `Bar`
are different failures with different fixes, and one exception type for both would send
the reader to the wrong file.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal

from fking.strategy._errors import StrategyContractError

__all__ = [
    "require_open_unit_fraction",
    "require_positive_duration",
    "require_positive_int",
    "require_text",
    "require_utc",
]


def require_text(candidate: str, field_name: str) -> str:
    """Non-blank text. Whitespace is not a declaration."""
    if not candidate.strip():
        raise StrategyContractError(f"{field_name} must not be blank")
    return candidate


def require_utc(moment: datetime, field_name: str) -> datetime:
    """A timezone-aware UTC instant.

    A non-UTC aware datetime is rejected rather than converted. `astimezone(UTC)` would
    silently accept a value whose offset was guessed wrong upstream; raising forces the
    guess to be made where the data enters (`.claude/rules/time-and-timezones.md`).
    """
    if moment.tzinfo is None or moment.utcoffset() is None:
        raise StrategyContractError(f"{field_name} must be timezone-aware; got naive {moment!r}")
    if moment.utcoffset() != UTC.utcoffset(None):
        raise StrategyContractError(f"{field_name} must be UTC; got offset {moment.utcoffset()!r}")
    return moment


def require_positive_duration(candidate: timedelta, field_name: str) -> timedelta:
    """A strictly positive duration. Zero is not a horizon and not an interval."""
    if candidate <= timedelta(0):
        raise StrategyContractError(f"{field_name} must be positive; got {candidate}")
    return candidate


def require_positive_int(candidate: int, field_name: str) -> int:
    """A strictly positive whole number, and never a `bool`.

    `bool` is a subclass of `int`, so `warm_up_bars=True` would otherwise declare a
    one-bar warm-up and read as a flag somebody meant to set elsewhere.
    """
    if isinstance(candidate, bool) or not isinstance(candidate, int):
        raise StrategyContractError(f"{field_name} must be an int; got {candidate!r}")
    if candidate < 1:
        raise StrategyContractError(f"{field_name} must be at least 1; got {candidate}")
    return candidate


def require_open_unit_fraction(candidate: Decimal, field_name: str) -> Decimal:
    """A fraction strictly inside `(0, 1)`.

    Both ends are excluded and both exclusions are load-bearing. Zero says the thesis is
    wrong the instant it is taken, which is not a thesis. One says a long is invalidated
    at a price of zero, which is never reached, so the strategy would never be proved
    wrong -- and an unfalsifiable strategy is exactly what the invalidation level exists
    to prevent.
    """
    if not isinstance(candidate, Decimal):
        raise StrategyContractError(
            f"{field_name} must be a Decimal constructed from a string; got {candidate!r}"
        )
    if not Decimal("0") < candidate < Decimal("1"):
        raise StrategyContractError(
            f"{field_name} must lie strictly between 0 and 1; got {candidate}"
        )
    return candidate
