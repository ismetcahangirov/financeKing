"""Fetch one alternative archive, parse it, and write what it holds. One period at a time.

This is the composition the rest of the package exists to make safe: verified bytes in,
`AltPoint`s out, and no step anywhere at which a caller could supply its own
`available_at_utc`. The interesting property is what the signature *lacks* -- there is no
availability parameter, no lag override and no "skip the checksum" flag, so the only way
to write an observation is through the declaration that measured the lag.

**One archive period per call, and the period is a parameter.** A range loop belongs to
whatever is doing the ranging -- the backfill, the system beat, an operator -- because
those three want different failure behaviour from a missing month, and a loop in here
would have to pick one. A 404 is an ordinary answer for an alternative series, since the
archive begins months after the corpus does (VF-028), and `ArchiveUnavailableError`
propagates rather than being swallowed into a zero-row success.

**Idempotent by construction, not by a guard here.** The writer's `ON CONFLICT DO NOTHING`
means re-ingesting a month already held returns zero written, so a resumed backfill and a
duplicated schedule tick converge on the same state -- which is required, because Redis
Streams delivery is at-least-once and the beat can re-fire a claimed window.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from datetime import date, datetime
from types import MappingProxyType
from typing import Final, Protocol

import structlog

from fking.data.alt.funding import parse_funding_rate_archive
from fking.data.alt.metrics import parse_open_interest_archive
from fking.data.alt.probe import ALT_MARKET
from fking.data.alt.registry import (
    ARCHIVE_DATASET,
    ARCHIVE_GRANULARITY,
    BINANCE_FUNDING_RATE,
    BINANCE_OPEN_INTEREST,
    registered,
)
from fking.data.alt.spec import AltObservation, AltPoint, AltSeriesRef, require_utc
from fking.data.alt.store import AltObservationWriter
from fking.data.archive import ArchiveCoordinate, ArchiveFetcher
from fking.data.format_resolver import ArchiveFormat, resolve_archive_format
from fking.data.loaders import extract_single_member
from fking.platform.errors import DataUnavailableError

__all__ = ["PARSED_SOURCES", "AltIngestOutcome", "ingest_alt_period"]

_LOG = structlog.get_logger(__name__)


class AltArchiveParser(Protocol):
    """One archive member's bytes in, observations out. Pure: no clock, no file, no write.

    A `Protocol` rather than a `Callable[..., ...]` alias, because the alias would have to
    spell its parameters as `...` and `mypy --strict` reads that as an explicit `Any` --
    which would let a parser with the wrong keyword names into the table and fail at the
    call site instead of at registration.
    """

    def __call__(
        self,
        member_bytes: bytes,
        *,
        source: str,
        now_utc: datetime,
        archive_format: ArchiveFormat,
    ) -> tuple[AltObservation, ...]:
        """The docstring is the whole body; a trailing `...` after it does nothing."""


# Which sources this repository can read the payload of, as a table rather than a branch.
# Kept here rather than in the registry because "is this source declared" and "can this
# source's payload be read" are different questions -- the same split, for the same
# reason, as `fking.data.loaders._PARSERS` against `DECLARED_FORMATS`. A source can still
# acquire the first without the second; the three sources with no egress are exactly that
# case today, and they refuse by name rather than by silence.
_PARSERS: Final[Mapping[str, AltArchiveParser]] = MappingProxyType(
    {
        BINANCE_FUNDING_RATE.source_id: parse_funding_rate_archive,
        BINANCE_OPEN_INTEREST.source_id: parse_open_interest_archive,
    }
)

PARSED_SOURCES: Final[frozenset[str]] = frozenset(_PARSERS)
"""Sources `ingest_alt_period` can read. Everything else refuses, naming what is missing."""

# What the two archive-delivered sources have in common and where they differ is the whole
# argument for one protocol rather than two functions: `fundingRate` is monthly-only with a
# millisecond epoch, `metrics` is daily-only with a naive datetime string, and neither
# difference is visible in the bytes. Both come from the resolved declaration, so a parser
# holds only its own layout.


@dataclass(frozen=True, slots=True)
class AltIngestOutcome:
    """What one period's ingest did, including when it did nothing new.

    `observations_parsed` and `observations_written` are both reported because their
    difference is the answer to "is this corpus complete" -- a run that parsed 93
    settlements and wrote 0 recognised a month it already held, and a run that reports
    only one of the two numbers cannot tell those apart.
    """

    series: AltSeriesRef
    archive_date: date
    archive_url: str
    archive_sha256_hex: str
    observations_parsed: int
    observations_written: int


async def ingest_alt_period(
    fetcher: ArchiveFetcher,
    writer: AltObservationWriter,
    *,
    series: AltSeriesRef,
    archive_date: date,
    now_utc: datetime,
) -> AltIngestOutcome:
    """Ingest one archive period of one alternative series.

    `now_utc` is the timestamp plausibility reference for the whole parse, fixed once. A
    bound re-read per row drifts mid-file, and then the same raw integer can be accepted
    at the top of an archive and rejected at the bottom.

    Raises:
        DataUnavailableError: the source is unregistered, has no archive egress, or has
            no parser in this repository yet.
        ArchiveUnavailableError: the archive for that period is not published. Ordinary
            for a series that begins months after the corpus does, and propagated rather
            than reported as an empty success.
        DataIntegrityError: the checksum failed twice, or the payload contradicts the
            declared layout.
    """
    require_utc(now_utc, "now_utc")
    source_id = series.source_id
    source = registered(source_id)
    if source_id not in ARCHIVE_DATASET:
        raise DataUnavailableError(
            f"{source_id} is declared {source.delivery.value}, so there is no archive to "
            f"ingest. Its measured lag, cadence and terms are registered; reaching it is a "
            f"separate egress decision (docs/adr/0017)"
        )
    parse = _PARSERS.get(source_id)
    if parse is None:
        raise DataUnavailableError(
            f"{source_id} has an archive but no parser in this repository; parseable "
            f"sources are {sorted(PARSED_SOURCES)}. A parser arrives with a recorded "
            f"archive and a declared layout, never before them"
        )

    dataset = ARCHIVE_DATASET[source_id]
    # Resolved here rather than inside the parser: the date this archive covers is the
    # caller's fact, and a parser deriving it from the payload would be inferring the
    # declaration from the very bytes the declaration exists to interpret.
    archive_format = resolve_archive_format(
        market=ALT_MARKET, dataset=dataset, archive_date=archive_date
    )
    coordinate = ArchiveCoordinate(
        market=ALT_MARKET,
        dataset=dataset,
        symbol=series.series_id,
        archive_date=archive_date,
    )
    fetched = await fetcher.fetch(
        coordinate,
        # The archive's own date, because the granularity is stated rather than resolved:
        # `fundingRate` is monthly-only and `metrics` daily-only, so the calendar rule is
        # wrong for both in opposite directions (VF-029).
        today_utc=archive_date,
        granularity=ARCHIVE_GRANULARITY[source_id],
    )

    member = extract_single_member(fetched.path.read_bytes(), source=fetched.url)
    observations = parse(member, source=fetched.url, now_utc=now_utc, archive_format=archive_format)
    points: list[AltPoint] = [source.point(series, observation) for observation in observations]
    written = await writer.append(points)

    _LOG.info(
        "alt.period_ingested",
        source_id=source_id,
        series_id=series.series_id,
        archive_date=archive_date.isoformat(),
        url=fetched.url,
        sha256=fetched.sha256_hex,
        observations_parsed=len(observations),
        observations_written=written,
        served_from_cache=fetched.served_from_cache,
    )
    return AltIngestOutcome(
        series=series,
        archive_date=archive_date,
        archive_url=fetched.url,
        archive_sha256_hex=fetched.sha256_hex,
        observations_parsed=len(observations),
        observations_written=written,
    )
