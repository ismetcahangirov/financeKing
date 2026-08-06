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

from fking.evolution._errors import LifecycleTransitionError

__all__ = [
    "LIVE_STATES",
    "SCORED_STATES",
    "LifecycleEvent",
    "LifecycleState",
    "ReasonClass",
    "derive_current_state",
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


def require_permitted_transition(from_state: LifecycleState, to_state: LifecycleState) -> None:
    """Refuse a transition the lifecycle does not admit, naming both states.

    The database enforces all three of these as well. This raises first so that a caller
    gets `retired is terminal` rather than
    `ck_lifecycle_event_retired_is_terminal`, which is the name of a rule and not the
    rule.
    """
    if from_state is to_state:
        raise LifecycleTransitionError(
            f"{to_state.value} -> {to_state.value} is not a transition; nothing moved"
        )
    if from_state is LifecycleState.RETIRED:
        raise LifecycleTransitionError(
            "retired is terminal: there is no reactivate and no unretire. Re-testing a "
            "hypothesis you already rejected gives a second correlated look at the full "
            "statistical cost (EVOLUTION_ENGINE.md section 8)"
        )
    if to_state is LifecycleState.NONEXISTENT:
        raise LifecycleTransitionError(
            "nonexistent is where a strategy comes from, never where it goes"
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
