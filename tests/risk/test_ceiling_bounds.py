"""Risk limits are configuration bounded in both directions.

Both directions are asserted for every bound, derived from `HARD_CEILINGS` and
`HARD_FLOORS` themselves -- so a bound added without a test cannot happen, and a bound
added for a field `RiskLimits` does not carry fails immediately rather than silently
constraining nothing.

The floor half is the reason this file exists. A single mapping validated with
`if value > bound: raise` is correct for every ceiling and backwards for every floor,
and the backwards case does not raise: it accepts `min_free_margin_ratio = 0` and reads
as a passing config check.

`RISK_PHILOSOPHY.md` section 9, `CONFIGURATION.md` section 8.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import replace
from decimal import Decimal
from typing import Final, Literal

import pytest

from fking.risk.ceilings import (
    HARD_CEILINGS,
    HARD_FLOORS,
    Ceiling,
    Floor,
    assert_above_floors,
    assert_within_ceilings,
)
from fking.risk.limits import GATE_FIELDS, RiskLimits

pytestmark = pytest.mark.unit

CEILING_NAMES: Final[tuple[str, ...]] = tuple(sorted(HARD_CEILINGS))
FLOOR_NAMES: Final[tuple[str, ...]] = tuple(sorted(HARD_FLOORS))

# The limits `RiskLimits` stores as `int`. A Decimal override on one of these is coerced
# by the constructor, so a test that wants an exact value has to hand over an int.
INTEGER_FIELDS: Final[frozenset[str]] = frozenset(
    {"max_open_positions", "max_orders_per_minute", "min_trades_for_kelly"}
)


def _from_values(
    values: Mapping[str, Decimal],
    *,
    kill_switch_enabled: Literal[True] = True,
    require_invalidation_level: Literal[True] = True,
) -> RiskLimits:
    """Build from a full mapping of bounded values, coercing the integer-typed limits.

    Written out field by field rather than splatted through `**`, because `mypy --strict`
    rejects a `**dict[str, Decimal]` against a signature holding `int` and `Literal[True]`
    fields -- which is the type system working, not an obstacle to route around.
    """
    return RiskLimits(
        max_portfolio_notional_usd=values["max_portfolio_notional_usd"],
        max_position_notional_usd=values["max_position_notional_usd"],
        max_leverage=values["max_leverage"],
        max_daily_drawdown_ratio=values["max_daily_drawdown_ratio"],
        max_total_drawdown_ratio=values["max_total_drawdown_ratio"],
        max_open_positions=int(values["max_open_positions"]),
        max_orders_per_minute=int(values["max_orders_per_minute"]),
        max_single_order_notional_usd=values["max_single_order_notional_usd"],
        max_correlated_exposure_ratio=values["max_correlated_exposure_ratio"],
        min_free_margin_ratio=values["min_free_margin_ratio"],
        min_trades_for_kelly=int(values["min_trades_for_kelly"]),
        conviction_floor=values["conviction_floor"],
        kill_switch_enabled=kill_switch_enabled,
        require_invalidation_level=require_invalidation_level,
    )


def _override(name: str, wanted: Decimal) -> RiskLimits:
    """Default limits with exactly one bounded field replaced."""
    values = dict(RiskLimits().bounded_values())
    values[name] = wanted
    return _from_values(values)


def _at_every_bound() -> RiskLimits:
    """Every bounded limit sitting exactly on its own bound, all at once.

    One field at a time is not enough to prove the bounds are mutually adoptable: a set
    of bounds that cannot all be reached simultaneously documents a configuration nobody
    can actually run, and nothing else would say so.
    """
    exact = {name: ceiling.bound for name, ceiling in HARD_CEILINGS.items()}
    exact.update({name: floor.bound for name, floor in HARD_FLOORS.items()})
    return _from_values(exact)


# ---------------------------------------------------------------------------
# the bound tables themselves
# ---------------------------------------------------------------------------


def test_ceilings_and_floors_name_disjoint_fields() -> None:
    """A limit bounded in both directions would have one of the two bounds ignored by
    whichever validator ran second, and nothing would report which."""
    assert set(HARD_CEILINGS) & set(HARD_FLOORS) == set()


@pytest.mark.parametrize("name", CEILING_NAMES + FLOOR_NAMES)
def test_every_bound_names_a_field_that_is_actually_submitted(name: str) -> None:
    """A bound on a field nobody reports constrains nothing and nothing says so."""
    assert name in RiskLimits().bounded_values()


def test_ceilings_are_not_mutable_at_runtime() -> None:
    """A dict here would let a test -- or generated code -- widen a ceiling in-process,
    and a ceiling that can be raised at runtime is not a ceiling."""
    with pytest.raises(TypeError):
        HARD_CEILINGS["max_leverage"] = Ceiling(Decimal("1000"))  # type: ignore[index]  # the point of the test


def test_floors_are_not_mutable_at_runtime() -> None:
    with pytest.raises(TypeError):
        HARD_FLOORS["min_free_margin_ratio"] = Floor(Decimal("0"))  # type: ignore[index]  # the point of the test


# ---------------------------------------------------------------------------
# ceilings: larger is riskier
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("name", CEILING_NAMES)
def test_shipped_default_is_at_or_below_its_ceiling(name: str) -> None:
    """If the default is illegal the safe baseline is not safe."""
    assert RiskLimits().bounded_values()[name] <= HARD_CEILINGS[name].bound


@pytest.mark.parametrize("name", CEILING_NAMES)
def test_a_limit_exactly_at_its_ceiling_is_accepted(name: str) -> None:
    """The ceiling is inclusive. An exclusive bound makes the documented number
    unreachable, and the first person to hit that reads it as an off-by-one to fix."""
    assert _at_every_bound().bounded_values()[name] == HARD_CEILINGS[name].bound


@pytest.mark.parametrize("name", CEILING_NAMES)
def test_twice_the_ceiling_refuses_and_names_both_numbers(name: str) -> None:
    """Both the requested value and the bound, because a message carrying only one of
    them cannot tell the reader whether they misread the limit or the limit moved."""
    ceiling = HARD_CEILINGS[name].bound
    requested = ceiling * 2
    with pytest.raises(ValueError, match="hard ceiling") as raised:
        _override(name, requested)
    rendered = str(raised.value)
    assert name in rendered
    assert str(requested) in rendered
    assert str(ceiling) in rendered
    assert "safety:critical" in rendered


@pytest.mark.parametrize("name", CEILING_NAMES)
def test_tightening_below_a_ceiling_is_always_allowed(name: str) -> None:
    tightened = HARD_CEILINGS[name].bound / 2
    assert _override(name, tightened).bounded_values()[name] == (
        Decimal(int(tightened)) if name in INTEGER_FIELDS else tightened
    )


# ---------------------------------------------------------------------------
# floors: smaller is riskier -- the half a single mapping gets backwards
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("name", FLOOR_NAMES)
def test_shipped_default_is_at_or_above_its_floor(name: str) -> None:
    assert RiskLimits().bounded_values()[name] >= HARD_FLOORS[name].bound


@pytest.mark.parametrize("name", FLOOR_NAMES)
def test_a_limit_exactly_at_its_floor_is_accepted(name: str) -> None:
    assert _at_every_bound().bounded_values()[name] == HARD_FLOORS[name].bound


@pytest.mark.parametrize("name", FLOOR_NAMES)
def test_half_the_floor_refuses_and_names_both_numbers(name: str) -> None:
    floor = HARD_FLOORS[name].bound
    requested = floor / 2
    with pytest.raises(ValueError, match="hard floor") as raised:
        _override(name, requested)
    rendered = str(raised.value)
    assert name in rendered
    assert str(int(requested) if name in INTEGER_FIELDS else requested) in rendered
    assert str(floor) in rendered
    assert "safety:critical" in rendered


@pytest.mark.parametrize("name", FLOOR_NAMES)
def test_tightening_above_a_floor_is_always_allowed(name: str) -> None:
    """For a floor, tightening means raising it. Configuration may always be more
    conservative, including to the point where nothing trades."""
    tightened = HARD_FLOORS[name].bound * 2
    assert _override(name, tightened).bounded_values()[name] == tightened


def test_zero_free_margin_is_refused() -> None:
    """The exact case a single-mapping validator accepts.

    `0 > 0.25` is False, so a `value > bound` loop lets this through and reports a
    passing configuration check while authorising trading with no margin buffer at all.
    """
    with pytest.raises(ValueError, match="min_free_margin_ratio=0 is below") as raised:
        _override("min_free_margin_ratio", Decimal("0"))
    assert str(HARD_FLOORS["min_free_margin_ratio"].bound) in str(raised.value)


# ---------------------------------------------------------------------------
# refusal semantics
# ---------------------------------------------------------------------------


def test_an_out_of_bounds_limit_is_refused_rather_than_clamped() -> None:
    """`min(configured, ceiling)` would leave the system safe, the author wrong, and
    nobody informed -- and the author makes the next decision on the belief they hold.
    Nothing is constructed here at all; the only outcome is the exception.
    """
    ceiling = HARD_CEILINGS["max_position_notional_usd"].bound
    with pytest.raises(ValueError, match="hard ceiling"):
        _override("max_position_notional_usd", ceiling * 2)


def test_every_breach_is_reported_in_one_message() -> None:
    """Fixing one limit and being told about the next on the following boot turns one
    misconfiguration into three restarts."""
    with pytest.raises(ValueError, match="hard ceiling") as raised:
        replace(
            RiskLimits(),
            max_leverage=HARD_CEILINGS["max_leverage"].bound * 3,
            max_open_positions=int(HARD_CEILINGS["max_open_positions"].bound) * 3,
        )
    rendered = str(raised.value)
    assert "max_leverage" in rendered
    assert "max_open_positions" in rendered


def test_a_floor_breach_is_refused_even_when_every_ceiling_is_satisfied() -> None:
    """The failure a ceiling-only validator cannot see, demonstrated directly.

    Every `value > bound` check passes on this submission, so the version of this
    system that shipped one mapping and one loop would have accepted it -- and it
    authorises trading on a 1% margin buffer.
    """
    submitted = dict(RiskLimits().bounded_values())
    submitted["min_free_margin_ratio"] = Decimal("0.01")

    assert_within_ceilings(submitted, HARD_CEILINGS, scope="risk")

    with pytest.raises(ValueError, match="hard floor"):
        assert_above_floors(submitted, HARD_FLOORS, scope="risk")


def test_a_bound_whose_field_was_not_submitted_is_refused() -> None:
    """The shape a bound takes after the field it guarded is renamed: still in the
    mapping, guarding nothing, reported by nothing."""
    with pytest.raises(ValueError, match="constrains nothing"):
        assert_within_ceilings({}, HARD_CEILINGS, scope="risk")


def test_an_unsubmitted_floor_is_refused_too() -> None:
    with pytest.raises(ValueError, match="constrains nothing"):
        assert_above_floors({}, HARD_FLOORS, scope="risk")


def test_an_empty_bound_set_accepts_anything() -> None:
    """The validators refuse missing fields, not extra ones: a caller reporting more
    than the mapping bounds is normal, because the two mappings are checked separately
    against one submission."""
    assert_within_ceilings({"max_leverage": Decimal("1000")}, {}, scope="risk")
    assert_above_floors({"conviction_floor": Decimal("0")}, {}, scope="risk")


# ---------------------------------------------------------------------------
# gates
# ---------------------------------------------------------------------------


def test_every_declared_gate_is_a_field_of_the_model() -> None:
    """A gate named in `GATE_FIELDS` but absent from the model would be checked against
    an `AttributeError` rather than against its value."""
    for field_name in GATE_FIELDS:
        assert hasattr(RiskLimits(), field_name)


def test_the_kill_switch_cannot_be_switched_off_at_runtime() -> None:
    """`Literal[True]` stops a typed caller; this stops the untyped one -- a value
    arriving from an environment variable or a JSON payload carries no static type at
    all, so the runtime check is not redundant with the annotation.

    CLAUDE.md section 11: gates exist because someone will be in a hurry later.
    """
    with pytest.raises(ValueError, match="not configurable"):
        # Deliberately ill-typed. tests/risk/test_ceiling_types.py asserts that mypy
        # rejects this same expression; here it must also fail at runtime.
        _from_values(RiskLimits().bounded_values(), kill_switch_enabled=False)  # type: ignore[arg-type]


def test_the_invalidation_requirement_cannot_be_switched_off_at_runtime() -> None:
    with pytest.raises(ValueError, match="not configurable"):
        _from_values(RiskLimits().bounded_values(), require_invalidation_level=False)  # type: ignore[arg-type]


def test_both_gates_are_engaged_by_default() -> None:
    limits = RiskLimits()
    assert limits.kill_switch_enabled is True
    assert limits.require_invalidation_level is True


# ---------------------------------------------------------------------------
# the bound types
# ---------------------------------------------------------------------------


def test_ceiling_treats_equality_as_legal_and_anything_above_as_a_breach() -> None:
    ceiling = Ceiling(Decimal("3"))
    assert ceiling.is_exceeded_by(Decimal("3.0000000001"))
    assert not ceiling.is_exceeded_by(Decimal("3"))
    assert not ceiling.is_exceeded_by(Decimal("0"))


def test_floor_treats_equality_as_legal_and_anything_below_as_a_breach() -> None:
    floor = Floor(Decimal("0.25"))
    assert floor.is_undercut_by(Decimal("0.2499999999"))
    assert not floor.is_undercut_by(Decimal("0.25"))
    assert not floor.is_undercut_by(Decimal("100"))


def test_bounds_are_frozen() -> None:
    """A bound whose value can be reassigned in-process is a suggestion."""
    with pytest.raises(AttributeError):
        Ceiling(Decimal("3")).bound = Decimal("9")  # type: ignore[misc]  # the point of the test


def test_limits_are_frozen() -> None:
    """A risk limit another module can mutate after construction is a limit whose value
    depends on evaluation order, and the decision it produced stops being replayable."""
    with pytest.raises(AttributeError):
        RiskLimits().max_leverage = Decimal("100")  # type: ignore[misc]  # the point of the test
