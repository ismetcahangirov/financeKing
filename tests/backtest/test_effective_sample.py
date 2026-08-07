"""`n_eff`, and the guarantee that the raw observation count never stands in for it.

The construction in the first test is the canonical one: a strategy holding a five-day
position earns, each day, the average of the last five independent shocks. Consecutive
returns then share four of their five inputs, the lag-`k` autocorrelation is `(5-k)/5`
for `k < 5`, and the inflation factor `1 + 2*sum` comes out at exactly 5. `n_eff` is
therefore `n/5` -- one independent episode per hold, which is the true sample and is
what the daily count overstates by a factor of five.

Getting this wrong is not a rounding matter. The t-statistic scales with `sqrt(n)`, so
reporting 1000 overlapping daily observations as independent overstates it by `sqrt(5)`,
about 2.24, which moves a p-value of 0.15 below 0.01 with nothing else changed. The
mutation operators in P6 produce overlapping-position designs by default, so this is the
common case rather than a corner.
"""

from __future__ import annotations

import random
from collections.abc import Sequence
from decimal import Decimal
from typing import Final

import pytest

from fking.backtest.portfolio import (
    ANNUALISATION_DAYS,
    MIN_OBSERVATIONS_FOR_EFFECTIVE_SAMPLE,
    MetricInputError,
    PortfolioReport,
    RiskLimitBreachedError,
    assemble_report,
    effective_sample_size,
    path_economics,
    path_statistics,
    risk_profile,
)
from tests.backtest.portfolio_support import (
    path_from_returns,
    state_with_fill_count,
)

pytestmark = pytest.mark.unit

# Fixed, so a failure reproduces exactly from the CI log. A flaky statistical test
# trains people to re-run rather than to read the failure.
_SEED: Final = 20260807

_HOLD_DAYS: Final = 5

# Long enough that the significance band (about +/-0.062 at n=1000) separates the four
# real lags of a five-day hold from the noise beyond them.
_OBSERVATION_COUNT: Final = 1000


def overlapping_hold_returns(
    *, observation_count: int, hold_days: int, seed: int
) -> tuple[Decimal, ...]:
    """Daily returns of a strategy holding each position for `hold_days` days.

    Each day's attributed return is the mean of the `hold_days` most recent independent
    shocks, which is exactly what an overlapping hold produces: one economic decision
    spread across several daily marks.
    """
    generator = random.Random(seed)
    shocks = [
        Decimal(str(round(generator.gauss(0.0, 0.01), 8)))
        for _ in range(observation_count + hold_days - 1)
    ]
    return tuple(
        sum(shocks[index : index + hold_days], start=Decimal("0")) / Decimal(hold_days)
        for index in range(observation_count)
    )


def test_a_five_day_hold_reports_an_effective_sample_near_one_fifth_of_the_days() -> None:
    """The headline property: 1000 overlapping days are about 200 independent episodes."""
    returns = overlapping_hold_returns(
        observation_count=_OBSERVATION_COUNT, hold_days=_HOLD_DAYS, seed=_SEED
    )
    sample = effective_sample_size(returns)

    assert sample.observation_count == _OBSERVATION_COUNT
    # The theoretical factor is exactly 5; the estimate is sampling noise around it.
    assert Decimal("4.0") < sample.inflation_factor < Decimal("6.0")
    assert Decimal("160") < sample.n_eff < Decimal("250")
    assert sample.significant_lag_count == _HOLD_DAYS - 1


def test_independent_daily_returns_are_not_deflated() -> None:
    """A strategy whose days are genuinely independent keeps its whole sample."""
    generator = random.Random(_SEED)
    returns = tuple(
        Decimal(str(round(generator.gauss(0.0, 0.01), 8))) for _ in range(_OBSERVATION_COUNT)
    )
    sample = effective_sample_size(returns)

    assert sample.inflation_factor == Decimal("1")
    assert sample.n_eff == Decimal(_OBSERVATION_COUNT)
    assert sample.significant_lag_count == 0


def test_negative_autocorrelation_cannot_manufacture_more_days_than_the_path_has() -> None:
    """The floor at one: reporting more independent observations than observations is
    the same fabrication running backwards."""
    alternating = tuple(
        Decimal("0.01") if index % 2 == 0 else Decimal("-0.01") for index in range(200)
    )
    sample = effective_sample_size(alternating)

    assert sample.inflation_factor == Decimal("1")
    assert sample.n_eff == Decimal("200")


def test_a_constant_series_has_no_autocorrelation_to_correct_for() -> None:
    """Zero variance is not zero independence; it is a denominator that does not exist."""
    sample = effective_sample_size(tuple(Decimal("0") for _ in range(50)))

    assert sample.inflation_factor == Decimal("1")
    assert sample.n_eff == Decimal("50")
    assert sample.autocorrelations == ()


def test_too_few_days_is_refused_rather_than_estimated() -> None:
    """A band of +/-0.69 admits nothing, so an estimate here would report independence
    that was never established."""
    short = tuple(Decimal("0.001") * Decimal(index) for index in range(4))
    with pytest.raises(MetricInputError, match="below the"):
        effective_sample_size(short)
    assert len(short) < MIN_OBSERVATIONS_FOR_EFFECTIVE_SAMPLE


def _report(returns: Sequence[Decimal]) -> PortfolioReport:
    return assemble_report(path=path_from_returns(returns), final_state=state_with_fill_count(3))


def test_the_sharpe_t_statistic_uses_n_eff_and_not_the_observation_count() -> None:
    """The downstream consequence, asserted as a number rather than as an intention.

    The reported t-statistic is the per-day Sharpe scaled by `sqrt(n_eff)`. The one the
    raw daily count would have produced is the same figure scaled by `sqrt(n)`, so the
    ratio between them is `sqrt(inflation_factor)` -- about 2.24 for a five-day hold.
    """
    overlapping = overlapping_hold_returns(
        observation_count=_OBSERVATION_COUNT, hold_days=_HOLD_DAYS, seed=_SEED
    )
    statistics = _report(overlapping).statistics
    assert statistics.effective_sample is not None
    assert statistics.sharpe_ratio is not None
    assert statistics.sharpe_t_statistic is not None

    per_day_sharpe = statistics.sharpe_ratio / Decimal(ANNUALISATION_DAYS).sqrt()
    naive_t_statistic = per_day_sharpe * Decimal(_OBSERVATION_COUNT).sqrt()
    corrected_t_statistic = per_day_sharpe * statistics.effective_sample.n_eff.sqrt()

    assert abs(statistics.sharpe_t_statistic - corrected_t_statistic) < Decimal("0.001")
    # The uncorrected figure is materially larger, and always in the flattering
    # direction. 1/sqrt(5) is about 0.447, so 0.6 is a loose bound that still fails
    # outright if the raw count were ever substituted.
    assert abs(statistics.sharpe_t_statistic) < abs(naive_t_statistic) * Decimal("0.6")

    economics = path_economics(overlapping)
    risk = risk_profile(overlapping)
    recomputed = path_statistics(overlapping, risk=risk, economics=economics)
    assert recomputed.sharpe_t_statistic == statistics.sharpe_t_statistic


def test_independent_days_lose_nothing_to_the_correction() -> None:
    """The control for the test above: with no overlap the two figures coincide."""
    generator = random.Random(_SEED)
    independent = tuple(
        Decimal(str(round(generator.gauss(0.0005, 0.01), 8))) for _ in range(_OBSERVATION_COUNT)
    )
    statistics = _report(independent).statistics
    assert statistics.effective_sample is not None
    assert statistics.sharpe_ratio is not None
    assert statistics.sharpe_t_statistic is not None

    per_day_sharpe = statistics.sharpe_ratio / Decimal(ANNUALISATION_DAYS).sqrt()
    assert statistics.effective_sample.n_eff == Decimal(_OBSERVATION_COUNT)
    assert abs(
        statistics.sharpe_t_statistic - per_day_sharpe * Decimal(_OBSERVATION_COUNT).sqrt()
    ) < Decimal("0.001")


def test_the_overfitting_gate_is_handed_n_eff_as_the_episode_count() -> None:
    """`independent_episode_count` is the effective sample. The raw daily count is never
    substituted, which is the whole point of computing `n_eff` at all."""
    returns = overlapping_hold_returns(
        observation_count=_OBSERVATION_COUNT, hold_days=_HOLD_DAYS, seed=_SEED
    )
    report = _report(returns)
    evidence = report.sharpe_evidence(
        trials_at_time_of_run=200, sharpe_variance_across_trials=Decimal("0.01")
    )

    assert report.statistics.effective_sample is not None
    assert evidence.independent_episode_count == report.statistics.effective_sample.episode_count
    assert evidence.independent_episode_count < report.credibility.observation_count


def test_a_breached_run_cannot_hand_its_sharpe_to_the_gate() -> None:
    """A hard negative, not a discount: the gate never sees the number at all."""
    returns = overlapping_hold_returns(
        observation_count=_OBSERVATION_COUNT, hold_days=_HOLD_DAYS, seed=_SEED
    )
    breached = state_with_fill_count(3).with_risk_limit_breach(
        occurs_at_utc=state_with_fill_count(3).as_of_utc
    )
    report = assemble_report(path=path_from_returns(returns), final_state=breached)

    assert report.is_clean is False
    with pytest.raises(RiskLimitBreachedError, match="is not evidence"):
        report.sharpe_evidence(
            trials_at_time_of_run=200, sharpe_variance_across_trials=Decimal("0.01")
        )
