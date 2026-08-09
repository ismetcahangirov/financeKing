"""The conviction calibration map survives a restart, unchanged, against real PostgreSQL.

Three claims, and only the first is obvious.

**The restored map is identical to the one that was written.** Not "close enough": the map
is a step function whose steps are `Decimal` fractions, and a map that shifts by one unit
in the last place across a restart sizes a signal differently before and after, for no
reason anybody can see in a log. This is where the eighteen-place quantization in
`fking.risk.calibration` pays for itself -- without it the fit carries the context's full
precision, `NUMERIC(38, 18)` rounds it on the way in, and this test is the only place in
the system where that difference is observable.

**The read is as-of, not latest.** A decision taken at `t` must read the newest map whose
`available_at_utc` is at or before `t`. Reading the latest row instead is the look-ahead
issue #49 exists to close, and it is one `ORDER BY ... LIMIT 1` away at all times.

**The rows cannot be rewritten.** A calibration row is a claim about what was knowable at
an instant; an `UPDATE` in place destroys the evidence for that claim and leaves an audit
unable to tell a correct point-in-time fit from one that read the whole record.

Never a mock. The failures worth catching here live in `NUMERIC(38, 18)` rounding, the
`CHECK` constraints, and the `BEFORE UPDATE OR DELETE` trigger, and a mock has none of
them (`CLAUDE.md` section 5).
"""

from __future__ import annotations

import uuid
from collections.abc import Mapping
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Final

import pytest
import sqlalchemy as sa
from sqlalchemy.exc import DBAPIError, IntegrityError
from sqlalchemy.ext.asyncio import AsyncConnection, AsyncEngine

from fking.risk.calibration import (
    CalibrationMap,
    ClosedTrade,
    ConvictionParameters,
    fit_calibration,
    from_calibration_row,
    to_calibration_row,
)

pytestmark = [pytest.mark.integration, pytest.mark.slow]

_STRATEGY_ID: Final = "trailing-return-v3"
_EPOCH: Final = datetime(2026, 1, 1, tzinfo=UTC)
_PARAMETERS: Final = ConvictionParameters()

_INSERT_MAP = sa.text(
    """
    INSERT INTO risk_conviction_map (map_id, strategy_id, available_at_utc, observation_count)
    VALUES (:map_id, :strategy_id, :available_at_utc, :observation_count)
    """
)

_INSERT_BUCKET = sa.text(
    """
    INSERT INTO risk_conviction_map_bucket
        (map_id, bucket_index, conviction_upper_bound, trade_count, hit_rate_fraction,
         mean_return_fraction, fitted_return_fraction, calibrated_fraction)
    VALUES
        (:map_id, :bucket_index, :conviction_upper_bound, :trade_count, :hit_rate_fraction,
         :mean_return_fraction, :fitted_return_fraction, :calibrated_fraction)
    """
)

_SELECT_MAP_AS_OF = sa.text(
    """
    SELECT map_id, strategy_id, available_at_utc, observation_count
      FROM risk_conviction_map
     WHERE strategy_id = :strategy_id
       AND available_at_utc <= :as_of_utc
     ORDER BY available_at_utc DESC
     LIMIT 1
    """
)

_SELECT_BUCKETS = sa.text(
    """
    SELECT conviction_upper_bound, trade_count, hit_rate_fraction, mean_return_fraction,
           fitted_return_fraction, calibrated_fraction
      FROM risk_conviction_map_bucket
     WHERE map_id = :map_id
     ORDER BY bucket_index
    """
)

# `_informative_record` reports ten distinct convictions, so the fit produces ten deciles
# and ten buckets. Asserted rather than assumed: a codec that dropped buckets would still
# satisfy the equality below if the fit had collapsed to one.
_EXPECTED_BUCKET_COUNT: Final = 10

_DECIMAL_FIELDS: Final = (
    "conviction_upper_bound",
    "hit_rate_fraction",
    "mean_return_fraction",
    "fitted_return_fraction",
    "calibrated_fraction",
)


def _informative_record(trade_count: int) -> list[ClosedTrade]:
    """A strategy whose conviction predicts its outcome, so the map has ten real steps.

    An uninformative record fits flat, and a flat map round-trips trivially -- every
    bucket holds the same number, so a codec that dropped the ordering would still pass.

    The returns are chosen so the *calibrated* fractions are ninths: the bucket means run
    from -0.04 to 0.05, so normalising against the 0.09 span gives `j / 9`, which is
    non-terminating for eight of the ten buckets and therefore fills all eighteen decimal
    places. A record whose calibrated fractions were round numbers would round-trip
    through `NUMERIC(38, 18)` no matter how the scale were handled.
    """
    return [
        ClosedTrade(
            strategy_id=_STRATEGY_ID,
            reported_conviction=Decimal(index % 10) / Decimal("10"),
            realised_return_fraction=(Decimal(index % 10) - Decimal("4")) / Decimal("100"),
            closed_at_utc=_EPOCH + timedelta(hours=index),
        )
        for index in range(trade_count)
    ]


async def _write(connection: AsyncConnection, calibration: CalibrationMap) -> uuid.UUID:
    """Persist a map the way a repository would: from the codec's output, never from floats.

    The codec emits every fraction as a string, and it is turned back into a `Decimal`
    here rather than earlier because the driver's `NUMERIC` adapter takes `Decimal`. The
    string form is what guarantees nothing became a float between the fit and this line.
    """
    row = to_calibration_row(calibration)
    map_id = uuid.uuid4()
    await connection.execute(
        _INSERT_MAP,
        {
            "map_id": map_id,
            "strategy_id": row["strategy_id"],
            "available_at_utc": datetime.fromisoformat(str(row["available_at_utc"])),
            "observation_count": row["observation_count"],
        },
    )
    buckets = row["buckets"]
    assert isinstance(buckets, tuple)
    for bucket_index, bucket in enumerate(buckets):
        assert isinstance(bucket, Mapping)
        await connection.execute(
            _INSERT_BUCKET,
            {
                "map_id": map_id,
                "bucket_index": bucket_index,
                "trade_count": bucket["trade_count"],
                **{name: Decimal(str(bucket[name])) for name in _DECIMAL_FIELDS},
            },
        )
    return map_id


async def _read_as_of(connection: AsyncConnection, as_of_utc: datetime) -> CalibrationMap | None:
    """The restart path: rebuild the map that was knowable at `as_of_utc`, or nothing."""
    header = (
        await connection.execute(
            _SELECT_MAP_AS_OF, {"strategy_id": _STRATEGY_ID, "as_of_utc": as_of_utc}
        )
    ).one_or_none()
    if header is None:
        return None
    buckets = (await connection.execute(_SELECT_BUCKETS, {"map_id": header.map_id})).all()
    return from_calibration_row(
        {
            "strategy_id": header.strategy_id,
            "available_at_utc": header.available_at_utc.isoformat(),
            "observation_count": header.observation_count,
            "buckets": tuple(
                {
                    "trade_count": bucket.trade_count,
                    **{name: str(getattr(bucket, name)) for name in _DECIMAL_FIELDS},
                }
                for bucket in buckets
            ),
        }
    )


@pytest.mark.asyncio
async def test_the_restored_map_is_identical_to_the_one_written(app_engine: AsyncEngine) -> None:
    fitted = fit_calibration(
        _informative_record(200),
        strategy_id=_STRATEGY_ID,
        as_of_utc=_EPOCH + timedelta(days=30),
        parameters=_PARAMETERS,
    )
    assert fitted.is_fitted, "a flat map would make this round trip prove nothing"
    assert len(fitted.buckets) == _EXPECTED_BUCKET_COUNT
    # One ninth, to the last place the column holds. If this stops being true the round
    # trip below is passing on values that fit comfortably inside `NUMERIC(38, 18)` and
    # would survive any handling of scale at all.
    assert fitted.buckets[1].calibrated_fraction == Decimal("0.111111111111111111")

    async with app_engine.begin() as connection:
        await _write(connection, fitted)
        restored = await _read_as_of(connection, _EPOCH + timedelta(days=365))

    assert restored == fitted
    # Equality on `Decimal` is numeric, so it would accept a value that changed scale on
    # the way through the column. The exact text is what a later reader sees.
    assert restored is not None
    assert [str(bucket.calibrated_fraction) for bucket in restored.buckets] == [
        str(bucket.calibrated_fraction) for bucket in fitted.buckets
    ]


@pytest.mark.asyncio
async def test_the_as_of_read_returns_the_map_that_had_been_fitted_by_then(
    app_engine: AsyncEngine,
) -> None:
    """Two fits, one decision instant between them. The later map must not be visible.

    This is the same experiment `feature_as_of()` runs against the feature store, applied
    to risk state -- and it is the one the risk engine would otherwise fail, because
    nothing about "load the calibration map" suggests a point-in-time query.
    """
    record = _informative_record(200)
    earlier_at = _EPOCH + timedelta(hours=150)
    later_at = _EPOCH + timedelta(hours=250)
    earlier = fit_calibration(
        record, strategy_id=_STRATEGY_ID, as_of_utc=earlier_at, parameters=_PARAMETERS
    )
    later = fit_calibration(
        record, strategy_id=_STRATEGY_ID, as_of_utc=later_at, parameters=_PARAMETERS
    )
    assert earlier.observation_count < later.observation_count

    async with app_engine.begin() as connection:
        await _write(connection, earlier)
        await _write(connection, later)

        assert await _read_as_of(connection, earlier_at) == earlier
        assert await _read_as_of(connection, later_at - timedelta(seconds=1)) == earlier
        assert await _read_as_of(connection, later_at) == later
        assert await _read_as_of(connection, earlier_at - timedelta(seconds=1)) is None


@pytest.mark.asyncio
async def test_two_fits_at_the_same_instant_are_refused(app_engine: AsyncEngine) -> None:
    """Both would be "the" map at that moment, and the as-of read would pick arbitrarily."""
    fitted = fit_calibration(
        _informative_record(200),
        strategy_id=_STRATEGY_ID,
        as_of_utc=_EPOCH + timedelta(days=30),
        parameters=_PARAMETERS,
    )
    async with app_engine.begin() as connection:
        await _write(connection, fitted)
        with pytest.raises(IntegrityError, match="uq_risk_conviction_map_strategy_id"):
            await _write(connection, fitted)


@pytest.mark.asyncio
async def test_a_persisted_map_cannot_be_rewritten(engine: AsyncEngine) -> None:
    """The trigger, exercised on the column an incident would be tempted to fix.

    Raising a strategy's top-bucket calibrated fraction by hand is the exact repair
    somebody reaches for when a strategy "should be sized larger", and it would rewrite
    history rather than record a new fit.

    Run as the migrator rather than the application role on purpose: `fking_app` has no
    `UPDATE` grant at all, so it would fail one layer earlier and never reach the trigger.
    The grant is asserted by `test_the_application_role_cannot_update_or_delete`, which is
    parametrised over `APPEND_ONLY_TABLES` and therefore already covers both tables here;
    this test covers the backstop for the migration that later widens that grant.
    """
    fitted = fit_calibration(
        _informative_record(200),
        strategy_id=_STRATEGY_ID,
        as_of_utc=_EPOCH + timedelta(days=30),
        parameters=_PARAMETERS,
    )
    async with engine.begin() as connection:
        map_id = await _write(connection, fitted)

    async with engine.begin() as connection:
        with pytest.raises(DBAPIError, match="append-only"):
            await connection.execute(
                sa.text(
                    "UPDATE risk_conviction_map_bucket SET calibrated_fraction = 1 "
                    "WHERE map_id = :map_id AND bucket_index = 0"
                ),
                {"map_id": map_id},
            )
