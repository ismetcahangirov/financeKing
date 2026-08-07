"""Lifecycle states, the events that move between them, and the state derived from those.

**Events, not a mutable state column.** Every transition is an append-only row carrying
from-state, to-state, the survival score and its components, the sample counts, the trial
indices, the correlation id and the reason. Current state is *derived* -- the `to_state`
of the highest-sequence event -- and never stored anywhere the application can write.

The reason is not purity. A state column the application can `UPDATE` is a state column
somebody will correct during an incident, at 03:00, to make a dashboard render or to
unblock a deploy. The correction is exactly the row the investigation afterwards needed,
and it is the row that no longer exists. `ARCHITECTURE.md` section 11 requires that a
decision be fully reconstructable from the record alone, months later, with no access to
application memory; a state column cannot satisfy that even when nobody edits it, because
it records the answer and not the question.

**Sample counts are independent episodes, never raw observations.** 41,208 hourly bars
containing 37 funding-extremity episodes is a sample of 37. A t-statistic computed as if
it were 41,208 is off by roughly a factor of 33, in the flattering direction, and it is
the number a promotion gate reads.

`EVOLUTION_ENGINE.md` sections 1, 2 and 8.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import Decimal
from enum import StrEnum
from types import MappingProxyType
from typing import Final
from uuid import UUID

from fking.evolution._errors import IllegalTransitionError, LifecycleTransitionError

__all__ = [
    "CAPITAL_AUTHORITY",
    "LIVE_STATES",
    "PERMITTED_TRANSITIONS",
    "SCORED_STATES",
    "CapitalAuthority",
    "CapitalPosture",
    "LifecycleEvent",
    "LifecycleState",
    "ReasonClass",
    "capital_authority_for",
    "derive_current_state",
    "is_permitted_transition",
    "require_permitted_transition",
]


class LifecycleState(StrEnum):
    """`EVOLUTION_ENGINE.md` section 1, plus two states the diagram has and does not name."""

    NONEXISTENT = "nonexistent"
    """Before the strategy exists. Only ever a from-state.

    It exists so the genesis event carries a from-state like every other row, rather than
    a NULL that every reader then has to special-case -- and a reader who forgets is a
    reader whose population count silently omits its newest members.
    """

    PROPOSED = "proposed"
    BACKTESTED = "backtested"
    VALIDATED = "validated"
    PAPER = "paper"
    CHALLENGER = "challenger"
    CHAMPION = "champion"

    QUARANTINED = "quarantined"
    """A descendant of a `defect` retirement, capital withdrawn, re-test pending.

    Distinct from `retired` because "this was tested and failed" and "this inherited a
    bug from its parent and has not been re-tested" call for different actions, and
    collapsing them loses the second one entirely (section 8).
    """

    RETIRED = "retired"
    """Terminal. No outgoing edges, no reactivate, no unretire (section 8)."""


class ReasonClass(StrEnum):
    """Why a transition happened. The class, not the prose, is what a query groups by."""

    GENESIS = "genesis"
    GATE_PASSED = "gate_passed"
    GATE_FAILED = "gate_failed"
    DEFECT = "defect"
    RISK = "risk"
    DECAY = "decay"
    SUPERSEDED = "superseded"
    ENVIRONMENTAL = "environmental"
    QUARANTINE = "quarantine"
    OPERATOR = "operator"


LIVE_STATES: Final[frozenset[LifecycleState]] = frozenset(
    {LifecycleState.PAPER, LifecycleState.CHALLENGER, LifecycleState.CHAMPION}
)
"""States that hold capital or produce live decisions.

Lineage collapse is measured over these and not over the whole population: an inbred set
of retired genomes is history, while an inbred set of live ones is a portfolio whose
measured correlations are about to stop describing it (section 5.6).
"""

SCORED_STATES: Final[frozenset[LifecycleState]] = frozenset(
    {
        LifecycleState.VALIDATED,
        LifecycleState.PAPER,
        LifecycleState.CHALLENGER,
        LifecycleState.CHAMPION,
    }
)
"""States a strategy cannot enter without a score and the sample behind it."""


class CapitalPosture(StrEnum):
    """How much of the portfolio a state is allowed to move (section 1, section 3)."""

    NONE = "none"
    NOTIONAL_ONLY = "notional_only"
    """Positions are computed and scored; nothing reaches a venue."""

    FRACTIONAL = "fractional"
    FULL = "full"


@dataclass(frozen=True, slots=True)
class CapitalAuthority:
    """What a state may risk, as a fraction of a champion's risk budget.

    A fraction rather than an absolute so that the state machine states a *relative*
    authority and the risk engine keeps sole ownership of the absolute number. A state
    machine that named a notional would be sizing positions, which is the one thing it
    must not do (`RISK_PHILOSOPHY.md`).
    """

    posture: CapitalPosture
    risk_budget_fraction_of_champion: Decimal

    def __post_init__(self) -> None:
        if not Decimal(0) <= self.risk_budget_fraction_of_champion <= Decimal(1):
            raise LifecycleTransitionError(
                f"a risk budget fraction is on [0, 1], got {self.risk_budget_fraction_of_champion}"
            )
        reaches_a_venue = self.posture in {CapitalPosture.FRACTIONAL, CapitalPosture.FULL}
        if reaches_a_venue is (self.risk_budget_fraction_of_champion == Decimal(0)):
            raise LifecycleTransitionError(
                f"posture {self.posture.value} and fraction "
                f"{self.risk_budget_fraction_of_champion} disagree about whether this "
                f"state reaches a venue"
            )


# 25% of a champion's risk budget for a challenger, from EVOLUTION_ENGINE.md section 3.
# Deliberately non-zero and deliberately not a knob: a challenger evaluated at zero
# allocation is a paper strategy with a different label, and the entire point of the
# state is to measure partial fills, rejects, queue position and funding -- none of which
# a simulated fill produces. Lowering it to zero would silently delete the only evidence
# the `challenger -> champion` gate is allowed to read.
_CHALLENGER_RISK_BUDGET_FRACTION: Final = Decimal("0.25")

CAPITAL_AUTHORITY: Final[Mapping[LifecycleState, CapitalAuthority]] = MappingProxyType(
    {
        LifecycleState.NONEXISTENT: CapitalAuthority(CapitalPosture.NONE, Decimal(0)),
        LifecycleState.PROPOSED: CapitalAuthority(CapitalPosture.NONE, Decimal(0)),
        LifecycleState.BACKTESTED: CapitalAuthority(CapitalPosture.NONE, Decimal(0)),
        LifecycleState.VALIDATED: CapitalAuthority(CapitalPosture.NONE, Decimal(0)),
        LifecycleState.PAPER: CapitalAuthority(CapitalPosture.NOTIONAL_ONLY, Decimal(0)),
        LifecycleState.CHALLENGER: CapitalAuthority(
            CapitalPosture.FRACTIONAL, _CHALLENGER_RISK_BUDGET_FRACTION
        ),
        LifecycleState.CHAMPION: CapitalAuthority(CapitalPosture.FULL, Decimal(1)),
        # Quarantine is capital withdrawal first and a re-test second: a descendant of a
        # defective genome is presumed to carry the defect until it has been re-tested,
        # and the presumption is worthless if it keeps its allocation while it waits.
        LifecycleState.QUARANTINED: CapitalAuthority(CapitalPosture.NONE, Decimal(0)),
        LifecycleState.RETIRED: CapitalAuthority(CapitalPosture.NONE, Decimal(0)),
    }
)


def capital_authority_for(state: LifecycleState) -> CapitalAuthority:
    """What `state` may risk. Total over the enum, so a new state cannot default to zero.

    A `Mapping.get` with a zero default would be the friendlier spelling and the wrong
    one: adding a state and forgetting its authority would then read as "no capital",
    which is the answer that lets the omission survive review.
    """
    authority = CAPITAL_AUTHORITY.get(state)
    if authority is None:  # pragma: no cover - unreachable while the mapping is total
        raise LifecycleTransitionError(f"{state.value} declares no capital authority")
    return authority


PERMITTED_TRANSITIONS: Final[Mapping[LifecycleState, frozenset[LifecycleState]]] = MappingProxyType(
    {
        LifecycleState.NONEXISTENT: frozenset({LifecycleState.PROPOSED}),
        LifecycleState.PROPOSED: frozenset(
            {LifecycleState.BACKTESTED, LifecycleState.QUARANTINED, LifecycleState.RETIRED}
        ),
        LifecycleState.BACKTESTED: frozenset(
            {LifecycleState.VALIDATED, LifecycleState.QUARANTINED, LifecycleState.RETIRED}
        ),
        LifecycleState.VALIDATED: frozenset(
            {LifecycleState.PAPER, LifecycleState.QUARANTINED, LifecycleState.RETIRED}
        ),
        LifecycleState.PAPER: frozenset(
            {LifecycleState.CHALLENGER, LifecycleState.QUARANTINED, LifecycleState.RETIRED}
        ),
        LifecycleState.CHALLENGER: frozenset(
            {LifecycleState.CHAMPION, LifecycleState.QUARANTINED, LifecycleState.RETIRED}
        ),
        # No champion -> challenger edge. A demoted champion is retired, because
        # demotion means the evidence that promoted it has been superseded, and
        # keeping it in the pool makes the search re-examine a hypothesis it has
        # already tested (section 3).
        LifecycleState.CHAMPION: frozenset({LifecycleState.QUARANTINED, LifecycleState.RETIRED}),
        # The re-test re-enters at `backtested`, which is the arrow the section 1
        # diagram draws back into that box. Not at `proposed`: the genome's identity
        # is unchanged, so the contract gate it already passed still applies, and
        # re-running it would charge the same checks twice. Not at `validated`: the
        # CPCV evidence is exactly what a leaked feature invalidates.
        LifecycleState.QUARANTINED: frozenset({LifecycleState.BACKTESTED, LifecycleState.RETIRED}),
        LifecycleState.RETIRED: frozenset(),
    }
)
"""Every edge the lifecycle has. A pair absent from here is refused, not warned about.

An allowlist rather than a denylist of the three impossible shapes, because a denylist
answers "is this one of the mistakes we thought of" and an allowlist answers "is this one
of the transitions the design has". `proposed -> champion` is not a mistake anyone would
write deliberately; it is what a scheduler produces when a promotion loop reads a stale
state, and under a denylist it commits full allocation to a genome that has never been
backtested.
"""


def is_permitted_transition(from_state: LifecycleState, to_state: LifecycleState) -> bool:
    """Whether the edge exists. Callers deciding *what* to do next use this; callers
    recording a decision already made use `require_permitted_transition`."""
    return to_state in PERMITTED_TRANSITIONS[from_state]


def require_permitted_transition(from_state: LifecycleState, to_state: LifecycleState) -> None:
    """Refuse an edge the lifecycle does not have, naming both states.

    The three most common refusals get their own message before the table is consulted,
    because `retired -> proposed is not an edge` is the name of a rule and
    `retired is terminal` is the rule. Everything else falls through to the table, so a
    new state is refused everywhere until it is given edges rather than being silently
    reachable from nowhere.
    """
    if from_state is to_state:
        raise IllegalTransitionError(
            f"{to_state.value} -> {to_state.value} is not a transition; nothing moved"
        )
    if from_state is LifecycleState.RETIRED:
        raise IllegalTransitionError(
            "retired is terminal: there is no reactivate and no unretire. Re-testing a "
            "hypothesis you already rejected gives a second correlated look at the full "
            "statistical cost (EVOLUTION_ENGINE.md section 8)"
        )
    if to_state is LifecycleState.NONEXISTENT:
        raise IllegalTransitionError(
            "nonexistent is where a strategy comes from, never where it goes"
        )
    if not is_permitted_transition(from_state, to_state):
        raise IllegalTransitionError(
            f"{from_state.value} -> {to_state.value} is not an edge the lifecycle has; "
            f"from {from_state.value} the only destinations are "
            f"{sorted(state.value for state in PERMITTED_TRANSITIONS[from_state])}"
        )


def _require_utc(moment: datetime, field_name: str) -> datetime:
    if moment.tzinfo is None or moment.utcoffset() is None:
        raise LifecycleTransitionError(f"{field_name} must be timezone-aware, got {moment!r}")
    if moment.utcoffset() != UTC.utcoffset(None):
        raise LifecycleTransitionError(
            f"{field_name} must be UTC, got offset {moment.utcoffset()!r}. Converting here "
            f"would accept a value whose offset was guessed wrong upstream"
        )
    return moment


@dataclass(frozen=True, slots=True)
class LifecycleEvent:
    """One transition, carrying everything needed to re-derive why it happened.

    The test of sufficiency is not "did we record the event". It is: given only this row,
    can somebody months from now answer why this strategy was in this state at this
    moment? Anything computed and then discarded belongs in `score_components`.
    """

    event_id: UUID
    strategy_id: str
    genome_hash: str
    correlation_id: UUID
    causation_id: UUID | None
    from_state: LifecycleState
    to_state: LifecycleState
    reason_class: ReasonClass
    reason: str
    survival_score: Decimal | None
    score_components: Mapping[str, Decimal]
    independent_episode_count: int
    forward_independent_episode_count: int
    global_trial_index: int
    family_trial_index: int
    scoring_version: str
    occurred_at_utc: datetime

    def __post_init__(self) -> None:
        require_permitted_transition(self.from_state, self.to_state)
        _require_utc(self.occurred_at_utc, "occurred_at_utc")

        if not self.reason.strip():
            raise LifecycleTransitionError(
                "a transition states its reason; the reason class alone is a category, "
                "not an explanation"
            )
        if self.independent_episode_count < 0 or self.forward_independent_episode_count < 0:
            raise LifecycleTransitionError("episode counts are counts and cannot be negative")
        if self.global_trial_index < 0 or self.family_trial_index < 0:
            raise LifecycleTransitionError("trial indices cannot be negative")
        if self.family_trial_index > self.global_trial_index:
            raise LifecycleTransitionError(
                f"family trial index {self.family_trial_index} exceeds the global "
                f"{self.global_trial_index}; the family is a subset by construction, so "
                f"one of the two counters was reset"
            )
        if self.survival_score is not None and not Decimal(0) <= self.survival_score <= Decimal(1):
            raise LifecycleTransitionError(
                f"survival_score is a fraction on [0, 1], got {self.survival_score}"
            )
        if self.to_state in SCORED_STATES:
            self._require_evidence()

        object.__setattr__(self, "score_components", MappingProxyType(dict(self.score_components)))

    def _require_evidence(self) -> None:
        """A promotion with no evidence behind it is a decision that cannot be re-derived."""
        if self.survival_score is None or not self.score_components:
            raise LifecycleTransitionError(
                f"entering {self.to_state.value} requires a survival score and its "
                f"components; a score with no decomposition cannot be re-checked"
            )
        if self.independent_episode_count <= 0:
            raise LifecycleTransitionError(
                f"entering {self.to_state.value} requires a sample; zero independent "
                f"episodes is INSUFFICIENT_SAMPLE, not a low score"
            )
        if self.global_trial_index < 1:
            raise LifecycleTransitionError(
                f"entering {self.to_state.value} requires a trial count that was actually "
                f"read. Zero never means 'nothing was tried, so no deflation was needed'"
            )


def derive_current_state(events: Iterable[LifecycleEvent]) -> LifecycleState:
    """The state implied by an ordered event stream.

    `events` must already be in chain order -- the store yields them ordered by `seq`,
    which is the order they were written in. Re-sorting here by `occurred_at_utc` would
    be wrong: two transitions can share a decision instant, and the record's own order is
    the only total one.
    """
    ordered: Sequence[LifecycleEvent] = tuple(events)
    if not ordered:
        return LifecycleState.NONEXISTENT
    return ordered[-1].to_state
