"""Effective sample size: how many of the daily returns are actually independent draws.

    n_eff = n / (1 + 2 * sum_(k=1..K) rho_k)

`rho_k` is the lag-`k` autocorrelation of the daily attributed-return series and `K` is
the last lag before the first statistically insignificant one. For a strategy holding
five-day positions the daily series is a moving average of five overlapping shocks, so
`rho_k = (5 - k) / 5` for `k = 1..4`, the sum is exactly 2, the denominator is 5 and
`n_eff` is `n / 5`. That is not a coincidence of that example -- it is the correction
recovering the number of non-overlapping holding periods the sample actually contains.

Skipping it is how overlapping-position strategies manufacture apparent significance, and
the mutation operators in P6 produce overlapping-position designs by default
(`SCORING_ENGINE.md` section 4). A t-statistic computed on `n` instead of `n_eff` for a
five-day hold is overstated by `sqrt(5)`, and nothing about the result looks unusual.

**`n_eff` is capped at `n`.** Negative autocorrelation drives the denominator below one,
and the uncapped expression then reports more independent draws than there are
observations -- at `sum(rho) = -0.4` it claims `5n`. That is arithmetic, not information:
mean-reversion in the return series does not create data. The cap is applied silently
because the uncapped figure has no defensible reading, and the alternative -- refusing --
would reject a legitimately mean-reverting strategy for being mean-reverting.

`float` here is the statistical exception in `.claude/rules/decimal-and-money.md`, bounded
to `_float_return_fractions`: `Decimal` in, `Decimal(str(...))` out, never implicitly
mid-expression. An autocorrelation is an estimate whose sampling error is around
`1/sqrt(n)`, twelve orders of magnitude above anything `2**-53` contributes.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from decimal import ROUND_HALF_EVEN, Decimal
from math import floor, sqrt
from typing import Final

from fking.backtest.accounting._curve import EquityCurve
from fking.backtest.accounting._errors import EffectiveSampleError

# Below this the autocorrelation estimates are noise: the 95% band is +/- 1.96/sqrt(n),
# which at n = 7 is +/- 0.74 -- wider than almost any real autocorrelation, so the
# significance test admits nothing and the correction silently does not happen. Refusing
# is the honest answer; a window this short is not evidence about dependence either way.
MIN_OBSERVATIONS_FOR_AUTOCORRELATION: Final[int] = 8

# Two-sided 95% normal quantile. The Bartlett band for the null of white noise is
# +/- z / sqrt(n); it is an approximation, and it is the standard one, which matters more
# than precision here because the alternative is a threshold nobody can reproduce.
_NORMAL_QUANTILE_95: Final[float] = 1.959963984540054

# Autocorrelation at lag k is estimated from n - k pairs, so the estimate degrades exactly
# as the lag grows. n // 4 is the conventional ceiling; past it the estimate is drawn from
# fewer than three quarters of the sample and contributes more variance than signal.
_MAX_LAG_DIVISOR: Final[int] = 4

_SAMPLE_QUANTUM: Final = Decimal("0.0001")
_ONE: Final = Decimal("1")


@dataclass(frozen=True, slots=True)
class EffectiveSample:
    """What a daily return series is worth as evidence, and how that was arrived at.

    `autocorrelation_by_lag` is kept rather than only the resulting count, because the
    two questions a reader asks next -- "is the dependence one long tail or one big lag"
    and "did the search hit the lag ceiling" -- are answerable from the sequence and
    unanswerable from the total.
    """

    observation_count: int
    effective_observation_count: Decimal
    autocorrelation_by_lag: tuple[Decimal, ...]
    reached_lag_ceiling: bool

    @property
    def significant_lag_count(self) -> int:
        """How many leading lags were significant, and therefore summed."""
        return len(self.autocorrelation_by_lag)

    @property
    def independent_episode_count(self) -> int:
        """`n_eff` floored to an integer, for `SharpeEvidence.independent_episode_count`.

        The only sanctioned way to fill that field from an equity curve. Floored rather
        than rounded: rounding up hands the deflated Sharpe half an observation the
        sample does not contain, and the correction is the one place a rounding
        convention should lean against the result.
        """
        return max(2, floor(self.effective_observation_count))


@dataclass(frozen=True, slots=True)
class _FloatReturns:
    """The one place `Decimal` becomes `float` in this module."""

    return_fractions: tuple[float, ...]
    observation_count: int


def _float_return_fractions(return_fractions: Sequence[Decimal]) -> _FloatReturns:
    return _FloatReturns(
        return_fractions=tuple(float(fraction) for fraction in return_fractions),
        observation_count=len(return_fractions),
    )


def _autocorrelation_by_lag(inputs: _FloatReturns, *, max_lag: int) -> list[float]:
    """Lag-1..`max_lag` sample autocorrelations, on the biased (divide-by-n) estimator.

    The biased form is deliberate: it is what makes the estimated autocorrelation
    function positive semi-definite, so the summed denominator cannot come out negative
    from an estimation artefact alone.
    """
    mean_return = sum(inputs.return_fractions) / inputs.observation_count
    deviations = [fraction - mean_return for fraction in inputs.return_fractions]
    total_square = sum(deviation * deviation for deviation in deviations)
    if total_square <= 0.0:
        raise EffectiveSampleError(
            "the daily return series has zero variance, so its autocorrelation is 0/0. "
            "A constant return series has no Sharpe to deflate either; check "
            "time_in_market_pct before asking for an effective sample."
        )
    return [
        sum(
            deviations[index] * deviations[index - lag]
            for index in range(lag, inputs.observation_count)
        )
        / total_square
        for lag in range(1, max_lag + 1)
    ]


def effective_sample(curve: EquityCurve) -> EffectiveSample:
    """The effective sample size of `curve`'s daily return series.

    Lags are summed from 1 until the first one whose estimate falls inside the
    white-noise band. Stopping at the first insignificant lag rather than summing every
    lag up to the ceiling is what keeps the denominator from accumulating noise: past the
    true dependence horizon each additional term is a draw from a distribution centred on
    zero, and summing forty of them moves `n_eff` by more than the real correction does.
    """
    return_fractions = curve.daily_return_fractions
    if len(return_fractions) < MIN_OBSERVATIONS_FOR_AUTOCORRELATION:
        raise EffectiveSampleError(
            f"{len(return_fractions)} daily returns is below the "
            f"{MIN_OBSERVATIONS_FOR_AUTOCORRELATION} needed to estimate autocorrelation; "
            f"reporting the raw count instead would hand back the very number the "
            f"correction exists to replace"
        )

    inputs = _float_return_fractions(return_fractions)
    max_lag = max(1, inputs.observation_count // _MAX_LAG_DIVISOR)
    estimates = _autocorrelation_by_lag(inputs, max_lag=max_lag)
    band = _NORMAL_QUANTILE_95 / sqrt(inputs.observation_count)

    significant: list[float] = []
    for estimate in estimates:
        if abs(estimate) <= band:
            break
        significant.append(estimate)

    denominator = 1.0 + 2.0 * sum(significant)
    observation_count = inputs.observation_count
    if denominator <= 1.0:
        # Mean reversion in the return series does not create data. See the module
        # docstring: the uncapped expression reports more independent draws than there
        # are observations, and that figure has no reading.
        effective = float(observation_count)
    else:
        effective = observation_count / denominator

    return EffectiveSample(
        observation_count=observation_count,
        effective_observation_count=max(
            _ONE, Decimal(str(effective)).quantize(_SAMPLE_QUANTUM, rounding=ROUND_HALF_EVEN)
        ),
        autocorrelation_by_lag=tuple(
            Decimal(str(estimate)).quantize(_SAMPLE_QUANTUM, rounding=ROUND_HALF_EVEN)
            for estimate in significant
        ),
        reached_lag_ceiling=len(significant) == max_lag,
    )
