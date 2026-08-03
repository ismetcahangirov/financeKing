"""The archive egress client and `GuardedArchiveEgress`.

The load-bearing tests here are the cross-rejection pair: the archive client must
refuse every host the trading client permits, and the trading client must refuse the
archive host. Either direction passing alone would leave one path able to reach the
other's endpoints, which is the failure the two-allowlist design exists to make
impossible rather than merely unlikely.
"""

from __future__ import annotations

import hashlib
from pathlib import Path

import httpx
import pytest

from fking.platform.safety import PERMITTED_HOSTS, SafetyViolation, guarded_client
from fking.platform.safety.archive import (
    ARCHIVE_HOSTS,
    ArchiveEgress,
    ArchiveUnavailableError,
    GuardedArchiveEgress,
    assert_archive_host_permitted,
    guarded_archive_client,
)

pytestmark = pytest.mark.unit

ARCHIVE_BASE = "https://data.binance.vision"
SAMPLE_URL = f"{ARCHIVE_BASE}/data/spot/daily/klines/BTCUSDT/1m/BTCUSDT-1m-2025-01-02.zip"
TIMEOUT_SECONDS = 7.5
EXPECTED_REQUESTS = 2

# Production trading endpoints. None of these is in either allowlist, and the archive
# client must refuse them exactly as the trading client does.
PRODUCTION_TRADING_HOSTS = [
    "api.binance.com",
    "api1.binance.com",
    "fapi.binance.com",
    "dapi.binance.com",
    "stream.binance.com",
    "fstream.binance.com",
    "api.bybit.com",
]


class TestHostValidation:
    @pytest.mark.parametrize("host", sorted(PERMITTED_HOSTS))
    def test_every_trading_host_is_refused_by_the_archive_path(self, host: str) -> None:
        """The archive fetcher must not be able to reach an order endpoint.

        Parametrized over the live allowlist rather than a copy of it, so a host added
        to `PERMITTED_HOSTS` is covered here on the commit that adds it.
        """
        with pytest.raises(SafetyViolation, match="ARCHIVE_HOSTS"):
            assert_archive_host_permitted(f"https://{host}/api/v3/order")

    @pytest.mark.parametrize("host", PRODUCTION_TRADING_HOSTS)
    def test_every_production_trading_host_is_refused(self, host: str) -> None:
        with pytest.raises(SafetyViolation):
            assert_archive_host_permitted(f"https://{host}/api/v3/time")

    @pytest.mark.parametrize("host", sorted(ARCHIVE_HOSTS))
    def test_the_trading_client_refuses_every_archive_host(self, host: str) -> None:
        """The other direction. Adding the archive host to PERMITTED_HOSTS fails here."""
        with pytest.raises(SafetyViolation, match="PERMITTED_HOSTS"):
            guarded_client(base_url=f"https://{host}")

    def test_a_permitted_archive_url_is_accepted(self) -> None:
        assert assert_archive_host_permitted(SAMPLE_URL) == "data.binance.vision"

    @pytest.mark.parametrize(
        "url",
        [
            "https://data.binance.vision.attacker.example/data/spot/x.zip",
            "https://api.binance.com/?note=data.binance.vision",
            "https://data.binance.vision@api.binance.com/data/spot/x.zip",
            "http://data.binance.vision/data/spot/x.zip",
            "https:///data/spot/x.zip",
        ],
    )
    def test_lookalike_and_downgrade_urls_are_refused(self, url: str) -> None:
        with pytest.raises(SafetyViolation):
            assert_archive_host_permitted(url)

    def test_a_trailing_dot_is_the_same_name(self) -> None:
        """`example.com.` and `example.com` are one host; a mismatch here would let the
        dotted form be evaluated as an unrecognised -- and therefore separately
        considered -- name."""
        assert assert_archive_host_permitted("https://data.binance.vision./data/x.zip") == (
            "data.binance.vision"
        )

    def test_the_rejection_names_which_allowlist_refused(self) -> None:
        """With two allowlists, "not in the permitted host set" is ambiguous exactly
        when somebody is deciding which list to add a host to."""
        with pytest.raises(SafetyViolation, match=r"ARCHIVE_HOSTS"):
            assert_archive_host_permitted("https://api.binance.com/x")


class TestTransportConfiguration:
    def test_a_production_base_url_is_refused_at_construction(self) -> None:
        with pytest.raises(SafetyViolation):
            guarded_archive_client(base_url="https://api.binance.com")

    def test_a_permitted_base_url_is_accepted(self) -> None:
        client = guarded_archive_client(base_url=ARCHIVE_BASE)
        assert str(client.base_url).startswith(ARCHIVE_BASE)

    def test_an_empty_base_url_is_allowed_because_every_request_is_checked(self) -> None:
        assert guarded_archive_client() is not None

    def test_proxy_environment_cannot_reroute_a_validated_host(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A substituted archive that matches a substituted checksum verifies perfectly."""
        monkeypatch.setenv("HTTPS_PROXY", "http://127.0.0.1:9")
        monkeypatch.setenv("ALL_PROXY", "http://127.0.0.1:9")
        assert guarded_archive_client(base_url=ARCHIVE_BASE).trust_env is False

    def test_redirects_are_not_followed(self) -> None:
        assert guarded_archive_client(base_url=ARCHIVE_BASE).follow_redirects is False

    def test_the_timeout_is_applied(self) -> None:
        client = guarded_archive_client(timeout_seconds=TIMEOUT_SECONDS)
        assert client.timeout.connect == TIMEOUT_SECONDS

    def test_no_credential_is_attached(self) -> None:
        """The second barrier behind the allowlist: it cannot sign, so it cannot order."""
        client = guarded_archive_client(base_url=ARCHIVE_BASE)
        assert client.auth is None
        header_names = {name.lower() for name in client.headers}
        assert not header_names & {"authorization", "x-mbx-apikey", "cookie"}


@pytest.mark.asyncio
class TestPerRequestValidation:
    """The failure mode a construction-time check cannot see."""

    async def test_an_absolute_trading_url_is_refused_at_request_time(self) -> None:
        async with guarded_archive_client(base_url=ARCHIVE_BASE) as client:
            with pytest.raises(SafetyViolation):
                await client.get("https://api.binance.com/api/v3/order")

    async def test_an_absolute_testnet_url_is_refused_at_request_time(self) -> None:
        async with guarded_archive_client(base_url=ARCHIVE_BASE) as client:
            with pytest.raises(SafetyViolation):
                await client.get("https://testnet.binance.vision/api/v3/order")


def _egress_over(handler: httpx.MockTransport) -> GuardedArchiveEgress:
    """A real `GuardedArchiveEgress` with its socket layer substituted.

    Substituting the transport is not the same as mocking a venue response: there is no
    exchange semantics here, only bytes, and the guard hook still runs on every request
    because it is an event hook on the client rather than a property of the transport.
    """
    egress = GuardedArchiveEgress()
    egress._client._transport = handler
    return egress


@pytest.mark.asyncio
class TestGuardedArchiveEgress:
    async def test_it_satisfies_the_protocol(self) -> None:
        async with GuardedArchiveEgress() as egress:
            assert isinstance(egress, ArchiveEgress)

    async def test_the_guard_still_runs_with_the_transport_substituted(self) -> None:
        """Otherwise every test below would be proving something about a mock."""
        transport = httpx.MockTransport(lambda _request: httpx.Response(200, content=b""))
        async with _egress_over(transport) as egress:
            with pytest.raises(SafetyViolation):
                await egress.get_text("https://api.binance.com/api/v3/time")

    async def test_get_text_returns_the_body_and_counts_the_request(self) -> None:
        transport = httpx.MockTransport(lambda _request: httpx.Response(200, text="hello"))
        async with _egress_over(transport) as egress:
            assert await egress.get_text(f"{SAMPLE_URL}.CHECKSUM") == "hello"
            assert egress.request_count == 1

    async def test_get_text_raises_on_a_non_200(self) -> None:
        transport = httpx.MockTransport(lambda _request: httpx.Response(404))
        async with _egress_over(transport) as egress:
            with pytest.raises(ArchiveUnavailableError, match="404"):
                await egress.get_text(f"{SAMPLE_URL}.CHECKSUM")

    async def test_download_writes_the_bytes_and_returns_their_digest(self, tmp_path: Path) -> None:
        payload = b"PK\x03\x04" + bytes(range(256)) * 40
        transport = httpx.MockTransport(lambda _request: httpx.Response(200, content=payload))
        destination = tmp_path / "archive.zip"

        async with _egress_over(transport) as egress:
            digest = await egress.download(SAMPLE_URL, destination)

        assert destination.read_bytes() == payload
        assert digest == hashlib.sha256(payload).hexdigest()

    async def test_download_raises_on_a_non_200_and_writes_nothing(self, tmp_path: Path) -> None:
        """The body is never read on a non-200, so the destination stays absent rather
        than holding an HTML error page that a parser would then be handed."""
        transport = httpx.MockTransport(lambda _request: httpx.Response(403, text="denied"))
        destination = tmp_path / "archive.zip"

        async with _egress_over(transport) as egress:
            with pytest.raises(ArchiveUnavailableError, match="403"):
                await egress.download(SAMPLE_URL, destination)

        assert not destination.exists()

    async def test_the_request_counter_accumulates_across_calls(self) -> None:
        transport = httpx.MockTransport(lambda _request: httpx.Response(200, text="x"))
        async with _egress_over(transport) as egress:
            assert egress.request_count == 0
            await egress.get_text(SAMPLE_URL)
            await egress.get_text(SAMPLE_URL)
            assert egress.request_count == EXPECTED_REQUESTS
