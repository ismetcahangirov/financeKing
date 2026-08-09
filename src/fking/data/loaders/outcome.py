"""What a parse produced, and every named way a row failed to become a record.

`DATA_PIPELINE.md` section 4 states the requirement this module encodes:

> A run that reports only `rows_out` has hidden its rejections. Rejections are the
> interesting half of the output.

So `rejection_reasons` is a mapping keyed by reason and never a bare total, and the
reasons are a `StrEnum` rather than free strings. That is not tidiness. A rejection
counter becomes a Prometheus label the moment ingestion is instrumented, and a free
string produces a new time series every time someone rephrases a message -- which is
indistinguishable, on a dashboard, from a new failure appearing and the old one stopping.
"""

from __future__ import annotations

from collections.abc import Collection, Mapping
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
from enum import StrEnum
from types import MappingProxyType

from fking.data.format_resolver import EpochUnit
from fking.platform.errors import DataIntegrityError

__all__ = ["NormalizationResult", "RejectionReason", "refuse_above_ceiling"]


class RejectionReason(StrEnum):
    """Why one row was not turned into a record.

    Every member is reachable from a test. A reason with no test is a counter that reads
    zero forever, which is worse than an absent counter: an absent counter prompts the
    question, and a zero one answers it wrongly.
    """

    FIELD_COUNT = "field_count"
    """The row did not hold the declared number of columns."""

    EPOCH_NOT_INTEGER = "epoch_not_integer"
    """A timestamp field was not a base-10 integer."""

    EPOCH_OUT_OF_RANGE = "epoch_out_of_range"
    """A timestamp normalised outside the plausible window under the declared unit.

    Uniform across a file when the declared epoch unit is wrong, which is how trap 1
    surfaces here: every row is rejected, the rejection fraction reaches 1, and the
    fraction gate turns it into a refusal instead of a corrupt corpus.
    """

    DECIMAL_UNPARSEABLE = "decimal_unparseable"
    """A price or quantity field was not an exact finite decimal."""

    PRICE_NOT_POSITIVE = "price_not_positive"
    """A price was zero or negative, which no traded print can be."""

    VOLUME_NEGATIVE = "volume_negative"
    """A volume was negative. Zero is legitimate -- an untraded minute is an observation."""

    OHLC_NOT_BRACKETING = "ohlc_not_bracketing"
    """High below, or low above, the open/close pair it must contain."""

    INTERVAL_NOT_FORWARD = "interval_not_forward"
    """A bar whose close_time does not follow its open_time."""

    TRADE_COUNT_NOT_INTEGER = "trade_count_not_integer"
    """The trade-count field was not a non-negative base-10 integer."""

    IDENTIFIER_BLANK = "identifier_blank"
    """A venue identifier field was empty, so the row has nothing to reconcile against."""

    BOOLEAN_UNRECOGNISED = "boolean_unrecognised"
    """A boolean field held a token the declared encoding does not define.

    This is trap 3's detector. `True`/`False` read under a lowercase comparison would
    otherwise become `False` on every row: counts right, prices right, volumes right, and
    the trade side uniformly inverted.
    """


@dataclass(frozen=True, slots=True)
class NormalizationResult:
    """The audit record of one file's parse.

    `first_event_time_utc` and `last_event_time_utc` are `None` only for a file with no
    data rows at all. That is a real observation -- an illiquid symbol can print nothing
    for a day -- and it is deliberately not conflated with a gap, which is a claim about
    data the archive should have held and did not (`DATA_PIPELINE.md` section 4).

    They are the first and last *accepted* rows, not the first and last lines. A result
    describing rows that were rejected would be describing data the caller does not have.
    """

    rows_in: int
    rows_out: int
    rows_rejected: int
    rejection_reasons: Mapping[RejectionReason, int]
    epoch_unit_applied: EpochUnit
    first_event_time_utc: datetime | None
    last_event_time_utc: datetime | None
    source_checksum_hex: str

    def __post_init__(self) -> None:
        # A frozen dataclass protects the binding, not the mapping bound to it. Without
        # the copy a caller holding the dict it passed in can still mutate the counts
        # inside an audit record.
        object.__setattr__(
            self, "rejection_reasons", MappingProxyType(dict(self.rejection_reasons))
        )

    @property
    def rejection_fraction(self) -> Decimal:
        """Rejected rows as a fraction of rows read; `0` for an empty file.

        A fraction in [0, 1], never a percent -- `docs/rules/naming.md` reserves
        `_pct` for 0-100 so that a bare `0.001` cannot be read as either 0.1% or 0.001%.
        """
        if self.rows_in == 0:
            return Decimal("0")
        return Decimal(self.rows_rejected) / Decimal(self.rows_in)

    def describe_rejections(self) -> str:
        """Per-reason counts, ordered by count, for an error message or a log field."""
        if not self.rejection_reasons:
            return "none"
        ordered = sorted(self.rejection_reasons.items(), key=lambda pair: (-pair[1], pair[0]))
        return ", ".join(f"{reason.value}={tally}/{self.rows_in}" for reason, tally in ordered)


def refuse_above_ceiling(
    outcome: NormalizationResult,
    *,
    ceiling: Decimal,
    source: str,
    reasons: Collection[RejectionReason] | None = None,
) -> None:
    """Raise when the rejected share of a file exceeds `ceiling`. Nothing partial survives.

    Lives here rather than in the parse driver because two callers need the same rule and
    the same message: the driver, which adjudicates every parse, and the ingestion quality
    gates, which adjudicate the reasons no numbered gate of their own owns
    (`DATA_PIPELINE.md` section 10). Two implementations of one threshold drift, and the
    one that drifts is the counter nobody watches.

    Args:
        outcome: The parse being adjudicated.
        ceiling: Rejected fraction tolerated, in [0, 1]. Never a percent.
        source: The file, for the message.
        reasons: Restrict the count to these reasons; `None` counts every rejection. A
            restricted call reports the *restricted* denominator honestly: it is a share of
            the rows read, not of the rows that failed for this reason.

    Raises:
        DataIntegrityError: the counted rejections exceed `ceiling * rows_in`.
    """
    if reasons is None:
        rejected = outcome.rows_rejected
        scope = "rows"
    else:
        rejected = sum(outcome.rejection_reasons.get(reason, 0) for reason in reasons)
        scope = f"rows failing {sorted(reason.value for reason in reasons)}"
    if rejected == 0:
        return
    # Multiplication rather than division: `rejected <= ceiling * rows_in` is exact in
    # Decimal, while `rejected / rows_in <= ceiling` rounds to context precision and can
    # put a value sitting exactly on the boundary onto the wrong side of it.
    if Decimal(rejected) <= ceiling * outcome.rows_in:
        return
    raise DataIntegrityError(
        f"archive member {source} rejected {rejected} of {outcome.rows_in} {scope}, above "
        f"the declared ceiling of {ceiling}: {outcome.describe_rejections()}. A rejection "
        f"rate this high is a format that has drifted rather than a few bad rows -- a wrong epoch "
        f"unit or a changed boolean encoding rejects every row identically. Nothing is "
        f"returned, because a partially ingested file parses cleanly and changes every "
        f"statistic computed from it"
    )
