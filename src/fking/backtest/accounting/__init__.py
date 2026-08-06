"""Portfolio accounting, and the daily equity path every path statistic is drawn from.

The ledger is `PortfolioAccount`: cash in one quote asset plus a position per instrument,
advanced by fills and by funding settlements, immutable, and idempotent on both. Equity is
`cash + sum(signed_quantity * mark)` and there is no second definition of it anywhere.

The path is `EquityCurve`: that account sampled on a **fixed daily grid of UTC midnights,
identical for every strategy**. The grid is the point of the package rather than a detail
of it. Computing Sharpe on per-trade returns and annualising by the strategy's own trade
frequency hands `sqrt(f)` to the strategy: two strategies with identical equity curves and
a tenfold difference in trade count receive Sharpes differing by `sqrt(10)`, and the
evolution engine learns to trade more often, which is a fee rather than an edge
(`SCORING_ENGINE.md` section 4). The window must start and end on 00:00:00Z and is refused
otherwise -- crypto has no session close to make a misaligned grid visible, so the
misalignment surfaces only as an incomparability between two runs.

`effective_sample` is the count anything statistical is allowed to use.
`EquityCurve.observation_count` is `n`; `EffectiveSample.independent_episode_count` is
`n_eff`, and it is what fills `SharpeEvidence.independent_episode_count`. For a five-day
hold the two differ by a factor of five, and a t-statistic computed on the first is
overstated by `sqrt(5)` with nothing about it looking unusual.

Everything is `Decimal` from the opening balance to the reported mark, quantised with
`ROUND_HALF_EVEN` at the reporting boundary. The one `float` is the autocorrelation
estimate inside `_sample`, behind a named conversion.

The metric suite proper -- Sharpe, Sortino, Calmar, ulcer, VaR/CVaR and the per-regime
breakdown -- consumes this curve and arrives in its own pull request. Everything not in
`__all__` is private and may change without notice.
"""

from fking.backtest.accounting._account import AccountTransition, PortfolioAccount
from fking.backtest.accounting._curve import (
    REPORT_QUANTUM,
    AccountEvent,
    EquityCurve,
    EquityPoint,
    event_instant_utc,
    mark_to_market,
)
from fking.backtest.accounting._errors import (
    AccountCurrencyError,
    AccountLedgerError,
    EffectiveSampleError,
    EquityPathRuinedError,
    EventOrderError,
    GridBoundaryError,
    MarkUnavailableError,
    PortfolioAccountingError,
)
from fking.backtest.accounting._funding import FundingKey, FundingSettlement, settle_funding
from fking.backtest.accounting._grid import ONE_DAY, TRADING_DAYS_PER_YEAR, daily_mark_grid
from fking.backtest.accounting._sample import (
    MIN_OBSERVATIONS_FOR_AUTOCORRELATION,
    EffectiveSample,
    effective_sample,
)

__all__: tuple[str, ...] = (
    "MIN_OBSERVATIONS_FOR_AUTOCORRELATION",
    "ONE_DAY",
    "REPORT_QUANTUM",
    "TRADING_DAYS_PER_YEAR",
    "AccountCurrencyError",
    "AccountEvent",
    "AccountLedgerError",
    "AccountTransition",
    "EffectiveSample",
    "EffectiveSampleError",
    "EquityCurve",
    "EquityPathRuinedError",
    "EquityPoint",
    "EventOrderError",
    "FundingKey",
    "FundingSettlement",
    "GridBoundaryError",
    "MarkUnavailableError",
    "PortfolioAccount",
    "PortfolioAccountingError",
    "daily_mark_grid",
    "effective_sample",
    "event_instant_utc",
    "mark_to_market",
    "settle_funding",
)
