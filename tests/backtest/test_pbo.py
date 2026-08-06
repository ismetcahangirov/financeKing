"""PBO indicts the search: a random in-sample winner fails regardless of its score.

The synthetic search below has no relationship between the in-sample and out-of-sample
halves of each path -- both are drawn independently from a seeded generator. A selection
procedure applied to it is picking uniformly at random, so its winner lands below the
out-of-sample median about half the time and PBO comes out near 0.5.

Nothing in that construction says anything about how *good* the winner was. That is the
statistic's whole contribution: it measures whether the ranking carried information, so a
search that ranked noise fails even when the number it produced looks excellent
(`BACKTEST_ENGINE.md` section 6.6).
"""

from __future__ import annotations

import random
from decimal import Decimal
from typing import Final

import pytest

from fking.backtest.validation import (
    MAX_PROBABILITY_OF_BACKTEST_OVERFITTING,
    PathSplit,
    PathSplitMalformedError,
    SharpeEvidence,
    ValidationRefusal,
    assess_validation,
    probability_of_backtest_overfitting,
)

pytestmark = pytest.mark.unit

_SEED: Final = 20260801
_SPLIT_COUNT: Final = 200
_CONFIGURATION_COUNT: Final = 24
# The degenerate cases below need only enough paths to show the fraction is 0 or 1.
_AGREEING_SPLIT_COUNT: Final = 10


def _uncorrelated_splits() -> tuple[PathSplit, ...]:
    """Paths whose out-of-sample ranking is independent of the in-sample ranking."""
    generator = random.Random(_SEED)
    return tuple(
        PathSplit(
            in_sample_sharpe=tuple(
                Decimal(str(generator.gauss(0.0, 1.0))) for _ in range(_CONFIGURATION_COUNT)
            ),
            out_of_sample_sharpe=tuple(
                Decimal(str(generator.gauss(0.0, 1.0))) for _ in range(_CONFIGURATION_COUNT)
            ),
        )
        for _ in range(_SPLIT_COUNT)
    )


def _strong_evidence() -> SharpeEvidence:
    """A candidate that sails through the deflated-Sharpe gate.

    Chosen so the PBO refusal below cannot be confused with a weak Sharpe: this is the
    "high PBO with a good mean" case, which is the classic signature the statistic exists
    to catch.
    """
    return SharpeEvidence(
        observed_sharpe=Decimal("0.9"),
        trials_at_time_of_run=24,
        independent_episode_count=120,
        skewness=Decimal("0.1"),
        kurtosis=Decimal("3.0"),
        sharpe_variance_across_trials=Decimal("0.01"),
    )


def test_a_search_whose_winner_is_effectively_random_exceeds_the_ceiling() -> None:
    overfitting = probability_of_backtest_overfitting(_uncorrelated_splits())

    assert overfitting.split_count == _SPLIT_COUNT
    assert overfitting.configuration_count == _CONFIGURATION_COUNT
    assert overfitting.probability_of_backtest_overfitting > MAX_PROBABILITY_OF_BACKTEST_OVERFITTING
    assert len(overfitting.rank_logits) == _SPLIT_COUNT


def test_a_high_pbo_fails_validation_even_with_an_excellent_deflated_sharpe() -> None:
    report = assess_validation(_strong_evidence(), _uncorrelated_splits())

    assert ValidationRefusal.OVERFITTING_PROBABILITY_ABOVE_CEILING in report.refusals
    assert ValidationRefusal.DEFLATED_SHARPE_BELOW_FLOOR not in report.refusals
    assert report.is_evidence is False
    # The finding is about the search, so it implicates every candidate that search
    # produced rather than only this one.
    assert report.indicts_the_search is True


def test_a_search_whose_ranking_holds_out_of_sample_reports_zero() -> None:
    ascending = tuple(Decimal(index) for index in range(_CONFIGURATION_COUNT))
    splits = tuple(
        PathSplit(in_sample_sharpe=ascending, out_of_sample_sharpe=ascending)
        for _ in range(_AGREEING_SPLIT_COUNT)
    )

    overfitting = probability_of_backtest_overfitting(splits)

    assert overfitting.probability_of_backtest_overfitting == Decimal("0")
    assert overfitting.underperforming_split_count == 0
    assert all(logit > Decimal("0") for logit in overfitting.rank_logits)


def test_a_winner_that_lands_last_out_of_sample_counts_against_every_path() -> None:
    ascending = tuple(Decimal(index) for index in range(_CONFIGURATION_COUNT))
    splits = tuple(
        PathSplit(in_sample_sharpe=ascending, out_of_sample_sharpe=tuple(reversed(ascending)))
        for _ in range(_AGREEING_SPLIT_COUNT)
    )

    overfitting = probability_of_backtest_overfitting(splits)

    assert overfitting.probability_of_backtest_overfitting == Decimal("1")
    assert overfitting.underperforming_split_count == _AGREEING_SPLIT_COUNT


def test_a_winner_exactly_on_the_median_counts_as_overfit() -> None:
    """Three configurations, winner ranked second of three: relative rank is 1/2.

    The logit is exactly zero, and the boundary is resolved against the search. A
    selection procedure that picked a median performer learned nothing on that path, and
    a threshold that let it through would be a threshold with a favourable rounding rule.
    """
    split = PathSplit(
        in_sample_sharpe=(Decimal("0"), Decimal("1"), Decimal("2")),
        out_of_sample_sharpe=(Decimal("0"), Decimal("3"), Decimal("1")),
    )

    overfitting = probability_of_backtest_overfitting([split])

    assert overfitting.rank_logits == (Decimal("0.000000000000"),)
    assert overfitting.probability_of_backtest_overfitting == Decimal("1")


@pytest.mark.parametrize(
    ("in_sample_length", "out_of_sample_length"),
    [(3, 2), (1, 1)],
)
def test_a_split_that_cannot_be_ranked_is_refused(
    in_sample_length: int, out_of_sample_length: int
) -> None:
    with pytest.raises(PathSplitMalformedError):
        PathSplit(
            in_sample_sharpe=tuple(Decimal(index) for index in range(in_sample_length)),
            out_of_sample_sharpe=tuple(Decimal(index) for index in range(out_of_sample_length)),
        )


def test_a_search_with_no_paths_is_refused_rather_than_reported_as_zero() -> None:
    with pytest.raises(PathSplitMalformedError, match="has not been validated"):
        probability_of_backtest_overfitting([])


def test_paths_scoring_different_configuration_counts_are_refused() -> None:
    splits = [
        PathSplit(
            in_sample_sharpe=(Decimal("0"), Decimal("1")),
            out_of_sample_sharpe=(Decimal("1"), Decimal("0")),
        ),
        PathSplit(
            in_sample_sharpe=(Decimal("0"), Decimal("1"), Decimal("2")),
            out_of_sample_sharpe=(Decimal("2"), Decimal("1"), Decimal("0")),
        ),
    ]

    with pytest.raises(PathSplitMalformedError, match="not be comparable across paths"):
        probability_of_backtest_overfitting(splits)
