"""Economics, risk and path statistics, all computed from a daily return series.

Every function here takes a `Sequence[Decimal]` of daily returns and nothing else. That
signature is the enforcement behind issue #38's first acceptance criterion: the trade
count is not an argument, so it cannot enter a Sharpe, a Sortino, a Calmar or an ulcer
index by accident. Two strategies with identical daily equity curves and a tenfold
difference in trade count get identical numbers because there is no channel through
which they could differ.

**Undefined is reported as `None`, never as zero.** A regime bucket in which the
strategy never traded has zero return variation, so its Sharpe has a zero denominator; a
window with no losing day has no downside deviation, so its Sortino is unbounded; a
window with no drawdown has no Calmar. Substituting zero in any of those reads as "no
edge" and substituting a large number reads as "excellent", and both are inventions. The
`None` says the ratio does not exist for this window, which is the true statement.

**The risk-free rate is zero and is not a parameter.** A demo account holds no
interest-bearing collateral, so a non-zero rate would be a number with no source -- and
`docs/rules/naming.md` is explicit that a magic constant in risk-adjacent code with
no provenance gets "cleaned up" later by someone who does not know what it protected.

`float` inside this module is the statistical exception in
`docs/rules/decimal-and-money.md`, bounded by `_float_series`. Every money-of-record
quantity -- equity, realised PnL, fees, funding -- stays `Decimal` from the fill that
produced it to the report that publishes it, and never passes through here at all.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from decimal import Decimal
from math import sqrt
from typing import Final

from fking.backtest.portfolio._errors import MetricInputError
from fking.backtest.portfolio._grid import ANNUALISATION_DAYS
from fking.backtest.portfolio._sample import (
    MIN_OBSERVATIONS_FOR_EFFECTIVE_SAMPLE,
    EffectiveSample,
    effective_sample_size,
)

_ONE: Final = Decimal("1")
_HUNDRED: Final = Decimal("100")
_QUANTUM: Final = Decimal("0.000001")

# Two boundaries are the minimum an `EquityPath` accepts, so one daily return is the
# minimum a series can hold; a sample standard deviation needs two.
MIN_OBSERVATIONS_FOR_DISPERSION: Final[int] = 2


def _float_series(return_fractions: Sequence[Decimal]) -> tuple[float, ...]:
    """The one place `Decimal` becomes `float` in this module."""
    return tuple(float(return_fraction) for return_fraction in return_fractions)


def _statistic(number: float) -> Decimal:
    """The way back out: `float` becomes `Decimal` at a named boundary, never inline."""
    return Decimal(str(number)).quantize(_QUANTUM)


def _ratio_or_none(numerator: float, denominator: float) -> Decimal | None:
    """A ratio, or `None` when its denominator does not exist for this window."""
    if denominator <= 0.0:
        return None
    return _statistic(numerator / denominator)


def _require_dispersion_sample(return_fractions: Sequence[Decimal]) -> tuple[float, ...]:
    if len(return_fractions) < MIN_OBSERVATIONS_FOR_DISPERSION:
        raise MetricInputError(
            f"{len(return_fractions)} daily returns cannot support a dispersion "
            f"estimate; at least {MIN_OBSERVATIONS_FOR_DISPERSION} are needed"
        )
    return _float_series(return_fractions)


@dataclass(frozen=True, slots=True)
class PathEconomics:
    """What the equity path earned, in units a reader can compare across windows.

    Computable from any subsequence of daily returns, which is what lets a regime bucket
    carry the same fields as the whole run. The money-of-record totals live in
    `LedgerTotals` instead, because a realised PnL cannot be attributed to a regime from
    the equity path alone.
    """

    total_return_fraction: Decimal
    annualised_return_fraction: Decimal
    observation_count: int


@dataclass(frozen=True, slots=True)
class RiskProfile:
    """Dispersion and drawdown, annualised on the fixed daily grid.

    `ulcer_index_pct` is the root-mean-square drawdown across the window, in percent. It
    is reported alongside the maximum because the maximum is a single day's reading: two
    paths can share a 20% worst drawdown while one spent a week there and the other
    fourteen months, and only the ulcer index separates them.
    """

    annualised_volatility_fraction: Decimal
    downside_deviation_fraction: Decimal
    max_drawdown_fraction: Decimal
    ulcer_index_pct: Decimal
    worst_daily_return_fraction: Decimal


@dataclass(frozen=True, slots=True)
class PathStatistics:
    """The risk-adjusted ratios, their moments, and the sample they rest on.

    `effective_sample` is `None` when the window is too short to estimate an
    autocorrelation, and `sharpe_t_statistic` is then `None` too. The raw observation
    count is never substituted: an overlapping-position strategy that reported its
    t-statistic against `n` rather than `n_eff` would overstate it by the square root of
    the overlap, which is the whole failure the effective sample exists to prevent.
    """

    sharpe_ratio: Decimal | None
    sortino_ratio: Decimal | None
    calmar_ratio: Decimal | None
    skewness: Decimal | None
    kurtosis: Decimal | None
    effective_sample: EffectiveSample | None
    sharpe_t_statistic: Decimal | None


def effective_sample_or_none(return_fractions: Sequence[Decimal]) -> EffectiveSample | None:
    """`n_eff` for the window, or `None` when the window is too short to estimate it.

    `None` rather than a fallback to `n`. A short regime bucket is reported and flagged
    (`RegimeCoverage.THIN`), never silently credited with independence nobody measured.
    """
    if len(return_fractions) < MIN_OBSERVATIONS_FOR_EFFECTIVE_SAMPLE:
        return None
    return effective_sample_size(return_fractions)


def path_economics(return_fractions: Sequence[Decimal]) -> PathEconomics:
    """Compounded and annualised return over the window.

    The compounding is exact `Decimal`; only the fractional power that annualises it
    crosses into `float`, and it crosses back immediately.
    """
    if not return_fractions:
        raise MetricInputError("path economics needs at least one daily return")
    growth = _ONE
    for return_fraction in return_fractions:
        growth *= _ONE + return_fraction
    observation_count = len(return_fractions)
    annualised = float(growth) ** (ANNUALISATION_DAYS / observation_count) - 1.0
    return PathEconomics(
        total_return_fraction=(growth - _ONE).quantize(_QUANTUM),
        annualised_return_fraction=_statistic(annualised),
        observation_count=observation_count,
    )


def risk_profile(return_fractions: Sequence[Decimal]) -> RiskProfile:
    """Volatility, downside deviation, drawdown and the ulcer index for the window.

    Drawdown is measured on the wealth index the window's own returns compound to, which
    is what makes a regime bucket's drawdown answerable: the alternative -- slicing the
    whole run's drawdown series by regime -- attributes to a bucket a peak that was set
    in another one.
    """
    series = _require_dispersion_sample(return_fractions)
    observation_count = len(series)
    mean = sum(series) / observation_count
    variance = sum((observation - mean) ** 2 for observation in series) / (observation_count - 1)
    # Only the losing days enter the downside denominator, but every day enters its
    # divisor. Dividing by the count of losing days instead would reward a strategy for
    # having few of them twice: once in the numerator and once in the denominator.
    downside_variance = sum(min(observation, 0.0) ** 2 for observation in series) / (
        observation_count - 1
    )
    annualiser = sqrt(ANNUALISATION_DAYS)

    wealth = 1.0
    peak = 1.0
    drawdowns: list[float] = []
    for observation in series:
        wealth *= 1.0 + observation
        peak = max(peak, wealth)
        drawdowns.append(1.0 - wealth / peak)
    ulcer = sqrt(sum(drawdown**2 for drawdown in drawdowns) / observation_count)

    return RiskProfile(
        annualised_volatility_fraction=_statistic(sqrt(variance) * annualiser),
        downside_deviation_fraction=_statistic(sqrt(downside_variance) * annualiser),
        max_drawdown_fraction=_statistic(max(drawdowns)),
        ulcer_index_pct=(_statistic(ulcer) * _HUNDRED).quantize(_QUANTUM),
        worst_daily_return_fraction=min(return_fractions).quantize(_QUANTUM),
    )


def path_statistics(
    return_fractions: Sequence[Decimal], *, risk: RiskProfile, economics: PathEconomics
) -> PathStatistics:
    """The risk-adjusted ratios and the moments that qualify them.

    Skewness and kurtosis are the population third and fourth standardised moments, with
    kurtosis on the convention where a normal distribution reads 3 -- the form
    `fking.backtest.validation.deflated_sharpe_ratio` consumes. Getting that convention
    wrong shifts the deflation term by `1/4 * SR^2` in the flattering direction.
    """
    series = _require_dispersion_sample(return_fractions)
    observation_count = len(series)
    mean = sum(series) / observation_count
    deviations = tuple(observation - mean for observation in series)
    sample_variance = sum(deviation**2 for deviation in deviations) / (observation_count - 1)
    sample_deviation = sqrt(sample_variance)

    second_moment = sum(deviation**2 for deviation in deviations) / observation_count
    if second_moment > 0.0:
        third_moment = sum(deviation**3 for deviation in deviations) / observation_count
        fourth_moment = sum(deviation**4 for deviation in deviations) / observation_count
        skewness: Decimal | None = _statistic(third_moment / second_moment**1.5)
        kurtosis: Decimal | None = _statistic(fourth_moment / second_moment**2)
    else:
        skewness = None
        kurtosis = None

    annualiser = sqrt(ANNUALISATION_DAYS)
    sharpe = _ratio_or_none(mean * annualiser, sample_deviation)
    sample = effective_sample_or_none(return_fractions)
    t_statistic = (
        None
        if sharpe is None or sample is None
        else _ratio_or_none(mean * sqrt(float(sample.n_eff)), sample_deviation)
    )

    return PathStatistics(
        sharpe_ratio=sharpe,
        sortino_ratio=_ratio_or_none(
            mean * ANNUALISATION_DAYS, float(risk.downside_deviation_fraction)
        ),
        calmar_ratio=_ratio_or_none(
            float(economics.annualised_return_fraction), float(risk.max_drawdown_fraction)
        ),
        skewness=skewness,
        kurtosis=kurtosis,
        effective_sample=sample,
        sharpe_t_statistic=t_statistic,
    )
