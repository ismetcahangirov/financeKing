"""The deflated Sharpe ratio, and the evidence record that makes it impossible to omit.

Three properties carry this module.

**A Sharpe cannot be expressed without its trial count.** `SharpeEvidence` has no default
for `trials_at_time_of_run`, so the field cannot be dropped by omission -- constructing
the record without it is a `ValidationError`, not a value of zero
(`BACKTEST_ENGINE.md` section 6.5). That is the whole of the defence: every other clause
here is arithmetic that a caller could in principle reproduce, but a trial count that was
never carried alongside the number cannot be recovered later at all.

**The correction grows slowly, which is exactly why the counter is allowed to drift.**
`SR*` -- the expected maximum Sharpe under the null of zero edge -- grows roughly as
`sqrt(2 ln K)`. Going from `K = 100` to `K = 10,000` raises the bar by
`sqrt(ln 10000 / ln 100) = sqrt(2)`, about 1.41x. A hundredfold increase in search effort
raises the threshold by 41%, which feels survivable and is not: the benchmark never comes
back down and it applies to every strategy in the population at once. With three years of
daily observations and `K = 10,000`, a Sharpe near 2.5 is producible by strategies with
zero edge, and every backtesting library on earth will print that 2.5 without mentioning
`K`.

**Skew and kurtosis sit in the denominator, and the sign is the point.** Negative skew
raises the denominator and lowers the deflated figure; excess kurtosis does the same. A
strategy that earns steadily and loses catastrophically has precisely that moment
signature, so the statistical correction and the economic objection agree. The raw Sharpe
does the opposite -- it rewards the shape. Crypto returns are strongly fat-tailed, so
omitting kurtosis inflates the deflated figure *specifically* for the strategies most
likely to blow up (`BACKTEST_ENGINE.md` section 6.5).

`float` inside this module is the statistical exception in
`.claude/rules/decimal-and-money.md`: these are estimates whose sampling error is many
orders of magnitude larger than 2^-53. The exception is bounded by `_float_inputs`, the
one named conversion boundary in this package -- `Decimal` in on the way down,
`Decimal(str(...))` out on the way back, never implicitly mid-expression. Everything this
module publishes is `Decimal`, so nothing crosses a module boundary as a `float`.
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
from math import e, sqrt
from statistics import NormalDist
from typing import Annotated, Final

from pydantic import BaseModel, ConfigDict, Field

from fking.backtest.validation._errors import (
    SharpeEvidenceUnusableError,
    TrialCountUnavailableError,
)

# Euler-Mascheroni constant. It appears in the expected value of the maximum of N
# independent Gaussians; Bailey & Lopez de Prado (2014), "The Deflated Sharpe Ratio",
# eq. 5. Named rather than inlined because a bare 0.577 in this file would eventually be
# "simplified" by someone who did not know what it was.
_EULER_MASCHERONI: Final[float] = 0.5772156649015329

# A statistic, not money: bounded to the interval a probability lives in, and rendered at
# enough digits that two deflated Sharpes near the floor are distinguishable.
_PROBABILITY_QUANTUM: Final = Decimal("0.000000000001")

_ZERO: Final = Decimal("0")


# A dimensionless statistic that may take either sign. `allow_inf_nan=False` is the
# load-bearing half: a NaN skewness propagates through the deflation arithmetic without
# raising and compares unequal to itself, so it would surface as a deflated Sharpe of NaN
# that fails every threshold comparison silently rather than loudly.
SignedStatistic = Annotated[Decimal, Field(allow_inf_nan=False)]
# Sharpe dispersion across the trials that were searched. Strictly positive: a zero
# variance claims every configuration in the search scored identically, which makes the
# expected maximum equal to the mean and deflates by nothing.
PositiveVariance = Annotated[Decimal, Field(gt=0, allow_inf_nan=False)]


class SharpeEvidence(BaseModel):
    """One observed Sharpe together with everything needed to discount it.

    Every field is required. In particular `trials_at_time_of_run` has no default, so a
    Sharpe cannot be recorded without the count of configurations that were searched to
    find it -- `BACKTEST_ENGINE.md` section 6.5 states that as a schema requirement
    rather than a convention precisely because the omission is invisible afterwards.

    `observed_sharpe` and `independent_episode_count` are both per-episode quantities.
    An episode is an independent occurrence of the phenomenon the strategy trades, never
    a bar: 41,208 hourly bars containing 37 distinct funding-extremity episodes is a
    sample of 37, and treating it as 41,208 overstates the t-statistic by a factor near
    33 (`.claude/rules/overfitting-defences.md` clause 7).
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    observed_sharpe: SignedStatistic
    trials_at_time_of_run: Annotated[int, Field(ge=1)]
    independent_episode_count: Annotated[int, Field(ge=2)]
    skewness: SignedStatistic
    kurtosis: SignedStatistic
    sharpe_variance_across_trials: PositiveVariance


@dataclass(frozen=True, slots=True)
class _FloatInputs:
    """The one place `Decimal` becomes `float` in this package."""

    observed_sharpe: float
    skewness: float
    kurtosis: float
    sharpe_variance_across_trials: float


def _float_inputs(evidence: SharpeEvidence) -> _FloatInputs:
    return _FloatInputs(
        observed_sharpe=float(evidence.observed_sharpe),
        skewness=float(evidence.skewness),
        kurtosis=float(evidence.kurtosis),
        sharpe_variance_across_trials=float(evidence.sharpe_variance_across_trials),
    )


def expected_max_sharpe(trial_count: int, sharpe_variance_across_trials: Decimal) -> Decimal:
    """`SR*`: the Sharpe the best of `trial_count` zero-edge configurations would show.

    A single trial returns zero. That is not a special case bolted on -- with one
    configuration no selection took place, so the null the observed Sharpe must beat is
    the null Sharpe itself, and the maximum-of-N expression is undefined below two
    anyway. It is also the property that makes the gate's dependence on the trial count
    demonstrable: the same strategy, the same returns, one trial instead of five
    hundred, and the verdict flips.

    Zero or a negative count is a refusal rather than a benchmark of zero, because that
    is what an unreadable ledger returns.
    """
    if trial_count < 1:
        raise TrialCountUnavailableError(
            f"trial count is {trial_count}; refusing to deflate against an unreadable "
            f"ledger, because a benchmark of zero reports a searched result as an "
            f"unsearched one"
        )
    if sharpe_variance_across_trials <= _ZERO:
        raise SharpeEvidenceUnusableError(
            f"sharpe_variance_across_trials is {sharpe_variance_across_trials}; the "
            f"expected maximum of a degenerate distribution is its mean, which deflates "
            f"by nothing"
        )
    if trial_count == 1:
        return _ZERO

    normal = NormalDist()
    dispersion = sqrt(float(sharpe_variance_across_trials))
    upper_quantile = normal.inv_cdf(1.0 - 1.0 / trial_count)
    lower_quantile = normal.inv_cdf(1.0 - 1.0 / (trial_count * e))
    benchmark = dispersion * (
        (1.0 - _EULER_MASCHERONI) * upper_quantile + _EULER_MASCHERONI * lower_quantile
    )
    return Decimal(str(benchmark))


def deflated_sharpe_ratio(evidence: SharpeEvidence) -> Decimal:
    """The probability the observed Sharpe reflects skill rather than the search.

    Bailey & Lopez de Prado (2014), eq. 9: the observed Sharpe is reduced by `SR*`,
    scaled by the effective sample, and divided by a term that grows as the return
    distribution becomes more adverse.

    Returns a probability in [0, 1]. A value below the gate's floor means the observed
    Sharpe is within what the search alone would have produced.
    """
    benchmark = expected_max_sharpe(
        evidence.trials_at_time_of_run, evidence.sharpe_variance_across_trials
    )
    inputs = _float_inputs(evidence)

    # Negative skew and fat tails both enlarge this, which lowers the result. The
    # statistical correction and the economic objection point the same way here, and
    # that agreement is the reason the moments are in the formula at all.
    variance_term = (
        1.0
        - inputs.skewness * inputs.observed_sharpe
        + ((inputs.kurtosis - 1.0) / 4.0) * inputs.observed_sharpe**2
    )
    if variance_term <= 0.0:
        raise SharpeEvidenceUnusableError(
            f"the deflation variance term is {variance_term:.6f} for skewness "
            f"{evidence.skewness} and kurtosis {evidence.kurtosis} at Sharpe "
            f"{evidence.observed_sharpe}; the statistic is undefined, not weak"
        )

    statistic = (
        (inputs.observed_sharpe - float(benchmark))
        * sqrt(evidence.independent_episode_count - 1)
        / sqrt(variance_term)
    )
    return Decimal(str(NormalDist().cdf(statistic))).quantize(_PROBABILITY_QUANTUM)
