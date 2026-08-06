"""Venue profiles: the numbers, and the invariants that keep them honest.

The assertion that matters most is `cost_model_calibratable`. It is `False` on every
profile in this repository, and the check is written over the whole mapping rather than
over a list somebody maintains, so a fourth venue arriving with the flag set to `True`
fails here rather than three months later inside a backtest whose spread assumption came
from an order book populated by other bots.
"""

from __future__ import annotations

from typing import Final

import pytest

from fking.domain import Venue
from fking.execution import (
    BINANCE_FUTURES_TESTNET,
    BINANCE_SPOT_TESTNET,
    VENUE_PROFILES,
    VenueProfile,
    VenueProfileError,
)
from fking.platform.safety import PERMITTED_HOSTS, assert_host_permitted

pytestmark = pytest.mark.unit

# Verified 2026-08-01 (.claude/contexts/binance-testnet.md, facts 5 and 2). Named so each
# assertion reads as a claim about the venue rather than as a magic number.
SPOT_TESTNET_ORDERS_PER_10S: Final[int] = 50
FUTURES_LISTEN_KEY_KEEPALIVE_SECONDS: Final[int] = 1_800

ALL_PROFILES = pytest.mark.parametrize(
    "profile", sorted(VENUE_PROFILES.values(), key=lambda entry: entry.venue_id), ids=str
)


@ALL_PROFILES
def test_every_testnet_profile_refuses_to_calibrate_a_cost_model(profile: VenueProfile) -> None:
    """Futures testnet measured 7.5bp against production's 0.16bp, with 10x the volume.

    A cost model built from that reads as conservative and is fiction -- pessimistic on
    spread and simultaneously optimistic on fill probability and capacity, because the
    book has no real queue and no adverse selection.
    """
    assert profile.cost_model_calibratable is False


@ALL_PROFILES
def test_every_profile_endpoint_is_in_the_compiled_in_allowlist(profile: VenueProfile) -> None:
    for url in profile.endpoint_urls:
        assert assert_host_permitted(url) in PERMITTED_HOSTS


@ALL_PROFILES
def test_every_profile_is_frozen(profile: VenueProfile) -> None:
    """A mutable profile is a rate limit somebody can raise at runtime."""
    with pytest.raises(ValueError, match="frozen"):
        profile.order_rate_per_10s = 100_000  # type: ignore[misc]


@ALL_PROFILES
def test_the_client_order_id_budget_leaves_room_for_a_digest(profile: VenueProfile) -> None:
    assert len(profile.client_order_id_prefix) < profile.client_order_id_max_len
    assert set(profile.client_order_id_prefix) <= set(profile.client_order_id_charset)


def test_the_spot_order_rate_is_the_testnet_figure_not_the_production_one() -> None:
    """50/10s, not 100/10s.

    Testnet is the tighter constraint and the environment this system runs in. Budgeting
    against the production figure produces -1015 TOO_MANY_ORDERS here, in the order path,
    on exactly the bursts that matter.
    """
    assert BINANCE_SPOT_TESTNET.order_rate_per_10s == SPOT_TESTNET_ORDERS_PER_10S


@ALL_PROFILES
def test_the_drift_budget_is_tighter_than_the_signing_window(profile: VenueProfile) -> None:
    """A drift check that only fires once requests already fail is not a check.

    `recvWindow` is never widened to make drift go away: a wide window does not fix a
    wrong clock, it lets requests signed against one through, and every timestamp the
    audit log then records is wrong by the same amount.
    """
    assert profile.max_clock_drift_ms < profile.recv_window_ms


def test_the_spot_profile_has_no_listen_key_because_spot_has_no_listen_key() -> None:
    """`POST /api/v3/userDataStream` is 410 Gone. Spot authenticates the socket instead."""
    assert BINANCE_SPOT_TESTNET.user_data_mechanism == "session_logon_ed25519"
    assert BINANCE_SPOT_TESTNET.listen_key_keepalive_seconds is None
    assert BINANCE_SPOT_TESTNET.ws_api_url is not None


def test_the_futures_profile_keepalive_leaves_a_whole_missed_beat_of_headroom() -> None:
    """The key expires after 60 minutes without a keepalive; the cadence is 30."""
    assert BINANCE_FUTURES_TESTNET.user_data_mechanism == "listen_key"
    assert (
        BINANCE_FUTURES_TESTNET.listen_key_keepalive_seconds == FUTURES_LISTEN_KEY_KEEPALIVE_SECONDS
    )


def test_a_listen_key_venue_declaring_no_keepalive_is_refused() -> None:
    fields = BINANCE_FUTURES_TESTNET.model_dump()
    fields["listen_key_keepalive_seconds"] = None
    with pytest.raises(VenueProfileError, match="fills stop arriving silently"):
        VenueProfile(**fields)


def test_a_session_logon_venue_declaring_a_keepalive_is_refused() -> None:
    """A keepalive here would refresh a key that does not exist, and would look healthy."""
    fields = BINANCE_SPOT_TESTNET.model_dump()
    fields["listen_key_keepalive_seconds"] = 1_800
    with pytest.raises(VenueProfileError, match="a key that does not exist"):
        VenueProfile(**fields)


def test_a_prefix_outside_the_venue_charset_is_refused() -> None:
    fields = BINANCE_SPOT_TESTNET.model_dump()
    fields["client_order_id_prefix"] = "fk/"
    with pytest.raises(VenueProfileError, match="outside the venue's accepted charset"):
        VenueProfile(**fields)


def test_a_prefix_that_consumes_the_whole_id_is_refused() -> None:
    fields = BINANCE_SPOT_TESTNET.model_dump()
    fields["client_order_id_max_len"] = 3
    with pytest.raises(VenueProfileError, match="no digest would fit"):
        VenueProfile(**fields)


def test_every_venue_the_domain_knows_about_has_a_profile() -> None:
    """A `Venue` with no profile is a venue whose rate limit is whatever the code says."""
    assert set(VENUE_PROFILES) == set(Venue)


def test_a_profile_rejects_an_unknown_field() -> None:
    """`extra="forbid"` on what we author: an unexpected key here is a typo that would
    otherwise be silently ignored, leaving the real field at its default."""
    fields = BINANCE_SPOT_TESTNET.model_dump()
    fields["order_rate_per_10_s"] = 100
    with pytest.raises(ValueError, match="Extra inputs are not permitted"):
        VenueProfile(**fields)
