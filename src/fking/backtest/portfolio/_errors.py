"""Failures of portfolio accounting and of the metric suite computed from it.

All of them are terminal for the run that raised one, for the reason
`fking.backtest._errors` gives: a portfolio that carried on past a missing mark, a
malformed daily grid or a path that reached ruin would still produce an equity curve,
and that curve would be indistinguishable from a correct one.
"""

from __future__ import annotations

from fking.backtest._errors import BacktestError


class PortfolioError(BacktestError):
    """Base for every failure of portfolio accounting and its metric suite."""


class PortfolioAccountingError(PortfolioError):
    """State was asked to advance in a way that would corrupt the books.

    Two shapes: a fill or funding settlement stamped before the instant the state is
    already at, and a mapping whose key disagrees with the instrument its value names.
    Both are ordering faults, and both would produce a plausible equity number.
    """


class MarkPriceMissingError(PortfolioAccountingError):
    """A position was held and no mark was supplied for it.

    Refused rather than defaulted. Treating an absent mark as zero writes the whole
    position off, and treating it as the entry price freezes unrealised PnL at zero --
    which is the shape that makes a drawdown invisible for exactly as long as the feed
    gap lasts.
    """


class EquityPathError(PortfolioError):
    """The daily mark-to-market grid is not a grid, or the path reached ruin.

    The grid is fixed, daily and identical for every strategy. That is what makes two
    Sharpes comparable at all: a per-trade annualisation lets a strategy set its own
    `sqrt(f)` and the evolution engine then learns to trade more often, which is a fee
    rather than an edge (issue #38).
    """


class RiskLimitBreachedError(PortfolioError):
    """A run that breached a risk limit was asked for a result that presumes it did not.

    Raised by `PortfolioReport.sharpe_evidence` and by `require_clean_result`, so a
    breached run cannot reach the validation gate at all. `SURVIVAL_PROTOCOL.md` scores
    a limit breach harder than it rewards profit, and a breach that only lowered a score
    would still let an excellent Sharpe carry a strategy through -- which is the exact
    trade the survival score exists to refuse.
    """


class MetricInputError(PortfolioError):
    """A statistic was asked for on a sample that cannot support it.

    Distinct from an undefined *ratio*: a Sharpe whose denominator is zero is reported
    as `None` and read as "undefined", because a flat regime bucket is an ordinary
    observation. This error is for a sample too short to estimate anything from.
    """
