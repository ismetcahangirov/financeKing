"""Constructing a venue cannot produce a client that can reach production.

Three separate mechanisms are asserted here, because each closes a hole the other two
leave open:

1. **Per-request host validation.** An absolute production URL passed at call time
   replaces whatever base the client was built with, so a construction-time check never
   sees it. Asserted on both the REST path and the WebSocket path, and asserted to fire
   *before a socket is opened* rather than merely before a response is returned -- the
   connector is monkeypatched to fail loudly if it is ever reached.
2. **Re-validating `exchange.urls` after `set_sandbox_mode(True)`.** Those endpoints come
   from `ccxt`, and "a library changing a default base URL in a minor bump" is item 4 of
   the threat model. A sandbox map containing a production host must abort construction,
   not log.
3. **Profiles refuse a production host at construction.** A `VenueProfile` is data, and
   data is the thing people edit; a profile naming `api.binance.com` cannot exist.

`SafetyViolation` is never caught anywhere in this file -- `pytest.raises` is used
throughout, which is what `tools/checks/no_catch_safety.py` requires and why it scans
`tests/` as well as `src/`.
"""

from __future__ import annotations

from typing import Final

import aiohttp
import pytest
import websockets

from fking.domain import Venue
from fking.execution import (
    BINANCE_SPOT_TESTNET,
    BYBIT_TESTNET,
    VENUE_PROFILES,
    BinanceVenue,
    PermanentExchangeError,
    VenueProfile,
)
from fking.platform.safety import (
    SafetyViolation,
    VenueResponseMetadata,
    assert_sandbox_urls_permitted,
    guarded_aiohttp_session,
    guarded_ccxt,
    guarded_ws_connect,
)

pytestmark = pytest.mark.unit

PRODUCTION_REST_URLS: Final[tuple[str, ...]] = (
    "https://api.binance.com/api/v3/order",
    "https://api1.binance.com/api/v3/account",
    "https://fapi.binance.com/fapi/v1/order",
    "https://dapi.binance.com/dapi/v1/order",
    "https://papi.binance.com/papi/v1/um/order",
    "https://api.bybit.com/v5/order/create",
)

PRODUCTION_WS_URLS: Final[tuple[str, ...]] = (
    "wss://stream.binance.com:9443/ws",
    "wss://fstream.binance.com/ws",
    "wss://ws-api.binance.com/ws-api/v3",
    "wss://stream.bybit.com/v5/private",
)


class _SocketOpenedError(Exception):
    """Raised by the patched connector. Its existence in a traceback is the failure."""


@pytest.fixture
def refuse_to_open_a_socket(monkeypatch: pytest.MonkeyPatch) -> None:
    """Make any actual connection attempt an unmistakable failure.

    Without this the tests below would pass for the wrong reason on a machine with no
    network: a DNS failure and a refused request are both "no order reached Binance",
    and only one of them is the guarantee.
    """

    async def _explode(*_args: object, **_kwargs: object) -> None:
        raise _SocketOpenedError("the transport opened a connection to a non-allowlisted host")

    monkeypatch.setattr(aiohttp.TCPConnector, "connect", _explode)


@pytest.mark.asyncio
@pytest.mark.parametrize("url", PRODUCTION_REST_URLS)
@pytest.mark.usefixtures("refuse_to_open_a_socket")
async def test_a_production_url_passed_per_request_is_refused_before_a_socket_opens(
    url: str,
) -> None:
    """The failure mode a construction-time check cannot see.

    `ccxt` resolves each request's URL from its own `urls` map, so the base the session
    was built with is not the base the request uses. The check therefore lives on the
    request, and this asserts it fires there.
    """
    session = guarded_aiohttp_session()
    try:
        with pytest.raises(SafetyViolation, match="not in the compiled-in"):
            await session.get(url)
    finally:
        await session.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("url", PRODUCTION_WS_URLS)
async def test_a_production_websocket_url_is_refused_before_a_socket_opens(
    url: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The same guarantee on the stream path.

    A WebSocket URL is fixed at connect time and cannot be overridden per message, so
    one check is sufficient -- but it has to happen before `websockets.connect`, which is
    what the patched connector proves.
    """

    async def _explode(*_args: object, **_kwargs: object) -> None:
        raise _SocketOpenedError("guarded_ws_connect opened a socket to a non-allowlisted host")

    monkeypatch.setattr(websockets, "connect", _explode)

    with pytest.raises(SafetyViolation, match="not in the compiled-in"):
        async with guarded_ws_connect(url):
            pass  # pragma: no cover - the context manager never yields


@pytest.mark.asyncio
@pytest.mark.usefixtures("refuse_to_open_a_socket")
async def test_a_permitted_host_still_reaches_the_transport() -> None:
    """The guard must not be a blanket refusal.

    Without this, a `guarded_aiohttp_session` that rejected *everything* would satisfy
    every other test in this file -- and would look exactly like a working guard.
    """
    session = guarded_aiohttp_session()
    try:
        with pytest.raises(_SocketOpenedError):
            await session.get("https://testnet.binance.vision/api/v3/time")
    finally:
        await session.close()


@pytest.mark.asyncio
async def test_the_guarded_session_ignores_proxy_environment_variables(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """With trust_env on, HTTPS_PROXY reroutes a validated host and the URL still reads testnet."""
    monkeypatch.setenv("HTTPS_PROXY", "http://127.0.0.1:9")
    session = guarded_aiohttp_session()
    try:
        assert session.trust_env is False
    finally:
        await session.close()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "venue", [Venue.BINANCE_SPOT_TESTNET, Venue.BINANCE_FUTURES_TESTNET], ids=str
)
async def test_a_constructed_exchange_resolves_only_allowlisted_endpoints(venue: Venue) -> None:
    """`set_sandbox_mode(True)` is applied and its result is checked, not trusted."""
    profile = VENUE_PROFILES[venue]
    exchange = await guarded_ccxt(
        ccxt_exchange_id=profile.ccxt_exchange_id,
        venue_id=str(profile.venue_id),
        options={"defaultType": "spot" if profile.market == "spot" else "future"},
    )
    try:
        assert exchange.venue_id == str(venue)
        assert exchange.request_count == 0
    finally:
        await exchange.aclose()


def test_a_sandbox_url_map_containing_a_production_host_aborts_construction() -> None:
    """The library-regression case: ccxt ships a sandbox map with a production endpoint.

    Aborting rather than logging is the whole point. A logged warning on a machine
    nobody is watching is indistinguishable from no check at all, and the first
    observable symptom of the alternative is a filled order.
    """
    poisoned = {
        "api": {
            "public": "https://testnet.binance.vision/api/v3",
            # One entry, buried among correct ones, exactly as a regression would arrive.
            "private": "https://api.binance.com/api/v3",
        }
    }
    with pytest.raises(SafetyViolation, match=r"api\.binance\.com"):
        assert_sandbox_urls_permitted(poisoned)


def test_the_saved_production_map_set_sandbox_mode_leaves_behind_is_not_a_false_positive() -> None:
    """`set_sandbox_mode(True)` moves the production map to `apiBackup`.

    Validating that key would refuse every correct construction, so it is excluded --
    and excluding it is safe because nothing resolves a request from it while sandbox
    mode is on, and the per-request hook would refuse the request if anything did. This
    test pins the exclusion so that widening it is a deliberate edit rather than a
    side effect of loosening the walk.
    """
    with_backup = {
        "api": {"public": "https://testnet.binance.vision/api/v3"},
        "apiBackup": {"public": "https://api.binance.com/api/v3"},
        "www": "https://www.binance.com",
        "doc": ["https://developers.binance.com/en"],
        "fees": "https://www.binance.com/en/fee/schedule",
    }
    assert assert_sandbox_urls_permitted(with_backup) == ("https://testnet.binance.vision/api/v3",)


@pytest.mark.parametrize(
    "field",
    ["rest_url", "ws_stream_url", "ws_api_url"],
)
def test_a_profile_naming_a_production_host_cannot_be_constructed(field: str) -> None:
    """A profile is data, and data is what people edit.

    Every URL on it is validated at construction, so the misconfiguration is a
    `SafetyViolation` at import rather than a discovery on the first order.
    """
    fields = BINANCE_SPOT_TESTNET.model_dump()
    fields[field] = (
        "wss://stream.binance.com:9443/ws" if "ws" in field else ("https://api.binance.com")
    )
    with pytest.raises(SafetyViolation):
        VenueProfile(**fields)


def test_a_non_binance_profile_is_refused_by_the_binance_adapter() -> None:
    """The Bybit profile exists; the Bybit *adapter* is #112 and is not this class.

    Refusing loudly beats a class that half-works against a venue whose symbol naming,
    filter semantics, error codes and rate limits are all different.
    """

    class _NeverCalled:
        @property
        def venue_id(self) -> str:
            return str(BYBIT_TESTNET.venue_id)

        @property
        def request_count(self) -> int:
            return 0

        @property
        def last_response_metadata(self) -> VenueResponseMetadata | None:
            return None

        async def call(self, _endpoint: str, _params: object) -> str:  # pragma: no cover
            raise AssertionError("the adapter must refuse before it can call anything")

        async def aclose(self) -> None:  # pragma: no cover
            raise AssertionError("the adapter must refuse before it can close anything")

    with pytest.raises(PermanentExchangeError, match="not a Binance market"):
        BinanceVenue(_NeverCalled(), BYBIT_TESTNET)
