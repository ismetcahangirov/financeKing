"""Builders for the evolution suite.

Transitions are built by *chaining* rather than by naming both endpoints:
`next_transition(previous, LifecycleState.PAPER)` takes its `from_state` from the event
before it. That is the same rule the store enforces -- an event's from-state is the
derived current state, not a field a caller chooses -- so a test that constructs an
impossible sequence has to say so explicitly instead of doing it by typo.

Everything else is `dataclasses.replace` over a valid base, which is what a frozen
domain object is for.
"""

from __future__ import annotations

from collections.abc import Iterator, Mapping, Sequence
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from uuid import uuid4

import pytest

from fking.evolution import (
    SCORED_STATES,
    ExpressionNode,
    Genome,
    GenomeRecord,
    LifecycleEvent,
    LifecycleState,
    MutationOperator,
    NodeKind,
    ReasonClass,
    genome_hash,
)

# A frozen instant rather than a clock read: every digest, every ordering assertion and
# every reconstructed row in this suite has to be reproducible from the log alone.
FIXED_MOMENT: datetime = datetime(2026, 8, 5, 9, 30, tzinfo=UTC)

SCORING_VERSION: str = "scoring-2026.08"

# The evidence a transition into a scored state must carry. Real-looking numbers because
# the constraint that reads them is about their presence, and a row of zeros would pass
# a reader's eye while failing the CHECK.
SCORE_COMPONENTS: Mapping[str, Decimal] = {
    "deflated_sharpe": Decimal("0.9612"),
    "fold_sign_consistency_fraction": Decimal("0.8214"),
    "capacity_usd": Decimal("14000"),
}
SURVIVAL_SCORE: Decimal = Decimal("0.72")
INDEPENDENT_EPISODES: int = 37
FORWARD_INDEPENDENT_EPISODES: int = 21
GLOBAL_TRIAL_INDEX: int = 1847
FAMILY_TRIAL_INDEX: int = 612
GENESIS_TRIAL_INDEX: int = 12


def comparison_rule(
    *,
    feature_id: str = "momentum.4h",
    parameter_name: str = "entry_threshold",
    operator: str = "gt",
) -> ExpressionNode:
    """`feature > parameter`, the smallest entry rule the contract admits."""
    return ExpressionNode(
        kind=NodeKind.COMPARISON,
        operator=operator,
        children=(
            ExpressionNode(kind=NodeKind.FEATURE, feature_id=feature_id),
            ExpressionNode(kind=NodeKind.PARAMETER, parameter_name=parameter_name),
        ),
    )


def build_genome(
    *,
    entry_rule: ExpressionNode | None = None,
    parameters: Mapping[str, Decimal] | None = None,
    feature_ids: frozenset[str] = frozenset({"momentum.4h"}),
    holding_horizon: timedelta = timedelta(hours=8),
) -> Genome:
    return Genome(
        entry_rule=comparison_rule() if entry_rule is None else entry_rule,
        parameters={"entry_threshold": Decimal("1.5")} if parameters is None else parameters,
        feature_ids=feature_ids,
        holding_horizon=holding_horizon,
    )


def build_record(
    genome: Genome | None = None,
    *,
    generation_number: int = 0,
    parent_genome_hashes: Sequence[str] = (),
    mutation_operators: Sequence[MutationOperator] = (),
    trial_index_at_creation: int = GENESIS_TRIAL_INDEX,
) -> GenomeRecord:
    return GenomeRecord(
        genome=build_genome() if genome is None else genome,
        generation_number=generation_number,
        trial_index_at_creation=trial_index_at_creation,
        mutation_operators=tuple(mutation_operators),
        scoring_version=SCORING_VERSION,
        parent_genome_hashes=tuple(parent_genome_hashes),
        created_at_utc=FIXED_MOMENT,
    )


def build_genesis(strategy_id: str, genome: Genome | None = None) -> LifecycleEvent:
    """`nonexistent -> proposed`. Every strategy's first row, and the base every other
    event in a test is derived from."""
    return LifecycleEvent(
        event_id=uuid4(),
        strategy_id=strategy_id,
        genome_hash=genome_hash(build_genome() if genome is None else genome),
        correlation_id=uuid4(),
        causation_id=None,
        from_state=LifecycleState.NONEXISTENT,
        to_state=LifecycleState.PROPOSED,
        reason_class=ReasonClass.GENESIS,
        reason="seeded by the founder population",
        survival_score=None,
        score_components={},
        independent_episode_count=0,
        forward_independent_episode_count=0,
        global_trial_index=GENESIS_TRIAL_INDEX,
        family_trial_index=3,
        scoring_version=SCORING_VERSION,
        occurred_at_utc=FIXED_MOMENT,
    )


def next_transition(
    previous: LifecycleEvent,
    to_state: LifecycleState,
    *,
    reason_class: ReasonClass = ReasonClass.GATE_PASSED,
    reason: str | None = None,
    from_state: LifecycleState | None = None,
) -> LifecycleEvent:
    """The event that follows `previous`.

    `from_state` defaults to `previous.to_state` and is overridable only so that a test
    can construct the disagreement the store is supposed to refuse.

    One `replace`, not two. `LifecycleEvent` validates in `__post_init__`, so a first
    call that blanked the evidence would raise before a second call could restore it --
    which is the invariant working, and the reason a builder cannot construct an event
    in stages.
    """
    carries_evidence = to_state in SCORED_STATES
    return replace(
        previous,
        event_id=uuid4(),
        correlation_id=uuid4(),
        from_state=previous.to_state if from_state is None else from_state,
        to_state=to_state,
        reason_class=reason_class,
        reason=f"cleared the gate into {to_state.value}" if reason is None else reason,
        survival_score=SURVIVAL_SCORE if carries_evidence else None,
        score_components=SCORE_COMPONENTS if carries_evidence else {},
        independent_episode_count=INDEPENDENT_EPISODES if carries_evidence else 0,
        forward_independent_episode_count=(FORWARD_INDEPENDENT_EPISODES if carries_evidence else 0),
        global_trial_index=GLOBAL_TRIAL_INDEX if carries_evidence else GENESIS_TRIAL_INDEX,
        family_trial_index=FAMILY_TRIAL_INDEX if carries_evidence else 3,
    )


def unique_genomes(how_many: int) -> Iterator[Genome]:
    """Distinct genomes that differ structurally, so each starts its own lineage."""
    for index in range(how_many):
        feature_id = f"synthetic.feature.{index:04d}"
        yield build_genome(
            entry_rule=comparison_rule(feature_id=feature_id),
            feature_ids=frozenset({feature_id}),
        )


@pytest.fixture
def strategy_id() -> str:
    return "strat-momentum-0001"
