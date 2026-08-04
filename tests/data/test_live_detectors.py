"""The two detectors, including the case each one is blind to.

The last pair of tests is the point of the file. `DATA_PIPELINE.md` section 5 requires
both detectors and says they fail differently; a suite that only proved each one finds
its own gap would not show that either was necessary. So the silent-stream test asserts
the cadence detector reports **and** that the sequence detector reports nothing, which is
the evidence that one is not a cheaper version of the other.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest
from hypothesis import given
from hypothesis import strategies as st

from fking.data.backfill.registry import GapKind
from fking.data.live.detectors import (
    CADENCE_GRACE,
    CadenceGapDetector,
    SequenceGapDetector,
)
from fking.platform.errors import DataIntegrityError

pytestmark = pytest.mark.unit

MINUTE = timedelta(minutes=1)
T0 = datetime(2026, 8, 4, 12, 0, tzinfo=UTC)

# A real aggregate trade id from tests/fixtures/streams/, so the arithmetic below runs
# on the magnitude the venue actually assigns rather than on a toy integer.
FIRST_AGG_ID = 6209562
BASELINE_AGG_ID = 100
MISSING_PRINTS = 3
MISSING_PRINTS_SHARING_AN_INSTANT = 2
SILENT_MINUTES = 10
SPLIT_RUN_LENGTH = 2


def test_the_first_print_establishes_a_baseline_and_reveals_nothing() -> None:
    """A session starting mid-tape has no left edge; inventing one would report every
    restart as a loss of every trade since genesis."""
    detector = SequenceGapDetector()
    assert detector.observe(FIRST_AGG_ID, T0) is None
    assert detector.last_id == FIRST_AGG_ID


def test_a_skipped_id_is_reported_within_one_message_with_its_exact_size() -> None:
    detector = SequenceGapDetector()
    detector.observe(BASELINE_AGG_ID, T0)
    gap = detector.observe(BASELINE_AGG_ID + MISSING_PRINTS + 1, T0 + timedelta(seconds=2))

    assert gap is not None
    assert gap.gap_kind is GapKind.SEQUENCE
    assert gap.missing_bar_count == MISSING_PRINTS  # 101, 102, 103
    assert gap.gap_start_utc == T0
    assert gap.gap_end_utc == T0 + timedelta(seconds=2)


def test_contiguous_ids_report_nothing() -> None:
    detector = SequenceGapDetector()
    detector.observe(BASELINE_AGG_ID, T0)
    assert detector.observe(BASELINE_AGG_ID + 1, T0 + timedelta(milliseconds=1)) is None


def test_a_repeated_id_is_not_a_gap() -> None:
    detector = SequenceGapDetector()
    detector.observe(BASELINE_AGG_ID, T0)
    assert detector.observe(BASELINE_AGG_ID, T0 + timedelta(milliseconds=5)) is None


def test_a_gap_between_two_prints_sharing_an_instant_still_has_positive_width() -> None:
    """Event times are milliseconds, so two prints can share one. A zero-width gap is
    rejected by `coverage_gap`'s own CHECK, and the smallest region the missing prints
    can honestly be said to occupy is that millisecond."""
    detector = SequenceGapDetector()
    detector.observe(BASELINE_AGG_ID, T0)
    gap = detector.observe(BASELINE_AGG_ID + MISSING_PRINTS_SHARING_AN_INSTANT + 1, T0)

    assert gap is not None
    assert gap.gap_end_utc > gap.gap_start_utc
    assert gap.duration == timedelta(milliseconds=1)
    assert gap.missing_bar_count == MISSING_PRINTS_SHARING_AN_INSTANT


def test_an_id_going_backwards_stops_rather_than_reporting_a_gap() -> None:
    detector = SequenceGapDetector()
    detector.observe(BASELINE_AGG_ID, T0)
    with pytest.raises(DataIntegrityError, match="went backwards"):
        detector.observe(BASELINE_AGG_ID - 1, T0 + timedelta(seconds=1))


def test_a_bar_arriving_on_time_produces_no_cadence_gap() -> None:
    """The first interval a session can be held to is the one it started inside."""
    detector = CadenceGapDetector(interval=MINUTE, started_at_utc=T0)
    detector.observe(T0)
    detector.observe(T0 + MINUTE)
    assert detector.poll(T0 + MINUTE + CADENCE_GRACE) == ()


def test_a_silent_minute_is_reported_once_its_grace_window_expires() -> None:
    detector = CadenceGapDetector(interval=MINUTE, started_at_utc=T0)
    # 91 seconds of silence: one second past the 90s grace on the minute that opened at
    # T0, whose bar was published at T0+60s and never arrived.
    gaps = detector.poll(T0 + timedelta(seconds=91))

    assert len(gaps) == 1
    assert gaps[0].gap_kind is GapKind.CADENCE
    assert gaps[0].missing_bar_count == 1
    assert gaps[0].gap_start_utc == T0
    assert gaps[0].gap_end_utc == T0 + MINUTE


def test_a_minute_still_inside_its_grace_window_is_not_reported() -> None:
    """Reporting at 89 seconds would make ordinary publication jitter look like a gap,
    which is how a detector gets its threshold widened until it stops detecting."""
    detector = CadenceGapDetector(interval=MINUTE, started_at_utc=T0)
    assert detector.poll(T0 + timedelta(seconds=89)) == ()


def test_a_silent_stream_reports_nothing_to_the_sequence_detector() -> None:
    """The case the sequence detector structurally cannot see: connected and silent.

    Ninety-one simulated seconds pass with no message at all. A detector driven by
    arrivals has nothing to compare, so it reports nothing -- which is exactly why the
    cadence detector exists and why one of the two is not redundant.
    """
    sequence = SequenceGapDetector()
    cadence = CadenceGapDetector(interval=MINUTE, started_at_utc=T0)
    sequence.observe(BASELINE_AGG_ID, T0)

    silent_until = T0 + timedelta(seconds=91)
    assert cadence.poll(silent_until) != ()
    assert sequence.last_id == BASELINE_AGG_ID


def test_a_run_of_silent_minutes_is_one_gap_with_an_exact_count() -> None:
    """One outage, not ten. Ten rows would each carry their own discovery instant and
    describe one event as ten."""
    detector = CadenceGapDetector(interval=MINUTE, started_at_utc=T0)
    gaps = detector.poll(T0 + (SILENT_MINUTES - 1) * MINUTE + CADENCE_GRACE)

    assert len(gaps) == 1
    assert gaps[0].missing_bar_count == SILENT_MINUTES
    assert gaps[0].gap_start_utc == T0
    assert gaps[0].gap_end_utc == T0 + SILENT_MINUTES * MINUTE


def test_an_observed_minute_splits_a_run_into_two_gaps() -> None:
    detector = CadenceGapDetector(interval=MINUTE, started_at_utc=T0)
    detector.observe(T0 + 2 * MINUTE)
    gaps = detector.poll(T0 + 4 * MINUTE + CADENCE_GRACE)

    assert [gap.missing_bar_count for gap in gaps] == [SPLIT_RUN_LENGTH, SPLIT_RUN_LENGTH]
    assert gaps[0].gap_start_utc == T0
    assert gaps[1].gap_start_utc == T0 + 3 * MINUTE


def test_the_same_minute_is_never_reported_twice() -> None:
    """The registry deduplicates by bounds, but a detector that re-reports would make
    every poll a write and every gap look freshly discovered."""
    detector = CadenceGapDetector(interval=MINUTE, started_at_utc=T0)
    first = detector.poll(T0 + timedelta(seconds=91))
    second = detector.poll(T0 + timedelta(seconds=95))

    assert len(first) == 1
    assert second == ()


def test_a_bar_at_or_below_the_watermark_is_discarded_rather_than_rejected() -> None:
    """A reconnect replays minutes the previous session already accounted for."""
    detector = CadenceGapDetector(interval=MINUTE, started_at_utc=T0)
    detector.poll(T0 + timedelta(seconds=91))
    detector.observe(T0)  # the minute already reported missing, arriving late
    assert detector.poll(T0 + timedelta(seconds=120)) == ()


@given(
    silent_minutes=st.integers(min_value=1, max_value=240),
    extra_seconds=st.integers(min_value=0, max_value=59),
)
def test_reported_cadence_gaps_always_account_for_exactly_the_elapsed_minutes(
    silent_minutes: int, extra_seconds: int
) -> None:
    """The count and the bounds must agree, at every silence length.

    An off-by-one here is invisible in a single example and permanent in a coverage
    report: `missing_bar_count` is what a backtest's availability check reads.
    """
    detector = CadenceGapDetector(interval=MINUTE, started_at_utc=T0)
    now = T0 + silent_minutes * MINUTE + CADENCE_GRACE + timedelta(seconds=extra_seconds)
    gaps = detector.poll(now)

    reported = sum(gap.missing_bar_count or 0 for gap in gaps)
    assert reported == sum(int(gap.duration / MINUTE) for gap in gaps)
    # silent_minutes + 1, because the interval the session started inside is the first
    # one it can be held to and `extra_seconds` never reaches the next boundary.
    assert reported == silent_minutes + 1
    # The watermark lands on the newest interval whose grace has expired, and never past
    # it: an overshoot would silently excuse the next minute from ever being checked.
    assert detector.watermark_open_time_utc <= now - CADENCE_GRACE
    assert detector.watermark_open_time_utc + MINUTE > now - CADENCE_GRACE
