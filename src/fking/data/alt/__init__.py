"""Alternative data sources: one adapter shape, each declaring what it is late by.

`fking.data.loaders` reads the market-data corpus, where a bar is late by the length of
its own interval. This package reads everything else -- funding rates, open interest, an
index, news, macro releases -- where the lateness is a publisher's choice and is measured
in hours or weeks rather than minutes.

The public surface is small on purpose:

- `AltSourceSpec` is the declaration. It refuses a non-positive `availability_lag`, and
  `point()` is the only path to an `AltPoint`, so `available_at_utc` is always derived
  from the declaration or from the source's own published release calendar.
- `registry` holds the five measured sources and says which are reachable today.
- `funding` parses the one archive-delivered payload this repository can read, and
  `ingest` is the composition: verified bytes in, `AltPoint`s written, with no step at
  which a caller could supply its own `available_at_utc`.
- `probe` discovers where a symbol's history actually starts, which is not where the
  contract was listed.
- `store` is the as-of read and the ingest-role write, over `alt_observations`.

`SOURCES.md` is the human-facing register of the same facts and carries the terms-of-service
position for each; `tests/data/test_alt_sources.py` asserts the two do not drift.
"""

from __future__ import annotations

from fking.data.alt.funding import FUNDING_RATE_COLUMNS, parse_funding_rate_archive
from fking.data.alt.ingest import PARSED_SOURCES, AltIngestOutcome, ingest_alt_period
from fking.data.alt.probe import ALT_MARKET, AltAvailability, probe_earliest_archive_date
from fking.data.alt.registry import (
    ALT_SOURCES,
    ARCHIVE_DATASET,
    ARCHIVE_GRANULARITY,
    registered,
)
from fking.data.alt.spec import (
    GLOBAL_SERIES,
    AltObservation,
    AltPoint,
    AltSeriesRef,
    AltSourceSpec,
    Delivery,
    Revision,
)
from fking.data.alt.store import (
    AltObservationWriter,
    AltSeries,
    AltStore,
    AltValue,
    PostgresAltStore,
)

__all__ = [
    "ALT_MARKET",
    "ALT_SOURCES",
    "ARCHIVE_DATASET",
    "ARCHIVE_GRANULARITY",
    "FUNDING_RATE_COLUMNS",
    "GLOBAL_SERIES",
    "PARSED_SOURCES",
    "AltAvailability",
    "AltIngestOutcome",
    "AltObservation",
    "AltObservationWriter",
    "AltPoint",
    "AltSeries",
    "AltSeriesRef",
    "AltSourceSpec",
    "AltStore",
    "AltValue",
    "Delivery",
    "PostgresAltStore",
    "Revision",
    "ingest_alt_period",
    "parse_funding_rate_archive",
    "probe_earliest_archive_date",
    "registered",
]
