"""The lineage store, against real Postgres as the application role.

The reconstruction test is the one to read first. `ARCHITECTURE.md` section 11 requires
that a decision be answerable from the record alone, months later, with no access to
application memory -- so the test writes a transition, throws the object away, reads the
row back through a fresh store, and requires the reconstructed event to be equal field
for field. If a field cannot survive that round trip, the audit is incomplete however
many rows it holds.
"""

from __future__ import annotations

from dataclasses import replace
from decimal import Decimal
from typing import Final

import pytest
import sqlalchemy as sa
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncEngine

from fking.evolution import (
    DEFAULT_COLLAPSE_MAX_GENERATIONS,
    Genome,
    LifecycleEvent,
    LifecycleState,
    LifecycleTransitionError,
    LineageCycleError,
    LineageStore,
    MutationOperator,
    ReasonClass,
    genome_hash,
    lineage_id_for,
)
from tests.evolution.conftest import (
    FAMILY_TRIAL_INDEX,
    FORWARD_INDEPENDENT_EPISODES,
    GENESIS_TRIAL_INDEX,
    GLOBAL_TRIAL_INDEX,
    INDEPENDENT_EPISODES,
    SCORING_VERSION,
    SURVIVAL_SCORE,
    build_genesis,
    build_genome,
    build_record,
    comparison_rule,
    next_transition,
    unique_genomes,
)

pytestmark = [pytest.mark.integration, pytest.mark.slow]

# `proposed` to `champion`, in order. Walking the real path rather than jumping is what
# makes the derived-state check in the store meaningful for these tests.
_PROMOTION_PATH: Final[tuple[LifecycleState, ...]] = (
    LifecycleState.BACKTESTED,
    LifecycleState.VALIDATED,
    LifecycleState.PAPER,
    LifecycleState.CHALLENGER,
    LifecycleState.CHAMPION,
)

_GENESIS_PLUS_ONE: Final[int] = 2
_GENESIS_PLUS_TWO: Final[int] = 3
_FULL_PROMOTION_EVENTS: Final[int] = 6
_DEEP_LINEAGE_DEPTH: Final[int] = 7
_DESCENDED_LIVE: Final[int] = 6
_INDEPENDENT_LIVE: Final[int] = 5


async def _admit(
    store: LineageStore,
    *,
    strategy_id: str,
    genome: Genome,
    parents: tuple[str, ...] = (),
    generation_number: int = 0,
) -> LifecycleEvent:
    genesis = build_genesis(strategy_id, genome)
    await store.admit_strategy(
        strategy_id=strategy_id,
        record=build_record(
            genome,
            parent_genome_hashes=parents,
            generation_number=generation_number,
            mutation_operators=(MutationOperator.PARAMETER_JITTER,) if parents else (),
        ),
        genesis_event=genesis,
    )
    return genesis


async def _promote_to(
    store: LineageStore, latest: LifecycleEvent, target: LifecycleState
) -> LifecycleEvent:
    """Walk a strategy from wherever it is to `target` through the real transitions."""
    for state in _PROMOTION_PATH:
        latest = next_transition(latest, state)
        await store.append_lifecycle_event(latest)
        if state is target:
            return latest
    raise AssertionError(f"{target.value} is not on the promotion path")


# ---------------------------------------------------------------------------
# Reconstruction from the record alone
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_one_transition_is_reconstructed_from_the_event_table_alone(
    app_engine: AsyncEngine, strategy_id: str
) -> None:
    genome = build_genome()
    genesis = await _admit(LineageStore(app_engine), strategy_id=strategy_id, genome=genome)

    # Through `backtested`, because `proposed -> validated` is not an edge: the shortcut
    # a test would take is refused by the same table the production path walks.
    backtested = next_transition(genesis, LifecycleState.BACKTESTED)
    await LineageStore(app_engine).append_lifecycle_event(backtested)
    promotion = replace(
        next_transition(backtested, LifecycleState.VALIDATED),
        causation_id=backtested.event_id,
    )
    await LineageStore(app_engine).append_lifecycle_event(promotion)

    # A fresh store: nothing of the writer's memory survives into the read.
    reconstructed = await LineageStore(app_engine).load_lifecycle_events(strategy_id)

    assert len(reconstructed) == _GENESIS_PLUS_TWO
    assert reconstructed[-1] == promotion

    read_back = reconstructed[-1]
    assert read_back.correlation_id == promotion.correlation_id
    assert read_back.causation_id == backtested.event_id
    assert read_back.survival_score == SURVIVAL_SCORE
    assert read_back.score_components["deflated_sharpe"] == Decimal("0.9612")
    assert read_back.independent_episode_count == INDEPENDENT_EPISODES
    assert read_back.forward_independent_episode_count == FORWARD_INDEPENDENT_EPISODES
    assert read_back.global_trial_index == GLOBAL_TRIAL_INDEX
    assert read_back.family_trial_index == FAMILY_TRIAL_INDEX
    assert read_back.genome_hash == genome_hash(genome)


@pytest.mark.asyncio
async def test_every_event_row_carries_its_correlation_components_and_episode_counts(
    app_engine: AsyncEngine, strategy_id: str
) -> None:
    """The columns the acceptance criterion names are NOT NULL for every row written."""
    genome = build_genome()
    store = LineageStore(app_engine)
    genesis = await _admit(store, strategy_id=strategy_id, genome=genome)
    await _promote_to(store, genesis, LifecycleState.CHAMPION)

    async with app_engine.connect() as connection:
        rows = (
            await connection.execute(
                sa.text(
                    """
                    SELECT correlation_id, score_components, independent_episode_count,
                           forward_independent_episode_count, scoring_version
                      FROM evolution.strategy_lifecycle_events ORDER BY seq
                    """
                )
            )
        ).all()

    assert len(rows) == _FULL_PROMOTION_EVENTS
    for row in rows:
        assert row.correlation_id is not None
        assert row.score_components is not None
        assert row.independent_episode_count is not None
        assert row.forward_independent_episode_count is not None
        assert row.scoring_version == SCORING_VERSION


# ---------------------------------------------------------------------------
# Identity and lineage
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_a_parameter_only_mutant_joins_its_parent_lineage(
    app_engine: AsyncEngine,
) -> None:
    """It cannot escape the family's accumulated trial count by acquiring a fresh id."""
    store = LineageStore(app_engine)
    parent = build_genome(parameters={"entry_threshold": Decimal("1.5")})
    child = build_genome(parameters={"entry_threshold": Decimal("1.9")})

    await _admit(store, strategy_id="strat-parent", genome=parent)
    await _admit(
        store,
        strategy_id="strat-child",
        genome=child,
        parents=(genome_hash(parent),),
        generation_number=1,
    )

    async with app_engine.connect() as connection:
        lineages = (
            await connection.scalars(
                sa.text("SELECT lineage_id FROM evolution.genome ORDER BY genome_hash")
            )
        ).all()

    assert genome_hash(child) != genome_hash(parent)
    assert set(lineages) == {lineage_id_for(parent)}


@pytest.mark.asyncio
async def test_a_structurally_new_hypothesis_starts_a_new_lineage(
    app_engine: AsyncEngine,
) -> None:
    store = LineageStore(app_engine)
    parent = build_genome()
    restructured = build_genome(
        entry_rule=comparison_rule(feature_id="funding.extremity"),
        feature_ids=frozenset({"funding.extremity"}),
    )

    await _admit(store, strategy_id="strat-parent", genome=parent)
    await _admit(
        store,
        strategy_id="strat-fork",
        genome=restructured,
        parents=(genome_hash(parent),),
        generation_number=1,
    )

    async with app_engine.connect() as connection:
        lineages = (
            await connection.scalars(sa.text("SELECT lineage_id FROM evolution.genome"))
        ).all()

    assert set(lineages) == {lineage_id_for(parent), lineage_id_for(restructured)}


@pytest.mark.asyncio
async def test_recording_the_same_genome_twice_keeps_the_first_creation_record(
    app_engine: AsyncEngine,
) -> None:
    """Identity is content, so a rediscovery is the same hypothesis, not a new one."""
    store = LineageStore(app_engine)
    genome = build_genome()
    await store.record_genome(build_record(genome))
    await store.record_genome(build_record(genome, trial_index_at_creation=9999))

    async with app_engine.connect() as connection:
        rows = (
            await connection.scalars(
                sa.text("SELECT trial_index_at_creation FROM evolution.genome")
            )
        ).all()

    assert list(rows) == [GENESIS_TRIAL_INDEX]


@pytest.mark.asyncio
async def test_a_crossover_child_records_both_parents_in_order(
    app_engine: AsyncEngine,
) -> None:
    store = LineageStore(app_engine)
    mother, father, child = tuple(unique_genomes(3))
    await store.record_genome(build_record(mother))
    await store.record_genome(build_record(father))
    await store.record_genome(
        build_record(
            child,
            generation_number=1,
            parent_genome_hashes=(genome_hash(mother), genome_hash(father)),
            mutation_operators=(MutationOperator.SUBTREE_CROSSOVER,),
        )
    )

    graph = await store.load_lineage_graph()

    assert graph.parents_of[genome_hash(child)] == (genome_hash(mother), genome_hash(father))


# ---------------------------------------------------------------------------
# The graph refuses what it cannot answer
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_a_parent_hash_that_is_absent_raises_a_foreign_key_violation(
    app_engine: AsyncEngine,
) -> None:
    """A dangling parent makes every ancestry walk from that child terminate silently,
    which reads as a founder."""
    store = LineageStore(app_engine)
    orphan = build_genome()
    absent = genome_hash(build_genome(parameters={"entry_threshold": Decimal("99")}))

    with pytest.raises(IntegrityError, match="fk_evolution_genome_parent_parent_genome"):
        await store.record_genome(
            build_record(orphan, parent_genome_hashes=(absent,), generation_number=1)
        )


@pytest.mark.asyncio
async def test_a_cycle_is_rejected_by_the_ancestry_walk_rather_than_stored(
    app_engine: AsyncEngine,
) -> None:
    """Two hops, which the depth-one CHECK cannot see."""
    store = LineageStore(app_engine)
    founder, middle, youngest = tuple(unique_genomes(3))
    await store.record_genome(build_record(founder))
    await store.record_genome(
        build_record(middle, generation_number=1, parent_genome_hashes=(genome_hash(founder),))
    )
    await store.record_genome(
        build_record(youngest, generation_number=2, parent_genome_hashes=(genome_hash(middle),))
    )

    with pytest.raises(LineageCycleError, match="would close a cycle"):
        await store.record_genome(
            build_record(founder, parent_genome_hashes=(genome_hash(youngest),))
        )

    async with app_engine.connect() as connection:
        edge_rows = await connection.scalar(sa.text("SELECT count(*) FROM evolution.genome_parent"))

    assert edge_rows == _GENESIS_PLUS_ONE
    assert len((await store.load_lineage_graph()).parents_of) == _GENESIS_PLUS_ONE


@pytest.mark.asyncio
async def test_a_self_parent_is_refused_by_the_database_too(
    app_engine: AsyncEngine,
) -> None:
    """The one cycle a CHECK can see, kept as a constraint because a mutation operator
    declaring a genome its own parent is the shape that actually occurs."""
    store = LineageStore(app_engine)
    genome = build_genome()
    await store.record_genome(build_record(genome))

    async with app_engine.begin() as connection:
        with pytest.raises(IntegrityError, match="acyclic_at_depth_one"):
            await connection.execute(
                sa.text(
                    "INSERT INTO evolution.genome_parent "
                    "(child_genome_hash, parent_genome_hash, parent_ordinal) "
                    "VALUES (:h, :h, 0)"
                ),
                {"h": bytes.fromhex(genome_hash(genome))},
            )


# ---------------------------------------------------------------------------
# Transitions are checked against the derived state
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_a_transition_from_a_state_the_strategy_is_not_in_is_refused(
    app_engine: AsyncEngine, strategy_id: str
) -> None:
    store = LineageStore(app_engine)
    genome = build_genome()
    genesis = await _admit(store, strategy_id=strategy_id, genome=genome)

    with pytest.raises(LifecycleTransitionError, match="is in proposed"):
        await store.append_lifecycle_event(
            next_transition(genesis, LifecycleState.CHAMPION, from_state=LifecycleState.CHALLENGER)
        )

    assert await store.current_state(strategy_id) is LifecycleState.PROPOSED


@pytest.mark.asyncio
async def test_retirement_is_terminal_in_the_store_as_well(
    app_engine: AsyncEngine, strategy_id: str
) -> None:
    store = LineageStore(app_engine)
    genome = build_genome()
    genesis = await _admit(store, strategy_id=strategy_id, genome=genome)
    retired = next_transition(
        genesis,
        LifecycleState.RETIRED,
        reason_class=ReasonClass.DEFECT,
        reason="failed the contract gate: reads the future",
    )
    await store.append_lifecycle_event(retired)

    with pytest.raises(LifecycleTransitionError, match="retired is terminal"):
        await store.append_lifecycle_event(
            next_transition(
                retired,
                LifecycleState.PROPOSED,
                reason_class=ReasonClass.OPERATOR,
                reason="give it another chance",
            )
        )


@pytest.mark.asyncio
async def test_a_genesis_event_must_come_from_nonexistent(
    app_engine: AsyncEngine, strategy_id: str
) -> None:
    store = LineageStore(app_engine)
    genome = build_genome()
    genesis = build_genesis(strategy_id, genome)

    with pytest.raises(LifecycleTransitionError, match="genesis event comes from"):
        await store.admit_strategy(
            strategy_id=strategy_id,
            record=build_record(genome),
            genesis_event=next_transition(genesis, LifecycleState.BACKTESTED),
        )


@pytest.mark.asyncio
async def test_a_strategy_with_no_events_cannot_be_left_behind(
    app_engine: AsyncEngine, strategy_id: str
) -> None:
    """The refused genesis rolls back the strategy row with it: one transaction."""
    store = LineageStore(app_engine)
    genesis = build_genesis(strategy_id)
    with pytest.raises(LifecycleTransitionError):
        await store.admit_strategy(
            strategy_id=strategy_id,
            record=build_record(),
            genesis_event=next_transition(genesis, LifecycleState.BACKTESTED),
        )

    async with app_engine.connect() as connection:
        strategies = await connection.scalar(sa.text("SELECT count(*) FROM evolution.strategy"))

    assert strategies == 0


@pytest.mark.asyncio
async def test_a_republished_event_is_refused_by_the_derived_state(
    app_engine: AsyncEngine, strategy_id: str
) -> None:
    """At-least-once delivery meets an append-only table.

    The first refusal is the derived state: the strategy has already moved, so the
    transition being redelivered is no longer the one to record. That is the layer a
    consumer sees, and it fires before anything reaches the table.
    """
    store = LineageStore(app_engine)
    genome = build_genome()
    genesis = await _admit(store, strategy_id=strategy_id, genome=genome)
    transition = next_transition(genesis, LifecycleState.BACKTESTED)
    await store.append_lifecycle_event(transition)

    with pytest.raises(LifecycleTransitionError, match="is in backtested"):
        await store.append_lifecycle_event(transition)

    assert len(await store.load_lifecycle_events(strategy_id)) == _GENESIS_PLUS_ONE


@pytest.mark.asyncio
async def test_a_duplicate_event_id_is_refused_by_the_database(
    app_engine: AsyncEngine, strategy_id: str
) -> None:
    """The second layer, which holds when the state check cannot.

    A row in an append-only table cannot be removed, so a duplicate that slipped past the
    application would be permanent -- which is why the producer's own event id carries a
    UNIQUE constraint rather than being trusted to be used correctly.
    """
    store = LineageStore(app_engine)
    genome = build_genome()
    await _admit(store, strategy_id=strategy_id, genome=genome)
    recorded = (await store.load_lifecycle_events(strategy_id))[0]

    async with app_engine.begin() as connection:
        with pytest.raises(IntegrityError, match="uq_lifecycle_event_event_id"):
            await connection.execute(
                sa.text(
                    """
                    INSERT INTO evolution.strategy_lifecycle_events (
                        event_id, strategy_id, genome_hash, correlation_id, from_state,
                        to_state, reason_class, reason, score_components,
                        independent_episode_count, forward_independent_episode_count,
                        global_trial_index, family_trial_index, scoring_version,
                        occurred_at_utc, prev_hash, row_hash
                    ) VALUES (
                        :event_id, :s, :g, gen_random_uuid(), 'proposed', 'backtested',
                        'gate_passed', 'redelivered', '{}'::jsonb, 0, 0, 1, 0,
                        'scoring-2026.08', now(), '\\x00'::bytea, '\\x00'::bytea
                    )
                    """
                ),
                {
                    "event_id": recorded.event_id,
                    "s": strategy_id,
                    "g": bytes.fromhex(genome_hash(genome)),
                },
            )


# ---------------------------------------------------------------------------
# Defect propagation and collapse, read through the database
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_quarantine_targets_are_every_descendant_at_unbounded_depth(
    app_engine: AsyncEngine,
) -> None:
    """A depth bound here would leave the great-grandchildren of a leak in production."""
    store = LineageStore(app_engine)
    generations = tuple(unique_genomes(_DEEP_LINEAGE_DEPTH + 1))
    await store.record_genome(build_record(generations[0]))
    for depth, genome in enumerate(generations[1:], start=1):
        await store.record_genome(
            build_record(
                genome,
                generation_number=depth,
                parent_genome_hashes=(genome_hash(generations[depth - 1]),),
            )
        )

    targets = await store.quarantine_targets(genome_hash(generations[0]))

    assert targets == frozenset(genome_hash(genome) for genome in generations[1:])
    assert genome_hash(generations[0]) not in targets
    assert len(targets) == _DEEP_LINEAGE_DEPTH


@pytest.mark.asyncio
async def test_the_collapse_report_flags_a_seeded_inbred_population(
    app_engine: AsyncEngine,
) -> None:
    """Six of eleven live strategies descend from one founder within five generations."""
    store = LineageStore(app_engine)
    founder, *rest = tuple(unique_genomes(1 + _DESCENDED_LIVE + _INDEPENDENT_LIVE))
    await store.record_genome(build_record(founder))

    live_states = (LifecycleState.PAPER, LifecycleState.CHALLENGER, LifecycleState.CHAMPION)
    for index, genome in enumerate(rest[:_DESCENDED_LIVE]):
        genesis = await _admit(
            store,
            strategy_id=f"strat-descended-{index}",
            genome=genome,
            parents=(genome_hash(founder),),
            generation_number=1,
        )
        await _promote_to(store, genesis, live_states[index % len(live_states)])
    for index, genome in enumerate(rest[_DESCENDED_LIVE:]):
        genesis = await _admit(store, strategy_id=f"strat-independent-{index}", genome=genome)
        await _promote_to(store, genesis, live_states[index % len(live_states)])

    report = await store.lineage_collapse_report()

    assert report.live_strategy_count == _DESCENDED_LIVE + _INDEPENDENT_LIVE
    assert report.max_generations == DEFAULT_COLLAPSE_MAX_GENERATIONS
    assert report.dominant_ancestor_genome_hash == genome_hash(founder)
    assert report.dominant_share_fraction > Decimal("0.5")
    assert report.is_collapsed


@pytest.mark.asyncio
async def test_a_retired_descendant_does_not_count_toward_a_collapse(
    app_engine: AsyncEngine,
) -> None:
    """An inbred set of retired genomes is history; only the live book is a portfolio."""
    store = LineageStore(app_engine)
    founder, *rest = tuple(unique_genomes(1 + _DESCENDED_LIVE + _INDEPENDENT_LIVE))
    await store.record_genome(build_record(founder))

    for index, genome in enumerate(rest[:_DESCENDED_LIVE]):
        genesis = await _admit(
            store,
            strategy_id=f"strat-descended-{index}",
            genome=genome,
            parents=(genome_hash(founder),),
            generation_number=1,
        )
        await store.append_lifecycle_event(
            next_transition(
                genesis,
                LifecycleState.RETIRED,
                reason_class=ReasonClass.DECAY,
                reason="score below the retention floor for two cycles",
            )
        )
    for index, genome in enumerate(rest[_DESCENDED_LIVE:]):
        genesis = await _admit(store, strategy_id=f"strat-independent-{index}", genome=genome)
        await _promote_to(store, genesis, LifecycleState.PAPER)

    report = await store.lineage_collapse_report()

    assert report.live_strategy_count == _INDEPENDENT_LIVE
    assert not report.is_collapsed
