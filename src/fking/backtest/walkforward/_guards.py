"""Construction-time validators that raise this package's own vocabulary.

A third copy of checks that also exist in `fking.domain` and in `fking.backtest`, and
deliberately not an import of either. `fking.backtest._guards` raises `RunConfigError`,
whose docstring says "a run configuration is malformed, so no run can be identified by
it" -- which is a true sentence about a `RunConfig` and a misleading one about a
walk-forward plan, where nothing is being identified and no run has been configured. The
error a reader sees names what they got wrong, and reusing the closest available class
costs that.

The cost is two short functions. The same trade is already made between `fking.domain`,
`fking.backtest`, `fking.data` and `fking.platform.scheduler`, for the same reason.
"""

from __future__ import annotations

from datetime import datetime, timedelta
from decimal import Decimal
from typing import Final

from fking.backtest.walkforward._errors import WalkForwardError

_UTC_OFFSET: Final = timedelta(0)


def require_utc(candidate: object, field_name: str, failure: type[WalkForwardError]) -> datetime:
    """A timezone-aware datetime whose offset is exactly UTC.

    Rejects rather than converts. A fold boundary stamped in `Europe/Baku` orders fine,
    compares fine, and moves every training window four hours -- straight through the
    purge, which is measured in hours. `astimezone(UTC)` here would launder that wrong
    offset into a confident value with no record that anybody guessed.
    """
    if not isinstance(candidate, datetime):
        raise failure(
            f"{field_name} must be a datetime, got {type(candidate).__name__} {candidate!r}"
        )
    if candidate.tzinfo is None or candidate.utcoffset() is None:
        raise failure(f"{field_name} must be timezone-aware; got naive {candidate!r}")
    if candidate.utcoffset() != _UTC_OFFSET:
        raise failure(f"{field_name} must be UTC; got offset {candidate.utcoffset()!r}")
    return candidate


def require_finite_decimal(
    candidate: object, field_name: str, failure: type[WalkForwardError]
) -> Decimal:
    """An exact, finite `Decimal`. Signed values are allowed; a float is not.

    A `Decimal("NaN")` Sharpe would propagate through the regression without raising and
    compare unequal to itself, so a single one turns the decay slope into a value that is
    neither negative, positive nor zero -- and every comparison against a promotion
    threshold silently takes the false branch.
    """
    if isinstance(candidate, float):
        raise failure(
            f"{field_name} must be a Decimal constructed from str, not a float; "
            f"got {candidate!r}, which is already rounded"
        )
    if not isinstance(candidate, Decimal):
        raise failure(
            f"{field_name} must be a Decimal, got {type(candidate).__name__} {candidate!r}"
        )
    if not candidate.is_finite():
        raise failure(f"{field_name} must be finite; got {candidate}")
    return candidate
