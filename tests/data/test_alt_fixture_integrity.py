"""Every alternative-series fixture is a genuine recording, and still the bytes recorded.

The corpus fixtures have this already (`test_archive_fixture_integrity.py`); the
alternative ones need their own because they live under a separate root, for the reason
`tests/support/alt_fixtures.py` gives. Without it, "recorded from `data.binance.vision`"
is a claim in a docstring, and a fixture edited by hand to make a parser test pass would be
indistinguishable from one the recorder wrote.

The edit would most likely be to the exact field a test exists to protect. The funding
recording carries three of those: `1577923200002`, a settlement two milliseconds off the
eight-hour boundary; `8.4E-7`, a rate in scientific notation; and negative rates in the
opening rows. Each is the kind of value somebody "tidies" while chasing a failure, and each
is asserted here so that tidying it fails loudly rather than silently weakening the parser
test that depends on it.
"""

from __future__ import annotations

import hashlib
import zipfile
from io import BytesIO

import pytest

from fking.data.format_resolver import Dataset
from tests.support import alt_fixtures
from tests.support.alt_fixtures import RecordedAltArchive

pytestmark = pytest.mark.unit

ALL_FIXTURES = alt_fixtures.recorded_alt_archives()
SHA256_HEX_LENGTH = 64

# 31 days, three settlements a day, plus the header row.
JANUARY_2020_LINES = 94


def test_the_alt_corpus_is_not_empty() -> None:
    """A vacuously passing parametrised suite is the failure mode this closes."""
    assert ALL_FIXTURES


@pytest.mark.parametrize("recorded", ALL_FIXTURES, ids=lambda recorded: recorded.label)
def test_the_recorded_digest_still_describes_the_bytes(recorded: RecordedAltArchive) -> None:
    payload = recorded.read()
    expected = recorded.archive_sha256 if recorded.is_whole_archive else recorded.fragment_sha256

    assert hashlib.sha256(payload).hexdigest() == expected


@pytest.mark.parametrize("recorded", ALL_FIXTURES, ids=lambda recorded: recorded.label)
def test_the_provenance_names_the_archive_it_came_from(recorded: RecordedAltArchive) -> None:
    """A fixture with no resolvable upstream is a fixture nobody can re-record."""
    assert recorded.source_url.startswith("https://data.binance.vision/data/")
    assert recorded.symbol in recorded.source_url
    assert len(recorded.archive_sha256) == SHA256_HEX_LENGTH
    assert len(recorded.member_sha256) == SHA256_HEX_LENGTH


def test_the_funding_recording_is_a_whole_verified_month() -> None:
    """A fragment cannot prove that ninety-three settlements arrived, and a short month is
    exactly what a truncated download produces."""
    recorded = alt_fixtures.funding_rate_archive()

    with zipfile.ZipFile(BytesIO(recorded.read())) as bundle:
        members = bundle.namelist()
        assert len(members) == 1
        member = bundle.read(members[0])

    assert hashlib.sha256(member).hexdigest() == recorded.member_sha256
    assert recorded.member_line_count == JANUARY_2020_LINES
    assert recorded.dataset is Dataset.FUNDING_RATE


def test_the_funding_recording_still_carries_the_three_awkward_shapes() -> None:
    """If any of these is ever "cleaned up", the parser assertions that depend on them
    become decoration."""
    recorded = alt_fixtures.funding_rate_archive()
    with zipfile.ZipFile(BytesIO(recorded.read())) as bundle:
        member = bundle.read(bundle.namelist()[0])

    assert b"1577923200002" in member, "the off-boundary settlement was removed"
    assert b"8.4E-7" in member, "the scientific-notation rate was rewritten"
    assert b",-0.000" in member, "the negative rates were removed"
    assert member.startswith(b"calc_time,funding_interval_hours,last_funding_rate")
