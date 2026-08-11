"""Every number that differs between spot, futures and the fallback venue, as data.

A constant written inline in a throttle is a testnet fact that will one day govern a
non-testnet decision, and nothing in the diff that moves it will look like a risk
change. `50` in a rate limiter reads as an implementation detail; `order_rate_per_10s`
on a named profile reads as what it is -- the measured spot-testnet budget, which is
*tighter* than production's 100/10s and is therefore the binding constraint for anything
validated here (.claude/contexts/binance-testnet.md, fact 5).

Two fields carry more than their type suggests:

- `cost_model_calibratable` is `False` on every testnet profile and is the mechanism
  behind the "never calibrate on testnet" non-negotiable. Futures testnet measured a
  7.5bp spread against production's 0.16bp with roughly 10x inflated volume -- a factor
  of 47 in one direction and an order of magnitude in the other. A cost model built from
  that looks conservative and is fiction.
- `max_clock_drift_ms` pairs with `recv_window_ms` in a drift check on connect and on
  the reconcile timer. **You never widen `recvWindow` to make drift go away.** A wide
  window does not fix a wrong clock; it lets requests signed against a wrong clock
  through, and every timestamp then written to the audit log is wrong by the same amount.

Host validation happens at profile construction, against the compiled-in allowlist. A
profile naming a production host cannot be constructed, so a misconfiguration is a
`SafetyViolation` at import rather than a discovery on the first order.
"""

from __future__ import annotations

from typing import Final, Literal, Self

from pydantic import BaseModel, ConfigDict, Field, model_validator

from fking.domain import Venue
from fking.execution._errors import VenueProfileError
from fking.platform.safety import assert_host_permitted

__all__ = [
    "BINANCE_FUTURES_TESTNET",
    "BINANCE_SPOT_TESTNET",
    "BYBIT_TESTNET",
    "VENUE_PROFILES",
    "UserDataMechanism",
    "VenueProfile",
]

# Two genuinely different mechanisms, not one with a flag. Spot binds authentication to
# the *socket* via an Ed25519 `session.logon` -- `POST /api/v3/userDataStream` returns
# 410 Gone on testnet and production alike -- so the session dies with the connection and
# a reconnect re-authenticates. Futures' `listenKey` is a server-side resource that
# outlives the socket and dies of a missed keepalive instead. Those are opposite
# lifetimes, and a single implementation with a branch gets one of them wrong.
UserDataMechanism = Literal["session_logon_ed25519", "listen_key"]


class VenueProfile(BaseModel):
    """Frozen configuration for one venue. Data, never code."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    # The domain's `Venue` rather than a Literal of the same three strings: two
    # vocabularies for one concept diverge the day a fourth venue is added to only one
    # of them, and `Order.instrument.venue` already speaks this one.
    venue_id: Venue
    # The ccxt exchange id, which is the venue *family* rather than the environment:
    # spot and futures testnet are both `binance`, separated by `market`.
    ccxt_exchange_id: Literal["binance", "bybit"]
    market: Literal["spot", "futures"]

    rest_url: str
    # None where the venue has no separate WebSocket API endpoint. Spot has one because
    # `session.logon` is issued there; futures authenticates over REST and streams over
    # the stream host, so it has nothing to put here.
    ws_api_url: str | None
    ws_stream_url: str

    # The unit the venue stamps on *live* responses. Unrelated to the archive split in
    # ADR-0013, and conflating the two produces a -1021 that reads like clock drift.
    timestamp_unit: Literal["ms", "us"]

    order_rate_per_10s: int = Field(gt=0)
    # `None` where the venue publishes no request-weight budget and sends no used-weight
    # header. That is Bybit: v5 meters per endpoint and per UID, so a weight number here
    # would be invented, and a throttle told to shed against an invented limit sheds
    # against nothing. Absent is the honest encoding; a placeholder is not.
    request_weight_per_minute: int | None = Field(default=None, gt=0)
    recv_window_ms: int = Field(gt=0, le=60_000)
    max_clock_drift_ms: int = Field(gt=0)

    user_data_mechanism: UserDataMechanism
    listen_key_keepalive_seconds: int | None = Field(default=None, gt=0)

    client_order_id_prefix: str = Field(min_length=1)
    client_order_id_charset: str = Field(min_length=1)
    client_order_id_max_len: int = Field(gt=0)

    reconcile_interval_seconds: int = Field(gt=0)
    request_timeout_ms: int = Field(gt=0)

    cost_model_calibratable: bool

    @property
    def endpoint_urls(self) -> tuple[str, ...]:
        """Every URL this profile would have the process connect to."""
        candidates = (self.rest_url, self.ws_api_url, self.ws_stream_url)
        return tuple(url for url in candidates if url is not None)

    @model_validator(mode="after")
    def _hosts_are_allowlisted(self) -> Self:
        for url in self.endpoint_urls:
            assert_host_permitted(url)
        return self

    @model_validator(mode="after")
    def _keepalive_matches_the_user_data_mechanism(self) -> Self:
        needs_keepalive = self.user_data_mechanism == "listen_key"
        if needs_keepalive and self.listen_key_keepalive_seconds is None:
            raise VenueProfileError(
                f"{self.venue_id} authenticates with a listenKey but declares no "
                f"keepalive interval; the key expires and fills stop arriving silently"
            )
        if not needs_keepalive and self.listen_key_keepalive_seconds is not None:
            raise VenueProfileError(
                f"{self.venue_id} authenticates the socket itself but declares a "
                f"listenKey keepalive of {self.listen_key_keepalive_seconds}s; a "
                f"keepalive here would refresh a key that does not exist"
            )
        return self

    @model_validator(mode="after")
    def _client_order_id_prefix_fits_the_venue(self) -> Self:
        outside = sorted(set(self.client_order_id_prefix) - set(self.client_order_id_charset))
        if outside:
            raise VenueProfileError(
                f"{self.venue_id} client_order_id_prefix contains {outside}, which is "
                f"outside the venue's accepted charset"
            )
        # The prefix plus at least one digest character has to fit, or every id the
        # venue accepts is the prefix and nothing else -- which is one id, not a key.
        if len(self.client_order_id_prefix) >= self.client_order_id_max_len:
            raise VenueProfileError(
                f"{self.venue_id} client_order_id_prefix is "
                f"{len(self.client_order_id_prefix)} characters and the venue accepts "
                f"{self.client_order_id_max_len}; no digest would fit"
            )
        return self


# Binance accepts `.` and `_` in newClientOrderId alongside alphanumerics, and caps it
# at 36 characters. The prefix is how a reconciliation pass tells an order this system
# placed from one a human placed in the testnet UI -- which happens, and which must not
# be mistaken for a divergence.
_BINANCE_CLIENT_ORDER_ID_CHARSET: Final[str] = (
    "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789.-_"
)

BINANCE_SPOT_TESTNET: Final = VenueProfile(
    venue_id=Venue.BINANCE_SPOT_TESTNET,
    ccxt_exchange_id="binance",
    market="spot",
    rest_url="https://testnet.binance.vision",
    ws_api_url="wss://ws-api.testnet.binance.vision/ws-api/v3",
    ws_stream_url="wss://stream.testnet.binance.vision/stream",
    timestamp_unit="ms",
    # 50, not production's 100. Testnet is the tighter constraint and it is the
    # environment this system actually runs in; sizing a burst against the production
    # figure produces -1015 TOO_MANY_ORDERS here. Verified 2026-08-01.
    order_rate_per_10s=50,
    # Binance spot meters request weight per IP per minute and reports consumption in
    # `X-MBX-USED-WEIGHT-1M`. 6000 is the documented spot ceiling and testnet answers
    # with the same header. Source: Binance spot API "Limits" section, checked
    # 2026-08-11; also .claude/contexts/binance-testnet.md fact table row
    # "Request weight per minute".
    request_weight_per_minute=6_000,
    recv_window_ms=5_000,
    # Deliberately well below recv_window_ms: drift is a hard stop before it becomes a
    # -1021 in the order path, and a margin that equals the window gives no warning.
    max_clock_drift_ms=1_000,
    user_data_mechanism="session_logon_ed25519",
    listen_key_keepalive_seconds=None,
    client_order_id_prefix="fk-",
    client_order_id_charset=_BINANCE_CLIENT_ORDER_ID_CHARSET,
    client_order_id_max_len=36,
    # Spot testnet is wiped roughly every 30 days without notice -- keys survive,
    # balances and open orders do not -- so reconciliation is a timer, not an error path.
    reconcile_interval_seconds=300,
    request_timeout_ms=10_000,
    cost_model_calibratable=False,
)

BINANCE_FUTURES_TESTNET: Final = VenueProfile(
    venue_id=Venue.BINANCE_FUTURES_TESTNET,
    ccxt_exchange_id="binance",
    market="futures",
    rest_url="https://testnet.binancefuture.com",
    ws_api_url=None,
    ws_stream_url="wss://stream.binancefuture.com/ws",
    timestamp_unit="ms",
    order_rate_per_10s=50,
    # USD-M futures meters a *smaller* per-minute weight budget than spot and reports it
    # in `X-MBX-USED-WEIGHT-1M` on every response. Source: Binance USD-M futures API
    # "Limits" section, checked 2026-08-11.
    request_weight_per_minute=2_400,
    recv_window_ms=5_000,
    max_clock_drift_ms=1_000,
    user_data_mechanism="listen_key",
    # The key expires after 60 minutes without a keepalive; 30 minutes is Binance's own
    # documented cadence and leaves a whole missed keepalive of headroom.
    listen_key_keepalive_seconds=1_800,
    client_order_id_prefix="fk-",
    client_order_id_charset=_BINANCE_CLIENT_ORDER_ID_CHARSET,
    client_order_id_max_len=36,
    reconcile_interval_seconds=300,
    request_timeout_ms=10_000,
    cost_model_calibratable=False,
)

# The fallback venue (ADR-0007). Its adapter arrives with #112 -- what exists here is the
# profile, because "testnet remains available and free" is a standing assumption and an
# assumption with no fallback is a single point of failure for the whole project. None of
# the Binance facts transfer: symbol naming, filter semantics, error codes and rate
# limits are Bybit's.
BYBIT_TESTNET: Final = VenueProfile(
    venue_id=Venue.BYBIT_TESTNET,
    ccxt_exchange_id="bybit",
    market="futures",
    rest_url="https://api-testnet.bybit.com",
    ws_api_url=None,
    ws_stream_url="wss://stream-testnet.bybit.com/v5/public/linear",
    timestamp_unit="ms",
    order_rate_per_10s=50,
    recv_window_ms=5_000,
    max_clock_drift_ms=1_000,
    user_data_mechanism="listen_key",
    listen_key_keepalive_seconds=1_800,
    client_order_id_prefix="fk-",
    # Bybit's orderLinkId accepts alphanumerics, `-` and `_`, and caps at 36.
    client_order_id_charset="abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789-_",
    client_order_id_max_len=36,
    reconcile_interval_seconds=300,
    request_timeout_ms=10_000,
    cost_model_calibratable=False,
)

VENUE_PROFILES: Final[dict[Venue, VenueProfile]] = {
    profile.venue_id: profile
    for profile in (BINANCE_SPOT_TESTNET, BINANCE_FUTURES_TESTNET, BYBIT_TESTNET)
}
