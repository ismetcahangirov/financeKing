"""The fixed-fractional term, and what a strategy pays for not stating its stop.

`q = (r_used * E) / |P_entry - P_invalidation|`. The worked numbers here are the ones a
reader can check by hand, which the property suite deliberately cannot give them: the
properties prove the relations hold everywhere and say nothing about whether the
constant is 2.5 or 25.

`RISK_PHILOSOPHY.md` section 3.1.
"""

from __future__ import annotations

from dataclasses import replace
from decimal import Decimal
from typing import Final

import pytest

from fking.domain import Direction, DomainError, Instrument, Venue
from fking.risk.sizing import SizingInputs, SizingParameters, size_position

pytestmark = pytest.mark.unit

BTCUSDT: Final = Instrument(
    venue=Venue.BINANCE_SPOT_TESTNET,
    symbol="BTCUSDT",
    base_asset="BTC",
    quote_asset="USDT",
    tick_size=Decimal("0.01"),
    lot_step=Decimal("0.00001"),
    min_notional_quote=Decimal("10"),
)

# Chosen so the fixed-fractional term binds and the arithmetic is checkable by hand:
# equity 100,000 at r = 0.005 is a 500 USDT risk budget, and a 1,000 USDT stop distance
# gives exactly 0.5 BTC. Every other term is set generously enough not to interfere.
STATED: Final = SizingInputs(
    instrument=BTCUSDT,
    direction=Direction.LONG,
    equity_usd=Decimal("100000"),
    entry_quote_price=Decimal("60000"),
    invalidation_quote_price=Decimal("59000"),
    atr_14_quote=Decimal("400"),
    return_series=(Decimal("0.01"), Decimal("-0.01"), Decimal("0.01")),
    volatility_floor_annualised=Decimal("0.20"),
    closed_trade_count=0,
    realised_mean_return_fraction=Decimal("0"),
    realised_return_stdev_fraction=Decimal("0"),
    permitted_notional_usd=Decimal("1000000"),
)


def test_a_stated_invalidation_level_sizes_the_risk_budget_over_the_stop_distance() -> None:
    sizing = size_position(STATED, SizingParameters())

    assert sizing.fixed_fractional_base_quantity == Decimal("0.5")
    assert sizing.risk_fraction_used == Decimal("0.005")
    assert sizing.used_invalidation_fallback is False


def test_an_unstated_invalidation_level_falls_back_to_atr_at_half_the_risk_fraction() -> None:
    """`2.5 * ATR_14` for the denominator *and* `r_used` halved on top of it.

    Two separate penalties, and both are load-bearing: the ATR distance is an estimate
    of a stop the strategy declined to name, and halving the fraction is what stops
    "decline to state a stop" from being the cheaper option.
    """
    unstated = replace(STATED, invalidation_quote_price=None)

    sizing = size_position(unstated, SizingParameters())

    # r_used = 0.005 * 0.5 = 0.0025 -> 250 USDT budget; 2.5 * 400 = 1,000 USDT stop.
    assert sizing.risk_fraction_used == Decimal("0.0025")
    assert sizing.fixed_fractional_base_quantity == Decimal("0.25")
    assert sizing.used_invalidation_fallback is True


def test_the_fallback_is_flagged_on_the_decision_record_not_only_in_the_quantity() -> None:
    """A quantity that is merely smaller is indistinguishable from a wider stated stop.

    `ARCHITECTURE.md` section 11 requires the decision to be reconstructable months
    later from the record alone, and "was this an estimate or a statement?" is exactly
    the question that separates a strategy that was disciplined from one that was
    charged for not being.
    """
    stated = size_position(STATED, SizingParameters())
    unstated = size_position(replace(STATED, invalidation_quote_price=None), SizingParameters())

    assert (stated.used_invalidation_fallback, unstated.used_invalidation_fallback) == (
        False,
        True,
    )
    assert unstated.risk_fraction_used < stated.risk_fraction_used


def test_a_wider_stated_stop_produces_a_smaller_position_at_the_same_conviction() -> None:
    """The budget is risk, not exposure, and this is the consequence that surprises.

    A high-conviction signal with a distant stop gets a *smaller* position than a
    low-conviction signal with a tight stop.
    """
    # A 500 USDT stop is 1 BTC on a 500 USDT budget; a 5,000 USDT stop is a tenth of it.
    tight = size_position(
        replace(STATED, invalidation_quote_price=Decimal("59500")), SizingParameters()
    )
    wide = size_position(
        replace(STATED, invalidation_quote_price=Decimal("55000")), SizingParameters()
    )

    assert tight.fixed_fractional_base_quantity == Decimal("1")
    assert wide.fixed_fractional_base_quantity == Decimal("0.1")


def test_an_invalidation_level_sitting_on_the_entry_sizes_zero_rather_than_dividing() -> None:
    """An unmeasurable distance to being wrong reads as no position, not an unbounded one."""
    degenerate = replace(STATED, invalidation_quote_price=STATED.entry_quote_price)

    sizing = size_position(degenerate, SizingParameters())

    assert sizing.fixed_fractional_base_quantity == Decimal("0")
    assert sizing.base_quantity == Decimal("0")
    assert sizing.binding_term == "venue_filters"


def test_an_unstated_level_with_no_atr_also_sizes_zero() -> None:
    unmeasurable = replace(STATED, invalidation_quote_price=None, atr_14_quote=Decimal("0"))

    assert size_position(unmeasurable, SizingParameters()).base_quantity == Decimal("0")


def test_sizing_a_flat_signal_is_refused_rather_than_answered_with_zero() -> None:
    with pytest.raises(DomainError, match="category error"):
        replace(STATED, direction=Direction.FLAT, invalidation_quote_price=None)
