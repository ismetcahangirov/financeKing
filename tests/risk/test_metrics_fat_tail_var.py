"""Empirical VaR against a parametric-normal estimate, on a synthetic fat-tailed series.

Issue #56's third acceptance criterion, verbatim: "Empirical VaR on a fat-tailed synthetic
series exceeds the Gaussian estimate by the expected margin; a parametric-normal
implementation fails the test." The Gaussian estimate is computed here, in the test, on
purpose -- production code never touches a normal distribution (`fking.risk.metrics`'s
module docstring explains why: crypto's tails are far fatter than a Gaussian predicts, and
a parametric estimate understates risk exactly where the number is relied upon). That also
makes this a regression guard: if `historical_tail_risk` were ever rewritten as a
mean/stdev formula, its output would collapse onto the Gaussian line below and this test
would fail.
"""

from __future__ import annotations

from decimal import Decimal
from typing import Final

import pytest

from fking.risk.metrics import historical_tail_risk

pytestmark = pytest.mark.unit

# 99% one-tailed z-score: P(Z <= -2.326) = 0.01 (Abramowitz & Stegun, table 26.1). Sourced
# once here, in a test, because production code must never carry this constant --
# `docs/rules/decimal-and-money.md`'s statistics carve-out excludes `fking.risk`
# unconditionally, and this whole module exists to prove that exclusion is upheld.
_Z_99: Final = Decimal("2.326")


def _population_mean(series: tuple[Decimal, ...]) -> Decimal:
    return sum(series, start=Decimal("0")) / Decimal(len(series))


def _population_stdev(series: tuple[Decimal, ...]) -> Decimal:
    mean = _population_mean(series)
    variance = sum(((value - mean) ** 2 for value in series), start=Decimal("0")) / Decimal(
        len(series)
    )
    return variance.sqrt()


def _gaussian_var_loss_ratio(series: tuple[Decimal, ...]) -> Decimal:
    """The parametric-normal formula `historical_tail_risk` deliberately does not implement."""
    mean = _population_mean(series)
    stdev = _population_stdev(series)
    return -(mean - _Z_99 * stdev)


def test_empirical_var_exceeds_the_gaussian_estimate_on_a_fat_tailed_series() -> None:
    """99 quiet days and one large shock: real excess kurtosis, not a hand-picked outlier.

    A Gaussian fit to this sample sees a small standard deviation, dominated by the 99
    quiet +/-0.1% days, and therefore reports a small VaR. The empirical 99% VaR on 100
    observations reads the single worst day directly -- the shock is exactly the 1-in-100
    tail -- and reports it undiluted.
    """
    observation_count = 100
    quiet = (Decimal("0.001"), Decimal("-0.001")) * 49  # 98 alternating +/-0.1% days
    series = (*quiet, Decimal("0.001"), Decimal("-0.10"))  # 100th day: a 10% shock
    assert len(series) == observation_count

    empirical = historical_tail_risk(series, confidence_ratio=Decimal("0.99"))
    gaussian_var_loss_ratio = _gaussian_var_loss_ratio(series)

    assert empirical.tail_sample_size == 1
    assert empirical.var_loss_ratio == Decimal("0.10")
    assert empirical.var_loss_ratio > gaussian_var_loss_ratio
    # Not just "larger" -- large enough that a parametric-normal implementation, which
    # would report something close to gaussian_var_loss_ratio, could not pass this bound.
    assert empirical.var_loss_ratio > gaussian_var_loss_ratio * Decimal("3")
