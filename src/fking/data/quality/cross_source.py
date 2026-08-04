"""Gate 10: where an archive and a live stream cover the same instant, they must agree.

This is the gate that finds problems nobody predicted. Archive and stream usually agree.
When they do not, something upstream changed -- a schema revision, a new epoch unit, a
symbol renaming -- and this is the earliest possible warning, before either copy has been
used to compute anything.

**Its failure is an escalation, not a preference.** There is deliberately no "prefer the
archive" or "prefer the stream" resolution, because both sources were believed correct
five seconds ago and choosing between them silently is how the *reason* they diverged
stops being investigated. `DATA_PIPELINE.md` section 11 files a checksum that fails twice
under the same heading for the same reason.

The comparison is restricted to the overlap. An archive file covering a whole day and a
stream buffer covering four minutes disagree about 1,436 minutes for a reason that is not
a disagreement, and a gate that reported those would be turned off within a week.
"""

from __future__ import annotations

import dataclasses
from collections.abc import Mapping, Sequence
from datetime import datetime

from fking.data.loaders import ArchiveRecord
from fking.data.quality.gates import Gate, QualityGateError

__all__ = ["assert_cross_source_agreement"]


def assert_cross_source_agreement(
    archive_records: Sequence[ArchiveRecord],
    stream_records: Sequence[ArchiveRecord],
    *,
    source: str,
) -> int:
    """Compare the two sources over the instants they both cover. Returns the overlap size.

    Args:
        archive_records: Records parsed from a `data.binance.vision` archive.
        stream_records: Records captured from the live WebSocket feed, same coordinate.
        source: The coordinate under comparison, for the message.

    Returns:
        How many instants were actually compared. Zero is not a pass -- a caller that
        ignores it is running a gate over an empty intersection and reporting agreement.

    Raises:
        QualityGateError: an instant inside the overlap is present in one source and not
            the other, either source holds two records for one instant, or any field
            differs between the two.
    """
    archive_by_time = _index_by_event_time(archive_records, side="archive", source=source)
    stream_by_time = _index_by_event_time(stream_records, side="stream", source=source)
    if not archive_by_time or not stream_by_time:
        return 0

    first = max(min(archive_by_time), min(stream_by_time))
    last = min(max(archive_by_time), max(stream_by_time))
    if first > last:
        return 0

    compared = 0
    for event_time in sorted(instant for instant in archive_by_time if first <= instant <= last):
        compared += 1
        streamed = stream_by_time.get(event_time)
        if streamed is None:
            raise QualityGateError(
                Gate.CROSS_SOURCE_AGREEMENT,
                f"{source} at {event_time.isoformat()} is in the archive and absent from the "
                f"stream, inside the overlap [{first.isoformat()}, {last.isoformat()}]. One "
                f"source is missing a record the other observed, which is a coverage change "
                f"upstream rather than a difference of opinion about a value",
            )
        differences = _field_differences(archive_by_time[event_time], streamed)
        if differences:
            raise QualityGateError(
                Gate.CROSS_SOURCE_AGREEMENT,
                f"{source} at {event_time.isoformat()} differs between archive and stream on "
                f"{differences}. Neither copy is preferred: both were believed correct, so the "
                f"divergence is escalated rather than resolved -- a silent merge would leave "
                f"the schema revision, epoch-unit change or symbol rename that caused it "
                f"undiagnosed",
            )

    for event_time in stream_by_time:
        if first <= event_time <= last and event_time not in archive_by_time:
            raise QualityGateError(
                Gate.CROSS_SOURCE_AGREEMENT,
                f"{source} at {event_time.isoformat()} is in the stream and absent from the "
                f"archive, inside the overlap [{first.isoformat()}, {last.isoformat()}]. The "
                f"archive is the source a backtest reads, so a stream-only record is a hole "
                f"in the corpus rather than a surplus in the buffer",
            )
    return compared


def _index_by_event_time(
    records: Sequence[ArchiveRecord], *, side: str, source: str
) -> Mapping[datetime, ArchiveRecord]:
    """Records keyed by instant, refusing a duplicate rather than keeping the last one.

    Keeping the last would make the gate's verdict depend on iteration order, and the
    duplicate is itself the finding: two records for one instant means a file was merged
    with itself or a stream replayed a frame.
    """
    indexed: dict[datetime, ArchiveRecord] = {}
    for record in records:
        if record.event_time_utc in indexed:
            raise QualityGateError(
                Gate.CROSS_SOURCE_AGREEMENT,
                f"{source} {side} holds two records at {record.event_time_utc.isoformat()}; "
                f"a duplicated instant makes any agreement verdict depend on which copy was "
                f"compared",
            )
        indexed[record.event_time_utc] = record
    return indexed


def _field_differences(archived: ArchiveRecord, streamed: ArchiveRecord) -> str:
    """Every field on which the two records disagree, rendered for a message.

    Compared field by field rather than by `==` so the message names *which* field moved.
    "The bars differ" sends an investigator to read both rows; "close_quote_price differs"
    sends them to the column that changed.
    """
    if type(archived) is not type(streamed):
        return f"record type ({type(archived).__name__} vs {type(streamed).__name__})"
    return ", ".join(
        f"{field.name} ({getattr(archived, field.name)!r} vs {getattr(streamed, field.name)!r})"
        for field in dataclasses.fields(archived)
        if getattr(archived, field.name) != getattr(streamed, field.name)
    )
