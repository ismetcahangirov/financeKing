"""The as-of guarantee for alternative series, against a real database.

Two questions this file answers that no in-process test can, because the answers live in
grants, a `SECURITY DEFINER` body and a `CHECK` constraint rather than in Python
(`CLAUDE.md` section 5):

1. **A funding rate settled at 08:00 is invisible at `as_of=08:00` and visible at
   `as_of=08:00 + lag`.** The lag is one minute for this source, so the two reads are a
   minute apart and return different things. That single minute is the whole contract: a
   join on `event_time` would return the value at 08:00 and the backtest would be trading
   on a number that had not been broadcast yet.
2. **A revision is a new row, and an `as_of` before it returns the first print.** This is
   the macro case and it is where a mistake is most expensive: GDP is restated weeks
   later, sometimes materially, and a backfill over the original would make every
   historical backtest a test of a belief nobody held at the time.

Plus the two structural claims underneath both: `fking_app` cannot read
`alt_observations` at all, and `available_at_utc <= event_time_utc` is refused by the
database rather than by the writer.
"""

from __future__ import annotations

import hashlib
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from pathlib import Path

import pytest
import sqlalchemy as sa
from sqlalchemy.exc import IntegrityError, ProgrammingError
from sqlalchemy.ext.asyncio import AsyncEngine

from fking.data.alt import (
    ALT_MARKET,
    ARCHIVE_DATASET,
    ARCHIVE_GRANULARITY,
    AltObservation,
    AltObservationWriter,
    PostgresAltStore,
    ingest_alt_period,
)
from fking.data.alt.registry import (
    BINANCE_FUNDING_RATE,
    BINANCE_OPEN_INTEREST,
    FEAR_AND_GREED,
    FRED_RELEASES,
)
from fking.data.archive import ArchiveCoordinate, ArchiveFetcher, archive_url
from fking.platform.errors import DataUnavailableError, FeatureContractError
from tests.support import alt_fixtures
from tests.support.archive_stub import StubArchiveEgress

pytestmark = [pytest.mark.integration, pytest.mark.slow]

_SETTLEMENT = datetime(2026, 3, 1, 8, 0, tzinfo=UTC)
_FUNDING = BINANCE_FUNDING_RATE.series("BTCUSDT")
_GDP = FRED_RELEASES.series("GDPC1")
_LOOKBACK = timedelta(days=1)
# A first print and one restatement of the same observation period.
_FIRST_PRINT_AND_REVISION = 2

_RAW_INSERT = sa.text(
    """
    INSERT INTO alt_observations (
        source_id, series_id, event_time_utc, available_at_utc, observed_value
    )
    VALUES (:source_id, :series_id, :event_time_utc, :available_at_utc, :observed_value)
    """
)


# ---------------------------------------------------------------------------
# 1. A funding rate is invisible at its own settlement instant
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_a_funding_rate_is_invisible_at_settlement_and_visible_after_the_lag(
    app_engine: AsyncEngine, ingest_engine: AsyncEngine
) -> None:
    writer = AltObservationWriter(ingest_engine)
    written = await writer.append(
        [
            BINANCE_FUNDING_RATE.point(
                _FUNDING,
                AltObservation(event_time_utc=_SETTLEMENT, observed_value=Decimal("0.00037409")),
            )
        ]
    )
    assert written == 1

    store = PostgresAltStore(app_engine)
    lag = BINANCE_FUNDING_RATE.availability_lag

    at_settlement = await store.load(_FUNDING, as_of=_SETTLEMENT, lookback=_LOOKBACK)
    just_before_publication = await store.load(
        _FUNDING, as_of=_SETTLEMENT + lag - timedelta(seconds=1), lookback=_LOOKBACK
    )
    at_publication = await store.load(_FUNDING, as_of=_SETTLEMENT + lag, lookback=_LOOKBACK)

    assert at_settlement.values == ()
    assert just_before_publication.values == ()
    assert len(at_publication.values) == 1
    assert at_publication.values[0].event_time_utc == _SETTLEMENT
    assert at_publication.values[0].observed_value == Decimal("0.00037409")


@pytest.mark.asyncio
async def test_the_series_carries_the_question_it_answered(app_engine: AsyncEngine) -> None:
    """A series detached from the instant it was read at cannot be checked for look-ahead
    afterwards, and the audit requirement is reconstruction months later."""
    store = PostgresAltStore(app_engine)
    as_of = _SETTLEMENT + timedelta(hours=1)

    answer = await store.load(_FUNDING, as_of=as_of, lookback=_LOOKBACK)

    assert answer.as_of == as_of
    assert answer.lookback == _LOOKBACK
    assert answer.series == _FUNDING


# ---------------------------------------------------------------------------
# 2. A revision is a new row, and an earlier as-of returns the first print
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_a_revision_is_a_new_row_and_an_earlier_as_of_returns_the_first_print(
    app_engine: AsyncEngine, ingest_engine: AsyncEngine
) -> None:
    observation_period_end = datetime(2026, 6, 30, tzinfo=UTC)
    first_print_at = datetime(2026, 7, 30, 12, 30, tzinfo=UTC)
    revised_at = datetime(2026, 8, 27, 12, 30, tzinfo=UTC)

    writer = AltObservationWriter(ingest_engine)
    written = await writer.append(
        [
            FRED_RELEASES.point(
                _GDP,
                AltObservation(
                    event_time_utc=observation_period_end,
                    observed_value=Decimal("2.1"),
                    published_at_utc=first_print_at,
                ),
            ),
            FRED_RELEASES.point(
                _GDP,
                AltObservation(
                    event_time_utc=observation_period_end,
                    observed_value=Decimal("1.4"),
                    published_at_utc=revised_at,
                ),
            ),
        ]
    )

    # Two rows, not one updated row. Same event time, different availability.
    assert written == _FIRST_PRINT_AND_REVISION

    store = PostgresAltStore(app_engine)
    lookback = timedelta(days=120)
    before_the_first_print = await store.load(
        _GDP, as_of=first_print_at - timedelta(seconds=1), lookback=lookback
    )
    between_the_two = await store.load(
        _GDP, as_of=revised_at - timedelta(seconds=1), lookback=lookback
    )
    after_the_revision = await store.load(_GDP, as_of=revised_at, lookback=lookback)

    assert before_the_first_print.values == ()
    assert [entry.observed_value for entry in between_the_two.values] == [Decimal("2.1")]
    assert [entry.observed_value for entry in after_the_revision.values] == [Decimal("1.4")]


@pytest.mark.asyncio
async def test_re_ingesting_the_same_observation_writes_nothing(
    ingest_engine: AsyncEngine,
) -> None:
    """Re-running a backfill over a month already held is a no-op, not a duplicate."""
    writer = AltObservationWriter(ingest_engine)
    point = BINANCE_FUNDING_RATE.point(
        _FUNDING,
        AltObservation(
            event_time_utc=datetime(2026, 4, 1, 8, 0, tzinfo=UTC),
            observed_value=Decimal("0.0001"),
        ),
    )

    assert await writer.append([point]) == 1
    assert await writer.append([point]) == 0


# ---------------------------------------------------------------------------
# 3. The structural guarantees under both
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_the_application_role_cannot_read_the_table_at_all(
    app_engine: AsyncEngine,
) -> None:
    """Not "should not" -- `permission denied`, from a connection running as that role.
    A look-ahead defect becomes a failure to connect rather than a review miss."""
    async with app_engine.connect() as connection:
        with pytest.raises(ProgrammingError, match="permission denied"):
            await connection.execute(sa.text("SELECT * FROM alt_observations"))


@pytest.mark.asyncio
async def test_an_availability_at_or_before_the_event_is_refused_by_the_database(
    ingest_engine: AsyncEngine,
) -> None:
    """Strictly greater here, unlike `feature_values`: everything in this table was
    published by a third party after the instant it stamps, so a zero lag is the
    permissive answer and it is refused at both ends."""
    async with ingest_engine.begin() as connection:
        with pytest.raises(IntegrityError, match="availability_follows_event"):
            await connection.execute(
                _RAW_INSERT,
                {
                    "source_id": BINANCE_FUNDING_RATE.source_id,
                    "series_id": "BTCUSDT",
                    "event_time_utc": _SETTLEMENT,
                    "available_at_utc": _SETTLEMENT,
                    "observed_value": Decimal("0.0001"),
                },
            )


@pytest.mark.asyncio
async def test_an_unregistered_source_id_is_refused_by_the_database(
    ingest_engine: AsyncEngine,
) -> None:
    """A row whose source nobody declared is a row whose availability lag nobody stated."""
    async with ingest_engine.begin() as connection:
        with pytest.raises(IntegrityError, match="source_id_is_known"):
            await connection.execute(
                _RAW_INSERT,
                {
                    "source_id": "binance.somethingElse",
                    "series_id": "BTCUSDT",
                    "event_time_utc": _SETTLEMENT,
                    "available_at_utc": _SETTLEMENT + timedelta(minutes=1),
                    "observed_value": Decimal("0.0001"),
                },
            )


@pytest.mark.asyncio
async def test_the_ingest_role_may_append_and_may_not_rewrite(
    ingest_engine: AsyncEngine,
) -> None:
    """An observation that can be UPDATEd is an observation whose first print can be
    rewritten to match a backtest."""
    async with ingest_engine.begin() as connection:
        await connection.execute(
            _RAW_INSERT,
            {
                "source_id": BINANCE_FUNDING_RATE.source_id,
                "series_id": "ETHUSDT",
                "event_time_utc": _SETTLEMENT,
                "available_at_utc": _SETTLEMENT + timedelta(minutes=1),
                "observed_value": Decimal("0.0001"),
            },
        )

    async with ingest_engine.connect() as connection:
        with pytest.raises(ProgrammingError, match="permission denied"):
            await connection.execute(
                sa.text(
                    "UPDATE alt_observations SET observed_value = 0 WHERE series_id = 'ETHUSDT'"
                )
            )


# ---------------------------------------------------------------------------
# 4. The pieces compose: verified archive in, as-of read out
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_a_recorded_month_ingests_and_is_then_readable_only_after_the_lag(
    app_engine: AsyncEngine, ingest_engine: AsyncEngine, tmp_path: Path
) -> None:
    """The whole path, from the bytes Binance actually served to a point-in-time read.

    The stub host serves the *recorded* archive under its real URL, so the checksum
    verification, the zip unwrap, the header gate and the parse are the production ones;
    only the socket is replaced.
    """
    recorded = alt_fixtures.funding_rate_archive()
    series = BINANCE_FUNDING_RATE.series(recorded.symbol)
    coordinate = ArchiveCoordinate(
        market=ALT_MARKET,
        dataset=ARCHIVE_DATASET[series.source_id],
        symbol=recorded.symbol,
        archive_date=recorded.archive_date,
    )
    granularity = ARCHIVE_GRANULARITY[series.source_id]
    url = archive_url(coordinate, granularity)
    payload = recorded.read()
    assert hashlib.sha256(payload).hexdigest() == recorded.archive_sha256

    fetcher = ArchiveFetcher(egress=StubArchiveEgress({url: payload}), cache_root=tmp_path)
    outcome = await ingest_alt_period(
        fetcher,
        AltObservationWriter(ingest_engine),
        series=series,
        archive_date=recorded.archive_date,
        now_utc=alt_fixtures.NOW_UTC,
    )

    assert outcome.archive_sha256_hex == recorded.archive_sha256
    assert outcome.observations_parsed == outcome.observations_written
    assert outcome.observations_written == recorded.member_line_count - 1  # minus the header

    store = PostgresAltStore(app_engine)
    first_settlement = datetime(2020, 1, 1, tzinfo=UTC)
    lag = BINANCE_FUNDING_RATE.availability_lag

    at_settlement = await store.load(series, as_of=first_settlement, lookback=timedelta(days=1))
    after_lag = await store.load(series, as_of=first_settlement + lag, lookback=timedelta(days=1))

    assert at_settlement.values == ()
    assert [entry.observed_value for entry in after_lag.values] == [Decimal("-0.00012359")]


@pytest.mark.asyncio
async def test_a_zero_lookback_is_refused(app_engine: AsyncEngine) -> None:
    """A zero window returns nothing and reads downstream as "this source has no
    history", which is a different claim from "this window holds nothing"."""
    store = PostgresAltStore(app_engine)

    with pytest.raises(FeatureContractError, match="lookback must be positive"):
        await store.load(_FUNDING, as_of=_SETTLEMENT, lookback=timedelta(0))


@pytest.mark.asyncio
async def test_appending_nothing_writes_nothing_and_opens_no_transaction(
    ingest_engine: AsyncEngine,
) -> None:
    """An empty archive period is an ordinary answer for a symbol listed mid-month."""
    assert await AltObservationWriter(ingest_engine).append([]) == 0


@pytest.mark.asyncio
async def test_a_source_with_an_archive_but_no_parser_is_refused_by_name(
    ingest_engine: AsyncEngine, tmp_path: Path
) -> None:
    """Open interest is fetchable and probeable today and not parseable, because its
    `create_time` is a naive datetime string that `ArchiveFormat` has no member for
    (VF-029, #155). The refusal names that rather than a fetcher pretending to work."""
    fetcher = ArchiveFetcher(egress=StubArchiveEgress({}), cache_root=tmp_path)

    with pytest.raises(DataUnavailableError, match="no parser in this repository"):
        await ingest_alt_period(
            fetcher,
            AltObservationWriter(ingest_engine),
            series=BINANCE_OPEN_INTEREST.series("BTCUSDT"),
            archive_date=date(2024, 1, 2),
            now_utc=alt_fixtures.NOW_UTC,
        )


@pytest.mark.asyncio
async def test_a_source_with_no_egress_cannot_be_ingested(
    ingest_engine: AsyncEngine, tmp_path: Path
) -> None:
    """Declared, measured, and refused — rather than a fetcher that pretends to work."""
    fetcher = ArchiveFetcher(egress=StubArchiveEgress({}), cache_root=tmp_path)

    with pytest.raises(DataUnavailableError, match="egress_not_provisioned"):
        await ingest_alt_period(
            fetcher,
            AltObservationWriter(ingest_engine),
            series=FEAR_AND_GREED.series("GLOBAL"),
            archive_date=date(2026, 8, 1),
            now_utc=alt_fixtures.NOW_UTC,
        )
