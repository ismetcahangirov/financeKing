"""Portfolio accounting and the metric suite computed on a daily mark-to-market grid.

Three properties carry this package and each of them is structural rather than
conventional.

**Every path statistic is computed on a fixed daily grid, identical for every
strategy.** `path_statistics` takes a sequence of daily returns and nothing else, so a
trade count cannot reach a Sharpe. Annualising per-trade returns by the strategy's own
frequency would make `sqrt(f)` a parameter the strategy controls, and the evolution
engine would learn to trade more often -- a fee, not an edge. Only per-trade quantities
such as edge-to-cost and capacity are properties of an individual trade rather than of
the equity path, and those live in `fking.backtest.costs`.

**Everywhere an `n` appears it is `n_eff`.** Overlapping positions make consecutive daily
returns dependent, and treating a five-day-hold strategy's 1000 observations as 1000
independent draws overstates its t-statistic by about 2.2. `sharpe_evidence` feeds
`n_eff` to the overfitting gate and there is no argument through which the raw count
could be substituted.

**Credibility is read before the Sharpe, and a breached run is not a result.**
`SECTION_ORDER` fixes the reading order, `time_in_market_pct` and
`risk_limit_breach_count` are required fields, and `sharpe_evidence` refuses a run that
breached a risk limit rather than discounting it.

Everything not in `__all__` is private and may change without notice.
"""

from fking.backtest.portfolio._errors import (
    EquityPathError,
    MarkPriceMissingError,
    MetricInputError,
    PortfolioAccountingError,
    PortfolioError,
    RiskLimitBreachedError,
)
from fking.backtest.portfolio._grid import (
    ANNUALISATION_DAYS,
    DailyMark,
    EquityPath,
    EquityPoint,
    build_equity_path,
)
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
from fking.backtest.portfolio._regime import (
    MIN_REGIME_EFFECTIVE_SAMPLE,
    RegimeCoverage,
    RegimeSlice,
    regime_breakdown,
)
from fking.backtest.portfolio._report import (
    SECTION_ORDER,
    Credibility,
    LedgerTotals,
    PortfolioReport,
    ReportSection,
    assemble_report,
    require_clean_result,
)
from fking.backtest.portfolio._sample import (
    MIN_OBSERVATIONS_FOR_EFFECTIVE_SAMPLE,
    EffectiveSample,
    effective_sample_size,
)
from fking.backtest.portfolio._state import PortfolioState

__all__ = [
    "ANNUALISATION_DAYS",
    "MIN_OBSERVATIONS_FOR_DISPERSION",
    "MIN_OBSERVATIONS_FOR_EFFECTIVE_SAMPLE",
    "MIN_REGIME_EFFECTIVE_SAMPLE",
    "SECTION_ORDER",
    "Credibility",
    "DailyMark",
    "EffectiveSample",
    "EquityPath",
    "EquityPathError",
    "EquityPoint",
    "LedgerTotals",
    "MarkPriceMissingError",
    "MetricInputError",
    "PathEconomics",
    "PathStatistics",
    "PortfolioAccountingError",
    "PortfolioError",
    "PortfolioReport",
    "PortfolioState",
    "RegimeCoverage",
    "RegimeSlice",
    "ReportSection",
    "RiskLimitBreachedError",
    "RiskProfile",
    "assemble_report",
    "build_equity_path",
    "effective_sample_or_none",
    "effective_sample_size",
    "path_economics",
    "path_statistics",
    "regime_breakdown",
    "require_clean_result",
    "risk_profile",
]
