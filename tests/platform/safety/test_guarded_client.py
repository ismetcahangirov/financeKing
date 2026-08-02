"""The guarded transports.

The load-bearing test here is the per-request one. Validating only at construction is
the failure mode `httpx` and `ccxt` both create: an absolute URL passed to `.get()`
replaces `base_url` entirely, so a constructor check never sees it.
"""

from __future__ import annotations

import ast
import inspect
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

import httpx
import pytest
import websockets

import fking.platform.safety._allowlist as allowlist_module
import fking.platform.safety.client as safety_client
from fking.platform.safety import (
    PERMITTED_HOSTS,
    SafetyViolation,
    guarded_client,
    guarded_ws_connect,
)

pytestmark = pytest.mark.unit

PERMITTED_BASE = "https://testnet.binance.vision"
TIMEOUT_SECONDS = 3.5
EXPECTED_HOST_COUNT = 7


class TestConstruction:
    def test_a_production_base_url_is_refused_at_construction(self) -> None:
        with pytest.raises(SafetyViolation):
            guarded_client(base_url="https://api.binance.com")

    def test_a_permitted_base_url_is_accepted(self) -> None:
        client = guarded_client(base_url=PERMITTED_BASE)
        assert str(client.base_url).startswith(PERMITTED_BASE)

    def test_an_empty_base_url_is_allowed_because_every_request_is_checked(self) -> None:
        """A client with no base URL is still safe: the hook runs per request."""
        assert guarded_client() is not None


class TestTransportConfiguration:
    def test_proxy_environment_cannot_reroute_a_validated_host(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """With trust_env on, HTTPS_PROXY silently changes where the bytes go.

        The URL still reads testnet.binance.vision, the host check still passes, and
        the request is answered by whatever the proxy chose.
        """
        monkeypatch.setenv("HTTPS_PROXY", "http://127.0.0.1:9")
        monkeypatch.setenv("ALL_PROXY", "http://127.0.0.1:9")
        assert guarded_client(base_url=PERMITTED_BASE).trust_env is False

    def test_redirects_are_not_followed(self) -> None:
        """A redirect is re-validated by the hook, so the risk is not a bypass.

        It is provenance: an allowlisted redirect still changes which endpoint
        answered, and reconciliation attributes the response to the host we asked.
        """
        assert guarded_client(base_url=PERMITTED_BASE).follow_redirects is False

    def test_the_timeout_is_applied(self) -> None:
        client = guarded_client(base_url=PERMITTED_BASE, timeout_seconds=TIMEOUT_SECONDS)
        assert client.timeout.connect == TIMEOUT_SECONDS


@pytest.mark.asyncio
class TestPerRequestValidation:
    """The failure mode a construction-time check cannot see."""

    async def test_an_absolute_production_url_is_refused_at_request_time(self) -> None:
        async with guarded_client(base_url=PERMITTED_BASE) as client:
            with pytest.raises(SafetyViolation):
                await client.get("https://api.binance.com/api/v3/time")

    async def test_a_lookalike_absolute_url_is_refused_at_request_time(self) -> None:
        async with guarded_client(base_url=PERMITTED_BASE) as client:
            with pytest.raises(SafetyViolation):
                await client.get("https://testnet.binance.vision.attacker.example/api/v3/time")

    async def test_a_relative_path_resolves_against_the_permitted_base(self) -> None:
        """Must reach the network rather than the guard.

        Asserting on the connection error is the point: it proves the request got
        past validation and was actually attempted, so a guard that rejected
        everything would fail this test.
        """
        async with guarded_client(base_url="https://ws-api.testnet.binance.vision") as client:
            with pytest.raises(httpx.HTTPError):
                await client.get("/nonexistent", timeout=0.001)


@pytest.mark.asyncio
class TestGuardedWebSocket:
    async def test_a_production_websocket_host_is_refused(self) -> None:
        with pytest.raises(SafetyViolation):
            async with guarded_ws_connect("wss://stream.binance.com:9443/ws"):
                pytest.fail("connection to a production host was attempted")

    async def test_a_plaintext_websocket_scheme_is_refused(self) -> None:
        with pytest.raises(SafetyViolation, match="scheme"):
            async with guarded_ws_connect("ws://stream.testnet.binance.vision/ws"):
                pytest.fail("a plaintext websocket was attempted")

    async def test_a_permitted_host_reaches_the_transport_with_its_url_intact(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The accept path, with the socket layer substituted.

        Connecting for real would need `stream.testnet.binance.vision` reachable from
        CI, which makes a unit test depend on a third party's uptime. Substituting the
        transport is not the same as mocking a venue response -- there is no exchange
        behaviour being invented here, only a socket that is not opened. What is
        asserted is this wrapper's own contract: the validated URL is passed through
        unaltered, the timeout is honoured, and the connection is what the caller gets.
        """
        opened: list[tuple[str, float]] = []
        sentinel = object()

        @asynccontextmanager
        async def fake_connect(url: str, *, open_timeout: float) -> AsyncIterator[object]:
            opened.append((url, open_timeout))
            yield sentinel

        # Patched on the module object the kernel holds a reference to, so this is the
        # same `websockets.connect` that `guarded_ws_connect` will call.
        monkeypatch.setattr(websockets, "connect", fake_connect)

        url = "wss://stream.testnet.binance.vision/ws"
        async with guarded_ws_connect(url, timeout_seconds=4.5) as connection:
            assert connection is sentinel

        assert opened == [(url, 4.5)]


class TestNoConfigurationOverride:
    """No environment variable and no configuration key can widen the allowlist."""

    @pytest.mark.parametrize(
        "variable",
        [
            "FKING_ALLOWED_HOSTS",
            "FKING_PERMITTED_HOSTS",
            "ALLOW_MAINNET",
            "FKING_ALLOW_MAINNET",
            "FKING_SAFETY_DISABLED",
            "BINANCE_BASE_URL",
        ],
    )
    def test_no_environment_variable_widens_the_allowlist(
        self, monkeypatch: pytest.MonkeyPatch, variable: str
    ) -> None:
        monkeypatch.setenv(variable, "api.binance.com")
        with pytest.raises(SafetyViolation):
            guarded_client(base_url="https://api.binance.com")

    def test_the_module_reads_no_environment_and_no_files(self) -> None:
        """The structural version of the same claim.

        A test that sets the variables it thought of only covers those names. This
        asserts the module contains no mechanism to read *any* of them.
        """
        forbidden_reads = {"getenv", "environ", "open", "read_text", "load", "loads"}
        for module in (allowlist_module, safety_client):
            tree = ast.parse(inspect.getsource(module))
            used = {
                node.attr if isinstance(node, ast.Attribute) else node.id
                for node in ast.walk(tree)
                if isinstance(node, ast.Attribute | ast.Name)
            }
            assert not (used & forbidden_reads), (
                f"{module.__name__} reads external state: {sorted(used & forbidden_reads)}"
            )


def test_permitted_hosts_is_reexported_from_the_package() -> None:
    """Callers import from `fking.platform.safety`, never from a private submodule."""
    assert len(PERMITTED_HOSTS) == EXPECTED_HOST_COUNT
