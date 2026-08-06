"""What a lifecycle event refuses at construction, and how state is derived from a stream.

These are the checks that fire before anything reaches the database, and they exist for a
reason the constraints alone do not cover: a caller gets `retired is terminal` rather than
`ck_lifecycle_event_retired_is_terminal`, which is the name of a rule and not the rule.
The database still enforces every one of them -- `test_lifecycle_events_are_append_only.py`
is the other half.
"""

from __future__ import annotations

from dataclasses import replace
from datetime import UTC, datetime, timedelta, timezone
from decimal import Decimal

import pytest

from fking.evolution import (
    LifecycleEvent,
    LifecycleState,
    LifecycleTransitionError,
    ReasonClass,
    derive_current_state,
    require_permitted_transition,
)
from tests.evolution.conftest import build_genesis, next_transition

pytestmark = pytest.mark.unit


@pytest.fixture
def genesis() -> LifecycleEvent:
    return build_genesis("strat-unit")


def test_a_transition_to_the_state_already_held_is_not_a_transition() -> None:
    with pytest.raises(LifecycleTransitionError, match="nothing moved"):
        require_permitted_transition(LifecycleState.PAPER, LifecycleState.PAPER)


def test_nothing_transitions_out_of_retired() -> None:
    with pytest.raises(LifecycleTransitionError, match="retired is terminal"):
        require_permitted_transition(LifecycleState.RETIRED, LifecycleState.PROPOSED)


def test_nothing_transitions_into_nonexistent() -> None:
    with pytest.raises(LifecycleTransitionError, match="never where it goes"):
        require_permitted_transition(LifecycleState.PROPOSED, LifecycleState.NONEXISTENT)


def test_a_naive_decision_instant_is_rejected_at_construction(
    genesis: LifecycleEvent,
) -> None:
    with pytest.raises(LifecycleTransitionError, match="timezone-aware"):
        replace(genesis, occurred_at_utc=datetime(2026, 8, 5, 9, 30))  # noqa: DTZ001 - the point


def test_an_aware_but_non_utc_instant_is_rejected_rather_than_converted(
    genesis: LifecycleEvent,
) -> None:
    """Converting would accept a value whose offset was guessed wrong upstream."""
    baku = timezone(timedelta(hours=4))
    with pytest.raises(LifecycleTransitionError, match="must be UTC"):
        replace(genesis, occurred_at_utc=datetime(2026, 8, 5, 13, 30, tzinfo=baku))


def test_a_transition_states_its_reason(genesis: LifecycleEvent) -> None:
    """The reason class is a category; the reason is the explanation."""
    with pytest.raises(LifecycleTransitionError, match="states its reason"):
        replace(genesis, reason="   ")


def test_a_negative_episode_count_is_refused(genesis: LifecycleEvent) -> None:
    with pytest.raises(LifecycleTransitionError, match="cannot be negative"):
        replace(genesis, independent_episode_count=-1)


def test_a_negative_forward_episode_count_is_refused(genesis: LifecycleEvent) -> None:
    with pytest.raises(LifecycleTransitionError, match="cannot be negative"):
        replace(genesis, forward_independent_episode_count=-3)


def test_a_negative_trial_index_is_refused(genesis: LifecycleEvent) -> None:
    with pytest.raises(LifecycleTransitionError, match="trial indices cannot be negative"):
        replace(genesis, global_trial_index=-1, family_trial_index=-1)


def test_a_family_ahead_of_the_global_count_means_a_counter_was_reset(
    genesis: LifecycleEvent,
) -> None:
    """The family is a subset by construction, so it cannot be larger."""
    with pytest.raises(LifecycleTransitionError, match="one of the two counters was reset"):
        replace(genesis, global_trial_index=10, family_trial_index=11)


def test_a_survival_score_outside_zero_to_one_is_refused(genesis: LifecycleEvent) -> None:
    validated = next_transition(genesis, LifecycleState.VALIDATED)
    with pytest.raises(LifecycleTransitionError, match="fraction on"):
        replace(validated, survival_score=Decimal("1.4"))


def test_entering_a_scored_state_requires_a_score_and_its_components(
    genesis: LifecycleEvent,
) -> None:
    validated = next_transition(genesis, LifecycleState.VALIDATED)
    with pytest.raises(LifecycleTransitionError, match="requires a survival score"):
        replace(validated, survival_score=None)
    with pytest.raises(LifecycleTransitionError, match="requires a survival score"):
        replace(validated, score_components={})


def test_entering_a_scored_state_requires_a_sample(genesis: LifecycleEvent) -> None:
    """Zero independent episodes is INSUFFICIENT_SAMPLE, not a low score."""
    validated = next_transition(genesis, LifecycleState.VALIDATED)
    with pytest.raises(LifecycleTransitionError, match="INSUFFICIENT_SAMPLE"):
        replace(validated, independent_episode_count=0)


def test_entering_a_scored_state_requires_a_trial_count_that_was_read(
    genesis: LifecycleEvent,
) -> None:
    """Zero never means 'nothing was tried, so no deflation was needed'."""
    validated = next_transition(genesis, LifecycleState.VALIDATED)
    with pytest.raises(LifecycleTransitionError, match="trial count that was actually"):
        replace(validated, global_trial_index=0, family_trial_index=0)


def test_an_unscored_transition_may_carry_no_score(genesis: LifecycleEvent) -> None:
    """`proposed -> backtested` has run nothing worth scoring yet, and `0.0` would be a
    claim rather than an absence."""
    backtested = next_transition(genesis, LifecycleState.BACKTESTED)

    assert backtested.survival_score is None
    assert dict(backtested.score_components) == {}


def test_score_components_are_copied_so_a_caller_cannot_edit_a_recorded_event(
    genesis: LifecycleEvent,
) -> None:
    mutable = {"deflated_sharpe": Decimal("0.96")}
    event = replace(next_transition(genesis, LifecycleState.VALIDATED), score_components=mutable)

    mutable["deflated_sharpe"] = Decimal("0.10")

    assert event.score_components["deflated_sharpe"] == Decimal("0.96")


def test_an_empty_stream_derives_to_nonexistent() -> None:
    assert derive_current_state(()) is LifecycleState.NONEXISTENT


def test_the_derived_state_is_the_last_events_destination(genesis: LifecycleEvent) -> None:
    backtested = next_transition(genesis, LifecycleState.BACKTESTED)
    validated = next_transition(backtested, LifecycleState.VALIDATED)

    assert derive_current_state((genesis, backtested, validated)) is LifecycleState.VALIDATED


def test_a_retirement_carries_its_reason_class(genesis: LifecycleEvent) -> None:
    """The class is what a query groups by when a defect sweep asks who was quarantined."""
    retired = next_transition(
        genesis,
        LifecycleState.RETIRED,
        reason_class=ReasonClass.DEFECT,
        reason="look-ahead leak in the 4h momentum feature",
    )

    assert retired.reason_class is ReasonClass.DEFECT
    assert retired.occurred_at_utc.tzinfo is UTC
