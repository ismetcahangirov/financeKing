"""The shared parse loop: rows in, records and a `NormalizationResult` out.

One driver rather than one per dataset, because the parts that must not vary between
datasets are exactly the parts here: the conservation identity
`rows_in == rows_out + rows_rejected`, the per-reason tally, and the rejection-fraction
gate. A dataset that re-implemented this loop would eventually re-implement one of the
three differently, and the one it got wrong would be the counter that never fires.

The two failure levels are the design:

- A **row** that cannot be parsed is rejected and tallied under a named reason. One
  malformed print in 3.5 million must not stop a backfill.
- A **file** whose rejection fraction exceeds the declared ceiling raises, and nothing
  partial is returned. That is what makes a drifting boolean encoding or a mis-declared
  epoch unit loud: both are uniform across a file, so they take the fraction to 1.

Nothing is logged from here. The exception message carries the per-reason counts, and a log
line beside a raise would report the same failure twice at two severities -- which is how
an alert threshold ends up calibrated against a multiplier nobody knows about
(`.claude/rules/error-handling.md`).
"""

from __future__ import annotations

from collections.abc import Callable, Sequence
from dataclasses import dataclass

from fking.data.loaders._fields import RowRejected
from fking.data.loaders.outcome import (
    NormalizationResult,
    RejectionReason,
    refuse_above_ceiling,
)
from fking.data.loaders.records import ArchiveRecord
from fking.data.loaders.source import split_rows
from fking.data.loaders.spec import IngestionSpec

__all__ = ["DatasetParser", "parse_rows"]


@dataclass(frozen=True, slots=True)
class DatasetParser[RecordT: ArchiveRecord]:
    """A dataset's layout and its row parser, paired so neither can be used without the other.

    The pairing is the point. A column list used with the wrong row parser is a field-count
    check that passes and an index mapping that is wrong, which is the silent-remapping
    failure `source.split_rows` refuses at the header. Keeping them in one frozen object
    means the dispatch table cannot mismatch them.
    """

    columns: tuple[str, ...]
    parse_row: Callable[[Sequence[str], IngestionSpec], RecordT]


def parse_rows[RecordT: ArchiveRecord](
    payload: bytes,
    spec: IngestionSpec,
    parser: DatasetParser[RecordT],
    *,
    source: str,
) -> tuple[tuple[RecordT, ...], NormalizationResult]:
    """Parse one archive member's CSV bytes.

    Pure: no clock is read, no file is opened, nothing is written. The plausibility
    reference is `spec.now_utc`, so the same bytes and the same spec produce the same
    records forever -- which is what makes a fixture a regression test rather than a
    snapshot of one afternoon.

    Raises:
        DataIntegrityError: the header contradicts the declaration in either direction, a
            declared header does not match the layout, the payload is not decodable, or the
            rejection fraction exceeds `spec.max_rejection_fraction`.
    """
    rows = split_rows(
        payload,
        source=source,
        has_header_row=spec.archive_format.has_header_row,
        columns=parser.columns,
    )

    accepted: list[RecordT] = []
    tally: dict[RejectionReason, int] = {}
    for row in rows:
        try:
            accepted.append(parser.parse_row(row, spec))
        except RowRejected as rejected:
            tally[rejected.reason] = tally.get(rejected.reason, 0) + 1

    outcome = NormalizationResult(
        rows_in=len(rows),
        rows_out=len(accepted),
        rows_rejected=len(rows) - len(accepted),
        rejection_reasons=tally,
        epoch_unit_applied=spec.archive_format.require_epoch_unit(),
        first_event_time_utc=accepted[0].event_time_utc if accepted else None,
        last_event_time_utc=accepted[-1].event_time_utc if accepted else None,
        source_checksum_hex=spec.source_checksum_hex,
    )
    refuse_above_ceiling(outcome, ceiling=spec.max_rejection_fraction, source=source)
    return tuple(accepted), outcome
