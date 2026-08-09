"""Effective sample size: how many independent observations a daily return series holds.

Everywhere an `n` appears in this package it is `n_eff = n / (1 + 2 * sum(rho_k))`,
summing the lag-`k` autocorrelation of daily attributed returns up to the first
non-significant lag. For a strategy holding five-day positions this comes out near
`n / 5`, because a five-day hold overlaps four of its neighbours and the returns are not
five independent draws -- they are one draw seen five times.

Skipping the correction is how an overlapping-position strategy manufactures apparent
significance, and it is not an exotic failure: the mutation operators in P6 produce
overlapping-position designs by default, so the uncorrected count would be wrong for the
majority of the population rather than for an unlucky corner of it. The t-statistic
scales with `sqrt(n)`, so treating 1000 overlapping daily observations as independent
overstates it by `sqrt(5)`, about 2.2 -- which converts a p-value of 0.15 into one below
0.01 with no other change.

**The stopping rule.** Summation stops at the first lag whose autocorrelation is not
significant under Bartlett's white-noise approximation, `|rho_k| > 1.96 / sqrt(n)`.
Summing every computable lag instead would accumulate estimation noise: the standard
error of each `rho_k` is itself about `1/sqrt(n)`, so a hundred insignificant lags
contribute a random walk of that size to the correction and the resulting `n_eff` is
dominated by the tail rather than by the dependence structure.

**The inflation factor is floored at one.** Negative autocorrelation -- a mean-reverting
overlay, or the bid-ask bounce in a high-turnover series -- can produce a factor below
one and therefore an `n_eff` larger than `n`. Reporting more independent observations
than observations is the same manufacture of significance running in the opposite
direction, so the floor is a refusal rather than a convenience.

`float` inside this module is the statistical exception in
`docs/rules/decimal-and-money.md`, bounded by `_float_series` -- the one named
conversion boundary here. Everything published is `Decimal`, so nothing leaves as a
`float`.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from decimal import Decimal
from math import sqrt
from typing import Final

from fking.backtest.portfolio._errors import MetricInputError

# Two-sided 95% critical value of the standard normal. Bartlett's approximation gives
# each sample autocorrelation a standard error near 1/sqrt(n) under the white-noise
# null, so this times 1/sqrt(n) is the band a lag must clear to be counted.
_SIGNIFICANCE_Z: Final[float] = 1.959963984540054

# Below this the autocorrelation estimates are noise: at n = 8 the significance band is
# already +/-0.69, so nothing but a near-deterministic series clears it and the
# correction reports independence it has not established.
MIN_OBSERVATIONS_FOR_EFFECTIVE_SAMPLE: Final[int] = 8

# Lags beyond a quarter of the sample are estimated from fewer than 75% of the pairs and
# are conventionally not trusted; the rule is Box-Jenkins' and it also bounds the walk
# the stopping rule above is designed to avoid.
_MAX_LAG_FRACTION: Final[int] = 4

_QUANTUM: Final = Decimal("0.000001")
_ONE: Final = Decimal("1")


def _float_series(return_fractions: Sequence[Decimal]) -> tuple[float, ...]:
    """The one place `Decimal` becomes `float` in this module."""
    return tuple(float(return_fraction) for return_fraction in return_fractions)


@dataclass(frozen=True, slots=True)
class EffectiveSample:
    """The autocorrelation-corrected observation count, with its own working shown.

    `autocorrelations` holds every lag that was *examined*, including the first
    non-significant one that stopped the sum. Publishing only the corrected number would
    make a surprising `n_eff` unarguable months later, and the correction is exactly the
    kind of figure a reader wants to check rather than accept.
    """

    observation_count: int
    n_eff: Decimal
    inflation_factor: Decimal
    significant_lag_count: int
    autocorrelations: tuple[Decimal, ...]

    @property
    def episode_count(self) -> int:
        """`n_eff` floored to a whole number, for the statistics that need an integer.

        Floored rather than rounded: half an independent episode is not one, and the
        rounding that would make it one always moves in the direction that helps the
        strategy.
        """
        return int(self.n_eff)


def effective_sample_size(return_fractions: Sequence[Decimal]) -> EffectiveSample:
    """`n_eff` for a series of daily attributed returns.

    A series with no variation returns `n_eff == n`: a constant has no autocorrelation
    structure to correct for, and inventing one from a zero denominator would divide by
    nothing.
    """
    observation_count = len(return_fractions)
    if observation_count < MIN_OBSERVATIONS_FOR_EFFECTIVE_SAMPLE:
        raise MetricInputError(
            f"{observation_count} observations is below the "
            f"{MIN_OBSERVATIONS_FOR_EFFECTIVE_SAMPLE} needed to estimate an "
            f"autocorrelation; at this length the significance band admits almost "
            f"nothing and the correction would report independence it never established"
        )

    series = _float_series(return_fractions)
    mean = sum(series) / observation_count
    deviations = tuple(observation - mean for observation in series)
    denominator = sum(deviation * deviation for deviation in deviations)
    if denominator == 0.0:
        return EffectiveSample(
            observation_count=observation_count,
            n_eff=Decimal(observation_count),
            inflation_factor=_ONE,
            significant_lag_count=0,
            autocorrelations=(),
        )

    band = _SIGNIFICANCE_Z / sqrt(observation_count)
    max_lag = max(1, observation_count // _MAX_LAG_FRACTION)
    examined: list[Decimal] = []
    significant_sum = 0.0
    significant_lag_count = 0
    for lag in range(1, max_lag + 1):
        covariance = sum(
            deviations[index] * deviations[index - lag] for index in range(lag, observation_count)
        )
        autocorrelation = covariance / denominator
        examined.append(Decimal(str(autocorrelation)).quantize(_QUANTUM))
        if abs(autocorrelation) <= band:
            break
        significant_sum += autocorrelation
        significant_lag_count += 1

    inflation = max(1.0, 1.0 + 2.0 * significant_sum)
    inflation_factor = Decimal(str(inflation)).quantize(_QUANTUM)
    return EffectiveSample(
        observation_count=observation_count,
        n_eff=(Decimal(observation_count) / inflation_factor).quantize(_QUANTUM),
        inflation_factor=inflation_factor,
        significant_lag_count=significant_lag_count,
        autocorrelations=tuple(examined),
    )
