"""The distribution across paths, which is the finding. The mean is not.

A mean Sharpe of 1.1 assembled from paths ranging -0.9 to 3.0 is a strategy with no
stable edge, and the mean hides that completely. So `PathDistribution` reports the
spread, the tails and the fraction of paths that were positive at all, and it keeps every
path's trade count attached -- a twenty-eight-path summary in which fourteen paths had
three trades each is a twenty-eight-path summary of fourteen paths, and that has to be
visible rather than reconstructable (`BACKTEST_ENGINE.md` section 6.2).

**Stability is a defect signal.** `p95 - p05` very small across paths is not a triumph:
either the folds are not independent or the same data is in every training set. The flag
is computed here rather than left to a reader, because the reading it invites -- "very
consistent" -- is the opposite of what it means.

**Thin paths are excluded and counted, never absorbed.** A path below
`MINIMUM_PATH_TRADES` is marked insufficient and left out of every statistic, and the
number excluded is reported alongside. Averaging a three-trade Sharpe into the mean makes
the mean an average of an estimate and a rumour; dropping it silently makes the path
count a count of the paths that happened to trade.

Arithmetic is `Decimal` throughout. `docs/rules/decimal-and-money.md` would permit float64
for a statistical estimate, and the reason not to is determinism rather than precision:
these numbers are carried in a validation record a promotion gate reads, and a value whose
last bits depend on summation order is a value two runs of the same plan can disagree
about.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from decimal import Decimal
from typing import Final

from fking.backtest.cpcv._errors import CpcvConfigError

#: `BACKTEST_ENGINE.md` section 6.7: at least 30 trades in every CPCV test fold. Below it
#: the Sharpe's own standard error swamps the estimate, and the number is not weak
#: evidence -- it is not evidence either way, in either direction.
MINIMUM_PATH_TRADES: Final = 30

#: `p95 - p05` at or below this is reported as a defect signal. The value is deliberately
#: generous: genuine CPCV paths over crypto minute bars disagree by whole Sharpe points,
#: and a spread of a tenth means the paths are not independent draws. It is a trigger for
#: an investigation, not a rejection threshold, which is why nothing branches on it.
SUSPICIOUS_SHARPE_SPREAD: Final = Decimal("0.10")

# Twelve places, matching `fking.backtest.validation._pbo`: enough that a percentile of a
# several-hundred-path distribution is legible, and far beyond any threshold this project
# compares against.
_STATISTIC_QUANTUM: Final = Decimal("0.000000000001")

_ZERO: Final = Decimal("0")

# Two included paths is the arithmetic floor for a spread. At one, p05 and p95 are the
# same number, the spread is zero, and the stability flag fires on a distribution that
# was never computed.
_MINIMUM_INCLUDED_PATHS: Final = 2


def _require_finite_decimal(candidate: object, field_name: str) -> Decimal:
    """An exact, finite `Decimal`. Signed values are allowed; a float is not.

    Typed as `object` rather than as `Decimal` so the float branch is reachable. The
    annotation on the field says `Decimal` and the caller is under `mypy --strict`, but
    the values reaching this package come from an injected evaluator, and an evaluator is
    where a `float` Sharpe enters -- which is a value that is already rounded and whose
    last bits depend on summation order.
    """
    if isinstance(candidate, float):
        raise CpcvConfigError(
            f"{field_name} must be a Decimal constructed from str, not a float; "
            f"got {candidate!r}, which is already rounded"
        )
    if not isinstance(candidate, Decimal):
        raise CpcvConfigError(
            f"{field_name} must be a Decimal, got {type(candidate).__name__} {candidate!r}"
        )
    if not candidate.is_finite():
        # A `Decimal("NaN")` compares unequal to itself, so one of them turns every
        # percentile into a value that is neither above nor below any threshold, and
        # each comparison downstream silently takes the false branch.
        raise CpcvConfigError(f"{field_name} must be finite; got {candidate}")
    return candidate


def _require_count(candidate: object, field_name: str) -> int:
    """A non-negative `int`, and specifically not a `bool`."""
    if not isinstance(candidate, int) or isinstance(candidate, bool):
        raise CpcvConfigError(
            f"{field_name} must be an int, got {type(candidate).__name__} {candidate!r}"
        )
    if candidate < 0:
        raise CpcvConfigError(f"{field_name} must not be negative; got {candidate}")
    return candidate


class DistributionRefusedError(CpcvConfigError):
    """The paths supplied do not support a distribution.

    Raised rather than returning zeros. A `sharpe_p05` of zero reads as "measured, and
    flat", which is a different statement from "not measured" and the one a reader will
    act on.
    """


@dataclass(frozen=True, slots=True)
class PathPerformance:
    """What one path produced: its Sharpe, and how many trades stand behind it.

    The trade count is a required field rather than optional metadata, because it is what
    decides whether the Sharpe is admitted at all. A performance record that could omit
    it would let a path be counted before anybody asked what it was counted from.
    """

    path_index: int
    trade_count: int
    sharpe_ratio: Decimal

    def __post_init__(self) -> None:
        _require_count(self.path_index, "path_index")
        _require_count(self.trade_count, "trade_count")
        _require_finite_decimal(self.sharpe_ratio, "sharpe_ratio")

    @property
    def is_insufficient(self) -> bool:
        """Whether this path traded too little for its Sharpe to mean anything."""
        return self.trade_count < MINIMUM_PATH_TRADES


@dataclass(frozen=True, slots=True)
class PathDistribution:
    """The spread across paths, with the excluded paths counted rather than absorbed."""

    path_total: int
    included_path_total: int
    insufficient_path_total: int

    #: The indices left out, not merely how many. A reader who sees eight exclusions and
    #: cannot tell whether they were consecutive groups has lost the diagnosis.
    insufficient_path_indices: tuple[int, ...]

    sharpe_mean: Decimal
    sharpe_p05: Decimal
    sharpe_p95: Decimal

    #: `p95 - p05`. Reported as its own field because it is the number the stability
    #: reading is made from, and a reader deriving it will not.
    sharpe_spread: Decimal

    fraction_of_paths_positive: Decimal

    #: Every path's trade count, in path order, including the excluded ones.
    trade_counts: tuple[int, ...]

    @property
    def is_suspiciously_stable(self) -> bool:
        """Whether the paths agree so closely that the design is in question."""
        return self.sharpe_spread <= SUSPICIOUS_SHARPE_SPREAD


def _percentile(ordered: Sequence[Decimal], fraction: Decimal) -> Decimal:
    """Linear interpolation between the two neighbouring order statistics.

    The whole computation is exact `Decimal` up to one final quantisation, so the same
    paths in the same order produce the same percentile on every machine. The index
    arithmetic uses `(n - 1) * fraction`, which puts p00 on the minimum and p100 on the
    maximum rather than off the end of the sample.
    """
    position = (Decimal(len(ordered) - 1) * fraction).quantize(_STATISTIC_QUANTUM)
    lower_index = int(position)
    upper_index = min(lower_index + 1, len(ordered) - 1)
    weight = position - Decimal(lower_index)
    lower = ordered[lower_index]
    upper = ordered[upper_index]
    return (lower + (upper - lower) * weight).quantize(_STATISTIC_QUANTUM)


def path_distribution(performances: Sequence[PathPerformance]) -> PathDistribution:
    """Summarise the paths, excluding the thin ones and saying how many were excluded.

    The exclusion rule is applied here and nowhere else, so the count reported and the
    paths omitted are guaranteed to be the same set. A caller filtering before it called
    would produce a distribution whose `insufficient_path_total` was zero and whose
    `path_total` had already been reduced -- which is the exclusion made invisible in
    exactly the way `BACKTEST_ENGINE.md` section 9 forbids: mark it, exclude it, and
    report how many were excluded.
    """
    if not performances:
        raise DistributionRefusedError(
            "a distribution over zero paths is not a distribution; a CPCV run that "
            "completed no path has not been validated, which is a different statement "
            "from a poor spread"
        )
    included = [performance for performance in performances if not performance.is_insufficient]
    excluded = [performance for performance in performances if performance.is_insufficient]
    if len(included) < _MINIMUM_INCLUDED_PATHS:
        raise DistributionRefusedError(
            f"{len(included)} of {len(performances)} paths cleared "
            f"{MINIMUM_PATH_TRADES} trades; at least {_MINIMUM_INCLUDED_PATHS} are needed "
            f"before a spread exists, and a p05 computed from fewer would be one path's "
            f"Sharpe wearing a percentile's name"
        )

    ordered = sorted(performance.sharpe_ratio for performance in included)
    total = sum(ordered, _ZERO)
    included_total = Decimal(len(included))
    positive = sum(1 for sharpe in ordered if sharpe > _ZERO)
    sharpe_p05 = _percentile(ordered, Decimal("0.05"))
    sharpe_p95 = _percentile(ordered, Decimal("0.95"))

    return PathDistribution(
        path_total=len(performances),
        included_path_total=len(included),
        insufficient_path_total=len(excluded),
        insufficient_path_indices=tuple(performance.path_index for performance in excluded),
        sharpe_mean=(total / included_total).quantize(_STATISTIC_QUANTUM),
        sharpe_p05=sharpe_p05,
        sharpe_p95=sharpe_p95,
        sharpe_spread=(sharpe_p95 - sharpe_p05).quantize(_STATISTIC_QUANTUM),
        fraction_of_paths_positive=(Decimal(positive) / included_total).quantize(
            _STATISTIC_QUANTUM
        ),
        trade_counts=tuple(performance.trade_count for performance in performances),
    )
