"""Frozen digests of every feature definition that has ever been registered.

One entry per `(name, version)`, and **entries are never edited and never removed.**
That is the whole mechanism. A digest recomputed from a `compute` function that no
longer matches its locked value means the definition changed under a version number
somebody else's results are attributed to, and the fix is a new version rather than a
new digest.

Removal is forbidden for the same reason: a `(name, version)` that has left this file
is a version whose definition can be quietly re-used for something else, and every
backtest that recorded the old number would then be describing a computation nobody can
reconstruct.

Adding a version is a two-line diff -- the new `FeatureSpec` in `registry`, and its
digest here -- and both lines sit in the same pull request, next to the version number
being incremented. That adjacency is the review.
"""

from __future__ import annotations

from collections.abc import Mapping
from types import MappingProxyType
from typing import Final

__all__ = ["DEFINITION_DIGESTS"]

DEFINITION_DIGESTS: Final[Mapping[tuple[str, int], str]] = MappingProxyType(
    {
        ("trailing_return_fraction", 1): "43434fc52aa382fef357044c4c9ed2f6",
        ("trailing_realised_volatility", 1): "888a349bd311691ecfef21f152190a6a",
    }
)
