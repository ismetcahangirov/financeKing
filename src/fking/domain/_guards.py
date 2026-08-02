"""Construction-time validators shared by every domain type.

Each takes `object` rather than the type it validates. That is deliberate: annotating
the parameter as `Decimal` would make the `isinstance` check look redundant to a
reader and unreachable to `mypy --warn-unreachable`, and the entire point is that the
value arriving here has not been checked by anything. Annotations are a claim; these
functions are the verification.

Every guard returns the validated value so it can be used in an expression, and every
guard raises rather than coercing. Coercion is what hides the upstream mistake:
`astimezone(UTC)` silently accepts a datetime whose offset was guessed wrong three
modules ago, and `Decimal(str(candidate))` silently accepts a float whose error was
baked in by the parser before this process started.
"""

from __future__ import annotations

from datetime import datetime, timedelta
from decimal import Decimal
from typing import Final

from fking.domain._errors import DomainError

_UTC_OFFSET: Final = timedelta(0)
_ZERO: Final = Decimal("0")
_ONE: Final = Decimal("1")


def require_utc(candidate: object, field_name: str) -> datetime:
    """A timezone-aware datetime whose offset is exactly UTC.

    Rejects rather than converts. An aware datetime in `Europe/Baku` compares fine,
    sorts fine, and is four hours wrong in every bar alignment; converting it here
    would launder a wrong guess into a confident value.
    """
    if not isinstance(candidate, datetime):
        raise DomainError(
            f"{field_name} must be a datetime, got {type(candidate).__name__} {candidate!r}"
        )
    if candidate.tzinfo is None or candidate.utcoffset() is None:
        raise DomainError(f"{field_name} must be timezone-aware; got naive {candidate!r}")
    if candidate.utcoffset() != _UTC_OFFSET:
        raise DomainError(
            f"{field_name} must be UTC; got offset {candidate.utcoffset()!r} in {candidate!r}"
        )
    return candidate


def require_decimal(candidate: object, field_name: str) -> Decimal:
    """An exact, finite `Decimal`.

    `float` is named separately from every other wrong type because it is the one that
    arrives by accident and the one whose error is already present before this call:
    `Decimal(0.1)` is `Decimal('0.1000000000000000055511151231257827021181583404541015625')`
    because the literal was rounded to the nearest double by the parser, not by us.

    `NaN` and the infinities are rejected too. They are legal `Decimal` values, they
    propagate through arithmetic without raising, and `Decimal("NaN") == Decimal("NaN")`
    is False -- so a single one turns every equality-based reconciliation downstream
    into a permanent, unexplained mismatch.
    """
    if isinstance(candidate, float):
        raise DomainError(
            f"{field_name} must be a Decimal constructed from str, not a float; "
            f"got {candidate!r}, which is already rounded"
        )
    if not isinstance(candidate, Decimal):
        raise DomainError(
            f"{field_name} must be a Decimal, got {type(candidate).__name__} {candidate!r}"
        )
    if not candidate.is_finite():
        raise DomainError(f"{field_name} must be finite; got {candidate}")
    return candidate


def require_positive_decimal(candidate: object, field_name: str) -> Decimal:
    """A finite `Decimal` strictly greater than zero."""
    quantity = require_decimal(candidate, field_name)
    if quantity <= _ZERO:
        raise DomainError(f"{field_name} must be positive; got {quantity}")
    return quantity


def require_non_negative_decimal(candidate: object, field_name: str) -> Decimal:
    """A finite `Decimal` of zero or more."""
    quantity = require_decimal(candidate, field_name)
    if quantity < _ZERO:
        raise DomainError(f"{field_name} must not be negative; got {quantity}")
    return quantity


def require_fraction(candidate: object, field_name: str) -> Decimal:
    """A finite `Decimal` in the closed interval [0, 1].

    A dimensionless ratio, never a percent. `.claude/rules/naming.md` reserves `_pct`
    for 0-100 precisely so that a bare 0.02 cannot be read as either 2% or 0.02%.
    """
    fraction = require_decimal(candidate, field_name)
    if not _ZERO <= fraction <= _ONE:
        raise DomainError(f"{field_name} must lie in [0, 1]; got {fraction}")
    return fraction


def require_positive_duration(candidate: object, field_name: str) -> timedelta:
    """A strictly positive `timedelta`."""
    if not isinstance(candidate, timedelta):
        raise DomainError(
            f"{field_name} must be a timedelta, got {type(candidate).__name__} {candidate!r}"
        )
    if candidate <= timedelta(0):
        raise DomainError(f"{field_name} must be positive; got {candidate}")
    return candidate


def require_text(candidate: object, field_name: str) -> str:
    """A non-empty string carrying at least one non-whitespace character.

    An empty `rationale` or `client_order_id` is a field somebody meant to fill in.
    Accepting it produces an audit row that satisfies the schema and answers nothing.
    """
    if not isinstance(candidate, str):
        raise DomainError(
            f"{field_name} must be a string, got {type(candidate).__name__} {candidate!r}"
        )
    if not candidate.strip():
        raise DomainError(f"{field_name} must not be blank")
    return candidate


def require_asset_code(candidate: object, field_name: str) -> str:
    """An uppercase alphanumeric asset code such as `BTC` or `USDT`.

    Deliberately not Unicode-tolerant, and deliberately not a `.upper()` call: the
    exchange's own casing is what must be echoed back on the next request, so a code
    that does not already match is a normalisation bug upstream, not a formatting
    preference here. Symbols are a separate question -- see `Instrument.symbol`, where
    a venue really does serve non-ASCII values.
    """
    code = require_text(candidate, field_name)
    if not code.isascii() or not code.isalnum() or code != code.upper():
        raise DomainError(
            f"{field_name} must be an uppercase ASCII alphanumeric code; got {code!r}"
        )
    return code
