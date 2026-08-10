"""The Monte Carlo confidence interval on an estimate, not the spread across paths.

These are two different objects and only one of them is guaranteed to react to path
count. `TradeBootstrapReport.max_drawdown_p05`/`p95` describe the *population* of
drawdowns the resampling can produce -- a property of the trades and the block length,
not of how many paths were drawn -- so a percentile band computed from few paths can, on
any given seed, come out narrower than one from many; it is a noisy estimate of a fixed
target and the noise does not point one direction.

A standard-error confidence interval around the *mean* drawdown is a different object:
by the central limit theorem its half-width is `z * s / sqrt(path_total)`, so for a fixed
underlying return-generating process, halving the standard error costs quadrupling the
path count and quartering it costs sixteen times as many paths. That `1 / sqrt(n)` term
is the whole content of issue #43's acceptance criterion that the interval "widens
measurably between 1000 paths and 100 paths" -- it is arithmetic, not a property of any
particular resampled sample, which is what makes it safe to pin as a regression fixture
rather than leaving it to come out right on average.

The consequence stated plainly: **path count is not a knob that can be turned down for
speed without the report showing it.** A run that quietly drops from 1000 paths to 100
reports a confidence interval measurably wider than before, on the same trades, under the
same seed derivation scheme.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from decimal import Decimal
from typing import Final

from fking.backtest.montecarlo._errors import MonteCarloConfigError
from fking.backtest.montecarlo._stats import (
    STATISTIC_QUANTUM,
    mean,
    sample_standard_deviation,
    two_sided_z_score,
)

# Two paths is the arithmetic floor for a sample standard deviation; below it there is no
# dispersion to estimate a standard error from.
MIN_PATHS_FOR_CONFIDENCE_INTERVAL: Final[int] = 2

DEFAULT_CONFIDENCE: Final = Decimal("0.95")


@dataclass(frozen=True, slots=True)
class DrawdownConfidenceInterval:
    """The Monte Carlo standard-error band around the mean max-drawdown estimate."""

    path_total: int
    confidence: Decimal
    mean_max_drawdown_fraction: Decimal
    lower_bound_fraction: Decimal
    upper_bound_fraction: Decimal

    @property
    def width_fraction(self) -> Decimal:
        """`upper - lower`. The number issue #43's path-count criterion is pinned on."""
        return (self.upper_bound_fraction - self.lower_bound_fraction).quantize(STATISTIC_QUANTUM)


def drawdown_confidence_interval(
    path_max_drawdowns: Sequence[Decimal], *, confidence: Decimal = DEFAULT_CONFIDENCE
) -> DrawdownConfidenceInterval:
    """The standard-error confidence interval on the mean of `path_max_drawdowns`.

    Not a percentile band over the sample -- see the module docstring for why that is a
    different object that this criterion is not about.
    """
    if len(path_max_drawdowns) < MIN_PATHS_FOR_CONFIDENCE_INTERVAL:
        raise MonteCarloConfigError(
            f"a confidence interval needs at least {MIN_PATHS_FOR_CONFIDENCE_INTERVAL} "
            f"paths; got {len(path_max_drawdowns)}"
        )
    path_total = len(path_max_drawdowns)
    sample_mean = mean(path_max_drawdowns)
    deviation = sample_standard_deviation(path_max_drawdowns)
    z_score = two_sided_z_score(confidence)
    half_width = z_score * deviation / (float(path_total) ** 0.5)
    half_width_decimal = Decimal(str(half_width)).quantize(STATISTIC_QUANTUM)

    return DrawdownConfidenceInterval(
        path_total=path_total,
        confidence=confidence,
        mean_max_drawdown_fraction=sample_mean,
        lower_bound_fraction=(sample_mean - half_width_decimal).quantize(STATISTIC_QUANTUM),
        upper_bound_fraction=(sample_mean + half_width_decimal).quantize(STATISTIC_QUANTUM),
    )
