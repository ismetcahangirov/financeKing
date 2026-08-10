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

__all__ = [
    "bollinger_band_width_fraction",
    "bollinger_z_score",
    "donchian_channel_breakout_state",
    "trailing_realised_volatility",
    "trailing_return_fraction",
]


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


def donchian_channel_breakout_state(
    observations: Sequence[FeatureObservation], window: FeatureWindow
) -> tuple[FeaturePoint, ...]:
    """`1` at a new window high close, `-1` at a new window low close, `0` between them.

    The window is `(t - lookback, t]` and the extremes are taken over the closes inside
    it, the newest of which is the observation being stamped -- so the comparison is
    "is this close the highest one that has happened", never "the highest one that will".
    An observation with no predecessor at or before the window start yields no point.

    Ties resolve to a breakout: a close equal to the window high *is* the high, and the
    Donchian rule is an inclusive one. A window whose high and low are the same price is
    the one case where both tests pass at once, and it emits `0` -- a market that has not
    moved has no channel to break out of, and calling it a breakout in both directions at
    once would produce a signal from an absence of information.

    Exact throughout: an extreme is a comparison between two `Decimal` closes and a state
    is one of three exact values, so nothing here has an estimate's error to hide behind.
    """
    points: list[FeaturePoint] = []
    for index, observation in enumerate(observations):
        window_start = observation.event_time_utc - window.lookback
        first_inside = index
        while first_inside > 0 and observations[first_inside - 1].event_time_utc > window_start:
            first_inside -= 1
        if first_inside == 0:
            # No observation at or before the window start, so the window is not full and
            # the extremes would be taken over however much history the caller loaded.
            continue
        closes = [
            observations[position].close_quote_price for position in range(first_inside, index + 1)
        ]
        highest = max(closes)
        lowest = min(closes)
        if highest == lowest:
            state = Decimal("0")
        elif observation.close_quote_price == highest:
            state = Decimal("1")
        elif observation.close_quote_price == lowest:
            state = Decimal("-1")
        else:
            state = Decimal("0")
        points.append(
            FeaturePoint(
                event_time_utc=observation.event_time_utc,
                available_at_utc=observation.event_time_utc + window.availability_lag,
                feature_value=state,
            )
        )
    return tuple(points)


def bollinger_z_score(
    observations: Sequence[FeatureObservation], window: FeatureWindow
) -> tuple[FeaturePoint, ...]:
    """How many sample standard deviations the close sits from the window's mean close.

    The mean and the standard deviation are taken over the closes inside `(t - lookback,
    t]`, all of which had already happened at `t`, and the point being stamped is inside
    its own window -- which is what a Bollinger band is. A centred window, or one that
    standardised against the mean of the whole sample, is the same arithmetic reading the
    future, and it is the version that produces a beautiful equity curve.

    A window whose closes are all one price emits nothing rather than a zero: the
    dispersion the score divides by is zero there, and `0` would assert "exactly at the
    mean" on evidence that cannot distinguish that from any other position.
    """
    points: list[FeaturePoint] = []
    for index, observation in enumerate(observations):
        window_start = observation.event_time_utc - window.lookback
        first_inside = index
        while first_inside > 0 and observations[first_inside - 1].event_time_utc > window_start:
            first_inside -= 1
        if first_inside == 0:
            continue
        closes = [
            float(observations[position].close_quote_price)
            for position in range(first_inside, index + 1)
        ]
        # Bound inside the body rather than at module scope, so `definition_digest` moves
        # if it ever changes: a constant a feature depends on but does not contain is one
        # that can be edited without any version number noticing.
        minimum_closes_for_stdev = 2
        if len(closes) < minimum_closes_for_stdev:
            continue
        dispersion = statistics.stdev(closes)
        if dispersion == 0.0:
            continue
        points.append(
            FeaturePoint(
                event_time_utc=observation.event_time_utc,
                available_at_utc=observation.event_time_utc + window.availability_lag,
                feature_value=Decimal(
                    str(
                        (float(observation.close_quote_price) - statistics.fmean(closes))
                        / dispersion
                    )
                ),
            )
        )
    return tuple(points)


def bollinger_band_width_fraction(
    observations: Sequence[FeatureObservation], window: FeatureWindow
) -> tuple[FeaturePoint, ...]:
    """One Bollinger standard deviation, as a dimensionless fraction of the mean close.

    The same window and the same statistic `bollinger_z_score` divides by, expressed
    relative to the price level so that it can be compared across instruments and used as
    a distance. It is a separate feature rather than a second return value because a
    feature is one series with one lookback and one lag, and the two are read by different
    parts of a strategy: the score decides *whether*, the width decides *how far wrong*.

    A window with no dispersion emits `0` rather than nothing. Unlike the score there is
    no division by it here, and "the price did not move over the window" is a true
    statement about width that a consumer can act on -- by refusing to size against it.
    """
    points: list[FeaturePoint] = []
    for index, observation in enumerate(observations):
        window_start = observation.event_time_utc - window.lookback
        first_inside = index
        while first_inside > 0 and observations[first_inside - 1].event_time_utc > window_start:
            first_inside -= 1
        if first_inside == 0:
            continue
        closes = [
            float(observations[position].close_quote_price)
            for position in range(first_inside, index + 1)
        ]
        minimum_closes_for_stdev = 2
        if len(closes) < minimum_closes_for_stdev:
            continue
        points.append(
            FeaturePoint(
                event_time_utc=observation.event_time_utc,
                available_at_utc=observation.event_time_utc + window.availability_lag,
                feature_value=Decimal(str(statistics.stdev(closes) / statistics.fmean(closes))),
            )
        )
    return tuple(points)
