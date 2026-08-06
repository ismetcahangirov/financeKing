"""Genome identity: a typed expression tree, a parameter vector, and the digests over them.

**Identity is content, never a row id.** `genome_hash` is a SHA-256 over the canonical
serialisation of the entry rule, the parameter vector, the declared feature set and the
declared holding horizon. Nothing else is in it -- not the creation instant, not the
generation, not the operators that produced it, not who proposed it. Two runs that
construct the same hypothesis get the same identity, on different machines, months apart.

Two consequences carry the whole design, and both are the point rather than side effects:

**A "fixed" strategy is a new genome.** Editing a threshold, swapping a feature or
changing the horizon produces a different digest, so the repaired strategy cannot inherit
its predecessor's held-out vault access and re-enters at `proposed`. The vault's value is
entirely in its untouched-ness (`EVOLUTION_ENGINE.md` section 5.3) and a hash that
survived a fix would be the mechanism by which it gets spent on iteration.

**A parameter-only mutation cannot escape its parent's accumulated trial count.**
`structure_hash` is the same digest with the parameter *values* removed -- parameter
*names* stay, because a genome that gained a parameter is a different hypothesis -- and
`lineage_id_for` is derived from it. So the 40th jitter of one parent lands in that
parent's lineage and inherits its family trial count. Without this, a lineage that has
consumed 612 trials across nine generations acquires a fresh id and reports itself as a
two-trial newcomer, and the family deflation term in `SCORING_ENGINE.md` believes it.

**Why normalise `Decimal` before hashing, when the audit codec deliberately does not.**
`fking.domain.encode` renders a `Decimal` as its exact string so that an audit row
records the digits the venue actually sent. Here the opposite is required:
`Decimal("1.50")` and `Decimal("1.5")` are the same threshold, a mutation operator that
happens to produce one spelling rather than the other has changed nothing, and two
identities for one hypothesis would charge the trial ledger twice and split its lineage.
Trailing zeros are therefore removed, and the zero sign with them -- `Decimal("-0.0")`
and `Decimal("0")` compare equal, so they must digest equal.

The tree is *typed*, and the type check runs at construction. An expression whose root is
numeric is not an entry rule, and a comparison between two booleans is not a comparison.
Both would still produce a perfectly stable digest, which is precisely why they must be
refused here: a stable identity for something that cannot run is the worst outcome, since
it would then be recorded, mutated and inherited.

`EVOLUTION_ENGINE.md` sections 5.6 and 6.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from datetime import timedelta
from decimal import Decimal
from enum import StrEnum
from types import MappingProxyType
from typing import Final

from fking.domain import JsonValue
from fking.evolution._errors import GenomeError

__all__ = [
    "ExpressionNode",
    "ExpressionType",
    "Genome",
    "MutationOperator",
    "NodeKind",
    "canonical_payload",
    "genome_hash",
    "lineage_id_for",
    "structure_hash",
]

_MICROSECOND: Final = timedelta(microseconds=1)

# Sixteen hex characters of the structure digest. Long enough that a collision across
# every genome this project will ever evaluate is not worth reasoning about, short enough
# that a lineage id appears whole in a log line and in a Grafana legend -- and a lineage
# id nobody can read back is a lineage id nobody traces a collapse with.
_LINEAGE_ID_HEX_CHARS: Final = 16

_LINEAGE_ID_PREFIX: Final = "lin-"


class ExpressionType(StrEnum):
    """What a node evaluates to. There are two, and mixing them is the bug being caught."""

    NUMERIC = "numeric"
    BOOLEAN = "boolean"


class NodeKind(StrEnum):
    """The node kinds the mutation operators in `EVOLUTION_ENGINE.md` section 6 produce."""

    FEATURE = "feature"
    """A point-in-time feature read. Leaf, numeric."""

    PARAMETER = "parameter"
    """A reference into the parameter vector. Leaf, numeric, and the only leaf whose
    contribution to `genome_hash` is not also in `structure_hash`."""

    CONSTANT = "constant"
    """A literal baked into the tree. Leaf, numeric. Structural, unlike a parameter: a
    constant is a claim the hypothesis makes, and changing it changes the hypothesis."""

    ARITHMETIC = "arithmetic"
    """Two numeric children, one numeric result."""

    COMPARISON = "comparison"
    """Two numeric children, one boolean result."""

    LOGICAL = "logical"
    """Two or more boolean children, one boolean result."""

    NEGATION = "negation"
    """One boolean child, one boolean result."""


class MutationOperator(StrEnum):
    """The operators of `EVOLUTION_ENGINE.md` section 6, recorded per genome.

    A genome records the operators that produced it as an ordered sequence rather than
    one name, because they compose: a mutant may be a parameter jitter *and* a horizon
    change, and recording only the last would make the population's operator mix
    unmeasurable at exactly the moment somebody asks which operator is producing the
    survivors.
    """

    PARAMETER_JITTER = "parameter_jitter"
    PARAMETER_RESET = "parameter_reset"
    FEATURE_SWAP = "feature_swap"
    OPERATOR_SWAP = "operator_swap"
    RULE_DELETION = "rule_deletion"
    RULE_ADDITION = "rule_addition"
    HORIZON_CHANGE = "horizon_change"
    INVALIDATION_CHANGE = "invalidation_change"
    SUBTREE_CROSSOVER = "subtree_crossover"
    BLEND_CROSSOVER = "blend_crossover"


_ARITHMETIC_OPERATORS: Final[frozenset[str]] = frozenset({"add", "subtract", "multiply", "divide"})
_COMPARISON_OPERATORS: Final[frozenset[str]] = frozenset({"lt", "le", "gt", "ge"})
_LOGICAL_OPERATORS: Final[frozenset[str]] = frozenset({"and", "or"})

_LEAF_KINDS: Final[frozenset[NodeKind]] = frozenset(
    {NodeKind.FEATURE, NodeKind.PARAMETER, NodeKind.CONSTANT}
)
_NUMERIC_KINDS: Final[frozenset[NodeKind]] = frozenset({*_LEAF_KINDS, NodeKind.ARITHMETIC})

_BINARY_CHILDREN: Final = 2
_MIN_LOGICAL_CHILDREN: Final = 2


def _require_finite(candidate: Decimal, field_name: str) -> Decimal:
    """Reject NaN and the infinities before anything tries to normalise them.

    `Decimal("NaN").normalize()` raises `InvalidOperation` under this project's trapping
    decimal context, and it would raise from inside the digest -- which is the one place
    where an exception is indistinguishable from a hashing bug.
    """
    if not candidate.is_finite():
        raise GenomeError(f"{field_name} must be a finite Decimal, got {candidate!r}")
    return candidate


def _canonical_decimal(candidate: Decimal) -> str:
    """The one spelling of a numeric quantity that both a mutant and its twin produce.

    `normalize()` strips trailing zeros so that 1.50 and 1.5 are one threshold; `"f"`
    formatting keeps 1E+2 from being a second spelling of 100; and the zero branch exists
    because `Decimal("-0.0").normalize()` is `Decimal("-0")`, which compares equal to
    `Decimal("0")` and must therefore digest equal.
    """
    normalised = _require_finite(candidate, "a decimal").normalize()
    if normalised.is_zero():
        return "0"
    return format(normalised, "f")


@dataclass(frozen=True, slots=True)
class ExpressionNode:
    """One node of the typed expression tree.

    One dataclass rather than a union of seven, because the tree is serialised,
    deserialised, hashed, generated by mutation operators and generated again by
    Hypothesis -- and a union would need a discriminator in every one of those paths,
    which is what `kind` already is. The cost is that three of the six fields are `None`
    for any given node; `__post_init__` refuses a node where the wrong ones are set, so
    the looseness is not observable.
    """

    kind: NodeKind
    operator: str | None = None
    feature_id: str | None = None
    parameter_name: str | None = None
    constant: Decimal | None = None
    children: tuple[ExpressionNode, ...] = ()

    def __post_init__(self) -> None:
        _validate_node(self)

    @property
    def expression_type(self) -> ExpressionType:
        """What this node evaluates to."""
        if self.kind in _NUMERIC_KINDS:
            return ExpressionType.NUMERIC
        return ExpressionType.BOOLEAN

    def walk(self) -> tuple[ExpressionNode, ...]:
        """This node and every node beneath it, parents before children."""
        found: list[ExpressionNode] = [self]
        for child in self.children:
            found.extend(child.walk())
        return tuple(found)

    def referenced_feature_ids(self) -> frozenset[str]:
        return frozenset(
            node.feature_id
            for node in self.walk()
            if node.kind is NodeKind.FEATURE and node.feature_id is not None
        )

    def referenced_parameter_names(self) -> frozenset[str]:
        return frozenset(
            node.parameter_name
            for node in self.walk()
            if node.kind is NodeKind.PARAMETER and node.parameter_name is not None
        )


def _require_text(candidate: str | None, field_name: str, kind: NodeKind) -> str:
    if candidate is None or not candidate.strip():
        raise GenomeError(f"a {kind.value} node needs a non-empty {field_name}")
    return candidate


def _require_children(node: ExpressionNode, *, expected: int) -> None:
    if len(node.children) != expected:
        raise GenomeError(
            f"a {node.kind.value} node takes exactly {expected} children, got {len(node.children)}"
        )


def _require_child_types(node: ExpressionNode, *, expected: ExpressionType) -> None:
    offenders = [
        child.kind.value for child in node.children if child.expression_type is not expected
    ]
    if offenders:
        raise GenomeError(
            f"a {node.kind.value} node takes {expected.value} children; "
            f"got {offenders}, which are not"
        )


def _validate_node(node: ExpressionNode) -> None:
    """Refuse a node whose shape does not match its kind, at construction.

    Deliberately a chain of branches, one per kind. A dispatch table would hide the two
    asymmetries that matter -- that `logical` is variadic while every other inner node is
    fixed-arity, and that `constant` is the only leaf carrying a value -- behind
    indirection a reader has to trace.
    """
    if node.kind in _LEAF_KINDS and node.children:
        raise GenomeError(f"a {node.kind.value} node is a leaf and takes no children")

    if node.kind is NodeKind.FEATURE:
        _require_text(node.feature_id, "feature_id", node.kind)
    elif node.kind is NodeKind.PARAMETER:
        _require_text(node.parameter_name, "parameter_name", node.kind)
    elif node.kind is NodeKind.CONSTANT:
        if node.constant is None:
            raise GenomeError("a constant node needs a constant")
        _require_finite(node.constant, "a constant node's constant")
    elif node.kind is NodeKind.ARITHMETIC:
        _require_operator(node, _ARITHMETIC_OPERATORS)
        _require_children(node, expected=_BINARY_CHILDREN)
        _require_child_types(node, expected=ExpressionType.NUMERIC)
    elif node.kind is NodeKind.COMPARISON:
        _require_operator(node, _COMPARISON_OPERATORS)
        _require_children(node, expected=_BINARY_CHILDREN)
        _require_child_types(node, expected=ExpressionType.NUMERIC)
    elif node.kind is NodeKind.LOGICAL:
        _require_operator(node, _LOGICAL_OPERATORS)
        if len(node.children) < _MIN_LOGICAL_CHILDREN:
            raise GenomeError(
                f"a logical node takes at least {_MIN_LOGICAL_CHILDREN} children, "
                f"got {len(node.children)}"
            )
        _require_child_types(node, expected=ExpressionType.BOOLEAN)
    else:
        _require_children(node, expected=1)
        _require_child_types(node, expected=ExpressionType.BOOLEAN)

    _require_no_stray_fields(node)


def _require_operator(node: ExpressionNode, permitted: frozenset[str]) -> None:
    if node.operator not in permitted:
        raise GenomeError(
            f"a {node.kind.value} node's operator must be one of {sorted(permitted)}, "
            f"got {node.operator!r}"
        )


def _require_no_stray_fields(node: ExpressionNode) -> None:
    """A field set on a kind that has no use for it would silently enter the digest.

    Two genomes that behave identically would then hash differently, which splits one
    lineage in two and charges the trial ledger twice for one hypothesis.
    """
    stray = [
        name
        for name, is_set, permitted_kinds in (
            (
                "operator",
                node.operator is not None,
                (NodeKind.ARITHMETIC, NodeKind.COMPARISON, NodeKind.LOGICAL),
            ),
            ("feature_id", node.feature_id is not None, (NodeKind.FEATURE,)),
            ("parameter_name", node.parameter_name is not None, (NodeKind.PARAMETER,)),
            ("constant", node.constant is not None, (NodeKind.CONSTANT,)),
        )
        if is_set and node.kind not in permitted_kinds
    ]
    if stray:
        raise GenomeError(f"a {node.kind.value} node must not set {sorted(stray)}")


@dataclass(frozen=True, slots=True)
class Genome:
    """A complete, runnable hypothesis, and the thing `genome_hash` is an identity for.

    `parameters` is copied into a `MappingProxyType` at construction: `frozen=True`
    protects the binding, not the object bound, and a caller holding the original dict
    could otherwise mutate a genome's parameter vector after its hash had been recorded.
    """

    entry_rule: ExpressionNode
    parameters: Mapping[str, Decimal]
    feature_ids: frozenset[str]
    holding_horizon: timedelta

    def __post_init__(self) -> None:
        if self.entry_rule.expression_type is not ExpressionType.BOOLEAN:
            raise GenomeError(
                f"an entry rule must evaluate to a boolean, got "
                f"{self.entry_rule.expression_type.value}"
            )
        if not self.feature_ids:
            raise GenomeError("a genome declares at least one feature; it computes nothing without")
        if self.holding_horizon <= timedelta(0):
            raise GenomeError(
                f"holding_horizon must be positive, got {self.holding_horizon!r}; a "
                f"non-positive horizon leaves the CPCV embargo length undefined"
            )
        # No sub-microsecond check: `timedelta` cannot represent one. It rounds at
        # construction, so `holding_horizon_microseconds` is exact by the type's own
        # resolution and a guard here would be a branch nothing can reach.
        undeclared_features = self.entry_rule.referenced_feature_ids() - self.feature_ids
        if undeclared_features:
            raise GenomeError(
                f"the entry rule reads {sorted(undeclared_features)}, which the genome "
                f"does not declare; the feature store refuses an undeclared read and a "
                f"silent substitution is worse"
            )
        undeclared_parameters = self.entry_rule.referenced_parameter_names() - set(self.parameters)
        if undeclared_parameters:
            raise GenomeError(
                f"the entry rule reads parameters {sorted(undeclared_parameters)}, which "
                f"the genome does not declare"
            )
        for name, parameter in self.parameters.items():
            if not name.strip():
                raise GenomeError("a parameter name must be non-empty")
            _require_finite(parameter, f"parameter {name!r}")

        object.__setattr__(self, "parameters", MappingProxyType(dict(self.parameters)))

    @property
    def holding_horizon_microseconds(self) -> int:
        return self.holding_horizon // _MICROSECOND


def _encode_node(node: ExpressionNode) -> JsonValue:
    """The tree as canonical JSON data. Every key always present, so absence is explicit."""
    return {
        "kind": node.kind.value,
        "operator": node.operator,
        "feature_id": node.feature_id,
        "parameter_name": node.parameter_name,
        "constant": None if node.constant is None else _canonical_decimal(node.constant),
        "children": [_encode_node(child) for child in node.children],
    }


def _json_strings(items: Iterable[str]) -> list[JsonValue]:
    """A JSON array of strings.

    `list(items)` is a `list[str]`, and `list` is invariant, so it is not a
    `list[JsonValue]` however obviously each element is one. Building it empty and
    extending is the spelling that types without a `cast`.
    """
    collected: list[JsonValue] = []
    collected.extend(items)
    return collected


def canonical_payload(genome: Genome, *, include_parameter_values: bool) -> JsonValue:
    """The data `genome_hash` and `structure_hash` are taken over.

    With `include_parameter_values=False` the parameter *names* survive and their values
    do not. That asymmetry is the whole lineage rule: re-tuning a threshold stays inside
    the family, while gaining a parameter is a new hypothesis and starts a new one.

    Feature ids are sorted rather than emitted as a set, because `frozenset` iteration
    order varies between processes and an unsorted list would make one genome's digest a
    function of which interpreter ran.
    """
    parameter_payload: JsonValue
    if include_parameter_values:
        parameter_values: dict[str, JsonValue] = {}
        for name, parameter in sorted(genome.parameters.items()):
            parameter_values[name] = _canonical_decimal(parameter)
        parameter_payload = parameter_values
    else:
        parameter_payload = _json_strings(sorted(genome.parameters))

    payload: dict[str, JsonValue] = {
        "entry_rule": _encode_node(genome.entry_rule),
        "parameters": parameter_payload,
        "feature_ids": _json_strings(sorted(genome.feature_ids)),
        "holding_horizon_microseconds": genome.holding_horizon_microseconds,
        # A version tag on the recipe itself. When a node kind is added, the digest input
        # changes shape, and without this every historical genome would silently acquire
        # a new identity rather than the change being a decision somebody made.
        "schema_version": 1,
    }
    return payload


def _digest(payload: JsonValue) -> str:
    rendered = json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    return hashlib.sha256(rendered.encode("utf-8")).hexdigest()


def genome_hash(genome: Genome) -> str:
    """The content address of this exact hypothesis, at these exact parameters."""
    return _digest(canonical_payload(genome, include_parameter_values=True))


def structure_hash(genome: Genome) -> str:
    """The content address of the hypothesis, independent of where its knobs are set."""
    return _digest(canonical_payload(genome, include_parameter_values=False))


def lineage_id_for(genome: Genome) -> str:
    """The family this genome belongs to.

    Derived from `structure_hash`, never assigned. An assignable lineage id is one a
    mutation operator can mint fresh, and a fresh id is how a family that has consumed
    612 trials reports itself as a newcomer.
    """
    return f"{_LINEAGE_ID_PREFIX}{structure_hash(genome)[:_LINEAGE_ID_HEX_CHARS]}"
