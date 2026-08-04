"""Bytes to a written Parquet file, with every gate in front of the write.

The ordering here is the whole point of the module. `DATA_PIPELINE.md` section 10 opens
with "Gates run at ingestion and block the write. A gate that runs after the write is a
report, not a gate", and a report is read on Tuesdays. So there is exactly one function
that turns verified archive bytes into a file on disk, and it cannot reach the write
without having passed every blocking gate.

The order is not arbitrary. Each gate is placed at the earliest point its inputs exist, so
that the cheapest and most diagnostic refusal happens first:

1. **Checksum** (gate 1) needs only the bytes, and a truncated file must not be unzipped.
2. **Header** (gate 2) needs the first line, and a header misread shifts every column.
3. **First timestamp** (gate 3) needs one field, and a wrong epoch unit is worth catching
   on row one rather than after 1.4 million identical rejections.
4. **Parse**, which rejects rows and tallies reasons but adjudicates nothing.
5. **Monotone** (gate 4) needs the records, and it precedes cadence because a gap report
   over unordered rows is arithmetic about a sequence that does not exist.
6. **Boolean / OHLC / volume / residual ceilings** (gates 5, 6, 7 and the declared
   ceiling), each naming which threshold it exceeded.
7. **Cadence and continuity** (gates 8 and 9), which report and never refuse.
8. **Write.**

Step 4 hands the parser a ceiling of 1 and the gates adjudicate instead. The refusals are
identical -- the declared ceiling still governs, applied by
`assert_residual_rejections_within_ceiling` to every reason no numbered gate owns -- but
the message now names *which* gate and *which* reason, and "0.4% of rows were rejected"
cannot distinguish a drifted boolean encoding from a wrong epoch unit while
"boolean_unrecognised=1440/1440" can.

Gates 10 and 11 are absent here on purpose. Cross-source agreement needs two sources and
one file is one source; no-synthesised-rows is a question about the whole store. They live
in `cross_source` and `standing` and run on their own cadence.
"""

from __future__ import annotations

import dataclasses
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
from pathlib import Path
from typing import Final

from fking.data.format_resolver import Dataset
from fking.data.loaders import (
    KLINE_COLUMNS,
    TRADE_COLUMNS,
    ArchiveRecord,
    HeaderExpectationError,
    IngestionSpec,
    KlineRecord,
    NormalizationResult,
    extract_single_member,
    parse_archive,
)
from fking.data.loaders.source import split_rows
from fking.data.parquet.schema import RecordSource
from fking.data.parquet.writer import WriteOutcome, write_records
from fking.data.quality.gates import (
    CadenceGap,
    Gate,
    PriceContinuityFlag,
    QualityGateError,
    assert_bar_timestamps_are_monotone,
    assert_boolean_rejections_within_ceiling,
    assert_checksum_matches,
    assert_first_timestamp_is_plausible,
    assert_no_negative_volume,
    assert_ohlc_rejections_within_ceiling,
    assert_residual_rejections_within_ceiling,
    detect_cadence_gaps,
    flag_price_discontinuities,
)

__all__ = ["IngestionOutcome", "gate_archive", "ingest_archive"]

# The parser reports; the gates adjudicate. See the module docstring: a ceiling of 1 does
# not relax the declared threshold, it relocates the decision to the gate that can name
# which reason exceeded which limit.
_REPORT_ONLY_CEILING: Final[Decimal] = Decimal("1")


@dataclass(frozen=True, slots=True)
class _DatasetLayout:
    """What gates 2 and 3 need to know about a dataset's columns.

    `epoch_column_index` is declared per dataset and is not zero everywhere: a kline row
    opens with its `open_time`, but a trade row opens with its `trade_id` and carries the
    epoch fifth. Reading column 0 on a trades file normalises a nine-digit trade id as a
    timestamp, which lands in 1970 and refuses every genuine trades archive -- a gate that
    rejects only correct files, which is worse than no gate.
    """

    columns: tuple[str, ...]
    epoch_column_index: int


# Keyed the way the row parsers are keyed. Two column lists would let a header pass while
# every field afterwards was read one column across.
# The epoch index is looked up in the declared layout rather than written as a number, so
# a column list that changes shape moves this with it -- and a column that is renamed
# raises at import, which is where a layout change should be noticed.
_DATASET_LAYOUTS: Final[dict[Dataset, _DatasetLayout]] = {
    Dataset.KLINES: _DatasetLayout(
        columns=KLINE_COLUMNS, epoch_column_index=KLINE_COLUMNS.index("open_time")
    ),
    Dataset.TRADES: _DatasetLayout(
        columns=TRADE_COLUMNS, epoch_column_index=TRADE_COLUMNS.index("time")
    ),
}


@dataclass(frozen=True, slots=True)
class IngestionOutcome:
    """What one gated ingestion produced, including everything it chose not to refuse.

    `cadence_gaps` and `continuity_flags` are the two non-blocking gates' findings, and
    they are on the outcome rather than in a log line because they are inputs to decisions
    elsewhere: a backtest whose window contains a gap narrows its window or refuses to run
    (`BACKTEST_ENGINE.md`), and a continuity flag is the earliest visible symptom of an
    epoch unit that changed under a symbol.

    `write` is `None` on the `gate_archive` path, which adjudicates without persisting.
    """

    normalization: NormalizationResult
    cadence_gaps: tuple[CadenceGap, ...]
    continuity_flags: tuple[PriceContinuityFlag, ...]
    write: WriteOutcome | None


def gate_archive(
    archive_bytes: bytes, spec: IngestionSpec, *, source: str
) -> tuple[tuple[ArchiveRecord, ...], IngestionOutcome]:
    """Run every ingestion gate over one archive and return what survived. Writes nothing.

    Separated from `ingest_archive` so that the gates can be exercised, and reasoned about,
    without a filesystem in the picture -- and so that a caller writing to somewhere other
    than the Parquet corpus cannot reach a write by a path that skipped them.

    Args:
        archive_bytes: The verified `.zip` as fetched.
        spec: The resolved format, the verified digest and the run's reference instant.
        source: A human-readable name for the file, used in every refusal message.

    Returns:
        The accepted records and the outcome, whose `write` is `None`.

    Raises:
        QualityGateError: any blocking gate refused.
        DataIntegrityError: the archive is unreadable, or the residual rejection share
            exceeds the declared ceiling.
    """
    assert_checksum_matches(
        archive_bytes, expected_sha256_hex=spec.source_checksum_hex, source=source
    )
    member = extract_single_member(archive_bytes, source=source)

    layout = _declared_layout(spec)
    try:
        rows = split_rows(
            member,
            source=source,
            has_header_row=spec.archive_format.has_header_row,
            columns=layout.columns,
        )
    except HeaderExpectationError as mismatch:
        raise QualityGateError(Gate.HEADER_EXPECTATION, str(mismatch)) from mismatch

    if rows and len(rows[0]) > layout.epoch_column_index:
        assert_first_timestamp_is_plausible(
            rows[0][layout.epoch_column_index],
            unit=spec.archive_format.epoch_unit,
            now_utc=spec.now_utc,
            source=source,
        )

    records, normalization = parse_archive(
        member,
        dataclasses.replace(spec, max_rejection_fraction=_REPORT_ONLY_CEILING),
        source=source,
    )

    assert_bar_timestamps_are_monotone(records, source=source)
    assert_boolean_rejections_within_ceiling(
        normalization, ceiling=spec.max_rejection_fraction, source=source
    )
    assert_ohlc_rejections_within_ceiling(normalization, source=source)
    assert_no_negative_volume(normalization, source=source)
    assert_residual_rejections_within_ceiling(
        normalization, ceiling=spec.max_rejection_fraction, source=source
    )

    interval = spec.coordinate.interval
    gaps = (
        detect_cadence_gaps(records, interval=interval, source=source)
        if interval is not None
        else ()
    )
    flags = flag_price_discontinuities(_klines_only(records), source=source)

    # The digest is the one carried by the spec, so the outcome states which bytes it
    # describes without the caller having to pair it with the fetch that produced them.
    return records, IngestionOutcome(
        normalization=normalization,
        cadence_gaps=gaps,
        continuity_flags=flags,
        write=None,
    )


def ingest_archive(
    archive_bytes: bytes,
    spec: IngestionSpec,
    *,
    source: str,
    write_root: Path,
    ingested_at_utc: datetime,
) -> IngestionOutcome:
    """Gate one archive and, only if every gate passed, write it to the Parquet corpus.

    The single sanctioned path from archive bytes to the corpus. Nothing partial survives a
    refusal: the write is the last statement in the function, so a gate that raises leaves
    the destination path absent rather than holding a plausible short file.

    Args:
        archive_bytes: The verified `.zip` as fetched.
        spec: The resolved format, the verified digest and the run's reference instant.
        source: A human-readable name for the file, used in every refusal message.
        write_root: The corpus root, typically `data/parquet`.
        ingested_at_utc: Wall time of the write, injected. Aware UTC.

    Returns:
        The outcome, with `write` populated.

    Raises:
        QualityGateError: any blocking gate refused; nothing was written.
        DataIntegrityError: the archive is unreadable, the residual rejection share
            exceeds the declared ceiling, or the batch is empty.
    """
    records, outcome = gate_archive(archive_bytes, spec, source=source)
    written = write_records(
        records,
        coordinate=spec.coordinate,
        source=RecordSource.ARCHIVE,
        ingested_at_utc=ingested_at_utc,
        root=write_root,
    )
    return dataclasses.replace(outcome, write=written)


def _declared_layout(spec: IngestionSpec) -> _DatasetLayout:
    """The column layout and epoch position the spec's dataset declares.

    Read from the table rather than passed in, so that gate 2 compares a declared header
    against the same layout the row parser will index into, and gate 3 reads the same
    column the row parser will read.
    """
    layout = _DATASET_LAYOUTS.get(spec.archive_format.dataset)
    if layout is None:  # pragma: no cover - resolve_archive_format refuses undeclared first
        raise QualityGateError(
            Gate.HEADER_EXPECTATION,
            f"no column layout is declared for dataset "
            f"{spec.archive_format.dataset.value!r}, so the header cannot be checked "
            f"against anything. A layout arrives with a recorded fragment, never before it",
        )
    return layout


def _klines_only(records: Sequence[ArchiveRecord]) -> tuple[KlineRecord, ...]:
    """The kline records in a batch.

    Gate 9 is defined on consecutive bar closes, and a trade print has no close. Filtering
    rather than branching on the dataset keeps the single-dataset invariant -- a batch holds
    one record type -- from becoming an assumption two call sites apart.
    """
    return tuple(record for record in records if isinstance(record, KlineRecord))
