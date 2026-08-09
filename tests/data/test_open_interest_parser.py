"""The `metrics` parser: a whole recorded day, and every way a day is refused.

Three claims, each failing differently:

1. The recorded day parses into the 288 samples Binance actually filed, every one of them
   aware UTC, with the value read exactly from the source text rather than through a
   `float`.
2. Every timestamp is aware UTC on **every** path. This is the defect the encoding exists
   for and it does not announce itself: a naive datetime is correct to the second, joins
   against nothing, and produces an empty feature rather than a wrong one.
3. A file that contradicts the declaration is refused whole. A day holds 288 rows, so
   there is no rejection budget -- one unparseable row at that size is the layout moving,
   not background noise.

The malformed payloads below are mutations of the real recording's shape, not inventions:
the header, the column count and the spacing all come from the file. `docs/rules/
testing-rules.md` bans hand-written *fixtures* precisely because an author cannot guess
what a venue emits; a mutation of a verified recording is the sanctioned way to reach an
error path the archive will not produce on demand.
"""

from __future__ import annotations

import itertools
import re
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Final

import pytest

from fking.data.alt import (
    METRICS_COLUMNS,
    AltObservation,
    parse_open_interest_archive,
)
from fking.data.alt.registry import BINANCE_OPEN_INTEREST
from fking.data.loaders import extract_single_member
from fking.platform.errors import DataIntegrityError
from tests.support import alt_fixtures

pytestmark = pytest.mark.unit

NOW_UTC: Final[datetime] = alt_fixtures.NOW_UTC

# One day at five-minute sampling. Stated rather than counted from the file, because "the
# parser returned what the file held" is satisfied by a parser that returns whatever it
# was given, including a truncated day.
SAMPLES_PER_DAY: Final[int] = 288

METRICS_FORMAT: Final = alt_fixtures.metrics_archive().archive_format()
HEADER: Final[bytes] = (",".join(METRICS_COLUMNS) + "\n").encode("ascii")


def _row(create_time: str, *, symbol: str = "BTCUSDT", open_interest: str = "76608.798") -> bytes:
    """One row in the recorded layout, with the two fields under test parameterised.

    The five columns nobody reads are filled with the recording's own first-row values, so
    a mutation is exactly one field away from a real archive line.
    """
    fields = (
        create_time,
        symbol,
        open_interest,
        "3388422457.29960000",
        "1.02072986",
        "1.25576533",
        "1.08284578",
        "1.16475026",
    )
    return (",".join(fields) + "\n").encode("ascii")


def _parse(payload: bytes, *, source: str = "mutated") -> tuple[AltObservation, ...]:
    return parse_open_interest_archive(
        payload, source=source, now_utc=NOW_UTC, archive_format=METRICS_FORMAT
    )


def _recorded_observations() -> tuple[AltObservation, ...]:
    recorded = alt_fixtures.metrics_archive()
    member = extract_single_member(recorded.read(), source=recorded.label)
    return parse_open_interest_archive(
        member,
        source=recorded.label,
        now_utc=NOW_UTC,
        archive_format=recorded.archive_format(),
    )


# ---------------------------------------------------------------------------
# 1. The recorded day
# ---------------------------------------------------------------------------


def test_the_whole_recorded_day_parses_into_every_sample_it_holds() -> None:
    observations = _recorded_observations()

    assert len(observations) == SAMPLES_PER_DAY
    assert observations[0].event_time_utc == datetime(2024, 1, 2, tzinfo=UTC)
    assert observations[-1].event_time_utc == datetime(2024, 1, 2, 23, 55, tzinfo=UTC)


def test_the_value_is_the_exact_decimal_binance_wrote() -> None:
    """`Decimal("76608.79800000")`, not `Decimal(76608.798)`. The second carries the
    double's rounding error before this code runs, and widening the type afterwards cannot
    undo it (`docs/rules/decimal-and-money.md`)."""
    first = _recorded_observations()[0]

    assert first.observed_value == Decimal("76608.79800000")
    assert str(first.observed_value) == "76608.79800000", "trailing zeros are the venue's own"


def test_every_timestamp_is_aware_utc() -> None:
    """The defect the whole encoding exists for. A naive datetime here is correct to the
    second, compares equal to nothing, joins against nothing, and produces an *empty*
    feature rather than a visibly wrong one."""
    for observation in _recorded_observations():
        assert observation.event_time_utc.tzinfo is not None
        assert observation.event_time_utc.utcoffset() == timedelta(0)


def test_the_recorded_day_holds_the_declared_five_minute_cadence_throughout() -> None:
    """The acceptance criterion of #155, asserted across the recording rather than on the
    first pair: a file that drifted after row 200 would pass a spot check."""
    observations = _recorded_observations()
    spacings = {
        later.event_time_utc - earlier.event_time_utc
        for earlier, later in itertools.pairwise(observations)
    }

    assert spacings == {BINANCE_OPEN_INTEREST.cadence}


def test_no_published_release_instant_is_invented() -> None:
    """Binance publishes no release calendar for this series, so `available_at_utc` comes
    from the declared lag. A `published_at_utc` filled in here would override that lag with
    a number nobody measured."""
    assert all(observation.published_at_utc is None for observation in _recorded_observations())


def test_the_declared_lag_is_the_sampling_period() -> None:
    """Stated as a test because the parser's spacing check is only load-bearing while it
    is true: the archived series is the five-minute aggregate, so the value stamped at T is
    knowable once T's period closes, and a spacing that is not five minutes makes every
    derived `available_at_utc` wrong in the look-ahead direction."""
    assert BINANCE_OPEN_INTEREST.availability_lag == BINANCE_OPEN_INTEREST.cadence


# ---------------------------------------------------------------------------
# 2. Refusals
# ---------------------------------------------------------------------------


def test_a_spacing_that_is_not_the_declared_cadence_refuses_the_file() -> None:
    """The other half of #155's cadence criterion. Ten minutes between samples means either
    a missing bucket or a changed sampling period; both make the declared lag a fiction, and
    the second makes it look-ahead."""
    payload = HEADER + _row("2024-01-02 00:00:00") + _row("2024-01-02 00:10:00")

    with pytest.raises(DataIntegrityError, match="sampling period"):
        _parse(payload)


def test_a_duplicate_sample_refuses_the_file() -> None:
    """The store keys on `(series, event_time, available_at)`, so a duplicate collapses two
    samples into one silently rather than raising at the insert."""
    payload = HEADER + _row("2024-01-02 00:00:00") + _row("2024-01-02 00:00:00")

    with pytest.raises(DataIntegrityError, match="not after the previous"):
        _parse(payload)


def test_a_second_symbol_in_the_file_refuses_it() -> None:
    """One archive member covers one instrument. A file holding two would be written into a
    single series under whichever id the caller supplied, silently interleaving them."""
    payload = (
        HEADER
        + _row("2024-01-02 00:00:00", symbol="BTCUSDT")
        + _row("2024-01-02 00:05:00", symbol="ETHUSDT")
    )

    with pytest.raises(DataIntegrityError, match="earlier rows carry"):
        _parse(payload)


def test_a_file_without_the_declared_header_is_refused() -> None:
    """Trap 2 in its metrics form. Note that the first field is a datetime string rather
    than an integer, so the numeric header heuristic cannot tell a data row from a header
    here -- the column-name comparison is what catches it, which is why the declared layout
    is compared rather than merely counted."""
    with pytest.raises(DataIntegrityError, match="declared layout"):
        _parse(_row("2024-01-02 00:00:00"))


def test_a_reordered_header_is_refused_rather_than_read_positionally() -> None:
    """Every field would still parse. `sum_open_interest` would hold the quote-denominated
    figure, which is the same series scaled by roughly the price of Bitcoin -- a number that
    looks like open interest and is not."""
    swapped = list(METRICS_COLUMNS)
    swapped[2], swapped[3] = swapped[3], swapped[2]
    payload = (",".join(swapped) + "\n").encode("ascii") + _row("2024-01-02 00:00:00")

    with pytest.raises(DataIntegrityError, match="declared layout"):
        _parse(payload)


def test_a_file_with_no_rows_at_all_parses_to_nothing() -> None:
    """A header and no samples is a real answer for a symbol listed mid-day, not an error."""
    assert _parse(HEADER, source="empty") == ()


@pytest.mark.parametrize(
    ("payload", "expected"),
    [
        (b"2024-01-02 00:00:00,BTCUSDT,76608.798\n", "holds 3 columns"),
        (_row("2024-01-02 00:00:00")[:-1] + b",extra\n", "holds 9 columns"),
        (_row("1704153600000"), "is not '%Y-%m-%d %H:%M:%S'"),
        (_row("2024-01-02T00:00:00"), "is not '%Y-%m-%d %H:%M:%S'"),
        (_row("2024-01-02 00:00:00+00:00"), "is not '%Y-%m-%d %H:%M:%S'"),
        (_row("2024-13-45 32:99:99"), "not a real instant"),
        (_row("2009-12-31 23:59:59"), "plausible window"),
        (_row("2024-01-02 00:00:00", symbol=""), "symbol is empty"),
        (_row("2024-01-02 00:00:00", open_interest=" 76608.798 "), "not plain decimal"),
        (_row("2024-01-02 00:00:00", open_interest="NaN"), "not plain decimal"),
        (_row("2024-01-02 00:00:00", open_interest="7_6"), "not plain decimal"),
        (_row("2024-01-02 00:00:00", open_interest="-1"), "is negative"),
    ],
    ids=[
        "short_row",
        "extra_column",
        "epoch_where_a_datetime_is_declared",
        "iso_t_separator",
        "explicit_offset",
        "impossible_calendar_instant",
        "before_any_archive",
        "empty_symbol",
        "padded_decimal",
        "nan",
        "underscore_separated",
        "negative_open_interest",
    ],
)
def test_every_malformed_row_refuses_the_whole_file(payload: bytes, expected: str) -> None:
    """288 rows a day is no rejection budget.

    The corpus driver tallies a bad row and continues, because one bad print in 3.5 million
    must not stop a backfill. Here a single bad row is one part in 288, and the failures
    that matter are uniform rather than isolated -- a changed timestamp spelling, a renamed
    column, a different sampling period. Three cases are the ones a permissive `Decimal()`
    would accept as a *different number*: `" 76608.798 "` strips, `7_6` is seventy-six, and
    `NaN` constructs happily and then compares False forever.
    """
    with pytest.raises(DataIntegrityError, match=re.escape(expected)):
        _parse(HEADER + payload)


def test_the_parser_refuses_a_declaration_that_is_not_this_datasets() -> None:
    """The caller resolves the declaration, so handing over the wrong one is reachable.

    Without this check the funding declaration would send the metrics parser looking for an
    epoch, which raises -- but with a message about a malformed timestamp rather than about
    the wrong declaration, and those send an investigator to different files.
    """
    funding_format = alt_fixtures.funding_rate_archive().archive_format()

    with pytest.raises(DataIntegrityError, match="reads 'naive_utc_datetime'"):
        parse_open_interest_archive(
            HEADER + _row("2024-01-02 00:00:00"),
            source="mutated",
            now_utc=NOW_UTC,
            archive_format=funding_format,
        )


def test_a_naive_reference_instant_is_refused() -> None:
    """`now_utc` moves the plausibility window by its offset, so a naive one moves it by
    whatever the machine's timezone happens to be."""
    with pytest.raises(DataIntegrityError, match="timezone-aware"):
        parse_open_interest_archive(
            HEADER,
            source="empty",
            now_utc=datetime(2026, 8, 5),  # noqa: DTZ001 -- the subject
            archive_format=METRICS_FORMAT,
        )
