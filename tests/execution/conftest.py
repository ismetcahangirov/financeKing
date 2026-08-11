"""The recorded-response corpus, and the fake venue that replays it.

Every adapter test in this package replays bytes a real testnet returned. Nothing here
constructs a response by hand: a hand-written fixture encodes what its author believes
Binance returns, and the two things nobody hand-writes wrongly are the exact string
encoding of a decimal field and the deliberate non-ASCII symbols in `exchangeInfo`
(`docs/rules/testing-rules.md`).

`RecordedExchange` implements `GuardedExchange` -- the same Protocol the real
`GuardedCcxtExchange` implements -- so a test drives `BinanceVenue` through the identical
code path the demo runtime uses, differing only in where the response text came from.
"""

from __future__ import annotations

import hashlib
from collections.abc import Iterator, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Final

import pytest
import yaml

from fking.domain import Venue
from fking.platform.safety import VenueResponseMetadata

RECORDED_ROOT: Final[Path] = Path(__file__).resolve().parents[1] / "fixtures" / "recorded"

# ccxt implicit-API method name -> the fixture directory holding its recorded response.
# Written out rather than derived, so that adding an endpoint to the adapter without
# recording a response for it is a missing key rather than a silently untested method.
ENDPOINT_RECORDINGS: Final[Mapping[Venue, Mapping[str, str]]] = {
    Venue.BINANCE_SPOT_TESTNET: {
        "publicGetExchangeInfo": "exchangeInfo",
        "privateGetAccount": "account_rejected",
        "privateGetOpenOrders": "openOrders_rejected",
        "privateGetMyTrades": "myTrades_rejected",
        "privatePostOrder": "order_rejected",
        "privateDeleteOrder": "orderCancel_rejected",
        "privatePostOrderCancelReplace": "orderCancelReplace_rejected",
    },
    Venue.BINANCE_FUTURES_TESTNET: {
        "fapiPublicGetExchangeInfo": "exchangeInfo",
        "fapiPrivateV3GetBalance": "balance_rejected",
        "fapiPrivateV3GetPositionRisk": "positionRisk_rejected",
        "fapiPrivateGetOpenOrders": "openOrders_rejected",
        "fapiPrivateGetUserTrades": "userTrades_rejected",
        "fapiPrivatePostOrder": "order_rejected",
        "fapiPrivateDeleteOrder": "orderCancel_rejected",
        "fapiPrivatePutOrder": "orderAmend_rejected",
    },
}


@dataclass(frozen=True, slots=True)
class Recording:
    """One captured venue response, as it sits on disk."""

    path: Path
    venue: str
    endpoint: str
    http_status: int
    body: str
    body_sha256: str
    derivation: Mapping[str, object] | None
    # Absent from captures taken before the recorder learned to keep them. Empty rather
    # than `None`, because "this recording carries no headers" and "this response had no
    # headers" are the same thing to every consumer here, and a `None` would only add a
    # branch at each of them.
    headers: Mapping[str, str]

    @property
    def recomputed_sha256(self) -> str:
        return hashlib.sha256(self.body.encode("utf-8")).hexdigest()


def _load(path: Path) -> Recording:
    document = yaml.safe_load(path.read_text(encoding="utf-8"))
    header = document["_recording"]
    recorded_headers = header.get("headers") or {}
    return Recording(
        path=path,
        venue=header["venue"],
        endpoint=header["endpoint"],
        http_status=header["http_status"],
        body=document["body"],
        body_sha256=header["body_sha256"],
        derivation=header["derivation"],
        headers={str(name): str(value) for name, value in recorded_headers.items()},
    )


def iter_recordings() -> Iterator[Recording]:
    """Every recording in the corpus, in a stable order."""
    for path in sorted(RECORDED_ROOT.rglob("*.yaml")):
        yield _load(path)


def recorded_venues() -> tuple[Venue, ...]:
    """The venues the corpus currently covers.

    Derived from the tree rather than asserted, because the corpus grows one recording
    session at a time and a test parametrized over a hardcoded list would fail for the
    wrong reason. `test_fixture_integrity` is where the corpus's *completeness* is
    asserted, in one place, so a gap is reported once and by name.
    """
    return tuple(
        venue
        for venue in ENDPOINT_RECORDINGS
        if (RECORDED_ROOT / str(venue)).is_dir()
        and all(
            (RECORDED_ROOT / str(venue) / directory).is_dir()
            for directory in ENDPOINT_RECORDINGS[venue].values()
        )
    )


def load_recording(venue: Venue, directory: str) -> Recording:
    """The newest recording for one endpoint.

    Newest rather than first: filenames are capture timestamps, so a re-record is meant
    to supersede its predecessor while the older capture stays in the tree as evidence
    of what the venue used to send.
    """
    candidates = sorted((RECORDED_ROOT / str(venue) / directory).glob("*.yaml"))
    if not candidates:
        raise FileNotFoundError(f"no recording under {RECORDED_ROOT / str(venue) / directory}")
    return _load(candidates[-1])


class RecordedExchange:
    """`GuardedExchange` replaying the corpus. Never touches a socket."""

    def __init__(self, venue: Venue) -> None:
        self._venue = venue
        self._recordings = {
            endpoint: load_recording(venue, directory)
            for endpoint, directory in ENDPOINT_RECORDINGS[venue].items()
        }
        self._request_count = 0
        self._last_response_metadata: VenueResponseMetadata | None = None
        self.calls: list[tuple[str, Mapping[str, str]]] = []
        self.closed = False
        # Set to make `call` raise *after* it has recorded the response, which is how a
        # transport failure that still saw a 429 behaves. Without it there is no way to
        # prove the governor observes responses it never got to return.
        self.raise_after_recording: BaseException | None = None

    def override(self, endpoint: str, directory: str) -> None:
        """Answer `endpoint` from a different recording in the same venue's corpus.

        The rate-limit recordings are not endpoint-specific -- a 429 can answer any
        request -- so binding one to an endpoint is the test's choice rather than the
        corpus's, and it is made explicitly here rather than by a second fake.
        """
        self._recordings[endpoint] = load_recording(self._venue, directory)

    @property
    def venue_id(self) -> str:
        return str(self._venue)

    @property
    def request_count(self) -> int:
        return self._request_count

    @property
    def last_response_metadata(self) -> VenueResponseMetadata | None:
        return self._last_response_metadata

    async def call(self, endpoint: str, params: Mapping[str, str]) -> str:
        self._request_count += 1
        self.calls.append((endpoint, dict(params)))
        recording = self._recordings[endpoint]
        self._last_response_metadata = VenueResponseMetadata.of(
            http_status=recording.http_status, headers=recording.headers
        )
        if self.raise_after_recording is not None:
            raise self.raise_after_recording
        return recording.body

    async def aclose(self) -> None:
        self.closed = True


@pytest.fixture(params=recorded_venues(), ids=str)
def recorded_exchange(request: pytest.FixtureRequest) -> RecordedExchange:
    """One `RecordedExchange` per venue the corpus covers."""
    venue: Venue = request.param
    return RecordedExchange(venue)
