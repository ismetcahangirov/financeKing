"""The Bailey & Lopez de Prado equations, recomputed by an independent numerical route.

A test that asserts `deflated_sharpe_ratio` equals a constant produced by
`deflated_sharpe_ratio` proves nothing, and a test that reimplements it with the same
`statistics.NormalDist` calls proves only that the arithmetic was copied faithfully. So
this file computes the same two quantities without touching `NormalDist` at all: the
normal CDF from `math.erf`, and its inverse by bisection on that CDF.

The two routes are genuinely different. `NormalDist.inv_cdf` is a rational approximation
(Wichura's AS 241); the bisection here converges on the erf-based CDF to a tolerance far
tighter than the six decimal places asserted. Agreement between them at six places is a
real check on the transcription -- eq. 5 for `SR*` and eq. 9 for the deflated ratio in
Bailey & Lopez de Prado (2014), "The Deflated Sharpe Ratio: Correcting for Selection
Bias, Backtest Overfitting and Non-Normality", *Journal of Portfolio Management* 40(5).

What it deliberately does not claim: these are not the paper's own printed worked
figures, which were not available offline to this session. It is the paper's *formulae*,
evaluated at inputs stated here, by two independent numerical implementations. A sign
error, a transposed term or a misplaced Euler-Mascheroni weight fails it; a
misunderstanding of the paper shared by both implementations would not.
"""

from __future__ import annotations

from decimal import Decimal
from math import erf, sqrt
from typing import Final

import pytest

from fking.backtest.validation import (
    SharpeEvidence,
    deflated_sharpe_ratio,
    expected_max_sharpe,
)

pytestmark = pytest.mark.unit

# Bailey & Lopez de Prado (2014), eq. 5. Restated here rather than imported, because
# importing the constant under test from the module under test is how a reference
# implementation stops being one.
EULER_MASCHERONI: Final[float] = 0.5772156649015329

# Six decimal places, which is the criterion in issue #82. The two routes agree far
# closer than this in practice; the tolerance is the claim, not the observation.
TOLERANCE: Final[Decimal] = Decimal("0.000001")


def normal_cdf(quantile: float) -> float:
    """The standard normal CDF from `math.erf`, sharing no code with `NormalDist`."""
    return 0.5 * (1.0 + erf(quantile / sqrt(2.0)))


def normal_inverse_cdf(probability: float) -> float:
    """The inverse, by bisection on `normal_cdf`.

    Slow and boring on purpose. A rational approximation here would be a second copy of
    the thing being checked; bisection converges from the definition, so the only way
    both routes agree is if the definition is the same one.

    200 halvings of a 40-wide bracket is a residual near 2^-200, so the returned value is
    exact to double precision and the six-place tolerance is entirely spent on the
    transcription rather than on the root finder.
    """
    low, high = -20.0, 20.0
    for _ in range(200):
        middle = (low + high) / 2.0
        if normal_cdf(middle) < probability:
            low = middle
        else:
            high = middle
    return (low + high) / 2.0


def reference_expected_max_sharpe(trial_count: int, variance_across_trials: float) -> float:
    """`SR*`, eq. 5: the Sharpe the best of `trial_count` zero-edge trials would show."""
    upper = normal_inverse_cdf(1.0 - 1.0 / trial_count)
    # `1 - 1/(N e)` in the paper; `exp(1)` written as the base of the natural log so the
    # expression reads as the paper's rather than as a call to a constant.
    lower = normal_inverse_cdf(1.0 - 1.0 / (trial_count * 2.718281828459045))
    return sqrt(variance_across_trials) * (
        (1.0 - EULER_MASCHERONI) * upper + EULER_MASCHERONI * lower
    )


def reference_deflated_sharpe(evidence: SharpeEvidence) -> float:
    """The deflated ratio, eq. 9.

    It takes the same input model as the implementation under test on purpose. What has
    to be independent is the arithmetic, not the container the numbers arrive in, and a
    second set of six parameters here would be six more chances to transpose one.
    """
    observed_sharpe = float(evidence.observed_sharpe)
    benchmark = reference_expected_max_sharpe(
        evidence.trials_at_time_of_run, float(evidence.sharpe_variance_across_trials)
    )
    denominator = sqrt(
        1.0
        - float(evidence.skewness) * observed_sharpe
        + ((float(evidence.kurtosis) - 1.0) / 4.0) * observed_sharpe**2
    )
    return normal_cdf(
        (observed_sharpe - benchmark) * sqrt(evidence.independent_episode_count - 1) / denominator
    )


# (trial_count, variance_across_trials). Spanning four orders of magnitude in the trial
# count, because `SR*` grows as sqrt(2 ln K) and a transcription error in the weighting
# shows up as a divergence that widens with K rather than as a constant offset.
BENCHMARK_CASES: Final[tuple[tuple[int, str], ...]] = (
    (2, "0.0100"),
    (10, "0.0100"),
    (100, "0.0100"),
    (1_847, "0.0025"),
    (10_000, "0.0400"),
    (50_000, "0.0100"),
)


@pytest.mark.parametrize(("trial_count", "variance"), BENCHMARK_CASES)
def test_the_selection_benchmark_matches_an_independent_implementation(
    trial_count: int, variance: str
) -> None:
    ours = expected_max_sharpe(trial_count, Decimal(variance))
    theirs = Decimal(str(reference_expected_max_sharpe(trial_count, float(variance))))

    assert abs(ours - theirs) < TOLERANCE


# The negative-skew and fat-tail rows are the ones that matter: those terms sit in a
# denominator, and a sign error there produces a number that still looks like a
# probability.
DEFLATION_CASES: Final[tuple[SharpeEvidence, ...]] = (
    # symmetric and mesokurtic after a modest search
    SharpeEvidence(
        observed_sharpe=Decimal("0.50"),
        trials_at_time_of_run=100,
        independent_episode_count=120,
        skewness=Decimal("0"),
        kurtosis=Decimal("3"),
        sharpe_variance_across_trials=Decimal("0.0100"),
    ),
    # 37 funding-extremity episodes after a 200-point grid
    SharpeEvidence(
        observed_sharpe=Decimal("0.42"),
        trials_at_time_of_run=200,
        independent_episode_count=37,
        skewness=Decimal("-0.30"),
        kurtosis=Decimal("6.00"),
        sharpe_variance_across_trials=Decimal("0.0100"),
    ),
    # the same result once the project has searched 21x harder
    SharpeEvidence(
        observed_sharpe=Decimal("0.42"),
        trials_at_time_of_run=4200,
        independent_episode_count=37,
        skewness=Decimal("-0.30"),
        kurtosis=Decimal("6.00"),
        sharpe_variance_across_trials=Decimal("0.0100"),
    ),
    # positive skew and thin tails, which raise rather than lower it
    SharpeEvidence(
        observed_sharpe=Decimal("1.10"),
        trials_at_time_of_run=1847,
        independent_episode_count=500,
        skewness=Decimal("0.45"),
        kurtosis=Decimal("2.50"),
        sharpe_variance_across_trials=Decimal("0.0025"),
    ),
    # the smallest defined search, at the floor of both counts
    SharpeEvidence(
        observed_sharpe=Decimal("0.05"),
        trials_at_time_of_run=2,
        independent_episode_count=5,
        skewness=Decimal("-1.20"),
        kurtosis=Decimal("8.00"),
        sharpe_variance_across_trials=Decimal("0.0400"),
    ),
    # strongly adverse moments against a very large pool
    SharpeEvidence(
        observed_sharpe=Decimal("0.80"),
        trials_at_time_of_run=50000,
        independent_episode_count=90,
        skewness=Decimal("-0.90"),
        kurtosis=Decimal("9.00"),
        sharpe_variance_across_trials=Decimal("0.0100"),
    ),
)


@pytest.mark.parametrize("case", DEFLATION_CASES, ids=lambda c: f"K={c.trials_at_time_of_run}")
def test_the_deflated_sharpe_matches_an_independent_implementation(
    case: SharpeEvidence,
) -> None:
    ours = deflated_sharpe_ratio(case)
    theirs = Decimal(str(reference_deflated_sharpe(case)))

    assert abs(ours - theirs) < TOLERANCE
