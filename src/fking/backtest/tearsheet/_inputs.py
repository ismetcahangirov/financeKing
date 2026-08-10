"""Everything the tearsheet renders, gathered into one frozen input.

The type is a container rather than a fetcher on purpose: `render_tearsheet` performs no
I/O, reads no clock and consults no environment. Every fact the document states arrives
through this dataclass, so a rendering can be reproduced from stored values alone and a
test can construct the exact run it wants to see rendered without a corpus on disk.

`equity_path` is required even for a run whose curve will be suppressed. An optional path
would let the suppression rule pass vacuously -- "no curve was drawn" would mean "there
was nothing to draw" rather than "this result has not earned the picture" -- and the
whole point of the rule is that the curve exists and is deliberately withheld.

`engine_git_sha` is supplied, never discovered. Reading it from `git` at render time
would make the document describe the working tree that rendered it rather than the build
that produced the run, which is the same substitution the "never regenerated on demand"
rule exists to prevent.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from datetime import date
from decimal import Decimal
from types import MappingProxyType
from typing import Final

from fking.backtest.cpcv import PathDistribution
from fking.backtest.feed import SymbolCoverage
from fking.backtest.portfolio import EquityPath
from fking.backtest.results import BacktestResult
from fking.backtest.tearsheet._errors import TearsheetInputError

__all__ = [
    "MIN_ENGINE_SHA_LENGTH",
    "EngineBuild",
    "HeldOutStatus",
    "TearsheetInputs",
]

# An abbreviated git SHA is ambiguous below seven hex digits in a repository of any size,
# and `git rev-parse --short` itself will not emit fewer. A four-character "sha" in a
# provenance header is a placeholder somebody typed.
MIN_ENGINE_SHA_LENGTH: Final[int] = 7

_HEX_DIGITS: Final[frozenset[str]] = frozenset("0123456789abcdefABCDEF")


def _require_text(candidate: str, field_name: str) -> str:
    if not candidate.strip():
        raise TearsheetInputError(
            f"{field_name} must not be blank; an empty provenance field renders as "
            f"'nothing to report' rather than 'nobody supplied it'"
        )
    return candidate


@dataclass(frozen=True, slots=True)
class EngineBuild:
    """Which build of the engine produced the run.

    Two fields rather than one: the SHA identifies the source, and `is_working_tree_dirty`
    says whether the source is what actually ran. A run produced from an edited working
    tree is not reproducible from its SHA, and the header says so rather than implying a
    clean build by omission.
    """

    git_sha: str
    is_working_tree_dirty: bool = False

    def __post_init__(self) -> None:
        _require_text(self.git_sha, "engine git_sha")
        if len(self.git_sha) < MIN_ENGINE_SHA_LENGTH:
            raise TearsheetInputError(
                f"engine git_sha {self.git_sha!r} is {len(self.git_sha)} characters; at "
                f"least {MIN_ENGINE_SHA_LENGTH} are needed for it to identify a commit"
            )
        if not all(character in _HEX_DIGITS for character in self.git_sha):
            raise TearsheetInputError(
                f"engine git_sha {self.git_sha!r} is not hexadecimal; a branch name or a "
                f"tag identifies a moving target, not the build that produced the run"
            )

    @property
    def label(self) -> str:
        """The SHA as the header states it, dirtiness included."""
        return f"{self.git_sha}-dirty" if self.is_working_tree_dirty else self.git_sha


@dataclass(frozen=True, slots=True)
class HeldOutStatus:
    """The permanently held-out period, and whether this run consumed it.

    Rendered on every tearsheet including the runs that never went near it, because
    "the held-out period is intact" is a claim that has to be restated per artefact to
    mean anything. A footer that mentioned the held-out period only when it was burned
    would leave a reader unable to distinguish "intact" from "the field was dropped".
    """

    start: date
    end: date
    is_burned: bool

    def __post_init__(self) -> None:
        if self.end <= self.start:
            raise TearsheetInputError(
                f"held-out end {self.end.isoformat()} is not after start {self.start.isoformat()}"
            )

    @property
    def label(self) -> str:
        """What the footer says about the window."""
        state = "BURNED -- read, and burned on read" if self.is_burned else "intact, never read"
        return f"{self.start.isoformat()} .. {self.end.isoformat()}: {state}"


@dataclass(frozen=True, slots=True)
class TearsheetInputs:
    """One run's complete tearsheet input: the audited result and its provenance.

    `cpcv_distribution` is `None` when combinatorial purged cross-validation did not run.
    That is a rendered state, not an omitted section: a document that silently drops the
    envelope when CPCV is absent is indistinguishable from one whose CPCV produced a band
    so narrow it disappeared.
    """

    backtest_result: BacktestResult
    engine: EngineBuild
    equity_path: EquityPath
    coverage: tuple[SymbolCoverage, ...]
    parameters: Mapping[str, Decimal]
    feature_versions: Mapping[str, str]
    held_out: HeldOutStatus
    cpcv_distribution: PathDistribution | None = None

    def __post_init__(self) -> None:
        if not self.coverage:
            raise TearsheetInputError(
                "coverage names no series; a provenance footer that lists no data is a "
                "claim that the run read nothing"
            )
        for name, parameter in self.parameters.items():
            _require_text(name, "parameter name")
            if not isinstance(parameter, Decimal):
                # `docs/rules/decimal-and-money.md`: the footer prints the parameter set
                # verbatim, and a float that reached it would be printed as the binary
                # double it already is -- `0.1` rendering as `0.1000000000000000055`, or
                # worse, rendering as `0.1` while the run used the double.
                raise TearsheetInputError(
                    f"parameter {name!r} is a {type(parameter).__name__}, not a Decimal"
                )
        for feature_name, feature_version in self.feature_versions.items():
            _require_text(feature_name, "feature name")
            _require_text(feature_version, f"version of feature {feature_name!r}")

        # `frozen=True` protects the binding, not the mapping bound. A parameter changed
        # through a reference the caller kept would leave the rendered footer describing a
        # configuration that no longer exists (`docs/rules/immutability.md`).
        object.__setattr__(self, "parameters", MappingProxyType(dict(self.parameters)))
        object.__setattr__(self, "feature_versions", MappingProxyType(dict(self.feature_versions)))
