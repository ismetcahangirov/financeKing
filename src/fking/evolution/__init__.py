"""Strategy lifecycle, scoring and mutation. Knows about populations and survival.

Generating strategies is easy and mostly produces garbage; the hard part is rejecting
them correctly. The overfitting defences live here -- the global monotone trial
counter, the deflated Sharpe, and the promotion gate that requires forward
out-of-sample evidence rather than the validation performance that was selected on.

The public surface today is the lineage store and the identity it is keyed on:

- `fking.evolution.genome` -- content-addressed genome identity. `genome_hash` is what a
  hypothesis *is*; `structure_hash` is what it is *about*, and `lineage_id_for` derives
  the family from the second so a parameter-only mutation cannot mint a fresh lineage and
  escape its parent's accumulated trial count.
- `fking.evolution.lifecycle` -- states, the edges between them, the capital each state
  is allowed to move, the append-only events that record a move, and the current state
  derived from those events. There is no state column anywhere.
- `fking.evolution.lineage` -- the pure genealogy graph: ancestry, descendants, and the
  collapse report that escalates diversity pressure regardless of what the measured
  correlations say.
- `fking.evolution.store` -- the only writer of the `evolution` schema.

Modules whose names begin with `_` are internal and carry no compatibility promise.
"""

from fking.evolution._errors import (
    EvolutionError,
    GenomeError,
    IllegalTransitionError,
    LifecycleTransitionError,
    LineageCycleError,
    LineageError,
)
from fking.evolution.genome import (
    ExpressionNode,
    ExpressionType,
    Genome,
    MutationOperator,
    NodeKind,
    genome_hash,
    lineage_id_for,
    structure_hash,
)
from fking.evolution.lifecycle import (
    CAPITAL_AUTHORITY,
    LIVE_STATES,
    PERMITTED_TRANSITIONS,
    SCORED_STATES,
    CapitalAuthority,
    CapitalPosture,
    LifecycleEvent,
    LifecycleState,
    ReasonClass,
    capital_authority_for,
    derive_current_state,
    is_permitted_transition,
    require_permitted_transition,
)
from fking.evolution.lineage import (
    DEFAULT_COLLAPSE_MAX_GENERATIONS,
    DEFAULT_COLLAPSE_THRESHOLD_FRACTION,
    LineageCollapseReport,
    LineageGraph,
    lineage_collapse_report,
)
from fking.evolution.store import ChainVerification, GenomeRecord, LineageStore

__all__: tuple[str, ...] = (
    "CAPITAL_AUTHORITY",
    "DEFAULT_COLLAPSE_MAX_GENERATIONS",
    "DEFAULT_COLLAPSE_THRESHOLD_FRACTION",
    "LIVE_STATES",
    "PERMITTED_TRANSITIONS",
    "SCORED_STATES",
    "CapitalAuthority",
    "CapitalPosture",
    "ChainVerification",
    "EvolutionError",
    "ExpressionNode",
    "ExpressionType",
    "Genome",
    "GenomeError",
    "GenomeRecord",
    "IllegalTransitionError",
    "LifecycleEvent",
    "LifecycleState",
    "LifecycleTransitionError",
    "LineageCollapseReport",
    "LineageCycleError",
    "LineageError",
    "LineageGraph",
    "LineageStore",
    "MutationOperator",
    "NodeKind",
    "ReasonClass",
    "capital_authority_for",
    "derive_current_state",
    "genome_hash",
    "is_permitted_transition",
    "lineage_collapse_report",
    "lineage_id_for",
    "require_permitted_transition",
    "structure_hash",
)
