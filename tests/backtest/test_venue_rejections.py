"""The rejection taxonomy, driven by the filters the venue itself published.

Every bound in this file comes out of `tests/fixtures/recorded/`. Nothing asserts against
a number somebody believed Binance enforces, which is the failure mode
`docs/rules/testing-rules.md` bans hand-written fixtures to prevent -- the recorded
notional floor is 5.00 and a plausible hand-written 10.00 would make a whole band of
order sizes behave differently in backtest than on the venue.
"""

from __future__ import annotations

from datetime import timedelta
from decimal import Decimal

import pytest

from fking.backtest import OrderAckEvent, RejectEvent
from fking.backtest.venue import (
    RejectReason,
    VenueSimulationError,
    parse_order_rate_budget,
    parse_symbol_filters,
    screen_order,
)
from fking.domain import OrderType, Side
from tests.backtest.venue_support import (
    EPOCH,
    make_bar,
    make_order,
    make_venue,
    recorded_filters,
    recorded_order_rate_budget,
)

pytestmark = pytest.mark.unit


def test_filters_are_read_from_the_recorded_payload() -> None:
    filters = recorded_filters()

    assert filters.tick_size == Decimal("0.01000000")
    assert filters.lot_step == Decimal("0.00001000")
    assert filters.min_notional_quote == Decimal("5.00000000")
    assert filters.bid_multiplier_up == Decimal("2")
    assert filters.bid_multiplier_down == Decimal("0.5")


def test_the_narrowest_published_order_budget_wins() -> None:
    """Spot testnet publishes 50 per 10 seconds and 160 000 per day."""
    max_orders, window = recorded_order_rate_budget()

    assert (max_orders, window) == (50, timedelta(seconds=10))


def test_a_missing_filter_block_raises_rather_than_defaulting() -> None:
    payload = {"symbol": "BTCUSDT", "filters": [{"filterType": "PRICE_FILTER"}]}

    with pytest.raises(VenueSimulationError, match="no LOT_SIZE filter"):
        parse_symbol_filters(payload)


def test_a_json_number_in_a_filter_is_refused() -> None:
    """A number the parser has already turned into a double cannot be repaired here."""
    payload = {
        "symbol": "BTCUSDT",
        "filters": [
            # Every block is present, so the refusal is about the JSON number rather than
            # about a filter the payload never carried.
            {
                "filterType": "PRICE_FILTER",
                "minPrice": 0.01,
                "maxPrice": "1000.0",
                "tickSize": "0.01",
            },
            {"filterType": "LOT_SIZE", "minQty": "0.001", "maxQty": "9000", "stepSize": "0.001"},
            {"filterType": "NOTIONAL", "minNotional": "5.0"},
            {
                "filterType": "PERCENT_PRICE_BY_SIDE",
                "bidMultiplierUp": "2",
                "bidMultiplierDown": "0.5",
                "askMultiplierUp": "2",
                "askMultiplierDown": "0.5",
            },
        ],
    }

    with pytest.raises(VenueSimulationError, match="string-encoded decimal"):
        parse_symbol_filters(payload)


def test_a_payload_with_no_order_rate_limit_is_refused() -> None:
    with pytest.raises(VenueSimulationError, match="unlimited order rate"):
        parse_order_rate_budget({"rateLimits": []})


@pytest.mark.parametrize(
    ("limit_quote_price", "base_quantity", "reason"),
    [
        # 0.001 BTC at 64000 is 64 USDT, well above the floor, but the price is off the
        # 0.01 tick lattice.
        ("64000.005", "0.001", RejectReason.PRICE_FILTER),
        # A bid at half the reference is exactly the bidMultiplierDown bound; below it is
        # outside the band.
        ("30000.00", "0.001", RejectReason.PERCENT_PRICE_BY_SIDE),
        # 0.000015 is not a multiple of the 0.00001 step.
        ("64000.00", "0.000015", RejectReason.LOT_SIZE),
        # 0.00001 BTC at 64000 is 0.64 USDT, below the recorded 5.00 floor.
        ("64000.00", "0.00001", RejectReason.MIN_NOTIONAL),
    ],
)
def test_each_filter_breach_reports_its_own_taxonomy_member(
    limit_quote_price: str, base_quantity: str, reason: RejectReason
) -> None:
    venue = make_venue(spread_bps=Decimal("0"))
    venue.observe(make_bar())

    answer = venue.submit(
        make_order(limit_quote_price=limit_quote_price, base_quantity=base_quantity),
        decided_at_utc=EPOCH + timedelta(minutes=1),
    )

    assert isinstance(answer, RejectEvent)
    assert reason.value in answer.reason
    assert venue.report.rejection_counts[reason] == 1


def test_a_sub_min_notional_rejection_carries_the_venue_code_and_message() -> None:
    """-1013 with the venue's own filter name, so backtest and demo rejections compare."""
    rejection = screen_order(
        recorded_filters(),
        make_order(base_quantity="0.00001", limit_quote_price="64000.00"),
        Decimal("64000.00"),
    )

    assert rejection is not None
    assert rejection.reason is RejectReason.MIN_NOTIONAL
    assert (rejection.venue_code, rejection.venue_message) == (-1013, "Filter failure: NOTIONAL")


def test_a_market_order_is_screened_against_the_same_notional_floor() -> None:
    """`applyMinToMarket` is true on every symbol in the recording, so it is not exempt."""
    rejection = screen_order(
        recorded_filters(),
        make_order(order_type=OrderType.MARKET, limit_quote_price=None, base_quantity="0.00001"),
        Decimal("64000.00"),
    )

    assert rejection is not None
    assert rejection.reason is RejectReason.MIN_NOTIONAL


def test_size_beyond_the_modelled_band_is_rejected_rather_than_filled() -> None:
    venue = make_venue(spread_bps=Decimal("0"), touch_base=Decimal("2"), band_base=Decimal("10"))
    venue.observe(make_bar())
    ack = venue.submit(
        make_order(order_type=OrderType.MARKET, limit_quote_price=None, base_quantity="25"),
        decided_at_utc=EPOCH + timedelta(minutes=1),
    )
    assert isinstance(ack, OrderAckEvent)

    events = venue.resolve_ack(ack)

    assert len(events) == 1
    assert isinstance(events[0], RejectEvent)
    assert venue.report.rejection_counts[RejectReason.UNFILLABLE_DEPTH] == 1
    assert venue.report.fill_count == 0


def test_the_venue_refuses_rather_than_delays_once_the_order_budget_is_spent() -> None:
    """A limiter that sleeps turns a capacity problem into a latency problem."""
    venue = make_venue(spread_bps=Decimal("0"), order_rate_budget=2)
    venue.observe(make_bar())

    answers = [
        venue.submit(
            make_order(ordinal=index, base_quantity="0.001"),
            decided_at_utc=EPOCH + timedelta(minutes=1),
        )
        for index in range(1, 4)
    ]

    assert [isinstance(answer, RejectEvent) for answer in answers] == [False, False, True]
    assert venue.report.rejection_counts[RejectReason.ORDER_RATE_BUDGET] == 1


def test_the_budget_window_rolls_forward() -> None:
    venue = make_venue(spread_bps=Decimal("0"), order_rate_budget=1)
    venue.observe(make_bar())

    first = venue.submit(
        make_order(ordinal=1, base_quantity="0.001"), decided_at_utc=EPOCH + timedelta(minutes=1)
    )
    blocked = venue.submit(
        make_order(ordinal=2, base_quantity="0.001"), decided_at_utc=EPOCH + timedelta(minutes=1)
    )
    later = venue.submit(
        make_order(ordinal=3, base_quantity="0.001"), decided_at_utc=EPOCH + timedelta(minutes=2)
    )

    assert not isinstance(first, RejectEvent)
    assert isinstance(blocked, RejectEvent)
    assert not isinstance(later, RejectEvent)


def test_the_report_carries_a_zero_for_every_reason_nothing_raised() -> None:
    """Zero counts and unmodelled reasons must not be the same output."""
    counts = make_venue().report.rejection_counts

    assert set(counts) == set(RejectReason)
    assert set(counts.values()) == {0}


def test_an_order_arriving_before_any_bar_closed_is_refused_not_priced() -> None:
    venue = make_venue()

    with pytest.raises(VenueSimulationError, match="before any bar had closed"):
        venue.submit(make_order(side=Side.SELL, limit_quote_price="65000.00"), decided_at_utc=EPOCH)
