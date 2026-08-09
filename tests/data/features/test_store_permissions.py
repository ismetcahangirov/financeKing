"""The guarantee, asserted against a real database as the roles that are subject to it.

This file lives beside the rest of `tests/data/` rather than at the
`tests/integration/...` path issue #29 names, because this repository groups tests by the
module they exercise and expresses the container/no-container split with the
`integration` marker instead. Selecting integration tests is `-m integration`, and
grouping by directory as well would give the same fact two homes that can disagree.

Four claims, each of which fails differently if the mechanism is wrong:

1. `fking_app` cannot read `feature_values` at all. Not "should not" -- `permission
   denied`, from a connection actually running as that role.
2. `fking_feature_as_of()` returns the value as it was *believed* at `as_of`, so a
   revision published later is invisible to an earlier read.
3. `available_at_utc < event_time_utc` is refused by the database, not by the writer.
4. `fking_ingest` may append and may not rewrite.

A mock would prove the mock is locked down. The interesting behaviour here is entirely
in grants, a `SECURITY DEFINER` body and a `CHECK` constraint, and none of those exist in
Python (`CLAUDE.md` section 5).
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest
import sqlalchemy as sa
from sqlalchemy.exc import DBAPIError, IntegrityError, ProgrammingError
from sqlalchemy.ext.asyncio import AsyncEngine

from fking.data.features.spec import FeaturePoint, FeatureRef
from fking.data.features.store import FeatureValueWriter, PostgresFeatureStore
from fking.data.format_resolver import Market
from tests.support.availability import permitting

pytestmark = [pytest.mark.integration, pytest.mark.slow]

_REF = FeatureRef(name="trailing_return_fraction", version=1, market=Market.SPOT, symbol="BTCUSDT")
_EVENT = datetime(2026, 3, 1, 12, 0, tzinfo=UTC)
_LOOKBACK = timedelta(hours=6)
_STEP_COUNT = 3

# Wide enough to cover every window read below and no wider. The availability contract is
# a required constructor argument, so these tests state what the corpus holds even though
# their subject is the grant matrix and the as-of bound (#30).
_AVAILABLE = permitting(
    earliest_event_time_utc=_EVENT - timedelta(days=1),
    latest_event_time_utc=_EVENT + timedelta(days=1),
)

_INSERT = sa.text(
    """
    INSERT INTO feature_values (
        feature_name, feature_version, market, symbol,
        event_time_utc, available_at_utc, feature_value
    )
    VALUES ('trailing_return_fraction', 1, 'spot', 'BTCUSDT',
            :event_time_utc, :available_at_utc, :feature_value)
    """
)


async def _append_raw(
    engine: AsyncEngine, *, event_time_utc: datetime, available_at_utc: datetime, decimal_text: str
) -> None:
    async with engine.begin() as connection:
        await connection.execute(
            _INSERT,
            {
                "event_time_utc": event_time_utc,
                "available_at_utc": available_at_utc,
                "feature_value": Decimal(decimal_text),
            },
        )


# ---------------------------------------------------------------------------
# 1. The application role cannot see the table
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "statement",
    [
        "SELECT * FROM feature_values",
        "SELECT count(*) FROM feature_values",
        "INSERT INTO feature_values DEFAULT VALUES",
        "UPDATE feature_values SET feature_value = 0",
        "DELETE FROM feature_values",
    ],
)
@pytest.mark.asyncio
async def test_the_application_role_holds_nothing_on_the_feature_table(
    app_engine: AsyncEngine, statement: str
) -> None:
    """`SELECT` is in the list on purpose.

    Revoking only the writes would leave the role able to run the read with no `as_of`
    at all, which is the read the whole design exists to make unavailable. A look-ahead
    defect is then `permission denied for table feature_values` rather than a review
    miss (`docs/rules/no-lookahead.md`).
    """
    async with app_engine.connect() as connection:
        with pytest.raises((ProgrammingError, DBAPIError)) as refused:
            await connection.execute(sa.text(statement))
    # asyncpg's own class name travels inside the wrapped message rather than as the
    # wrapper's type, so the assertion reads the text: SQLAlchemy re-raises every
    # asyncpg error as its own `ProgrammingError` and the specific condition would be
    # invisible to a check on the class.
    assert "permission denied for table feature_values" in str(refused.value).lower()
    assert "InsufficientPrivilegeError" in str(refused.value)


@pytest.mark.asyncio
async def test_the_application_role_can_call_the_as_of_reader(
    app_engine: AsyncEngine, ingest_engine: AsyncEngine
) -> None:
    """The other half. A lockdown that also blocks the sanctioned path is not a
    lockdown, it is an outage nobody notices until a strategy runs."""
    await _append_raw(
        ingest_engine, event_time_utc=_EVENT, available_at_utc=_EVENT, decimal_text="0.25"
    )
    series = await PostgresFeatureStore(app_engine, _AVAILABLE).load(
        _REF, as_of=_EVENT, lookback=_LOOKBACK
    )
    assert [entry.feature_value for entry in series.values] == [Decimal("0.25")]


@pytest.mark.asyncio
async def test_the_ingest_role_cannot_call_the_as_of_reader(ingest_engine: AsyncEngine) -> None:
    """The writer has no as-of read to perform, and `EXECUTE` defaults to PUBLIC.

    The grant matrix in `tests/platform/persistence/test_privileges.py` asserts the same
    thing from the catalogue over every declared routine; this asserts it as behaviour,
    from a connection that is actually subject to it.
    """
    async with ingest_engine.connect() as connection:
        with pytest.raises((ProgrammingError, DBAPIError)) as refused:
            await connection.execute(
                sa.text(
                    "SELECT * FROM fking_feature_as_of("
                    "'trailing_return_fraction', 1, 'spot', 'BTCUSDT', now(), INTERVAL '1 hour')"
                )
            )
    assert "permission denied" in str(refused.value).lower()


# ---------------------------------------------------------------------------
# 2. available_at_utc, not event_time_utc, governs visibility
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_a_revision_is_invisible_until_the_instant_it_was_published(
    app_engine: AsyncEngine, ingest_engine: AsyncEngine
) -> None:
    """The read returns what was *believed* at `as_of`, not what is true now.

    Both rows share an `event_time_utc` and differ in `available_at_utc`, which is what
    makes a correction a second row rather than an overwrite. A store that returned the
    latest revision regardless would make every historical backtest a test of knowledge
    nobody had at the time.
    """
    published_at = _EVENT + timedelta(hours=2)
    await _append_raw(
        ingest_engine, event_time_utc=_EVENT, available_at_utc=_EVENT, decimal_text="0.25"
    )
    await _append_raw(
        ingest_engine,
        event_time_utc=_EVENT,
        available_at_utc=published_at,
        decimal_text="0.31",
    )
    store = PostgresFeatureStore(app_engine, _AVAILABLE)

    before = await store.load(_REF, as_of=published_at - timedelta(seconds=1), lookback=_LOOKBACK)
    after = await store.load(_REF, as_of=published_at, lookback=_LOOKBACK)

    assert [entry.feature_value for entry in before.values] == [Decimal("0.25")]
    assert [entry.feature_value for entry in after.values] == [Decimal("0.31")]
    assert before.values[0].event_time_utc == after.values[0].event_time_utc


@pytest.mark.asyncio
async def test_a_value_published_after_the_as_of_is_not_returned_at_all(
    app_engine: AsyncEngine, ingest_engine: AsyncEngine
) -> None:
    """The plain case, and the one `WHERE event_time <= :t` gets wrong.

    The event happened before `as_of`; the system could not have known it until
    afterwards. Filtering on event time returns it, looks completely correct, and is a
    backtest that knew a correction before it was issued.
    """
    await _append_raw(
        ingest_engine,
        event_time_utc=_EVENT,
        available_at_utc=_EVENT + timedelta(hours=3),
        decimal_text="0.25",
    )
    series = await PostgresFeatureStore(app_engine, _AVAILABLE).load(
        _REF, as_of=_EVENT + timedelta(hours=1), lookback=_LOOKBACK
    )
    assert series.values == ()


@pytest.mark.asyncio
async def test_the_lookback_bounds_the_window_from_the_other_side(
    app_engine: AsyncEngine, ingest_engine: AsyncEngine
) -> None:
    """`(as_of - lookback, as_of]`, half-open at the old end.

    A value stamped exactly at the window start is outside it. Closing that end would
    make the window's width depend on whether an observation happens to land on the
    boundary.
    """
    for offset_hours, decimal_text in ((0, "0.10"), (5, "0.20"), (7, "0.30")):
        stamped = _EVENT - timedelta(hours=offset_hours)
        await _append_raw(
            ingest_engine,
            event_time_utc=stamped,
            available_at_utc=stamped,
            decimal_text=decimal_text,
        )
    series = await PostgresFeatureStore(app_engine, _AVAILABLE).load(
        _REF, as_of=_EVENT, lookback=timedelta(hours=6)
    )
    assert [entry.feature_value for entry in series.values] == [Decimal("0.20"), Decimal("0.10")]


# ---------------------------------------------------------------------------
# 3. The database refuses a value that claims to have been knowable too early
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_availability_before_the_event_is_refused_by_the_database(
    ingest_engine: AsyncEngine,
) -> None:
    """Nothing can be known before it happened, and the `CHECK` is what says so.

    In the writer it would be an assertion about the writer that ran; here it is a
    property of the table, which holds for a hand-written `INSERT` during an incident as
    well.
    """
    with pytest.raises(IntegrityError, match="availability_follows_event"):
        await _append_raw(
            ingest_engine,
            event_time_utc=_EVENT,
            available_at_utc=_EVENT - timedelta(seconds=1),
            decimal_text="0.25",
        )


# ---------------------------------------------------------------------------
# 4. The writer may append and may not rewrite
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "statement",
    ["UPDATE feature_values SET feature_value = 0", "DELETE FROM feature_values"],
)
@pytest.mark.asyncio
async def test_the_ingest_role_cannot_rewrite_a_feature_value(
    ingest_engine: AsyncEngine, statement: str
) -> None:
    """A feature value that can be `UPDATE`d is a feature value whose history can be
    rewritten to match a backtest that has already been run."""
    async with ingest_engine.connect() as connection:
        with pytest.raises((ProgrammingError, DBAPIError)) as refused:
            await connection.execute(sa.text(statement))
    assert "permission denied" in str(refused.value).lower()


@pytest.mark.asyncio
async def test_writing_the_same_point_twice_is_idempotent(ingest_engine: AsyncEngine) -> None:
    """Recomputation over an unchanged series is a no-op, so a re-run is safe.

    It also means a *different* value cannot land at coordinates that already hold one,
    which is the intended refusal: that is either a new definition, which needs a new
    version, or a correction, which needs a later `available_at_utc`.
    """
    writer = FeatureValueWriter(ingest_engine)
    points = (
        FeaturePoint(event_time_utc=_EVENT, available_at_utc=_EVENT, feature_value=Decimal("0.25")),
    )
    # The write is not inside the assert: `python -O` strips assert statements and would
    # take the INSERT with them, leaving a test that passes having written nothing.
    first_pass = await writer.append(_REF, points)
    second_pass = await writer.append(_REF, points)
    assert (first_pass, second_pass) == (1, 0)


@pytest.mark.asyncio
async def test_a_written_series_reads_back_through_the_as_of_path(
    app_engine: AsyncEngine, ingest_engine: AsyncEngine
) -> None:
    """The round trip, across both roles, in the order the system performs it."""
    writer = FeatureValueWriter(ingest_engine)
    points = tuple(
        FeaturePoint(
            event_time_utc=_EVENT - timedelta(minutes=15 * step),
            available_at_utc=_EVENT - timedelta(minutes=15 * step),
            feature_value=Decimal(f"0.0{step}"),
        )
        for step in range(_STEP_COUNT)
    )
    written = await writer.append(_REF, points)
    assert written == _STEP_COUNT

    series = await PostgresFeatureStore(app_engine, _AVAILABLE).load(
        _REF, as_of=_EVENT, lookback=_LOOKBACK
    )
    assert series.as_of == _EVENT
    assert series.feature == _REF
    assert [entry.event_time_utc for entry in series.values] == sorted(
        point.event_time_utc for point in points
    )
    assert all(isinstance(entry.feature_value, Decimal) for entry in series.values)
