"""Field-level parsers shared by every dataset parser, and the row-rejection signal.

Each parser here is strict about the spellings that are *dangerous* and permissive about
the ones that are exact. `int("1_0")` is 10 and `Decimal("1_0")` is `Decimal('10')`,
because Python accepts underscores as digit separators -- so a field that arrived
malformed can silently become a different number rather than a rejection. `Decimal("
1 ")` strips whitespace, and `Decimal("NaN")` constructs happily and then makes every
equality comparison downstream False forever. All four are refused by pattern. A decimal
exponent is admitted, because `Decimal("1e-8")` is exact and no precision is at stake.

`RowRejected` is a control-flow signal, not a member of the error taxonomy in
`fking.platform.errors`. That is deliberate on both counts. A rejected row is an expected
outcome that gets counted rather than a failure -- one bad row in 3.5 million must not
stop a backfill -- and it must not be a `DataIntegrityError` subclass, because the parser
*catches* `DataIntegrityError` from `epoch_to_utc` and a shared base would make those two
conditions indistinguishable at the handler. It never leaves this package: the driver in
`fking.data.loaders.driver` wraps every row parse.
"""

from __future__ import annotations

import re
from collections.abc import Mapping, Sequence
from datetime import datetime
from decimal import Decimal, InvalidOperation
from typing import Final

from fking.data.format_resolver import BooleanEncoding, EpochUnit, epoch_to_utc
from fking.data.loaders.outcome import RejectionReason
from fking.platform.errors import DataIntegrityError

__all__ = [
    "BOOLEAN_TOKENS",
    "RowRejected",
    "parse_boolean",
    "parse_decimal",
    "parse_epoch",
    "parse_non_negative_decimal",
    "parse_non_negative_int",
    "parse_positive_decimal",
    "require_field_count",
]

# Plain decimal notation with an optional sign and an optional exact exponent. Excludes
# `NaN`, `Infinity`, `1_0`, and any leading or trailing whitespace -- see the module
# docstring for why each of those is a silent wrong answer rather than a loud one.
_DECIMAL_TOKEN: Final[re.Pattern[str]] = re.compile(
    r"\A[+-]?[0-9]+(?:\.[0-9]*)?(?:[eE][+-]?[0-9]+)?\Z"
)
_SIGNED_INTEGER_TOKEN: Final[re.Pattern[str]] = re.compile(r"\A[+-]?[0-9]+\Z")
_UNSIGNED_INTEGER_TOKEN: Final[re.Pattern[str]] = re.compile(r"\A[0-9]+\Z")

# Exact, case-sensitive token tables. Not `.lower()`, not `json.loads`, and emphatically
# not "anything I do not recognise is False" -- that default returns False for every row
# of a spot trades file and inverts the sign of every order-flow feature built on it
# (F-005, DATA_PIPELINE.md section 3, trap 3).
BOOLEAN_TOKENS: Final[Mapping[BooleanEncoding, Mapping[str, bool]]] = {
    BooleanEncoding.PYTHON: {"True": True, "False": False},
    BooleanEncoding.JSON: {"true": True, "false": False},
    BooleanEncoding.NUMERIC: {"1": True, "0": False},
}

_ZERO: Final[Decimal] = Decimal("0")


class RowRejected(Exception):  # noqa: N818 -- a counted outcome, not an Error condition
    """One row cannot become a record. Carries the reason that will be tallied."""

    def __init__(self, reason: RejectionReason, detail: str) -> None:
        super().__init__(f"{reason.value}: {detail}")
        self.reason = reason
        self.detail = detail


def require_field_count(row: Sequence[str], *, expected: int) -> None:
    """Reject a row that does not hold exactly `expected` columns.

    Exactly, not at least. A row with an extra column means the layout changed upstream,
    and reading the first N of N+1 fields would keep working while meaning something else.
    """
    if len(row) != expected:
        raise RowRejected(
            RejectionReason.FIELD_COUNT,
            f"expected {expected} columns, found {len(row)}",
        )


def parse_decimal(raw: str, *, column: str) -> Decimal:
    """An exact finite `Decimal` built from the raw source substring.

    Never via `float`. `Decimal(0.1)` is already
    `Decimal('0.1000000000000000055511151231257827021181583404541015625')` because the
    literal was rounded to the nearest double by the parser, and widening the type
    afterwards cannot undo it (`docs/rules/decimal-and-money.md`).
    """
    if not _DECIMAL_TOKEN.match(raw):
        raise RowRejected(
            RejectionReason.DECIMAL_UNPARSEABLE,
            f"{column}={raw!r} is not plain decimal notation",
        )
    try:
        parsed = Decimal(raw)
    except InvalidOperation as invalid:  # pragma: no cover - the pattern admits nothing else
        raise RowRejected(
            RejectionReason.DECIMAL_UNPARSEABLE, f"{column}={raw!r} is not a decimal"
        ) from invalid
    if not parsed.is_finite():  # pragma: no cover - the pattern excludes NaN and Infinity
        raise RowRejected(RejectionReason.DECIMAL_UNPARSEABLE, f"{column}={raw!r} is not finite")
    return parsed


def parse_positive_decimal(raw: str, *, column: str) -> Decimal:
    """A `Decimal` strictly greater than zero. For prices, which no print can lack."""
    parsed = parse_decimal(raw, column=column)
    if parsed <= _ZERO:
        raise RowRejected(RejectionReason.PRICE_NOT_POSITIVE, f"{column}={raw!r} is not positive")
    return parsed


def parse_non_negative_decimal(raw: str, *, column: str) -> Decimal:
    """A `Decimal` of zero or more. For volumes.

    Zero is accepted on purpose: a minute in which nothing traded is an observation, and
    discarding it shortens the series and moves every rolling window computed from it.
    """
    parsed = parse_decimal(raw, column=column)
    if parsed < _ZERO:
        raise RowRejected(RejectionReason.VOLUME_NEGATIVE, f"{column}={raw!r} is negative")
    return parsed


def parse_epoch(raw: str, *, column: str, unit: EpochUnit, now_utc: datetime) -> datetime:
    """A raw archive epoch, normalised to an aware UTC datetime under the declared unit.

    The plausibility window inside `epoch_to_utc` is the cheapest detector of a wrong unit
    that exists: milliseconds read as microseconds land in 1970, microseconds read as
    milliseconds land near the year 56,000, and both are absurd rather than subtle. A
    failure is converted into a *row* rejection rather than propagated, because a wrong
    declaration rejects every row and the rejection-fraction gate then refuses the file --
    one rule instead of a separate first-row assertion, and the error message carries
    `epoch_out_of_range=1440/1440`, which names the cause exactly.
    """
    if not _SIGNED_INTEGER_TOKEN.match(raw):
        raise RowRejected(
            RejectionReason.EPOCH_NOT_INTEGER,
            f"{column}={raw!r} is not a base-10 integer",
        )
    try:
        return epoch_to_utc(int(raw), unit=unit, now_utc=now_utc)
    except DataIntegrityError as out_of_range:
        raise RowRejected(
            RejectionReason.EPOCH_OUT_OF_RANGE, f"{column}={raw!r} under {unit.value}"
        ) from out_of_range


def parse_non_negative_int(raw: str, *, column: str) -> int:
    """A non-negative base-10 integer. For trade counts."""
    if not _UNSIGNED_INTEGER_TOKEN.match(raw):
        raise RowRejected(
            RejectionReason.TRADE_COUNT_NOT_INTEGER,
            f"{column}={raw!r} is not a non-negative base-10 integer",
        )
    return int(raw)


def parse_boolean(raw: str, *, column: str, encoding: BooleanEncoding) -> bool:
    """A boolean under the declared encoding, or a rejection.

    There is no fallback branch. An unrecognised token is the *only* signal that an
    upstream encoding has drifted, and consuming it as `False` would make trap 3 recur on
    a new dataset with no evidence anywhere that it had.
    """
    tokens = BOOLEAN_TOKENS[encoding]
    if raw not in tokens:
        raise RowRejected(
            RejectionReason.BOOLEAN_UNRECOGNISED,
            f"{column}={raw!r} is not one of {sorted(tokens)} under {encoding.value}",
        )
    return tokens[raw]
