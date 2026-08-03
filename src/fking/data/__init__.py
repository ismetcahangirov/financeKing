"""Ingestion, storage and the feature store. Knows about sources, schemas and
point-in-time semantics.

The distinction this module exists to keep straight is `event_time` versus
`available_at`: when a thing happened, versus the earliest instant this system could
have known it. Only the second governs visibility, and filtering on the first is the
single most common form of look-ahead.

The second distinction, and the one every parser here depends on, is that **format is a
property of `(market, dataset, date)`, never of the codebase**. Anything that looks like a
module-level parsing constant is the epoch-unit trap waiting to recur, so
`format_resolver` is the only place a format decision is made and it refuses to guess.

The parsers themselves are `fking.data.loaders`, a subpackage with its own surface. They
are not re-exported here: a caller wanting one names `fking.data.loaders`, which keeps this
namespace to the things every consumer of ingested data needs -- coordinates, formats,
epochs -- rather than the internals of turning bytes into records.
"""

from fking.data.archive import (
    ArchiveCoordinate,
    ArchiveFetcher,
    FetchedArchive,
    Granularity,
    resolve_granularity,
)
from fking.data.format_resolver import (
    ArchiveFormat,
    BooleanEncoding,
    Dataset,
    EpochUnit,
    Market,
    epoch_to_utc,
    resolve_archive_format,
)

__all__: tuple[str, ...] = (
    "ArchiveCoordinate",
    "ArchiveFetcher",
    "ArchiveFormat",
    "BooleanEncoding",
    "Dataset",
    "EpochUnit",
    "FetchedArchive",
    "Granularity",
    "Market",
    "epoch_to_utc",
    "resolve_archive_format",
    "resolve_granularity",
)
