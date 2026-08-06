"""The best of a 500-point grid over one window is rejected, and one trial is not.

The pair is the whole test. The *same* return series, the same skewness, the same
kurtosis, the same effective sample -- and a verdict that flips purely on the trial count.
That is what proves the gate is driven by how hard the search looked rather than by the
candidate's own shape, which is the property a raw Sharpe cannot express at all.

The grid is simulated from a seeded generator drawing zero-mean returns, so every
configuration in it has no edge by construction. Its winner is therefore, by definition,
a number produced by selection alone -- and a per-episode Sharpe near 0.2 over 250
episodes looks entirely respectable when printed without `K`.

`random.Random(...)` with its own instance rather than the module-level generator:
`pytest-randomly` reseeds `random` per test, so a module-level draw would produce a
different grid on a different run order and this file's assertions would become a
coin flip on the shuffle.
"""

from __future__ import annotations

import random
import statistics
from decimal import Decimal
from typing import Final

import pytest

from fking.backtest.validation import (
    MIN_DEFLATED_SHARPE,
    PathSplit,
    SharpeEvidence,
    ValidationRefusal,
    assess_validation,
    deflated_sharpe_ratio,
)

pytestmark = pytest.mark.unit

# Fixed so the grid, its winner and every moment derived from it are identical on every
# run and on every machine. Same value the suite pins in `addopts`.
_SEED: Final = 20260801
_GRID_POINT_COUNT: Final = 500
_EPISODE_COUNT: Final = 250


def _noise_grid() -> tuple[list[list[float]], list[float]]:
    """500 configurations of zero-edge per-episode returns, and their Sharpes."""
    generator = random.Random(_SEED)
    episodes_by_configuration = [
        [generator.gauss(0.0, 1.0) for _ in range(_EPISODE_COUNT)] for _ in range(_GRID_POINT_COUNT)
    ]
    sharpes = [
        statistics.fmean(episodes) / statistics.stdev(episodes)
        for episodes in episodes_by_configuration
    ]
    return episodes_by_configuration, sharpes


def _winning_evidence(trials_at_time_of_run: int) -> SharpeEvidence:
    """The grid's best configuration, discounted against a stated trial count."""
    episodes_by_configuration, sharpes = _noise_grid()
    winner_index = max(range(len(sharpes)), key=lambda index: sharpes[index])
    winner_episodes = episodes_by_configuration[winner_index]

    mean_return = statistics.fmean(winner_episodes)
    deviation = statistics.stdev(winner_episodes)
    standardised = [(episode - mean_return) / deviation for episode in winner_episodes]
    skewness = statistics.fmean([moment**3 for moment in standardised])
    kurtosis = statistics.fmean([moment**4 for moment in standardised])

    return SharpeEvidence(
        observed_sharpe=Decimal(str(sharpes[winner_index])),
        trials_at_time_of_run=trials_at_time_of_run,
        independent_episode_count=_EPISODE_COUNT,
        skewness=Decimal(str(skewness)),
        kurtosis=Decimal(str(kurtosis)),
        sharpe_variance_across_trials=Decimal(str(statistics.pvariance(sharpes))),
    )


def _informative_splits() -> tuple[PathSplit, ...]:
    """Splits whose in-sample winner is also the out-of-sample best, so PBO is zero.

    The point of this file is the deflated Sharpe, and a PBO refusal arriving alongside
    it would make the verdict unattributable. These splits deliberately carry perfect
    rank agreement so that any refusal below comes from the trial count and nothing else.
    """
    ascending = tuple(Decimal(index) for index in range(8))
    return tuple(
        PathSplit(in_sample_sharpe=ascending, out_of_sample_sharpe=ascending) for _ in range(16)
    )


def test_best_of_a_five_hundred_point_grid_is_refused_by_the_deflated_sharpe_gate() -> None:
    report = assess_validation(_winning_evidence(_GRID_POINT_COUNT), _informative_splits())

    assert report.refusals == (ValidationRefusal.DEFLATED_SHARPE_BELOW_FLOOR,)
    assert report.is_evidence is False
    assert report.deflated_sharpe < MIN_DEFLATED_SHARPE
    # The benchmark is what the search alone explains. For a zero-edge grid it should
    # account for essentially the whole observed figure, which is the reason the
    # candidate fails despite an observed Sharpe that reads as respectable.
    assert report.selection_benchmark_sharpe > Decimal("0")
    assert report.observed_sharpe > Decimal("0")


def test_the_same_returns_at_one_trial_pass_because_nothing_was_selected() -> None:
    report = assess_validation(_winning_evidence(1), _informative_splits())

    assert report.refusals == ()
    assert report.is_evidence is True
    assert report.deflated_sharpe >= MIN_DEFLATED_SHARPE
    # No selection took place, so there is nothing for the observed Sharpe to beat.
    assert report.selection_benchmark_sharpe == Decimal("0")


def test_the_verdict_moves_only_because_the_trial_count_moved() -> None:
    """The two evidence records differ in exactly one field."""
    searched = _winning_evidence(_GRID_POINT_COUNT)
    unsearched = _winning_evidence(1)

    assert searched.model_dump(exclude={"trials_at_time_of_run"}) == unsearched.model_dump(
        exclude={"trials_at_time_of_run"}
    )
    assert deflated_sharpe_ratio(searched) < deflated_sharpe_ratio(unsearched)
