"""Every corrupt fixture is still the declared mutation of a still-pristine recording.

Without this, "derived from a real recording" is a claim in a sidecar. A fixture somebody
edited by hand to make a gate fire would be indistinguishable from one the generator wrote,
and the edit would most likely be to the exact bytes the gate exists to refuse.

The derivation is re-run rather than compared between sidecars, so an edit to *both* the
fixture and its record of itself still fails: the source recording's own integrity test
pins the pristine bytes, and this pins everything downstream of them.
"""

from __future__ import annotations

import pytest

from tests.support import corrupt_fixtures
from tests.support.corrupt_fixtures import CorruptArchive
from tools.corrupt_archive_fixture import CORRUPTIONS, Corruption, build

pytestmark = pytest.mark.unit

ALL_CORRUPT = corrupt_fixtures.corrupt_archives()

# One per blocking gate a single file can trip, both directions of the header gate, and one
# each for the two reporting gates. Stated as a number so that a fixture quietly
# disappearing fails here rather than turning a parametrised suite green by emptying it.
MINIMUM_CORRUPT_FIXTURES = 10


def test_the_corrupt_corpus_is_not_empty() -> None:
    assert len(ALL_CORRUPT) >= MINIMUM_CORRUPT_FIXTURES


@pytest.mark.parametrize("corrupt", ALL_CORRUPT, ids=lambda corrupt: corrupt.name)
def test_the_fixture_exists_and_matches_its_recorded_digest(corrupt: CorruptArchive) -> None:
    assert corrupt.path.is_file()
    assert corrupt.digest_matches_the_file()


@pytest.mark.parametrize("corruption", CORRUPTIONS, ids=lambda corruption: corruption.name)
def test_rederiving_the_mutation_reproduces_the_committed_bytes(corruption: Corruption) -> None:
    """The generator is deterministic and the committed file is what it produces.

    This is the test that makes a hand-edit visible. It also pins the generator's
    determinism -- a zip built with the wall clock would fail here one second after being
    written, which is why the member timestamp is fixed.
    """
    corrupted, sidecar = build(corruption)
    committed = corrupt_fixtures.find(corruption.name)
    assert committed.read() == corrupted
    assert committed.corrupt_archive_sha256 == sidecar["corrupt_archive_sha256"]
    assert committed.pristine_archive_sha256 == sidecar["pristine_archive_sha256"]


@pytest.mark.parametrize("corrupt", ALL_CORRUPT, ids=lambda corrupt: corrupt.name)
def test_every_fixture_names_the_gate_it_exists_to_trip(corrupt: CorruptArchive) -> None:
    """A corpus whose files do not say what they are for is a corpus nobody prunes."""
    assert corrupt.gate.strip()
    assert corrupt.rationale.strip()
    assert corrupt.source_recording.strip()
