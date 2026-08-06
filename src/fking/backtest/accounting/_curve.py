"""The equity path: the account sampled on the daily grid, and what that series says.

Every path statistic in the metric suite is a function of the series this module
produces, so two properties are load-bearing.

**Reported numbers are quantised with `ROUND_HALF_EVEN`, at construction.** Banker's
rounding is unbiased over many roundings; `ROUND_HALF_UP` adds half a tick of expected
value per rounded half, which across a year of daily marks becomes a visible upward drift
in exactly the number the evolution engine optimises
(`.claude/rules/decimal-and-money.md`). The quantisation happens in `EquityPoint`'s
constructor rather than in the builder, so a second builder written later cannot forget
it.

**Equity is derived from the two quantised components, never quantised separately.**
Rounding cash, exposure and their sum independently lets the reported equity differ from
the reported cash plus the reported exposure by one tick -- a discrepancy that reads as a
ledger fault to anyone checking the arithmetic by hand, which is precisely who checks it.

The series is `Decimal` end to end. A Sharpe computed from it may be `float` under the
statistical exception, but that conversion happens in the module that computes the Sharpe,
against a named boundary, and never here.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime
from decimal import ROUND_HALF_EVEN, Decimal
from typing import Final

from fking.backtest.accounting._account import PortfolioAccount
from fking.backtest.accounting._errors import (
    EquityPathRuinedError,
    EventOrderError,
    GridBoundaryError,
)
from fking.backtest.accounting._funding import FundingSettlement
from fking.backtest.accounting._grid import ONE_DAY, daily_mark_grid
from fking.backtest.accounting._guards import require_utc
from fking.domain import Fill, Instrument

# One satoshi of USDT. Wide enough that two adjacent daily marks on a five-figure account
# are never collapsed onto the same value, and narrow enough to sit inside the
# NUMERIC(38, 18) the series is stored in.
REPORT_QUANTUM: Final = Decimal("0.00000001")
_PCT_QUANTUM: Final = Decimal("0.0001")
_ZERO: Final = Decimal("0")
_HUNDRED: Final = Decimal("100")

#: What the ledger can be advanced by. A union rather than a base class: a fill and a
#: funding settlement share a timestamp and nothing else, and inventing a common
#: supertype for them would be an abstraction with one property on it.
AccountEvent = Fill | FundingSettlement


def event_instant_utc(event: AccountEvent) -> datetime:
    """The venue's timestamp for `event`, which is what the grid buckets it by.

    Never a local receipt time. Our clock's relationship to the venue's is an unknown
    offset plus network delay, and attributing an 00:00:04Z fill to the previous day
    because our clock ran fast moves a day of PnL between buckets.
    """
    if isinstance(event, Fill):
        return event.event_time_utc
    return event.occurs_at_utc


@dataclass(frozen=True, slots=True)
class EquityPoint:
    """The account marked at one grid instant.

    `has_exposure_in_interval` reports the interval *ending* at this mark, so it is true
    for a day the strategy opened and closed inside. Sampling exposure at the mark alone
    would report a day of intraday trading as a flat day, and `time_in_market_pct` is
    read as the caveat on the Sharpe -- understating it hides exactly the case the caveat
    exists for. The first point of a curve closes no interval; its flag reports the
    exposure carried into the run.
    """

    mark_time_utc: datetime
    cash_quote: Decimal
    exposure_quote: Decimal
    has_exposure_in_interval: bool

    def __post_init__(self) -> None:
        require_utc(self.mark_time_utc, "mark_time_utc")
        object.__setattr__(
            self, "cash_quote", self.cash_quote.quantize(REPORT_QUANTUM, rounding=ROUND_HALF_EVEN)
        )
        object.__setattr__(
            self,
            "exposure_quote",
            self.exposure_quote.quantize(REPORT_QUANTUM, rounding=ROUND_HALF_EVEN),
        )

    @property
    def equity_quote(self) -> Decimal:
        """Cash plus signed exposure, from the two already-quantised components."""
        return self.cash_quote + self.exposure_quote


@dataclass(frozen=True, slots=True)
class EquityCurve:
    """Attributed equity on the daily grid, with the series derived from it.

    Holds at least two points, because one point is a balance rather than a path and
    every statistic downstream needs a return.
    """

    quote_asset: str
    points: tuple[EquityPoint, ...]

    def __post_init__(self) -> None:
        if len(self.points) < 2:
            raise GridBoundaryError(
                f"an equity curve needs at least two marks to produce one return; got "
                f"{len(self.points)}"
            )
        for earlier, later in zip(self.points, self.points[1:], strict=False):
            if later.mark_time_utc - earlier.mark_time_utc != ONE_DAY:
                raise GridBoundaryError(
                    f"marks at {earlier.mark_time_utc.isoformat()} and "
                    f"{later.mark_time_utc.isoformat()} are "
                    f"{later.mark_time_utc - earlier.mark_time_utc} apart; the grid is "
                    f"exactly one day and an irregular gap enters the variance at a "
                    f"different scale from every other observation"
                )

    @property
    def opening_equity_quote(self) -> Decimal:
        """Equity at the first mark."""
        return self.points[0].equity_quote

    @property
    def closing_equity_quote(self) -> Decimal:
        """Equity at the last mark."""
        return self.points[-1].equity_quote

    @property
    def observation_count(self) -> int:
        """How many daily returns the curve produces: one fewer than its marks.

        This is `n`, and it is deliberately not the number anything statistical consumes.
        The count that feeds a t-statistic or a deflated Sharpe is
        `effective_sample(curve).independent_episode_count`, which is smaller whenever
        positions overlap -- and overlapping positions are what the mutation operators
        produce by default (`.claude/rules/overfitting-defences.md` clause 7).
        """
        return len(self.points) - 1

    @property
    def daily_return_fractions(self) -> tuple[Decimal, ...]:
        """The daily return series, as dimensionless fractions.

        Simple returns rather than log returns: the drawdown, ulcer and Calmar figures
        are all defined on the equity path itself, and a series that does not compound
        back to the observed equity makes those disagree with the curve they are drawn
        against.
        """
        fractions: list[Decimal] = []
        for earlier, later in zip(self.points, self.points[1:], strict=False):
            opening = earlier.equity_quote
            if opening <= _ZERO:
                raise EquityPathRuinedError(
                    f"equity at {earlier.mark_time_utc.isoformat()} is {opening}; no "
                    f"return can be computed across it. Dividing by a non-positive "
                    f"opening equity inverts the sign of the move, so the mark at which "
                    f"the account was wiped out would report as a gain. Ruin is a "
                    f"terminal outcome, not a point on a curve."
                )
            fractions.append(later.equity_quote / opening - 1)
        return tuple(fractions)

    @property
    def time_in_market_pct(self) -> Decimal:
        """Percent of grid days on which the account carried exposure at any point.

        Travels with every Sharpe drawn from this curve, and is not optional. Marking
        daily penalises a strategy in the market 10% of the time by roughly
        `sqrt(0.10)`, so a selective strategy with an excellent active-period edge scores
        below a mediocre always-on one. That is deliberate -- capital allocated is
        capital committed -- but it is a real distortion, and the correct reading of
        "high edge, low participation" is an allocation problem rather than a strategy
        problem (`SCORING_ENGINE.md` section 4). A reader who cannot see this number
        cannot make that reading.
        """
        intervals = self.points[1:]
        exposed_day_count = sum(1 for point in intervals if point.has_exposure_in_interval)
        return (Decimal(exposed_day_count) * _HUNDRED / Decimal(len(intervals))).quantize(
            _PCT_QUANTUM, rounding=ROUND_HALF_EVEN
        )


def mark_to_market(
    *,
    opening_account: PortfolioAccount,
    events: Sequence[AccountEvent],
    marks_at: Mapping[datetime, Mapping[Instrument, Decimal]],
    window_start_utc: datetime,
    window_end_utc: datetime,
) -> EquityCurve:
    """Walk `events` across the daily grid and record equity at every boundary.

    One forward pass, which is why the ordering guard is not optional: an event arriving
    after one stamped later than it lands in the wrong bucket, which moves PnL between
    days without changing the total. The closing equity is unaffected and every path
    statistic changes, so nothing downstream can detect it.

    Events must fall in `(window_start_utc, window_end_utc]`, matching the interval
    convention in `_grid`. An event stamped exactly at the opening boundary belongs to the
    day before the run began and is refused rather than folded into the first interval --
    silently absorbing it would credit the run with a fill it did not make.

    `marks_at` is consulted only where exposure exists, so a flat day needs no prices. A
    day with exposure and no mark raises `MarkUnavailableError` naming the instrument.
    """
    grid = daily_mark_grid(window_start_utc=window_start_utc, window_end_utc=window_end_utc)
    ordered = _validated_events(events, window_start_utc=grid[0], window_end_utc=grid[-1])

    account = opening_account
    points = [
        _point_at(
            account,
            mark_time_utc=grid[0],
            marks=marks_at.get(grid[0], {}),
            has_exposure_in_interval=account.has_exposure,
        )
    ]

    cursor = 0
    for boundary in grid[1:]:
        had_exposure = account.has_exposure
        while cursor < len(ordered) and event_instant_utc(ordered[cursor]) <= boundary:
            account = _advanced(account, ordered[cursor])
            had_exposure = had_exposure or account.has_exposure
            cursor += 1
        points.append(
            _point_at(
                account,
                mark_time_utc=boundary,
                marks=marks_at.get(boundary, {}),
                has_exposure_in_interval=had_exposure,
            )
        )

    return EquityCurve(quote_asset=opening_account.quote_asset, points=tuple(points))


def _advanced(account: PortfolioAccount, event: AccountEvent) -> PortfolioAccount:
    if isinstance(event, Fill):
        return account.with_fill(event).after
    return account.with_funding(event).after


def _point_at(
    account: PortfolioAccount,
    *,
    mark_time_utc: datetime,
    marks: Mapping[Instrument, Decimal],
    has_exposure_in_interval: bool,
) -> EquityPoint:
    return EquityPoint(
        mark_time_utc=mark_time_utc,
        cash_quote=account.cash_quote,
        exposure_quote=account.exposure_quote(marks),
        has_exposure_in_interval=has_exposure_in_interval,
    )


def _validated_events(
    events: Sequence[AccountEvent], *, window_start_utc: datetime, window_end_utc: datetime
) -> tuple[AccountEvent, ...]:
    """`events` unchanged, having proved it is ordered and inside the window.

    Deliberately not a sort. Sorting here would make an out-of-order stream produce a
    plausible curve, and the stream being out of order means the engine's queue ordering
    is wrong -- which is a determinism failure that outranks the run
    (`BACKTEST_ENGINE.md` section 5), not an input to tidy up.
    """
    ordered = tuple(events)
    previous: datetime | None = None
    for event in ordered:
        instant = require_utc(event_instant_utc(event), "event instant")
        if previous is not None and instant < previous:
            raise EventOrderError(
                f"event at {instant.isoformat()} follows one at {previous.isoformat()}; "
                f"the mark-to-market walk is a single forward pass and cannot bucket it"
            )
        if not window_start_utc < instant <= window_end_utc:
            raise EventOrderError(
                f"event at {instant.isoformat()} falls outside the marked window "
                f"({window_start_utc.isoformat()}, {window_end_utc.isoformat()}]; an "
                f"event on the opening boundary belongs to the day before the run"
            )
        previous = instant
    return ordered
