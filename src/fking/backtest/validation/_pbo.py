"""Probability of backtest overfitting: the statistic that indicts the search.

PBO is the fraction of CPCV path-splits in which the configuration that was best **in
sample** underperforms the **median** out of sample (`BACKTEST_ENGINE.md` section 6.6).
Note what it does not measure: nothing here reads the winner's absolute performance. A
search whose in-sample champion lands below the out-of-sample median half the time has
been ranking noise, and that remains true whether the champion's Sharpe was 0.3 or 3.0.

That is why a high PBO with a good mean escalates rather than merely rejecting one
candidate. The finding is a property of the search process, so every other strategy that
process produced is implicated, including the ones already promoted.

The splits are an input rather than something generated here. Constructing the
combinatorial purged partition is the cross-validation harness's job, and a module that
both chose the splits and scored them could not be audited against the harness that
produced the paths.

`float` appears once, inside `_logit`, and only to take a logarithm. Everything this
module publishes is `Decimal`; the ranks themselves are computed by exact comparison, so
no ordering decision is ever made on a rounded number.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from decimal import Decimal
from math import log
from typing import Final

from fking.backtest.validation._errors import ValidationGateError

# Twelve places: enough that the logit of a rank in a several-hundred-configuration
# search is legible, and far beyond any threshold this project compares against.
_STATISTIC_QUANTUM: Final = Decimal("0.000000000001")

_ZERO: Final = Decimal("0")

# One configuration cannot underperform a median it defines, so its rank is always both
# best and median and the statistic degenerates to a constant. Two is the floor at which
# a ranking exists at all.
_MIN_CONFIGURATIONS_PER_SPLIT: Final = 2


class PathSplitMalformedError(ValidationGateError):
    """A split does not score the same configurations in sample and out of sample.

    The whole statistic is "where did the in-sample winner land out of sample", so the
    two vectors must be indexed by the same configurations in the same order. A length
    mismatch means the caller has aligned them by position and the positions disagree,
    which produces a perfectly plausible PBO for a question nobody asked.
    """


@dataclass(frozen=True, slots=True)
class PathSplit:
    """One CPCV path: every configuration scored in sample and out of sample.

    Both tuples are indexed by configuration, in the same order. A single configuration
    makes the statistic vacuous -- its rank is always both best and median -- so two is
    the floor.
    """

    in_sample_sharpe: tuple[Decimal, ...]
    out_of_sample_sharpe: tuple[Decimal, ...]

    def __post_init__(self) -> None:
        if len(self.in_sample_sharpe) != len(self.out_of_sample_sharpe):
            raise PathSplitMalformedError(
                f"split scores {len(self.in_sample_sharpe)} configurations in sample and "
                f"{len(self.out_of_sample_sharpe)} out of sample; the vectors are indexed "
                f"by configuration and cannot be aligned by position"
            )
        if len(self.in_sample_sharpe) < _MIN_CONFIGURATIONS_PER_SPLIT:
            raise PathSplitMalformedError(
                f"a split over {len(self.in_sample_sharpe)} configuration(s) has no "
                f"median to underperform; PBO measures a ranking, not a score"
            )


@dataclass(frozen=True, slots=True)
class OverfittingProbability:
    """The PBO statistic with the per-split evidence that produced it."""

    split_count: int
    configuration_count: int
    underperforming_split_count: int
    probability_of_backtest_overfitting: Decimal
    # One relative-rank logit per split, in split order. Negative means the in-sample
    # winner landed at or below the out-of-sample median on that path. Retained because
    # the fraction alone cannot distinguish "just below the median on every path" from
    # "catastrophic on a third of them", and those two have different diagnoses.
    rank_logits: tuple[Decimal, ...]


def _winning_configuration_index(in_sample_sharpe: Sequence[Decimal]) -> int:
    """The configuration a searcher would have selected on this path's training half.

    Ties break toward the lower index, which is the same rule `max` applies and is
    stated here so the choice is deliberate rather than incidental to the builtin.
    """
    return max(range(len(in_sample_sharpe)), key=lambda index: (in_sample_sharpe[index], -index))


def _out_of_sample_rank(out_of_sample_sharpe: Sequence[Decimal], winner_index: int) -> int:
    """One-based ascending rank of `winner_index`: rank 1 is the worst configuration.

    Exact `Decimal` comparison, never a rounded proxy. Ties resolve by index so that two
    configurations with identical out-of-sample Sharpe still receive distinct ranks, and
    the rank vector remains a permutation.
    """
    ordered = sorted(
        range(len(out_of_sample_sharpe)),
        key=lambda index: (out_of_sample_sharpe[index], index),
    )
    return ordered.index(winner_index) + 1


def _logit(relative_rank: Decimal) -> Decimal:
    """`ln(r / (1 - r))`, the symmetric measure of how far the winner fell.

    `r` is `rank / (configuration_count + 1)`, so it is strictly inside (0, 1) and the
    logarithm is always defined -- the `+ 1` in the denominator is what buys that, and
    is the reason the best configuration does not produce an infinite logit.
    """
    return Decimal(str(log(float(relative_rank) / (1.0 - float(relative_rank))))).quantize(
        _STATISTIC_QUANTUM
    )


def probability_of_backtest_overfitting(
    splits: Sequence[PathSplit],
) -> OverfittingProbability:
    """The fraction of paths on which the in-sample winner failed to beat the median.

    A logit at or below zero counts as overfit. Zero itself -- the winner landing exactly
    on the median -- counts against the search, because a selection procedure that picked
    a median performer learned nothing on that path.
    """
    if not splits:
        raise PathSplitMalformedError(
            "PBO over zero splits is not a probability; a search with no CPCV paths has "
            "not been validated, which is a different statement from a low PBO"
        )
    configuration_count = len(splits[0].in_sample_sharpe)
    mismatched = [
        index
        for index, split in enumerate(splits)
        if len(split.in_sample_sharpe) != configuration_count
    ]
    if mismatched:
        raise PathSplitMalformedError(
            f"splits {mismatched} score a different number of configurations than split "
            f"0 ({configuration_count}); the ranks would not be comparable across paths"
        )

    divisor = Decimal(configuration_count + 1)
    logits: list[Decimal] = []
    for split in splits:
        winner_index = _winning_configuration_index(split.in_sample_sharpe)
        rank = _out_of_sample_rank(split.out_of_sample_sharpe, winner_index)
        logits.append(_logit(Decimal(rank) / divisor))

    underperforming_split_count = sum(1 for logit in logits if logit <= _ZERO)
    return OverfittingProbability(
        split_count=len(splits),
        configuration_count=configuration_count,
        underperforming_split_count=underperforming_split_count,
        probability_of_backtest_overfitting=(
            Decimal(underperforming_split_count) / Decimal(len(splits))
        ).quantize(_STATISTIC_QUANTUM),
        rank_logits=tuple(logits),
    )
