"""The three column shapes this schema is allowed to use for money, time and text ids.

Factories rather than module-level `Column` objects: a SQLAlchemy type instance is
reusable, but writing `Money` as a bare name invites `sa.Column("x", Money)` in one
place and `sa.Column("x", Money())` in another, and only one of those carries the
precision. A function call cannot be spelled the wrong way by accident.
"""

from __future__ import annotations

from decimal import Decimal
from typing import Final

import sqlalchemy as sa

# Matches the process-wide decimal context precision of 38 (docs/rules/decimal-and-money.md),
# so a value representable in the database is representable in memory and the round trip
# cannot lose digits. The scale of 18 is what lets a satoshi-denominated quantity and a
# USD notional live in the same type without either being truncated.
MONEY_PRECISION: Final[int] = 38
MONEY_SCALE: Final[int] = 18


def money() -> sa.Numeric[Decimal]:
    """`NUMERIC(38, 18)`.

    `asdecimal` is left at its default True so the driver returns `Decimal`. Postgres
    `numeric` has no float representation to fall back to, which is the point: there is
    no configuration of this column that yields a `float` to Python.
    """
    return sa.Numeric(precision=MONEY_PRECISION, scale=MONEY_SCALE)


def utc_timestamp() -> sa.DateTime:
    """`TIMESTAMPTZ`.

    `TIMESTAMP WITHOUT TIME ZONE` stores wall-clock digits and discards the offset on
    insert, irreversibly and silently. Every temporal column in this schema is this type
    and every one of them is named with a `_utc` suffix, which is what
    `test_schema_contract.py` keys on.
    """
    return sa.DateTime(timezone=True)


def identifier() -> sa.Text:
    """A textual identifier of unbounded length.

    `TEXT` rather than `VARCHAR(n)`: Postgres stores them identically, and the length
    limit's only effect is to reject a venue that lengthened its own id format -- at
    insert time, in the order path, with the exchange's answer already in hand.
    """
    return sa.Text()
