"""The eleven data-quality gates from `DATA_PIPELINE.md` section 10, and the write they block.

Gates run at ingestion and **before the write**. A gate that runs after the write is a
report, and reports get read on Tuesdays -- by which time the corpus already holds the
rows, every statistic computed since was computed from them, and the fix is a re-derivation
rather than a refusal.

The layout:

| Module | Holds |
|---|---|
| `gates` | The `Gate` enum, `QualityGateError`, and one function per gate 1-9 |
| `ingest` | The ordering: bytes, gates, parse, gates, write. The only path to the corpus |
| `cross_source` | Gate 10, which needs two sources and so cannot run inside one file's parse |
| `standing` | Gate 11, a query about the whole store rather than about any one file |

Three of the gates decline to do the obvious thing, and each refusal is load-bearing:

- **Gate 9 flags rather than rejects.** A 50% single-minute move on a thin altcoin is a
  real event, and a gate that discards unusual-but-real data biases every downstream
  volatility estimate toward calm -- which flatters exactly the strategies this system
  exists to reject.
- **Gate 4 refuses the whole file rather than the offending rows.** Out-of-order rows are a
  symptom of a wrong epoch unit or a merged file upstream; dropping them hides the cause
  and leaves a plausible file behind.
- **Gate 10 escalates rather than merging.** Archive and stream were both believed correct
  five seconds ago. Picking one silently is how the schema revision, epoch-unit change or
  symbol rename that caused the divergence stops being investigated.
"""

from __future__ import annotations

from fking.data.quality.cross_source import assert_cross_source_agreement
from fking.data.quality.gates import (
    CADENCE_INTERVALS,
    CONTINUITY_LOWER_RATIO,
    CONTINUITY_UPPER_RATIO,
    OHLC_REJECTION_CEILING,
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
from fking.data.quality.ingest import IngestionOutcome, gate_archive, ingest_archive
from fking.data.quality.standing import (
    SynthesisedRowReport,
    assert_no_synthesised_rows,
    count_synthesised_rows,
)

__all__ = [
    "CADENCE_INTERVALS",
    "CONTINUITY_LOWER_RATIO",
    "CONTINUITY_UPPER_RATIO",
    "OHLC_REJECTION_CEILING",
    "CadenceGap",
    "Gate",
    "IngestionOutcome",
    "PriceContinuityFlag",
    "QualityGateError",
    "SynthesisedRowReport",
    "assert_bar_timestamps_are_monotone",
    "assert_boolean_rejections_within_ceiling",
    "assert_checksum_matches",
    "assert_cross_source_agreement",
    "assert_first_timestamp_is_plausible",
    "assert_no_negative_volume",
    "assert_no_synthesised_rows",
    "assert_ohlc_rejections_within_ceiling",
    "assert_residual_rejections_within_ceiling",
    "count_synthesised_rows",
    "detect_cadence_gaps",
    "flag_price_discontinuities",
    "gate_archive",
    "ingest_archive",
]
