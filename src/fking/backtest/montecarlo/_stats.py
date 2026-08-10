"""Percentile and dispersion helpers shared by the resampling methods in this package.

Deliberately not imported from `fking.backtest.cpcv._distribution`, which computes the
same percentile arithmetic: that module is private to its package
(`docs/rules/module-boundaries.md`), and the trade made here is the one already made
between `fking.data`, `fking.platform.scheduler` and `fking.backtest._guards` -- a few
short functions duplicated in each package that needs them, rather than a promoted shared
module that would need to speak both packages' vocabulary at once.

`float` is used internally, under the statistical exception in
`docs/rules/decimal-and-money.md`: these are estimates over a resampled distribution, not
money. `Decimal` in, `Decimal(str(...))` out, at the two named boundaries below.
"""

from __future__ import annotations

from collections.abc import Sequence
from decimal import Decimal
from math import sqrt
from statistics import NormalDist
from typing import Final

from fking.backtest.montecarlo._errors import MonteCarloConfigError

# Twelve places, matching `fking.backtest.cpcv._distribution` and
# `fking.backtest.validation._deflated`: enough that two nearby percentiles over a
# several-hundred-path distribution are legible, and far beyond any threshold this
# project compares against.
STATISTIC_QUANTUM: Final = Decimal("0.000000000001")

_ZERO: Final = Decimal("0")

# Two observations is the arithmetic floor for an `n - 1` sample dispersion estimate.
_MIN_OBSERVATIONS_FOR_DISPERSION: Final = 2


def percentile(ordered: Sequence[Decimal], fraction: Decimal) -> Decimal:
    """Linear interpolation between the two neighbouring order statistics of a sorted
    sequence.

    The whole computation is exact `Decimal` up to one final quantisation, so the same
    paths in the same order produce the same percentile on every machine. The index
    arithmetic uses `(n - 1) * fraction`, which puts p00 on the minimum and p100 on the
    maximum rather than off the end of the sample.
    """
    if not ordered:
        raise MonteCarloConfigError("percentile needs at least one observation")
    position = (Decimal(len(ordered) - 1) * fraction).quantize(STATISTIC_QUANTUM)
    lower_index = int(position)
    upper_index = min(lower_index + 1, len(ordered) - 1)
    weight = position - Decimal(lower_index)
    lower = ordered[lower_index]
    upper = ordered[upper_index]
    return (lower + (upper - lower) * weight).quantize(STATISTIC_QUANTUM)


def mean(values: Sequence[Decimal]) -> Decimal:
    """The exact `Decimal` mean of a non-empty sequence."""
    if not values:
        raise MonteCarloConfigError("mean needs at least one observation")
    return (sum(values, _ZERO) / Decimal(len(values))).quantize(STATISTIC_QUANTUM)


def sample_standard_deviation(values: Sequence[Decimal]) -> float:
    """The `n - 1` sample standard deviation, computed in `float`.

    Needs at least two observations -- a one-path sample has no dispersion to estimate,
    and `0.0` would read as "measured, and zero" rather than "not measured".
    """
    if len(values) < _MIN_OBSERVATIONS_FOR_DISPERSION:
        raise MonteCarloConfigError(
            f"sample standard deviation needs at least two observations; got {len(values)}"
        )
    series = tuple(float(value) for value in values)
    series_mean = sum(series) / len(series)
    variance = sum((observation - series_mean) ** 2 for observation in series) / (len(series) - 1)
    return sqrt(variance)


def two_sided_z_score(confidence: Decimal) -> float:
    """The z-score for a two-sided confidence interval at the given confidence level."""
    if confidence <= _ZERO or confidence >= Decimal("1"):
        raise MonteCarloConfigError(f"confidence must be in (0, 1); got {confidence}")
    tail = (Decimal("1") - confidence) / Decimal("2")
    return NormalDist().inv_cdf(1.0 - float(tail))
