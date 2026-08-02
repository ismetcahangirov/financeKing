"""The allowlist literal itself.

A golden test, deliberately: widening the allowlist must break a test, not merely
pass review. The CI label check makes the change visible to the reviewer; this makes
it visible to the author, at the moment they make it. Removing either one is itself a
change to the safety kernel.
"""

from __future__ import annotations

import pytest

from fking.platform.safety import PERMITTED_HOSTS

pytestmark = pytest.mark.unit

# The seven endpoints from .claude/contexts/binance-testnet.md, which is the researched
# table rather than an inferred one. Note `stream.binancefuture.com` and NOT
# `fstream.binancefuture.com`: the `fstream.` prefix belongs to *production*
# (fstream.binance.com), and carrying a production naming pattern into a testnet
# allowlist is exactly the class of mistake this module exists to prevent.
EXPECTED_HOSTS = frozenset(
    {
        "testnet.binance.vision",
        "stream.testnet.binance.vision",
        "ws-api.testnet.binance.vision",
        "testnet.binancefuture.com",
        "stream.binancefuture.com",
        "api-testnet.bybit.com",
        "stream-testnet.bybit.com",
    }
)


def test_allowlist_is_exactly_the_expected_literal() -> None:
    assert PERMITTED_HOSTS == EXPECTED_HOSTS


def test_allowlist_is_immutable() -> None:
    """A set could be mutated at import time by anything that got a reference."""
    assert isinstance(PERMITTED_HOSTS, frozenset)


@pytest.mark.parametrize("host", sorted(EXPECTED_HOSTS))
def test_no_permitted_host_is_a_production_endpoint(host: str) -> None:
    """Every entry must be recognisably a test endpoint.

    This is the assertion that would have caught `fstream.binancefuture.com` being
    swapped for `fstream.binance.com` in a hurried edit: the hostnames differ by four
    characters and sit in a block that shows as entirely changed after a reformat.
    """
    assert "testnet" in host or "binancefuture" in host, (
        f"{host} does not look like a testnet endpoint"
    )
    assert not host.endswith(("api.binance.com", "fapi.binance.com", "api.bybit.com"))
