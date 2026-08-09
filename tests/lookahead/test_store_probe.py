"""The probe at the storage layer, and the demonstration that it can fail.

`test_probe.py` poisons the *input* to a computation. This file poisons what the corpus
*believes*: it publishes a revision of a value that already exists, stamped as available
later, and requires an earlier as-of read to be byte-identical to what it returned before
the revision existed.

The second test is the one that makes the first mean something. It runs the same read with
one predicate changed -- `available_at_utc <= :as_of` becomes `event_time_utc <= :as_of` --
and requires the probe to go **red**. That is the leak `docs/rules/no-lookahead.md`
calls the single most common form of the bug, it looks completely correct in a diff, and
without this test nothing in the repository would notice if it were introduced.

Note which role runs which query. The leaky statement is executed as `fking_ingest`,
because `fking_app` holds no privilege on `feature_values` at all -- so this file also
demonstrates, from the outside, why that grant matrix is what it is: as the application
role the leaky read is not a subtle mistake, it is `permission denied`.
"""

from __future__ import annotations

from collections.abc import Iterable
from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest
import sqlalchemy as sa
from sqlalchemy.exc import DBAPIError, ProgrammingError
from sqlalchemy.ext.asyncio import AsyncEngine

from fking.data.features.spec import FeatureRef
from fking.data.features.store import PostgresFeatureStore
from fking.data.format_resolver import Market
from tests.support.availability import permitting

pytestmark = [pytest.mark.integration, pytest.mark.slow]

_REF = FeatureRef(name="trailing_return_fraction", version=1, market=Market.SPOT, symbol="BTCUSDT")
_EVENT = datetime(2026, 3, 1, 12, 0, tzinfo=UTC)
_LOOKBACK = timedelta(hours=6)
_PUBLISHED_LATE = _EVENT + timedelta(hours=3)
_AVAILABLE = permitting(
    earliest_event_time_utc=_EVENT - timedelta(days=1),
    latest_event_time_utc=_EVENT + timedelta(days=1),
)

_APPEND = sa.text(
    """
    INSERT INTO feature_values (
        feature_name, feature_version, market, symbol,
        event_time_utc, available_at_utc, feature_value
    )
    VALUES ('trailing_return_fraction', 1, 'spot', 'BTCUSDT',
            :event_time_utc, :available_at_utc, :feature_value)
    """
)

# The leak, spelled out. Identical to fking_feature_as_of()'s body except for the one
# predicate, so the diff between the two is exactly the defect and nothing else.
_LEAKY_READ = sa.text(
    """
    SELECT DISTINCT ON (event_time_utc) event_time_utc, feature_value
      FROM feature_values
     WHERE feature_name = 'trailing_return_fraction'
       AND feature_version = 1
       AND market = 'spot'
       AND symbol = 'BTCUSDT'
       AND event_time_utc <= CAST(:as_of AS timestamptz)
       AND event_time_utc > CAST(:as_of AS timestamptz) - CAST(:lookback AS interval)
     ORDER BY event_time_utc, available_at_utc DESC
    """
)


async def _publish(
    engine: AsyncEngine, *, event_time_utc: datetime, available_at_utc: datetime, decimal_text: str
) -> None:
    async with engine.begin() as connection:
        await connection.execute(
            _APPEND,
            {
                "event_time_utc": event_time_utc,
                "available_at_utc": available_at_utc,
                "feature_value": Decimal(decimal_text),
            },
        )


def _exact_text(rows: Iterable[tuple[datetime, Decimal]]) -> tuple[str, ...]:
    """Exact decimal text per row, so a revision differing in the last digit is a
    different answer. `format(value, "f")` never falls back to scientific notation and
    preserves trailing zeros, which is what "byte-identical" has to mean here."""
    return tuple(
        f"{event_time_utc.isoformat()}|{format(feature_value, 'f')}"
        for event_time_utc, feature_value in rows
    )


async def _read_through_the_store(engine: AsyncEngine, *, as_of: datetime) -> tuple[str, ...]:
    series = await PostgresFeatureStore(engine, _AVAILABLE).load(
        _REF, as_of=as_of, lookback=_LOOKBACK
    )
    return _exact_text((entry.event_time_utc, entry.feature_value) for entry in series.values)


async def _read_filtering_on_event_time(engine: AsyncEngine, *, as_of: datetime) -> tuple[str, ...]:
    """The same read with the predicate reverted. Runs as `fking_ingest` by necessity."""
    async with engine.connect() as connection:
        rows = (
            await connection.execute(_LEAKY_READ, {"as_of": as_of, "lookback": _LOOKBACK})
        ).all()
    return _exact_text((row.event_time_utc, row.feature_value) for row in rows)


@pytest.mark.asyncio
async def test_publishing_a_revision_later_cannot_move_an_earlier_read(
    app_engine: AsyncEngine, ingest_engine: AsyncEngine
) -> None:
    """The probe. Poison what the corpus believes; the past must not move."""
    await _publish(
        ingest_engine, event_time_utc=_EVENT, available_at_utc=_EVENT, decimal_text="0.25"
    )
    before_revision = await _read_through_the_store(app_engine, as_of=_EVENT + timedelta(hours=1))

    await _publish(
        ingest_engine,
        event_time_utc=_EVENT,
        available_at_utc=_PUBLISHED_LATE,
        decimal_text="0.31",
    )
    after_revision = await _read_through_the_store(app_engine, as_of=_EVENT + timedelta(hours=1))

    assert before_revision, "the read returned nothing, so the comparison verified nothing"
    assert before_revision == after_revision
    assert before_revision == ("2026-03-01T12:00:00+00:00|0.250000000000000000",)


@pytest.mark.asyncio
async def test_the_probe_goes_red_when_the_read_filters_on_event_time(
    ingest_engine: AsyncEngine,
) -> None:
    """Break the guard on purpose and require the probe to notice.

    A leak test that has never been observed to fail is not evidence of anything
    (`DATA_PIPELINE.md` section 7). The predicate reverted here is one word wide, reads
    correctly, and hands a backtest a correction that had not been published yet.
    """
    await _publish(
        ingest_engine, event_time_utc=_EVENT, available_at_utc=_EVENT, decimal_text="0.25"
    )
    as_of = _EVENT + timedelta(hours=1)
    before_revision = await _read_filtering_on_event_time(ingest_engine, as_of=as_of)

    await _publish(
        ingest_engine,
        event_time_utc=_EVENT,
        available_at_utc=_PUBLISHED_LATE,
        decimal_text="0.31",
    )
    after_revision = await _read_filtering_on_event_time(ingest_engine, as_of=as_of)

    assert before_revision, "the leaky read returned nothing, so it demonstrated nothing"
    with pytest.raises(AssertionError):
        assert before_revision == after_revision
    assert after_revision == ("2026-03-01T12:00:00+00:00|0.310000000000000000",)


@pytest.mark.asyncio
async def test_the_application_role_cannot_run_the_leaky_read_at_all(
    app_engine: AsyncEngine,
) -> None:
    """The defence in depth behind the one above.

    The leaky statement had to be executed as `fking_ingest` because the role every
    strategy, backtest and risk process connects as holds nothing on `feature_values`. A
    look-ahead defect at this layer is therefore a permission error rather than a review
    miss.
    """
    async with app_engine.connect() as connection:
        with pytest.raises((ProgrammingError, DBAPIError)) as refused:
            await connection.execute(_LEAKY_READ, {"as_of": _EVENT, "lookback": _LOOKBACK})
    assert "permission denied for table feature_values" in str(refused.value).lower()
