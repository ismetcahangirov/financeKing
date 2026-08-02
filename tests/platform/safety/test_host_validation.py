"""Host validation, including the lookalikes hand-written cases run out of.

The two Hypothesis properties are the point of this module. A substring check passes
every one of the deterministic cases below and fails the properties, which is why the
properties exist: `"testnet.binance.vision" in url` is the implementation somebody
reaches for, and it accepts `https://api.binance.com/?note=testnet.binance.vision`.
"""

from __future__ import annotations

import pytest
from hypothesis import given
from hypothesis import strategies as st

from fking.platform.safety import PERMITTED_HOSTS, SafetyViolation, assert_host_permitted

pytestmark = pytest.mark.unit

PRODUCTION_HOSTS = [
    "api.binance.com",
    "api1.binance.com",
    "api2.binance.com",
    "api-gcp.binance.com",
    "fapi.binance.com",
    "dapi.binance.com",
    "stream.binance.com",
    "fstream.binance.com",
    "api.bybit.com",
    "stream.bybit.com",
]


@pytest.mark.parametrize("host", PRODUCTION_HOSTS)
def test_production_hosts_are_rejected(host: str) -> None:
    with pytest.raises(SafetyViolation):
        assert_host_permitted(f"https://{host}/api/v3/time")


@pytest.mark.parametrize("host", sorted(PERMITTED_HOSTS))
def test_permitted_hosts_are_accepted(host: str) -> None:
    assert assert_host_permitted(f"https://{host}/api/v3/time") == host


@pytest.mark.parametrize("host", sorted(PERMITTED_HOSTS))
def test_a_permitted_host_is_accepted_over_websockets(host: str) -> None:
    assert assert_host_permitted(f"wss://{host}/ws") == host


@pytest.mark.parametrize("host", sorted(PERMITTED_HOSTS))
def test_host_matching_is_case_insensitive(host: str) -> None:
    """DNS is case-insensitive, so an uppercase host is the same endpoint.

    Rejecting it would be a false negative that sends someone looking for a bug in
    their URL construction rather than in their endpoint.
    """
    assert assert_host_permitted(f"https://{host.upper()}/") == host


class TestLookalikes:
    """Each of these contains a permitted host as a substring and is not one."""

    @pytest.mark.parametrize(
        "url",
        [
            # A suffix attack: the real host is attacker.example.
            "https://testnet.binance.vision.attacker.example/api/v3/time",
            # userinfo: everything before @ is credentials, not a host.
            "https://testnet.binance.vision@api.binance.com/api/v3/time",
            # The permitted host hidden in a query parameter.
            "https://api.binance.com/api/v3/time?note=testnet.binance.vision",
            # ...and in a path, and in a fragment.
            "https://api.binance.com/testnet.binance.vision/order",
            "https://api.binance.com/api/v3/time#testnet.binance.vision",
            # A prefix that is not a subdomain boundary.
            "https://nottestnet.binance.vision/api/v3/time",
        ],
    )
    def test_a_lookalike_url_is_rejected(self, url: str) -> None:
        with pytest.raises(SafetyViolation):
            assert_host_permitted(url)

    def test_a_trailing_dot_fqdn_is_accepted_as_the_same_host(self) -> None:
        """`example.com.` is the same name in DNS, with the root label made explicit.

        Treating it as a different host would be a false negative; treating it as a
        *different permitted* host would let `api.binance.com.` through. The
        normalisation must strip it and then match.
        """
        assert assert_host_permitted("https://testnet.binance.vision./api/v3/time") == (
            "testnet.binance.vision"
        )

    def test_a_trailing_dot_does_not_smuggle_a_production_host(self) -> None:
        with pytest.raises(SafetyViolation):
            assert_host_permitted("https://api.binance.com./api/v3/time")


class TestSchemes:
    @pytest.mark.parametrize("scheme", ["http", "ws", "ftp", "file"])
    def test_a_non_tls_scheme_is_rejected_even_on_a_permitted_host(self, scheme: str) -> None:
        """A downgrade to plaintext exposes the API key on the wire.

        The host is right, so a host-only check would accept this.
        """
        with pytest.raises(SafetyViolation, match="scheme"):
            assert_host_permitted(f"{scheme}://testnet.binance.vision/api/v3/time")


class TestMalformed:
    @pytest.mark.parametrize("url", ["", "not-a-url", "https://", "/api/v3/time", "://x"])
    def test_a_url_without_a_host_is_rejected(self, url: str) -> None:
        """Never silently pass. A URL we cannot parse is a URL we cannot vouch for."""
        with pytest.raises(SafetyViolation):
            assert_host_permitted(url)


# ---------------------------------------------------------------------------
# Properties. Hand-written lookalikes cover what was thought of; these cover the
# shape of the mistake rather than its instances.
# ---------------------------------------------------------------------------

hostname_characters = st.text(
    alphabet=st.characters(whitelist_categories=("Ll", "Nd"), whitelist_characters="-."),
    min_size=1,
    max_size=40,
)


@given(host=hostname_characters)
def test_no_host_outside_the_allowlist_is_ever_accepted(host: str) -> None:
    normalised = host.rstrip(".").casefold()
    if normalised in PERMITTED_HOSTS:
        return
    # `pytest.raises` rather than try/except: tools/checks/no_catch_safety.py forbids
    # an `except SafetyViolation` clause anywhere in src/ or tests/, because a test
    # that catches it is the precedent the next `except` will point at.
    with pytest.raises(SafetyViolation):
        assert_host_permitted(f"https://{host}/")


@given(
    permitted=st.sampled_from(sorted(PERMITTED_HOSTS)),
    position=st.sampled_from(["userinfo", "suffix", "path", "query", "fragment"]),
)
def test_a_permitted_host_outside_the_host_position_is_never_accepted(
    permitted: str, position: str
) -> None:
    """The property a substring check fails.

    Wherever a permitted name appears in a URL, only the host position may authorise
    the request -- and in every construction below the host is api.binance.com.
    """
    hostile = "api.binance.com"
    url = {
        "userinfo": f"https://{permitted}@{hostile}/api/v3/time",
        "suffix": f"https://{permitted}.{hostile}/api/v3/time",
        "path": f"https://{hostile}/{permitted}/api/v3/time",
        "query": f"https://{hostile}/api/v3/time?host={permitted}",
        "fragment": f"https://{hostile}/api/v3/time#{permitted}",
    }[position]

    with pytest.raises(SafetyViolation):
        assert_host_permitted(url)
