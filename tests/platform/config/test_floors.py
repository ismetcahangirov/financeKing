"""The other half of the bounded pattern: limits where smaller is riskier.

`test_ceilings.py` proves that a limit above its ceiling aborts startup. Every assertion
in it passes just as happily against a model that has no floors at all, because `0 >
0.25` is `False` -- a ceilings-only validator accepts `min_free_margin_ratio = 0`,
authorises trading with no margin buffer, and reports a passing configuration check.
That was the state of `RiskSettings` until issue #171: `conviction_floor` carried
`ge=0` against a compiled-in floor of 0.10, and two of the three floors had no
configuration surface at all.

Derived from `HARD_FLOORS` itself, so a floor added without a test cannot happen.

CONFIGURATION.md section 8, RISK_PHILOSOPHY.md section 9.
"""

from __future__ import annotations

from collections.abc import Mapping
from decimal import Decimal
from typing import Final, Protocol

import pytest
from hypothesis import given
from hypothesis import strategies as st
from pydantic import BaseModel, ValidationError

from fking.platform.config import HARD_CEILINGS, HARD_FLOORS, RiskSettings

# Imported by name rather than reached through a model, because the failure it guards
# against is a bound whose field no longer exists -- which no model can be built to
# demonstrate from outside.
from fking.platform.config.settings import _assert_above_floors, _assert_within_ceilings

pytestmark = pytest.mark.unit


class _BoundValidator(Protocol):
    """The shape both direction validators share. Positional-only, so the two differing
    parameter names (`ceilings`, `floors`) do not make them structurally distinct."""

    def __call__(
        self, model: BaseModel, bounds: Mapping[str, Decimal | int], /, *, scope: str
    ) -> None: ...


FLOOR_NAMES: Final[tuple[str, ...]] = tuple(sorted(HARD_FLOORS))

# The floors typed `Decimal` on the model. A non-integral draw against the one
# `int`-typed limit fails on the coercion, which is a different rejection than the one
# under test.
DECIMAL_FLOOR_NAMES: Final[tuple[str, ...]] = tuple(
    name for name in FLOOR_NAMES if RiskSettings.model_fields[name].annotation is Decimal
)


def _risk_with(name: str, override: Decimal) -> RiskSettings:
    """Default risk settings with exactly one limit replaced."""
    payload = RiskSettings().model_dump()
    payload[name] = override
    return RiskSettings.model_validate(payload)


def test_every_floor_names_a_real_risk_field() -> None:
    """A floor on a field nobody configures is a floor that bounds nothing.

    This is the assertion issue #171 would have failed: `min_free_margin_ratio` and
    `min_trades_for_kelly` were compiled-in floors with no configuration surface, so
    the operator could not tighten them and the config tree could not check them.
    """
    unknown = [name for name in FLOOR_NAMES if name not in RiskSettings.model_fields]
    assert unknown == []


def test_no_limit_is_bounded_in_both_directions() -> None:
    """A field in both mappings has two bounds that can close on each other until no
    value is legal, and the failure surfaces as a config file nobody can fix."""
    assert set(HARD_FLOORS) & set(HARD_CEILINGS) == set()


def test_floors_are_not_mutable_at_runtime() -> None:
    """A dict here would let a test -- or an agent -- lower a floor in-process."""
    with pytest.raises(TypeError):
        HARD_FLOORS["conviction_floor"] = Decimal("0")  # type: ignore[index]  # the point of the test


@pytest.mark.parametrize("name", FLOOR_NAMES)
def test_default_is_at_or_above_its_floor(name: str) -> None:
    """The shipped default must itself be legal, or the safe baseline is not safe."""
    assert Decimal(str(getattr(RiskSettings(), name))) >= HARD_FLOORS[name]


@pytest.mark.parametrize("name", FLOOR_NAMES)
def test_a_limit_exactly_at_its_floor_is_accepted(name: str) -> None:
    """The floor is inclusive. An exclusive bound makes the documented number
    unreachable, and the first person to hit that reads it as an off-by-one to fix."""
    assert Decimal(str(getattr(_risk_with(name, HARD_FLOORS[name]), name))) == HARD_FLOORS[name]


@pytest.mark.parametrize("name", FLOOR_NAMES)
def test_a_limit_below_its_floor_refuses_and_names_both_numbers(name: str) -> None:
    """Refuse, and say what was asked for as well as what is allowed.

    A refusal that names only the bound leaves the reader guessing which of twelve
    limits they got wrong; one that names only the submitted value tells them nothing
    about where to move it. Issue #171 acceptance criterion.
    """
    floor = HARD_FLOORS[name]
    submitted = floor / 2
    with pytest.raises(ValidationError) as raised:
        _risk_with(name, submitted)
    rendered = str(raised.value)
    assert name in rendered
    assert str(floor) in rendered
    assert str(submitted) in rendered
    assert "safety:critical" in rendered


@pytest.mark.parametrize("name", FLOOR_NAMES)
def test_raising_a_floor_is_always_allowed(name: str) -> None:
    """Configuration may only make the system more conservative, and for these limits
    conservative is upwards."""
    raised = HARD_FLOORS[name] * 2
    assert Decimal(str(getattr(_risk_with(name, raised), name))) == raised


@pytest.mark.parametrize("name", DECIMAL_FLOOR_NAMES)
@given(shortfall=st.decimals(min_value="0.000001", max_value="0.1", places=6))
def test_no_value_below_the_floor_is_accepted(name: str, shortfall: Decimal) -> None:
    """The example test proves one number. This proves the boundary has no holes."""
    with pytest.raises(ValidationError):
        _risk_with(name, HARD_FLOORS[name] - shortfall)


@pytest.mark.parametrize("validate", [_assert_above_floors, _assert_within_ceilings])
def test_a_bound_on_a_missing_field_refuses_rather_than_passing_silently(
    validate: _BoundValidator,
) -> None:
    """Both validators refuse a bound they cannot evaluate.

    This is the shape a bound takes the day after the field it guarded is renamed: the
    mapping still lists it, nothing reads it, and every configuration passes. Asserted
    for both directions because a guard added to one validator and not the other leaves
    exactly one half of the bounds able to go quiet.
    """
    with pytest.raises(ValueError, match="constrains nothing"):
        validate(RiskSettings(), {"conviction_ceiling": Decimal("1")}, scope="risk")


@given(shortfall=st.integers(min_value=1, max_value=100))
def test_no_trade_count_below_the_kelly_floor_is_accepted(shortfall: int) -> None:
    """The integer-typed floor, which the Decimal property above cannot reach.

    Below the floor the Kelly term is not merely noisy, it is omitted from the sizing
    `min()` entirely -- so a configuration that lowers it is asking for a Kelly fraction
    estimated from a record too short to estimate it from.
    """
    with pytest.raises(ValidationError):
        _risk_with("min_trades_for_kelly", HARD_FLOORS["min_trades_for_kelly"] - shortfall)
