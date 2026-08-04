"""Availability contracts for tests whose subject is something else.

Every `PostgresFeatureStore` needs one, because the contract is a required constructor
argument rather than an optional one (`fking.data.features.store`). A test about grants or
about the as-of bound still has to state what the corpus holds, and stating it inline in
each file would put the same six lines in four places -- where they would drift, and where
the drift would look like a deliberate difference.

Deliberately not a permissive stand-in. `permitting()` builds a declaration that really
covers the window the caller names, so a test that quietly widens its window fails here
rather than reading a series that the corpus, in the story the test is telling, does not
hold.
"""

from __future__ import annotations

from collections.abc import Sequence
from datetime import datetime

from fking.data.features.availability import (
    AvailabilityContract,
    AvailabilityDeclaration,
    AvailabilityGap,
    SeriesAddress,
)
from fking.data.format_resolver import Dataset, Market


def permitting(
    *,
    earliest_event_time_utc: datetime,
    latest_event_time_utc: datetime,
    market: Market = Market.SPOT,
    symbol: str = "BTCUSDT",
    dataset: Dataset = Dataset.KLINES,
    resolutions: tuple[str, ...] = ("1m",),
    known_gaps: Sequence[AvailabilityGap] = (),
) -> AvailabilityContract:
    """A contract holding exactly one series over exactly the range given."""
    return AvailabilityContract.of(
        [
            AvailabilityDeclaration(
                address=SeriesAddress(market=market, symbol=symbol, dataset=dataset),
                resolutions=resolutions,
                earliest_event_time_utc=earliest_event_time_utc,
                latest_event_time_utc=latest_event_time_utc,
                known_gaps=tuple(known_gaps),
                refuses_if_unavailable=True,
            )
        ]
    )
