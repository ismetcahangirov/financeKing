"""Admission control, driven with an injected clock and recorded venue responses.

Every test here moves time by calling `FrozenClock.advance`, never by waiting. That is
not only speed: a limiter that passed these tests by sleeping would take eleven minutes
to run them, and the whole design claim of this module is that no caller ever waits.
`test_exhaustion_does_not_sleep` asserts the claim directly, on wall time.

The `429` and `418` responses are replayed from the corpus under
`tests/fixtures/recorded/`, where both are marked `# SYNTHETIC:` with the reason: they
cannot be captured for real without deliberately provoking an IP ban of up to three days
on an address shared by every developer and CI runner.
"""

from __future__ import annotations

import time
from collections.abc import Mapping
from typing import Final

import pytest
import structlog

from fking.domain import Venue
from fking.execution import (
    BINANCE_SPOT_TESTNET,
    BYBIT_TESTNET,
    VENUE_PROFILES,
    RateBudgetExhausted,
    RequestClass,
    ThrottleConfigurationError,
    ThrottledExchange,
    VenueIpBannedError,
    VenueRateGovernor,
    parse_rate_limit_headers,
)
from fking.execution.binance import _ENDPOINTS
from fking.execution.throttle import ENDPOINT_COSTS, EndpointCost
from fking.platform.safety import VenueResponseMetadata
from tests.execution.conftest import RecordedExchange, load_recording
from tests.support.frozen_clock import FrozenClock

pytestmark = pytest.mark.unit

# The spot order endpoint: one order slot, weight 1. Named so a reader does not have to
# reverse the table to know why a test that submits 50 of them is at the limit.
_SUBMIT_ENDPOINT: Final[str] = "privatePostOrder"
# Weight 20 on spot, and the endpoint the governor-level tests bill: a read whose
# declared cost is stated in one place keeps the arithmetic in these tests readable.
_MY_TRADES_ENDPOINT: Final[str] = "privateGetMyTrades"
# Weight 80 on spot -- the worst-case openOrders reading, used where a test drives
# `ThrottledExchange` and therefore bills the table rather than a literal.
_OPEN_ORDERS_ENDPOINT: Final[str] = "privateGetOpenOrders"

_ORDER_COST: Final[EndpointCost] = EndpointCost(request_weight=1, consumes_order_slot=True)
_READ_COST: Final[EndpointCost] = EndpointCost(request_weight=20, consumes_order_slot=False)

# The issue's shedding scenario: 85% of the spot per-minute budget already consumed.
_EIGHTY_FIVE_PERCENT_USED: Final[int] = 5_100

# `Retry-After: 300` is the value carried by the recorded 429.
_RECORDED_RETRY_AFTER_SECONDS: Final[int] = 300

# The issue's ceiling for a rejected call's wall time. Generous by two orders of
# magnitude against any interval a sleeping implementation would choose, and still
# far below what a loaded CI runner adds to a few hundred arithmetic operations.
_REJECTION_BUDGET_SECONDS: Final[float] = 0.005

# Enough readings to discard one-time initialisation and still have a population to take
# a minimum over. Both are small because a warm refusal is microseconds.
_TIMING_ATTEMPTS: Final[int] = 12
_TIMING_WARMUPS: Final[int] = 2

# 1000 from the header plus one 20-weight read charged after it.
_OBSERVED_PLUS_LOCAL_WEIGHT: Final[int] = 1_020
_HTTP_OK: Final[int] = 200

# An arbitrary readable weight, small enough that no ceiling is anywhere near it.
_SAMPLE_WEIGHT_READING: Final[int] = 42

# U+FF11 U+FF12, the full-width digits for "12". Built from code points rather than
# written out so this source file stays ASCII: `int()` accepts these and returns 12,
# which is not the number the header would have been claiming.
_FULL_WIDTH_TWELVE: Final[str] = chr(0xFF11) + chr(0xFF12)


def _governor(
    clock: FrozenClock, *, venue: Venue = Venue.BINANCE_SPOT_TESTNET
) -> VenueRateGovernor:
    return VenueRateGovernor(
        profile=VENUE_PROFILES[venue],
        monotonic_seconds=clock.monotonic_seconds,
        clock=clock.now_utc,
    )


def _metadata(http_status: int, headers: Mapping[str, str]) -> VenueResponseMetadata:
    return VenueResponseMetadata.of(http_status=http_status, headers=headers)


def _fill_the_order_window(governor: VenueRateGovernor) -> None:
    """Consume every order slot the profile allows, without exceeding it."""
    for _ in range(BINANCE_SPOT_TESTNET.order_rate_per_10s):
        governor.admit(
            request_class=RequestClass.ORDER, endpoint=_SUBMIT_ENDPOINT, cost=_ORDER_COST
        )


class TestTheOrderRateBudget:
    def test_the_limit_is_the_profile_s_and_is_never_exceeded(self) -> None:
        clock = FrozenClock()
        governor = _governor(clock)

        _fill_the_order_window(governor)

        assert governor.orders_in_window() == BINANCE_SPOT_TESTNET.order_rate_per_10s
        with pytest.raises(RateBudgetExhausted) as refused:
            governor.admit(
                request_class=RequestClass.ORDER, endpoint=_SUBMIT_ENDPOINT, cost=_ORDER_COST
            )
        assert refused.value.request_class == RequestClass.ORDER
        assert refused.value.venue_id == str(Venue.BINANCE_SPOT_TESTNET)

    def test_exhaustion_does_not_sleep(self) -> None:
        """A refusal costs no wall time, which is the whole design claim.

        Measured on `time.perf_counter` rather than asserted by reading the source: a
        limiter that awaited an `asyncio.sleep` would satisfy a source grep for `sleep`
        and still block the order path.

        The *fastest* of several refusals rather than one reading, and only after a
        warm-up. Under `--cov` the first refusal pays for structlog's and the metric
        SDK's one-time initialisation with line tracing on, which measured 9.9ms on this
        machine against roughly 20 microseconds once warm -- and a test that fails when
        coverage is enabled is a test people learn to run without coverage. Timing noise
        is one-sided: it only ever adds, so a minimum is the reading that describes the
        code rather than the machine, and a sleep would raise the minimum too.
        """
        clock = FrozenClock()
        governor = _governor(clock)
        _fill_the_order_window(governor)

        readings_seconds: list[float] = []
        for _ in range(_TIMING_ATTEMPTS):
            started_seconds = time.perf_counter()
            with pytest.raises(RateBudgetExhausted):
                governor.admit(
                    request_class=RequestClass.ORDER, endpoint=_SUBMIT_ENDPOINT, cost=_ORDER_COST
                )
            readings_seconds.append(time.perf_counter() - started_seconds)

        assert min(readings_seconds[_TIMING_WARMUPS:]) < _REJECTION_BUDGET_SECONDS
        # And the clock did not move, so nothing in the refusal path waited on time.
        assert clock.monotonic_seconds() == 0.0

    def test_a_slot_returns_once_the_window_has_rolled_past_it(self) -> None:
        clock = FrozenClock()
        governor = _governor(clock)
        _fill_the_order_window(governor)

        clock.advance(9.999)
        with pytest.raises(RateBudgetExhausted) as refused:
            governor.admit(
                request_class=RequestClass.ORDER, endpoint=_SUBMIT_ENDPOINT, cost=_ORDER_COST
            )
        assert refused.value.budget_free_in_seconds == pytest.approx(0.001)

        clock.advance(0.002)
        governor.admit(
            request_class=RequestClass.ORDER, endpoint=_SUBMIT_ENDPOINT, cost=_ORDER_COST
        )
        assert governor.orders_in_window() == 1

    def test_a_cancel_does_not_consume_an_order_slot(self) -> None:
        """The emergency path must not be the first thing the throttle refuses."""
        clock = FrozenClock()
        governor = _governor(clock)
        _fill_the_order_window(governor)

        cancel_cost = ENDPOINT_COSTS["spot"]["privateDeleteOrder"]
        assert cancel_cost.consumes_order_slot is False
        governor.admit(
            request_class=RequestClass.ORDER, endpoint="privateDeleteOrder", cost=cancel_cost
        )


class TestShedding:
    def test_shedding_admits_reconciliation_and_refuses_backfill_at_85_percent(self) -> None:
        """The ordering the issue specifies, at the utilisation it specifies.

        Two governors rather than one, because a refusal must not depend on what was
        asked first: shedding is a property of the class, not of arrival order.
        """
        headers = {"x-mbx-used-weight-1m": str(_EIGHTY_FIVE_PERCENT_USED)}

        reconciler_clock = FrozenClock()
        reconciler = _governor(reconciler_clock)
        reconciler.observe(_metadata(_HTTP_OK, headers))
        reconciler.admit(
            request_class=RequestClass.RECONCILIATION,
            endpoint=_MY_TRADES_ENDPOINT,
            cost=_READ_COST,
        )

        backfill_clock = FrozenClock()
        backfill = _governor(backfill_clock)
        backfill.observe(_metadata(_HTTP_OK, headers))
        with pytest.raises(RateBudgetExhausted) as refused:
            backfill.admit(
                request_class=RequestClass.BACKFILL,
                endpoint=_MY_TRADES_ENDPOINT,
                cost=_READ_COST,
            )
        assert refused.value.request_class == RequestClass.BACKFILL

    def test_the_order_path_survives_a_utilisation_that_sheds_everything_else(self) -> None:
        clock = FrozenClock()
        governor = _governor(clock)
        # 99% consumed: above the reconciliation ceiling as well as the discretionary one.
        governor.observe(_metadata(_HTTP_OK, {"x-mbx-used-weight-1m": "5940"}))

        with pytest.raises(RateBudgetExhausted):
            governor.admit(
                request_class=RequestClass.RECONCILIATION,
                endpoint=_MY_TRADES_ENDPOINT,
                cost=_READ_COST,
            )
        governor.admit(
            request_class=RequestClass.ORDER, endpoint=_SUBMIT_ENDPOINT, cost=_ORDER_COST
        )

    def test_locally_charged_weight_sheds_without_any_header(self) -> None:
        """The estimate has to bind on its own, because the header lags a round trip."""
        clock = FrozenClock()
        governor = _governor(clock)
        # 240 reads at weight 20 is 4800, exactly the 80% discretionary ceiling.
        for _ in range(240):
            governor.admit(
                request_class=RequestClass.RESEARCH,
                endpoint=_MY_TRADES_ENDPOINT,
                cost=_READ_COST,
            )
        with pytest.raises(RateBudgetExhausted):
            governor.admit(
                request_class=RequestClass.RESEARCH,
                endpoint=_MY_TRADES_ENDPOINT,
                cost=_READ_COST,
            )

    def test_charges_made_after_an_observation_are_added_to_it(self) -> None:
        """Neither number alone is complete, so the governor takes the larger."""
        clock = FrozenClock()
        governor = _governor(clock)
        governor.observe(_metadata(_HTTP_OK, {"x-mbx-used-weight-1m": "1000"}))
        governor.admit(
            request_class=RequestClass.RESEARCH, endpoint=_MY_TRADES_ENDPOINT, cost=_READ_COST
        )

        assert governor.used_request_weight() == _OBSERVED_PLUS_LOCAL_WEIGHT

    def test_a_weight_charge_leaves_the_window_after_a_minute(self) -> None:
        clock = FrozenClock()
        governor = _governor(clock)
        governor.observe(_metadata(_HTTP_OK, {"x-mbx-used-weight-1m": "5940"}))

        clock.advance(60.001)

        assert governor.used_request_weight() == 0
        governor.admit(
            request_class=RequestClass.BACKFILL, endpoint=_MY_TRADES_ENDPOINT, cost=_READ_COST
        )

    def test_a_venue_with_no_published_weight_budget_sheds_nothing_on_weight(self) -> None:
        """Bybit meters per endpoint and publishes no weight header.

        Shedding against a number nobody published would refuse correct requests for a
        reason that does not exist.
        """
        clock = FrozenClock()
        governor = _governor(clock, venue=Venue.BYBIT_TESTNET)
        assert BYBIT_TESTNET.request_weight_per_minute is None

        for _ in range(500):
            governor.admit(
                request_class=RequestClass.BACKFILL,
                endpoint=_MY_TRADES_ENDPOINT,
                cost=_READ_COST,
            )


class TestTheRecorded429:
    def test_retry_after_is_honoured_exactly_and_refuses_for_the_full_window(self) -> None:
        recording = load_recording(Venue.BINANCE_SPOT_TESTNET, "rateLimited_rejected")
        clock = FrozenClock()
        governor = _governor(clock)

        governor.observe(_metadata(recording.http_status, recording.headers))

        assert [incident.reason for incident in governor.incidents] == ["retry_after"]
        incident = governor.incidents[0]
        assert incident.retry_after_seconds == _RECORDED_RETRY_AFTER_SECONDS
        assert incident.observed_at_utc == clock.now_utc()
        assert incident.observed_at_utc.tzinfo is not None

        # Refused for the whole window, including the order path: another request now is
        # the one that turns a 429 into a 418.
        clock.advance(_RECORDED_RETRY_AFTER_SECONDS - 0.001)
        with pytest.raises(RateBudgetExhausted) as refused:
            governor.admit(
                request_class=RequestClass.ORDER, endpoint=_SUBMIT_ENDPOINT, cost=_ORDER_COST
            )
        assert refused.value.budget_free_in_seconds == pytest.approx(0.001)

        clock.advance(0.002)
        governor.admit(
            request_class=RequestClass.ORDER, endpoint=_SUBMIT_ENDPOINT, cost=_ORDER_COST
        )

    @pytest.mark.asyncio
    async def test_retry_after_makes_exactly_one_attempt(self) -> None:
        """The 429 is not retried inside its own window, by anything.

        Asserted on the transport's own request counter rather than on a mock's call
        list: the counter is what `GuardedExchange` exposes precisely so that "we went to
        the venue" is falsifiable.
        """
        clock = FrozenClock()
        governor = _governor(clock)
        exchange = RecordedExchange(Venue.BINANCE_SPOT_TESTNET)
        exchange.override("privateGetAccount", "rateLimited_rejected")
        throttled = ThrottledExchange(exchange, governor, request_class=RequestClass.RECONCILIATION)

        await throttled.call("privateGetAccount", {})
        assert exchange.request_count == 1

        with pytest.raises(RateBudgetExhausted):
            await throttled.call("privateGetAccount", {})
        assert exchange.request_count == 1

    def test_a_429_without_a_usable_retry_after_falls_back_to_the_venue_s_own_window(
        self,
    ) -> None:
        """No invented backoff schedule: the fallback is the weight window's width."""
        clock = FrozenClock()
        governor = _governor(clock)

        governor.observe(_metadata(429, {"retry-after": "in a bit"}))

        assert governor.incidents[0].reason == "no_retry_after"
        assert governor.incidents[0].retry_after_seconds is None
        clock.advance(59.999)
        with pytest.raises(RateBudgetExhausted):
            governor.admit(
                request_class=RequestClass.ORDER, endpoint=_SUBMIT_ENDPOINT, cost=_ORDER_COST
            )
        clock.advance(0.002)
        governor.admit(
            request_class=RequestClass.ORDER, endpoint=_SUBMIT_ENDPOINT, cost=_ORDER_COST
        )

    def test_a_429_is_logged_at_error_because_it_means_the_throttle_is_miscalibrated(
        self,
    ) -> None:
        recording = load_recording(Venue.BINANCE_SPOT_TESTNET, "rateLimited_rejected")
        governor = _governor(FrozenClock())

        with structlog.testing.capture_logs() as captured:
            governor.observe(_metadata(recording.http_status, recording.headers))

        breaches = [entry for entry in captured if entry["event"] == "ratelimit.throttle_breached"]
        assert [entry["log_level"] for entry in breaches] == ["error"]
        assert breaches[0]["retry_after_seconds"] == _RECORDED_RETRY_AFTER_SECONDS


class TestTheRecorded418:
    def test_a_ban_is_a_hard_stop_with_a_critical_log_and_no_further_request(self) -> None:
        recording = load_recording(Venue.BINANCE_SPOT_TESTNET, "ipBanned_rejected")
        clock = FrozenClock()
        governor = _governor(clock)

        with structlog.testing.capture_logs() as captured:
            governor.observe(_metadata(recording.http_status, recording.headers))

        bans = [entry for entry in captured if entry["event"] == "ratelimit.ip_banned"]
        assert [entry["log_level"] for entry in bans] == ["critical"]
        assert governor.is_banned is True
        assert governor.incidents[0].reason == "ip_banned"

        with pytest.raises(VenueIpBannedError):
            governor.admit(
                request_class=RequestClass.ORDER, endpoint=_SUBMIT_ENDPOINT, cost=_ORDER_COST
            )

    def test_a_ban_never_times_out(self) -> None:
        """Binance escalates from 2 minutes to 3 days; a timer here is a retry loop."""
        recording = load_recording(Venue.BINANCE_SPOT_TESTNET, "ipBanned_rejected")
        clock = FrozenClock()
        governor = _governor(clock)
        governor.observe(_metadata(recording.http_status, recording.headers))

        clock.advance(3 * 24 * 60 * 60)

        assert governor.is_banned is True
        with pytest.raises(VenueIpBannedError):
            governor.admit(
                request_class=RequestClass.BACKFILL,
                endpoint=_MY_TRADES_ENDPOINT,
                cost=_READ_COST,
            )

    @pytest.mark.asyncio
    async def test_a_banned_venue_receives_no_further_request_through_the_transport(
        self,
    ) -> None:
        clock = FrozenClock()
        governor = _governor(clock)
        exchange = RecordedExchange(Venue.BINANCE_SPOT_TESTNET)
        exchange.override("privateGetAccount", "ipBanned_rejected")
        throttled = ThrottledExchange(exchange, governor, request_class=RequestClass.RECONCILIATION)

        await throttled.call("privateGetAccount", {})
        assert exchange.request_count == 1

        with pytest.raises(VenueIpBannedError):
            await throttled.call("privateGetAccount", {})
        assert exchange.request_count == 1


class TestHeaderParsing:
    def test_an_absent_header_is_not_a_reading(self) -> None:
        observation = parse_rate_limit_headers(_metadata(_HTTP_OK, {}))
        assert observation.used_request_weight_1m is None
        assert observation.retry_after_seconds is None
        assert observation.malformed_headers == ()

    def test_header_names_are_matched_without_regard_to_case(self) -> None:
        """Binance's spot and futures fleets do not agree on the casing."""
        observation = parse_rate_limit_headers(
            _metadata(_HTTP_OK, {"X-MBX-USED-WEIGHT-1M": str(_SAMPLE_WEIGHT_READING)})
        )
        assert observation.used_request_weight_1m == _SAMPLE_WEIGHT_READING

    @pytest.mark.parametrize(
        "raw",
        [
            "",
            "-1",
            "1.5",
            "1_0",
            _FULL_WIDTH_TWELVE,
            # aiohttp joins repeated headers with a comma; `int()` refuses this one, but a
            # parser that stripped punctuation would silently read 6000200.
            "6000, 200",
            "abc",
        ],
    )
    def test_a_value_this_parser_does_not_model_is_reported_as_malformed(self, raw: str) -> None:
        """Never guessed at. `int()` would accept two of these and mean something else."""
        observation = parse_rate_limit_headers(_metadata(_HTTP_OK, {"x-mbx-used-weight-1m": raw}))
        assert observation.used_request_weight_1m is None
        assert observation.malformed_headers == ("x-mbx-used-weight-1m",)

    def test_an_unreadable_weight_header_makes_the_governor_assume_the_worst(self) -> None:
        """The safe direction: shed discretionary traffic, keep the order path."""
        clock = FrozenClock()
        governor = _governor(clock)

        with structlog.testing.capture_logs() as captured:
            governor.observe(_metadata(_HTTP_OK, {"x-mbx-used-weight-1m": "??"}))

        assert governor.used_request_weight() == BINANCE_SPOT_TESTNET.request_weight_per_minute
        assert [
            entry["log_level"]
            for entry in captured
            if entry["event"] == "ratelimit.header_unparseable"
        ] == ["error"]
        with pytest.raises(RateBudgetExhausted):
            governor.admit(
                request_class=RequestClass.BACKFILL,
                endpoint=_MY_TRADES_ENDPOINT,
                cost=_READ_COST,
            )

    def test_an_unreadable_header_on_a_venue_with_no_budget_changes_nothing(self) -> None:
        governor = _governor(FrozenClock(), venue=Venue.BYBIT_TESTNET)
        governor.observe(_metadata(_HTTP_OK, {"x-mbx-used-weight-1m": "??"}))
        assert governor.used_request_weight() == 0


class TestTheThrottledExchangeView:
    def test_every_endpoint_the_adapter_calls_has_a_declared_weight(self) -> None:
        """A missing entry would spend an unknown amount of the venue's budget."""
        for market, endpoints in _ENDPOINTS.items():
            declared = ENDPOINT_COSTS[market]
            called = {
                name
                for name in (
                    endpoints.exchange_info,
                    endpoints.balances,
                    endpoints.open_orders,
                    endpoints.my_trades,
                    endpoints.submit,
                    endpoints.cancel,
                    endpoints.cancel_replace,
                    endpoints.positions,
                )
                if name is not None
            }
            assert called <= set(declared), f"{market} is missing {called - set(declared)}"

    @pytest.mark.asyncio
    async def test_an_undeclared_endpoint_is_refused_rather_than_admitted_blind(self) -> None:
        clock = FrozenClock()
        throttled = ThrottledExchange(
            RecordedExchange(Venue.BINANCE_SPOT_TESTNET),
            _governor(clock),
            request_class=RequestClass.RECONCILIATION,
        )
        with pytest.raises(ThrottleConfigurationError, match="no declared request weight"):
            await throttled.call("publicGetSomethingNobodyDeclared", {})

    @pytest.mark.asyncio
    async def test_an_order_cannot_be_issued_through_a_sheddable_view(self) -> None:
        """An order must never be droppable as low-priority traffic."""
        clock = FrozenClock()
        throttled = ThrottledExchange(
            RecordedExchange(Venue.BINANCE_SPOT_TESTNET),
            _governor(clock),
            request_class=RequestClass.BACKFILL,
        )
        with pytest.raises(ThrottleConfigurationError, match="places an order"):
            await throttled.call(_SUBMIT_ENDPOINT, {})

    @pytest.mark.asyncio
    async def test_two_views_draw_on_one_shared_budget(self) -> None:
        """The venue meters per IP, so two views must not each believe they own it."""
        clock = FrozenClock()
        governor = _governor(clock)
        exchange = RecordedExchange(Venue.BINANCE_SPOT_TESTNET)
        research = ThrottledExchange(exchange, governor, request_class=RequestClass.RESEARCH)
        reconciler = ThrottledExchange(
            exchange, governor, request_class=RequestClass.RECONCILIATION
        )

        # 60 openOrders reads at the table's worst-case weight of 80 reach the 80%
        # discretionary ceiling exactly.
        for _ in range(60):
            await research.call(_OPEN_ORDERS_ENDPOINT, {})

        with pytest.raises(RateBudgetExhausted):
            await research.call(_OPEN_ORDERS_ENDPOINT, {})
        # The reconciler's ceiling is higher, so the shared budget has left it room.
        await reconciler.call(_OPEN_ORDERS_ENDPOINT, {})

    @pytest.mark.asyncio
    async def test_the_view_delegates_the_transport_s_own_properties(self) -> None:
        exchange = RecordedExchange(Venue.BINANCE_SPOT_TESTNET)
        throttled = ThrottledExchange(
            exchange, _governor(FrozenClock()), request_class=RequestClass.RECONCILIATION
        )

        assert throttled.venue_id == str(Venue.BINANCE_SPOT_TESTNET)
        assert throttled.request_class is RequestClass.RECONCILIATION
        # Before any call the transport has nothing to report, and the view must say so
        # rather than inventing a status a governor would then act on.
        assert exchange.last_response_metadata is None

        await throttled.call("publicGetExchangeInfo", {})
        metadata = throttled.last_response_metadata
        assert metadata is not None
        assert metadata.http_status == _HTTP_OK

        await throttled.aclose()
        assert exchange.closed is True

    @pytest.mark.asyncio
    async def test_a_failing_call_still_reaches_the_governor(self) -> None:
        """A 429 arrives as a failure, and that is the response it must not miss."""
        clock = FrozenClock()
        governor = _governor(clock)
        exchange = RecordedExchange(Venue.BINANCE_SPOT_TESTNET)
        exchange.override("privateGetAccount", "rateLimited_rejected")
        exchange.raise_after_recording = RuntimeError("transport blew up after the response")
        throttled = ThrottledExchange(exchange, governor, request_class=RequestClass.RECONCILIATION)

        with pytest.raises(RuntimeError):
            await throttled.call("privateGetAccount", {})

        assert [incident.reason for incident in governor.incidents] == ["retry_after"]
