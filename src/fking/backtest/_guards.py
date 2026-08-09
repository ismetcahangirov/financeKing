"""Construction-time validators for the engine's own types.

Deliberately a second implementation of checks that also exist in `fking.domain`, and
not an import of them. `fking.domain._guards` is private to that package -- a leading
underscore means the package makes no promise the module exists -- and
`docs/rules/module-boundaries.md` blocks a cross-package import of one. The domain's
public promise is its constructors, which validate what they build; nothing here builds
a domain object.

The cost is four short functions duplicated in each package that needs them, and the
same trade is already made in `fking.data` and `fking.platform.scheduler`. The
alternative -- promoting the guards to `platform` so everyone can share them -- would
put "what a valid price looks like" in the layer that is supposed to hold no trading
vocabulary at all.

Each takes `object` rather than the type it validates, for the reason the domain's
copies do: annotating the parameter would make the `isinstance` check read as redundant
to a reviewer and unreachable to `mypy --warn-unreachable`, and the whole point is that
the value arriving here has been checked by nothing.
"""

from __future__ import annotations

from datetime import datetime, timedelta
from decimal import Decimal
from typing import Final

from fking.backtest._errors import RunConfigError

_UTC_OFFSET: Final = timedelta(0)


def require_utc(candidate: object, field_name: str) -> datetime:
    """A timezone-aware datetime whose offset is exactly UTC.

    Rejects rather than converts. An event stamped in `Europe/Baku` sorts fine, compares
    fine, and puts every bar four hours out of position -- and `astimezone(UTC)` here
    would launder that wrong offset into a confident value with no record that anybody
    guessed.
    """
    if not isinstance(candidate, datetime):
        raise RunConfigError(
            f"{field_name} must be a datetime, got {type(candidate).__name__} {candidate!r}"
        )
    if candidate.tzinfo is None or candidate.utcoffset() is None:
        raise RunConfigError(f"{field_name} must be timezone-aware; got naive {candidate!r}")
    if candidate.utcoffset() != _UTC_OFFSET:
        raise RunConfigError(
            f"{field_name} must be UTC; got offset {candidate.utcoffset()!r} in {candidate!r}"
        )
    return candidate


def require_finite_decimal(candidate: object, field_name: str) -> Decimal:
    """An exact, finite `Decimal`. Signed values are allowed; a float is not.

    `float` is named separately from every other wrong type because it is the one that
    arrives by accident and the one whose error predates this call: `Decimal(0.1)` is
    already `0.1000000000000000055511151231257827021181583404541015625` because the
    literal was rounded by the parser.

    Finiteness matters here more than elsewhere. A `Decimal("NaN")` funding rate
    propagates through arithmetic without raising and compares unequal to itself, so a
    single one makes every equality-based determinism check report a difference that has
    no cause anybody can find.
    """
    if isinstance(candidate, float):
        raise RunConfigError(
            f"{field_name} must be a Decimal constructed from str, not a float; "
            f"got {candidate!r}, which is already rounded"
        )
    if not isinstance(candidate, Decimal):
        raise RunConfigError(
            f"{field_name} must be a Decimal, got {type(candidate).__name__} {candidate!r}"
        )
    if not candidate.is_finite():
        raise RunConfigError(f"{field_name} must be finite; got {candidate}")
    return candidate


def require_text(candidate: object, field_name: str) -> str:
    """A non-empty string carrying at least one non-whitespace character.

    A blank `strategy_id` or timer label satisfies the schema and answers nothing when
    the trace it lands in is read months later.
    """
    if not isinstance(candidate, str):
        raise RunConfigError(
            f"{field_name} must be a string, got {type(candidate).__name__} {candidate!r}"
        )
    if not candidate.strip():
        raise RunConfigError(f"{field_name} must not be blank")
    return candidate


def require_positive_int(candidate: object, field_name: str) -> int:
    """An `int` strictly greater than zero, and never a `bool`.

    `True` satisfies `isinstance(x, int)`, and an event budget of `True` is a budget of
    one -- which halts the run after a single event and reports it as exhaustion.
    """
    if not isinstance(candidate, int) or isinstance(candidate, bool):
        raise RunConfigError(
            f"{field_name} must be an int, got {type(candidate).__name__} {candidate!r}"
        )
    if candidate <= 0:
        raise RunConfigError(f"{field_name} must be positive; got {candidate}")
    return candidate
