"""Admission control for one venue's IP budget. It refuses; it never waits.

A limiter that sleeps until the budget clears converts a capacity problem into a latency
problem, and latency in the order path produces fills at prices the decision never saw --
the same class of loss the whole execution stack exists to avoid, arriving through the
mechanism that was supposed to prevent it. So there is no `sleep` in this module and no
coroutine that can suspend: `VenueRateGovernor.admit` either returns or raises
`RateBudgetExhausted`, synchronously, and the caller has to decide. The risk engine is
told a fact about capacity rather than being made to wait for one.

Three budgets, in the order they bind:

1. **A hard stop after HTTP 418.** Binance escalates IP bans from 2 minutes to 3 days and
   every request into a ban extends it, so a ban is terminal for the governor's lifetime.
   There is no timer that clears it, because a timer is a retry loop with better manners.
2. **A `Retry-After` cooldown after HTTP 429.** Honoured exactly, never rounded up into an
   invented backoff schedule. A 429 also means the proactive shedding below failed to fire
   in time, which is a defect in this module's calibration and is logged as one.
3. **The proactive budgets**, which are the only ones that should ever bind in steady
   state: a rolling 10-second order-rate window sized from
   `VenueProfile.order_rate_per_10s`, and a rolling 60-second request-weight window sized
   from `VenueProfile.request_weight_per_minute`, shed by request class so that a backfill
   query is refused long before a reconciliation read and a reconciliation read is refused
   long before an order.

Two deliberate over-estimates, both in the direction that cannot cause a ban:

- The order window is **rolling**, while Binance's is a fixed interval. A rolling window
  refuses some schedules the venue would have accepted; a fixed one accepts some the venue
  would refuse, because our interval boundary is not the venue's.
- Used weight is `max(our own rolling total, the venue's last header plus everything we
  have charged since)`. The header lags by one round trip and counts requests ccxt issued
  internally that we never billed ourselves, so neither number alone is complete.

Nothing here reads the wall clock or the monotonic clock implicitly. Both are injected,
which is what makes a 10-second window testable without a 10-second test.
"""

from __future__ import annotations

import time
from collections import deque
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from enum import StrEnum
from typing import Final

import structlog

from fking.execution._errors import RateBudgetExhausted, VenueIpBannedError
from fking.execution.venue_profile import VenueProfile
from fking.platform.safety import GuardedExchange, VenueResponseMetadata
from fking.platform.telemetry import counter, gauge
from fking.platform.telemetry._registry import (
    EXECUTION_RATE_BUDGET_REFUSALS,
    EXECUTION_REQUEST_WEIGHT_USED,
)

__all__ = [
    "ENDPOINT_COSTS",
    "EndpointCost",
    "RateLimitIncident",
    "RateLimitObservation",
    "RequestClass",
    "ThrottleConfigurationError",
    "ThrottledExchange",
    "VenueRateGovernor",
    "parse_rate_limit_headers",
]

_LOG: Final = structlog.get_logger(__name__)

# Binance meters order submissions over a fixed 10-second interval and request weight over
# a fixed 1-minute one. Both are modelled here as rolling windows of the same width -- see
# the module docstring for why the stricter shape is the correct error direction.
_ORDER_WINDOW_SECONDS: Final[int] = 10
_WEIGHT_WINDOW_SECONDS: Final[int] = 60

# Binance's own name for the per-minute weight consumption header. Lower-cased because
# `VenueResponseMetadata` normalises header names -- the spot and futures fleets do not
# agree on the casing, and a case-sensitive lookup reads "no header" on one of them.
_USED_WEIGHT_HEADER: Final[str] = "x-mbx-used-weight-1m"
_RETRY_AFTER_HEADER: Final[str] = "retry-after"

_TOO_MANY_REQUESTS_STATUS: Final[int] = 429
_IP_BANNED_STATUS: Final[int] = 418


class RequestClass(StrEnum):
    """What a request is for, which is what decides whether it is shed.

    The class is a property of the *caller's intent*, never of the endpoint: the same
    `GET /api/v3/exchangeInfo` is a reconciliation input at startup and a research query
    in a backfill, and shedding it is correct in exactly one of those cases. That is why
    a `ThrottledExchange` is bound to a class at construction, where the intent is known,
    rather than inferring one from the endpoint name.
    """

    ORDER = "order"
    RECONCILIATION = "reconciliation"
    MARKET_DATA = "market_data"
    RESEARCH = "research"
    BACKFILL = "backfill"


# Percent of the venue's per-minute weight budget above which a class stops being
# admitted. 80 for the two discretionary classes is the issue's proactive threshold: by
# the time a 429 arrives it is already too late to be graceful, because the next one
# escalates to a 418. The order path is capped at 100 rather than lower because a
# capacity refusal there means a risk decision went unplaced, which is a worse outcome
# than consuming the last of the budget -- the order rate window still bounds it.
_WEIGHT_CEILING_PERCENT: Final[Mapping[RequestClass, int]] = {
    RequestClass.ORDER: 100,
    RequestClass.RECONCILIATION: 95,
    RequestClass.MARKET_DATA: 90,
    RequestClass.RESEARCH: 80,
    RequestClass.BACKFILL: 80,
}

_PERCENT: Final[int] = 100


class ThrottleConfigurationError(Exception):
    """A programming error at the throttle seam, not a venue failure.

    Two conditions reach it: an endpoint with no declared weight, and an order-path
    endpoint issued through a view that is not the order path. Neither is retryable and
    neither came from a response, which is why this is not an `ExchangeError` -- the same
    reasoning `UnknownVenueEndpointError` carries in the safety kernel.
    """


@dataclass(frozen=True, slots=True)
class EndpointCost:
    """What one call to an endpoint costs against the venue's two budgets."""

    request_weight: int
    consumes_order_slot: bool


# Documented request weights, Binance spot API "Limits", checked 2026-08-11. Where a
# weight varies by argument the *worst* case is billed: `GET /api/v3/openOrders` is 6 with
# a symbol and 80 without, and billing 6 for a call that might cost 80 is how a budget
# that looks healthy produces a 429.
_SPOT_ENDPOINT_COSTS: Final[Mapping[str, EndpointCost]] = {
    "publicGetExchangeInfo": EndpointCost(request_weight=20, consumes_order_slot=False),
    "privateGetAccount": EndpointCost(request_weight=20, consumes_order_slot=False),
    "privateGetOpenOrders": EndpointCost(request_weight=80, consumes_order_slot=False),
    "privateGetMyTrades": EndpointCost(request_weight=20, consumes_order_slot=False),
    "privatePostOrder": EndpointCost(request_weight=1, consumes_order_slot=True),
    # A cancel carries weight but does not consume an order slot: Binance's 10-second
    # counter is `ORDERS`, incremented by placements. Billing a cancel against it would
    # make the emergency path -- cancel everything -- the first thing to be refused.
    "privateDeleteOrder": EndpointCost(request_weight=1, consumes_order_slot=False),
    "privatePostOrderCancelReplace": EndpointCost(request_weight=1, consumes_order_slot=True),
}

# Documented request weights, Binance USD-M futures API "Limits", checked 2026-08-11.
# `POST /fapi/v1/order` is documented at weight 0 and is billed at 1 here: a request that
# costs nothing cannot exist for an IP-metered API, and the discrepancy is small enough
# that over-billing it only sheds research traffic slightly earlier.
_FUTURES_ENDPOINT_COSTS: Final[Mapping[str, EndpointCost]] = {
    "fapiPublicGetExchangeInfo": EndpointCost(request_weight=1, consumes_order_slot=False),
    "fapiPrivateV3GetBalance": EndpointCost(request_weight=5, consumes_order_slot=False),
    "fapiPrivateV3GetPositionRisk": EndpointCost(request_weight=5, consumes_order_slot=False),
    # 40 without a symbol, 1 with one. Worst case, for the reason on the spot table.
    "fapiPrivateGetOpenOrders": EndpointCost(request_weight=40, consumes_order_slot=False),
    "fapiPrivateGetUserTrades": EndpointCost(request_weight=5, consumes_order_slot=False),
    "fapiPrivatePostOrder": EndpointCost(request_weight=1, consumes_order_slot=True),
    "fapiPrivateDeleteOrder": EndpointCost(request_weight=1, consumes_order_slot=False),
    # The futures amend. It replaces a live order's terms, so it establishes a new one
    # against the venue's order counter exactly as spot's cancel-replace does.
    "fapiPrivatePutOrder": EndpointCost(request_weight=1, consumes_order_slot=True),
}

ENDPOINT_COSTS: Final[Mapping[str, Mapping[str, EndpointCost]]] = {
    "spot": _SPOT_ENDPOINT_COSTS,
    "futures": _FUTURES_ENDPOINT_COSTS,
}


@dataclass(frozen=True, slots=True)
class RateLimitObservation:
    """What a response said about the budget, after hostile-input parsing.

    `malformed_headers` exists because "the header was absent" and "the header was
    present and unreadable" are different facts with different safe responses. Absent is
    normal -- an error envelope from a proxy has no `X-MBX-USED-WEIGHT-1M` -- and leaves
    the local estimate governing. Unreadable means the venue is sending something this
    parser does not model, and the governor assumes the budget is fully consumed rather
    than assuming it is fine.
    """

    http_status: int
    used_request_weight_1m: int | None
    retry_after_seconds: int | None
    malformed_headers: tuple[str, ...]


def _parse_non_negative_header(
    headers: Mapping[str, str], name: str, malformed: list[str]
) -> int | None:
    """Read `name` as a non-negative integer, recording it as malformed if it is not."""
    raw = headers.get(name)
    if raw is None:
        return None
    stripped = raw.strip()
    # An explicit ASCII-digit test rather than a try/except around `int()`: `int()`
    # accepts full-width Unicode digits and underscore separators, so a header this
    # parser claimed to understand would not be the number the venue sent.
    if not stripped.isascii() or not stripped.isdigit():
        malformed.append(name)
        return None
    return int(stripped)


def parse_rate_limit_headers(metadata: VenueResponseMetadata) -> RateLimitObservation:
    """Turn one response's transport facts into the two numbers the governor acts on.

    Both headers are optional and both are attacker-shaped in the sense that matters
    here: they arrive as text from a system that has changed its response format before.
    Nothing indexes into them and nothing raises -- a response that cannot be understood
    still has to be delivered to the caller, so the *governor* decides what an
    unreadable header means, and it decides pessimistically.
    """
    malformed: list[str] = []
    return RateLimitObservation(
        http_status=metadata.http_status,
        used_request_weight_1m=_parse_non_negative_header(
            metadata.headers, _USED_WEIGHT_HEADER, malformed
        ),
        # RFC 9110 also permits an HTTP-date here. Binance sends delta-seconds and this
        # parser models only that; a date lands in `malformed_headers`, and the governor's
        # response to an unusable Retry-After on a 429 is the venue's own window width,
        # which is a documented number rather than an invented backoff.
        retry_after_seconds=_parse_non_negative_header(
            metadata.headers, _RETRY_AFTER_HEADER, malformed
        ),
        malformed_headers=tuple(malformed),
    )


@dataclass(frozen=True, slots=True)
class RateLimitIncident:
    """One time the venue told us the throttle was wrong.

    Kept as a record rather than only a log line because "how often did we approach the
    limit" is the measurement that decides whether the calibration is a tuning question
    or a design finding, and a log stream with finite retention cannot answer it months
    later.
    """

    venue_id: str
    http_status: int
    observed_at_utc: datetime
    retry_after_seconds: int | None
    used_request_weight_1m: int | None
    reason: str


MonotonicSeconds = Callable[[], float]
"""An elapsed-time source in seconds. `time.monotonic` in production, a counter in tests.

Never a wall clock: an NTP step during a 10-second window would move the window's edge
and either admit an order the venue refuses or refuse one it would have taken.
"""

Clock = Callable[[], datetime]
"""The wall clock, injected. Used only to stamp incidents, never to measure elapsed time."""


def _system_now_utc() -> datetime:
    return datetime.now(UTC)


class VenueRateGovernor:
    """The admission decision for one venue's IP budget, shared by every caller on it.

    One instance per venue per process, because the budget Binance meters is per IP: two
    governors on the same host each believe they own the whole budget and together spend
    twice it, which is a 429 arriving at exactly the moment both are busy.
    """

    def __init__(
        self,
        *,
        profile: VenueProfile,
        monotonic_seconds: MonotonicSeconds = time.monotonic,
        clock: Clock = _system_now_utc,
    ) -> None:
        self._profile = profile
        self._monotonic_seconds = monotonic_seconds
        self._clock = clock
        self._order_slots_used_at: deque[float] = deque()
        self._weight_charged: deque[tuple[float, int]] = deque()
        self._observed_used_weight: int | None = None
        self._observed_at_seconds: float | None = None
        self._refuse_until_seconds: float | None = None
        self._banned = False
        self._incidents: list[RateLimitIncident] = []
        self._refusals = counter(EXECUTION_RATE_BUDGET_REFUSALS)
        self._weight_used = gauge(EXECUTION_REQUEST_WEIGHT_USED)

    @property
    def profile(self) -> VenueProfile:
        return self._profile

    @property
    def is_banned(self) -> bool:
        """`True` once a 418 has been seen. Never returns to `False`."""
        return self._banned

    @property
    def incidents(self) -> tuple[RateLimitIncident, ...]:
        """Every 429 and 418 this governor has seen, oldest first. Append-only."""
        return tuple(self._incidents)

    def used_request_weight(self) -> int:
        """The best available estimate of weight consumed in the trailing minute."""
        now_seconds = self._monotonic_seconds()
        self._expire(now_seconds)
        local_total = sum(charge for _, charge in self._weight_charged)
        if self._observed_used_weight is None or self._observed_at_seconds is None:
            return local_total
        # `>=` rather than `>`: a charge whose reading equals the observation's cannot be
        # known to be included in it, and on a coarse monotonic clock that tie is common.
        # Counting it twice over-states the total by one request's weight, which sheds
        # discretionary traffic a little early -- the direction that cannot end in a ban.
        charged_since_observation = sum(
            charge
            for charged_at, charge in self._weight_charged
            if charged_at >= self._observed_at_seconds
        )
        return max(local_total, self._observed_used_weight + charged_since_observation)

    def orders_in_window(self) -> int:
        """Order slots consumed in the trailing `_ORDER_WINDOW_SECONDS`."""
        now_seconds = self._monotonic_seconds()
        self._expire(now_seconds)
        return len(self._order_slots_used_at)

    def admit(self, *, request_class: RequestClass, endpoint: str, cost: EndpointCost) -> None:
        """Grant admission for one request, or refuse it. Returns immediately either way.

        Charges the budgets on the way out rather than after the response, because the
        venue charges when it receives the request and a governor that billed on
        completion would under-count exactly while a burst was in flight.
        """
        if self._banned:
            raise VenueIpBannedError(
                f"{self._profile.venue_id} returned HTTP {_IP_BANNED_STATUS} and this "
                f"governor is stopped; retrying into a ban extends it, so recovery is an "
                f"operator action after the cause is understood, not a timer",
                venue_id=str(self._profile.venue_id),
            )

        now_seconds = self._monotonic_seconds()
        self._expire(now_seconds)
        self._refuse_if_cooling_down(request_class=request_class, now_seconds=now_seconds)
        if cost.consumes_order_slot:
            self._refuse_if_order_rate_exhausted(
                request_class=request_class, endpoint=endpoint, now_seconds=now_seconds
            )
        self._refuse_if_weight_exhausted(
            request_class=request_class, endpoint=endpoint, cost=cost, now_seconds=now_seconds
        )

        if cost.consumes_order_slot:
            self._order_slots_used_at.append(now_seconds)
        self._weight_charged.append((now_seconds, cost.request_weight))
        self._publish_weight_utilisation()

    def observe(self, metadata: VenueResponseMetadata) -> None:
        """Reconcile the governor against what the venue just said.

        Called after every response, including failed ones: a 429 is only visible here,
        and a governor that only saw successes would learn nothing from the responses
        that matter most.
        """
        observation = parse_rate_limit_headers(metadata)
        now_seconds = self._monotonic_seconds()

        if _USED_WEIGHT_HEADER in observation.malformed_headers:
            self._assume_budget_spent(now_seconds)
        elif observation.used_request_weight_1m is not None:
            self._observed_used_weight = observation.used_request_weight_1m
            self._observed_at_seconds = now_seconds
            self._publish_weight_utilisation()

        if observation.http_status == _IP_BANNED_STATUS:
            self._record_ban(observation)
        elif observation.http_status == _TOO_MANY_REQUESTS_STATUS:
            self._record_throttle_breach(observation, now_seconds=now_seconds)

    def _publish_weight_utilisation(self) -> None:
        """Emit consumption as a fraction of the venue's budget, where it publishes one.

        Silent for a venue with no declared budget rather than emitting a zero: a flat
        zero on a dashboard reads as "plenty of headroom", which is a stronger claim than
        "this venue does not report weight" and the wrong one to act on.
        """
        ceiling = self._profile.request_weight_per_minute
        if ceiling is None:
            return
        self._weight_used.set(
            self.used_request_weight() / ceiling, venue=str(self._profile.venue_id)
        )

    def _assume_budget_spent(self, now_seconds: float) -> None:
        """Treat the whole per-minute budget as consumed when the header is unreadable.

        The safe direction: it sheds discretionary traffic immediately and leaves the
        order path admitted, which is the same ordering the proactive thresholds impose.
        Assuming the budget is *fine* would be the reading that ends in a ban.
        """
        _LOG.error(
            "ratelimit.header_unparseable",
            venue=str(self._profile.venue_id),
            reason=_USED_WEIGHT_HEADER,
            outcome="assumed_budget_spent",
        )
        ceiling = self._profile.request_weight_per_minute
        if ceiling is None:
            return
        self._observed_used_weight = ceiling
        self._observed_at_seconds = now_seconds

    def _record_ban(self, observation: RateLimitObservation) -> None:
        self._banned = True
        self._incidents.append(
            RateLimitIncident(
                venue_id=str(self._profile.venue_id),
                http_status=observation.http_status,
                observed_at_utc=self._clock(),
                retry_after_seconds=observation.retry_after_seconds,
                used_request_weight_1m=observation.used_request_weight_1m,
                reason="ip_banned",
            )
        )
        # CRITICAL because trading must stop: the venue has withdrawn access for a period
        # that escalates to three days if anything keeps asking, and there is no degraded
        # mode that trades without a venue.
        _LOG.critical(
            "ratelimit.ip_banned",
            venue=str(self._profile.venue_id),
            http_status=observation.http_status,
            retry_after_seconds=observation.retry_after_seconds,
            reason="ip_banned",
        )
        self._refusals.increment(
            venue=str(self._profile.venue_id), request_class="all", reason="ip_banned"
        )

    def _record_throttle_breach(
        self, observation: RateLimitObservation, *, now_seconds: float
    ) -> None:
        # No Retry-After we can read means we cannot honour one, and inventing a schedule
        # is what turns a 429 into a 418. The venue's own weight window is the smallest
        # interval after which the counter it refused on is guaranteed to have reset.
        honoured_seconds = observation.retry_after_seconds
        reason = "retry_after" if honoured_seconds is not None else "no_retry_after"
        cooldown_seconds = (
            honoured_seconds if honoured_seconds is not None else _WEIGHT_WINDOW_SECONDS
        )
        self._refuse_until_seconds = now_seconds + cooldown_seconds
        self._incidents.append(
            RateLimitIncident(
                venue_id=str(self._profile.venue_id),
                http_status=observation.http_status,
                observed_at_utc=self._clock(),
                retry_after_seconds=honoured_seconds,
                used_request_weight_1m=observation.used_request_weight_1m,
                reason=reason,
            )
        )
        # ERROR, not WARNING: a 429 means the proactive thresholds did not fire in time,
        # which is a calibration defect in this module and a page-worthy one -- the next
        # 429 escalates to an IP ban.
        _LOG.error(
            "ratelimit.throttle_breached",
            venue=str(self._profile.venue_id),
            http_status=observation.http_status,
            retry_after_seconds=honoured_seconds,
            used_request_weight=observation.used_request_weight_1m,
            reason=reason,
        )

    def _expire(self, now_seconds: float) -> None:
        """Drop window entries the venue has already forgotten."""
        order_floor = now_seconds - _ORDER_WINDOW_SECONDS
        while self._order_slots_used_at and self._order_slots_used_at[0] <= order_floor:
            self._order_slots_used_at.popleft()
        weight_floor = now_seconds - _WEIGHT_WINDOW_SECONDS
        while self._weight_charged and self._weight_charged[0][0] <= weight_floor:
            self._weight_charged.popleft()
        if self._observed_at_seconds is not None and self._observed_at_seconds <= weight_floor:
            self._observed_used_weight = None
            self._observed_at_seconds = None

    def _refuse(
        self, *, request_class: RequestClass, reason: str, message: str, free_in_seconds: float
    ) -> None:
        self._refusals.increment(
            venue=str(self._profile.venue_id), request_class=str(request_class), reason=reason
        )
        # WARNING for a shed discretionary request -- degraded but correct, the system did
        # the right thing on a worse path. ERROR when the order path is refused, because
        # then a risk decision was not established at the venue, which is the definition
        # of "a decision was not made".
        fields: dict[str, str | float] = {
            "venue": str(self._profile.venue_id),
            "request_class": str(request_class),
            "reason": reason,
            "budget_free_in_seconds": round(free_in_seconds, 3),
        }
        if request_class is RequestClass.ORDER:
            _LOG.error("ratelimit.order_refused", **fields)
        else:
            _LOG.warning("ratelimit.shed", **fields)
        raise RateBudgetExhausted(
            message,
            venue_id=str(self._profile.venue_id),
            request_class=str(request_class),
            budget_free_in_seconds=free_in_seconds,
        )

    def _refuse_if_cooling_down(self, *, request_class: RequestClass, now_seconds: float) -> None:
        if self._refuse_until_seconds is None:
            return
        if now_seconds >= self._refuse_until_seconds:
            self._refuse_until_seconds = None
            return
        self._refuse(
            request_class=request_class,
            reason="retry_after_cooldown",
            message=(
                f"{self._profile.venue_id} returned {_TOO_MANY_REQUESTS_STATUS} and asked "
                f"for a cooldown that has not elapsed; admitting anything now is the "
                f"request that escalates the refusal into an IP ban"
            ),
            free_in_seconds=self._refuse_until_seconds - now_seconds,
        )

    def _refuse_if_order_rate_exhausted(
        self, *, request_class: RequestClass, endpoint: str, now_seconds: float
    ) -> None:
        ceiling = self._profile.order_rate_per_10s
        if len(self._order_slots_used_at) < ceiling:
            return
        oldest_seconds = self._order_slots_used_at[0]
        self._refuse(
            request_class=request_class,
            reason="order_rate",
            message=(
                f"{self._profile.venue_id} allows {ceiling} orders per "
                f"{_ORDER_WINDOW_SECONDS}s and {len(self._order_slots_used_at)} are "
                f"already in the window; {endpoint} was not sent, so the intended "
                f"position was not established and must be treated as unplaced"
            ),
            free_in_seconds=oldest_seconds + _ORDER_WINDOW_SECONDS - now_seconds,
        )

    def _refuse_if_weight_exhausted(
        self,
        *,
        request_class: RequestClass,
        endpoint: str,
        cost: EndpointCost,
        now_seconds: float,
    ) -> None:
        ceiling = self._profile.request_weight_per_minute
        if ceiling is None:
            # The venue publishes no weight budget and sends no used-weight header. There
            # is nothing to shed against, and shedding against a number we made up would
            # refuse correct requests for a reason that does not exist.
            return
        shed_above = ceiling * _WEIGHT_CEILING_PERCENT[request_class] // _PERCENT
        projected = self.used_request_weight() + cost.request_weight
        if projected <= shed_above:
            return
        oldest_seconds = (
            self._weight_charged[0][0] if self._weight_charged else self._observed_at_seconds
        )
        free_in_seconds = (
            oldest_seconds + _WEIGHT_WINDOW_SECONDS - now_seconds
            if oldest_seconds is not None
            else _WEIGHT_WINDOW_SECONDS
        )
        self._refuse(
            request_class=request_class,
            reason="request_weight",
            message=(
                f"{self._profile.venue_id} request weight would reach {projected} against "
                f"a {shed_above} ceiling for {request_class}; {endpoint} was shed so that "
                f"order state and reconciliation keep the remaining budget"
            ),
            free_in_seconds=free_in_seconds,
        )


class ThrottledExchange:
    """A `GuardedExchange` view that consults a governor before every call.

    A view rather than a replacement: several of these share one `VenueRateGovernor`,
    each bound to the class of work its owner does, which is how a backfill and the order
    path draw on the same per-IP budget while being shed in the right order.

    It implements `GuardedExchange` so that `BinanceVenue` is constructed with one and
    changes not at all -- the adapter has no idea whether it is throttled, which is what
    keeps the throttle from becoming something a new method can forget to call.
    """

    def __init__(
        self,
        inner: GuardedExchange,
        governor: VenueRateGovernor,
        *,
        request_class: RequestClass,
    ) -> None:
        self._inner = inner
        self._governor = governor
        self._request_class = request_class
        self._costs = ENDPOINT_COSTS[governor.profile.market]

    @property
    def venue_id(self) -> str:
        return self._inner.venue_id

    @property
    def request_count(self) -> int:
        return self._inner.request_count

    @property
    def last_response_metadata(self) -> VenueResponseMetadata | None:
        return self._inner.last_response_metadata

    @property
    def request_class(self) -> RequestClass:
        return self._request_class

    def _cost_of(self, endpoint: str) -> EndpointCost:
        cost = self._costs.get(endpoint)
        if cost is None:
            raise ThrottleConfigurationError(
                f"{self.venue_id}: {endpoint} has no declared request weight, so admitting "
                f"it would spend an unknown amount of the venue's budget. Add it to "
                f"ENDPOINT_COSTS with the weight the venue documents"
            )
        if cost.consumes_order_slot and self._request_class is not RequestClass.ORDER:
            raise ThrottleConfigurationError(
                f"{self.venue_id}: {endpoint} places an order but was issued through a "
                f"{self._request_class} view, which is shed before the order path; an "
                f"order must never be droppable as low-priority traffic"
            )
        return cost

    async def call(self, endpoint: str, params: Mapping[str, str]) -> str:
        """Admit, then delegate, then reconcile the governor against the response.

        The admission check is synchronous and precedes the `await`, so a refusal costs
        no I/O and no scheduling latency -- which is the property that makes rejecting
        strictly better than waiting.
        """
        self._governor.admit(
            request_class=self._request_class, endpoint=endpoint, cost=self._cost_of(endpoint)
        )
        try:
            return await self._inner.call(endpoint, params)
        finally:
            # In `finally` because a 429 and a 418 both arrive as failures on this path,
            # and those are precisely the responses the governor must not miss.
            metadata = self._inner.last_response_metadata
            if metadata is not None:
                self._governor.observe(metadata)

    async def aclose(self) -> None:
        await self._inner.aclose()
