"""A clock that only moves when a test moves it.

Two readings, deliberately separate. `monotonic_seconds` is what
`fking.execution.VenueRateGovernor` measures its windows with, and `now_utc` is what it
stamps incidents with -- keeping them independent is what lets a test prove the windows
do not depend on the wall clock, which is the property an NTP step would otherwise break
in production and nowhere else.

`advance` moves both by the same amount, because a test that let them diverge would be
asserting against a machine that cannot exist.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Final

__all__ = ["EPOCH_UTC", "FrozenClock"]

# An arbitrary but fixed instant. Fixed rather than `now()` so that a failure message
# quotes the same timestamps on every run and a flaky test cannot hide behind the date.
EPOCH_UTC: Final[datetime] = datetime(2026, 8, 11, 9, 0, tzinfo=UTC)


class FrozenClock:
    """An injected clock whose only source of movement is `advance`."""

    __slots__ = ("_elapsed_seconds",)

    def __init__(self) -> None:
        self._elapsed_seconds = 0.0

    def advance(self, seconds: float) -> None:
        if seconds < 0:
            raise ValueError("a monotonic clock cannot move backwards")
        self._elapsed_seconds += seconds

    def monotonic_seconds(self) -> float:
        """Elapsed seconds since the clock was created. Never decreases."""
        return self._elapsed_seconds

    def now_utc(self) -> datetime:
        """Wall time, timezone-aware UTC, moved only by `advance`."""
        return EPOCH_UTC + timedelta(seconds=self._elapsed_seconds)
