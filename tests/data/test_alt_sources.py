"""The alternative-source contract: what each source is late by, and where it starts.

Four claims, each failing differently if the mechanism is wrong:

1. Every registered source declares a strictly positive `availability_lag`, and a point
   built from an observation carries `available_at_utc = event_time_utc + lag`. This is
   the property the whole package exists for; a zero lag is a claim that a published
   number was knowable before its publisher wrote it.
2. A source that publishes its own release calendar overrides the declared lag with the
   real release instant, and a release instant that contradicts the lag is refused rather
   than taken. This is the macro case: no fixed offset can express an observation period
   in June and a release at 08:30 on 26 August.
3. The recorded funding archive parses into the settlements Binance actually filed,
   including the three shapes a hand-written fixture would have smoothed away.
4. A symbol's history starts where the archive says it starts, discovered by probing, and
   a window opening before that is refused rather than answered short.

`SOURCES.md` is asserted against the registry here too. The register and the code are two
statements of the same measurement, and documentation that has drifted from the code is
worse than none because it trains readers to skim.
"""

from __future__ import annotations

import re
from datetime import UTC, date, datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path
from typing import Final

import pytest
from hypothesis import given
from hypothesis import strategies as st

from fking.data.alt import (
    ALT_MARKET,
    ALT_SOURCES,
    ARCHIVE_DATASET,
    ARCHIVE_GRANULARITY,
    PARSED_SOURCES,
    AltObservation,
    AltSourceSpec,
    Delivery,
    Revision,
    parse_funding_rate_archive,
    probe_earliest_archive_date,
    registered,
)
from fking.data.alt.registry import BINANCE_FUNDING_RATE, BINANCE_OPEN_INTEREST, FRED_RELEASES
from fking.data.archive import ArchiveCoordinate, archive_url
from fking.data.format_resolver import ALT_DATASETS, resolve_archive_format
from fking.data.loaders import extract_single_member
from fking.platform.errors import DataIntegrityError, DataUnavailableError, FeatureContractError
from tests.support import alt_fixtures
from tests.support.archive_stub import StubArchiveEgress

pytestmark = pytest.mark.unit

REPO_ROOT: Final[Path] = Path(__file__).resolve().parents[2]
NOW_UTC: Final[datetime] = alt_fixtures.NOW_UTC

# 31 days in January, three settlements a day. Stated rather than counted from the file,
# because "the parser returned what the file held" is satisfied by a parser that returns
# whatever it was given, including a truncated month.
JANUARY_2020_SETTLEMENTS: Final[int] = 93

_ALL_SOURCES = sorted(ALT_SOURCES.values(), key=lambda source: source.source_id)
_SOURCE_IDS = [source.source_id for source in _ALL_SOURCES]


# ---------------------------------------------------------------------------
# 1. Every source declares a lag, and the lag is what a point carries
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("source", _ALL_SOURCES, ids=_SOURCE_IDS)
def test_every_source_declares_a_strictly_positive_availability_lag(
    source: AltSourceSpec,
) -> None:
    """Parametrised over the registry, so a source added later is covered by construction."""
    assert source.availability_lag > timedelta(0)
    assert source.cadence > timedelta(0)


@pytest.mark.parametrize("source", _ALL_SOURCES, ids=_SOURCE_IDS)
def test_available_at_is_the_event_time_plus_the_declared_lag(source: AltSourceSpec) -> None:
    event_time_utc = datetime(2026, 3, 1, 8, 0, tzinfo=UTC)
    point = source.point(
        source.series("BTCUSDT"),
        AltObservation(event_time_utc=event_time_utc, observed_value=Decimal("0.0001")),
    )

    assert point.available_at_utc == event_time_utc + source.availability_lag
    assert point.available_at_utc > point.event_time_utc


@pytest.mark.parametrize("source", _ALL_SOURCES, ids=_SOURCE_IDS)
def test_every_source_states_its_terms_position_and_the_origin_of_its_numbers(
    source: AltSourceSpec,
) -> None:
    """SOURCES.md requires the position on automated use *before* ingestion, not after."""
    assert source.terms_position.strip()
    assert source.provenance.strip()


@given(
    offset_seconds=st.integers(min_value=-315_360_000, max_value=315_360_000),
    source_index=st.integers(min_value=0, max_value=len(_ALL_SOURCES) - 1),
)
@pytest.mark.property
def test_the_lag_is_added_for_every_event_time_and_every_source(
    offset_seconds: int, source_index: int
) -> None:
    """The arithmetic, over ten years either side of an epoch, rather than one example.

    An example-based test confirms the case its author thought of. The case worth catching
    is a lag applied conditionally -- only when positive, only in the future, only when the
    caller passed something -- and that is a shape only a range finds.
    """
    source = _ALL_SOURCES[source_index]
    event_time_utc = datetime(2026, 1, 1, tzinfo=UTC) + timedelta(seconds=offset_seconds)
    point = source.point(
        source.series("X"),
        AltObservation(event_time_utc=event_time_utc, observed_value=Decimal("1")),
    )

    assert point.available_at_utc - point.event_time_utc == source.availability_lag


def test_a_naive_event_time_is_refused_rather_than_assumed_to_be_utc() -> None:
    with pytest.raises(DataIntegrityError, match="timezone-aware"):
        BINANCE_FUNDING_RATE.point(
            BINANCE_FUNDING_RATE.series("BTCUSDT"),
            AltObservation(
                event_time_utc=datetime(2026, 3, 1, 8, 0),  # noqa: DTZ001 -- the subject
                observed_value=Decimal("0.0001"),
            ),
        )


def test_a_point_cannot_be_built_from_another_sources_declaration() -> None:
    """The one step at which a series could inherit a lag that was never measured for it."""
    with pytest.raises(DataIntegrityError, match="does not belong to source"):
        BINANCE_FUNDING_RATE.point(
            BINANCE_OPEN_INTEREST.series("BTCUSDT"),
            AltObservation(
                event_time_utc=datetime(2026, 3, 1, 8, 0, tzinfo=UTC),
                observed_value=Decimal("1"),
            ),
        )


def test_a_zero_lag_cannot_be_declared() -> None:
    """`FeatureSpec` permits a zero lag for a value derived from a closed bar. Nothing here
    is such a value: every row is something a third party published afterwards."""
    with pytest.raises(FeatureContractError, match="availability_lag"):
        AltSourceSpec(
            source_id="test.instant",
            delivery=Delivery.EGRESS_NOT_PROVISIONED,
            availability_lag=timedelta(0),
            cadence=timedelta(hours=1),
            unit="widgets",
            requires_credential=False,
            revision=Revision.FINAL,
            terms_position="a sentence long enough to be checkable by the guard",
            provenance="measured on a date, by a method, and written down here",
        )


def test_a_non_positive_cadence_is_refused() -> None:
    """A source that never publishes has no series, and its cadence sizes the gap
    detector — a zero there makes every window look complete."""
    with pytest.raises(FeatureContractError, match="cadence"):
        AltSourceSpec(
            source_id="test.silent",
            delivery=Delivery.EGRESS_NOT_PROVISIONED,
            availability_lag=timedelta(hours=1),
            cadence=timedelta(0),
            unit="widgets",
            requires_credential=False,
            revision=Revision.FINAL,
            terms_position="a sentence long enough to be checkable by the guard",
            provenance="measured on a date, by a method, and written down here",
        )


def test_an_offset_but_non_utc_timestamp_is_refused_rather_than_converted() -> None:
    """`astimezone(UTC)` would launder an offset guessed wrong upstream into a confident
    value, and in a 24/7 market there is no session boundary to make the shift visible."""
    baku = timezone(timedelta(hours=4))
    with pytest.raises(DataIntegrityError, match="must be UTC"):
        BINANCE_FUNDING_RATE.point(
            BINANCE_FUNDING_RATE.series("BTCUSDT"),
            AltObservation(
                event_time_utc=datetime(2026, 3, 1, 12, 0, tzinfo=baku),
                observed_value=Decimal("0.0001"),
            ),
        )


def test_a_blank_unit_is_refused() -> None:
    """The unit is declared once per source and never per row, so a blank one leaves every
    value in the store dimensionless."""
    with pytest.raises(FeatureContractError, match="unit"):
        AltSourceSpec(
            source_id="test.unitless",
            delivery=Delivery.EGRESS_NOT_PROVISIONED,
            availability_lag=timedelta(hours=1),
            cadence=timedelta(hours=1),
            unit="   ",
            requires_credential=False,
            revision=Revision.FINAL,
            terms_position="a sentence long enough to be checkable by the guard",
            provenance="measured on a date, by a method, and written down here",
        )


def test_a_placeholder_terms_position_is_refused() -> None:
    with pytest.raises(FeatureContractError, match="terms_position"):
        AltSourceSpec(
            source_id="test.undocumented",
            delivery=Delivery.EGRESS_NOT_PROVISIONED,
            availability_lag=timedelta(hours=1),
            cadence=timedelta(hours=1),
            unit="widgets",
            requires_credential=False,
            revision=Revision.FINAL,
            terms_position="tbd",
            provenance="measured on a date, by a method, and written down here",
        )


def test_an_unregistered_source_is_refused_and_the_refusal_names_the_real_ones() -> None:
    with pytest.raises(DataUnavailableError, match=re.escape("binance.fundingRate")):
        registered("binance.somethingElse")


# ---------------------------------------------------------------------------
# 2. A published release calendar beats the declared lag
# ---------------------------------------------------------------------------


def test_a_published_release_instant_overrides_the_declared_lag() -> None:
    """The macro case. Q2 GDP has an observation period in June and a release in August;
    a fixed offset cannot express that, and inferring one from the observation date is
    look-ahead by weeks."""
    observation_period_end = datetime(2026, 6, 30, tzinfo=UTC)
    released_at = datetime(2026, 8, 26, 12, 30, tzinfo=UTC)  # 08:30 US Eastern

    point = FRED_RELEASES.point(
        FRED_RELEASES.series("GDPC1"),
        AltObservation(
            event_time_utc=observation_period_end,
            observed_value=Decimal("2.1"),
            published_at_utc=released_at,
        ),
    )

    assert point.event_time_utc == observation_period_end
    assert point.available_at_utc == released_at
    assert point.available_at_utc - point.event_time_utc > FRED_RELEASES.availability_lag


def test_a_release_instant_inside_the_declared_minimum_delay_is_refused() -> None:
    """Taking the earlier of the two would move the whole series forward in time."""
    with pytest.raises(DataIntegrityError, match="minimum publication delay"):
        FRED_RELEASES.point(
            FRED_RELEASES.series("GDPC1"),
            AltObservation(
                event_time_utc=datetime(2026, 6, 30, tzinfo=UTC),
                observed_value=Decimal("2.1"),
                published_at_utc=datetime(2026, 6, 30, 0, 30, tzinfo=UTC),
            ),
        )


# ---------------------------------------------------------------------------
# 3. The recorded funding archive
# ---------------------------------------------------------------------------


# The declaration `ingest_alt_period` resolves for the recorded month, resolved rather than
# constructed so these tests exercise the same table production reads (#155).
FUNDING_FORMAT: Final = alt_fixtures.funding_rate_archive().archive_format()


def _funding_observations() -> tuple[AltObservation, ...]:
    recorded = alt_fixtures.funding_rate_archive()
    member = extract_single_member(recorded.read(), source=recorded.label)
    return parse_funding_rate_archive(
        member, source=recorded.label, now_utc=NOW_UTC, archive_format=FUNDING_FORMAT
    )


def test_the_whole_recorded_month_parses_into_every_settlement_it_holds() -> None:
    observations = _funding_observations()

    assert len(observations) == JANUARY_2020_SETTLEMENTS
    assert observations[0].event_time_utc == datetime(2020, 1, 1, tzinfo=UTC)
    assert observations[0].observed_value == Decimal("-0.00012359")


def test_negative_rates_survive_the_parse() -> None:
    """Shorts paying longs is an ordinary market state, not an anomaly to reject. A parser
    that refused them would delete exactly the regime a carry strategy is short in."""
    assert any(observation.observed_value < 0 for observation in _funding_observations())


def test_a_rate_in_scientific_notation_parses_exactly() -> None:
    """`8.4E-7` is in the recording. A digit-only pattern would reject it, and a `float`
    round-trip would not be exact -- which is why the parser builds from the source text."""
    values = {observation.observed_value for observation in _funding_observations()}
    assert Decimal("8.4E-7") in values


def test_settlements_are_not_exactly_on_the_eight_hour_boundary() -> None:
    """The recording carries `1577923200002` -- two milliseconds late. A parser or a
    downstream join that assumed an exact boundary would drop that settlement."""
    off_boundary = [
        observation
        for observation in _funding_observations()
        if observation.event_time_utc.second or observation.event_time_utc.microsecond
    ]
    assert off_boundary


def test_a_settlement_interval_that_is_not_the_declared_cadence_refuses_the_file() -> None:
    """Binance has changed the interval on individual perpetuals. When it does, every rate
    in the file means something else, and carry summed at the wrong interval is wrong by
    that ratio."""
    mutated = b"calc_time,funding_interval_hours,last_funding_rate\n1577836800000,4,-0.00012359\n"
    with pytest.raises(DataIntegrityError, match="funding_interval_hours"):
        parse_funding_rate_archive(
            mutated, source="mutated", now_utc=NOW_UTC, archive_format=FUNDING_FORMAT
        )


def test_a_duplicate_settlement_refuses_the_file() -> None:
    """The store keys on `(series, event_time, available_at)`, so a duplicate would collapse
    two settlements into one silently rather than raising at the insert."""
    mutated = (
        b"calc_time,funding_interval_hours,last_funding_rate\n"
        b"1577836800000,8,-0.00012359\n"
        b"1577836800000,8,0.00012359\n"
    )
    with pytest.raises(DataIntegrityError, match="not after the previous"):
        parse_funding_rate_archive(
            mutated, source="mutated", now_utc=NOW_UTC, archive_format=FUNDING_FORMAT
        )


def test_a_file_without_the_declared_header_is_refused() -> None:
    """Trap 2 in its funding form: reading a headed file as headless files the column names
    as a settlement, and reading a headless one as headed discards a real settlement."""
    mutated = b"1577836800000,8,-0.00012359\n"
    with pytest.raises(DataIntegrityError, match="has no header"):
        parse_funding_rate_archive(
            mutated, source="mutated", now_utc=NOW_UTC, archive_format=FUNDING_FORMAT
        )


@pytest.mark.parametrize(
    ("row", "expected"),
    [
        (b"1577836800000,8", "holds 2 columns"),
        (b"1577836800000,8,-0.0001,extra", "holds 4 columns"),
        (b"not-an-epoch,8,-0.0001", "not a base-10 integer"),
        (b"1577836800,8,-0.0001", "implausible"),
        (b"1577836800000,eight,-0.0001", "not a non-negative base-10 integer"),
        (b"1577836800000,8, -0.0001 ", "not plain decimal notation"),
        (b"1577836800000,8,NaN", "not plain decimal notation"),
        (b"1577836800000,8,1_0", "not plain decimal notation"),
        (b"1577836800000,8,2", "whole turn of the notional"),
    ],
    ids=[
        "short_row",
        "extra_column",
        "non_integer_epoch",
        "seconds_read_as_milliseconds",
        "non_integer_interval",
        "padded_decimal",
        "nan",
        "underscore_separated",
        "absurd_magnitude",
    ],
)
def test_every_malformed_row_refuses_the_whole_file(row: bytes, expected: str) -> None:
    """A ninety-row month has no rejection budget.

    The corpus driver tallies a bad row and continues, because one bad print in 3.5
    million must not stop a backfill. At this size a rejection ceiling that tolerated one
    row would be 1.1% -- eleven times looser than the corpus ceiling, and wide enough to
    admit exactly the uniform drift it exists to catch. Three of these cases are the ones
    a permissive `Decimal()` would silently accept as a *different number*: `" -0.0001 "`
    strips, `1_0` is ten, and `NaN` constructs happily and then compares False forever.
    """
    payload = b"calc_time,funding_interval_hours,last_funding_rate\n" + row + b"\n"
    with pytest.raises(DataIntegrityError, match=re.escape(expected)):
        parse_funding_rate_archive(
            payload, source="mutated", now_utc=NOW_UTC, archive_format=FUNDING_FORMAT
        )


def test_a_file_with_no_rows_at_all_parses_to_nothing() -> None:
    """A header and no settlements is a real answer for a symbol listed mid-month, not an
    error. Inventing a row here would be the one thing the corpus never does."""
    header = b"calc_time,funding_interval_hours,last_funding_rate\n"

    parsed = parse_funding_rate_archive(
        header, source="empty", now_utc=NOW_UTC, archive_format=FUNDING_FORMAT
    )

    assert parsed == ()


def test_a_naive_reference_instant_is_refused() -> None:
    """`now_utc` moves the plausibility window by its offset, so a naive one moves it by
    whatever the machine's timezone happens to be."""
    with pytest.raises(DataIntegrityError, match="timezone-aware"):
        parse_funding_rate_archive(
            b"calc_time,funding_interval_hours,last_funding_rate\n",
            source="empty",
            now_utc=datetime(2026, 8, 5),  # noqa: DTZ001 -- the subject
            archive_format=FUNDING_FORMAT,
        )


def test_a_parser_handed_the_wrong_declaration_refuses_before_reading_a_row() -> None:
    """The declaration is resolved by the caller, so passing the wrong one is reachable.

    `metrics` stamps a naive datetime string, so `require_epoch_unit()` refuses rather than
    letting the funding parser read `1577836800000` as a date -- which would also fail, but
    with a message about a malformed row rather than about the wrong declaration.
    """
    metrics_format = alt_fixtures.metrics_archive().archive_format()

    with pytest.raises(DataIntegrityError, match="not an epoch"):
        parse_funding_rate_archive(
            b"calc_time,funding_interval_hours,last_funding_rate\n1577836800000,8,-0.0001\n",
            source="mutated",
            now_utc=NOW_UTC,
            archive_format=metrics_format,
        )


# ---------------------------------------------------------------------------
# 4. Where a symbol's history actually starts
# ---------------------------------------------------------------------------


def _monthly_funding_urls(symbol: str, *, first: date, last: date) -> dict[str, bytes]:
    """A stub archive host holding one funding month per month in `[first, last]`."""
    urls: dict[str, bytes] = {}
    cursor = first.replace(day=1)
    while cursor <= last:
        urls[
            archive_url(
                ArchiveCoordinate(
                    market=ALT_MARKET,
                    dataset=ARCHIVE_DATASET[BINANCE_FUNDING_RATE.source_id],
                    symbol=symbol,
                    archive_date=cursor,
                ),
                ARCHIVE_GRANULARITY[BINANCE_FUNDING_RATE.source_id],
            )
        ] = b"stand-in bytes; the probe reads the CHECKSUM sibling, never the archive"
        cursor = (cursor.replace(day=28) + timedelta(days=4)).replace(day=1)
    return urls


@pytest.mark.asyncio
async def test_the_probe_finds_the_month_the_series_really_starts() -> None:
    """BTCUSDT's perpetual listed 2019-09-08 and its funding archive begins 2020-01. A
    backtest that assumed the listing date would run four empty months, which reads
    downstream as no signal rather than as no data."""
    egress = StubArchiveEgress(
        _monthly_funding_urls("BTCUSDT", first=date(2020, 1, 1), last=date(2026, 7, 1))
    )

    availability = await probe_earliest_archive_date(
        egress,
        source_id=BINANCE_FUNDING_RATE.source_id,
        symbol="BTCUSDT",
        today_utc=date(2026, 8, 5),
        now_utc=NOW_UTC,
    )

    assert availability.earliest_archive_date == date(2020, 1, 1)
    assert availability.probe_request_count > 0
    # A linear walk from the 2019-09 genesis would be ~83 requests. Binary search over the
    # month index is what makes this affordable per symbol at startup.
    assert availability.probe_request_count < 12  # noqa: PLR2004 -- log2(83) plus two probes


@pytest.mark.asyncio
async def test_a_window_before_the_probed_start_is_refused_rather_than_answered_short() -> None:
    egress = StubArchiveEgress(
        _monthly_funding_urls("BTCUSDT", first=date(2020, 1, 1), last=date(2026, 7, 1))
    )
    availability = await probe_earliest_archive_date(
        egress,
        source_id=BINANCE_FUNDING_RATE.source_id,
        symbol="BTCUSDT",
        today_utc=date(2026, 8, 5),
        now_utc=NOW_UTC,
    )

    with pytest.raises(DataUnavailableError, match="begins at 2020-01-01"):
        availability.require_window(
            window_start_utc=datetime(2019, 9, 8, tzinfo=UTC),
            window_end_utc=datetime(2020, 6, 1, tzinfo=UTC),
        )

    # And the window that opens on the first archived day is answered.
    availability.require_window(
        window_start_utc=datetime(2020, 1, 1, tzinfo=UTC),
        window_end_utc=datetime(2020, 6, 1, tzinfo=UTC),
    )


@pytest.mark.asyncio
async def test_a_symbol_with_no_archive_at_all_is_refused_before_any_search() -> None:
    """Bracketing requires a period that exists. Probing earlier periods for a series that
    has stopped would report a start date for something that is not there."""
    egress = StubArchiveEgress({})

    with pytest.raises(DataUnavailableError, match="no archive at the most recent period"):
        await probe_earliest_archive_date(
            egress,
            source_id=BINANCE_FUNDING_RATE.source_id,
            symbol="NOTLISTEDUSDT",
            today_utc=date(2026, 8, 5),
            now_utc=NOW_UTC,
        )


@pytest.mark.asyncio
async def test_a_stranded_island_of_history_is_refused_rather_than_called_a_start() -> None:
    """The binary search assumes existence is monotone. The assumption is checked with one
    extra probe rather than trusted, because a hole immediately after the boundary would
    otherwise be reported as the start of a continuous series."""
    urls = _monthly_funding_urls("BTCUSDT", first=date(2020, 1, 1), last=date(2026, 7, 1))
    del urls[
        archive_url(
            ArchiveCoordinate(
                market=ALT_MARKET,
                dataset=ARCHIVE_DATASET[BINANCE_FUNDING_RATE.source_id],
                symbol="BTCUSDT",
                archive_date=date(2020, 2, 1),
            ),
            ARCHIVE_GRANULARITY[BINANCE_FUNDING_RATE.source_id],
        )
    ]
    egress = StubArchiveEgress(urls)

    with pytest.raises(DataUnavailableError, match="not monotone"):
        await probe_earliest_archive_date(
            egress,
            source_id=BINANCE_FUNDING_RATE.source_id,
            symbol="BTCUSDT",
            today_utc=date(2026, 8, 5),
            now_utc=NOW_UTC,
        )


def _daily_metrics_urls(symbol: str, *, first: date, last: date) -> dict[str, bytes]:
    """The daily counterpart of `_monthly_funding_urls`, for the open-interest series."""
    urls: dict[str, bytes] = {}
    cursor = first
    while cursor <= last:
        urls[
            archive_url(
                ArchiveCoordinate(
                    market=ALT_MARKET,
                    dataset=ARCHIVE_DATASET[BINANCE_OPEN_INTEREST.source_id],
                    symbol=symbol,
                    archive_date=cursor,
                ),
                ARCHIVE_GRANULARITY[BINANCE_OPEN_INTEREST.source_id],
            )
        ] = b"stand-in bytes; the probe reads the CHECKSUM sibling, never the archive"
        cursor += timedelta(days=1)
    return urls


@pytest.mark.asyncio
async def test_the_probe_finds_the_day_the_open_interest_series_really_starts() -> None:
    """The daily half of the search, and the second of the two boundaries in VF-028.

    Open interest begins 2020-09-01 for BTCUSDT -- almost a year after the corpus genesis
    and eight months after funding. The two dates are different, which is why neither can
    be derived from the other and both are probed.
    """
    egress = StubArchiveEgress(
        _daily_metrics_urls("BTCUSDT", first=date(2020, 9, 1), last=date(2026, 8, 4))
    )

    availability = await probe_earliest_archive_date(
        egress,
        source_id=BINANCE_OPEN_INTEREST.source_id,
        symbol="BTCUSDT",
        today_utc=date(2026, 8, 5),
        now_utc=NOW_UTC,
    )

    assert availability.earliest_archive_date == date(2020, 9, 1)
    # A linear walk from the 2019-09-08 genesis would be ~2,500 requests. Binary search
    # over the day index is what makes a daily series probeable per symbol at startup.
    assert availability.probe_request_count < 16  # noqa: PLR2004 -- log2(2523) plus two probes


@pytest.mark.asyncio
async def test_an_empty_window_is_refused_before_the_start_date_is_consulted() -> None:
    """A window that closes at or before it opens returns nothing from any store, which
    reads downstream as an absent signal rather than as a malformed question."""
    egress = StubArchiveEgress(
        _monthly_funding_urls("BTCUSDT", first=date(2020, 1, 1), last=date(2026, 7, 1))
    )
    availability = await probe_earliest_archive_date(
        egress,
        source_id=BINANCE_FUNDING_RATE.source_id,
        symbol="BTCUSDT",
        today_utc=date(2026, 8, 5),
        now_utc=NOW_UTC,
    )

    with pytest.raises(DataUnavailableError, match="empty window"):
        availability.require_window(
            window_start_utc=datetime(2021, 1, 1, tzinfo=UTC),
            window_end_utc=datetime(2021, 1, 1, tzinfo=UTC),
        )


@pytest.mark.asyncio
async def test_a_source_with_no_egress_cannot_be_probed_and_says_why() -> None:
    with pytest.raises(DataUnavailableError, match="egress_not_provisioned"):
        await probe_earliest_archive_date(
            StubArchiveEgress({}),
            source_id="alternative.me.fearGreed",
            symbol="GLOBAL",
            today_utc=date(2026, 8, 5),
            now_utc=NOW_UTC,
        )


# ---------------------------------------------------------------------------
# The register and the code state the same measurement
# ---------------------------------------------------------------------------


def test_sources_md_names_every_registered_source() -> None:
    """`SOURCES.md` is the human-facing register and this is the one code reads. A source
    in one and not the other is a measurement somebody can act on without seeing its terms."""
    register = (REPO_ROOT / "SOURCES.md").read_text(encoding="utf-8")
    missing = [source_id for source_id in ALT_SOURCES if source_id not in register]

    assert missing == []


def test_every_archive_delivered_source_declares_a_dataset_and_a_granularity() -> None:
    """Granularity is a property of the dataset, not of the calendar: `fundingRate` is
    monthly-only and `metrics` daily-only, so `resolve_granularity` is wrong for both in
    opposite directions and every fetch must state which it wants."""
    archive_delivered = {
        source.source_id for source in ALT_SOURCES.values() if source.delivery is Delivery.ARCHIVE
    }

    assert set(ARCHIVE_DATASET) == archive_delivered
    assert set(ARCHIVE_GRANULARITY) == archive_delivered


def test_a_parsed_source_is_always_an_archive_delivered_one() -> None:
    """A parser for a source with no egress is unreachable code.

    The converse -- an archive with no parser -- stays representable, and stayed the state
    of `binance.openInterest` between #32 and #155. It is not the state of anything today,
    which the next test asserts positively.
    """
    assert set(ARCHIVE_DATASET) >= PARSED_SOURCES


def test_every_archive_delivered_source_has_a_declared_format_and_a_parser() -> None:
    """`fking.data.alt`'s half of the drift check `tests/data/test_parsers.py` runs over the
    corpus tables. A declared format with no parser is a file that cannot be read; a parser
    for a dataset with no declared format would be inventing an encoding, which is exactly
    what `docs/adr/0013` exists to prevent."""
    for source_id, dataset in ARCHIVE_DATASET.items():
        resolved = resolve_archive_format(
            market=ALT_MARKET, dataset=dataset, archive_date=date(2024, 1, 2)
        )

        assert resolved.dataset is dataset
        assert dataset in ALT_DATASETS, f"{dataset.value} must be excluded from the corpus tables"
        assert source_id in PARSED_SOURCES


def test_source_ids_are_dotted_and_lowercase_at_the_publisher() -> None:
    """One spelling convention, so a source id in a log line, a CHECK constraint and a
    dashboard filter are the same string."""
    pattern = re.compile(r"\A[a-z][a-z0-9.]*\.[A-Za-z][A-Za-z0-9]*\Z")

    assert [source_id for source_id in ALT_SOURCES if not pattern.match(source_id)] == []
