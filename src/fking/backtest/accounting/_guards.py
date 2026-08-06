"""Construction-time validators for the accounting ledger's own types.

A third copy of checks that also exist in `fking.domain._guards` and
`fking.backtest._guards`, for the reason `fking.backtest.feed._request` already gives:
the two existing copies raise error classes belonging to other concerns -- `DomainError`
and `RunConfigError` -- and a malformed opening balance surfacing as a *run configuration*
failure sends the reader to the wrong file. The cost is two short functions; the
alternative is an exception whose type is a lie about where the fault is.

Each takes `object` rather than the type it validates, so the `isinstance` check is not
unreachable to `mypy --warn-unreachable`. The whole point is that the value arriving here
has been checked by nothing.
"""

from __future__ import annotations

from datetime import datetime, timedelta
from decimal import Decimal
from typing import Final

from fking.backtest.accounting._errors import AccountLedgerError

_UTC_OFFSET: Final = timedelta(0)


def require_utc(candidate: object, field_name: str) -> datetime:
    """A timezone-aware datetime whose offset is exactly UTC.

    Rejects rather than converts. The daily grid is defined by UTC midnights, and an
    instant carrying a non-UTC offset lands in the adjacent day's bucket without any
    arithmetic looking wrong -- `astimezone(UTC)` here would silently accept an offset
    that was guessed three modules upstream.
    """
    if not isinstance(candidate, datetime):
        raise AccountLedgerError(
            f"{field_name} must be a datetime, got {type(candidate).__name__} {candidate!r}"
        )
    if candidate.tzinfo is None or candidate.utcoffset() is None:
        raise AccountLedgerError(f"{field_name} must be timezone-aware; got naive {candidate!r}")
    if candidate.utcoffset() != _UTC_OFFSET:
        raise AccountLedgerError(
            f"{field_name} must be UTC; got offset {candidate.utcoffset()!r} in {candidate!r}"
        )
    return candidate


def require_finite_decimal(candidate: object, field_name: str) -> Decimal:
    """An exact, finite `Decimal`. Signed values are allowed; a float is not.

    A `Decimal("NaN")` cash balance propagates through every subsequent addition without
    raising and compares unequal to itself, so one of them turns the whole equity curve
    into a series that fails every threshold comparison silently rather than loudly.
    """
    if isinstance(candidate, float):
        raise AccountLedgerError(
            f"{field_name} must be a Decimal constructed from str, not a float; "
            f"got {candidate!r}, which is already rounded"
        )
    if not isinstance(candidate, Decimal):
        raise AccountLedgerError(
            f"{field_name} must be a Decimal, got {type(candidate).__name__} {candidate!r}"
        )
    if not candidate.is_finite():
        raise AccountLedgerError(f"{field_name} must be finite; got {candidate}")
    return candidate
