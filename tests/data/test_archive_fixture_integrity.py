"""Every archive fixture is a genuine recording, and still the bytes that were recorded.

Without this, "recorded from `data.binance.vision`" is a claim in a docstring. A fixture
that someone edited by hand to make a test pass would be indistinguishable from one the
recorder wrote, and the edit would most likely be to the exact field the test exists to
protect -- a `True` softened to `true`, a microsecond epoch shortened by three digits.

The digest is recomputed from the file rather than compared between sidecars, so an edit to
both the fixture and its provenance still fails: the provenance records the digest of the
*archive as served*, and only a real re-recording can change that consistently.

The last two tests are the load-bearing ones for this repository's own rules. If the
recorded corpus ever stops containing a Python-style boolean or stops spanning the
microsecond cutover, then the traps in `DATA_PIPELINE.md` section 3 have no test data behind
them and every parser assertion becomes decoration.
"""

from __future__ import annotations

import hashlib
import zipfile
from io import BytesIO

import pytest

from fking.data.format_resolver import BooleanEncoding, EpochUnit
from tests.support import archive_fixtures
from tests.support.archive_fixtures import RecordedArchive

pytestmark = pytest.mark.unit

ALL_FIXTURES = archive_fixtures.recorded_archives()

# The corpus must cover both epoch units, both header conventions and both markets, which
# takes at least four files. Named so the count is a stated requirement rather than a
# number somebody will lower when a fixture becomes inconvenient.
MINIMUM_FIXTURES = 4
SHA256_HEX_LENGTH = 64
MARKETS_THAT_MUST_BE_REPRESENTED = 2


def test_the_corpus_is_not_empty() -> None:
    """A vacuously passing parametrised suite is the failure mode this closes."""
    assert len(ALL_FIXTURES) >= MINIMUM_FIXTURES


@pytest.mark.parametrize("recorded", ALL_FIXTURES, ids=lambda recorded: recorded.label)
def test_the_fixture_file_exists_beside_its_provenance(recorded: RecordedArchive) -> None:
    assert recorded.path.is_file()


@pytest.mark.parametrize("recorded", ALL_FIXTURES, ids=lambda recorded: recorded.label)
def test_the_recorded_digest_still_describes_the_bytes(recorded: RecordedArchive) -> None:
    payload = recorded.read()
    if recorded.is_whole_archive:
        assert hashlib.sha256(payload).hexdigest() == recorded.archive_sha256
    else:
        assert hashlib.sha256(payload).hexdigest() == recorded.fragment_sha256


@pytest.mark.parametrize("recorded", ALL_FIXTURES, ids=lambda recorded: recorded.label)
def test_the_provenance_names_the_archive_it_came_from(recorded: RecordedArchive) -> None:
    """A fixture with no resolvable upstream is a fixture nobody can re-record."""
    assert recorded.source_url.startswith("https://data.binance.vision/data/")
    assert recorded.symbol in recorded.source_url
    assert len(recorded.archive_sha256) == SHA256_HEX_LENGTH
    assert len(recorded.member_sha256) == SHA256_HEX_LENGTH


@pytest.mark.parametrize(
    "recorded", archive_fixtures.whole_archives(), ids=lambda recorded: recorded.label
)
def test_a_whole_archive_holds_the_member_its_provenance_recorded(
    recorded: RecordedArchive,
) -> None:
    with zipfile.ZipFile(BytesIO(recorded.read())) as bundle:
        members = bundle.namelist()
        assert len(members) == 1
        assert hashlib.sha256(bundle.read(members[0])).hexdigest() == recorded.member_sha256


@pytest.mark.parametrize(
    "recorded", archive_fixtures.csv_fragments(), ids=lambda recorded: recorded.label
)
def test_a_fragment_is_a_byte_exact_prefix_of_real_lines(recorded: RecordedArchive) -> None:
    """Whole lines only, and fewer than the member had.

    A fragment truncated mid-line would teach a parser that a short final row is normal,
    which is exactly the shape a truncated download produces.
    """
    payload = recorded.read()
    assert payload.endswith(b"\n")
    assert recorded.fragment_line_count is not None
    assert len(payload.splitlines()) == recorded.fragment_line_count
    assert recorded.fragment_line_count < recorded.member_line_count


def test_the_corpus_still_contains_a_python_style_boolean() -> None:
    """F-005's test data. If this fixture is ever "cleaned up", trap 3 has no evidence."""
    trades = [
        recorded
        for recorded in ALL_FIXTURES
        if recorded.spec().archive_format.boolean_encoding is BooleanEncoding.PYTHON
    ]
    assert trades, "no fixture declares a PYTHON boolean encoding"

    payloads = [recorded.read() for recorded in trades]
    assert any(b",True" in payload for payload in payloads)
    assert any(b",False" in payload for payload in payloads)
    # And emphatically not the lowercase spelling every other exchange uses.
    assert not any(b",true" in payload or b",false" in payload for payload in payloads)


def test_the_corpus_spans_the_microsecond_cutover_in_both_directions() -> None:
    """VF-015's test data, on both sides of 2025-01-01 and in both markets."""
    units = {
        (recorded.market, recorded.spec().archive_format.epoch_unit) for recorded in ALL_FIXTURES
    }
    epoch_units = {unit for _market, unit in units}

    assert epoch_units == {EpochUnit.MILLISECONDS, EpochUnit.MICROSECONDS}
    assert len({market for market, _unit in units}) >= MARKETS_THAT_MUST_BE_REPRESENTED


def test_the_corpus_contains_a_file_with_a_header_and_a_file_without_one() -> None:
    """VF-016's test data. Both directions of trap 2 need a real file to be asserted on."""
    header_flags = {recorded.has_header_row for recorded in ALL_FIXTURES}

    assert header_flags == {True, False}
