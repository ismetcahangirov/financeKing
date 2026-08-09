"""The feature functions themselves. Trailing only, and self-contained on purpose.

Two properties hold for every function in this module, and both are structural rather
than conventional.

**Trailing, never centred and never full-sample.** The value stamped at `t` is computed
from observations in the half-open window `(t - lookback, t]`, all of which had already
happened at `t`. A full-sample mean or standard deviation is the leak that most often
survives review, because the slice handed to the function *is* bounded by `t` and looks
point-in-time -- while inside it, every row sees every other row.

**No partial windows.** An observation without a full lookback behind it produces no
point at all, rather than a value computed from whatever history happened to be loaded.
A partial-window value is not a smaller version of the real one: it is a different
statistic, computed from a sample size that varies with where the caller started
reading, and no live run would ever have had it.

**Each function is self-contained.** They share no helper, which reads as duplication
and is not: `definition_digest` hashes one function's own syntax tree, so a change to a
shared helper would alter what every feature computes while leaving every digest --
and therefore every version number -- untouched. Validation of the input series happens
once, at the registry boundary in `evaluate`, so the loops here can trust their input.

`float` appears below, which is the sanctioned exception in
`docs/rules/decimal-and-money.md`: a standard deviation of returns is an estimate
whose sampling error is many orders of magnitude larger than 2^-53. The conversion
happens at a named boundary in one direction and comes back through `Decimal(str(...))`,
never implicitly mid-expression, and what leaves this module is always `Decimal`.
"""

from __future__ import annotations

import statistics
from collections.abc import Sequence
from decimal import Decimal

from fking.data.features.spec import FeatureObservation, FeaturePoint, FeatureWindow

__all__ = ["trailing_realised_volatility", "trailing_return_fraction"]


def trailing_return_fraction(
    observations: Sequence[FeatureObservation], window: FeatureWindow
) -> tuple[FeaturePoint, ...]:
    """Simple return over the declared lookback, as a dimensionless fraction.

    The base of each return is the newest observation at or before `t - lookback`, so
    the window is `(t - lookback, t]` and both endpoints are closed bars that had
    already happened at `t`. An observation with no such base -- the leading edge of the
    series -- yields no point.

    Exact throughout: a return is a ratio of two prices, both of which are `Decimal`,
    and there is no estimate here whose error would dwarf the arithmetic's.
    """
    points: list[FeaturePoint] = []
    for index, observation in enumerate(observations):
        window_start = observation.event_time_utc - window.lookback
        base: FeatureObservation | None = None
        for candidate in reversed(observations[:index]):
            if candidate.event_time_utc <= window_start:
                base = candidate
                break
        if base is None:
            continue
        points.append(
            FeaturePoint(
                event_time_utc=observation.event_time_utc,
                available_at_utc=observation.event_time_utc + window.availability_lag,
                feature_value=observation.close_quote_price / base.close_quote_price - Decimal("1"),
            )
        )
    return tuple(points)


def trailing_realised_volatility(
    observations: Sequence[FeatureObservation], window: FeatureWindow
) -> tuple[FeaturePoint, ...]:
    """Sample standard deviation of successive simple returns inside the lookback.

    The window is `(t - lookback, t]` and the returns are between consecutive
    observations inside it, so the earliest return uses one observation from before the
    window as its base -- which is data that also already existed at `t`.

    Two or more returns are required, because a sample standard deviation of one
    observation is undefined and a zero substituted for it would report a perfectly
    calm market on the thinnest possible evidence.
    """
    points: list[FeaturePoint] = []
    for index, observation in enumerate(observations):
        window_start = observation.event_time_utc - window.lookback
        first_inside = index
        while first_inside > 0 and observations[first_inside - 1].event_time_utc > window_start:
            first_inside -= 1
        if first_inside == 0:
            # No observation at or before the window start, so the oldest return inside
            # the window has no base and the window is not full.
            continue
        returns = [
            float(
                observations[position].close_quote_price
                / observations[position - 1].close_quote_price
                - Decimal("1")
            )
            for position in range(first_inside, index + 1)
        ]
        # Bound inside the function body rather than at module scope, so that
        # `definition_digest` -- which hashes this function's own syntax tree -- moves if
        # it ever changes. A constant a feature depends on but does not contain is a
        # constant that can be edited without any version number noticing.
        minimum_returns_for_stdev = 2
        if len(returns) < minimum_returns_for_stdev:
            continue
        points.append(
            FeaturePoint(
                event_time_utc=observation.event_time_utc,
                available_at_utc=observation.event_time_utc + window.availability_lag,
                feature_value=Decimal(str(statistics.stdev(returns))),
            )
        )
    return tuple(points)
