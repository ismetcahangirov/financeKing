"""Per-regime breakdown of the whole metric suite.

The aggregate conceals the finding. A Sharpe of 1.2 that is 3.0 in one regime and -0.4
in another is a regime bet wearing a strategy's clothes, and no amount of staring at the
1.2 reveals that -- so every metric this package computes is emitted per regime bucket
as well as over the whole path.

**A thin bucket is flagged, never dropped.** Dropping a bucket whose effective sample is
small removes exactly the regimes a strategy has least evidence for, which is the subset
a reader most needs to see. `regime_coverage` says `THIN` and the numbers are printed
anyway, qualified rather than deleted.

Two thinness conditions, and they are different failures. A bucket with fewer than two
days has no dispersion at all, so its risk profile and its ratios do not exist and are
`None`. A bucket with enough days but an effective sample below ten has numbers that
exist and cannot carry weight -- ten independent episodes is already a generous floor
for a claim about a regime.
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
from enum import StrEnum
from typing import Final

from fking.backtest.portfolio._grid import EquityPath
from fking.backtest.portfolio._metrics import (
    MIN_OBSERVATIONS_FOR_DISPERSION,
    PathEconomics,
    PathStatistics,
    RiskProfile,
    effective_sample_or_none,
    path_economics,
    path_statistics,
    risk_profile,
)

# Ten independent episodes. Below this the deflated Sharpe is undefined at two and
# barely informative at nine, and a regime claim resting on fewer is a claim about
# whichever handful of days happened to carry the label.
MIN_REGIME_EFFECTIVE_SAMPLE: Final = Decimal("10")


class RegimeCoverage(StrEnum):
    """Whether a bucket's numbers can carry weight."""

    SUFFICIENT = "sufficient"
    THIN = "thin"


@dataclass(frozen=True, slots=True)
class RegimeSlice:
    """One regime's share of the path, carrying the same metrics as the whole.

    `risk` and `statistics` are `None` only for a bucket too short to have a dispersion.
    That is a distinct statement from a ratio being `None` inside a populated
    `PathStatistics`, which means the ratio's own denominator was zero.
    """

    regime: str
    observation_count: int
    time_in_market_pct: Decimal
    economics: PathEconomics
    risk: RiskProfile | None
    statistics: PathStatistics | None
    regime_coverage: RegimeCoverage

    @property
    def n_eff(self) -> Decimal | None:
        """The bucket's effective sample size, or `None` when it could not be estimated."""
        if self.statistics is None or self.statistics.effective_sample is None:
            return None
        return self.statistics.effective_sample.n_eff


def regime_breakdown(path: EquityPath) -> tuple[RegimeSlice, ...]:
    """Every regime present on the path, in sorted label order.

    Sorted rather than first-seen: a report whose row order depends on which regime the
    run happened to open in is not comparable between two runs, and comparability
    between runs is the only reason the breakdown exists.
    """
    slices: list[RegimeSlice] = []
    for regime in path.regimes:
        return_fractions = path.returns_in_regime(regime)
        economics = path_economics(return_fractions)
        if len(return_fractions) < MIN_OBSERVATIONS_FOR_DISPERSION:
            slices.append(
                RegimeSlice(
                    regime=regime,
                    observation_count=len(return_fractions),
                    time_in_market_pct=path.time_in_market_pct_in_regime(regime),
                    economics=economics,
                    risk=None,
                    statistics=None,
                    regime_coverage=RegimeCoverage.THIN,
                )
            )
            continue

        risk = risk_profile(return_fractions)
        statistics = path_statistics(return_fractions, risk=risk, economics=economics)
        sample = effective_sample_or_none(return_fractions)
        coverage = (
            RegimeCoverage.SUFFICIENT
            if sample is not None and sample.n_eff >= MIN_REGIME_EFFECTIVE_SAMPLE
            else RegimeCoverage.THIN
        )
        slices.append(
            RegimeSlice(
                regime=regime,
                observation_count=len(return_fractions),
                time_in_market_pct=path.time_in_market_pct_in_regime(regime),
                economics=economics,
                risk=risk,
                statistics=statistics,
                regime_coverage=coverage,
            )
        )
    return tuple(slices)
