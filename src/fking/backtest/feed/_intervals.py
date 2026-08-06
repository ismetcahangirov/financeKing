"""How long one bar of a named interval lasts, declared rather than parsed.

The table is what turns a window into a *lattice* -- the exact set of open times the
corpus must hold for the window to be servable -- and the lattice is what makes a gap a
computable fact rather than an impression. Everything the coverage gate says about missing
bars is derived from here.

Parsing `"1m"` into `timedelta(minutes=1)` with a regex was the alternative and is wrong
for one reason: it also accepts `"1M"`, `"1w"` and `"3d"`, and the first two do not have a
fixed duration. A calendar month is 28 to 31 days, so a lattice built by adding a fixed
`timedelta` drifts against the venue's own bar boundaries and reports gaps that are not
there -- for a while, and then reports real ones as present. So the table declares only the
intervals whose duration is constant, and an interval outside it raises rather than being
approximated. The two Binance intervals deliberately absent are `1w` and `1M`; `3d` is
absent for a narrower reason, that nothing in this project has ingested one and an
un-ingested interval on this table is a lattice nobody has checked against a real archive.
"""

from __future__ import annotations

from collections.abc import Mapping
from datetime import timedelta
from typing import Final

from fking.backtest.feed._errors import FeedRequestError

__all__ = ["BAR_INTERVALS", "interval_duration"]

BAR_INTERVALS: Final[Mapping[str, timedelta]] = {
    "1m": timedelta(minutes=1),
    "3m": timedelta(minutes=3),
    "5m": timedelta(minutes=5),
    "15m": timedelta(minutes=15),
    "30m": timedelta(minutes=30),
    "1h": timedelta(hours=1),
    "2h": timedelta(hours=2),
    "4h": timedelta(hours=4),
    "6h": timedelta(hours=6),
    "8h": timedelta(hours=8),
    "12h": timedelta(hours=12),
    "1d": timedelta(days=1),
}
"""Every kline interval whose duration is a constant, keyed as the archive names it."""


def interval_duration(bar_interval: str) -> timedelta:
    """The duration of one `bar_interval`, or a refusal.

    Raises:
        FeedRequestError: the interval is not one this project builds a lattice on.
    """
    duration = BAR_INTERVALS.get(bar_interval)
    if duration is None:
        raise FeedRequestError(
            f"bar interval {bar_interval!r} has no declared duration; declared intervals are "
            f"{sorted(BAR_INTERVALS)}. Weekly and monthly bars are absent because their "
            f"duration is not constant, and a lattice built from a fixed timedelta drifts "
            f"against the venue's own boundaries rather than failing"
        )
    return duration
