"""The daily mark-to-market grid: one fixed lattice of UTC midnights, for every strategy.

The grid exists to take `sqrt(f)` away from the strategy. Computing Sharpe on per-trade
returns and annualising by the strategy's own trade frequency makes the annualisation
factor a parameter the strategy controls: a strategy trading ten times more often
annualises its per-trade Sharpe by `sqrt(10)`, so two strategies with *identical* equity
curves and different trade counts receive different Sharpes, and the evolution engine
learns to trade more often -- which is not an edge, it is a fee (`SCORING_ENGINE.md`
section 4).

**The boundary is 00:00:00 UTC and the window must be aligned to it.** A misaligned
window is refused rather than clamped, and that refusal is the whole reason this module
exists as something other than a `range`. Crypto has no session close: a run from 09:30 to
09:30 produces marks exactly 24 hours apart and daily returns that look completely normal,
each straddling two calendar days. Nothing about it is visible in the numbers. It only
surfaces when that run is compared against a midnight-aligned one -- which is the entire
purpose of a fixed grid -- as a divergence nobody attributes to the clock.

**UTC, not a calendar.** Every boundary is exactly 24 hours after the previous one, which
is true in UTC and false in any zone with daylight saving. Defining the grid on local
calendar days would produce two irregular intervals a year, in a series whose variance is
then annualised by `sqrt(365)`.

The interval a return is attributed to is **half-open on the left and closed on the
right**: `r_k` covers `(t_(k-1), t_k]`. An event stamped exactly at midnight therefore
belongs to the day that just ended, not the one starting. Either convention is defensible
and the failure is having two of them, so this one is stated here and asserted in
`tests/backtest/test_daily_grid.py`.
"""

from __future__ import annotations

from datetime import datetime, timedelta
from typing import Final

from fking.backtest.accounting._errors import GridBoundaryError

ONE_DAY: Final = timedelta(days=1)

# Sharpe, Sortino and the deflation inputs are all annualised from this series, so the
# figure is part of the grid's contract rather than a detail of whoever computes them.
# 365 rather than 252: crypto trades every day, and using an equity-market trading-day
# count would understate annualised volatility by roughly 19%.
TRADING_DAYS_PER_YEAR: Final[int] = 365


def _require_utc_midnight(candidate: datetime, field_name: str) -> datetime:
    if not isinstance(candidate, datetime):
        raise GridBoundaryError(
            f"{field_name} must be a datetime, got {type(candidate).__name__} {candidate!r}"
        )
    if candidate.tzinfo is None or candidate.utcoffset() is None:
        raise GridBoundaryError(f"{field_name} must be timezone-aware; got naive {candidate!r}")
    if candidate.utcoffset() != timedelta(0):
        raise GridBoundaryError(
            f"{field_name} must be UTC; got offset {candidate.utcoffset()!r} in {candidate!r}"
        )
    if (candidate.hour, candidate.minute, candidate.second, candidate.microsecond) != (0, 0, 0, 0):
        raise GridBoundaryError(
            f"{field_name} is {candidate.isoformat()}, which is not a UTC midnight. The "
            f"daily grid has no session close to make a misalignment visible, so a "
            f"window that does not start and end on 00:00:00Z is refused rather than "
            f"snapped: snapping would move the run's returns by a fraction of a day and "
            f"report a number that looks entirely ordinary."
        )
    return candidate


def daily_mark_grid(*, window_start_utc: datetime, window_end_utc: datetime) -> tuple[datetime, ...]:
    """Every mark instant from `window_start_utc` to `window_end_utc`, inclusive.

    Both ends must be UTC midnights, so the returned lattice has no partial day at either
    edge and the count of daily returns is exactly the number of whole days in the window.
    A partial first or last day would enter the variance with a different scale from every
    other observation, and the standard fix -- dropping it -- silently changes the window
    the result claims to cover.
    """
    start = _require_utc_midnight(window_start_utc, "window_start_utc")
    end = _require_utc_midnight(window_end_utc, "window_end_utc")
    if end <= start:
        raise GridBoundaryError(
            f"window_end_utc {end.isoformat()} is not after window_start_utc "
            f"{start.isoformat()}; a grid needs at least one whole day to mark across"
        )

    boundaries: list[datetime] = []
    moment = start
    while moment <= end:
        boundaries.append(moment)
        moment += ONE_DAY
    return tuple(boundaries)
