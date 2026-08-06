"""The declared invalidation rule, which is the denominator of every position it sizes.

`RISK_PHILOSOPHY.md` section 3.1 sizes a position as
`q = (r_used * E) / |P_entry - P_invalidation|`. Two consequences the tests below make
mechanical: a level that snaps *inward* silently enlarges the position, and a level a
strategy chose freely is strategy-side sizing under another name.
"""

from __future__ import annotations

from decimal import Decimal

import pytest

from fking.domain import Direction
from fking.strategy import InvalidationRule, StrategyContractError
from tests.strategy.harness import BTCUSDT

pytestmark = pytest.mark.unit

_ONE_PERCENT = InvalidationRule(adverse_move_fraction=Decimal("0.01"))


def test_a_long_is_invalidated_below_the_reference_price() -> None:
    level = _ONE_PERCENT.level_for(
        direction=Direction.LONG,
        reference_quote_price=Decimal("64000.00"),
        instrument=BTCUSDT,
    )

    assert level == Decimal("63360.00")


def test_a_short_is_invalidated_above_the_reference_price() -> None:
    level = _ONE_PERCENT.level_for(
        direction=Direction.SHORT,
        reference_quote_price=Decimal("64000.00"),
        instrument=BTCUSDT,
    )

    assert level == Decimal("64640.00")


@pytest.mark.parametrize(
    ("direction", "reference"),
    [
        (Direction.LONG, Decimal("64000.005")),
        (Direction.SHORT, Decimal("64000.005")),
        (Direction.LONG, Decimal("31337.77")),
        (Direction.SHORT, Decimal("31337.77")),
    ],
)
def test_the_level_is_snapped_away_from_the_reference_never_toward_it(
    direction: Direction, reference: Decimal
) -> None:
    """Snapping inward would tighten the stop by up to one tick without the strategy
    having said so, which enlarges the position on exactly the instruments whose tick is
    coarsest relative to their price."""
    level = _ONE_PERCENT.level_for(
        direction=direction, reference_quote_price=reference, instrument=BTCUSDT
    )
    exact = reference * (
        Decimal("1") - _ONE_PERCENT.adverse_move_fraction
        if direction is Direction.LONG
        else Decimal("1") + _ONE_PERCENT.adverse_move_fraction
    )

    assert level % BTCUSDT.tick_size == 0
    if direction is Direction.LONG:
        assert level <= exact
    else:
        assert level >= exact


def test_a_flat_direction_has_no_invalidation_level() -> None:
    """Flat asserts nothing, so there is nothing to invalidate -- and `Signal` refuses one
    on a flat direction for the same reason."""
    with pytest.raises(StrategyContractError, match="flat signal asserts nothing"):
        _ONE_PERCENT.level_for(
            direction=Direction.FLAT,
            reference_quote_price=Decimal("64000.00"),
            instrument=BTCUSDT,
        )


def test_a_non_positive_reference_price_is_refused() -> None:
    with pytest.raises(StrategyContractError, match="must be positive"):
        _ONE_PERCENT.level_for(
            direction=Direction.LONG,
            reference_quote_price=Decimal("0"),
            instrument=BTCUSDT,
        )


def test_a_reference_price_finer_than_the_tick_is_refused_rather_than_snapped_to_zero() -> None:
    """A level of zero is never reached, so the thesis could never be proved wrong."""
    with pytest.raises(StrategyContractError, match="coarser than the whole invalidation"):
        _ONE_PERCENT.level_for(
            direction=Direction.LONG,
            reference_quote_price=Decimal("0.005"),
            instrument=BTCUSDT,
        )


@pytest.mark.parametrize("fraction", ["0", "1", "1.5", "-0.01"])
def test_an_adverse_move_outside_the_open_unit_interval_is_refused(fraction: str) -> None:
    """Zero says the thesis is wrong the instant it is taken; one says a long is
    invalidated at a price of zero, which is never reached."""
    with pytest.raises(StrategyContractError, match="strictly between 0 and 1"):
        InvalidationRule(adverse_move_fraction=Decimal(fraction))


def test_an_adverse_move_declared_as_a_float_is_refused() -> None:
    """`Decimal(0.01)` is not one hundredth, and the error is baked in before any code
    here runs."""
    with pytest.raises(StrategyContractError, match="must be a Decimal"):
        InvalidationRule(adverse_move_fraction=0.01)  # type: ignore[arg-type]  # the point
