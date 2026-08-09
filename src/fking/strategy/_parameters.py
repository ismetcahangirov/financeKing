"""The declared parameter space: every number a strategy is allowed to have chosen.

This is the clause that looks like ceremony and is not. The evolution engine mutates a
strategy by perturbing its declared parameters within their declared bounds
(`EVOLUTION_ENGINE.md`). A hard-coded `20` inside `evaluate()` is a parameter that was
chosen by a human, never charged to the global trial ledger, cannot be searched, and
cannot be reproduced from the specification alone. Lineage becomes a lie the moment one
of those exists: the child's spec differs from the parent's in the recorded fields and
is identical in the field that actually drove the difference in behaviour.

So the bounds are part of the declaration and not part of a docstring, and `bind()` is
the only way a strategy obtains a value. It refuses an undeclared name rather than
ignoring it, because the mutation that silently does nothing is worse than the one that
fails: the trial is charged, the child is scored, and the score is the parent's.

`minimum < maximum` is required. A parameter with no room is a constant wearing a
parameter's name, and declaring it as one tells the search there is a dimension to
explore when there is not.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from decimal import Decimal
from types import MappingProxyType

from fking.strategy._errors import StrategyContractError
from fking.strategy._guards import require_text

__all__ = [
    "DecimalParameter",
    "IntegerParameter",
    "Parameter",
    "ParameterSpace",
    "ParameterValue",
    "decimal_parameter",
    "integer_parameter",
]

# The two kinds a strategy may declare. Deliberately not `float`: a threshold compared
# against a price, a return or a fraction is money-adjacent arithmetic, and
# `docs/rules/decimal-and-money.md` allows `float` only inside statistical
# computation in `backtest` and `data`.
ParameterValue = Decimal | int


@dataclass(frozen=True, slots=True, kw_only=True)
class DecimalParameter:
    """A continuous searchable dimension, in exact decimal arithmetic."""

    name: str
    default: Decimal
    minimum: Decimal
    maximum: Decimal

    def __post_init__(self) -> None:
        require_text(self.name, "name")
        for field_name, candidate in (
            ("default", self.default),
            ("minimum", self.minimum),
            ("maximum", self.maximum),
        ):
            if not isinstance(candidate, Decimal):
                raise StrategyContractError(
                    f"{self.name}.{field_name} must be a Decimal constructed from a "
                    f"string; got {candidate!r}"
                )
        _require_room(self.name, self.minimum, self.maximum)
        _require_inside(self.name, self.default, self.minimum, self.maximum)

    def coerce(self, candidate: ParameterValue) -> Decimal:
        """Accept `candidate` as this parameter's value, or refuse it."""
        if not isinstance(candidate, Decimal):
            raise StrategyContractError(
                f"{self.name} is a decimal parameter and was given {candidate!r}; a "
                f"mutation that changes a parameter's type is not a point in this space"
            )
        _require_inside(self.name, candidate, self.minimum, self.maximum)
        return candidate


@dataclass(frozen=True, slots=True, kw_only=True)
class IntegerParameter:
    """A discrete searchable dimension -- a bar count, a window, a rank."""

    name: str
    default: int
    minimum: int
    maximum: int

    def __post_init__(self) -> None:
        require_text(self.name, "name")
        for field_name, candidate in (
            ("default", self.default),
            ("minimum", self.minimum),
            ("maximum", self.maximum),
        ):
            # `bool` is an `int` subclass, so `maximum=True` would otherwise declare an
            # upper bound of 1 and read as a flag somebody meant to set elsewhere.
            if isinstance(candidate, bool) or not isinstance(candidate, int):
                raise StrategyContractError(
                    f"{self.name}.{field_name} must be an int; got {candidate!r}"
                )
        _require_room(self.name, self.minimum, self.maximum)
        _require_inside(self.name, self.default, self.minimum, self.maximum)

    def coerce(self, candidate: ParameterValue) -> int:
        """Accept `candidate` as this parameter's value, or refuse it."""
        if isinstance(candidate, bool) or not isinstance(candidate, int):
            raise StrategyContractError(
                f"{self.name} is an integer parameter and was given {candidate!r}; a "
                f"mutation that changes a parameter's type is not a point in this space"
            )
        _require_inside(self.name, candidate, self.minimum, self.maximum)
        return candidate


Parameter = DecimalParameter | IntegerParameter


@dataclass(frozen=True, slots=True)
class ParameterSpace:
    """Every dimension one strategy declares, and the only way to obtain a value.

    An empty space is legal and is the strongest kind of declaration there is: it says
    every number in the strategy came from theory rather than from a search, so the
    trial ledger owes it nothing.
    """

    parameters: tuple[Parameter, ...] = ()

    def __post_init__(self) -> None:
        seen: set[str] = set()
        for parameter in self.parameters:
            if parameter.name in seen:
                raise StrategyContractError(
                    f"parameter {parameter.name!r} is declared twice; two bounds for one "
                    f"dimension means a mutation's legality depends on iteration order"
                )
            seen.add(parameter.name)

    @property
    def declared_names(self) -> frozenset[str]:
        """Every name this space accepts."""
        return frozenset(parameter.name for parameter in self.parameters)

    def bind(
        self, overrides: Mapping[str, ParameterValue] | None = None
    ) -> Mapping[str, ParameterValue]:
        """The full parameter mapping, defaults filled in and every value bounds-checked.

        Returns a `MappingProxyType`, so a strategy holding the result cannot mutate the
        point it was constructed at -- which is what makes a replay of the same spec hash
        a replay of the same strategy.
        """
        supplied = dict(overrides or {})
        undeclared = sorted(set(supplied) - self.declared_names)
        if undeclared:
            raise StrategyContractError(
                f"{undeclared} are not declared parameters; declared names are "
                f"{sorted(self.declared_names)}. A mutation naming an undeclared "
                f"parameter changes nothing while still being charged as a trial"
            )
        bound: dict[str, ParameterValue] = {}
        for parameter in self.parameters:
            candidate = supplied.get(parameter.name, parameter.default)
            bound[parameter.name] = parameter.coerce(candidate)
        return MappingProxyType(bound)


def decimal_parameter(bound: Mapping[str, ParameterValue], name: str) -> Decimal:
    """Read one bound value as a `Decimal`, narrowing the union at the one place it must.

    A strategy body reading `bound[name]` directly gets `Decimal | int`, and the branch
    somebody writes to satisfy the type checker is a branch nothing exercises. Narrowing
    once, here, keeps the strategy body free of type ceremony and makes a spec/strategy
    mismatch a loud failure at construction rather than a silent one at the first
    comparison.
    """
    candidate = bound[name]
    if not isinstance(candidate, Decimal):
        raise StrategyContractError(
            f"{name} was declared as an integer parameter and is read as a decimal; "
            f"got {candidate!r}"
        )
    return candidate


def integer_parameter(bound: Mapping[str, ParameterValue], name: str) -> int:
    """Read one bound value as an `int`. See `decimal_parameter`."""
    candidate = bound[name]
    if isinstance(candidate, bool) or not isinstance(candidate, int):
        raise StrategyContractError(
            f"{name} was declared as a decimal parameter and is read as an integer; "
            f"got {candidate!r}"
        )
    return candidate


def _require_room(name: str, minimum: ParameterValue, maximum: ParameterValue) -> None:
    if minimum >= maximum:
        raise StrategyContractError(
            f"{name} declares minimum {minimum} and maximum {maximum}; a parameter with "
            f"no room is a constant wearing a parameter's name, and declaring it as one "
            f"tells the search there is a dimension to explore when there is not"
        )


def _require_inside(
    name: str, candidate: ParameterValue, minimum: ParameterValue, maximum: ParameterValue
) -> None:
    if not minimum <= candidate <= maximum:
        raise StrategyContractError(
            f"{name} was given {candidate}, outside its declared bounds [{minimum}, {maximum}]"
        )
