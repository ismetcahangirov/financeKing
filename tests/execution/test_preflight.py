"""The startup gate, item by item, against the recorded corpus.

Every symbol, filter and server-time value in this file comes from bytes a real testnet
returned -- `exchangeInfo` for the universe, the filters and the deliberate non-ASCII
symbols, `GET /api/v3/time` for the skew measurement, and the recorded `-1102` rejection
for the credential path. The only values constructed here are the *absence* of holdings
and open orders, which is a state the venue reports as an empty array and which the
corpus cannot capture without an authenticated recording.

The parametrized test is the one that matters: each of the ten items is made to fail on
its own while the other nine are satisfiable, and each failure must abort.
"""

from __future__ import annotations

from collections.abc import AsyncIterator, Mapping
from dataclasses import dataclass, replace
from datetime import UTC, datetime, timedelta
from itertools import cycle
from typing import Final, Literal

import pytest
import structlog

from fking.domain import Order, Venue
from fking.execution import (
    BINANCE_SPOT_TESTNET,
    BinanceVenue,
    ClockDriftError,
    ClockSkewMonitor,
    PermanentExchangeError,
    PreflightAbortedError,
    PreflightItem,
    VenueBalance,
    VenueExchangeInfo,
    VenueOrder,
    VenuePosition,
    VenueProfile,
    VenueTrade,
    assert_skew_within_budget,
    classify_timestamp_domain,
    measure_clock_skew,
    parse_venue_payload,
    preflight_or_abort,
    run_preflight,
)
from fking.execution.preflight import ClockSkewSample, PreflightReport
from tests.execution.conftest import RecordedExchange, load_recording

pytestmark = pytest.mark.unit

# BTCUSDT is present in the recorded spot exchangeInfo and carries a full filter set.
REQUIRED_SYMBOLS: Final[frozenset[str]] = frozenset({"BTCUSDT"})
PRODUCTION_PROVENANCE: Final[str] = "production:binance-spot-2026-07"
# The captured account rejection: "Mandatory parameter 'signature' was not sent".
MISSING_SIGNATURE_VENUE_CODE: Final[int] = -1102
# 100ms local offset corrected for a 40ms round trip leaves the venue 80ms ahead.
EXPECTED_SKEW_MS: Final[int] = 80


def _recorded_exchange_info() -> VenueExchangeInfo:
    body = load_recording(Venue.BINANCE_SPOT_TESTNET, "exchangeInfo").body
    return VenueExchangeInfo.model_validate(parse_venue_payload(body))


def _recorded_server_time_ms() -> int:
    payload = parse_venue_payload(load_recording(Venue.BINANCE_SPOT_TESTNET, "serverTime").body)
    assert isinstance(payload, Mapping)
    return int(payload["serverTime"])


@dataclass(slots=True)
class FakeVenue:
    """An `ExecutionVenue` whose reads come from the corpus.

    Only the four methods the pre-flight calls are exercised; the order-path methods raise
    so that a future edit which reaches for one from the startup gate fails loudly rather
    than placing an order before the gate has cleared.
    """

    profile_value: VenueProfile
    exchange_info_value: VenueExchangeInfo
    balances: tuple[VenueBalance, ...] = ()
    open_orders: tuple[VenueOrder, ...] = ()
    balances_error: Exception | None = None

    @property
    def profile(self) -> VenueProfile:
        return self.profile_value

    async def exchange_info(self) -> VenueExchangeInfo:
        return self.exchange_info_value

    async def fetch_balances(self) -> tuple[VenueBalance, ...]:
        if self.balances_error is not None:
            raise self.balances_error
        return self.balances

    async def fetch_positions(self) -> tuple[VenuePosition, ...]:
        return ()

    async def fetch_open_orders(
        self,
        *,
        symbol: str | None = None,  # noqa: ARG002 - Protocol shape
    ) -> tuple[VenueOrder, ...]:
        return self.open_orders

    async def fetch_my_trades(
        self,
        *,
        symbol: str,  # noqa: ARG002 - Protocol shape
        since_ms: int | None = None,  # noqa: ARG002 - Protocol shape
    ) -> tuple[VenueTrade, ...]:
        raise AssertionError("the startup gate does not read trade history")

    async def submit(self, order: Order) -> VenueOrder:  # noqa: ARG002 - Protocol shape
        raise AssertionError("the startup gate never places an order")

    async def cancel(
        self,
        *,
        symbol: str,  # noqa: ARG002 - Protocol shape
        client_order_id: str,  # noqa: ARG002 - Protocol shape
    ) -> VenueOrder:
        raise AssertionError("the startup gate never cancels an order")

    async def cancel_replace(
        self,
        *,
        replacement: Order,  # noqa: ARG002 - Protocol shape
        cancel_client_order_id: str,  # noqa: ARG002 - Protocol shape
    ) -> VenueOrder:
        raise AssertionError("the startup gate never replaces an order")

    async def aclose(self) -> None:
        return None


@dataclass(slots=True)
class FakeServerTime:
    epoch_ms: int

    async def server_time_ms(self) -> int:
        return self.epoch_ms


@dataclass(slots=True)
class FakeUserData:
    """A `UserDataSource` that delivers one event, or none at all."""

    profile_value: VenueProfile
    deliver_event: bool = True
    connected: bool = False

    @property
    def profile(self) -> VenueProfile:
        return self.profile_value

    async def connect(self) -> None:
        self.connected = True

    async def refresh(self) -> None:
        return None

    async def reconnect(self) -> None:
        return None

    def events(self) -> AsyncIterator[Mapping[str, object]]:
        async def _stream() -> AsyncIterator[Mapping[str, object]]:
            if self.deliver_event:
                yield {"e": "outboundAccountPosition", "E": _recorded_server_time_ms()}

        return _stream()

    async def aclose(self) -> None:
        return None


@dataclass(frozen=True, slots=True)
class Harness:
    """Everything `run_preflight` needs, with every item satisfiable."""

    venue: FakeVenue
    monitor: ClockSkewMonitor
    user_data: FakeUserData | None
    credential_kind: Literal["ed25519", "hmac"] = "ed25519"
    required_symbols: frozenset[str] = REQUIRED_SYMBOLS
    configured_order_rate_per_10s: int = BINANCE_SPOT_TESTNET.order_rate_per_10s
    provenance_id: str = PRODUCTION_PROVENANCE
    local_state_expects_holdings: bool = False

    async def run(self) -> PreflightReport:
        return await run_preflight(
            self.venue,
            clock_monitor=self.monitor,
            credential_kind=self.credential_kind,
            required_symbols=self.required_symbols,
            configured_order_rate_per_10s=self.configured_order_rate_per_10s,
            cost_parameter_provenance_id=self.provenance_id,
            user_data=self.user_data,
            local_state_expects_holdings=self.local_state_expects_holdings,
        )

    async def run_or_abort(self) -> PreflightReport:
        return await preflight_or_abort(
            self.venue,
            clock_monitor=self.monitor,
            credential_kind=self.credential_kind,
            required_symbols=self.required_symbols,
            configured_order_rate_per_10s=self.configured_order_rate_per_10s,
            cost_parameter_provenance_id=self.provenance_id,
            user_data=self.user_data,
            local_state_expects_holdings=self.local_state_expects_holdings,
        )


def _monitor(
    *, server_epoch_ms: int, local_offset_ms: int = 0, profile: VenueProfile = BINANCE_SPOT_TESTNET
) -> ClockSkewMonitor:
    """A monitor whose local clock is `local_offset_ms` ahead of the recorded server time.

    Both the wall clock and the monotonic counter are injected, so the round trip is a
    stated 20ms rather than whatever the test machine happened to take.
    """
    local_now = datetime(1970, 1, 1, tzinfo=UTC) + timedelta(
        milliseconds=server_epoch_ms + local_offset_ms
    )
    # A stated 20ms round trip rather than whatever the test machine happened to take.
    # Cycled rather than exhausted: a monitor is sampled once per pre-flight run, and
    # several tests run the same harness twice to inspect the report after the abort.
    readings = cycle((1000.0, 1000.02))

    return ClockSkewMonitor(
        FakeServerTime(server_epoch_ms),
        profile,
        now_utc=lambda: local_now,
        monotonic_seconds=lambda: next(readings),
    )


@pytest.fixture
def harness() -> Harness:
    exchange_info = _recorded_exchange_info()
    profile = BINANCE_SPOT_TESTNET
    return Harness(
        venue=FakeVenue(
            profile_value=profile,
            exchange_info_value=exchange_info,
            balances=(VenueBalance.model_validate({"asset": "USDT", "free": "10000.00000000"}),),
        ),
        monitor=_monitor(server_epoch_ms=_recorded_server_time_ms()),
        user_data=FakeUserData(profile_value=profile),
    )


@pytest.mark.asyncio
async def test_every_item_passes_against_the_recorded_corpus(harness: Harness) -> None:
    report = await harness.run_or_abort()

    assert report.is_ready
    assert [outcome.item for outcome in report.outcomes] == list(PreflightItem)


# One mutation per item, each breaking exactly that item while the other nine remain
# satisfiable. The tuple is the checklist made executable: an item added to PreflightItem
# without a failure case here fails test_every_item_has_a_failure_case below.
def _break_clock(harness: Harness) -> Harness:
    # 12 seconds, from the acceptance criteria. Well past the 1000ms budget and past the
    # 5000ms recvWindow, which is precisely the drift a wider window would "solve".
    return replace(
        harness,
        monitor=_monitor(server_epoch_ms=_recorded_server_time_ms(), local_offset_ms=12_000),
    )


def _break_credential(harness: Harness) -> Harness:
    return replace(harness, credential_kind="hmac")


def _break_universe(harness: Harness) -> Harness:
    return replace(harness, required_symbols=frozenset({"NOTLISTEDUSDT"}))


def _break_filters(harness: Harness) -> Harness:
    stripped = replace(
        harness.venue,
        exchange_info_value=harness.venue.exchange_info_value.model_copy(
            update={
                "symbols": tuple(
                    entry.model_copy(update={"filters": ()})
                    if entry.symbol in REQUIRED_SYMBOLS
                    else entry
                    for entry in harness.venue.exchange_info_value.symbols
                )
            }
        ),
    )
    return replace(harness, venue=stripped)


def _break_order_rate(harness: Harness) -> Harness:
    # Production's 100/10s against testnet's measured 50/10s: the number a limiter picks
    # up when it is configured from the wrong environment's documentation.
    return replace(harness, configured_order_rate_per_10s=100)


def _break_user_data(harness: Harness) -> Harness:
    return replace(harness, user_data=None)


def _break_provenance(harness: Harness) -> Harness:
    return replace(harness, provenance_id="testnet:binance-spot-2026-08")


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("item", "mutate"),
    [
        (PreflightItem.CLOCK_SKEW, _break_clock),
        (PreflightItem.CREDENTIAL_KEY_TYPE, _break_credential),
        (PreflightItem.SYMBOL_UNIVERSE, _break_universe),
        (PreflightItem.SYMBOL_FILTERS, _break_filters),
        (PreflightItem.ORDER_RATE_BUDGET, _break_order_rate),
        (PreflightItem.USER_DATA_HEARTBEAT, _break_user_data),
        (PreflightItem.COST_PARAMETER_PROVENANCE, _break_provenance),
    ],
    ids=lambda argument: argument.value if isinstance(argument, PreflightItem) else "",
)
async def test_each_item_fails_the_process_closed_in_isolation(
    harness: Harness, item: PreflightItem, mutate: object
) -> None:
    broken = mutate(harness)  # type: ignore[operator] # parametrized callables, one shape

    with pytest.raises(PreflightAbortedError, match=item.value):
        await broken.run_or_abort()

    report = await broken.run()
    assert [failure.item for failure in report.failures] == [item]
    assert not report.is_ready


@pytest.mark.asyncio
async def test_a_failure_emits_a_critical_record_naming_the_item(harness: Harness) -> None:
    """The operator-facing half of failing closed.

    The item travels as a *field* on one stable event name rather than as the event
    name itself: an interpolated event name cannot be aggregated, so "how often does
    the credential item fail" would become a regex against our own log format.
    """
    with structlog.testing.capture_logs() as captured:
        await _break_credential(harness).run()

    record = next(entry for entry in captured if entry["event"] == "preflight.item_failed")
    assert record["log_level"] == "critical"
    assert record["item"] == PreflightItem.CREDENTIAL_KEY_TYPE.value
    assert record["required_key_type"] == "ed25519"


@pytest.mark.asyncio
async def test_a_missing_filter_set_names_the_symbol_rather_than_defaulting(
    harness: Harness,
) -> None:
    """A defaulted tick size is a number nobody chose rounding a price the venue rejects."""
    report = await _break_filters(harness).run()

    (failure,) = report.failures
    assert failure.item is PreflightItem.SYMBOL_FILTERS
    assert "BTCUSDT" in failure.detail or "BTCUSDT" in failure.evidence.values()


@pytest.mark.asyncio
async def test_a_twelve_second_offset_aborts_and_leaves_recv_window_untouched(
    harness: Harness,
) -> None:
    """The acceptance criterion that stops the tempting fix.

    Widening `recvWindow` does not fix a wrong clock. It signs requests against one, and
    every timestamp then written to the append-only audit log is wrong by the same amount
    -- permanently, since audit rows cannot be corrected.
    """
    broken = _break_clock(harness)
    recv_window_before_ms = broken.venue.profile.recv_window_ms

    with pytest.raises(PreflightAbortedError, match="clock_skew"):
        await broken.run_or_abort()

    assert broken.venue.profile.recv_window_ms == recv_window_before_ms
    assert BINANCE_SPOT_TESTNET.recv_window_ms == recv_window_before_ms
    assert broken.venue.profile.max_clock_drift_ms < recv_window_before_ms


@pytest.mark.asyncio
async def test_an_hmac_key_on_the_spot_path_names_the_expected_key_type(
    harness: Harness,
) -> None:
    report = await _break_credential(harness).run()

    (failure,) = report.failures
    assert failure.evidence["required_key_type"] == "ed25519"
    assert failure.evidence["presented_key_type"] == "hmac"
    assert "session_logon_ed25519" in failure.detail


@pytest.mark.asyncio
async def test_a_recorded_signature_rejection_fails_the_credential_item(
    harness: Harness,
) -> None:
    """Replayed from the captured `-1102`, through the real adapter that raises it.

    The corpus holds unauthenticated captures, so the account endpoint comes back as a
    signature-class rejection -- exactly the shape a wrong key type produces. The
    rejection is taken from `BinanceVenue` driven over the recorded bytes rather than
    constructed here, so a change to the adapter's classification breaks this test.
    """
    adapter = BinanceVenue(RecordedExchange(Venue.BINANCE_SPOT_TESTNET), BINANCE_SPOT_TESTNET)
    with pytest.raises(PermanentExchangeError) as raised:
        await adapter.fetch_balances()
    assert raised.value.venue_code == MISSING_SIGNATURE_VENUE_CODE

    broken = replace(harness, venue=replace(harness.venue, balances_error=raised.value))

    report = await broken.run()

    (failure,) = report.failures
    assert failure.item is PreflightItem.CREDENTIAL_KEY_TYPE
    assert failure.evidence["venue_code"] == "-1102"
    assert "credential" in failure.detail


@pytest.mark.asyncio
async def test_zero_everything_is_routed_to_the_wipe_handler_not_reported_as_a_discrepancy(
    harness: Harness,
) -> None:
    """Spot testnet is wiped roughly every 30 days: keys survive, balances do not."""
    wiped = replace(
        harness,
        venue=replace(harness.venue, balances=(), open_orders=()),
        local_state_expects_holdings=True,
    )

    report = await wiped.run_or_abort()

    assert report.wipe_suspected
    assert report.is_ready  # routed, never a blocking failure


@pytest.mark.asyncio
async def test_an_empty_venue_with_no_local_holdings_is_not_a_wipe(harness: Harness) -> None:
    quiet = replace(harness, venue=replace(harness.venue, balances=(), open_orders=()))

    report = await quiet.run_or_abort()

    assert not report.wipe_suspected


@pytest.mark.asyncio
async def test_allowlist_logged(harness: Harness) -> None:
    """The allowlist appears in the boot log on every start (ARCHITECTURE.md section 8)."""
    with structlog.testing.capture_logs() as captured:
        await harness.run_or_abort()

    record = next(entry for entry in captured if entry["event"] == "safety.allowlist")
    assert "testnet.binance.vision" in record["permitted_hosts"]
    assert record["endpoints"] == list(BINANCE_SPOT_TESTNET.endpoint_urls)


@pytest.mark.asyncio
async def test_the_recorded_non_ascii_symbols_round_trip_through_the_log_sink(
    harness: Harness,
) -> None:
    """The corpus keeps testnet's deliberate non-ASCII symbols; the item must survive them."""
    listed = tuple(
        entry.symbol for entry in harness.venue.exchange_info_value.symbols if entry.is_trading
    )
    assert any(not symbol.isascii() for symbol in listed)

    report = await harness.run_or_abort()

    (round_trip,) = [
        outcome for outcome in report.outcomes if outcome.item is PreflightItem.SYMBOL_ROUND_TRIP
    ]
    assert int(round_trip.evidence["non_ascii_symbol_count"]) > 0


def test_every_item_has_a_failure_case() -> None:
    """The checklist and its tests cannot drift apart silently.

    Two items are absent from the parametrized list for stated reasons: the allowlist item
    raises `SafetyViolation`, which is a `BaseException` with no handler and therefore is
    not an item outcome at all; and the exchange-state item routes rather than fails.
    """
    parametrized = {
        PreflightItem.CLOCK_SKEW,
        PreflightItem.CREDENTIAL_KEY_TYPE,
        PreflightItem.SYMBOL_UNIVERSE,
        PreflightItem.SYMBOL_FILTERS,
        PreflightItem.ORDER_RATE_BUDGET,
        PreflightItem.USER_DATA_HEARTBEAT,
        PreflightItem.COST_PARAMETER_PROVENANCE,
    }
    unparametrized = set(PreflightItem) - parametrized
    assert unparametrized == {
        PreflightItem.ALLOWLISTED_ENDPOINTS,
        PreflightItem.SYMBOL_ROUND_TRIP,
        PreflightItem.EXCHANGE_STATE,
    }


# ---------------------------------------------------------------------------
# Skew measurement
# ---------------------------------------------------------------------------


def test_skew_is_measured_against_the_round_trip_midpoint() -> None:
    """`server - (local_before + rtt/2)`, from the recorded server time."""
    server_epoch_ms = _recorded_server_time_ms()
    local_before_utc = datetime(1970, 1, 1, tzinfo=UTC) + timedelta(
        milliseconds=server_epoch_ms - 100
    )

    sample = measure_clock_skew(
        venue_id="binance-spot-testnet",
        server_epoch_ms=server_epoch_ms,
        local_before_utc=local_before_utc,
        monotonic_before_seconds=1000,
        monotonic_after_seconds=1000.04,
    )

    # Local read 100ms before the venue's stamp, 40ms round trip: the midpoint is 20ms
    # later, so the venue is 80ms ahead of the corrected local reading.
    assert sample.skew_ms == EXPECTED_SKEW_MS
    assert sample.round_trip_seconds == pytest.approx(0.04)


def test_a_monotonic_regression_is_refused_rather_than_clamped() -> None:
    """An NTP step during the measurement must not be reported as a plausible skew."""
    with pytest.raises(ClockDriftError, match="monotonic regression"):
        measure_clock_skew(
            venue_id="binance-spot-testnet",
            server_epoch_ms=_recorded_server_time_ms(),
            local_before_utc=datetime(2026, 8, 6, tzinfo=UTC),
            monotonic_before_seconds=1000,
            monotonic_after_seconds=999,
        )


def test_a_naive_local_reading_is_refused() -> None:
    with pytest.raises(ClockDriftError, match="aware UTC"):
        measure_clock_skew(
            venue_id="binance-spot-testnet",
            server_epoch_ms=_recorded_server_time_ms(),
            local_before_utc=datetime(2026, 8, 6),  # noqa: DTZ001 - the point of the test
            monotonic_before_seconds=0,
            monotonic_after_seconds=0,
        )


@pytest.mark.parametrize("skew_ms", [-1_001, 1_001, 12_000])
def test_skew_past_the_budget_halts(skew_ms: int) -> None:
    sample = ClockSkewSample(
        venue_id="binance-spot-testnet",
        server_time_utc=datetime(2026, 8, 6, tzinfo=UTC),
        local_midpoint_utc=datetime(2026, 8, 6, tzinfo=UTC),
        round_trip_seconds=0,
        skew_ms=skew_ms,
    )

    with pytest.raises(ClockDriftError, match="Fix the host clock"):
        assert_skew_within_budget(sample, profile=BINANCE_SPOT_TESTNET)


@pytest.mark.parametrize("skew_ms", [-1_000, 0, 1_000])
def test_skew_inside_the_budget_passes(skew_ms: int) -> None:
    sample = ClockSkewSample(
        venue_id="binance-spot-testnet",
        server_time_utc=datetime(2026, 8, 6, tzinfo=UTC),
        local_midpoint_utc=datetime(2026, 8, 6, tzinfo=UTC),
        round_trip_seconds=0,
        skew_ms=skew_ms,
    )

    assert_skew_within_budget(sample, profile=BINANCE_SPOT_TESTNET)


@pytest.mark.asyncio
async def test_the_monitor_halts_on_a_breach_and_reports_the_sample_otherwise() -> None:
    server_epoch_ms = _recorded_server_time_ms()

    within = await _monitor(server_epoch_ms=server_epoch_ms, local_offset_ms=200).sample_or_halt()
    assert within.absolute_skew_ms <= BINANCE_SPOT_TESTNET.max_clock_drift_ms

    with pytest.raises(ClockDriftError):
        await _monitor(server_epoch_ms=server_epoch_ms, local_offset_ms=12_000).sample_or_halt()


# ---------------------------------------------------------------------------
# The two timestamp domains
# ---------------------------------------------------------------------------


def test_timestamp_domains_a_recv_window_rejection_is_clock_drift_not_a_data_unit_error() -> None:
    """`-1021` is a signing-clock failure. Milliseconds, both markets, fix the clock."""
    assert classify_timestamp_domain(venue_code=-1021, epoch_ms=None) == "request_signing"


def test_timestamp_domains_a_microsecond_epoch_is_a_data_unit_error_not_clock_drift() -> None:
    """The archive split (ADR-0013) read as milliseconds lands far outside the range.

    Naming this as drift would send the reader to the clock and leave the parser wrong.
    """
    microsecond_epoch = _recorded_server_time_ms() * 1_000

    assert classify_timestamp_domain(venue_code=None, epoch_ms=microsecond_epoch) == (
        "market_data_epoch"
    )


def test_timestamp_domains_a_well_formed_millisecond_epoch_is_not_a_data_unit_error() -> None:
    assert classify_timestamp_domain(venue_code=None, epoch_ms=_recorded_server_time_ms()) == (
        "request_signing"
    )
