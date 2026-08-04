"""The eleven data-quality gates, their thresholds, and the one exception they share.

`DATA_PIPELINE.md` section 10 is the specification. This module is its executable form:
one `Gate` member per row of that table, one function per gate, and a single
`QualityGateError` carrying the member that refused.

**A gate runs before the write.** A gate that runs after the write is a report, and
reports get read on Tuesdays. `fking.data.quality.ingest` is the ordering; this module is
the rules, so that a rule can be tested against a fixture without a filesystem anywhere
near it.

Three of the thresholds are deliberately not uniform, and the differences are the design
rather than an oversight:

- **Gate 9 flags and does not reject.** A 50% single-minute move on a thin altcoin is a
  real event. Rejecting it removes exactly the tail the risk engine most needs to have
  seen, and a gate that quietly discards unusual-but-real data biases every downstream
  volatility estimate toward calm -- which flatters every strategy this system exists to
  reject.
- **Gate 4 rejects the whole file, not the offending rows.** Out-of-order rows are a
  *symptom*: either the wrong epoch unit was applied (trap 1) or two files were merged
  upstream. Dropping the rows hides the cause and leaves a plausible file behind.
- **Gate 6 is ten times tighter than gate 5.** A drifted boolean encoding is uniform
  across a file and shows up at 100%, so 0.1% is a generous ceiling for it. An incoherent
  OHLC row is a corrupt print, and more than one in ten thousand means the corruption is
  systematic rather than incidental.

`Gate.REJECTION_CEILING` is the twelfth member and is not one of the eleven. It is
`IngestionSpec.max_rejection_fraction` applied to the reasons no numbered gate owns, so
that a file failing on a reason nobody wrote a gate for still refuses instead of passing
through the gaps between the gates that exist.
"""

from __future__ import annotations

import hashlib
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime, timedelta
from decimal import Decimal
from enum import StrEnum
from typing import Final

from fking.data.format_resolver import EpochUnit, epoch_to_utc
from fking.data.loaders import (
    ArchiveRecord,
    KlineRecord,
    NormalizationResult,
    RejectionReason,
    refuse_above_ceiling,
)
from fking.platform.errors import DataIntegrityError

__all__ = [
    "CADENCE_INTERVALS",
    "CONTINUITY_LOWER_RATIO",
    "CONTINUITY_UPPER_RATIO",
    "OHLC_REJECTION_CEILING",
    "CadenceGap",
    "Gate",
    "PriceContinuityFlag",
    "QualityGateError",
    "assert_bar_timestamps_are_monotone",
    "assert_boolean_rejections_within_ceiling",
    "assert_checksum_matches",
    "assert_first_timestamp_is_plausible",
    "assert_no_negative_volume",
    "assert_ohlc_rejections_within_ceiling",
    "assert_residual_rejections_within_ceiling",
    "detect_cadence_gaps",
    "flag_price_discontinuities",
]


class Gate(StrEnum):
    """One member per row of the `DATA_PIPELINE.md` section 10 table, plus the residual.

    A `StrEnum` rather than free strings for the same reason `RejectionReason` is one: a
    gate identifier becomes a Prometheus label the moment ingestion is instrumented, and a
    free string mints a new time series every time somebody rephrases a message -- which
    on a dashboard is indistinguishable from a new failure appearing while the old one
    stopped.
    """

    CHECKSUM = "checksum"
    HEADER_EXPECTATION = "header_expectation"
    FIRST_TIMESTAMP_PLAUSIBLE = "first_timestamp_plausible"
    MONOTONE_TIMESTAMPS = "monotone_timestamps"
    BOOLEAN_TOKENS = "boolean_tokens"
    OHLC_COHERENCE = "ohlc_coherence"
    NON_NEGATIVE_VOLUME = "non_negative_volume"
    BAR_CADENCE = "bar_cadence"
    PRICE_CONTINUITY = "price_continuity"
    CROSS_SOURCE_AGREEMENT = "cross_source_agreement"
    NO_SYNTHESISED_ROWS = "no_synthesised_rows"

    REJECTION_CEILING = "rejection_ceiling"
    """Not one of the eleven. The declared ceiling applied to the unowned reasons.

    Without it a new `RejectionReason` -- a column layout that changed upstream, a
    venue id that stopped being an integer -- would be counted, reported, and then
    allowed through, because every numbered gate above names the reasons it owns.
    """


class QualityGateError(DataIntegrityError):
    """A quality gate refused the file. Nothing partial is written.

    Defined here rather than in `fking.platform.errors` because it carries a `Gate`, and
    `platform` holds mechanism with no vocabulary from any other module
    (`.claude/rules/module-boundaries.md`). It is inside the taxonomy by inheritance: a
    handler for `DataIntegrityError` catches it, and terminal-not-retryable is exactly
    right -- re-reading the same bytes produces the same verdict.
    """

    def __init__(self, gate: Gate, message: str) -> None:
        super().__init__(f"gate {gate.value} refused: {message}")
        self.gate = gate


@dataclass(frozen=True, slots=True)
class _NamedCeiling:
    """One ceiling gate's rejection reason and the sentence that explains its refusal."""

    reason: RejectionReason
    consequence: str


# 0.01% -- gate 6, ten times tighter than the file-wide ceiling. An incoherent OHLC row is
# a corrupt print rather than a format drift, so one in ten thousand is the point at which
# "a few bad rows" stops being a credible explanation.
OHLC_REJECTION_CEILING: Final[Decimal] = Decimal("0.0001")

# exp(-0.5) and exp(0.5) to 30 significant figures. Gate 9 asks whether |log(p1/p0)| < 0.5,
# and comparing the price ratio against these two bounds answers exactly that question in
# exact Decimal arithmetic -- no float conversion, so the verdict does not depend on the
# rounding of a logarithm nobody inspects. Monotonicity of log makes the two statements
# identical; `.claude/rules/decimal-and-money.md` is why the float route was not taken even
# though the numeric exception would have permitted it.
CONTINUITY_LOWER_RATIO: Final[Decimal] = Decimal("0.606530659712633423603799534991")
CONTINUITY_UPPER_RATIO: Final[Decimal] = Decimal("1.64872127070012814684865078781")

# Declared, never inferred from the interval string. `1M` is a calendar month and has no
# fixed duration, so it is absent rather than approximated at 30 days -- and an absent
# entry raises, which is the same posture the format resolver takes toward an undeclared
# (market, dataset, date). Inferring is how trap 1 happened.
CADENCE_INTERVALS: Final[Mapping[str, timedelta]] = {
    "1s": timedelta(seconds=1),
    "1m": timedelta(minutes=1),
    "3m": timedelta(minutes=3),
    "5m": timedelta(minutes=5),
    "15m": timedelta(minutes=15),
    "30m": timedelta(minutes=30),
    "1h": timedelta(hours=1),
    "2h": timedelta(hours=2),
    "4h": timedelta(hours=4),
    "6h": timedelta(hours=6),
    "8h": timedelta(hours=8),
    "12h": timedelta(hours=12),
    "1d": timedelta(days=1),
    "3d": timedelta(days=3),
    "1w": timedelta(weeks=1),
}


@dataclass(frozen=True, slots=True)
class CadenceGap:
    """Bars the interval implies and the file does not hold. Recorded, never filled.

    Interior to the file: between the first and last bar it actually carries. A gap at
    either edge is a claim about the *corpus*, which spans files, and belongs to the
    coverage registry (`fking.data.backfill`) -- a single-file gate that reported edge gaps
    would report one
    on every file whose day begins with an untraded minute.

    No interpolation, no forward fill, no synthesised bars, ever
    (`DATA_PIPELINE.md` section 4). A synthetic bar has zero realised volatility and
    perfect mean reversion, which is catnip to exactly the strategies this system is
    trying to reject.
    """

    after_open_time_utc: datetime
    before_open_time_utc: datetime
    missing_bar_count: int


@dataclass(frozen=True, slots=True)
class PriceContinuityFlag:
    """A bar whose close moved more than `|log return| = 0.5` from its predecessor.

    A flag, never a rejection. The row is written. What this buys is that the move is
    *named* at ingestion, so an investigation months later does not have to rediscover it
    from the series, and so a genuinely wrong epoch unit -- which produces adjacent bars
    from different days -- shows up as a cluster of flags rather than as nothing.
    """

    open_time_utc: datetime
    previous_close_quote_price: Decimal
    close_quote_price: Decimal


def assert_checksum_matches(archive_bytes: bytes, *, expected_sha256_hex: str, source: str) -> None:
    """Gate 1. The bytes about to be parsed are the bytes that were verified.

    `ArchiveFetcher` already verified this file against its `.CHECKSUM` sibling, so a
    failure here means the bytes changed *after* verification: a truncated cache write, a
    partial read, a file swapped underneath us. That is defence in depth, and it is cheap.

    The failure this closes is not a corrupt file that fails to open. It is a **truncated**
    archive that parses cleanly for 80% of its rows and then ends, producing a day with a
    plausible row count, a plausible price range, and eleven missing hours nobody notices
    until a backtest six weeks later shows an implausibly clean edge in that window.

    Raises:
        QualityGateError: the digest of `archive_bytes` is not `expected_sha256_hex`.
    """
    observed = hashlib.sha256(archive_bytes).hexdigest()
    if observed == expected_sha256_hex:
        return
    raise QualityGateError(
        Gate.CHECKSUM,
        f"{source} hashes to {observed} but was verified as {expected_sha256_hex} "
        f"({len(archive_bytes)} bytes read). The bytes changed after verification, so they "
        f"are not the archive whose provenance this run would record. Re-fetch once; a "
        f"second mismatch is a data-integrity event affecting every backtest that consumed "
        f"the previous copy",
    )


def assert_first_timestamp_is_plausible(
    raw_epoch: str, *, unit: EpochUnit, now_utc: datetime, source: str
) -> datetime:
    """Gate 3. Stop on the first row rather than after rejecting every row.

    The row parser already rejects an out-of-range timestamp and the ceiling turns a
    file-wide failure into a refusal, so this gate changes no verdict. What it changes is
    *when* and *what is said*: a mis-declared epoch unit aborts on row one naming the raw
    magnitude and the unit applied, instead of after 1.4 million identical rejections whose
    message is a fraction. `DATA_PIPELINE.md` section 11 asks for exactly that -- "report
    the raw magnitude observed and the unit applied. The fix is the resolver, not the data".

    The window itself is `epoch_to_utc`'s, not a second copy of it. Two implementations of
    `[2010-01-01, now + 1 day)` would eventually disagree, and the disagreement would be a
    file this gate passed and every row of which the parser then rejected.

    Args:
        raw_epoch: The first data row's leading field, as the file wrote it.
        unit: The declared epoch unit for this `(market, dataset, date)`.
        now_utc: The run's one reference instant. Aware UTC.
        source: The file, for the message.

    Returns:
        The first timestamp, normalised.

    Raises:
        QualityGateError: the field is not an integer, or normalises outside the window.
    """
    try:
        ticks = int(raw_epoch)
    except ValueError as not_an_integer:
        raise QualityGateError(
            Gate.FIRST_TIMESTAMP_PLAUSIBLE,
            f"{source} opens with {raw_epoch!r}, which is not a base-10 integer, so no "
            f"epoch unit can be applied to it",
        ) from not_an_integer

    try:
        return epoch_to_utc(ticks, unit=unit, now_utc=now_utc)
    except DataIntegrityError as implausible:
        raise QualityGateError(
            Gate.FIRST_TIMESTAMP_PLAUSIBLE,
            f"{source} opens with an implausible timestamp: {implausible}. Nothing is "
            f"loaded, because a partial load of a file whose unit is wrong is worse than "
            f"none -- it parses cleanly and changes every statistic computed from it",
        ) from implausible


def assert_bar_timestamps_are_monotone(records: Sequence[ArchiveRecord], *, source: str) -> None:
    """Gate 4. Zero violations, and the whole file is refused rather than the rows.

    Rejecting the offending rows would leave a plausible file behind and hide the cause.
    Out-of-order rows are a symptom of one of two upstream events -- the wrong epoch unit
    applied to part of a file, or two files merged before publication -- and both invalidate
    every row, not only the ones that happen to appear late.

    Non-decreasing rather than strictly increasing: a trades archive legitimately prints
    several fills inside one microsecond, and requiring strict increase would refuse every
    liquid day.

    Raises:
        QualityGateError: any record's event time precedes its predecessor's.
    """
    for index in range(1, len(records)):
        previous = records[index - 1].event_time_utc
        current = records[index].event_time_utc
        if current >= previous:
            continue
        raise QualityGateError(
            Gate.MONOTONE_TIMESTAMPS,
            f"{source} row {index} is dated {current.isoformat()}, before row {index - 1} at "
            f"{previous.isoformat()}. Out-of-order rows mean the wrong epoch unit was applied "
            f"to part of this file or two files were merged upstream, so the whole file is "
            f"refused -- dropping the rows would hide the cause and leave a plausible file "
            f"behind",
        )


def assert_boolean_rejections_within_ceiling(
    outcome: NormalizationResult, *, ceiling: Decimal, source: str
) -> None:
    """Gate 5. Unrecognised boolean tokens below the declared ceiling, or the file fails.

    This is trap 3's detector at file scale. `True`/`False` read under a lowercase
    comparison rejects every row, so the fraction is 1 rather than something marginal --
    which is why a tight ceiling costs nothing and a loose one would still catch it.

    Raises:
        QualityGateError: the unrecognised-token share of rows read exceeds `ceiling`.
    """
    _refuse_named(outcome, gate=Gate.BOOLEAN_TOKENS, ceiling=ceiling, source=source)


def assert_ohlc_rejections_within_ceiling(outcome: NormalizationResult, *, source: str) -> None:
    """Gate 6. Incoherent bars below 0.01% of rows read, or the file fails.

    Ten times tighter than the file-wide ceiling on purpose. A bar whose high sits below
    its close is a corrupt print, not a format that drifted, and more than one in ten
    thousand means the corruption is systematic.

    Raises:
        QualityGateError: the incoherent-bar share of rows read exceeds 0.01%.
    """
    _refuse_named(outcome, gate=Gate.OHLC_COHERENCE, ceiling=OHLC_REJECTION_CEILING, source=source)


def assert_no_negative_volume(outcome: NormalizationResult, *, source: str) -> None:
    """Gate 7. Zero tolerated.

    Zero rather than a fraction because, unlike a boolean encoding or an epoch unit, there
    is no upstream change that makes a negative volume the *correct* reading of a file. It
    is a corrupt byte or a column that moved, and either invalidates the neighbouring
    columns of the same row -- which the row rejection has already discarded and which the
    gate now refuses to average away.

    Zero volume is legitimate and is not counted here: an untraded minute is an
    observation, and treating it as a fault is how a thin symbol becomes untestable.

    Raises:
        QualityGateError: any row was rejected for a negative volume.
    """
    negative = outcome.rejection_reasons.get(RejectionReason.VOLUME_NEGATIVE, 0)
    if negative == 0:
        return
    raise QualityGateError(
        Gate.NON_NEGATIVE_VOLUME,
        f"{source} holds {negative} row(s) with a negative volume out of {outcome.rows_in} "
        f"read. No upstream change makes that the correct reading of a file -- it is a "
        f"corrupt byte or a column that moved, and a moved column means the neighbouring "
        f"values are also being read as something they are not",
    )


def assert_residual_rejections_within_ceiling(
    outcome: NormalizationResult, *, ceiling: Decimal, source: str
) -> None:
    """The declared ceiling, applied to the reasons no numbered gate owns.

    Without this a rejection reason added later -- a column layout that changed upstream, a
    venue id that stopped being an integer -- would be counted, reported, and allowed
    through, because gates 5, 6 and 7 each name only their own reason. The set below is
    derived by subtraction rather than listed, so a new `RejectionReason` is covered on the
    day it is defined instead of on the day somebody remembers this file.

    Raises:
        DataIntegrityError: the unowned-reason share of rows read exceeds `ceiling`.
    """
    residual = tuple(reason for reason in RejectionReason if reason not in _OWNED_REASONS)
    refuse_above_ceiling(outcome, ceiling=ceiling, source=source, reasons=residual)


def detect_cadence_gaps(
    records: Sequence[ArchiveRecord], *, interval: str, source: str
) -> tuple[CadenceGap, ...]:
    """Gate 8. Report the bars the interval implies and the file does not hold.

    Records, never fills. A gap is information about the world -- an exchange outage, a
    maintenance window, a delisting, a genuine archive hole -- and filling it manufactures a
    price path that never traded.

    Assumes monotone input, which gate 4 has already established; running it first is the
    ordering `ingest` fixes, because a cadence report over unordered rows is arithmetic
    about a sequence that does not exist.

    Raises:
        QualityGateError: `interval` has no declared duration. Undeclared is not inferred.
    """
    duration = CADENCE_INTERVALS.get(interval)
    if duration is None:
        raise QualityGateError(
            Gate.BAR_CADENCE,
            f"{source} declares interval {interval!r}, whose bar duration is not declared in "
            f"CADENCE_INTERVALS ({sorted(CADENCE_INTERVALS)}). Inferring one from the string "
            f"is how a calendar month becomes 30 days and every cadence report after it is "
            f"quietly wrong",
        )

    gaps: list[CadenceGap] = []
    for index in range(1, len(records)):
        previous = records[index - 1].event_time_utc
        current = records[index].event_time_utc
        elapsed = current - previous
        if elapsed <= duration:
            continue
        missing, remainder = divmod(elapsed, duration)
        gaps.append(
            CadenceGap(
                after_open_time_utc=previous,
                before_open_time_utc=current,
                # `missing` counts the whole intervals spanned; one of them is the bar that
                # did arrive. A ragged remainder means the series is off-lattice as well as
                # short, and the count stays a count rather than becoming a fraction.
                missing_bar_count=int(missing) - 1 + (1 if remainder else 0),
            )
        )
    return tuple(gaps)


def flag_price_discontinuities(
    records: Sequence[KlineRecord], *, source: str
) -> tuple[PriceContinuityFlag, ...]:
    """Gate 9. Flag `|log return| >= 0.5` between consecutive closes. Never reject.

    `source` is accepted and unused by design: every other gate names the file it refused,
    and a reporting gate whose signature could not name its file would be the one that gets
    called with the wrong records when a caller loops over two datasets.
    """
    del source
    flagged: list[PriceContinuityFlag] = []
    for index in range(1, len(records)):
        previous = records[index - 1].close_quote_price
        current = records[index].close_quote_price
        if previous <= 0:  # pragma: no cover - the row parser rejects a non-positive price
            continue
        ratio = current / previous
        if CONTINUITY_LOWER_RATIO < ratio < CONTINUITY_UPPER_RATIO:
            continue
        flagged.append(
            PriceContinuityFlag(
                open_time_utc=records[index].open_time_utc,
                previous_close_quote_price=previous,
                close_quote_price=current,
            )
        )
    return tuple(flagged)


# Which rejection reason each ceiling gate adjudicates, and what it means when it fires.
# One table rather than four literals scattered through the functions above, because
# `_OWNED_REASONS` is derived from it: a numbered gate cannot claim a reason without the
# residual ceiling simultaneously ceasing to claim it, so no reason is adjudicated twice
# and none falls between the two.
_CEILING_GATES: Final[Mapping[Gate, _NamedCeiling]] = {
    Gate.BOOLEAN_TOKENS: _NamedCeiling(
        reason=RejectionReason.BOOLEAN_UNRECOGNISED,
        consequence=(
            "a boolean encoding that drifted upstream inverts the aggressor side on every "
            "row while counts, prices and volumes all stay correct"
        ),
    ),
    Gate.OHLC_COHERENCE: _NamedCeiling(
        reason=RejectionReason.OHLC_NOT_BRACKETING,
        consequence=(
            "a bar whose high does not bracket its own open and close describes a price "
            "path that did not happen, and a range computed from it is wrong in the "
            "direction that makes a strategy look better"
        ),
    ),
}

# Every reason a numbered gate owns. Gate 7 owns its reason with a threshold of zero
# rather than a ceiling, so it is added here rather than to the table above.
_OWNED_REASONS: Final[frozenset[RejectionReason]] = frozenset(
    {named.reason for named in _CEILING_GATES.values()} | {RejectionReason.VOLUME_NEGATIVE}
)


def _refuse_named(
    outcome: NormalizationResult, *, gate: Gate, ceiling: Decimal, source: str
) -> None:
    """Apply the shared ceiling rule to one gate's reason and relabel its refusal."""
    named = _CEILING_GATES[gate]
    try:
        refuse_above_ceiling(outcome, ceiling=ceiling, source=source, reasons=(named.reason,))
    except DataIntegrityError as above:
        raise QualityGateError(gate, f"{above}. Why it matters: {named.consequence}") from above
