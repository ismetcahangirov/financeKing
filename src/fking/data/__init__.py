"""Ingestion, storage and the feature store. Knows about sources, schemas and
point-in-time semantics.

The distinction this module exists to keep straight is `event_time` versus
`available_at`: when a thing happened, versus the earliest instant this system could
have known it. Only the second governs visibility, and filtering on the first is the
single most common form of look-ahead.

The second distinction, and the one every parser here will depend on, is that **format
is a property of `(market, dataset, date)`, never of the codebase**. Anything that looks
like a module-level parsing constant is the epoch-unit trap waiting to recur, so
`format_resolver` is the only place a format decision is made and it refuses to guess.
"""

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
    "ArchiveFormat",
    "BooleanEncoding",
    "Dataset",
    "EpochUnit",
    "Market",
    "epoch_to_utc",
    "resolve_archive_format",
)
