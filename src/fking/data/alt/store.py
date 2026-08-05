"""As-of reads over the alternative series, and the one write path that feeds them.

The guarantee is not implemented here. It is in
`migrations/versions/0014_alt_observations.py`: `fking_app` holds no privilege on
`alt_observations` at all, and `fking_alt_as_of()` is `SECURITY DEFINER` with the
`available_at_utc <= as_of` predicate inside its body. What is here is a typed façade over
that function, plus the checks that catch a bad `as_of` before it reaches the database.

The shapes are the ones `fking.data.features.store` argues for, for the same reasons, and
they are repeated rather than shared because the two read different tables through
different functions:

**`as_of` is keyword-only, non-optional and has no default.** A default is a value
somebody forgets to override, and the value they would forget is `now()` -- which is the
leak. Keyword-only additionally stops it being passed positionally into the `lookback`
slot, where a `datetime` would raise but a `timedelta` would not.

**What comes back carries no `available_at_utc`.** The function does not return it and
this type has no field for it, so a caller cannot re-derive "what would this look like
without the as-of bound". A value that could be filtered again by the caller is a value
whose filtering is the caller's decision.

**The writer takes `AltPoint`s, which only `AltSourceSpec.point()` can build.** That is
what stops a writer asserting an availability the declaration does not support: there is
no `available_at_utc` parameter anywhere on this path.

The writer is separate and connects as a different role. `fking_ingest` holds `SELECT` and
`INSERT` and nothing else: an observation that can be `UPDATE`d is an observation whose
first print can be rewritten to match a backtest -- which for a revised macro series is
precisely the rewrite that would make a strategy look like it traded on numbers nobody had.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime, timedelta
from decimal import Decimal
from typing import Final, Protocol

import sqlalchemy as sa
from sqlalchemy.ext.asyncio import AsyncEngine

from fking.data.alt.registry import registered
from fking.data.alt.spec import AltPoint, AltSeriesRef, require_utc
from fking.platform.errors import FeatureContractError

__all__ = [
    "AltObservationWriter",
    "AltSeries",
    "AltStore",
    "AltValue",
    "PostgresAltStore",
]

_READ: Final = sa.text(
    """
    SELECT event_time_utc, observed_value
      FROM fking_alt_as_of(:source_id, :series_id, :as_of, :lookback)
     ORDER BY event_time_utc
    """
)

_APPEND: Final = sa.text(
    """
    INSERT INTO alt_observations (
        source_id, series_id, event_time_utc, available_at_utc, observed_value
    )
    VALUES (
        :source_id, :series_id, :event_time_utc, :available_at_utc, :observed_value
    )
    ON CONFLICT DO NOTHING
    """
)


@dataclass(frozen=True, slots=True)
class AltValue:
    """One value as it was believed at the `as_of` that produced it."""

    event_time_utc: datetime
    observed_value: Decimal


@dataclass(frozen=True, slots=True)
class AltSeries:
    """The answer to one as-of read, carrying the question it answered.

    `as_of` and `lookback` travel with the values because a series detached from the
    instant it was read at is a series nobody can check for look-ahead afterwards, and the
    audit requirement is that a decision be reconstructable months later
    (`ARCHITECTURE.md` section 11).
    """

    series: AltSeriesRef
    as_of: datetime
    lookback: timedelta
    values: tuple[AltValue, ...]


class AltStore(Protocol):
    """Point-in-time reads over an alternative series."""

    async def load(
        self, series: AltSeriesRef, *, as_of: datetime, lookback: timedelta
    ) -> AltSeries:
        """Values as they were knowable at `as_of`, over `(as_of - lookback, as_of]`.

        The docstring is the whole body. A trailing `...` after it is a statement with no
        effect, and CodeQL is right to say so.
        """


class PostgresAltStore:
    """`AltStore` over `fking_alt_as_of()`, connected as `fking_app`.

    Holds an engine rather than a connection: a read is one short statement, and a caller
    handed a connection would either widen somebody else's transaction or commit inside it.
    """

    def __init__(self, engine: AsyncEngine) -> None:
        self._engine = engine

    async def load(
        self, series: AltSeriesRef, *, as_of: datetime, lookback: timedelta
    ) -> AltSeries:
        require_utc(as_of, "as_of")
        if lookback <= timedelta(0):
            raise FeatureContractError(
                f"lookback must be positive; got {lookback}. A zero window returns nothing "
                f"and reads as 'this source has no history'"
            )
        # Resolving the declaration here refuses a source nobody registered -- the route
        # by which a series reaches a strategy without ever declaring an availability lag.
        registered(series.source_id)
        async with self._engine.connect() as connection:
            rows = (
                await connection.execute(
                    _READ,
                    {
                        "source_id": series.source_id,
                        "series_id": series.series_id,
                        "as_of": as_of,
                        "lookback": lookback,
                    },
                )
            ).all()
        return AltSeries(
            series=series,
            as_of=as_of,
            lookback=lookback,
            values=tuple(
                AltValue(
                    event_time_utc=row.event_time_utc,
                    observed_value=row.observed_value,
                )
                for row in rows
            ),
        )


class AltObservationWriter:
    """The write path, connected as `fking_ingest`.

    `ON CONFLICT DO NOTHING`, so re-ingesting the same archive is idempotent and returns
    zero. It also means a *different* value cannot be written at coordinates that already
    hold one -- which is the intended refusal rather than a limitation: a value that
    changed under the same `(series, event_time, available_at)` is a restatement, and a
    restatement has a later `available_at_utc` by definition. If it does not, the source
    rewrote history silently and that is a data-integrity event, not an update.
    """

    def __init__(self, engine: AsyncEngine) -> None:
        self._engine = engine

    async def append(self, points: Sequence[AltPoint]) -> int:
        """Write points in one transaction; returns how many were new.

        Raises:
            DataUnavailableError: a point names an unregistered source.
        """
        if not points:
            return 0
        written = 0
        async with self._engine.begin() as connection:
            for point in points:
                registered(point.series.source_id)
                outcome = await connection.execute(
                    _APPEND,
                    {
                        "source_id": point.series.source_id,
                        "series_id": point.series.series_id,
                        "event_time_utc": point.event_time_utc,
                        "available_at_utc": point.available_at_utc,
                        "observed_value": point.observed_value,
                    },
                )
                written += outcome.rowcount
        return written
