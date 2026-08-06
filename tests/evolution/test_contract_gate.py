"""The contract gate refuses; it never warns.

Every assertion here is about a refusal that has to be *structural*. The gate is the only
thing standing between a machine-authored genome and a trial charge, and the trial charge
is permanent -- so the interesting tests are the ones where the proposal looks entirely
reasonable and is still turned away.
"""

from __future__ import annotations

from collections.abc import Collection
from dataclasses import replace
from datetime import timedelta
from decimal import Decimal

import pytest

from fking.evolution import (
    COMPILED_THRESHOLD_FLOORS,
    ContractCheck,
    ContractGateError,
    ContractGateThresholds,
    GenomeProposal,
    ParentThesis,
    evaluate_contract_gate,
    structure_hash,
)
from tests.evolution.conftest import build_genome, comparison_rule

pytestmark = pytest.mark.unit

CATALOGUE = frozenset({"momentum.4h", "funding.8h", "basis.1d"})

THESIS = (
    "Perpetual funding paid by longs at an extreme mean-reverts within one funding "
    "interval because the carry cost forces position closure."
)
INVALIDATION = "Two consecutive quarters in which extreme funding is followed by continuation."


def build_proposal(
    *,
    rationale: str = THESIS,
    invalidation_claim: str = INVALIDATION,
    feature_ids: frozenset[str] = frozenset({"momentum.4h"}),
    parent: ParentThesis | None = None,
) -> GenomeProposal:
    entry_rule = comparison_rule(feature_id=min(feature_ids))
    return GenomeProposal(
        genome=build_genome(entry_rule=entry_rule, feature_ids=feature_ids),
        rationale=rationale,
        invalidation_claim=invalidation_claim,
        parent=parent,
    )


def failed_checks(
    proposal: GenomeProposal, *, tombstoned_genome_hashes: Collection[str] = ()
) -> set[ContractCheck]:
    verdict = evaluate_contract_gate(
        proposal,
        available_feature_ids=CATALOGUE,
        tombstoned_genome_hashes=tombstoned_genome_hashes,
    )
    return {violation.check for violation in verdict.violations}


def test_a_well_formed_proposal_is_admitted() -> None:
    verdict = evaluate_contract_gate(build_proposal(), available_feature_ids=CATALOGUE)

    assert verdict.is_admitted
    assert verdict.violations == ()
    assert set(verdict.checks_run) == set(ContractCheck)


def test_a_feature_the_store_does_not_offer_is_refused_before_any_trial_is_charged() -> None:
    proposal = build_proposal(feature_ids=frozenset({"orderflow.imbalance.1m"}))

    assert ContractCheck.FEATURES_AVAILABLE in failed_checks(proposal)


def test_a_thesis_with_no_stated_invalidation_is_refused() -> None:
    assert ContractCheck.INVALIDATION_DECLARED in failed_checks(
        build_proposal(invalidation_claim="  ")
    )


def test_a_rationale_that_merely_repeats_the_invalidation_answers_one_question() -> None:
    long_enough = INVALIDATION + " " + INVALIDATION
    proposal = build_proposal(rationale=long_enough, invalidation_claim=long_enough)

    assert ContractCheck.RATIONALE_STATES_A_THESIS in failed_checks(proposal)


def test_a_mutant_that_changed_its_logic_and_copied_its_rationale_is_rejected() -> None:
    """The single most common machine-authored failure: mutate the tree, keep the words."""
    parent_genome = build_genome(entry_rule=comparison_rule(feature_id="momentum.4h"))
    mutant = build_proposal(
        feature_ids=frozenset({"funding.8h"}),
        parent=ParentThesis(structure_hash=structure_hash(parent_genome), rationale=THESIS),
    )

    assert mutant.structure_hash != structure_hash(parent_genome)
    assert ContractCheck.RATIONALE_TRACKS_THE_LOGIC in failed_checks(mutant)


def test_the_same_mutant_passes_once_its_rationale_describes_what_changed() -> None:
    parent_genome = build_genome(entry_rule=comparison_rule(feature_id="momentum.4h"))
    mutant = build_proposal(
        rationale="Funding, not momentum, carries the signal: the carry cost is the mechanism.",
        feature_ids=frozenset({"funding.8h"}),
        parent=ParentThesis(structure_hash=structure_hash(parent_genome), rationale=THESIS),
    )

    assert evaluate_contract_gate(mutant, available_feature_ids=CATALOGUE).is_admitted


def test_a_parameter_only_jitter_may_keep_its_parents_rationale() -> None:
    """Same claim about the world at a different setting. Demanding a fresh rationale
    here would train proposers to paraphrase, which is what the check exists to detect."""
    parent_genome = build_genome()
    jittered = replace(
        build_proposal(
            parent=ParentThesis(structure_hash=structure_hash(parent_genome), rationale=THESIS)
        ),
        genome=build_genome(parameters={"entry_threshold": Decimal("2.5")}),
    )

    assert jittered.structure_hash == structure_hash(parent_genome)
    assert evaluate_contract_gate(jittered, available_feature_ids=CATALOGUE).is_admitted


def test_a_tombstoned_hypothesis_cannot_be_resubmitted() -> None:
    proposal = build_proposal()

    failures = failed_checks(proposal, tombstoned_genome_hashes=(proposal.genome_hash,))

    assert ContractCheck.GENOME_NOT_TOMBSTONED in failures


def test_every_failing_check_is_reported_rather_than_the_first() -> None:
    """One round trip per defect would charge the trial ledger once per defect."""
    proposal = build_proposal(
        rationale="too short",
        invalidation_claim="",
        feature_ids=frozenset({"unknown.feature"}),
    )

    assert failed_checks(proposal) == {
        ContractCheck.FEATURES_AVAILABLE,
        ContractCheck.INVALIDATION_DECLARED,
        ContractCheck.RATIONALE_STATES_A_THESIS,
    }


def test_the_defect_reason_names_every_refused_check() -> None:
    verdict = evaluate_contract_gate(
        build_proposal(feature_ids=frozenset({"unknown.feature"})),
        available_feature_ids=CATALOGUE,
    )
    reason = verdict.defect_reason()

    assert verdict.genome_hash in reason
    assert ContractCheck.FEATURES_AVAILABLE.value in reason


def test_an_admitted_proposal_has_no_defect_to_record() -> None:
    verdict = evaluate_contract_gate(build_proposal(), available_feature_ids=CATALOGUE)

    with pytest.raises(ContractGateError, match="no defect to record"):
        verdict.defect_reason()


def test_an_empty_feature_catalogue_stops_the_gate_rather_than_failing_every_genome() -> None:
    with pytest.raises(ContractGateError, match="not about the genomes"):
        evaluate_contract_gate(build_proposal(), available_feature_ids=())


@pytest.mark.parametrize("threshold_name", sorted(COMPILED_THRESHOLD_FLOORS))
def test_configuration_below_a_compiled_floor_fails_at_construction(
    threshold_name: str,
) -> None:
    floor = COMPILED_THRESHOLD_FLOORS[threshold_name]

    with pytest.raises(ContractGateError, match="only make a contract gate stricter"):
        ContractGateThresholds(**{threshold_name: floor - 1})


@pytest.mark.parametrize("threshold_name", sorted(COMPILED_THRESHOLD_FLOORS))
def test_configuration_above_a_compiled_floor_is_accepted(threshold_name: str) -> None:
    floor = COMPILED_THRESHOLD_FLOORS[threshold_name]
    stricter = ContractGateThresholds(**{threshold_name: floor * 3})

    assert getattr(stricter, threshold_name) > floor


def test_a_stricter_threshold_refuses_a_proposal_the_default_admits() -> None:
    proposal = build_proposal()
    demanding = ContractGateThresholds(min_rationale_chars=len(THESIS) + 1)

    assert evaluate_contract_gate(proposal, available_feature_ids=CATALOGUE).is_admitted
    assert not evaluate_contract_gate(
        proposal, available_feature_ids=CATALOGUE, thresholds=demanding
    ).is_admitted


def test_the_gate_reads_no_clock_and_gives_the_same_verdict_twice() -> None:
    proposal = replace(build_proposal(), genome=build_genome(holding_horizon=timedelta(hours=12)))

    first = evaluate_contract_gate(proposal, available_feature_ids=CATALOGUE)
    second = evaluate_contract_gate(proposal, available_feature_ids=CATALOGUE)

    assert first == second
