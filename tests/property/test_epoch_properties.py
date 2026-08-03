"""Properties of epoch normalisation, over the whole plausible window.

The example-based tests in `tests/data/test_format_resolver.py` check the cutover
instant and the two absurd directions. They cannot check the claim that actually
matters, which is universal: **anywhere in the corpus, reading an epoch in the wrong
unit fails.** If that ever stops holding for some region of the window, a mixed-unit
series becomes constructible, and a mixed-unit series is the one failure mode in
VF-015 that produces correct-looking endpoints and corrupted spacing in the middle.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest
from hypothesis import given
from hypothesis import strategies as st

from fking.data import EpochUnit, epoch_to_utc
from fking.platform.errors import DataIntegrityError

pytestmark = [pytest.mark.property, pytest.mark.unit]

NOW_UTC = datetime(2026, 8, 3, tzinfo=UTC)
WINDOW_START = datetime(2010, 1, 1, tzinfo=UTC)

DIVISOR = {EpochUnit.MILLISECONDS: 1_000, EpochUnit.MICROSECONDS: 1_000_000}

# The whole declared window, minus a day at each end so that a rounding step cannot
# push a generated instant across a boundary the property is not about.
plausible_instants = st.datetimes(
    min_value=(WINDOW_START + timedelta(days=1)).replace(tzinfo=None),
    max_value=(NOW_UTC - timedelta(days=1)).replace(tzinfo=None),
    timezones=st.just(UTC),
)
units = st.sampled_from([EpochUnit.MILLISECONDS, EpochUnit.MICROSECONDS])


def to_raw_epoch(moment: datetime, unit: EpochUnit) -> int:
    """The inverse of `epoch_to_utc`, written here rather than imported.

    A round-trip property that used the production code for both directions would pass
    for any pair of mutually consistent bugs. This side is derived from `timestamp()`
    independently.
    """
    return int(moment.timestamp() * DIVISOR[unit])


@given(moment=plausible_instants, unit=units)
def test_round_trip_preserves_the_instant_at_the_units_resolution(
    moment: datetime, unit: EpochUnit
) -> None:
    resolution = timedelta(seconds=1) / DIVISOR[unit]
    recovered = epoch_to_utc(to_raw_epoch(moment, unit), unit=unit, now_utc=NOW_UTC)
    assert abs(recovered - moment) < resolution


@given(moment=plausible_instants)
def test_microseconds_read_as_milliseconds_never_return(moment: datetime) -> None:
    """A thousandfold overshoot lands near the year 56,000, outside the window.

    Stated over the whole window rather than at one instant, because the guard is a
    magnitude check and a magnitude check has edges.
    """
    with pytest.raises(DataIntegrityError):
        epoch_to_utc(
            to_raw_epoch(moment, EpochUnit.MICROSECONDS),
            unit=EpochUnit.MILLISECONDS,
            now_utc=NOW_UTC,
        )


@given(moment=plausible_instants)
def test_milliseconds_read_as_microseconds_never_return(moment: datetime) -> None:
    """A thousandfold undershoot lands in 1970, below the window."""
    with pytest.raises(DataIntegrityError):
        epoch_to_utc(
            to_raw_epoch(moment, EpochUnit.MILLISECONDS),
            unit=EpochUnit.MICROSECONDS,
            now_utc=NOW_UTC,
        )


@given(moment=plausible_instants, unit=units)
def test_normalisation_is_idempotent_in_the_only_sense_available(
    moment: datetime, unit: EpochUnit
) -> None:
    """Converting the same raw integer twice yields the same instant.

    Trivially true of a pure function, and worth asserting anyway: the day somebody
    reaches for a cache, a clock or a mutable default here, this is the test that
    notices.
    """
    raw_epoch = to_raw_epoch(moment, unit)
    first = epoch_to_utc(raw_epoch, unit=unit, now_utc=NOW_UTC)
    second = epoch_to_utc(raw_epoch, unit=unit, now_utc=NOW_UTC)
    assert first == second
    assert first.tzinfo is UTC
