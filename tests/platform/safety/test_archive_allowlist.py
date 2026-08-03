"""The archive allowlist literal, and its disjointness from the trading one.

A golden test for the same reason `test_allowlist.py` is one: widening an allowlist
must break a test, not merely pass review.

The disjointness assertions are the ones specific to this file. `ARCHIVE_HOSTS` and
`PERMITTED_HOSTS` are not two halves of one list -- they are two proofs, and the whole
value of the second egress path evaporates the moment a host appears in both.
"""

from __future__ import annotations

import pytest

from fking.platform.safety import PERMITTED_HOSTS
from fking.platform.safety.archive import ARCHIVE_HOSTS

pytestmark = pytest.mark.unit

EXPECTED_ARCHIVE_HOSTS = frozenset({"data.binance.vision"})


def test_archive_allowlist_is_exactly_the_expected_literal() -> None:
    assert ARCHIVE_HOSTS == EXPECTED_ARCHIVE_HOSTS


def test_archive_allowlist_is_immutable() -> None:
    assert isinstance(ARCHIVE_HOSTS, frozenset)


def test_the_two_allowlists_are_disjoint() -> None:
    """The point of the whole design: neither client can reach the other's hosts."""
    assert ARCHIVE_HOSTS.isdisjoint(PERMITTED_HOSTS), (
        f"a host appears in both allowlists: {sorted(ARCHIVE_HOSTS & PERMITTED_HOSTS)}. "
        f"Two egress paths that overlap are one egress path with extra ceremony"
    )


@pytest.mark.parametrize("host", sorted(EXPECTED_ARCHIVE_HOSTS))
def test_no_archive_host_is_a_trading_endpoint(host: str) -> None:
    """An archive host that can take an order is not an archive host.

    `data.binance.vision` serves static files and has no order endpoint at all, so this
    reads as belt-and-braces today. It is here for the addition that has not happened
    yet -- the one where somebody needs "just the exchangeInfo endpoint" and reaches
    for the list that has no credentials attached, on the grounds that it is the safer
    of the two.
    """
    assert not host.endswith(
        ("api.binance.com", "fapi.binance.com", "api.bybit.com", "binancefuture.com")
    )
    assert "testnet" not in host
