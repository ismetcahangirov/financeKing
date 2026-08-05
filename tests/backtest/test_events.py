"""What each event refuses at construction.

An event is the unit the whole run is reconstructed from, so a malformed one is not a
value to be repaired later -- it is a hole in the trace. Every refusal below happens
where the mistake is, rather than several frames downstream on a value nobody has
questioned.
"""

from __future__ import annotations

from collections.abc import Callable
from datetime import UTC, datetime, timedelta, timezone
from decimal import Decimal

import pytest

from fking.backtest import (
    FundingEvent,
    MarketDataEvent,
    OrderAckEvent,
    ReconciliationEvent,
    RejectEvent,
    RunConfigError,
    TimerEvent,
)
from fking.domain import Venue
from tests.support.backtest_events import bar_at
from tests.support.domain_factory import BTCUSDT, make_order, make_tick

pytestmark = pytest.mark.unit

START = datetime(2026, 8, 1, 0, 0, tzinfo=UTC)
NAIVE = datetime(2026, 8, 1, 0, 0)  # noqa: DTZ001 - the value every case below must refuse
BAKU = timezone(timedelta(hours=4))


def test_a_trade_is_knowable_at_the_instant_the_venue_printed_it() -> None:
    tick = make_tick()
    assert MarketDataEvent(observation=tick).occurs_at_utc == tick.event_time_utc


def test_a_bar_and_a_trade_take_their_instant_from_different_fields() -> None:
    """One derivation per observation kind, so neither can be scheduled by hand."""
    bar = bar_at(START)
    assert MarketDataEvent(observation=bar).occurs_at_utc == bar.close_time_utc
    assert MarketDataEvent(observation=make_tick()).occurs_at_utc == make_tick().event_time_utc


@pytest.mark.parametrize(
    "build",
    [
        pytest.param(
            lambda instant: FundingEvent(
                instrument=BTCUSDT, occurs_at_utc=instant, funding_rate=Decimal("0.0001")
            ),
            id="FundingEvent",
        ),
        pytest.param(
            lambda instant: OrderAckEvent(order=make_order(), occurs_at_utc=instant),
            id="OrderAckEvent",
        ),
        pytest.param(
            lambda instant: RejectEvent(
                order=make_order(), occurs_at_utc=instant, reason="LOT_SIZE"
            ),
            id="RejectEvent",
        ),
        pytest.param(
            lambda instant: TimerEvent(strategy_id="s", occurs_at_utc=instant, label="wake"),
            id="TimerEvent",
        ),
        pytest.param(
            lambda instant: ReconciliationEvent(
                venue=Venue.BINANCE_SPOT_TESTNET, occurs_at_utc=instant
            ),
            id="ReconciliationEvent",
        ),
    ],
)
@pytest.mark.parametrize(
    ("instant", "expected_message"),
    [
        (NAIVE, "timezone-aware"),
        (datetime(2026, 8, 1, 4, 0, tzinfo=BAKU), "must be UTC"),
        ("2026-08-01T00:00:00Z", "must be a datetime"),
    ],
    ids=["naive", "non-utc", "a string that looks like one"],
)
def test_every_event_refuses_an_instant_that_is_not_aware_utc(
    build: Callable[[object], object], instant: object, expected_message: str
) -> None:
    with pytest.raises(RunConfigError, match=expected_message):
        build(instant)


def test_a_negative_funding_rate_is_ordinary_and_accepted() -> None:
    """Shorts pay the longs in a bear market; a guard that refused this would be wrong."""
    event = FundingEvent(instrument=BTCUSDT, occurs_at_utc=START, funding_rate=Decimal("-0.0003"))
    assert event.funding_rate < 0


@pytest.mark.parametrize(
    ("rate", "expected_message"),
    [
        (0.0001, "not a float"),
        ("0.0001", "must be a Decimal"),
        (Decimal("NaN"), "must be finite"),
        (Decimal("Infinity"), "must be finite"),
    ],
    ids=["a float", "a string", "NaN", "infinity"],
)
def test_a_funding_rate_that_is_not_an_exact_finite_decimal_is_refused(
    rate: object, expected_message: str
) -> None:
    """`NaN` compares unequal to itself, so one of them makes every later comparison lie."""
    with pytest.raises(RunConfigError, match=expected_message):
        FundingEvent(instrument=BTCUSDT, occurs_at_utc=START, funding_rate=rate)  # type: ignore[arg-type]  # the value under test


@pytest.mark.parametrize(
    ("reason", "expected_message"),
    [("   ", "must not be blank"), (b"LOT_SIZE", "must be a string")],
    ids=["blank", "bytes"],
)
def test_a_rejection_without_a_readable_reason_is_refused(
    reason: object, expected_message: str
) -> None:
    """A rejection with no reason satisfies the schema and answers nothing in the trace."""
    with pytest.raises(RunConfigError, match=expected_message):
        RejectEvent(order=make_order(), occurs_at_utc=START, reason=reason)  # type: ignore[arg-type]  # the value under test


@pytest.mark.parametrize(("strategy_id", "label"), [("", "wake"), ("s", "  ")])
def test_a_timer_without_an_owner_or_a_label_is_refused(strategy_id: str, label: str) -> None:
    with pytest.raises(RunConfigError, match="must not be blank"):
        TimerEvent(strategy_id=strategy_id, occurs_at_utc=START, label=label)
