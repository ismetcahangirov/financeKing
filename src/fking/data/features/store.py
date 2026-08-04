"""As-of reads, and the one write path that feeds them.

The guarantee this module carries is not implemented in this module. It is implemented
in `migrations/versions/0010_feature_store.py`: `fking_app` holds no privilege on
`feature_values` at all, and `fking_feature_as_of()` is `SECURITY DEFINER` with the
`available_at_utc <= as_of` predicate inside its body. What is here is a typed façade
over that function, plus the checks that catch a bad `as_of` before it reaches the
database.

Two shapes in the signature are deliberate and are the reason this file exists rather
than a bare `text()` call at each call site.

**`as_of` is keyword-only, non-optional and has no default.** A default is a value
somebody forgets to override, and the value they would forget is `now()` -- which is
the leak. Keyword-only additionally stops it being passed positionally into the
`lookback` slot, where a `datetime` would raise but a `timedelta` would not.

**What comes back carries no `available_at_utc`.** The function does not return it and
this type has no field for it, so a caller cannot re-derive "what would this look like
without the as-of bound". A value that could be filtered again by the caller is a value
whose filtering is the caller's decision.

The writer is separate and connects as a different role. `fking_ingest` holds `SELECT`
and `INSERT` on `feature_values` and nothing else: a feature value that can be `UPDATE`d
is a feature value whose history can be rewritten to match a backtest.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime, timedelta
from decimal import Decimal
from typing import Final, Protocol

import sqlalchemy as sa
from sqlalchemy.ext.asyncio import AsyncEngine

from fking.data.features.spec import FeaturePoint, FeatureRef
from fking.platform.errors import FeatureContractError

__all__ = [
    "FeatureSeries",
    "FeatureStore",
    "FeatureValue",
    "FeatureValueWriter",
    "PostgresFeatureStore",
]

_UTC_OFFSET: Final = timedelta(0)

_READ: Final = sa.text(
    """
    SELECT event_time_utc, feature_value
      FROM fking_feature_as_of(
          :feature_name, :feature_version, :market, :symbol, :as_of, :lookback
      )
     ORDER BY event_time_utc
    """
)

_APPEND: Final = sa.text(
    """
    INSERT INTO feature_values (
        feature_name, feature_version, market, symbol,
        event_time_utc, available_at_utc, feature_value
    )
    VALUES (
        :feature_name, :feature_version, :market, :symbol,
        :event_time_utc, :available_at_utc, :feature_value
    )
    ON CONFLICT DO NOTHING
    """
)


@dataclass(frozen=True, slots=True)
class FeatureValue:
    """One value as it was believed at the `as_of` that produced it."""

    event_time_utc: datetime
    feature_value: Decimal


@dataclass(frozen=True, slots=True)
class FeatureSeries:
    """The answer to one as-of read, carrying the question it answered.

    `as_of` and `lookback` travel with the values because a series detached from the
    instant it was read at is a series nobody can check for look-ahead afterwards -- and
    the audit requirement is that a decision be reconstructable months later
    (`ARCHITECTURE.md` 11).
    """

    feature: FeatureRef
    as_of: datetime
    lookback: timedelta
    values: tuple[FeatureValue, ...]


class FeatureStore(Protocol):
    """Point-in-time reads. The only shape a strategy or a backtest may hold."""

    async def load(
        self, feature: FeatureRef, *, as_of: datetime, lookback: timedelta
    ) -> FeatureSeries:
        """Values as they were knowable at `as_of`, over `(as_of - lookback, as_of]`.

        The docstring is the whole body. A trailing `...` after it is a statement with
        no effect, and CodeQL is right to say so.
        """


class PostgresFeatureStore:
    """`FeatureStore` over `fking_feature_as_of()`, connected as `fking_app`.

    Holds an engine rather than a connection: a read is one short statement, and a
    caller handed a connection would either widen somebody else's transaction or commit
    inside it.
    """

    def __init__(self, engine: AsyncEngine) -> None:
        self._engine = engine

    async def load(
        self, feature: FeatureRef, *, as_of: datetime, lookback: timedelta
    ) -> FeatureSeries:
        _require_utc(as_of, "as_of")
        if lookback <= timedelta(0):
            raise FeatureContractError(
                f"lookback must be positive; got {lookback}. A zero window returns nothing "
                f"and reads as 'this feature has no history'"
            )
        async with self._engine.connect() as connection:
            rows = (
                await connection.execute(
                    _READ,
                    {
                        "feature_name": feature.name,
                        "feature_version": feature.version,
                        "market": feature.market.value,
                        "symbol": feature.symbol,
                        "as_of": as_of,
                        "lookback": lookback,
                    },
                )
            ).all()
        return FeatureSeries(
            feature=feature,
            as_of=as_of,
            lookback=lookback,
            values=tuple(
                FeatureValue(
                    event_time_utc=row.event_time_utc,
                    feature_value=row.feature_value,
                )
                for row in rows
            ),
        )


class FeatureValueWriter:
    """The write path, connected as `fking_ingest`.

    `ON CONFLICT DO NOTHING`, so re-running the same computation over the same series is
    idempotent and returns zero. It also means a *different* value cannot be written at
    coordinates that already hold one -- which is the intended refusal rather than a
    limitation: a value that changed under the same `(version, event_time, available_at)`
    is either a new definition, which needs a new version, or a correction, which needs a
    later `available_at_utc`. Both of those are new rows.
    """

    def __init__(self, engine: AsyncEngine) -> None:
        self._engine = engine

    async def append(self, feature: FeatureRef, points: Sequence[FeaturePoint]) -> int:
        """Write points for one series in one transaction; returns how many were new."""
        if not points:
            return 0
        written = 0
        async with self._engine.begin() as connection:
            for point in points:
                _require_utc(point.event_time_utc, "event_time_utc")
                _require_utc(point.available_at_utc, "available_at_utc")
                outcome = await connection.execute(
                    _APPEND,
                    {
                        "feature_name": feature.name,
                        "feature_version": feature.version,
                        "market": feature.market.value,
                        "symbol": feature.symbol,
                        "event_time_utc": point.event_time_utc,
                        "available_at_utc": point.available_at_utc,
                        "feature_value": point.feature_value,
                    },
                )
                written += outcome.rowcount
        return written


def _require_utc(candidate: datetime, field_name: str) -> datetime:
    """Aware, and exactly UTC. Rejected rather than converted.

    `astimezone(UTC)` would launder an offset that was guessed wrong upstream into a
    confident value, and in a 24/7 market there is no session boundary to make the
    resulting four-hour shift visible.
    """
    if candidate.tzinfo is None or candidate.utcoffset() is None:
        raise FeatureContractError(f"{field_name} must be timezone-aware; got naive {candidate!r}")
    if candidate.utcoffset() != _UTC_OFFSET:
        raise FeatureContractError(
            f"{field_name} must be UTC; got offset {candidate.utcoffset()!r} in {candidate!r}"
        )
    return candidate
