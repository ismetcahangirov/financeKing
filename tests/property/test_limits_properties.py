"""Properties of `RiskLimits` as an object, rather than of the bounds it enforces.

The acceptance biconditional -- accepted if and only if within every ceiling and above
every floor -- lives in `tests/risk/test_limits_property.py`, which `RISK_PHILOSOPHY.md`
section 9 names by path. This file asserts the properties that make that one meaningful:
that an accepted configuration is stored unmodified rather than clamped, that the object
cannot be edited afterwards, and that the safe direction is always reachable.

Those are separable failures. A validator could satisfy the biconditional perfectly and
still hand back an object whose `max_leverage` had been quietly reduced to the ceiling,
or one that a later module could raise back above it.

`docs/rules/testing-rules.md` clause 2: property tests are mandatory for every
function in `fking.risk`.
"""

from __future__ import annotations

import dataclasses
from collections.abc import Mapping
from dataclasses import replace
from decimal import Decimal
from typing import Final

import pytest
from hypothesis import given
from hypothesis import strategies as st

from fking.risk.ceilings import HARD_CEILINGS, HARD_FLOORS
from fking.risk.limits import GATE_FIELDS, RiskLimits

pytestmark = [pytest.mark.property, pytest.mark.unit]

# The limits the model stores as `int`. A Decimal handed to one of these is truncated on
# the way in, so any assertion about "stored exactly as submitted" has to compare against
# the truncated value rather than the drawn one.
INTEGER_FIELDS: Final[frozenset[str]] = frozenset(
    {"max_open_positions", "max_orders_per_minute", "min_trades_for_kelly"}
)

# Conformance by construction: ceiling-governed fields drawn at or below their bound,
# floor-governed fields at or above theirs. Every draw is therefore a configuration the
# model must accept, which is what lets these properties assert on the accepted object
# instead of spending most draws on the refusal path.
_CONFORMING: Final[st.SearchStrategy[dict[str, Decimal]]] = st.fixed_dictionaries(
    {
        **{
            # From zero, not from one: several ceilings are ratios below 1, and a limit
            # at zero halts trading, which is the most conservative configuration
            # available and one the model deliberately accepts.
            name: st.decimals(
                min_value=Decimal("0"),
                max_value=ceiling.bound,
                places=2,
                allow_nan=False,
                allow_infinity=False,
            )
            for name, ceiling in HARD_CEILINGS.items()
        },
        **{
            name: st.decimals(
                min_value=floor.bound,
                max_value=floor.bound * 3,
                places=2,
                allow_nan=False,
                allow_infinity=False,
            )
            for name, floor in HARD_FLOORS.items()
        },
    }
)


def _build(drawn: Mapping[str, Decimal]) -> RiskLimits:
    """Construct from a drawn configuration, coercing the integer-typed limits.

    Written out field by field rather than splatted through `**`: a bounded field added
    to `RiskLimits` without being added here is then a type error rather than a field
    that silently keeps its default through every property in this module.
    """
    return RiskLimits(
        max_portfolio_notional_usd=drawn["max_portfolio_notional_usd"],
        max_position_notional_usd=drawn["max_position_notional_usd"],
        max_leverage=drawn["max_leverage"],
        max_daily_drawdown_ratio=drawn["max_daily_drawdown_ratio"],
        max_total_drawdown_ratio=drawn["max_total_drawdown_ratio"],
        max_open_positions=int(drawn["max_open_positions"]),
        max_orders_per_minute=int(drawn["max_orders_per_minute"]),
        max_single_order_notional_usd=drawn["max_single_order_notional_usd"],
        max_correlated_exposure_ratio=drawn["max_correlated_exposure_ratio"],
        min_free_margin_ratio=drawn["min_free_margin_ratio"],
        min_trades_for_kelly=int(drawn["min_trades_for_kelly"]),
        conviction_floor=drawn["conviction_floor"],
    )


def _as_stored(drawn: Mapping[str, Decimal]) -> dict[str, Decimal]:
    return {
        name: Decimal(int(value)) if name in INTEGER_FIELDS else value
        for name, value in drawn.items()
    }


@given(drawn=_CONFORMING)
def test_an_accepted_configuration_is_stored_exactly_as_submitted(
    drawn: Mapping[str, Decimal],
) -> None:
    """Refuse, never clamp -- asserted on the object rather than on the exception.

    `min(configured, ceiling)` satisfies every acceptance test ever written, because it
    never raises. It is caught only by comparing what came back against what went in.
    """
    assert _build(drawn).bounded_values() == _as_stored(drawn)


@given(drawn=_CONFORMING)
def test_the_bounded_view_is_not_writable(drawn: Mapping[str, Decimal]) -> None:
    """The mapping the validators read is the mapping a caller could otherwise edit
    between construction and the next check."""
    with pytest.raises(TypeError):
        _build(drawn).bounded_values()["max_leverage"] = Decimal("1000")  # type: ignore[index]  # the point of the test


@given(drawn=_CONFORMING)
def test_the_bounded_view_covers_every_bound_and_nothing_else(
    drawn: Mapping[str, Decimal],
) -> None:
    """A field in the view with no bound is unconstrained and reads as checked; a bound
    with no field in the view constrains nothing and reads the same way."""
    assert set(_build(drawn).bounded_values()) == {*HARD_CEILINGS, *HARD_FLOORS}


@given(drawn=_CONFORMING)
def test_both_gates_survive_every_accepted_configuration(
    drawn: Mapping[str, Decimal],
) -> None:
    """There is no combination of limit values that switches a gate off as a side
    effect. A gate is not configurable, so it is also not reachable indirectly."""
    limits = _build(drawn)
    for field_name in GATE_FIELDS:
        assert getattr(limits, field_name) is True


@given(drawn=_CONFORMING)
def test_replacing_a_limit_leaves_the_original_object_untouched(
    drawn: Mapping[str, Decimal],
) -> None:
    """A risk limit another module can mutate is a limit whose value depends on
    evaluation order, and the decision it produced stops being replayable."""
    original = _build(drawn)
    before = dataclasses.asdict(original)

    tightened = replace(original, max_leverage=original.max_leverage / 2)

    assert dataclasses.asdict(original) == before
    assert tightened.max_leverage == original.max_leverage / 2


@given(drawn=_CONFORMING, divisor=st.integers(min_value=1, max_value=8))
def test_halving_every_ceiling_limit_is_always_accepted(
    drawn: Mapping[str, Decimal], divisor: int
) -> None:
    """Configuration may always make this system more conservative -- including to the
    point where nothing trades, which is the most conservative setting available and
    must not be mistaken for an invalid one."""
    scaled = dict(drawn)
    for name in HARD_CEILINGS:
        scaled[name] = drawn[name] / divisor

    submitted = _build(scaled).bounded_values()
    for name, ceiling in HARD_CEILINGS.items():
        assert submitted[name] <= ceiling.bound
