"""The lineage store: the only writer of the `evolution` schema.

Three things happen here that cannot happen anywhere else, and each one is the reason
this module exists rather than the calls being made inline by whoever needs them.

**A cycle is refused before the insert.** `record_genome` loads the edge set, asks the
pure `LineageGraph` whether the proposed parent is already downstream of the child, and
raises `LineageCycleError` if it is. The database can only see depth one -- a `CHECK`
cannot walk a graph -- so a two-hop cycle would otherwise be stored, and every ancestry
walk from that point on would run forever. The quarantine sweep after a look-ahead leak
is one of those walks, which is the worst possible loop to hang.

**A transition is checked against the derived state, not against a column.**
`append_lifecycle_event` reads the current state out of the event stream and refuses an
event whose `from_state` disagrees with it. That check is the whole reason there is no
state column: an event stream plus a writable column has two answers to one question, and
the column is the one that gets corrected during an incident.

**Nothing here updates or deletes.** There is no `update_*`, no `delete_*` and no
`correct_*` method, and the database would refuse one anyway -- `fking_app` holds only
`SELECT` and `INSERT`, and a `BEFORE UPDATE OR DELETE` trigger raises regardless of who
holds what. A correction is a new event whose `causation_id` points at the row being
corrected. `.claude/rules/append-only-audit.md`.

Hashes cross this boundary as lowercase hex `str` and are stored as `BYTEA`. Hex on the
Python side because a genome hash appears in log lines, issue titles and lineage ids;
`BYTEA` in the database because a 32-byte digest stored as 64 characters doubles every
index that carries it, and because `octet_length(...) = 32` is a constraint that a text
column cannot express.
"""

from __future__ import annotations

import json
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
from typing import Final
from uuid import UUID

import sqlalchemy as sa
from sqlalchemy.ext.asyncio import AsyncConnection, AsyncEngine

from fking.domain import JsonValue
from fking.evolution._errors import LifecycleTransitionError, LineageCycleError
from fking.evolution.genome import (
    Genome,
    MutationOperator,
    canonical_payload,
    genome_hash,
    lineage_id_for,
    structure_hash,
)
from fking.evolution.lifecycle import (
    LIVE_STATES,
    LifecycleEvent,
    LifecycleState,
    ReasonClass,
    derive_current_state,
    require_permitted_transition,
)
from fking.evolution.lineage import (
    DEFAULT_COLLAPSE_MAX_GENERATIONS,
    DEFAULT_COLLAPSE_THRESHOLD_FRACTION,
    LineageCollapseReport,
    LineageGraph,
    lineage_collapse_report,
)

__all__ = ["ChainVerification", "GenomeRecord", "LineageStore"]

_LIVE_STATE_NAMES: Final[tuple[str, ...]] = tuple(sorted(state.value for state in LIVE_STATES))


@dataclass(frozen=True, slots=True)
class GenomeRecord:
    """A genome plus everything about its creation that its digest deliberately excludes.

    None of these fields is in `genome_hash`, and that is the point: two runs that build
    the same hypothesis must land on the same identity even though one of them was
    generation 3 at trial 1,204 and the other was generation 11 at trial 48,730.
    """

    genome: Genome
    generation_number: int
    trial_index_at_creation: int
    mutation_operators: tuple[MutationOperator, ...]
    scoring_version: str
    parent_genome_hashes: tuple[str, ...]
    created_at_utc: datetime

    @property
    def genome_hash(self) -> str:
        return genome_hash(self.genome)

    @property
    def structure_hash(self) -> str:
        return structure_hash(self.genome)

    @property
    def lineage_id(self) -> str:
        return lineage_id_for(self.genome)


@dataclass(frozen=True, slots=True)
class ChainVerification:
    """The result of re-deriving every row hash in the lifecycle event stream.

    A non-null `first_broken_seq` pages a human immediately and is treated as a live risk
    incident rather than a data-quality ticket: neither the grants nor the trigger can see
    a superuser rewrite or a restore from a doctored dump, so the chain is the only thing
    that can, and a break in it means the record is no longer evidence.
    """

    checked_rows: int
    first_broken_seq: int | None
    reason: str | None

    @property
    def is_intact(self) -> bool:
        return self.first_broken_seq is None


_INSERT_GENOME: Final[sa.TextClause] = sa.text(
    """
    INSERT INTO evolution.genome (
        genome_hash, structure_hash, lineage_id, generation_number,
        trial_index_at_creation, mutation_operators, scoring_version, expression,
        parameters, feature_ids, holding_horizon_microseconds, created_at_utc
    )
    VALUES (
        :genome_hash, :structure_hash, :lineage_id, :generation_number,
        :trial_index_at_creation, CAST(:mutation_operators AS jsonb), :scoring_version,
        CAST(:expression AS jsonb), CAST(:parameters AS jsonb), CAST(:feature_ids AS jsonb),
        :holding_horizon_microseconds, :created_at_utc
    )
    -- A genome is content-addressed, so a second recording of the same hash is the same
    -- hypothesis and not a conflict. DO NOTHING rather than DO UPDATE because the row
    -- records when this hypothesis was *first* created and at which trial index, and an
    -- upsert would move both forward every time a mutation operator rediscovered it.
    ON CONFLICT (genome_hash) DO NOTHING
    """
)

_INSERT_PARENT: Final[sa.TextClause] = sa.text(
    """
    INSERT INTO evolution.genome_parent
        (child_genome_hash, parent_genome_hash, parent_ordinal)
    VALUES (:child_genome_hash, :parent_genome_hash, :parent_ordinal)
    ON CONFLICT (child_genome_hash, parent_genome_hash) DO NOTHING
    """
)

_INSERT_STRATEGY: Final[sa.TextClause] = sa.text(
    """
    INSERT INTO evolution.strategy (strategy_id, genome_hash, lineage_id, created_at_utc)
    VALUES (:strategy_id, :genome_hash, :lineage_id, :created_at_utc)
    """
)

_INSERT_EVENT: Final[sa.TextClause] = sa.text(
    """
    INSERT INTO evolution.strategy_lifecycle_events (
        event_id, strategy_id, genome_hash, correlation_id, causation_id, from_state,
        to_state, reason_class, reason, survival_score, score_components,
        independent_episode_count, forward_independent_episode_count, global_trial_index,
        family_trial_index, scoring_version, occurred_at_utc, prev_hash, row_hash
    )
    VALUES (
        :event_id, :strategy_id, :genome_hash, :correlation_id, :causation_id,
        :from_state, :to_state, :reason_class, :reason, :survival_score,
        CAST(:score_components AS jsonb), :independent_episode_count,
        :forward_independent_episode_count, :global_trial_index, :family_trial_index,
        :scoring_version, :occurred_at_utc,
        -- Placeholders. The BEFORE INSERT trigger overwrites both, which is the point:
        -- a writer that computes its own chain values can compute consistent ones for a
        -- forged row too.
        '\\x00'::bytea, '\\x00'::bytea
    )
    RETURNING seq
    """
)

_SELECT_EVENTS: Final[sa.TextClause] = sa.text(
    """
    SELECT seq, event_id, strategy_id, genome_hash, correlation_id, causation_id,
           from_state, to_state, reason_class, reason, survival_score, score_components,
           independent_episode_count, forward_independent_episode_count,
           global_trial_index, family_trial_index, scoring_version, occurred_at_utc
      FROM evolution.strategy_lifecycle_events
     WHERE strategy_id = :strategy_id
     -- seq, not occurred_at_utc: two transitions can share a decision instant and the
     -- chain order is the only total one.
     ORDER BY seq
    """
)


def _decimal_mapping(payload: object) -> Mapping[str, Decimal]:
    """Read a JSONB object of decimal-as-string back into exact `Decimal`s.

    A JSON *number* would already have been through a double by the time it reached here,
    so the writer emits strings and this refuses anything else rather than repairing it.
    """
    if not isinstance(payload, dict):
        raise LifecycleTransitionError(
            f"score_components must be a JSON object, got {type(payload).__name__}"
        )
    for name, component in payload.items():
        if not isinstance(component, str):
            raise LifecycleTransitionError(
                f"score component {name!r} must be a JSON string, not a JSON number: a "
                f"number has already lost precision by the time it is read"
            )
    return {str(name): Decimal(component) for name, component in payload.items()}


def _encoded_expression(genome: Genome) -> JsonValue:
    """The tree exactly as it is digested, so the row and the hash cannot disagree."""
    payload = canonical_payload(genome, include_parameter_values=True)
    if not isinstance(payload, dict):  # pragma: no cover - canonical_payload returns a dict
        raise TypeError("canonical_payload must produce an object")
    return payload["entry_rule"]


class LineageStore:
    """Reads and appends the evolution record. Never updates, never deletes."""

    def __init__(self, engine: AsyncEngine) -> None:
        self._engine = engine

    # -- writes ------------------------------------------------------------------------

    async def record_genome(self, record: GenomeRecord) -> str:
        """Record a genome and its parent edges, refusing a cycle before storing one."""
        async with self._engine.begin() as connection:
            await self._record_genome(connection, record)
        return record.genome_hash

    async def admit_strategy(
        self,
        *,
        strategy_id: str,
        record: GenomeRecord,
        genesis_event: LifecycleEvent,
    ) -> int:
        """Create a strategy and its genesis event in one transaction, returning the seq.

        One transaction because a strategy row with no events has no state at all: it
        would appear in the population with `current_state` NULL, which is the anomaly
        the `LEFT JOIN` in `evolution.strategy_current_state` exists to make visible
        rather than a state the system should ever be able to reach.
        """
        if genesis_event.from_state is not LifecycleState.NONEXISTENT:
            raise LifecycleTransitionError(
                f"a genesis event comes from {LifecycleState.NONEXISTENT.value}, not from "
                f"{genesis_event.from_state.value}"
            )
        async with self._engine.begin() as connection:
            await self._record_genome(connection, record)
            await connection.execute(
                _INSERT_STRATEGY,
                {
                    "strategy_id": strategy_id,
                    "genome_hash": bytes.fromhex(record.genome_hash),
                    "lineage_id": record.lineage_id,
                    "created_at_utc": record.created_at_utc,
                },
            )
            return await self._append(connection, genesis_event)

    async def append_lifecycle_event(self, event: LifecycleEvent) -> int:
        """Append one transition, returning its chain sequence number.

        The `from_state` is checked against the state derived from the stream, so an
        event that describes a transition the strategy is not in a position to make is
        refused rather than recorded -- and a refused transition leaves a stream that
        still reconstructs to exactly one current state.
        """
        async with self._engine.begin() as connection:
            observed = derive_current_state(await self._load_events(connection, event.strategy_id))
            if observed is not event.from_state:
                raise LifecycleTransitionError(
                    f"{event.strategy_id} is in {observed.value}, so a transition from "
                    f"{event.from_state.value} is not the one to record"
                )
            require_permitted_transition(event.from_state, event.to_state)
            return await self._append(connection, event)

    async def _record_genome(self, connection: AsyncConnection, record: GenomeRecord) -> None:
        child = record.genome_hash
        if record.parent_genome_hashes:
            graph = await self._load_lineage_graph(connection)
            for parent in record.parent_genome_hashes:
                if graph.would_create_cycle(child_genome_hash=child, parent_genome_hash=parent):
                    raise LineageCycleError(
                        f"declaring {parent} a parent of {child} would close a cycle; "
                        f"{child} is already upstream of it"
                    )

        await connection.execute(
            _INSERT_GENOME,
            {
                "genome_hash": bytes.fromhex(child),
                "structure_hash": bytes.fromhex(record.structure_hash),
                "lineage_id": record.lineage_id,
                "generation_number": record.generation_number,
                "trial_index_at_creation": record.trial_index_at_creation,
                "mutation_operators": json.dumps(
                    [operator.value for operator in record.mutation_operators]
                ),
                "scoring_version": record.scoring_version,
                "expression": json.dumps(_encoded_expression(record.genome), sort_keys=True),
                "parameters": json.dumps(
                    {name: str(parameter) for name, parameter in record.genome.parameters.items()},
                    sort_keys=True,
                ),
                "feature_ids": json.dumps(sorted(record.genome.feature_ids)),
                "holding_horizon_microseconds": record.genome.holding_horizon_microseconds,
                "created_at_utc": record.created_at_utc,
            },
        )
        for ordinal, parent in enumerate(record.parent_genome_hashes):
            await connection.execute(
                _INSERT_PARENT,
                {
                    "child_genome_hash": bytes.fromhex(child),
                    "parent_genome_hash": bytes.fromhex(parent),
                    "parent_ordinal": ordinal,
                },
            )

    @staticmethod
    async def _append(connection: AsyncConnection, event: LifecycleEvent) -> int:
        seq = await connection.scalar(
            _INSERT_EVENT,
            {
                "event_id": event.event_id,
                "strategy_id": event.strategy_id,
                "genome_hash": bytes.fromhex(event.genome_hash),
                "correlation_id": event.correlation_id,
                "causation_id": event.causation_id,
                "from_state": event.from_state.value,
                "to_state": event.to_state.value,
                "reason_class": event.reason_class.value,
                "reason": event.reason,
                "survival_score": event.survival_score,
                "score_components": json.dumps(
                    {name: str(component) for name, component in event.score_components.items()},
                    sort_keys=True,
                ),
                "independent_episode_count": event.independent_episode_count,
                "forward_independent_episode_count": event.forward_independent_episode_count,
                "global_trial_index": event.global_trial_index,
                "family_trial_index": event.family_trial_index,
                "scoring_version": event.scoring_version,
                "occurred_at_utc": event.occurred_at_utc,
            },
        )
        return int(str(seq))

    # -- reads -------------------------------------------------------------------------

    async def load_lifecycle_events(self, strategy_id: str) -> tuple[LifecycleEvent, ...]:
        """Every transition for one strategy, in chain order, reconstructed in full.

        Nothing here consults application state: `ARCHITECTURE.md` section 11 requires
        that a decision be answerable from the record alone, months later, and a loader
        that filled a missing field from a default would report success having
        reconstructed an event that never happened.
        """
        async with self._engine.connect() as connection:
            return await self._load_events(connection, strategy_id)

    async def current_state(self, strategy_id: str) -> LifecycleState:
        """Derived from the event stream. There is no column to read instead."""
        return derive_current_state(await self.load_lifecycle_events(strategy_id))

    async def load_lineage_graph(self) -> LineageGraph:
        async with self._engine.connect() as connection:
            return await self._load_lineage_graph(connection)

    async def live_genome_hashes(self) -> tuple[str, ...]:
        """The genomes of every strategy whose derived state holds capital."""
        async with self._engine.connect() as connection:
            rows = (
                await connection.execute(
                    sa.text(
                        """
                        SELECT genome_hash
                          FROM evolution.strategy_current_state
                         WHERE current_state = ANY(:live_states)
                         ORDER BY strategy_id
                        """
                    ),
                    {"live_states": list(_LIVE_STATE_NAMES)},
                )
            ).all()
        return tuple(bytes(row[0]).hex() for row in rows)

    async def lineage_collapse_report(
        self,
        *,
        max_generations: int = DEFAULT_COLLAPSE_MAX_GENERATIONS,
        threshold_fraction: Decimal = DEFAULT_COLLAPSE_THRESHOLD_FRACTION,
    ) -> LineageCollapseReport:
        """Whether one ancestor accounts for too much of the live book.

        The edges and the live set are loaded here; the arithmetic is the pure function in
        `fking.evolution.lineage`, so the same report is reproducible from an archived
        edge list with no database at all.
        """
        graph = await self.load_lineage_graph()
        live = await self.live_genome_hashes()
        return lineage_collapse_report(
            graph,
            live,
            max_generations=max_generations,
            threshold_fraction=threshold_fraction,
        )

    async def quarantine_targets(self, defective_genome_hash: str) -> frozenset[str]:
        """Every genome downstream of a defect, at unbounded depth.

        Read through the database's own recursive walk rather than the in-memory graph:
        this is called during an incident, when the population may be large and the
        answer must not depend on having loaded every edge into one process first.
        """
        async with self._engine.connect() as connection:
            rows = (
                await connection.execute(
                    sa.text(
                        "SELECT genome_hash, generations_forward "
                        "FROM evolution.genome_descendants(:genome_hash)"
                    ),
                    {"genome_hash": bytes.fromhex(defective_genome_hash)},
                )
            ).all()
        return frozenset(bytes(row[0]).hex() for row in rows if int(row[1]) > 0)

    async def verify_lifecycle_chain(self, *, since_seq: int = 0) -> ChainVerification:
        async with self._engine.connect() as connection:
            row = (
                await connection.execute(
                    sa.text(
                        "SELECT checked_rows, first_broken_seq, reason "
                        "FROM evolution.verify_lifecycle_chain(:since_seq)"
                    ),
                    {"since_seq": since_seq},
                )
            ).one()
        broken = row[1]
        detail = row[2]
        return ChainVerification(
            checked_rows=int(row[0]),
            first_broken_seq=None if broken is None else int(broken),
            reason=None if detail is None else str(detail),
        )

    # -- internals ---------------------------------------------------------------------

    @staticmethod
    async def _load_lineage_graph(connection: AsyncConnection) -> LineageGraph:
        rows = (
            await connection.execute(
                sa.text(
                    "SELECT child_genome_hash, parent_genome_hash, parent_ordinal "
                    "FROM evolution.genome_parent ORDER BY child_genome_hash, parent_ordinal"
                )
            )
        ).all()
        parents_of: dict[str, list[str]] = {}
        for row in rows:
            parents_of.setdefault(bytes(row[0]).hex(), []).append(bytes(row[1]).hex())
        # LineageGraph validates acyclicity at construction, so a graph that somehow holds
        # a cycle raises here -- on the read, before anything walks it.
        return LineageGraph({child: tuple(parents) for child, parents in parents_of.items()})

    @staticmethod
    async def _load_events(
        connection: AsyncConnection, strategy_id: str
    ) -> tuple[LifecycleEvent, ...]:
        rows: Sequence[sa.RowMapping] = (
            (await connection.execute(_SELECT_EVENTS, {"strategy_id": strategy_id}))
            .mappings()
            .all()
        )
        return tuple(
            LifecycleEvent(
                event_id=UUID(str(row["event_id"])),
                strategy_id=str(row["strategy_id"]),
                genome_hash=bytes(row["genome_hash"]).hex(),
                correlation_id=UUID(str(row["correlation_id"])),
                causation_id=(
                    None if row["causation_id"] is None else UUID(str(row["causation_id"]))
                ),
                from_state=LifecycleState(str(row["from_state"])),
                to_state=LifecycleState(str(row["to_state"])),
                reason_class=ReasonClass(str(row["reason_class"])),
                reason=str(row["reason"]),
                survival_score=(
                    None if row["survival_score"] is None else Decimal(str(row["survival_score"]))
                ),
                score_components=_decimal_mapping(row["score_components"]),
                independent_episode_count=int(row["independent_episode_count"]),
                forward_independent_episode_count=int(row["forward_independent_episode_count"]),
                global_trial_index=int(row["global_trial_index"]),
                family_trial_index=int(row["family_trial_index"]),
                scoring_version=str(row["scoring_version"]),
                occurred_at_utc=_as_datetime(row["occurred_at_utc"]),
            )
            for row in rows
        )


def _as_datetime(candidate: object) -> datetime:
    if not isinstance(candidate, datetime):  # pragma: no cover - TIMESTAMPTZ always yields one
        raise LifecycleTransitionError(f"occurred_at_utc came back as {type(candidate).__name__}")
    return candidate
