"""The guarded `ccxt` construction path.

`ccxt` builds its own transport by default, which makes it a complete bypass of
`client.py`: an `import-linter` contract sees `fking.execution -> ccxt`, not
`fking.execution -> aiohttp`, and the allowlist never runs. This module is where that is
closed, so it carries the kernel's 100% coverage floor along with the rest of
`fking.platform.safety`.

Three mechanisms, tested separately because each closes a hole the others leave:

1. the injected session's trace hook, which fires on requests ccxt issues internally;
2. the `urls` re-walk after `set_sandbox_mode(True)`, which refuses a sandbox map the
   dependency got wrong;
3. `call()` returning the venue's response *text*, which is what keeps `Decimal` built
   from the venue's characters rather than from ccxt's float-parsed structure.
"""

from __future__ import annotations

import re
from collections.abc import Mapping
from typing import Final

import aiohttp
import ccxt.async_support as ccxt_async
import pytest

from fking.platform.safety import (
    SafetyViolation,
    UnknownVenueEndpointError,
    VenueTransportError,
    assert_sandbox_urls_permitted,
    guarded_aiohttp_session,
    guarded_ccxt,
)
from fking.platform.safety.exchange import (
    GuardedCcxtExchange,
    GuardedExchange,
    _reject_redirect,
)

pytestmark = pytest.mark.unit

PERMITTED_URL: Final[str] = "https://testnet.binance.vision/api/v3/time"
PRODUCTION_URL: Final[str] = "https://api.binance.com/api/v3/order"


class _FakeCcxtExchange:
    """The slice of a ccxt exchange the kernel touches, with no transport behind it."""

    def __init__(self, *, body: str | None = "{}", raises: BaseException | None = None) -> None:
        self.id = "binance"
        self.urls: dict[str, object] = {
            "api": {"public": "https://testnet.binance.vision/api/v3"},
            "apiBackup": {"public": "https://api.binance.com/api/v3"},
        }
        self.last_http_response: str | None = None
        self.sandbox_enabled = False
        self.closed = False
        self._body = body
        self._raises = raises
        self.received: list[Mapping[str, str]] = []

    def set_sandbox_mode(self, enabled: bool) -> None:
        self.sandbox_enabled = enabled

    async def close(self) -> None:
        self.closed = True

    async def publicGetTime(self, params: Mapping[str, str]) -> object:  # noqa: N802
        """Named the way ccxt names its implicit-API methods, because that is the contract."""
        self.received.append(params)
        self.last_http_response = self._body
        if self._raises is not None:
            raise self._raises
        return {"serverTime": 0}

    not_callable = "this attribute exists and is not a method"


def _guarded(exchange: _FakeCcxtExchange, session: aiohttp.ClientSession) -> GuardedCcxtExchange:
    return GuardedCcxtExchange(exchange, session, "binance-spot-testnet")


class TestTheInjectedSession:
    @pytest.mark.asyncio
    async def test_a_production_host_is_refused_on_every_request(self) -> None:
        session = guarded_aiohttp_session()
        try:
            with pytest.raises(SafetyViolation, match="not in the compiled-in"):
                await session.get(PRODUCTION_URL)
        finally:
            await session.close()

    @pytest.mark.asyncio
    async def test_a_plaintext_scheme_is_refused(self) -> None:
        """A downgrade puts a signed request on the wire in clear."""
        session = guarded_aiohttp_session()
        try:
            with pytest.raises(SafetyViolation, match="non-TLS scheme"):
                await session.get("http://testnet.binance.vision/api/v3/time")
        finally:
            await session.close()

    @pytest.mark.asyncio
    async def test_the_session_does_not_trust_the_environment(self) -> None:
        session = guarded_aiohttp_session(timeout_seconds=3)
        try:
            assert session.trust_env is False
        finally:
            await session.close()

    @pytest.mark.asyncio
    async def test_a_redirect_is_refused_rather_than_followed(self) -> None:
        """Not about bypass -- the target would be re-validated. It is provenance: an
        allowlisted redirect still changes which endpoint answered, and reconciliation
        attributes a response to the host it asked."""

        class _Params:
            url = "https://testnet.binance.vision/api/v3/time"

        with pytest.raises(SafetyViolation, match=r"does not follow redirects"):
            await _reject_redirect(None, None, _Params())  # type: ignore[arg-type]


class TestTheSandboxUrlWalk:
    def test_an_allowlisted_map_returns_the_endpoints_it_validated(self) -> None:
        validated = assert_sandbox_urls_permitted(
            {"api": {"public": "https://testnet.binance.vision/api/v3"}}
        )
        assert validated == ("https://testnet.binance.vision/api/v3",)

    def test_a_production_endpoint_anywhere_in_the_map_aborts(self) -> None:
        with pytest.raises(SafetyViolation, match=re.escape("api.binance.com")):
            assert_sandbox_urls_permitted(
                {"api": {"nested": {"deep": ["https://api.binance.com/api/v3"]}}}
            )

    def test_the_documentation_keys_are_not_treated_as_endpoints(self) -> None:
        """`www` is https://www.binance.com and `fees` is a fee schedule page. Neither is
        ever fetched, and validating them would refuse every correct construction."""
        assert (
            assert_sandbox_urls_permitted(
                {
                    "logo": "https://github.com/user-attachments/assets/x",
                    "www": "https://www.binance.com",
                    "doc": ["https://developers.binance.com/en"],
                    "fees": "https://www.binance.com/en/fee/schedule",
                    "referral": {"url": "https://accounts.binance.com/register"},
                    "api_management": "https://www.binance.com/en/usercenter/settings/api-management",
                    "demo": {"public": "https://demo-api.binance.com/api/v3"},
                    "apiBackup": {"public": "https://api.binance.com/api/v3"},
                }
            )
            == ()
        )

    def test_every_endpoint_in_a_list_valued_key_is_validated(self) -> None:
        """ccxt stores several endpoints under one key for some exchanges, and the
        walk has to reach all of them rather than stopping at the first.

        Every other case in this class abandons the walk on the first violation, so
        without this one the list branch is never iterated to completion and a
        `break` smuggled into it would pass every test here.
        """
        validated = assert_sandbox_urls_permitted(
            {
                "api": {
                    "public": [
                        "https://testnet.binance.vision/api/v3",
                        "https://testnet.binance.vision/api/v1",
                    ]
                }
            }
        )
        assert validated == (
            "https://testnet.binance.vision/api/v3",
            "https://testnet.binance.vision/api/v1",
        )

    def test_a_later_endpoint_in_a_list_is_still_refused(self) -> None:
        """The violation is second, so a walk that checked only the head would pass."""
        with pytest.raises(SafetyViolation, match=re.escape("api.binance.com")):
            assert_sandbox_urls_permitted(
                {
                    "api": {
                        "public": [
                            "https://testnet.binance.vision/api/v3",
                            "https://api.binance.com/api/v3",
                        ]
                    }
                }
            )

    def test_a_non_string_leaf_is_skipped_rather_than_stringified(self) -> None:
        """ccxt's `urls` carries ints and None in places. Coercing them would produce a
        host check against the text 'None'."""
        assert assert_sandbox_urls_permitted({"api": {"port": 443, "unset": None}}) == ()


class TestConstruction:
    @pytest.mark.asyncio
    async def test_an_unknown_exchange_id_is_refused(self) -> None:
        """A client whose endpoints cannot be resolved is a client that cannot be checked."""
        with pytest.raises(SafetyViolation, match="unknown ccxt exchange id"):
            await guarded_ccxt(ccxt_exchange_id="notanexchange", venue_id="binance-spot-testnet")

    @pytest.mark.asyncio
    async def test_a_real_construction_enables_sandbox_mode_and_validates_its_urls(self) -> None:
        exchange = await guarded_ccxt(
            ccxt_exchange_id="binance",
            venue_id="binance-spot-testnet",
            api_key="not-a-real-key",
            secret="not-a-real-secret",  # noqa: S106 - a literal, not a credential
            options={"defaultType": "spot"},
            timeout_seconds=3,
        )
        try:
            assert isinstance(exchange, GuardedExchange)
            assert exchange.venue_id == "binance-spot-testnet"
            assert exchange.request_count == 0
        finally:
            await exchange.aclose()


class TestCall:
    @pytest.mark.asyncio
    async def test_the_raw_response_body_is_returned_not_the_parsed_structure(self) -> None:
        """The return value ccxt hands back has already been through the stdlib float
        parser. Reading the recorded body is what keeps `Decimal` exact."""
        session = guarded_aiohttp_session()
        fake = _FakeCcxtExchange(body='{"quoteQty":"0.00000001"}')
        guarded = _guarded(fake, session)
        try:
            assert await guarded.call("publicGetTime", {"symbol": "BTCUSDT"}) == (
                '{"quoteQty":"0.00000001"}'
            )
            assert guarded.request_count == 1
            assert fake.received == [{"symbol": "BTCUSDT"}]
        finally:
            await session.close()

    @pytest.mark.asyncio
    async def test_a_venue_rejection_is_returned_as_its_body_rather_than_a_ccxt_exception(
        self,
    ) -> None:
        """ccxt turns a rejection into one of its own exception types, having recorded
        the body first. Classifying it belongs to `fking.execution` against the venue's
        error codes, so the body is returned and the ccxt type is discarded."""
        session = guarded_aiohttp_session()
        fake = _FakeCcxtExchange(
            body='{"code":-2014,"msg":"API-key format invalid."}',
            raises=ccxt_async.AuthenticationError("binance"),
        )
        try:
            assert await _guarded(fake, session).call("publicGetTime", {}) == (
                '{"code":-2014,"msg":"API-key format invalid."}'
            )
        finally:
            await session.close()

    @pytest.mark.asyncio
    async def test_a_transport_failure_with_no_body_raises_without_leaking_a_ccxt_type(
        self,
    ) -> None:
        session = guarded_aiohttp_session()
        fake = _FakeCcxtExchange(body=None, raises=ccxt_async.RequestTimeout("binance"))
        try:
            with pytest.raises(VenueTransportError, match="produced no response body") as failed:
                await _guarded(fake, session).call("publicGetTime", {})
            assert failed.value.cause_type == "RequestTimeout"
            assert failed.value.venue_id == "binance-spot-testnet"
        finally:
            await session.close()

    @pytest.mark.asyncio
    async def test_a_stale_body_from_a_previous_call_is_never_returned(self) -> None:
        """ccxt leaves the last response in place. Without clearing it, a transport
        failure would hand the adapter the *previous* success to parse."""
        session = guarded_aiohttp_session()
        fake = _FakeCcxtExchange(body='{"serverTime":1}')
        guarded = _guarded(fake, session)
        try:
            assert await guarded.call("publicGetTime", {}) == '{"serverTime":1}'
            fake._body = None
            fake._raises = ccxt_async.NetworkError("binance")
            with pytest.raises(VenueTransportError):
                await guarded.call("publicGetTime", {})
        finally:
            await session.close()

    @pytest.mark.asyncio
    async def test_a_success_that_recorded_no_body_is_a_configuration_failure(self) -> None:
        """`enableLastHttpResponse` must stay on, or the Decimal path silently has no
        input and the adapter would parse `None`."""
        session = guarded_aiohttp_session()
        try:
            with pytest.raises(UnknownVenueEndpointError, match="enableLastHttpResponse"):
                await _guarded(_FakeCcxtExchange(body=None), session).call("publicGetTime", {})
        finally:
            await session.close()

    @pytest.mark.asyncio
    async def test_an_endpoint_the_exchange_does_not_implement_is_refused(self) -> None:
        session = guarded_aiohttp_session()
        try:
            with pytest.raises(UnknownVenueEndpointError, match="implements no endpoint"):
                await _guarded(_FakeCcxtExchange(), session).call("privateGetNothing", {})
        finally:
            await session.close()

    @pytest.mark.asyncio
    async def test_a_non_callable_attribute_is_not_mistaken_for_an_endpoint(self) -> None:
        session = guarded_aiohttp_session()
        try:
            with pytest.raises(UnknownVenueEndpointError, match="implements no endpoint"):
                await _guarded(_FakeCcxtExchange(), session).call("not_callable", {})
        finally:
            await session.close()

    @pytest.mark.asyncio
    async def test_closing_releases_both_the_exchange_and_the_injected_session(self) -> None:
        """ccxt sets own_session=False when a session is injected, so it releases its own
        resources and leaves ours open -- which means the kernel must close ours."""
        session = guarded_aiohttp_session()
        fake = _FakeCcxtExchange()
        await _guarded(fake, session).aclose()

        assert fake.closed is True
        assert session.closed is True
