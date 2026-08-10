"""What a strategy asks the feature store for, in its own module.

Split out of `_spec` rather than left there because two things need it and one of them
sits *below* the specification: `InvalidationRule` names the feature its distance scales
off, and `StrategySpec` holds the rule. Leaving the type in `_spec` would make the
invalidation rule import the specification that contains it, which is a cycle at import
time and a layering inversion at every other time.
"""

from __future__ import annotations

from dataclasses import dataclass

from fking.strategy._guards import require_positive_int, require_text

__all__ = ["FeatureRequirement"]


@dataclass(frozen=True, slots=True)
class FeatureRequirement:
    """One feature series a strategy needs, named the way the feature registry keys it.

    `feature_version` travels with the name everywhere, for the reason
    `fking.data.features.spec` gives: a reference carrying only a name resolves to
    "whatever definition is current", and that is the read that makes a historical result
    irreproducible. A strategy validated against v1 and run against v2 is a different
    experiment with the same lineage id.
    """

    feature_name: str
    feature_version: int

    def __post_init__(self) -> None:
        require_text(self.feature_name, "feature_name")
        require_positive_int(self.feature_version, "feature_version")

    @property
    def key(self) -> tuple[str, int]:
        """The feature registry's key, so a caller never assembles one by hand."""
        return (self.feature_name, self.feature_version)

    def describe(self) -> str:
        """`trailing_return_fraction v1` -- the phrase every refusal names it by."""
        return f"{self.feature_name} v{self.feature_version}"
