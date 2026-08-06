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
    "EvolutionError",
    "GenomeError",
    "LifecycleTransitionError",
    "LineageCycleError",
    "LineageError",
    "SpecificationAlreadyRegisteredError",
    "SpecificationNotRegisteredError",
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


class TrialSpecificationError(EvolutionError):
    """A search was declared in a shape the ledger cannot charge.

    Raised at construction, before anything is written. A specification with an empty
    grid axis, a non-positive symbol count, or a holdout request with nobody's name
    against it would still produce a `spec_hash`, and that digest would then be a stable
    identity for a search whose charge is meaningless -- after which every deflated
    Sharpe computed against it is wrong in the flattering direction.
    """


class TrialLedgerError(EvolutionError):
    """The trial ledger refused a write, or could not answer a read.

    Distinct from `TrialSpecificationError` because the caller's options differ: a
    malformed specification is fixed by the caller, and a ledger that will not answer is
    a halt condition. Nothing may be promoted against an unreadable ledger, because a
    trial count of zero is indistinguishable from "nothing has been tried" and would
    report a searched result as an unsearched one.
    """


class SpecificationAlreadyRegisteredError(TrialLedgerError):
    """This exact search has already been charged.

    A refusal rather than a no-op, and the distinction is not pedantic. Two registrations
    of one grid mean either a duplicate charge -- which the ledger is monotone, so it
    could never be undone -- or a caller that believes it registered something it did
    not. The charge already recorded is reported in the message, so a retry after an
    ambiguous failure can tell the two apart without writing anything.
    """


class SpecificationNotRegisteredError(TrialLedgerError):
    """An execution was reported against a `spec_hash` that was never declared.

    The anti-HARKing gate seen from below: registration happens before any data access,
    so an execution with no declaration behind it is a search that was run and then
    described. A foreign key refuses it in the database, one layer under whichever caller
    did the reporting, so the refusal does not depend on that caller being the backtest
    engine.
    """
