"""Bar and Tick invariants."""

from __future__ import annotations

from datetime import timedelta
from decimal import Decimal

import pytest

from fking.domain import Bar, DomainError, Side, Tick
from tests.support.domain_factory import BTCUSDT, EPOCH, make_bar, make_tick

pytestmark = pytest.mark.unit


def test_a_well_formed_bar_constructs() -> None:
    bar = make_bar()
    assert bar.close_time_utc - bar.open_time_utc == timedelta(minutes=1)
    assert bar.is_empty is False


def test_close_time_must_follow_open_time() -> None:
    with pytest.raises(DomainError, match="must follow"):
        Bar(
            instrument=BTCUSDT,
            open_time_utc=EPOCH,
            close_time_utc=EPOCH,
            open_quote_price=Decimal("64000"),
            high_quote_price=Decimal("64000"),
            low_quote_price=Decimal("64000"),
            close_quote_price=Decimal("64000"),
            base_volume=Decimal("0"),
            trade_count=0,
        )


@pytest.mark.parametrize(
    ("high_quote_price", "low_quote_price"),
    [
        # A high below the close, and a low above the open. Both are what a mis-keyed
        # epoch unit produces when a 1970 row is merged next to a 2026 one.
        ("64100.00", "63800.00"),
        ("64500.00", "64100.00"),
    ],
)
def test_high_and_low_must_bracket_open_and_close(
    high_quote_price: str, low_quote_price: str
) -> None:
    with pytest.raises(DomainError, match="do not bracket"):
        make_bar(high_quote_price=high_quote_price, low_quote_price=low_quote_price)


@pytest.mark.parametrize("trade_count", [-1, True, 4.0])
def test_trade_count_must_be_a_non_negative_int(trade_count: object) -> None:
    """`True` is an `int` to Python and is not a number anybody meant to write."""
    with pytest.raises(DomainError, match="trade_count"):
        make_bar(trade_count=trade_count)  # type: ignore[arg-type]  # the wrong type is the test


def test_a_zero_volume_bar_is_a_real_observation_not_a_gap() -> None:
    """Discarding it would shorten the series and move every rolling window over it."""
    bar = make_bar(base_volume="0", trade_count=0)
    assert bar.is_empty is True


def test_tick_reports_its_notional() -> None:
    tick = make_tick()
    assert tick.notional_quote == Decimal("32.0000000")
    assert tick.aggressor_side is Side.BUY


def test_tick_requires_a_venue_trade_id() -> None:
    """It is the only identifier both sides of a reconciliation agree on."""
    with pytest.raises(DomainError, match="venue_trade_id must not be blank"):
        Tick(
            instrument=BTCUSDT,
            venue_trade_id="",
            event_time_utc=EPOCH,
            quote_price=Decimal("64000"),
            base_quantity=Decimal("0.001"),
            aggressor_side=Side.SELL,
        )
