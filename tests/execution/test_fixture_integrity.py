"""The recorded corpus is what the venue sent, and can be shown to be.

A fixture whose digest still matches its body is a fixture nobody edited to make a test
pass. That is the whole guarantee, and it is worth stating why it is needed here rather
than in general: an adapter test that fails because Binance changed a field is a *useful*
failure, and the cheapest way to make it stop failing is to edit the fixture. The digest
turns that edit into a second failure that names the file.

The completeness assertion is separate and deliberate. `recorded_venues()` skips a venue
whose corpus is incomplete, so without this the whole adapter suite could quietly shrink
to nothing and still report green.
"""

from __future__ import annotations

import json
from typing import Final

import pytest

from fking.domain import Venue
from fking.execution import classify_symbol, parse_venue_payload
from tests.execution.conftest import (
    ENDPOINT_RECORDINGS,
    RECORDED_ROOT,
    Recording,
    iter_recordings,
    load_recording,
)

pytestmark = pytest.mark.unit

ALL_RECORDINGS: Final[tuple[Recording, ...]] = tuple(iter_recordings())

# The recorder is credential-free, so every venue that has been recorded at all must have
# been recorded completely. A partial corpus is how the adapter suite silently stops
# covering a market.
REQUIRED_VENUES: Final[tuple[Venue, ...]] = (
    Venue.BINANCE_SPOT_TESTNET,
    Venue.BINANCE_FUTURES_TESTNET,
)

# The status at which a response stops describing a result and starts describing a
# refusal, and the hex length of a SHA-256 digest.
FIRST_ERROR_STATUS: Final[int] = 400
SHA256_HEX_LENGTH: Final[int] = 64


def _identifier(recording: Recording) -> str:
    return f"{recording.venue}-{recording.path.parent.name}"


def test_the_corpus_is_not_empty() -> None:
    """A glob that matches nothing passes every parametrized assertion below."""
    assert ALL_RECORDINGS


@pytest.mark.parametrize("recording", ALL_RECORDINGS, ids=_identifier)
def test_a_recorded_body_still_matches_its_digest(recording: Recording) -> None:
    """The one sanctioned way a digest may fail is the synthetic-fixture exception.

    A response the venue will not produce on demand -- a `-1021` recvWindow rejection, a
    partial fill that stalls -- may be synthesized by editing a real capture, provided it
    carries a `# SYNTHETIC:` comment naming the reason and a `derived_from_sha256` that
    resolves. "The recording was stale so I updated the numbers" is not that exception;
    it is a hand-written fixture, and the answer is to re-record.
    """
    if recording.recomputed_sha256 == recording.body_sha256:
        return

    source = recording.path.read_text(encoding="utf-8")
    assert "# SYNTHETIC:" in source, (
        f"{recording.path.name} does not match its digest and claims no derivation; "
        f"re-record it rather than editing it"
    )
    assert recording.derivation is not None
    assert "derived_from_sha256" in recording.derivation


@pytest.mark.parametrize("recording", ALL_RECORDINGS, ids=_identifier)
def test_a_recorded_body_is_still_the_venue_json_it_was_captured_as(
    recording: Recording,
) -> None:
    """YAML must not have altered the bytes on the way to disk.

    Binance escapes non-ASCII as `\\uXXXX`, which is a backslash sequence in a file
    format with its own escaping rules -- so this is not a formality. If the round trip
    ever mangles it, `Decimal(body)` becomes a claim about PyYAML.
    """
    assert json.loads(recording.body) is not None


@pytest.mark.parametrize("recording", ALL_RECORDINGS, ids=_identifier)
def test_a_recorded_rejection_is_labelled_as_one_by_its_status_and_its_code(
    recording: Recording,
) -> None:
    """The header and the body have to agree, or the corpus is describing itself wrongly.

    A rejection recorded as HTTP 200 would be replayed by the adapter as a success and
    the error path would go untested while looking covered.
    """
    payload = parse_venue_payload(recording.body)
    is_rejection = recording.path.parent.name.endswith("_rejected")
    has_error_code = isinstance(payload, dict) and int(payload.get("code", 0)) < 0

    assert is_rejection == has_error_code
    assert is_rejection == (recording.http_status >= FIRST_ERROR_STATUS)


@pytest.mark.parametrize(
    "recording",
    [entry for entry in ALL_RECORDINGS if entry.derivation is not None],
    ids=_identifier,
)
def test_a_derived_recording_states_what_it_was_derived_from(recording: Recording) -> None:
    """`exchangeInfo` is stored as a declared symbol subset -- the full spot response is
    2.5 MB and 1,373 symbols, which is not a reviewable diff. The envelope is verbatim
    and the derivation records the source digest and both counts, so the narrowing is
    checkable rather than asserted."""
    derivation = recording.derivation
    assert derivation is not None
    assert derivation["kind"] == "symbol_subset"
    assert len(str(derivation["source_body_sha256"])) == SHA256_HEX_LENGTH
    assert int(derivation["kept_symbol_count"]) < int(derivation["source_symbol_count"])

    payload = parse_venue_payload(recording.body)
    assert isinstance(payload, dict)
    assert len(payload["symbols"]) == int(derivation["kept_symbol_count"])


@pytest.mark.parametrize("venue", REQUIRED_VENUES, ids=str)
def test_every_endpoint_the_adapter_calls_has_a_recording(venue: Venue) -> None:
    """A missing directory silently shrinks the adapter suite instead of failing it."""
    missing = [
        directory
        for directory in ENDPOINT_RECORDINGS[venue].values()
        if not sorted((RECORDED_ROOT / str(venue) / directory).glob("*.yaml"))
    ]
    assert missing == [], (
        f"{venue} has no recording for {missing}; run "
        f"`uv run python tools/record_exchange.py --endpoint all --venue {venue}`"
    )


@pytest.mark.parametrize("venue", REQUIRED_VENUES, ids=str)
def test_the_recorded_universe_retains_the_deliberate_non_ascii_symbols(venue: Venue) -> None:
    """Testnet serves them on purpose, and the subset rule keeps every one.

    Without this a re-record could narrow the corpus to liquid pairs and the Unicode
    handling would go untested -- which is exactly the state in which a
    `UnicodeEncodeError` crashes a startup on a Windows console codepage.
    """
    payload = parse_venue_payload(load_recording(venue, "exchangeInfo").body)
    assert isinstance(payload, dict)
    non_ascii = [
        entry["symbol"] for entry in payload["symbols"] if not str(entry["symbol"]).isascii()
    ]
    assert non_ascii, "the exchangeInfo recording no longer carries a non-ascii symbol"
    assert all(classify_symbol(symbol).is_tradable is False for symbol in non_ascii)
