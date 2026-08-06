"""The parameter space is what makes a mutation searchable and a lineage reconstructable.

The clause worth stating out loud: `bind` refuses an undeclared name rather than ignoring
it. A mutation that silently does nothing is worse than one that fails, because the trial
is charged, the child is scored, and the score belongs to the parent.
"""

from __future__ import annotations

from decimal import Decimal

import pytest

from fking.strategy import (
    DecimalParameter,
    IntegerParameter,
    ParameterSpace,
    StrategyContractError,
    decimal_parameter,
    integer_parameter,
)

pytestmark = pytest.mark.unit

_ENTRY = DecimalParameter(
    name="entry_return_fraction",
    default=Decimal("0.004"),
    minimum=Decimal("0.0005"),
    maximum=Decimal("0.05"),
)
_SPAN = IntegerParameter(name="conviction_span", default=3, minimum=1, maximum=10)
_SPACE = ParameterSpace((_ENTRY, _SPAN))
_DEFAULT_SPAN = 3


def test_binding_nothing_yields_every_declared_default() -> None:
    bound = _SPACE.bind()

    assert decimal_parameter(bound, "entry_return_fraction") == Decimal("0.004")
    assert integer_parameter(bound, "conviction_span") == _DEFAULT_SPAN


def test_an_override_inside_the_declared_bounds_is_accepted() -> None:
    bound = _SPACE.bind({"entry_return_fraction": Decimal("0.02")})

    assert decimal_parameter(bound, "entry_return_fraction") == Decimal("0.02")
    assert integer_parameter(bound, "conviction_span") == _DEFAULT_SPAN


def test_an_override_outside_the_declared_bounds_is_refused() -> None:
    """The bounds are the search space, and a point outside it is not a mutation."""
    with pytest.raises(StrategyContractError, match="outside its declared bounds"):
        _SPACE.bind({"entry_return_fraction": Decimal("0.5")})


def test_an_undeclared_parameter_name_is_refused_rather_than_ignored() -> None:
    with pytest.raises(StrategyContractError, match="not declared parameters"):
        _SPACE.bind({"lookback_bars": 20})


def test_a_decimal_parameter_given_an_integer_is_refused() -> None:
    """A mutation that changes a parameter's type is not a point in this space."""
    with pytest.raises(StrategyContractError, match="decimal parameter"):
        _SPACE.bind({"entry_return_fraction": 1})


def test_an_integer_parameter_given_a_decimal_is_refused() -> None:
    with pytest.raises(StrategyContractError, match="integer parameter"):
        _SPACE.bind({"conviction_span": Decimal("3")})


def test_an_integer_parameter_given_a_boolean_is_refused() -> None:
    """`bool` is an `int` subclass, so `True` would otherwise pass as the value 1."""
    with pytest.raises(StrategyContractError, match="integer parameter"):
        _SPACE.bind({"conviction_span": True})


def test_the_bound_mapping_cannot_be_mutated_by_its_holder() -> None:
    """A strategy that could rewrite its own bound point would replay differently from the
    spec hash that identifies it."""
    bound = _SPACE.bind()

    with pytest.raises(TypeError):
        bound["conviction_span"] = 9  # type: ignore[index]  # the point of the test


def test_a_parameter_with_no_room_between_its_bounds_is_refused() -> None:
    """A constant wearing a parameter's name tells the search there is a dimension to
    explore when there is not."""
    with pytest.raises(StrategyContractError, match="no room"):
        IntegerParameter(name="fixed", default=4, minimum=4, maximum=4)


def test_a_default_outside_its_own_bounds_is_refused() -> None:
    with pytest.raises(StrategyContractError, match="outside its declared bounds"):
        IntegerParameter(name="misdeclared", default=99, minimum=1, maximum=10)


def test_a_decimal_parameter_declared_with_a_float_is_refused() -> None:
    """`Decimal(0.1)` already carries 5.5e-18 of error before any code runs."""
    with pytest.raises(StrategyContractError, match="must be a Decimal"):
        DecimalParameter(
            name="entry_return_fraction",
            default=0.004,  # type: ignore[arg-type]  # the point of the test
            minimum=Decimal("0.001"),
            maximum=Decimal("0.05"),
        )


def test_an_integer_parameter_declared_with_a_decimal_is_refused() -> None:
    with pytest.raises(StrategyContractError, match="must be an int"):
        IntegerParameter(
            name="conviction_span",
            default=Decimal("3"),  # type: ignore[arg-type]  # the point of the test
            minimum=1,
            maximum=10,
        )


def test_a_blank_parameter_name_is_refused() -> None:
    with pytest.raises(StrategyContractError, match="name"):
        IntegerParameter(name="  ", default=3, minimum=1, maximum=10)


def test_declaring_one_dimension_twice_is_refused() -> None:
    """Two bounds for one dimension makes a mutation's legality depend on iteration order."""
    with pytest.raises(StrategyContractError, match="declared twice"):
        ParameterSpace(
            (_SPAN, IntegerParameter(name="conviction_span", default=5, minimum=2, maximum=9))
        )


def test_an_empty_space_is_legal_and_binds_to_nothing() -> None:
    """The strongest declaration there is: every number came from theory, so the trial
    ledger owes this strategy nothing."""
    assert dict(ParameterSpace().bind()) == {}
    assert ParameterSpace().declared_names == frozenset()


def test_reading_a_decimal_parameter_as_an_integer_is_refused() -> None:
    """The narrowing helpers fail loudly rather than letting a union leak into a body."""
    bound = _SPACE.bind()

    with pytest.raises(StrategyContractError, match="decimal parameter"):
        integer_parameter(bound, "entry_return_fraction")


def test_reading_an_integer_parameter_as_a_decimal_is_refused() -> None:
    bound = _SPACE.bind()

    with pytest.raises(StrategyContractError, match="integer parameter"):
        decimal_parameter(bound, "conviction_span")
