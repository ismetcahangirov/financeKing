"""Compiled-in bounds on every configurable risk limit, in both directions.

`CONFIGURATION.md` section 8 declares `HARD_CEILINGS` and stops there. That is half of
the problem, and the missing half is where the bug lives.

Most limits are "larger is riskier" and are bounded above: `max_position_notional_usd`,
`max_leverage`, `max_total_drawdown_ratio`. A few are "smaller is riskier" and must be
bounded *below*: `min_free_margin_ratio`, `min_trades_for_kelly`, `conviction_floor`.
A single validation loop over one mapping with `if value > bound: raise` handles the
first group correctly and the second group backwards -- it accepts
`min_free_margin_ratio = 0` cheerfully, which authorises trading with no margin buffer
and reads as a passing config check. `RISK_PHILOSOPHY.md` section 9 property 3 names
that exact failure.

So the two sets are separate mappings, checked by separate validators, holding values of
separate types. The uniform-comparison version does not typecheck, and the type system
is doing the reviewing rather than a human scanning a `>` inside a loop over a
dictionary.

**Why `Ceiling` and `Floor` are classes rather than `typing.NewType`.** A
`NewType("Floor", Decimal)` is assignable to `Decimal`, so it inherits `Decimal`'s
comparison operators and `floor > requested` type-checks cleanly -- which is precisely
the expression this module exists to make impossible. A frozen single-field class has no
ordering operators at all, so the only comparison available is the one the class
provides, and each class provides exactly one, in its own direction. `mypy --strict`
then rejects `HARD_FLOORS[name] > requested` with an unsupported-operand error rather
than accepting it as a Decimal comparison. `tests/risk/test_ceiling_types.py` runs mypy
over that spelling and asserts it fails.

**Refuse, never clamp.** `min(configured, ceiling)` is the tempting implementation and
it is wrong. Someone wrote `max_position_notional_usd: 50000` because they believed they
were running at $50k; clamping to $25k leaves the system safe, the person wrong, and
nobody informed -- and they will make the next decision on the belief they still hold.
`DECISION_FRAMEWORK.md` section 8 ranks a silent substitution of a default as the
second-worst failure mode available. Every breach message therefore carries both the
requested value and the bound.

Neither set of *values* is restated here. Both are imported from
`fking.platform.config`, which is where the configuration tree's own validators read
them, because two compiled-in copies of a safety constant is two numbers that can
disagree -- and the one that disagrees silently is whichever the reader is not looking
at. What this module adds is the direction, as a type. The floors used to be stated
here and only here, which left `RiskSettings.conviction_floor` free to carry `ge=0`
while this file said 0.10 (issue #171).

`RISK_PHILOSOPHY.md` section 9, `CONFIGURATION.md` section 8.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from decimal import Decimal
from types import MappingProxyType
from typing import Final

from fking.platform.config import HARD_CEILINGS as _CONFIGURED_CEILINGS
from fking.platform.config import HARD_FLOORS as _CONFIGURED_FLOORS

__all__ = [
    "HARD_CEILINGS",
    "HARD_FLOORS",
    "Ceiling",
    "Floor",
    "assert_above_floors",
    "assert_within_ceilings",
]

# Appended to every refusal. The direction of friction is the whole design: tightening a
# limit is free and needs no review; moving a compiled-in bound needs a source edit and
# a human who has said out loud that they intend it.
_ADVICE: Final[str] = (
    " Configuration may only make this system more conservative. Moving a compiled-in "
    "bound requires a source edit and a pull request labelled safety:critical."
)


@dataclass(frozen=True, slots=True)
class Ceiling:
    """An upper bound. Configuration may sit anywhere at or below it, never above.

    Deliberately not orderable. The only comparison this type offers is
    `is_exceeded_by`, so the direction of the check is chosen once, here, by the type
    of the bound -- not per call site by whoever wrote the loop.
    """

    bound: Decimal

    def is_exceeded_by(self, requested: Decimal) -> bool:
        """True when `requested` sits above this ceiling. Equality is legal."""
        return requested > self.bound


@dataclass(frozen=True, slots=True)
class Floor:
    """A lower bound. Configuration may sit anywhere at or above it, never below.

    The mirror of `Ceiling`, and a *different class* rather than the same class with a
    direction flag. A flag is a value, and a value can be wrong at one call site out of
    nine; a type cannot.
    """

    bound: Decimal

    def is_undercut_by(self, requested: Decimal) -> bool:
        """True when `requested` sits below this floor. Equality is legal."""
        return requested < self.bound


# MappingProxyType rather than a dict: a dict here can be widened in-process by a test
# fixture or by generated code, and a bound that can be moved at runtime is not a bound.
# Mutating either of these raises TypeError at the point of the attempt.
HARD_CEILINGS: Final[Mapping[str, Ceiling]] = MappingProxyType(
    {name: Ceiling(bound) for name, bound in _CONFIGURED_CEILINGS.items()}
)

HARD_FLOORS: Final[Mapping[str, Floor]] = MappingProxyType(
    {name: Floor(bound) for name, bound in _CONFIGURED_FLOORS.items()}
)


def _refuse(breaches: list[str]) -> None:
    """Raise once, naming every breach.

    Collected rather than raised on the first: fixing one limit and being told about the
    next on the following boot turns one misconfiguration into three restarts.
    """
    if breaches:
        raise ValueError("; ".join(breaches) + "." + _ADVICE)


def _assert_bounds_are_observed(
    observed: Mapping[str, Decimal], names: Mapping[str, object]
) -> None:
    """Refuse a bound whose field the caller did not report.

    A bound on a field that nobody submits bounds nothing, and nothing says so. That is
    the shape a bound takes after the field it guarded is renamed.
    """
    missing = sorted(set(names) - set(observed))
    if missing:
        raise ValueError(
            f"bounded limits {missing} were not submitted for validation; a bound on a "
            f"field nobody reports constrains nothing"
        )


def assert_within_ceilings(
    observed: Mapping[str, Decimal], ceilings: Mapping[str, Ceiling], *, scope: str
) -> None:
    """Refuse any submitted limit above its ceiling, reporting every breach at once.

    Takes `Mapping[str, Ceiling]` and nothing else. Handing it `HARD_FLOORS` is an
    argument-type error, so the two directions cannot be crossed by editing one line.
    """
    _assert_bounds_are_observed(observed, ceilings)
    _refuse(
        [
            f"{scope}.{name}={observed[name]} exceeds the compiled-in hard ceiling {ceiling.bound}"
            for name, ceiling in ceilings.items()
            if ceiling.is_exceeded_by(observed[name])
        ]
    )


def assert_above_floors(
    observed: Mapping[str, Decimal], floors: Mapping[str, Floor], *, scope: str
) -> None:
    """Refuse any submitted limit below its floor, reporting every breach at once.

    The half a single-mapping validator gets backwards. `min_free_margin_ratio = 0` is
    the canonical case: it authorises trading with no margin buffer at all and passes a
    `value > bound` check without complaint.
    """
    _assert_bounds_are_observed(observed, floors)
    _refuse(
        [
            f"{scope}.{name}={observed[name]} is below the compiled-in hard floor {floor.bound}"
            for name, floor in floors.items()
            if floor.is_undercut_by(observed[name])
        ]
    )
