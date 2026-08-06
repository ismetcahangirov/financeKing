"""Genome identity is content, and the content is canonical.

The two properties in this file are the acceptance criteria for #83 stated as tests: a
genome serialised with different field ordering must digest identically, and a
single-parameter change must not. Both run at 1000 Hypothesis examples explicitly rather
than inheriting the profile, because the `dev` profile is 100 and this is the check that
a whole lineage's trial accounting rests on.
"""

from __future__ import annotations

from datetime import timedelta
from decimal import Decimal

import pytest
from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st

from fking.evolution import (
    ExpressionNode,
    Genome,
    GenomeError,
    NodeKind,
    genome_hash,
    lineage_id_for,
    structure_hash,
)
from tests.evolution.conftest import build_genome, comparison_rule

pytestmark = pytest.mark.unit

# Explicit rather than profile-inherited: `dev` is 100 examples, and the acceptance
# criterion names 1000.
IDENTITY_SETTINGS = settings(
    max_examples=1000,
    deadline=None,
    suppress_health_check=[HealthCheck.too_slow],
)

parameter_names = st.text(
    alphabet=st.characters(min_codepoint=97, max_codepoint=122), min_size=1, max_size=12
)
parameter_decimals = st.decimals(
    min_value=Decimal("-100000"),
    max_value=Decimal("100000"),
    places=8,
    allow_nan=False,
    allow_infinity=False,
)
feature_ids = st.text(
    alphabet=st.characters(min_codepoint=97, max_codepoint=122), min_size=3, max_size=16
)
horizons = st.integers(min_value=1, max_value=7 * 24 * 60).map(
    lambda minutes: timedelta(minutes=minutes)
)


@st.composite
def genomes(draw: st.DrawFn) -> Genome:
    """A genome whose entry rule reads exactly the features and parameters it declares."""
    feature_id = draw(feature_ids)
    parameters = draw(st.dictionaries(parameter_names, parameter_decimals, min_size=1, max_size=6))
    chosen = min(parameters)
    return Genome(
        entry_rule=comparison_rule(feature_id=feature_id, parameter_name=chosen),
        parameters=parameters,
        feature_ids=frozenset({feature_id}),
        holding_horizon=draw(horizons),
    )


def _respelled(parameter: Decimal) -> Decimal:
    """The same number written with three extra trailing zeros.

    `Decimal('1.50')` and `Decimal('1.5')` are one threshold. A mutation operator that
    happens to produce one spelling rather than the other has changed nothing, and two
    identities for one hypothesis would charge the trial ledger twice.
    """
    return parameter.quantize(Decimal("1E-11"))


@given(genome=genomes())
@IDENTITY_SETTINGS
def test_field_order_and_decimal_spelling_do_not_change_the_genome_hash(
    genome: Genome,
) -> None:
    reordered = {
        name: _respelled(genome.parameters[name]) for name in reversed(list(genome.parameters))
    }
    twin = Genome(
        entry_rule=genome.entry_rule,
        parameters=reordered,
        feature_ids=genome.feature_ids,
        holding_horizon=genome.holding_horizon,
    )

    # The setup did what it claims: a different insertion order, and at least one
    # parameter written with a different number of trailing zeros.
    assert list(twin.parameters) == list(reversed(list(genome.parameters)))
    assert any(
        str(twin.parameters[name]) != str(genome.parameters[name]) for name in genome.parameters
    )

    assert genome_hash(twin) == genome_hash(genome)
    assert structure_hash(twin) == structure_hash(genome)


@given(genome=genomes(), shift=st.decimals(min_value="0.001", max_value="1000", places=3))
@IDENTITY_SETTINGS
def test_a_single_parameter_change_changes_the_genome_hash(genome: Genome, shift: Decimal) -> None:
    target = min(genome.parameters)
    moved = dict(genome.parameters)
    moved[target] = moved[target] + shift
    mutant = Genome(
        entry_rule=genome.entry_rule,
        parameters=moved,
        feature_ids=genome.feature_ids,
        holding_horizon=genome.holding_horizon,
    )

    assert mutant.parameters[target] != genome.parameters[target]
    assert genome_hash(mutant) != genome_hash(genome)
    # ... and the family is unchanged, which is the whole point of the second digest.
    assert structure_hash(mutant) == structure_hash(genome)
    assert lineage_id_for(mutant) == lineage_id_for(genome)


@given(genome=genomes())
@IDENTITY_SETTINGS
def test_a_new_feature_set_starts_a_new_lineage(genome: Genome) -> None:
    """Only a structurally new hypothesis starts a new family."""
    replacement = f"{min(genome.feature_ids)}.variant"
    restructured = Genome(
        entry_rule=comparison_rule(feature_id=replacement, parameter_name=min(genome.parameters)),
        parameters=genome.parameters,
        feature_ids=frozenset({replacement}),
        holding_horizon=genome.holding_horizon,
    )

    assert genome_hash(restructured) != genome_hash(genome)
    assert lineage_id_for(restructured) != lineage_id_for(genome)


def test_a_changed_horizon_starts_a_new_lineage() -> None:
    """The horizon sizes the CPCV embargo, so it is structure, not tuning."""
    original = build_genome(holding_horizon=timedelta(hours=8))
    longer = build_genome(holding_horizon=timedelta(hours=48))

    assert lineage_id_for(longer) != lineage_id_for(original)


def test_negative_zero_and_zero_are_one_threshold() -> None:
    """`Decimal('-0.0') == Decimal('0')`, so they must digest equal too."""
    positive = build_genome(parameters={"entry_threshold": Decimal("0")})
    negative = build_genome(parameters={"entry_threshold": Decimal("-0.0")})

    assert genome_hash(negative) == genome_hash(positive)


def test_an_exponent_spelling_is_not_a_second_identity() -> None:
    hundred = build_genome(parameters={"entry_threshold": Decimal("100")})
    exponent = build_genome(parameters={"entry_threshold": Decimal("1E+2")})

    assert genome_hash(exponent) == genome_hash(hundred)


def test_a_numeric_root_is_not_an_entry_rule() -> None:
    with pytest.raises(GenomeError, match="must evaluate to a boolean"):
        build_genome(entry_rule=ExpressionNode(kind=NodeKind.FEATURE, feature_id="momentum.4h"))


def test_a_comparison_between_booleans_is_refused() -> None:
    boolean_child = comparison_rule()
    with pytest.raises(GenomeError, match="numeric children"):
        ExpressionNode(
            kind=NodeKind.COMPARISON,
            operator="gt",
            children=(boolean_child, boolean_child),
        )


def test_a_leaf_carrying_a_field_it_has_no_use_for_is_refused() -> None:
    """A stray field would enter the digest and split one hypothesis into two."""
    with pytest.raises(GenomeError, match="must not set"):
        ExpressionNode(kind=NodeKind.FEATURE, feature_id="momentum.4h", operator="gt")


def test_an_undeclared_feature_read_is_refused() -> None:
    with pytest.raises(GenomeError, match="does not declare"):
        build_genome(
            entry_rule=comparison_rule(feature_id="funding.extremity"),
            feature_ids=frozenset({"momentum.4h"}),
        )


def test_an_undeclared_parameter_read_is_refused() -> None:
    with pytest.raises(GenomeError, match="parameters"):
        build_genome(
            entry_rule=comparison_rule(parameter_name="unknown_knob"),
            parameters={"entry_threshold": Decimal("1.5")},
        )


def test_a_non_positive_horizon_is_refused() -> None:
    with pytest.raises(GenomeError, match="holding_horizon must be positive"):
        build_genome(holding_horizon=timedelta(0))


def test_a_non_finite_parameter_is_refused_before_it_reaches_the_digest() -> None:
    with pytest.raises(GenomeError, match="finite"):
        build_genome(parameters={"entry_threshold": Decimal("Infinity")})


def test_parameters_are_copied_so_a_caller_cannot_mutate_a_recorded_genome() -> None:
    mutable = {"entry_threshold": Decimal("1.5")}
    genome = build_genome(parameters=mutable)
    before = genome_hash(genome)

    mutable["entry_threshold"] = Decimal("99")

    assert genome_hash(genome) == before


def test_a_logical_node_needs_two_boolean_children() -> None:
    with pytest.raises(GenomeError, match="at least 2 children"):
        ExpressionNode(kind=NodeKind.LOGICAL, operator="and", children=(comparison_rule(),))


def test_an_unknown_operator_is_refused() -> None:
    with pytest.raises(GenomeError, match="operator must be one of"):
        ExpressionNode(
            kind=NodeKind.COMPARISON,
            operator="approximately",
            children=(
                ExpressionNode(kind=NodeKind.FEATURE, feature_id="momentum.4h"),
                ExpressionNode(kind=NodeKind.CONSTANT, constant=Decimal("1")),
            ),
        )


def test_a_negation_wraps_exactly_one_boolean() -> None:
    negated = ExpressionNode(kind=NodeKind.NEGATION, children=(comparison_rule(),))
    genome = build_genome(entry_rule=negated)

    assert genome_hash(genome) != genome_hash(build_genome())


def test_lineage_id_is_derived_from_the_structure_digest() -> None:
    genome = build_genome()

    assert lineage_id_for(genome) == f"lin-{structure_hash(genome)[:16]}"


def test_an_arithmetic_node_composes_numerics_into_a_numeric() -> None:
    """The only inner node that stays numeric, so it can sit under a comparison."""
    scaled = ExpressionNode(
        kind=NodeKind.ARITHMETIC,
        operator="multiply",
        children=(
            ExpressionNode(kind=NodeKind.FEATURE, feature_id="momentum.4h"),
            ExpressionNode(kind=NodeKind.CONSTANT, constant=Decimal("2")),
        ),
    )
    rule = ExpressionNode(
        kind=NodeKind.COMPARISON,
        operator="gt",
        children=(
            scaled,
            ExpressionNode(kind=NodeKind.PARAMETER, parameter_name="entry_threshold"),
        ),
    )

    assert genome_hash(build_genome(entry_rule=rule)) != genome_hash(build_genome())


def test_a_constant_node_without_a_constant_is_refused() -> None:
    with pytest.raises(GenomeError, match="needs a constant"):
        ExpressionNode(kind=NodeKind.CONSTANT)


def test_a_non_finite_constant_is_refused() -> None:
    with pytest.raises(GenomeError, match="finite"):
        ExpressionNode(kind=NodeKind.CONSTANT, constant=Decimal("NaN"))


def test_a_feature_node_with_a_blank_id_is_refused() -> None:
    with pytest.raises(GenomeError, match="non-empty feature_id"):
        ExpressionNode(kind=NodeKind.FEATURE, feature_id="  ")


def test_a_parameter_node_with_a_blank_name_is_refused() -> None:
    with pytest.raises(GenomeError, match="non-empty parameter_name"):
        ExpressionNode(kind=NodeKind.PARAMETER, parameter_name="")


def test_a_leaf_with_children_is_refused() -> None:
    with pytest.raises(GenomeError, match="is a leaf and takes no children"):
        ExpressionNode(
            kind=NodeKind.CONSTANT,
            constant=Decimal("1"),
            children=(ExpressionNode(kind=NodeKind.FEATURE, feature_id="momentum.4h"),),
        )


def test_a_comparison_with_the_wrong_arity_is_refused() -> None:
    with pytest.raises(GenomeError, match="exactly 2 children"):
        ExpressionNode(
            kind=NodeKind.COMPARISON,
            operator="gt",
            children=(ExpressionNode(kind=NodeKind.FEATURE, feature_id="momentum.4h"),),
        )


def test_a_logical_node_joins_two_booleans() -> None:
    conjunction = ExpressionNode(
        kind=NodeKind.LOGICAL,
        operator="and",
        children=(comparison_rule(), comparison_rule(operator="lt")),
    )

    assert genome_hash(build_genome(entry_rule=conjunction)) != genome_hash(build_genome())


def test_a_logical_node_over_numerics_is_refused() -> None:
    with pytest.raises(GenomeError, match="boolean children"):
        ExpressionNode(
            kind=NodeKind.LOGICAL,
            operator="or",
            children=(
                ExpressionNode(kind=NodeKind.FEATURE, feature_id="momentum.4h"),
                ExpressionNode(kind=NodeKind.CONSTANT, constant=Decimal("1")),
            ),
        )


def test_a_negation_with_a_numeric_child_is_refused() -> None:
    with pytest.raises(GenomeError, match="boolean children"):
        ExpressionNode(
            kind=NodeKind.NEGATION,
            children=(ExpressionNode(kind=NodeKind.FEATURE, feature_id="momentum.4h"),),
        )


def test_a_genome_with_no_declared_features_is_refused() -> None:
    with pytest.raises(GenomeError, match="declares at least one feature"):
        Genome(
            entry_rule=ExpressionNode(
                kind=NodeKind.COMPARISON,
                operator="gt",
                children=(
                    ExpressionNode(kind=NodeKind.CONSTANT, constant=Decimal("1")),
                    ExpressionNode(kind=NodeKind.CONSTANT, constant=Decimal("0")),
                ),
            ),
            parameters={},
            feature_ids=frozenset(),
            holding_horizon=timedelta(hours=8),
        )


def test_the_horizon_round_trips_through_its_microsecond_form() -> None:
    """The digest carries whole microseconds, and `timedelta` has exactly that resolution."""
    genome = build_genome(holding_horizon=timedelta(hours=8, microseconds=7))

    assert genome.holding_horizon_microseconds == 8 * 60 * 60 * 1_000_000 + 7
    assert genome_hash(genome) != genome_hash(build_genome(holding_horizon=timedelta(hours=8)))


def test_a_blank_parameter_name_is_refused() -> None:
    with pytest.raises(GenomeError, match="parameter name must be non-empty"):
        build_genome(parameters={"entry_threshold": Decimal("1.5"), " ": Decimal("2")})
