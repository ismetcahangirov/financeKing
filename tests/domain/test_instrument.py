"""Instrument invariants and the exchange filter lattice."""

from __future__ import annotations

from decimal import Decimal

import pytest

from fking.domain import DomainError, Instrument, Side, Venue
from tests.support.domain_factory import BTCUSDT

pytestmark = pytest.mark.unit

# A step size that is not a power of ten. Binance really does publish these, and they
# are the case `Decimal.quantize` cannot express -- quantize snaps to a decimal
# exponent, and a value with two decimal places is off the 0.005 lattice half the time.
AWKWARD_STEP = Decimal("0.005")


def test_base_and_quote_asset_may_not_be_the_same() -> None:
    with pytest.raises(DomainError, match="both base and quote"):
        Instrument(
            venue=Venue.BINANCE_SPOT_TESTNET,
            symbol="BTCBTC",
            base_asset="BTC",
            quote_asset="BTC",
            tick_size=Decimal("0.01"),
            lot_step=Decimal("0.00001"),
            min_notional_quote=Decimal("10"),
        )


@pytest.mark.parametrize(
    ("tick_size", "lot_step", "min_notional_quote", "field_name"),
    [
        ("0", "0.00001", "10", "tick_size"),
        ("0.01", "0", "10", "lot_step"),
        ("0.01", "0.00001", "0", "min_notional_quote"),
    ],
)
def test_filters_must_be_positive(
    tick_size: str, lot_step: str, min_notional_quote: str, field_name: str
) -> None:
    """A zero filter would make quantization divide by zero on the first order."""
    with pytest.raises(DomainError, match=f"{field_name} must be positive"):
        Instrument(
            venue=Venue.BINANCE_SPOT_TESTNET,
            symbol="BTCUSDT",
            base_asset="BTC",
            quote_asset="USDT",
            tick_size=Decimal(tick_size),
            lot_step=Decimal(lot_step),
            min_notional_quote=Decimal(min_notional_quote),
        )


def test_a_non_ascii_symbol_is_held_verbatim() -> None:
    """Binance spot testnet serves a deliberate non-ASCII symbol in `exchangeInfo`.

    Refusing to hold it here would make the venue adapter unable to report what it
    actually received, and normalising it would change the bytes we must echo back.
    Deciding whether it is *tradable* is the adapter's job, not this type's.
    """
    symbol = "BTCüUSDT"
    instrument = Instrument(
        venue=Venue.BINANCE_SPOT_TESTNET,
        symbol=symbol,
        base_asset="BTC",
        quote_asset="USDT",
        tick_size=Decimal("0.01"),
        lot_step=Decimal("0.00001"),
        min_notional_quote=Decimal("10"),
    )
    assert instrument.symbol == symbol


@pytest.mark.parametrize(
    ("base_quantity", "expected"),
    [
        ("0.123456789", "0.12345"),
        ("0.00001", "0.00001"),
        ("0.000009", "0"),
        # Truncation toward zero, not floor: ROUND_FLOOR would take this to -0.12346
        # and make the short one step LARGER than the risk engine authorised.
        ("-0.123456789", "-0.12345"),
    ],
)
def test_quantize_base_quantity_truncates_toward_zero(base_quantity: str, expected: str) -> None:
    assert BTCUSDT.quantize_base_quantity(Decimal(base_quantity)) == Decimal(expected)


def test_quantize_base_quantity_handles_a_non_power_of_ten_step() -> None:
    instrument = Instrument(
        venue=Venue.BYBIT_TESTNET,
        symbol="AWKWARD",
        base_asset="AWK",
        quote_asset="USDT",
        tick_size=Decimal("0.01"),
        lot_step=AWKWARD_STEP,
        min_notional_quote=Decimal("5"),
    )
    assert instrument.quantize_base_quantity(Decimal("0.019")) == Decimal("0.015")
    assert instrument.quantize_base_quantity(Decimal("0.019")) % AWKWARD_STEP == Decimal("0")


@pytest.mark.parametrize(
    ("side", "expected"),
    [(Side.BUY, "63999.99"), (Side.SELL, "64000.00")],
)
def test_quantize_quote_price_never_crosses_further_into_the_book(
    side: Side, expected: str
) -> None:
    """A bid rounds down and an ask rounds up, so snapping never worsens the price."""
    assert BTCUSDT.quantize_quote_price(Decimal("63999.994"), side) == Decimal(expected)


def test_quantization_is_idempotent() -> None:
    once = BTCUSDT.quantize_base_quantity(Decimal("0.123456789"))
    assert BTCUSDT.quantize_base_quantity(once) == once


def test_min_notional_is_judged_on_magnitude() -> None:
    """A short of 0.001 BTC faces the same MIN_NOTIONAL filter as a long."""
    assert BTCUSDT.meets_min_notional(Decimal("0.001"), Decimal("64000")) is True
    assert BTCUSDT.meets_min_notional(Decimal("-0.001"), Decimal("64000")) is True
    assert BTCUSDT.meets_min_notional(Decimal("0.0001"), Decimal("64000")) is False
    assert BTCUSDT.notional_quote(Decimal("0.001"), Decimal("64000")) == Decimal("64.000")
