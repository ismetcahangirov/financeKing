"""Risk limits are configuration bounded by compiled-in ceilings.

The whole point of the pattern is directional: tightening a limit is free, loosening it
past the ceiling is impossible without a source edit and a `safety:critical` pull
request. Both directions are asserted here, for every ceiling, derived from
`HARD_CEILINGS` itself -- so a ceiling added without a test cannot happen.

CONFIGURATION.md section 8.
"""

from __future__ import annotations

from decimal import Decimal
from typing import Final

import pytest
from hypothesis import given
from hypothesis import strategies as st
from pydantic import ValidationError

from fking.platform.config import HARD_CEILINGS, RiskSettings

pytestmark = pytest.mark.unit

CEILING_NAMES: Final[tuple[str, ...]] = tuple(sorted(HARD_CEILINGS))

# The ceilings that are typed Decimal on the model. The two integer-typed limits are
# excluded from the property test because a non-integral draw fails on the int
# coercion, which is a different rejection than the one under test.
DECIMAL_CEILING_NAMES: Final[tuple[str, ...]] = tuple(
    name for name in CEILING_NAMES if RiskSettings.model_fields[name].annotation is Decimal
)


def _risk_with(name: str, override: Decimal) -> RiskSettings:
    """Default risk settings with exactly one limit replaced."""
    payload = RiskSettings().model_dump()
    payload[name] = override
    return RiskSettings.model_validate(payload)


def _risk_at_every_ceiling() -> RiskSettings:
    """Every limit at its ceiling at once.

    One field at a time will not do: `max_single_order_notional_usd`'s ceiling is above
    the *default* `max_position_notional_usd`, and the cross-field validator correctly
    refuses that combination. Raising them together is what proves the ceilings are
    mutually consistent -- a ceiling set that cannot all be reached simultaneously
    documents a configuration nobody can actually adopt.
    """
    payload = RiskSettings().model_dump()
    payload.update(HARD_CEILINGS)
    return RiskSettings.model_validate(payload)


def test_every_ceiling_names_a_real_risk_field() -> None:
    """A ceiling on a field that does not exist bounds nothing and nothing says so."""
    unknown = [name for name in CEILING_NAMES if name not in RiskSettings.model_fields]
    assert unknown == []


def test_ceilings_are_not_mutable_at_runtime() -> None:
    """A dict here would let a test -- or an agent -- widen a ceiling in-process."""
    with pytest.raises(TypeError):
        HARD_CEILINGS["max_leverage"] = Decimal("1000")  # type: ignore[index]  # the point of the test


@pytest.mark.parametrize("name", CEILING_NAMES)
def test_default_is_at_or_below_its_ceiling(name: str) -> None:
    """The shipped default must itself be legal, or the safe baseline is not safe."""
    assert Decimal(str(getattr(RiskSettings(), name))) <= HARD_CEILINGS[name]


@pytest.mark.parametrize("name", CEILING_NAMES)
def test_a_limit_exactly_at_its_ceiling_is_accepted(name: str) -> None:
    """The ceiling is inclusive. An exclusive bound would make the documented number
    unreachable, and the first person to hit that reads it as an off-by-one to fix."""
    assert Decimal(str(getattr(_risk_at_every_ceiling(), name))) == HARD_CEILINGS[name]


@pytest.mark.parametrize("name", CEILING_NAMES)
def test_a_limit_above_its_ceiling_refuses_and_names_the_field(name: str) -> None:
    ceiling = HARD_CEILINGS[name]
    with pytest.raises(ValidationError) as raised:
        _risk_with(name, ceiling * 2)
    rendered = str(raised.value)
    assert name in rendered
    assert str(ceiling) in rendered
    assert "safety:critical" in rendered


@pytest.mark.parametrize("name", CEILING_NAMES)
def test_tightening_a_limit_is_always_allowed(name: str) -> None:
    """Configuration may only make the system more conservative."""
    tightened = HARD_CEILINGS[name] / 2
    if RiskSettings.model_fields[name].annotation is int:
        tightened = tightened.to_integral_value()
    settings = _risk_with(name, tightened)
    assert Decimal(str(getattr(settings, name))) == tightened


@pytest.mark.parametrize("name", DECIMAL_CEILING_NAMES)
@given(excess=st.decimals(min_value="0.000001", max_value="1000", places=6))
def test_no_value_above_the_ceiling_is_accepted(name: str, excess: Decimal) -> None:
    """The example test proves one number. This proves the boundary has no holes."""
    with pytest.raises(ValidationError):
        _risk_with(name, HARD_CEILINGS[name] + excess)


@pytest.mark.parametrize(
    "field_name",
    ["kill_switch_enabled", "require_invalidation_level"],
)
def test_a_gate_cannot_be_switched_off_by_configuration(field_name: str) -> None:
    """CLAUDE.md section 11: adding a config flag to bypass a gate. Gates exist because
    someone will be in a hurry later, and that someone is you."""
    payload = RiskSettings().model_dump()
    payload[field_name] = False
    with pytest.raises(ValidationError):
        RiskSettings.model_validate(payload)
