"""The evolution module's errors. Every one of them is a refusal, not a malfunction.

They are separate types rather than one `EvolutionError` with a message, because each is
caught in a different place and answered differently: a malformed genome is discarded by
the proposal loop without consuming a trial, a lineage cycle is a bug in a mutation
operator that must reach a human, and an unknown transition is a state machine drifting
away from `EVOLUTION_ENGINE.md` section 1.
"""

from __future__ import annotations

from fking.platform.errors import FkingError

__all__ = [
    "ContractGateError",
    "EvolutionError",
    "GenomeError",
    "IllegalTransitionError",
    "LifecycleTransitionError",
    "LineageCycleError",
    "LineageError",
    "TrialLedgerError",
    "TrialSpecificationError",
]


class EvolutionError(FkingError):
    """Base for every refusal this module issues."""


class GenomeError(EvolutionError):
    """A genome is malformed, so it has no identity and cannot be recorded.

    Raised at construction rather than at hashing. A genome whose horizon is negative or
    whose expression references an undeclared feature would still produce a digest, and
    that digest would be a stable identity for something that cannot run -- which is the
    worst outcome, because it would then be recorded, mutated and inherited.
    """


class LineageError(EvolutionError):
    """The genealogy graph was asked something it cannot answer."""


class LineageCycleError(LineageError):
    """A parent edge would make a genome its own ancestor.

    Content addressing does not prevent this on its own: a genome's hash digests its own
    expression and parameters, not its parents, so nothing stops a caller declaring an
    ancestor as a child. The consequences are worse than a wrong answer -- an unbounded
    ancestry walk hangs whichever loop is walking it, and the quarantine sweep in
    `EVOLUTION_ENGINE.md` section 8 is one of them.
    """


class LifecycleTransitionError(EvolutionError):
    """A proposed transition is not one the lifecycle admits.

    `retired` is terminal, `nonexistent` is only ever a from-state, and a strategy cannot
    transition to the state it is already in. The database enforces all three as well;
    this raises first so that the caller gets a message naming the states rather than a
    constraint name.
    """


class IllegalTransitionError(LifecycleTransitionError):
    """The proposed edge is not in `PERMITTED_TRANSITIONS`.

    Separate from its parent because the two failures are answered differently. A
    `LifecycleTransitionError` from the store means the strategy is not where the caller
    thought it was -- a stale read, and the answer is to re-derive and retry. An
    `IllegalTransitionError` means the caller believes in an edge the lifecycle does not have,
    which is a bug in whatever computed the destination and is never retried.
    """


class ContractGateError(EvolutionError):
    """The contract gate could not be evaluated, as distinct from returning a refusal.

    A gate that cannot run is not a gate that passed. Raised when the inputs the gate
    needs are absent or self-contradictory -- an empty available-feature set, a parent
    reference with no rationale to compare against -- because the alternative is a
    verdict computed from missing evidence, which reads as `admitted` for exactly the
    genomes nobody checked.
    """


class TrialSpecificationError(EvolutionError):
    """A search was described in a way the ledger cannot charge.

    Raised at construction, before any database call and therefore before any data
    access, which is the only point at which the answer is still cheap. A grid whose
    product is zero, a symbol universe with a duplicate, a holdout request with nobody's
    name against it: each of these would otherwise reach the ledger as a row that either
    charges nothing or charges something no reader can interpret -- and the ledger is
    append-only, so neither is correctable afterwards.
    """


class TrialLedgerError(EvolutionError):
    """The trial ledger refused a charge, or could not be read.

    Deliberately not distinguished from "the ledger is empty" at the call site. Zero
    trials and an unreadable ledger are the same condition for every caller that
    matters: both mean the selection pool is unknown, and deflating against an unknown
    pool reports a searched result as an unsearched one. `expected_max_sharpe` refuses a
    non-positive count for the same reason.
    """
