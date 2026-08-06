"""The guarantee, stated as a biconditional over arbitrary generated configurations.

`RISK_PHILOSOPHY.md` section 9 names this file specifically and calls it the actual
guarantee -- the validator is merely how it is currently satisfied. So it is written to
survive a rewrite of the validator: it asserts against an oracle computed here, from
plain `Decimal` comparisons, never by calling the production `is_exceeded_by` /
`is_undercut_by` predicates. A property test that reuses the implementation's own
comparison proves the implementation agrees with itself.

Both halves matter and they fail differently:

- **Accepted implies within every bound.** A hole here is a configuration that got
  through, which is the failure with money attached.
- **Rejected implies at least one bound was crossed.** A hole here is a validator that
  refuses for a reason nobody declared, which is how a bound acquires a second,
  undocumented meaning. It is also what keeps `RiskLimits` free of cross-field rules:
  the moment one is added, this half stops holding, and it will say so.

Generation is confined to non-negative values. Zero and below are more conservative
than any bound -- nothing trades under a limit of zero -- and `RiskLimits` deliberately
permits them, so including them would exercise the same accept branch with no new
information. `tests/risk/test_ceiling_bounds.py` covers the boundary values by example.
"""

from __future__ import annotations

from dataclasses import replace
from decimal import Decimal
from typing import Final

import pytest
from hypothesis import given
from hypothesis import strategies as st

from fking.risk.ceilings import HARD_CEILINGS, HARD_FLOORS
from fking.risk.limits import RiskLimits

pytestmark = [pytest.mark.property, pytest.mark.unit]

# The limits `RiskLimits` stores as `int`. A generated Decimal is truncated on the way
# in, so the oracle has to compare against the truncated value rather than the drawn
# one -- otherwise a draw of 10.4 against a ceiling of 10 is scored as a breach the
# constructor never saw.
INTEGER_FIELDS: Final[frozenset[str]] = frozenset(
    {"max_open_positions", "max_orders_per_minute", "min_trades_for_kelly"}
)

BOUND_FOR: Final[dict[str, Decimal]] = {
    **{name: ceiling.bound for name, ceiling in HARD_CEILINGS.items()},
    **{name: floor.bound for name, floor in HARD_FLOORS.items()},
}


def _configurations() -> st.SearchStrategy[dict[str, Decimal]]:
    """One drawn value per bounded field, spanning both sides of its own bound.

    Each field is drawn over `[0, 2 * bound]` with two decimal places, so the exact
    bound is a value Hypothesis can and does hit rather than one it approaches -- and
    the inclusive-boundary behaviour is exercised by the same property that exercises
    the breaches.
    """
    return st.fixed_dictionaries(
        {
            name: st.decimals(
                min_value=Decimal("0"),
                max_value=bound * 2,
                places=2,
                allow_nan=False,
                allow_infinity=False,
            )
            for name, bound in BOUND_FOR.items()
        }
    )


def _as_stored(drawn: dict[str, Decimal]) -> dict[str, Decimal]:
    """The values `RiskLimits` will actually hold, after integer coercion."""
    return {
        name: Decimal(int(value)) if name in INTEGER_FIELDS else value
        for name, value in drawn.items()
    }


def _crossed_bounds(stored: dict[str, Decimal]) -> list[str]:
    """The oracle. Plain comparisons, written out in both directions on purpose."""
    breached = [name for name, ceiling in HARD_CEILINGS.items() if stored[name] > ceiling.bound]
    breached += [name for name, floor in HARD_FLOORS.items() if stored[name] < floor.bound]
    return sorted(breached)


def _build(drawn: dict[str, Decimal]) -> RiskLimits:
    """Construct from a drawn configuration, coercing the integer-typed limits.

    Written out field by field rather than splatted, so that adding a bounded field to
    `RiskLimits` without adding it here is a type error rather than a silently
    unexercised field.
    """
    return replace(
        RiskLimits(),
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


def _built_or_refused(drawn: dict[str, Decimal]) -> tuple[RiskLimits | None, str]:
    """Construct, or report the refusal message.

    Separated from the property so that the assertions about a refusal happen outside
    the `except` block: an assertion inside one turns a genuine construction failure
    into a confusing report about the assertion instead.
    """
    try:
        built = _build(drawn)
    except ValueError as refused:
        return None, str(refused)
    return built, ""


def test_every_bounded_field_is_generated() -> None:
    """The generator is derived from the bound tables, so a new bound is covered the
    moment it exists -- but a bound whose field `_build` forgets would be drawn and
    then dropped, and the property would pass having tested a default."""
    assert set(BOUND_FOR) == set(RiskLimits().bounded_values())


@given(drawn=_configurations())
def test_acceptance_is_exactly_conformance_to_every_bound(drawn: dict[str, Decimal]) -> None:
    stored = _as_stored(drawn)
    crossed = _crossed_bounds(stored)

    limits, refusal = _built_or_refused(drawn)

    if limits is None:
        assert crossed, f"refused a configuration that crosses no bound: {refusal}"
        # The refusal names what it refused. A message that does not is a message that
        # sends the reader to the wrong field.
        assert any(name in refusal for name in crossed)
        return

    assert crossed == [], f"accepted a configuration crossing {crossed}"

    # The accepted object really holds what it was given, and really conforms. Asserting
    # only "no exception" would pass against a validator that silently clamped.
    submitted = limits.bounded_values()
    for name, ceiling in HARD_CEILINGS.items():
        assert submitted[name] == stored[name]
        assert submitted[name] <= ceiling.bound
    for name, floor in HARD_FLOORS.items():
        assert submitted[name] == stored[name]
        assert submitted[name] >= floor.bound


@given(drawn=_configurations())
def test_a_conforming_configuration_stays_conforming_when_every_limit_is_tightened(
    drawn: dict[str, Decimal],
) -> None:
    """Tightening is free, in both directions of "tighter".

    Halving every ceiling-governed limit and doubling every floor-governed one is
    strictly more conservative, so it must be accepted whenever the original was. A
    validator that ever refused a tightening would make the safe move the expensive one.
    """
    stored = _as_stored(drawn)
    if _crossed_bounds(stored):
        return

    tightened = _build(
        {name: value / 2 if name in HARD_CEILINGS else value * 2 for name, value in drawn.items()}
    )
    submitted = tightened.bounded_values()
    for name, ceiling in HARD_CEILINGS.items():
        assert submitted[name] <= ceiling.bound
    for name, floor in HARD_FLOORS.items():
        assert submitted[name] >= floor.bound
