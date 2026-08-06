"""What portfolio accounting refuses, and why each refusal is terminal.

Every class is a leaf of `BacktestError`, so a caller that wanted "any failure this
engine raises on purpose" catches them too.

None is recoverable in-process, and the reason is the same one in every case: the
alternative to raising is a number. An account that silently skips an unpriced position
reports an equity that is missing an instrument; a curve that clamps a ruined equity to
zero reports a return of -100% followed by returns computed against zero. Both of those
land in a Sharpe, and neither is distinguishable afterwards from a correct one.
"""

from __future__ import annotations

from fking.backtest._errors import BacktestError


class PortfolioAccountingError(BacktestError):
    """Base for every failure of the accounting ledger and the equity path it produces."""


class AccountCurrencyError(PortfolioAccountingError):
    """An event denominated in an asset the account does not hold cash in.

    Applying an ETH-quoted fill to a USDT-denominated account would require an exchange
    rate, and a rate has an as-of time, a source and a staleness -- none of which fit in
    an addition. Converting here would launder a guess into the equity curve, so the
    account refuses and the caller runs a second account per quote asset.
    """


class AccountLedgerError(PortfolioAccountingError):
    """The ledger was handed an event it cannot apply to the state it holds.

    A malformed opening balance, a fill for an instrument whose position disagrees with
    it, or an event that would corrupt the one-position-per-instrument invariant.
    """


class MarkUnavailableError(PortfolioAccountingError):
    """A position carrying exposure has no mark at a grid instant.

    Treating a missing mark as zero prices the position at nothing, which reads as a
    total loss on that instrument and then as a full recovery at the next mark that does
    exist -- a drawdown the strategy never had, in the series the drawdown metrics are
    computed from. Carrying the previous mark forward is the same failure moved one step
    away, because it reports a price that was not observed.
    """


class GridBoundaryError(PortfolioAccountingError):
    """A mark-to-market window that is not aligned to the UTC daily boundary.

    Crypto has no session close, so nothing about a misaligned window looks wrong: a run
    from 09:30 to 09:30 produces marks that are exactly 24 hours apart and daily returns
    that straddle two calendar days each. It is only when two strategies with different
    windows are compared -- which is the entire purpose of a fixed grid
    (`SCORING_ENGINE.md` section 4) -- that the misalignment shows up, as a difference
    nobody attributes to the clock.
    """


class EventOrderError(PortfolioAccountingError):
    """Events arrived out of chronological order.

    The walk attributes each event to the grid interval containing it, in one forward
    pass. An event arriving after one stamped later than it is either attributed to the
    wrong day or dropped entirely, and both move PnL between days without changing the
    total -- which is invisible in the final equity and changes every path statistic
    computed from the daily series.
    """


class EquityPathRuinedError(PortfolioAccountingError):
    """Equity reached zero or below, so no return can be computed across that mark.

    Not a weak result -- an arithmetically undefined one. A return of
    `equity_k / equity_(k-1) - 1` through a non-positive denominator inverts the sign of
    the move, so the mark at which the account was wiped out reports as a gain. Ruin is a
    terminal outcome the caller records as ruin; it is not a point on an equity curve.
    """


class EffectiveSampleError(PortfolioAccountingError):
    """The daily return series is too short to estimate autocorrelation from.

    Returning the raw observation count as a fallback is the specific failure this
    prevents: it is the number the correction exists to replace, and it flatters every
    statistic computed from it (`.claude/rules/overfitting-defences.md` clause 7).
    """
