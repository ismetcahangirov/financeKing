"""Properties of the de-risking scalar, which is the size schedule approaching a limit.

Issue #52 names this file by path and states the four properties directly: the
multiplier lies in [0, 1], is monotone non-increasing in consumed budget, equals 1 at
60% of the budget, and equals 0 at 100%.

Each of the four fails in a different, silent way. Above 1 and the taper *increases*
size into a drawdown. Below 0 and a multiplication flips a long into a short. A
non-monotone reading means a deeper drawdown can size larger than a shallower one, so
the schedule is no longer a schedule. And a value above 0 at full budget means the
limit does not actually stop anything -- it merely narrows.

`.claude/rules/testing-rules.md` clause 2: property tests are mandatory for every
function in `fking.risk`.
"""

from __future__ import annotations

from decimal import Decimal

import pytest
from hypothesis import given
from hypothesis import strategies as st

from fking.risk.drawdown import derisk_scalar

pytestmark = [pytest.mark.property, pytest.mark.unit]

# Ratios drawn at 6 decimal places over a range that reaches well past any budget: the
# interesting arithmetic is at and beyond 100% consumption, not inside it.
ratios = st.decimals(
    min_value=Decimal("0"),
    max_value=Decimal("2"),
    places=6,
    allow_nan=False,
    allow_infinity=False,
)
budgets = st.decimals(
    min_value=Decimal("0.001"),
    max_value=Decimal("0.25"),
    places=6,
    allow_nan=False,
    allow_infinity=False,
)


@given(observed_ratio=ratios, budget_ratio=budgets)
def test_derisk_scalar_is_always_a_fraction(observed_ratio: Decimal, budget_ratio: Decimal) -> None:
    multiplier = derisk_scalar(observed_ratio=observed_ratio, budget_ratio=budget_ratio)
    assert Decimal("0") <= multiplier <= Decimal("1")


@given(lower=ratios, higher=ratios, budget_ratio=budgets)
def test_a_deeper_drawdown_never_sizes_larger(
    lower: Decimal, higher: Decimal, budget_ratio: Decimal
) -> None:
    """Monotone non-increasing. Equality is legal; an increase is not."""
    first, second = sorted((lower, higher))
    assert derisk_scalar(observed_ratio=first, budget_ratio=budget_ratio) >= derisk_scalar(
        observed_ratio=second, budget_ratio=budget_ratio
    )


@given(budget_ratio=budgets)
def test_full_size_is_retained_up_to_sixty_percent_of_the_budget(
    budget_ratio: Decimal,
) -> None:
    at_onset = derisk_scalar(
        observed_ratio=budget_ratio * Decimal("0.6"), budget_ratio=budget_ratio
    )
    assert at_onset == Decimal("1")


@given(budget_ratio=budgets)
def test_size_reaches_zero_exactly_at_the_budget(budget_ratio: Decimal) -> None:
    assert derisk_scalar(observed_ratio=budget_ratio, budget_ratio=budget_ratio) == Decimal("0")


@given(budget_ratio=budgets)
def test_the_taper_midpoint_is_half_size(budget_ratio: Decimal) -> None:
    """80% of the budget is the middle of the taper, so the multiplier is 0.5.

    Asserted separately from the endpoints because a schedule pinned only at 0.6 and 1.0
    is also satisfied by a step function, which is the shape this scalar exists to
    replace.
    """
    midpoint = derisk_scalar(
        observed_ratio=budget_ratio * Decimal("0.8"), budget_ratio=budget_ratio
    )
    assert midpoint == Decimal("0.5")


@given(observed_ratio=ratios)
def test_an_unusable_budget_halts_rather_than_permitting(observed_ratio: Decimal) -> None:
    """A zero budget is the most conservative configuration available, not a disabled one."""
    assert derisk_scalar(observed_ratio=observed_ratio, budget_ratio=Decimal("0")) == Decimal("0")
