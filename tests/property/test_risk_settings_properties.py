"""No reachable configuration of `RiskSettings` is looser than a compiled-in bound.

The example tests in `tests/platform/config/test_ceilings.py` and `test_floors.py` move
one limit at a time from a default baseline. That is exactly the shape of test issue
#171 slipped past: each limit was checked against the mapping somebody remembered to
check it against, and `conviction_floor` was checked against a `Field(ge=0)` constraint
that disagreed with the compiled-in floor of 0.10.

So the property here is stated over the whole configuration at once and in the only
direction that matters for safety: **whatever is submitted, if the model accepts it then
every bounded limit is inside its bound.** Not "the values I thought to try are refused"
-- no configuration at all, drawn from anywhere in the neighbourhood of the bounds, is
accepted while loose.

`docs/rules/testing-rules.md` clause 2.
"""

from __future__ import annotations

from collections.abc import Mapping
from decimal import Decimal
from typing import Final

import pytest
from hypothesis import given
from hypothesis import strategies as st
from pydantic import ValidationError

from fking.platform.config import HARD_CEILINGS, HARD_FLOORS, RiskSettings

pytestmark = [pytest.mark.property, pytest.mark.unit]

# The bounded limits the model stores as `int`. A Decimal is truncated on the way in, so
# any assertion about "stored exactly as submitted" compares against the truncated value.
INTEGER_FIELDS: Final[frozenset[str]] = frozenset(
    name
    for name in (*HARD_CEILINGS, *HARD_FLOORS)
    if RiskSettings.model_fields[name].annotation is int
)


def _up_to(ceiling: Decimal, *, integral: bool = False) -> st.SearchStrategy[Decimal]:
    """Positive values at or below a ceiling.

    From the smallest representable positive value rather than from zero. `RiskLimits`
    accepts a limit of zero -- it halts trading, which is the most conservative
    configuration available -- but the configuration tree carries `gt=0` on top of the
    ceiling, on the reading that a zero arriving from an environment variable is far
    more often an unset variable than a deliberate halt. A draw of zero would therefore
    be refused for a reason unrelated to the bounds under test.
    """
    return st.decimals(
        min_value=Decimal("1") if integral else Decimal("0.01"),
        max_value=ceiling,
        places=2,
        allow_nan=False,
        allow_infinity=False,
    )


BOUNDED_NAMES: Final[tuple[str, ...]] = tuple(sorted((*HARD_CEILINGS, *HARD_FLOORS)))

# Conformance by construction: ceiling-governed fields at or below their bound,
# floor-governed fields at or above theirs, and the two cross-field relations
# `RiskSettings` also enforces satisfied. Every draw is therefore a configuration the
# model must accept.
_CONFORMING: Final[st.SearchStrategy[dict[str, Decimal]]] = st.fixed_dictionaries(
    {
        **{
            name: _up_to(ceiling, integral=name in INTEGER_FIELDS)
            for name, ceiling in HARD_CEILINGS.items()
            # These four carry a cross-field or shape constraint of their own and are
            # pinned below, so that a draw is never refused for a reason unrelated to
            # the bounds under test.
            if name
            not in {
                "max_single_order_notional_usd",
                "max_position_notional_usd",
                "max_daily_drawdown_ratio",
                "max_total_drawdown_ratio",
            }
        },
        **{
            name: st.decimals(
                min_value=floor,
                # `conviction_floor` and `min_free_margin_ratio` are ratios the model
                # caps at 1, so twice the floor has to stay inside that cap; every floor
                # here is at or below 0.5 of its own cap, or an integer count.
                max_value=floor * 2,
                places=2,
                allow_nan=False,
                allow_infinity=False,
            )
            for name, floor in HARD_FLOORS.items()
        },
        "max_position_notional_usd": st.just(HARD_CEILINGS["max_position_notional_usd"]),
        "max_single_order_notional_usd": _up_to(HARD_CEILINGS["max_single_order_notional_usd"]),
        "max_total_drawdown_ratio": st.just(HARD_CEILINGS["max_total_drawdown_ratio"]),
        "max_daily_drawdown_ratio": _up_to(HARD_CEILINGS["max_daily_drawdown_ratio"]),
    }
)


def _payload(drawn: Mapping[str, Decimal]) -> dict[str, object]:
    """A model payload from a drawn configuration, coercing the integer-typed limits."""
    return {name: int(value) if name in INTEGER_FIELDS else value for name, value in drawn.items()}


def _bounded_values(settings: RiskSettings) -> dict[str, Decimal]:
    return {name: Decimal(str(getattr(settings, name))) for name in (*HARD_CEILINGS, *HARD_FLOORS)}


@given(
    drawn=_CONFORMING,
    loosened=st.sets(st.sampled_from(BOUNDED_NAMES)),
    factor=st.decimals(min_value="0.01", max_value="4", places=2),
)
def test_an_accepted_configuration_is_never_looser_than_a_compiled_in_bound(
    drawn: Mapping[str, Decimal], loosened: set[str], factor: Decimal
) -> None:
    """The whole issue, stated once: acceptance implies conformance, for any input.

    Both bounds are asserted against the same accepted object, because a validator that
    checks only the mapping it was written against passes every test aimed at that
    mapping. `conviction_floor = 0` passed a ceilings-only `RiskSettings` for months.

    Built by perturbing a conforming configuration rather than drawing each field from
    the whole Decimal line, for two reasons: a strategy that mostly produces 10^20
    proves only that pydantic rejects absurdities, and the empty perturbation set is
    always generated -- so this property cannot become vacuously true by refusing every
    payload it is handed, which is the way an "acceptance implies X" test dies quietly.
    """
    payload = _payload(drawn)
    for name in loosened:
        scaled = drawn[name] * factor
        payload[name] = int(scaled) if name in INTEGER_FIELDS else scaled

    try:
        settings = RiskSettings.model_validate(payload)
    except ValidationError:
        return  # Refused. A refusal proves nothing about the bounds and asserts nothing.

    submitted = _bounded_values(settings)
    for name, ceiling in HARD_CEILINGS.items():
        assert submitted[name] <= ceiling
    for name, floor in HARD_FLOORS.items():
        assert submitted[name] >= floor


@given(drawn=_CONFORMING)
def test_every_conforming_configuration_is_accepted_and_stored_unmodified(
    drawn: Mapping[str, Decimal],
) -> None:
    """The converse, which is what stops the property above being satisfiable by a model
    that refuses everything -- and which catches clamping.

    `min(configured, ceiling)` satisfies every acceptance test ever written, because it
    never raises. It is caught only by comparing what came back against what went in.
    """
    settings = RiskSettings.model_validate(_payload(drawn))

    expected = {
        name: Decimal(int(value)) if name in INTEGER_FIELDS else value
        for name, value in drawn.items()
    }
    assert _bounded_values(settings) == expected


@given(drawn=_CONFORMING, name=st.sampled_from(sorted(HARD_FLOORS)))
def test_lowering_any_single_limit_below_its_floor_is_refused(
    drawn: Mapping[str, Decimal], name: str
) -> None:
    """Refusal survives being embedded in an otherwise valid configuration.

    A validator that short-circuits on the first breach, or one that only runs when some
    other field is at its default, passes a single-field test and fails here.
    """
    payload = _payload(drawn)
    below = HARD_FLOORS[name] - Decimal("0.01")
    payload[name] = int(below) if name in INTEGER_FIELDS else below

    with pytest.raises(ValidationError):
        RiskSettings.model_validate(payload)
