"""The state machine: which edges exist, and what each state is allowed to risk.

The parametrisation is over *every* ordered pair of states rather than over a list of
interesting ones. A hand-written list only ever covers the edges somebody thought of, and
the edge that matters is the one nobody did -- `proposed -> champion`, which no author
writes deliberately and which a promotion loop reading a stale state produces on its own.
"""

from __future__ import annotations

from decimal import Decimal
from itertools import product

import pytest

from fking.evolution import (
    CAPITAL_AUTHORITY,
    PERMITTED_TRANSITIONS,
    CapitalPosture,
    IllegalTransitionError,
    LifecycleState,
    capital_authority_for,
    is_permitted_transition,
    require_permitted_transition,
)

pytestmark = pytest.mark.unit

ALL_ORDERED_PAIRS = tuple(product(LifecycleState, LifecycleState))

# The forward chain of EVOLUTION_ENGINE.md section 1, written out here independently of
# the table under test. Restating it is the point: a test that derived the expectation
# from PERMITTED_TRANSITIONS would assert the table equals itself.
FORWARD_CHAIN = (
    (LifecycleState.NONEXISTENT, LifecycleState.PROPOSED),
    (LifecycleState.PROPOSED, LifecycleState.BACKTESTED),
    (LifecycleState.BACKTESTED, LifecycleState.VALIDATED),
    (LifecycleState.VALIDATED, LifecycleState.PAPER),
    (LifecycleState.PAPER, LifecycleState.CHALLENGER),
    (LifecycleState.CHALLENGER, LifecycleState.CHAMPION),
)


@pytest.mark.parametrize(("from_state", "to_state"), FORWARD_CHAIN)
def test_the_forward_chain_is_admitted(
    from_state: LifecycleState, to_state: LifecycleState
) -> None:
    require_permitted_transition(from_state, to_state)


@pytest.mark.parametrize(
    "from_state", [state for state in LifecycleState if state is not LifecycleState.RETIRED]
)
def test_every_state_except_retired_can_retire(from_state: LifecycleState) -> None:
    if from_state is LifecycleState.NONEXISTENT:
        # A strategy that does not exist has nothing to retire, which is why the
        # nonexistent row is a single edge rather than the general "anything may retire".
        with pytest.raises(IllegalTransitionError, match="not an edge"):
            require_permitted_transition(from_state, LifecycleState.RETIRED)
        return
    require_permitted_transition(from_state, LifecycleState.RETIRED)


@pytest.mark.parametrize("to_state", list(LifecycleState))
def test_retired_has_no_outgoing_edge_including_to_itself(to_state: LifecycleState) -> None:
    with pytest.raises(IllegalTransitionError):
        require_permitted_transition(LifecycleState.RETIRED, to_state)


@pytest.mark.parametrize(("from_state", "to_state"), ALL_ORDERED_PAIRS)
def test_every_pair_outside_the_table_is_refused(
    from_state: LifecycleState, to_state: LifecycleState
) -> None:
    """The exhaustive half: the table is the whole specification of what is possible."""
    if is_permitted_transition(from_state, to_state):
        require_permitted_transition(from_state, to_state)
        return
    with pytest.raises(IllegalTransitionError):
        require_permitted_transition(from_state, to_state)


def test_a_promotion_cannot_skip_the_states_that_produce_its_evidence() -> None:
    with pytest.raises(IllegalTransitionError, match="only destinations are"):
        require_permitted_transition(LifecycleState.PROPOSED, LifecycleState.CHAMPION)


def test_a_demoted_champion_retires_rather_than_returning_to_challenger() -> None:
    with pytest.raises(IllegalTransitionError, match="not an edge"):
        require_permitted_transition(LifecycleState.CHAMPION, LifecycleState.CHALLENGER)
    require_permitted_transition(LifecycleState.CHAMPION, LifecycleState.RETIRED)


def test_a_quarantined_strategy_re_enters_at_backtested_and_nowhere_earlier() -> None:
    require_permitted_transition(LifecycleState.QUARANTINED, LifecycleState.BACKTESTED)
    for unreachable in (LifecycleState.PROPOSED, LifecycleState.VALIDATED, LifecycleState.PAPER):
        with pytest.raises(IllegalTransitionError):
            require_permitted_transition(LifecycleState.QUARANTINED, unreachable)


@pytest.mark.parametrize("state", list(LifecycleState))
def test_every_state_declares_its_capital_authority(state: LifecycleState) -> None:
    assert state in CAPITAL_AUTHORITY
    assert capital_authority_for(state) is CAPITAL_AUTHORITY[state]


def test_a_challenger_risks_a_quarter_of_a_champions_budget() -> None:
    """Non-zero on purpose: at zero it is a paper strategy with a different label, and
    the venue interaction the state exists to measure never happens."""
    challenger = capital_authority_for(LifecycleState.CHALLENGER)
    champion = capital_authority_for(LifecycleState.CHAMPION)

    assert challenger.risk_budget_fraction_of_champion == Decimal("0.25")
    assert challenger.risk_budget_fraction_of_champion * 4 == (
        champion.risk_budget_fraction_of_champion
    )
    assert challenger.posture is CapitalPosture.FRACTIONAL


@pytest.mark.parametrize(
    "state",
    [
        LifecycleState.NONEXISTENT,
        LifecycleState.PROPOSED,
        LifecycleState.BACKTESTED,
        LifecycleState.VALIDATED,
        LifecycleState.PAPER,
        LifecycleState.QUARANTINED,
        LifecycleState.RETIRED,
    ],
)
def test_no_state_before_challenger_reaches_a_venue(state: LifecycleState) -> None:
    authority = capital_authority_for(state)
    assert authority.risk_budget_fraction_of_champion == Decimal(0)
    assert authority.posture in {CapitalPosture.NONE, CapitalPosture.NOTIONAL_ONLY}


def test_paper_is_distinguishable_from_the_states_that_hold_nothing_at_all() -> None:
    """Both risk zero, and conflating them loses the fact that paper produces positions
    to score while `proposed` produces nothing to score at all."""
    assert capital_authority_for(LifecycleState.PAPER).posture is CapitalPosture.NOTIONAL_ONLY
    assert capital_authority_for(LifecycleState.PROPOSED).posture is CapitalPosture.NONE


def test_the_table_covers_every_state_so_a_new_one_cannot_be_silently_unreachable() -> None:
    assert set(PERMITTED_TRANSITIONS) == set(LifecycleState)
