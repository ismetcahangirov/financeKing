"""The portfolio's risk picture: historical VaR/CVaR, beta to BTC, realised volatility
against target, and concentration over risk contributions.

`docs/rules/decimal-and-money.md` bounds the one `float` exception to statistics in
`fking.backtest` and `fking.data`; `risk` is named a float-free package there and in
`tools/checks/money_types.py`, with no carve-out for this module. Every estimate here is
therefore exact `Decimal` arithmetic over an empirical sample -- no `numpy`, no parametric
distribution, no float anywhere. That is also the substantive point, not just a type rule:
a 3% daily loss against a 12% annualised vol target is a 3.9-sigma day under a Gaussian --
once a decade -- and a few times a year in crypto. A parametric-normal VaR understates risk
by a wide margin exactly where the number gets relied upon, so every estimate here reads
its answer directly off the sorted, realised sample.

**Nothing here re-estimates a covariance.** `compute_risk_metrics` takes the `RiskModel`
the sizing path is using and threads it straight through to `fking.risk.contribution
.portfolio_risk`, and `RiskMetricsSnapshot.risk_model` holds that exact object. A second
estimate with the same parameters is not the same estimate -- different lookback edge,
different shrinkage draw, one tick later -- and the two will drift apart in a way that
first shows up as an unexplained limit breach the metrics did not predict.

Every estimate reports the sample size it was read off. `RISK_PHILOSOPHY.md`'s own words
for this: a 99% VaR from 200 observations is estimated from two data points and must be
reported as such rather than to three decimal places.

Pure throughout: no clock read except through the injected `Clock`, no I/O, no randomness.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime
from decimal import ROUND_CEILING, Decimal, localcontext
from typing import Final

from fking.domain import DomainError
from fking.risk.contribution import PortfolioRisk, portfolio_risk
from fking.risk.covariance import MATH_PRECISION, RiskModel
from fking.risk.exposure import Clock
from fking.risk.sizing import VolatilityEstimate, volatility_used

__all__ = [
    "BetaEstimate",
    "ConcentrationEstimate",
    "RiskMetricsSnapshot",
    "TailRiskEstimate",
    "VolatilityAgainstTargetEstimate",
    "beta_to_market",
    "compute_risk_metrics",
    "concentration_herfindahl",
    "historical_tail_risk",
    "volatility_against_target",
]

_ZERO: Final = Decimal("0")
_ONE: Final = Decimal("1")


@dataclass(frozen=True, slots=True)
class TailRiskEstimate:
    """Empirical VaR and CVaR at one confidence level, read off one realised return series.

    Both are losses, positive when the tail was a loss and negative when even the worst
    observations in the sample were gains -- a book with no losing day at all reports a
    negative VaR, which is the correct and informative answer, not a case to special-case
    away.

    `sample_size` is the whole series; `tail_sample_size` is how many observations the
    CVaR average was actually taken over. The two are kept apart because the interesting
    failure is a `sample_size` that looks reassuring next to a `tail_sample_size` of one.
    """

    confidence_ratio: Decimal
    var_loss_ratio: Decimal
    cvar_loss_ratio: Decimal
    sample_size: int
    tail_sample_size: int


def historical_tail_risk(
    daily_return_ratio_series: Sequence[Decimal], *, confidence_ratio: Decimal
) -> TailRiskEstimate:
    """Historical-simulation VaR and CVaR: sort the sample, read off the tail.

    No distributional assumption anywhere -- see the module docstring. The tail is the
    worst `ceil((1 - confidence_ratio) * n)` observations, rounded *up* so a short series
    always contributes at least one observation to the tail rather than leaving CVaR
    undefined; a floored index would silently drop the tail to zero length below
    `n = 1 / (1 - confidence_ratio)` observations, which is exactly the short-sample case
    this function is obliged to keep reporting on, honestly, via `tail_sample_size`.

    `cvar_loss_ratio >= var_loss_ratio` always. This is not a guard, it is order theory:
    `cvar` is the mean of a set of observations that are each, by construction of the
    sort, at least as bad as the observation at the `VaR` quantile -- see
    `tests/property/test_metrics_properties.py`.
    """
    if not daily_return_ratio_series:
        raise DomainError(
            "no return observations supplied; VaR and CVaR cannot be estimated from an "
            "empty series and must not silently default to zero risk"
        )
    if not (_ZERO < confidence_ratio < _ONE):
        raise DomainError(
            f"confidence_ratio must be strictly between 0 and 1; got {confidence_ratio}"
        )
    for observation in daily_return_ratio_series:
        if not observation.is_finite():
            raise DomainError(f"non-finite return observation {observation}")

    sample_size = len(daily_return_ratio_series)
    sorted_returns = sorted(daily_return_ratio_series)
    tail_fraction = _ONE - confidence_ratio
    with localcontext() as context:
        context.prec = MATH_PRECISION
        raw_tail_size = (tail_fraction * Decimal(sample_size)).to_integral_value(
            rounding=ROUND_CEILING
        )
        tail_sample_size = max(1, min(sample_size, int(raw_tail_size)))
        tail = sorted_returns[:tail_sample_size]
        quantile_return_ratio = sorted_returns[tail_sample_size - 1]
        cvar_mean_return_ratio = sum(tail, start=_ZERO) / Decimal(tail_sample_size)

    return TailRiskEstimate(
        confidence_ratio=confidence_ratio,
        var_loss_ratio=-quantile_return_ratio,
        cvar_loss_ratio=-cvar_mean_return_ratio,
        sample_size=sample_size,
        tail_sample_size=tail_sample_size,
    )


@dataclass(frozen=True, slots=True)
class BetaEstimate:
    """Beta to the market factor (BTC), over the full sample and over a stress window.

    Both windows are computed and both are kept; `beta_used_ratio` is whichever carries
    the larger magnitude. A beta measured in a calm window and then relied on through a
    stress window is the same mistake `fking.risk.covariance` refuses to make for
    correlation: comovement calibrated on the period it was smallest overstates a hedge
    at exactly the moment the hedge is being counted on.
    """

    full_sample_beta_ratio: Decimal
    full_sample_size: int
    stress_window_beta_ratio: Decimal
    stress_window_sample_size: int
    beta_used_ratio: Decimal
    used_stress_window: bool


def beta_to_market(
    *,
    asset_daily_return_ratio_series: Sequence[Decimal],
    market_daily_return_ratio_series: Sequence[Decimal],
    stress_asset_daily_return_ratio_series: Sequence[Decimal],
    stress_market_daily_return_ratio_series: Sequence[Decimal],
) -> BetaEstimate:
    """`beta = Cov(asset, market) / Var(market)`, over both windows, larger magnitude wins.

    The stress window is supplied by the caller rather than discovered here, on the same
    principle as `fking.risk.covariance.estimate_risk_model`'s stress correlation floor:
    defining "stressed" from inside the estimator that is supposed to be checked against
    it is circular, and the caller who already knows which days were the stress window
    for the correlation estimate is the caller who should say so here too.
    """
    full_beta, full_sample_size = _beta_component(
        asset_daily_return_ratio_series,
        market_daily_return_ratio_series,
        window_name="full sample",
    )
    stress_beta, stress_sample_size = _beta_component(
        stress_asset_daily_return_ratio_series,
        stress_market_daily_return_ratio_series,
        window_name="stress window",
    )
    used_stress_window = abs(stress_beta) > abs(full_beta)
    return BetaEstimate(
        full_sample_beta_ratio=full_beta,
        full_sample_size=full_sample_size,
        stress_window_beta_ratio=stress_beta,
        stress_window_sample_size=stress_sample_size,
        beta_used_ratio=stress_beta if used_stress_window else full_beta,
        used_stress_window=used_stress_window,
    )


def _beta_component(
    asset_series: Sequence[Decimal], market_series: Sequence[Decimal], *, window_name: str
) -> tuple[Decimal, int]:
    """One window's beta and the observation count it was estimated from."""
    if len(asset_series) != len(market_series):
        raise DomainError(
            f"{window_name}: asset series has {len(asset_series)} observations, market "
            f"series has {len(market_series)}; a beta between two different days is not "
            f"a number this function can produce and must not be computed anyway"
        )
    if not asset_series:
        raise DomainError(f"{window_name}: no observations to estimate beta from")

    sample_size = len(asset_series)
    with localcontext() as context:
        context.prec = MATH_PRECISION
        # Zero mean, matching fking.risk.covariance's convention: over a short window the
        # sample mean of a daily return is dominated by volatility, so subtracting it
        # removes more signal than bias.
        covariance = sum(
            (asset * market for asset, market in zip(asset_series, market_series, strict=True)),
            start=_ZERO,
        ) / Decimal(sample_size)
        market_variance = sum((market * market for market in market_series), start=_ZERO) / Decimal(
            sample_size
        )
        if market_variance <= _ZERO:
            raise DomainError(
                f"{window_name}: BTC carries zero variance over {sample_size} "
                f"observations; a market factor that has not moved cannot anchor a beta"
            )
        beta = covariance / market_variance
    return beta, sample_size


@dataclass(frozen=True, slots=True)
class VolatilityAgainstTargetEstimate:
    """Realised volatility, annualised, against the target the sizing engine aims at.

    `realised` is `fking.risk.sizing.volatility_used`'s own estimate -- the same
    `max(EWMA, 60-day, floor)` construction the position sizer applies to this same
    return series, not a second estimator with its own edge cases and its own way of
    quietly disagreeing with the number that actually sized the book.
    """

    realised: VolatilityEstimate
    target_annualised_ratio: Decimal
    realised_to_target_ratio: Decimal
    sample_size: int


def volatility_against_target(
    daily_return_ratio_series: Sequence[Decimal],
    *,
    volatility_floor_annualised_ratio: Decimal,
    target_annualised_ratio: Decimal,
) -> VolatilityAgainstTargetEstimate:
    """How the book's realised volatility compares with the target it is sized against.

    A ratio above one means the book is running hotter than the target it was sized to;
    below one, cooler. Both are informative -- consistently well below one is capacity
    left on the table, not merely a metric sitting inside its band.
    """
    if not daily_return_ratio_series:
        raise DomainError("no return observations supplied; realised volatility needs at least one")
    if target_annualised_ratio <= _ZERO:
        raise DomainError(
            f"target_annualised_ratio is {target_annualised_ratio}; a volatility target "
            f"must be positive or the ratio to it is undefined"
        )
    realised = volatility_used(
        daily_return_ratio_series, floor_annualised=volatility_floor_annualised_ratio
    )
    return VolatilityAgainstTargetEstimate(
        realised=realised,
        target_annualised_ratio=target_annualised_ratio,
        realised_to_target_ratio=realised.used_annualised / target_annualised_ratio,
        sample_size=len(daily_return_ratio_series),
    )


@dataclass(frozen=True, slots=True)
class ConcentrationEstimate:
    """Herfindahl index over the book's *cluster* risk shares.

    Computed at the cluster level, not the symbol level. Five symbols at equal notional
    and equal pairwise correlation carry equal per-symbol risk shares by symmetry no
    matter how correlated they are with each other, so a per-symbol Herfindahl is blind
    to exactly the concentration `fking.risk.contribution`'s cluster limit exists to
    catch -- five strategies inside one correlation cluster reading as five diversified
    20% positions. Merging clusters is what this index has to move on, and it does,
    because fewer buckets holding the same total share is a textbook Herfindahl increase.

    Normalised by the sum of cluster-share *magnitudes*, not by `sigma_p` directly, and
    that choice is load-bearing rather than cosmetic. The signed cluster shares already
    sum to exactly one (`fking.risk.contribution`'s partition identity), which lower-bounds
    the sum of their magnitudes at one by the triangle inequality and guarantees
    `herfindahl_ratio` sits in `[1/k, 1]` for `k` held clusters regardless of how much
    internal hedging the book carries. A raw `|CTR_i| / sigma_p` share has no such bound:
    two offsetting positions can each carry a marginal contribution larger than the
    portfolio's own volatility while their signed sum still nets to `sigma_p` exactly, and
    an unbounded index is meaningless on precisely the hedged books worth watching most.

    `sample_size` here counts the clusters actually carrying weight, not a statistical
    observation count -- concentration is an exact decomposition of the current book, not
    an estimate with sampling error, and this field exists so every estimate in
    `RiskMetricsSnapshot` exposes the same shape.
    """

    herfindahl_ratio: Decimal
    sample_size: int


def concentration_herfindahl(portfolio: PortfolioRisk) -> ConcentrationEstimate:
    magnitudes = tuple(
        abs(share) for share in portfolio.cluster_risk_share_ratio.values() if share != _ZERO
    )
    held_cluster_count = len(magnitudes)
    if not magnitudes:
        return ConcentrationEstimate(herfindahl_ratio=_ZERO, sample_size=0)

    with localcontext() as context:
        context.prec = MATH_PRECISION
        total_magnitude = sum(magnitudes, start=_ZERO)
        herfindahl_ratio = sum(
            ((magnitude / total_magnitude) ** 2 for magnitude in magnitudes), start=_ZERO
        )
    return ConcentrationEstimate(herfindahl_ratio=herfindahl_ratio, sample_size=held_cluster_count)


@dataclass(frozen=True, slots=True)
class RiskMetricsSnapshot:
    """The portfolio's whole risk picture at one instant, against one risk model.

    `risk_model` is held by reference, never recomputed and never copied: every term here
    is a summary of the *same* `Sigma` the sizing path used to size the book, per the
    identity constraint issue #56 states explicitly. See the module docstring for what
    goes wrong when a caller substitutes a freshly estimated copy instead.
    """

    computed_at_utc: datetime
    risk_model: RiskModel
    portfolio_risk: PortfolioRisk
    tail_risk: TailRiskEstimate
    beta_to_btc: BetaEstimate
    rolling_volatility: VolatilityAgainstTargetEstimate
    concentration: ConcentrationEstimate


def compute_risk_metrics(  # noqa: PLR0913 - composes four independently-developed terms
    # (tail risk, beta, realised volatility, concentration), each with its own inputs; see
    # RiskEngine.decide()'s identical justification in fking.risk.engine.
    *,
    weight_ratio_by_symbol: Mapping[str, Decimal],
    model: RiskModel,
    portfolio_daily_return_ratio_series: Sequence[Decimal],
    confidence_ratio: Decimal,
    asset_daily_return_ratio_series: Sequence[Decimal],
    market_daily_return_ratio_series: Sequence[Decimal],
    stress_asset_daily_return_ratio_series: Sequence[Decimal],
    stress_market_daily_return_ratio_series: Sequence[Decimal],
    volatility_floor_annualised_ratio: Decimal,
    target_volatility_annualised_ratio: Decimal,
    clock: Clock,
) -> RiskMetricsSnapshot:
    """Assemble the whole risk picture. Every term above is pure; this only composes them.

    `model` passes straight through to `fking.risk.contribution.portfolio_risk` and is
    stored on the result by reference -- nothing in this function estimates a covariance.
    `portfolio_daily_return_ratio_series` is the book's own historical-simulation series
    (today's weights applied to each day's realised returns), which is what both the VaR
    and the realised-volatility terms below read.
    """
    computed_at_utc = clock()
    portfolio = portfolio_risk(weight_ratio_by_symbol=weight_ratio_by_symbol, model=model)
    return RiskMetricsSnapshot(
        computed_at_utc=computed_at_utc,
        risk_model=model,
        portfolio_risk=portfolio,
        tail_risk=historical_tail_risk(
            portfolio_daily_return_ratio_series, confidence_ratio=confidence_ratio
        ),
        beta_to_btc=beta_to_market(
            asset_daily_return_ratio_series=asset_daily_return_ratio_series,
            market_daily_return_ratio_series=market_daily_return_ratio_series,
            stress_asset_daily_return_ratio_series=stress_asset_daily_return_ratio_series,
            stress_market_daily_return_ratio_series=stress_market_daily_return_ratio_series,
        ),
        rolling_volatility=volatility_against_target(
            portfolio_daily_return_ratio_series,
            volatility_floor_annualised_ratio=volatility_floor_annualised_ratio,
            target_annualised_ratio=target_volatility_annualised_ratio,
        ),
        concentration=concentration_herfindahl(portfolio),
    )
