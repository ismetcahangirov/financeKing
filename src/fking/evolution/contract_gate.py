"""The gate that admits a generated genome into evaluation at all.

**Deterministic and score-free.** Nothing here reads a return, a Sharpe or a survival
score. A genome that fails is not a weak hypothesis whose number came out low; it is not
a hypothesis. `EVOLUTION_ENGINE.md` section 2 puts the gate on `proposed -> backtested`
for exactly that reason -- a defect must be caught before any evidence exists to argue
about, because once a number exists somebody will argue the defect is small relative to
it.

**Failure is `retired` with reason class `defect`, immediately, plus a tombstone.** Not a
warning, not a retry, not a lower rank in a queue. `defect_reason` renders the refusal
into the sentence the `retired` event carries, so the reason in the record is the same
string the gate produced rather than a paraphrase written at the call site.

**Every check runs; none short-circuits.** A proposal that fails three checks reports
three. Short-circuiting turns one round trip into three, and each round trip is a fresh
proposal that charges the trial ledger again -- so the cheap optimisation inside the gate
is paid for, at a much worse exchange rate, in the currency that gates the whole project.

## What this module checks, and what it does not, yet

Section 2 lists eight checks. Five are properties of the genome and its declared
environment, and are implemented here:

- every requested feature is declared available by the feature store;
- a falsifiable thesis is stated, with an explicit invalidation claim;
- the rationale is not a restatement of the invalidation claim;
- the rationale differs from the parent's when the logic differs;
- the genome is not a resubmission of a tombstoned hypothesis.

The other three -- type-checking under `mypy --strict`, purity verified by executing the
same bar sequence twice under different wall-clock times, and the adversarial look-ahead
poison test -- are properties of a *running* strategy. A `Genome` is a typed expression
tree; nothing in this repository yet turns one into a `fking.strategy.Strategy`, so there
is no artefact for those three to execute. They are absent rather than stubbed: a check
that always passes is worse than a missing check, because the verdict claims coverage the
gate does not have. `ContractCheck` therefore enumerates only what is evaluated, and a
caller reading `verdict.checks_run` sees exactly which questions were asked.
"""

from __future__ import annotations

from collections.abc import Collection, Mapping, Sequence
from dataclasses import dataclass
from enum import StrEnum
from types import MappingProxyType
from typing import Final

from fking.evolution._errors import ContractGateError
from fking.evolution.genome import Genome, genome_hash, structure_hash

__all__ = [
    "COMPILED_THRESHOLD_FLOORS",
    "ContractCheck",
    "ContractGateThresholds",
    "ContractGateVerdict",
    "ContractViolation",
    "GenomeProposal",
    "ParentThesis",
    "evaluate_contract_gate",
]


class ContractCheck(StrEnum):
    """The questions this gate asks. Each one is answerable without running the genome."""

    FEATURES_AVAILABLE = "features_available"
    """Every declared feature exists in the feature store's catalogue.

    An undeclared read is refused at the store, so this failure would otherwise surface
    as a backtest that crashed partway through -- after the trial had been charged.
    """

    INVALIDATION_DECLARED = "invalidation_declared"
    """The proposal states what would prove it wrong.

    A thesis with no invalidation is unfalsifiable, and an unfalsifiable thesis cannot be
    retired for `environmental` reasons because there is nothing to observe breaking.
    """

    RATIONALE_STATES_A_THESIS = "rationale_states_a_thesis"
    """The rationale is a mechanism, not a restatement of the invalidation clause."""

    RATIONALE_TRACKS_THE_LOGIC = "rationale_tracks_the_logic"
    """A mutant whose structure changed carries a rationale that also changed.

    This is the cheapest check here and it catches the most common machine-authored
    failure: mutate the expression tree, copy the parent's justification. The copied
    sentence then describes a hypothesis nobody holds, and every human reading the
    lineage afterwards reads a false account of what was tested.
    """

    GENOME_NOT_TOMBSTONED = "genome_not_tombstoned"
    """The hypothesis has not already been tested and retired.

    Rediscovery costs a full evaluation, burns trials, raises the deflation benchmark for
    the whole population and produces no new information -- and from the proposal loop's
    perspective each rediscovery looks like a novel candidate (section 8).
    """


@dataclass(frozen=True, slots=True)
class ContractViolation:
    """One refused check, with the detail needed to fix it without re-running the gate."""

    check: ContractCheck
    detail: str

    def __post_init__(self) -> None:
        if not self.detail.strip():
            raise ContractGateError(
                f"{self.check.value} was recorded as failed with no detail; a refusal "
                f"nobody can act on produces a resubmission of the same genome"
            )


# Compiled-in floors. Configuration may raise any of these and may not lower one, in the
# same pattern as risk limits: a gate configuration can loosen is a gate somebody loosens
# at 03:00 with positions open, and the loosening outlives the incident.
#
# The character counts are deliberately modest. They are a floor on effort, not a proxy
# for quality -- a gate that tried to judge a rationale's content would be scoring, and
# this gate is score-free by construction.
COMPILED_THRESHOLD_FLOORS: Final[Mapping[str, int]] = MappingProxyType(
    {
        "min_rationale_chars": 40,
        "min_invalidation_chars": 20,
    }
)


@dataclass(frozen=True, slots=True)
class ContractGateThresholds:
    """Gate thresholds. Constructing one below a compiled floor raises.

    Raising rather than clamping to the floor: a clamp accepts a configuration file that
    asks for something the system will not do and then does something else, so the
    operator's stated intent and the running behaviour diverge silently. A refusal at
    startup makes the disagreement a deployment failure, which is where it is cheap.
    """

    min_rationale_chars: int = COMPILED_THRESHOLD_FLOORS["min_rationale_chars"]
    min_invalidation_chars: int = COMPILED_THRESHOLD_FLOORS["min_invalidation_chars"]

    def __post_init__(self) -> None:
        configured = {
            "min_rationale_chars": self.min_rationale_chars,
            "min_invalidation_chars": self.min_invalidation_chars,
        }
        for name, floor in COMPILED_THRESHOLD_FLOORS.items():
            if configured[name] < floor:
                raise ContractGateError(
                    f"{name}={configured[name]} is below the compiled floor {floor}; "
                    f"configuration may only make a contract gate stricter"
                )


@dataclass(frozen=True, slots=True)
class ParentThesis:
    """What the proposal was mutated from, as far as the gate needs to know.

    The parent's `structure_hash` rather than its `genome_hash`: a parameter-only jitter
    is the same claim about the world at different settings, so requiring a fresh
    rationale for one would train proposers to paraphrase, and a paraphrase is exactly
    the signal this gate is trying to keep meaningful.
    """

    structure_hash: str
    rationale: str

    def __post_init__(self) -> None:
        if not self.structure_hash.strip():
            raise ContractGateError("a parent thesis carries the parent's structure hash")
        if not self.rationale.strip():
            raise ContractGateError(
                "a parent thesis carries the parent's rationale; comparing against an "
                "absent one would pass RATIONALE_TRACKS_THE_LOGIC by default, which is "
                "the check reporting a result it did not compute"
            )


@dataclass(frozen=True, slots=True)
class GenomeProposal:
    """A genome plus the claims its author makes about it.

    The rationale and the invalidation claim are separate fields rather than one block of
    prose because they are answerable separately: the first says why this should work,
    the second says what observation would end it, and a gate that reads one string
    cannot tell whether the second was ever written.
    """

    genome: Genome
    rationale: str
    invalidation_claim: str
    parent: ParentThesis | None = None

    @property
    def genome_hash(self) -> str:
        return genome_hash(self.genome)

    @property
    def structure_hash(self) -> str:
        return structure_hash(self.genome)


@dataclass(frozen=True, slots=True)
class ContractGateVerdict:
    """The gate's answer, carrying what was asked as well as what failed.

    `checks_run` is recorded rather than implied, so a verdict stored today is still
    readable after the gate grows a check: an old row shows the shorter list, and nobody
    has to infer from a date which checks a genome actually faced.
    """

    genome_hash: str
    checks_run: tuple[ContractCheck, ...]
    violations: tuple[ContractViolation, ...]

    @property
    def is_admitted(self) -> bool:
        return not self.violations

    def defect_reason(self) -> str:
        """The sentence the `retired`/`defect` lifecycle event carries.

        Rendered here so the reason in the append-only record is the gate's own text.
        A reason paraphrased at the call site is a reason that drifts from the check,
        and the record is the only thing an investigation months later has.
        """
        if self.is_admitted:
            raise ContractGateError(
                f"{self.genome_hash} passed the contract gate; there is no defect to "
                f"record, and writing one would tombstone an admissible hypothesis"
            )
        failures = "; ".join(
            f"{violation.check.value}: {violation.detail}" for violation in self.violations
        )
        return f"contract gate refused {self.genome_hash}: {failures}"


def _check_features_available(
    proposal: GenomeProposal, available_feature_ids: Collection[str]
) -> ContractViolation | None:
    missing = sorted(proposal.genome.feature_ids - frozenset(available_feature_ids))
    if not missing:
        return None
    return ContractViolation(
        check=ContractCheck.FEATURES_AVAILABLE,
        detail=(
            f"declares {missing}, which the feature store does not offer; the store "
            f"refuses an undeclared read and a silent substitution is worse"
        ),
    )


def _check_invalidation_declared(
    proposal: GenomeProposal, thresholds: ContractGateThresholds
) -> ContractViolation | None:
    claim = proposal.invalidation_claim.strip()
    if len(claim) >= thresholds.min_invalidation_chars:
        return None
    return ContractViolation(
        check=ContractCheck.INVALIDATION_DECLARED,
        detail=(
            f"the invalidation claim is {len(claim)} characters against a floor of "
            f"{thresholds.min_invalidation_chars}; a thesis nothing could falsify cannot "
            f"be retired for observing the world change"
        ),
    )


def _check_rationale_states_a_thesis(
    proposal: GenomeProposal, thresholds: ContractGateThresholds
) -> ContractViolation | None:
    rationale = proposal.rationale.strip()
    if len(rationale) < thresholds.min_rationale_chars:
        return ContractViolation(
            check=ContractCheck.RATIONALE_STATES_A_THESIS,
            detail=(
                f"the rationale is {len(rationale)} characters against a floor of "
                f"{thresholds.min_rationale_chars}"
            ),
        )
    if rationale == proposal.invalidation_claim.strip():
        return ContractViolation(
            check=ContractCheck.RATIONALE_STATES_A_THESIS,
            detail=(
                "the rationale and the invalidation claim are the same text, so one of "
                "the two questions was not answered"
            ),
        )
    return None


def _check_rationale_tracks_the_logic(proposal: GenomeProposal) -> ContractViolation | None:
    parent = proposal.parent
    if parent is None or parent.structure_hash == proposal.structure_hash:
        return None
    if proposal.rationale.strip() != parent.rationale.strip():
        return None
    return ContractViolation(
        check=ContractCheck.RATIONALE_TRACKS_THE_LOGIC,
        detail=(
            f"the expression tree changed -- structure {parent.structure_hash} became "
            f"{proposal.structure_hash} -- while the rationale is byte-identical to the "
            f"parent's, so the recorded justification describes a hypothesis that is no "
            f"longer the one being tested"
        ),
    )


def _check_genome_not_tombstoned(
    proposal: GenomeProposal, tombstoned_genome_hashes: Collection[str]
) -> ContractViolation | None:
    if proposal.genome_hash not in frozenset(tombstoned_genome_hashes):
        return None
    return ContractViolation(
        check=ContractCheck.GENOME_NOT_TOMBSTONED,
        detail=(
            f"{proposal.genome_hash} is tombstoned: this hypothesis has already been "
            f"tested and retired, and re-testing it buys a second correlated look at the "
            f"full statistical cost"
        ),
    )


def evaluate_contract_gate(
    proposal: GenomeProposal,
    *,
    available_feature_ids: Collection[str],
    tombstoned_genome_hashes: Collection[str] = (),
    thresholds: ContractGateThresholds | None = None,
) -> ContractGateVerdict:
    """Run every check and return the verdict. Pure: no clock, no I/O, no randomness.

    `available_feature_ids` is required and may not be empty. An empty catalogue would
    fail every proposal for the same reason, which reads in the record as a population
    of defective genomes rather than as a feature store that was not loaded -- and the
    two call for opposite responses.
    """
    if not available_feature_ids:
        raise ContractGateError(
            "the feature catalogue is empty; the gate refuses to run rather than refuse "
            "every genome for a reason that is not about the genomes"
        )
    applied = ContractGateThresholds() if thresholds is None else thresholds

    candidates: Sequence[ContractViolation | None] = (
        _check_features_available(proposal, available_feature_ids),
        _check_invalidation_declared(proposal, applied),
        _check_rationale_states_a_thesis(proposal, applied),
        _check_rationale_tracks_the_logic(proposal),
        _check_genome_not_tombstoned(proposal, tombstoned_genome_hashes),
    )
    return ContractGateVerdict(
        genome_hash=proposal.genome_hash,
        checks_run=tuple(ContractCheck),
        violations=tuple(violation for violation in candidates if violation is not None),
    )
