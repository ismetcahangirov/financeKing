"""Properties of the two bound types and the two validators that read them.

`tests/risk/test_ceiling_bounds.py` asserts the shipped bound table by example, and
`tests/risk/test_limits_property.py` asserts the model-level biconditional. This file
covers the layer between them -- the algebra the validators rely on -- because that is
where a plausible "simplification" lands: a `>=` swapped for a `>`, a single shared
comparison helper, a breach list that stops at the first entry.

Every property is stated against a plain `Decimal` comparison written out here, never
by calling the predicate under test. A property test that reuses the implementation's
own comparison proves only that the implementation agrees with itself.

`docs/rules/testing-rules.md` clause 2: property tests are mandatory for every
function in `fking.risk`.
"""

from __future__ import annotations

from collections.abc import Mapping
from decimal import Decimal
from typing import Final

import pytest
from hypothesis import given
from hypothesis import strategies as st

from fking.risk.ceilings import (
    HARD_CEILINGS,
    HARD_FLOORS,
    Ceiling,
    Floor,
    assert_above_floors,
    assert_within_ceilings,
)

pytestmark = [pytest.mark.property, pytest.mark.unit]

# Wide enough to straddle every shipped bound in both directions, and bounded so that
# the generator spends its budget near the interesting values rather than on magnitudes
# no configuration file will ever hold.
_BOUNDS: Final[st.SearchStrategy[Decimal]] = st.decimals(
    min_value=Decimal("0"),
    max_value=Decimal("100000"),
    places=4,
    allow_nan=False,
    allow_infinity=False,
)
_SUBMITTED: Final[st.SearchStrategy[Decimal]] = _BOUNDS


@given(bound=_BOUNDS, requested=_SUBMITTED)
def test_a_ceiling_and_a_floor_at_the_same_bound_never_both_refuse(
    bound: Decimal, requested: Decimal
) -> None:
    """The two predicates are opposite halves of one line, not two opinions about it.

    If both could refuse the same value there would exist a number no configuration
    could hold, and the field it governed would be unusable in a way nothing announces.
    """
    exceeded = Ceiling(bound).is_exceeded_by(requested)
    undercut = Floor(bound).is_undercut_by(requested)

    assert not (exceeded and undercut)
    assert (requested == bound) == (not exceeded and not undercut)


@given(bound=_BOUNDS, requested=_SUBMITTED)
def test_each_predicate_is_its_own_strict_comparison_and_nothing_else(
    bound: Decimal, requested: Decimal
) -> None:
    """Equality is legal on both sides. An exclusive bound makes the documented number
    unreachable, and the first person to hit that reads it as an off-by-one to fix."""
    assert Ceiling(bound).is_exceeded_by(requested) == (requested > bound)
    assert Floor(bound).is_undercut_by(requested) == (requested < bound)


@given(bound=_BOUNDS, accepted=_SUBMITTED, tightened=_SUBMITTED)
def test_moving_a_value_toward_safety_never_creates_a_breach(
    bound: Decimal, accepted: Decimal, tightened: Decimal
) -> None:
    """Monotonicity in the safe direction, which is the property that makes "tightening
    is free" true rather than merely intended.

    For a ceiling, safety is downward; for a floor, upward. A predicate that lost this
    would refuse a strictly more conservative configuration, and the safe move would
    become the expensive one.
    """
    ceiling = Ceiling(bound)
    if not ceiling.is_exceeded_by(accepted) and tightened <= accepted:
        assert not ceiling.is_exceeded_by(tightened)

    floor = Floor(bound)
    if not floor.is_undercut_by(accepted) and tightened >= accepted:
        assert not floor.is_undercut_by(tightened)


def _submission(scales: Mapping[str, Decimal]) -> dict[str, Decimal]:
    """Every bounded field, each sitting at its own bound scaled by the drawn factor.

    Scaling rather than drawing freely keeps the value adjacent to its bound, so a
    factor either side of 1 is a breach or a conformance rather than a number three
    orders of magnitude away that tests only the obvious case.
    """
    submitted = {name: ceiling.bound * scales[name] for name, ceiling in HARD_CEILINGS.items()}
    submitted.update({name: floor.bound * scales[name] for name, floor in HARD_FLOORS.items()})
    return submitted


_SCALES: Final[st.SearchStrategy[dict[str, Decimal]]] = st.fixed_dictionaries(
    {
        name: st.sampled_from(
            [Decimal("0"), Decimal("0.5"), Decimal("1"), Decimal("1.0001"), Decimal("2")]
        )
        for name in (*HARD_CEILINGS, *HARD_FLOORS)
    }
)


@given(scales=_SCALES)
def test_the_ceiling_validator_refuses_exactly_the_submissions_that_exceed_one(
    scales: Mapping[str, Decimal],
) -> None:
    submitted = _submission(scales)
    breached = sorted(
        name for name, ceiling in HARD_CEILINGS.items() if submitted[name] > ceiling.bound
    )

    if not breached:
        assert_within_ceilings(submitted, HARD_CEILINGS, scope="risk")
        return

    with pytest.raises(ValueError, match="hard ceiling") as raised:
        assert_within_ceilings(submitted, HARD_CEILINGS, scope="risk")

    rendered = str(raised.value)
    # Every breach, not the first: fixing one limit and being told about the next on
    # the following boot turns one misconfiguration into three restarts.
    for name in breached:
        assert name in rendered
        assert str(submitted[name]) in rendered
        assert str(HARD_CEILINGS[name].bound) in rendered


@given(scales=_SCALES)
def test_the_floor_validator_refuses_exactly_the_submissions_that_undercut_one(
    scales: Mapping[str, Decimal],
) -> None:
    submitted = _submission(scales)
    breached = sorted(name for name, floor in HARD_FLOORS.items() if submitted[name] < floor.bound)

    if not breached:
        assert_above_floors(submitted, HARD_FLOORS, scope="risk")
        return

    with pytest.raises(ValueError, match="hard floor") as raised:
        assert_above_floors(submitted, HARD_FLOORS, scope="risk")

    rendered = str(raised.value)
    for name in breached:
        assert name in rendered
        assert str(submitted[name]) in rendered
        assert str(HARD_FLOORS[name].bound) in rendered


@given(scales=_SCALES, dropped=st.sampled_from(sorted({*HARD_CEILINGS, *HARD_FLOORS})))
def test_a_bound_whose_field_is_missing_from_the_submission_is_always_refused(
    scales: Mapping[str, Decimal], dropped: str
) -> None:
    """A bound on a field nobody reports constrains nothing, and nothing says so.

    That is the state a bound reaches after the field it guarded is renamed, and it is
    indistinguishable from a passing check unless the validator refuses the gap.
    """
    submitted = _submission(scales)
    del submitted[dropped]

    # Branched rather than selecting a validator into a variable: the two signatures
    # take different bound types on purpose, and a variable holding either would be
    # typed as their join, which is the erasure this design exists to prevent.
    if dropped in HARD_CEILINGS:
        with pytest.raises(ValueError, match="constrains nothing") as raised:
            assert_within_ceilings(submitted, HARD_CEILINGS, scope="risk")
    else:
        with pytest.raises(ValueError, match="constrains nothing") as raised:
            assert_above_floors(submitted, HARD_FLOORS, scope="risk")
    assert dropped in str(raised.value)


@given(scales=_SCALES)
def test_every_refusal_names_the_route_to_moving_the_bound(
    scales: Mapping[str, Decimal],
) -> None:
    """A refusal that does not say what to do next gets resolved by whatever the reader
    guesses, and the available guess is to edit the number until it passes."""
    submitted = _submission(scales)

    if any(submitted[name] > ceiling.bound for name, ceiling in HARD_CEILINGS.items()):
        with pytest.raises(ValueError, match="safety:critical"):
            assert_within_ceilings(submitted, HARD_CEILINGS, scope="risk")

    if any(submitted[name] < floor.bound for name, floor in HARD_FLOORS.items()):
        with pytest.raises(ValueError, match="safety:critical"):
            assert_above_floors(submitted, HARD_FLOORS, scope="risk")
