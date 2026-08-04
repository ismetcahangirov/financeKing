"""Every recorded stream fixture is a genuine capture, and still the bytes captured.

The sibling of `test_archive_fixture_integrity.py`, for the same reason. A fixture
someone edited by hand to make a test pass would be indistinguishable from one the
recorder wrote, and the edit would most plausibly be to the exact field the test exists
to protect -- an `"x":false` flipped to `true`, an `"a"` renumbered to make a sequence
contiguous.

The last three tests are the load-bearing ones. If the corpus ever stops containing a
closed kline beside the open frames for the same minute, or stops covering all four
subscribed stream types, then the assertions in `test_live_ingestion.py` have no data
behind them and become decoration.
"""

from __future__ import annotations

import hashlib
import json

import pytest

from fking.data.live.streams import LIVE_STREAM_PROFILES, stream_names_for
from tests.support.stream_fixtures import RecordedStream, recorded_streams

pytestmark = pytest.mark.unit

ALL_RECORDINGS = recorded_streams()

# One per venue profile. Named as a requirement rather than a number somebody lowers
# when a recording becomes inconvenient: the spot and futures feeds differ in which
# streams they serve and in how they whitespace their JSON, and a parser proven against
# only one of them is a parser proven against half the surface.
MINIMUM_RECORDINGS = 2


def test_the_corpus_is_not_empty() -> None:
    """A vacuously passing parametrised suite is the failure mode this closes."""
    assert len(ALL_RECORDINGS) >= MINIMUM_RECORDINGS


def test_every_venue_profile_has_a_recording() -> None:
    recorded_venues = {recording.venue_id for recording in ALL_RECORDINGS}
    assert recorded_venues == set(LIVE_STREAM_PROFILES)


@pytest.mark.parametrize("recording", ALL_RECORDINGS, ids=lambda recording: recording.label)
def test_the_recorded_digest_still_describes_the_bytes(recording: RecordedStream) -> None:
    assert hashlib.sha256(recording.read_bytes()).hexdigest() == recording.frames_sha256


@pytest.mark.parametrize("recording", ALL_RECORDINGS, ids=lambda recording: recording.label)
def test_the_frame_count_matches_the_file(recording: RecordedStream) -> None:
    assert len(recording.frames()) == recording.frame_count


@pytest.mark.parametrize("recording", ALL_RECORDINGS, ids=lambda recording: recording.label)
def test_the_recording_subscribed_to_exactly_the_profile_streams(
    recording: RecordedStream,
) -> None:
    """A recording of a stream set the running system does not subscribe to proves nothing."""
    profile = LIVE_STREAM_PROFILES[recording.venue_id]
    assert set(recording.streams) == set(stream_names_for(profile, recording.symbol))


@pytest.mark.parametrize("recording", ALL_RECORDINGS, ids=lambda recording: recording.label)
def test_the_recording_holds_both_an_open_and_a_closed_kline_for_one_minute(
    recording: RecordedStream,
) -> None:
    """The corpus must be able to prove the open/closed distinction, not just assert it."""
    open_minutes: set[int] = set()
    closed_minutes: set[int] = set()
    for frame in recording.frames():
        payload = json.loads(frame).get("data", {})
        kline = payload.get("k")
        if not isinstance(kline, dict):
            continue
        (closed_minutes if kline["x"] else open_minutes).add(int(kline["t"]))
    assert closed_minutes, f"{recording.label} holds no closed kline"
    assert open_minutes & closed_minutes, (
        f"{recording.label} holds no minute with both an open and a closed frame, so it "
        f"cannot show that the open ones were skipped and the closed one kept"
    )


@pytest.mark.parametrize("recording", ALL_RECORDINGS, ids=lambda recording: recording.label)
def test_the_recording_covers_every_event_type_it_subscribed_to(
    recording: RecordedStream,
) -> None:
    """bookTicker and markPriceUpdate only exist on futures; both must be represented there."""
    seen = {json.loads(frame)["data"]["e"] for frame in recording.frames()}
    profile = LIVE_STREAM_PROFILES[recording.venue_id]
    expected = {
        "kline_1m": "kline",
        "aggTrade": "aggTrade",
        "bookTicker": "bookTicker",
        "markPrice@1s": "markPriceUpdate",
    }
    assert seen == {expected[suffix] for suffix in profile.stream_suffixes}


@pytest.mark.parametrize("recording", ALL_RECORDINGS, ids=lambda recording: recording.label)
def test_every_aggregate_trade_id_run_is_contiguous(recording: RecordedStream) -> None:
    """The recordings must be clean, or a detected gap could be the fixture's fault.

    A real drop inside a recording would make `test_live_ingestion.py`'s "a healthy
    replay reports no sequence gap" assertion fail for a reason that has nothing to do
    with the code under test, and the natural response -- relaxing the assertion -- would
    silently disable the detector's only end-to-end check.
    """
    ids = [
        int(json.loads(frame)["data"]["a"])
        for frame in recording.frames()
        if json.loads(frame)["data"]["e"] == "aggTrade"
    ]
    assert ids, f"{recording.label} holds no aggTrade frames"
    assert ids == list(range(ids[0], ids[0] + len(ids)))
