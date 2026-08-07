"""The fixed daily mark-to-market grid, and the equity path it produces.

**Every path statistic in this package is computed on this grid and no other.** The
alternative -- Sharpe on per-trade returns, annualised by the strategy's own trade
frequency -- makes `sqrt(f)` a free parameter the strategy controls. A strategy trading
ten times more often annualises its per-trade Sharpe by `sqrt(10)`, about 3.16, so two
strategies with identical equity curves and different trade counts receive different
Sharpes and the evolution engine learns to trade more often. That is a fee, not an edge
(issue #38).

The grid is midnight UTC, every day, no gaps. Crypto trades continuously, so there is no
session boundary to align to and no reason to drop weekends; `ANNUALISATION_DAYS` is
therefore 365 rather than the 252 an equities library would use. Using 252 on a
seven-day market overstates the annualised figure by `sqrt(365/252)`, about 20%, on
every strategy at once -- which is invisible precisely because it is uniform.

**The distortion this grid creates is accepted deliberately and reported rather than
hidden.** A strategy in the market 10% of the time is penalised by roughly `sqrt(0.10)`,
about 0.316, against its active-period figure. That is intended: a strategy holding a
slot and a risk budget while flat is consuming both, and rewarding active-period
performance would make "trade rarely, only in perfect conditions" the dominant
evolutionary strategy. `time_in_market_pct` travels with every score so the reader can
tell an allocation problem from a strategy problem.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime, timedelta
from decimal import Decimal
from types import MappingProxyType
from typing import Final

from fking.backtest._guards import require_finite_decimal, require_text, require_utc
from fking.backtest.portfolio._errors import EquityPathError
from fking.backtest.portfolio._state import PortfolioState
from fking.domain import Instrument

# Crypto settles continuously: no session, no weekend gap, no holiday calendar. An
# equities convention of 252 here would inflate every annualised figure by about 20%.
ANNUALISATION_DAYS: Final[int] = 365

# One return needs two boundaries. A single-point path has no return at all, and a
# metric suite computed from it would be a suite of division-by-zero guards.
MIN_GRID_BOUNDARIES: Final[int] = 2

_ONE_DAY: Final = timedelta(days=1)
_ZERO: Final = Decimal("0")
_ONE: Final = Decimal("1")
_HUNDRED: Final = Decimal("100")


def _require_grid_boundary(moment: datetime, what: str) -> datetime:
    """Exactly midnight UTC. A grid whose boundary drifts is not a grid."""
    require_utc(moment, what)
    if (moment.hour, moment.minute, moment.second, moment.microsecond) != (0, 0, 0, 0):
        raise EquityPathError(
            f"{what} is {moment.isoformat()}, which is not a midnight UTC grid "
            f"boundary. Two strategies marked at different instants of the day are not "
            f"comparable, and nothing in the numbers says so"
        )
    return moment


@dataclass(frozen=True, slots=True)
class DailyMark:
    """The marks and the regime label for one grid boundary.

    `regime` is supplied by the caller and must be derived from information available at
    or before this instant. Labelling a day from the regime it turned out to belong to
    is look-ahead entering through the breakdown rather than through a feature, and it
    produces a per-regime table that is flattering and false.
    """

    as_of_utc: datetime
    mark_quote_prices: Mapping[Instrument, Decimal]
    regime: str

    def __post_init__(self) -> None:
        _require_grid_boundary(self.as_of_utc, "DailyMark.as_of_utc")
        require_text(self.regime, "regime")
        for instrument, mark in self.mark_quote_prices.items():
            require_finite_decimal(mark, f"mark for {instrument.symbol}")
        object.__setattr__(
            self, "mark_quote_prices", MappingProxyType(dict(self.mark_quote_prices))
        )


@dataclass(frozen=True, slots=True)
class EquityPoint:
    """One day's mark-to-market, and what the portfolio was doing that day."""

    as_of_utc: datetime
    equity_usd: Decimal
    is_in_market: bool
    regime: str

    def __post_init__(self) -> None:
        _require_grid_boundary(self.as_of_utc, "EquityPoint.as_of_utc")
        require_finite_decimal(self.equity_usd, "equity_usd")
        require_text(self.regime, "regime")
        if self.equity_usd <= _ZERO:
            raise EquityPathError(
                f"equity is {self.equity_usd} at {self.as_of_utc.isoformat()}. A path "
                f"that reaches zero has reached ruin, and every return computed across "
                f"that boundary is a division by a capital base that no longer exists"
            )


@dataclass(frozen=True, slots=True)
class EquityPath:
    """A contiguous daily equity curve, and the only input the path statistics take.

    Contiguity is enforced rather than assumed. A missing day silently turns one daily
    return into a two-day return, which lowers the measured volatility and raises the
    Sharpe -- the direction that gets a strategy promoted.
    """

    points: tuple[EquityPoint, ...]

    def __post_init__(self) -> None:
        if len(self.points) < MIN_GRID_BOUNDARIES:
            raise EquityPathError(
                f"an equity path needs at least two grid boundaries to yield one daily "
                f"return; got {len(self.points)}"
            )
        for earlier, later in zip(self.points, self.points[1:], strict=False):
            if later.as_of_utc - earlier.as_of_utc != _ONE_DAY:
                raise EquityPathError(
                    f"the grid skips from {earlier.as_of_utc.isoformat()} to "
                    f"{later.as_of_utc.isoformat()}. A gap turns one daily return into "
                    f"a multi-day one, which lowers measured volatility and raises the "
                    f"Sharpe"
                )

    @property
    def observation_count(self) -> int:
        """How many daily returns the path yields: one fewer than its boundaries."""
        return len(self.points) - 1

    @property
    def starting_equity_usd(self) -> Decimal:
        return self.points[0].equity_usd

    @property
    def ending_equity_usd(self) -> Decimal:
        return self.points[-1].equity_usd

    @property
    def daily_return_fractions(self) -> tuple[Decimal, ...]:
        """One return per day, `equity_t / equity_(t-1) - 1`.

        Exact `Decimal` division at the process context's 38 digits. The equity path is
        `Decimal` end to end; `float` enters only inside the named boundary in
        `_metrics` and never on the way back out.
        """
        return tuple(
            later.equity_usd / earlier.equity_usd - _ONE
            for earlier, later in zip(self.points, self.points[1:], strict=False)
        )

    @property
    def earning_points(self) -> tuple[EquityPoint, ...]:
        """The boundaries a return is attributed to: every point but the opening one.

        Return `t` is earned over the day ending at point `t`, so the regime and the
        market participation that describe it are that point's, not its predecessor's.
        """
        return self.points[1:]

    @property
    def time_in_market_pct(self) -> Decimal:
        """Share of earning days on which any exposure was held, in percent, 0 to 100.

        Required on every report. A Sharpe read without it cannot be interpreted: a high
        figure at 5% participation and the same figure at 95% are different findings,
        and only the second one is a strategy.
        """
        earning = self.earning_points
        in_market = sum(1 for point in earning if point.is_in_market)
        return Decimal(in_market) * _HUNDRED / Decimal(len(earning))

    @property
    def regimes(self) -> tuple[str, ...]:
        """Every distinct regime label the earning days carry, in sorted order."""
        return tuple(sorted({point.regime for point in self.earning_points}))

    def returns_in_regime(self, regime: str) -> tuple[Decimal, ...]:
        """The daily returns earned on days labelled `regime`, in path order."""
        return tuple(
            return_fraction
            for return_fraction, point in zip(
                self.daily_return_fractions, self.earning_points, strict=True
            )
            if point.regime == regime
        )

    def time_in_market_pct_in_regime(self, regime: str) -> Decimal:
        """Participation within one regime bucket, in percent, 0 to 100."""
        days = tuple(point for point in self.earning_points if point.regime == regime)
        if not days:
            raise EquityPathError(f"no earning day carries the regime {regime!r}")
        in_market = sum(1 for point in days if point.is_in_market)
        return Decimal(in_market) * _HUNDRED / Decimal(len(days))


def build_equity_path(
    observations: Sequence[tuple[PortfolioState, DailyMark]],
) -> EquityPath:
    """Mark a sequence of daily portfolio states onto the fixed grid.

    Each observation pairs the state as it stood at a grid boundary with that day's
    marks. The state's own instant may precede the boundary -- the last fill of the day
    happened before midnight -- but never follow it, because marking a portfolio with
    state it did not yet have is look-ahead of the most direct kind.
    """
    points: list[EquityPoint] = []
    for state, mark in observations:
        _require_grid_boundary(mark.as_of_utc, "DailyMark.as_of_utc")
        if state.as_of_utc > mark.as_of_utc:
            raise EquityPathError(
                f"the portfolio stands at {state.as_of_utc.isoformat()} but is being "
                f"marked at {mark.as_of_utc.isoformat()}; a boundary cannot observe "
                f"state from after it"
            )
        points.append(
            EquityPoint(
                as_of_utc=mark.as_of_utc,
                equity_usd=state.equity_usd(mark.mark_quote_prices),
                is_in_market=state.is_in_market,
                regime=mark.regime,
            )
        )
    return EquityPath(points=tuple(points))
