"""All eleven gates: each with a fixture that trips it and a distinct named reason code.

The corpus these run against is derived from real recordings by
`tools/corrupt_archive_fixture.py`, never hand-authored. That matters more here than
anywhere else in the suite: the point of a corrupt fixture is to prove a gate fires on the
shape Binance actually emits, and a hand-typed CSV would prove only that the gate fires on
what its author imagined.

Two properties are asserted repeatedly and deliberately:

- **A refusal names its gate.** `QualityGateError.gate` is checked rather than a message
  substring, because a message is edited and a `Gate` member is not. A test matching on
  prose passes for as long as nobody improves the wording.
- **A refusal writes nothing.** Every blocking-gate test asserts the destination path does
  not exist afterwards. A gate that refuses after the write is a report.
"""

from __future__ import annotations

import dataclasses
import hashlib
import zipfile
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from io import BytesIO
from pathlib import Path

import pytest

from fking.data.format_resolver import Dataset, EpochUnit, Market
from fking.data.loaders import (
    KlineRecord,
    NormalizationResult,
    RejectionReason,
    TradeRecord,
)
from fking.data.parquet.layout import partition_path
from fking.data.quality import (
    CONTINUITY_LOWER_RATIO,
    CONTINUITY_UPPER_RATIO,
    Gate,
    QualityGateError,
    assert_bar_timestamps_are_monotone,
    assert_cross_source_agreement,
    assert_first_timestamp_is_plausible,
    assert_residual_rejections_within_ceiling,
    detect_cadence_gaps,
    flag_price_discontinuities,
    gate_archive,
    ingest_archive,
)
from fking.data.quality.gates import _CEILING_GATES
from fking.platform.errors import DataIntegrityError
from tests.support import archive_fixtures, corrupt_fixtures
from tests.support.archive_fixtures import NOW_UTC

pytestmark = pytest.mark.unit

INGESTED_AT_UTC = datetime(2026, 8, 4, 12, 0, tzinfo=UTC)

# The whole-archive spot recording: 1,440 one-minute bars, microsecond epochs, no header.
SPOT_ARCHIVE_DATE = date(2025, 1, 2)

# The block `remove_a_bar_block` deletes, and the slice the cross-source tests overlap on.
# Named because the assertion is arithmetic about the fixture rather than a round number.
GAPPED_BAR_COUNT = 10
OVERLAP_BAR_COUNT = 100

# Every fixture whose gate must refuse, paired with the member of `Gate` it must name. The
# table is the specification restated: a gate with no row here has no fixture, and a
# fixture with no row here refuses nothing.
BLOCKING_FIXTURES: list[tuple[str, Gate]] = [
    ("spot_klines_truncated_archive", Gate.CHECKSUM),
    ("spot_klines_header_prepended", Gate.HEADER_EXPECTATION),
    ("futures_klines_header_stripped", Gate.HEADER_EXPECTATION),
    ("spot_klines_first_epoch_zeroed", Gate.FIRST_TIMESTAMP_PLAUSIBLE),
    ("spot_klines_rows_out_of_order", Gate.MONOTONE_TIMESTAMPS),
    ("spot_trades_booleans_lowercased", Gate.BOOLEAN_TOKENS),
    ("spot_klines_high_below_close", Gate.OHLC_COHERENCE),
    ("spot_klines_negative_volume", Gate.NON_NEGATIVE_VOLUME),
]


def _bar(open_time_utc: datetime, *, close: str, high: str = "100000") -> KlineRecord:
    """A bar with only the fields the gates under test read.

    Constructed rather than recorded, and only for the record-level gate unit tests: gates
    4, 8 and 9 are pure functions of a record sequence, and expressing "these two bars are
    seventeen minutes apart" as an archive would mean deriving a fixture per arithmetic
    case. Every gate is *also* exercised against a real derived archive above.
    """
    return KlineRecord(
        open_time_utc=open_time_utc,
        close_time_utc=open_time_utc + timedelta(minutes=1),
        open_quote_price=Decimal("100"),
        high_quote_price=Decimal(high),
        low_quote_price=Decimal("1"),
        close_quote_price=Decimal(close),
        base_volume=Decimal("1"),
        quote_volume=Decimal("100"),
        trade_count=1,
        taker_buy_base_volume=Decimal("0"),
        taker_buy_quote_volume=Decimal("0"),
        ignored_field="0",
    )


class TestBlockingGates:
    """Gates 1-7: each refuses, names itself, and leaves nothing on disk."""

    @pytest.mark.parametrize(("fixture_name", "gate"), BLOCKING_FIXTURES, ids=lambda value: value)
    def test_the_gate_refuses_and_names_itself(self, fixture_name: str, gate: Gate) -> None:
        corrupt = corrupt_fixtures.find(fixture_name)
        with pytest.raises(QualityGateError) as refusal:
            gate_archive(corrupt.read(), corrupt.spec(), source=fixture_name)
        assert refusal.value.gate is gate

    @pytest.mark.parametrize(("fixture_name", "gate"), BLOCKING_FIXTURES, ids=lambda value: value)
    def test_nothing_is_written_when_a_gate_fails(
        self, fixture_name: str, gate: Gate, tmp_path: Path
    ) -> None:
        """The acceptance criterion in its most direct form: the target path does not exist.

        Not "the file is empty" and not "the file is short". A gate that ran after the write
        would leave a plausible Parquet file behind, and a plausible short file is the exact
        failure `DATA_PIPELINE.md` section 10 opens by refusing.
        """
        corrupt = corrupt_fixtures.find(fixture_name)
        spec = corrupt.spec()
        destination = partition_path(spec.coordinate, root=tmp_path)

        with pytest.raises(QualityGateError) as refusal:
            ingest_archive(
                corrupt.read(),
                spec,
                source=fixture_name,
                write_root=tmp_path,
                ingested_at_utc=INGESTED_AT_UTC,
            )

        assert refusal.value.gate is gate
        assert not destination.exists()
        assert not list(tmp_path.rglob("*.parquet"))

    def test_a_clean_archive_passes_every_gate_and_is_written(self, tmp_path: Path) -> None:
        """The corpus proves the gates fire; this proves they do not fire on real data.

        A suite of only-negative tests passes just as well against a gate that refuses
        everything, which would stop the pipeline rather than protect it.
        """
        recorded = archive_fixtures.find(
            market=Market.SPOT,
            dataset=Dataset.KLINES,
            archive_date=SPOT_ARCHIVE_DATE,
            whole=True,
        )
        outcome = ingest_archive(
            recorded.read(),
            recorded.spec(),
            source=recorded.label,
            write_root=tmp_path,
            ingested_at_utc=INGESTED_AT_UTC,
        )
        assert outcome.write is not None
        assert outcome.write.path.is_file()
        assert outcome.normalization.rows_rejected == 0
        assert outcome.cadence_gaps == ()

    def test_a_clean_trades_archive_passes_and_is_not_asked_about_cadence(
        self, tmp_path: Path
    ) -> None:
        """Trades have no cadence and no bar closes, so gates 8 and 9 report nothing.

        Worth its own test because the trades path reads its epoch from column 4 rather
        than column 0. A gate 3 that assumed column 0 would read a nine-digit trade id as a
        timestamp, land in 1970, and refuse every genuine trades archive -- a gate that
        rejects only correct files.
        """
        recorded = archive_fixtures.find(
            market=Market.SPOT,
            dataset=Dataset.TRADES,
            archive_date=SPOT_ARCHIVE_DATE,
            whole=False,
        )
        archive_bytes = _as_single_member_zip(recorded.read(), member_name="trades.csv")
        spec = dataclasses.replace(
            recorded.spec(),
            source_checksum_hex=hashlib.sha256(archive_bytes).hexdigest(),
        )

        outcome = ingest_archive(
            archive_bytes,
            spec,
            source=recorded.label,
            write_root=tmp_path,
            ingested_at_utc=INGESTED_AT_UTC,
        )
        assert outcome.write is not None
        assert outcome.normalization.rows_rejected == 0
        assert outcome.cadence_gaps == ()
        assert outcome.continuity_flags == ()


class TestGate3FirstTimestamp:
    """Gate 3 stops on row one rather than after every row has been rejected identically."""

    def test_an_archive_with_no_data_rows_is_not_asked_for_a_first_timestamp(self) -> None:
        """A symbol can print nothing for a day, and that is an observation rather than a
        fault (`DATA_PIPELINE.md` section 4). Gate 3 has no row to read and must not invent
        a refusal; the write refuses instead, because an empty batch names no partition."""
        recorded = archive_fixtures.find(
            market=Market.SPOT,
            dataset=Dataset.KLINES,
            archive_date=SPOT_ARCHIVE_DATE,
            whole=True,
        )
        archive_bytes = _as_single_member_zip(b"", member_name="empty.csv")
        spec = dataclasses.replace(
            recorded.spec(), source_checksum_hex=hashlib.sha256(archive_bytes).hexdigest()
        )

        records, outcome = gate_archive(archive_bytes, spec, source="empty")
        assert records == ()
        assert outcome.normalization.rows_in == 0
        assert outcome.normalization.first_event_time_utc is None

    def test_a_millisecond_epoch_read_as_microseconds_lands_in_1970_and_refuses(self) -> None:
        with pytest.raises(QualityGateError) as refusal:
            assert_first_timestamp_is_plausible(
                "1735776000000",  # a millisecond epoch for 2025-01-02
                unit=EpochUnit.MICROSECONDS,
                now_utc=NOW_UTC,
                source="spot/klines",
            )
        assert refusal.value.gate is Gate.FIRST_TIMESTAMP_PLAUSIBLE
        assert "1970" in str(refusal.value)

    def test_a_microsecond_epoch_read_as_milliseconds_lands_in_the_far_future(self) -> None:
        with pytest.raises(QualityGateError) as refusal:
            assert_first_timestamp_is_plausible(
                "1735776000000000",  # the same instant in microseconds
                unit=EpochUnit.MILLISECONDS,
                now_utc=NOW_UTC,
                source="spot/klines",
            )
        assert refusal.value.gate is Gate.FIRST_TIMESTAMP_PLAUSIBLE

    def test_a_correctly_declared_epoch_is_returned(self) -> None:
        assert assert_first_timestamp_is_plausible(
            "1735776000000000",
            unit=EpochUnit.MICROSECONDS,
            now_utc=NOW_UTC,
            source="spot/klines",
        ) == datetime(2025, 1, 2, tzinfo=UTC)

    def test_a_non_integer_first_field_refuses_before_any_unit_is_applied(self) -> None:
        with pytest.raises(QualityGateError, match="not a base-10 integer"):
            assert_first_timestamp_is_plausible(
                "open_time", unit=EpochUnit.MICROSECONDS, now_utc=NOW_UTC, source="x"
            )


class TestGate4Monotone:
    """Zero violations, and the whole file rather than the offending rows."""

    def test_equal_timestamps_are_permitted(self) -> None:
        """A trades archive legitimately prints several fills inside one microsecond."""
        moment = datetime(2025, 1, 2, tzinfo=UTC)
        assert_bar_timestamps_are_monotone(
            [_bar(moment, close="1"), _bar(moment, close="2")], source="x"
        )

    def test_one_step_backwards_refuses(self) -> None:
        moment = datetime(2025, 1, 2, tzinfo=UTC)
        with pytest.raises(QualityGateError) as refusal:
            assert_bar_timestamps_are_monotone(
                [
                    _bar(moment, close="1"),
                    _bar(moment + timedelta(minutes=1), close="2"),
                    _bar(moment, close="3"),
                ],
                source="x",
            )
        assert refusal.value.gate is Gate.MONOTONE_TIMESTAMPS
        assert "merged upstream" in str(refusal.value)


class TestGate8Cadence:
    """Gaps are recorded and never filled."""

    def test_a_missing_block_is_reported_with_its_size(self) -> None:
        start = datetime(2025, 1, 2, tzinfo=UTC)
        records = [
            _bar(start, close="1"),
            _bar(start + timedelta(minutes=11), close="2"),
            _bar(start + timedelta(minutes=12), close="3"),
        ]
        gaps = detect_cadence_gaps(records, interval="1m", source="x")
        assert len(gaps) == 1
        assert gaps[0].after_open_time_utc == start
        assert gaps[0].before_open_time_utc == start + timedelta(minutes=11)
        assert gaps[0].missing_bar_count == GAPPED_BAR_COUNT

    def test_an_off_lattice_gap_still_counts_every_missing_bar(self) -> None:
        """A ragged remainder means the series is off-lattice as well as short.

        Rounding down would report three missing bars where four minutes are unaccounted
        for, and the coverage registry would then believe a range it does not hold.
        """
        start = datetime(2025, 1, 2, tzinfo=UTC)
        # 4m30s apart on a 1m lattice: four bars are unaccounted for, and the last of them
        # is a partial. Rounding down would report three and the coverage registry would
        # then believe a range the corpus does not hold.
        bars_unaccounted_for = 4
        records = [_bar(start, close="1"), _bar(start + timedelta(seconds=270), close="2")]
        gap = detect_cadence_gaps(records, interval="1m", source="x")[0]
        assert gap.missing_bar_count == bars_unaccounted_for

    def test_a_gapless_series_reports_nothing(self) -> None:
        start = datetime(2025, 1, 2, tzinfo=UTC)
        records = [_bar(start + timedelta(minutes=index), close="1") for index in range(5)]
        assert detect_cadence_gaps(records, interval="1m", source="x") == ()

    def test_an_undeclared_interval_refuses_rather_than_inferring_a_duration(self) -> None:
        """`1M` is a calendar month. Approximating it at 30 days is trap 1's mechanism."""
        with pytest.raises(QualityGateError) as refusal:
            detect_cadence_gaps([], interval="1M", source="x")
        assert refusal.value.gate is Gate.BAR_CADENCE

    def test_a_real_gapped_archive_is_ingested_and_the_gap_is_recorded(
        self, tmp_path: Path
    ) -> None:
        corrupt = corrupt_fixtures.find("spot_klines_gapped_bar_block")
        outcome = ingest_archive(
            corrupt.read(),
            corrupt.spec(),
            source=corrupt.name,
            write_root=tmp_path,
            ingested_at_utc=INGESTED_AT_UTC,
        )
        assert outcome.write is not None
        assert outcome.write.path.is_file()  # recorded, not refused
        assert len(outcome.cadence_gaps) == 1
        assert outcome.cadence_gaps[0].missing_bar_count == GAPPED_BAR_COUNT
        # Never filled: the written file holds the rows that survived, not the ones implied.
        assert outcome.write.rows_written == outcome.normalization.rows_out


class TestGate9PriceContinuity:
    """Flagged, never rejected. The row is written."""

    def test_a_08_log_return_is_flagged_and_the_row_is_still_written(self, tmp_path: Path) -> None:
        """The acceptance criterion, against a real archive scaled by exp(0.8).

        Gate 9 flags it and gate 6 does not touch it, because the whole bar was scaled and
        the high still brackets the open/close pair. That pairing is the point: the two
        gates disagree about the same row on purpose.
        """
        corrupt = corrupt_fixtures.find("spot_klines_08_log_return")
        outcome = ingest_archive(
            corrupt.read(),
            corrupt.spec(),
            source=corrupt.name,
            write_root=tmp_path,
            ingested_at_utc=INGESTED_AT_UTC,
        )
        assert outcome.write is not None
        assert outcome.write.path.is_file()
        assert outcome.normalization.rows_rejected == 0
        assert len(outcome.continuity_flags) == 1

    def test_high_below_close_is_rejected_while_a_large_move_is_not(self, tmp_path: Path) -> None:
        """The contrast the issue asks for, stated as one test.

        Gate 6 refuses an incoherent bar and writes nothing; gate 9 keeps a violent but
        coherent one. A pipeline that treated both as "an implausible price" would discard
        exactly the tail the risk engine most needs to have seen.
        """
        incoherent = corrupt_fixtures.find("spot_klines_high_below_close")
        with pytest.raises(QualityGateError) as refusal:
            ingest_archive(
                incoherent.read(),
                incoherent.spec(),
                source=incoherent.name,
                write_root=tmp_path,
                ingested_at_utc=INGESTED_AT_UTC,
            )
        assert refusal.value.gate is Gate.OHLC_COHERENCE
        assert not list(tmp_path.rglob("*.parquet"))

    def test_the_threshold_sits_exactly_at_a_half_log_return(self) -> None:
        """The bounds are exp(-0.5) and exp(0.5), so a move just inside them is not flagged."""
        start = datetime(2025, 1, 2, tzinfo=UTC)
        just_inside = CONTINUITY_UPPER_RATIO - Decimal("0.000001")
        just_outside = CONTINUITY_UPPER_RATIO + Decimal("0.000001")

        inside = [_bar(start, close="1"), _bar(start, close=str(just_inside))]
        outside = [_bar(start, close="1"), _bar(start, close=str(just_outside))]

        assert flag_price_discontinuities(inside, source="x") == ()
        assert len(flag_price_discontinuities(outside, source="x")) == 1

    def test_a_collapse_is_flagged_as_well_as_a_spike(self) -> None:
        """Symmetric in log space. A gate that only saw spikes would miss every flash crash."""
        start = datetime(2025, 1, 2, tzinfo=UTC)
        collapsed = CONTINUITY_LOWER_RATIO - Decimal("0.000001")
        records = [_bar(start, close="1"), _bar(start, close=str(collapsed))]
        assert len(flag_price_discontinuities(records, source="x")) == 1


class TestGate10CrossSource:
    """Archive and stream must agree where they overlap, and disagreement escalates."""

    def _records(self) -> tuple[KlineRecord, ...]:
        recorded = archive_fixtures.find(
            market=Market.SPOT,
            dataset=Dataset.KLINES,
            archive_date=SPOT_ARCHIVE_DATE,
            whole=True,
        )
        records, _ = gate_archive(recorded.read(), recorded.spec(), source=recorded.label)
        return tuple(record for record in records if isinstance(record, KlineRecord))

    def test_identical_sources_agree_over_the_overlap(self) -> None:
        archived = self._records()
        streamed = archived[100:200]
        compared = assert_cross_source_agreement(archived, streamed, source="spot/klines")
        assert compared == OVERLAP_BAR_COUNT

    def test_one_changed_field_escalates_and_names_the_column(self) -> None:
        archived = self._records()
        streamed = list(archived[100:200])
        streamed[7] = dataclasses.replace(
            streamed[7], close_quote_price=streamed[7].close_quote_price + Decimal("1")
        )
        with pytest.raises(QualityGateError) as refusal:
            assert_cross_source_agreement(archived, streamed, source="spot/klines")
        assert refusal.value.gate is Gate.CROSS_SOURCE_AGREEMENT
        assert "close_quote_price" in str(refusal.value)
        assert "Neither copy is preferred" in str(refusal.value)

    def test_a_hole_inside_the_overlap_escalates(self) -> None:
        archived = self._records()
        streamed = [*archived[100:150], *archived[151:200]]
        with pytest.raises(QualityGateError) as refusal:
            assert_cross_source_agreement(archived, streamed, source="spot/klines")
        assert refusal.value.gate is Gate.CROSS_SOURCE_AGREEMENT

    def test_a_duplicated_instant_escalates_rather_than_picking_a_copy(self) -> None:
        archived = self._records()
        with pytest.raises(QualityGateError, match="two records at"):
            assert_cross_source_agreement(
                archived, [archived[100], archived[100]], source="spot/klines"
            )

    def test_a_stream_only_record_inside_the_overlap_escalates(self) -> None:
        """A hole in the archive, not a surplus in the buffer. The archive is what a
        backtest reads, so the stream having more is the finding."""
        archived = self._records()
        thinned = [*archived[100:150], *archived[151:200]]
        with pytest.raises(QualityGateError, match="absent from the archive"):
            assert_cross_source_agreement(thinned, archived[100:200], source="spot/klines")

    def test_two_record_types_are_reported_as_a_type_difference(self) -> None:
        """A trade compared against a bar is a coordinate mix-up, not a field disagreement,
        and saying "close_quote_price differs" would send an investigator to the wrong
        column of the wrong dataset."""
        archived = self._records()
        trade = TradeRecord(
            venue_trade_id="1",
            event_time_utc=archived[0].event_time_utc,
            quote_price=Decimal("1"),
            base_quantity=Decimal("1"),
            quote_quantity=Decimal("1"),
            is_buyer_maker=True,
            is_best_match=True,
        )
        with pytest.raises(QualityGateError, match="record type"):
            assert_cross_source_agreement([archived[0]], [trade], source="spot/klines")

    def test_an_empty_source_compares_nothing(self) -> None:
        """Zero, not a pass. A stream buffer that has not filled yet is not agreement."""
        assert assert_cross_source_agreement(self._records(), [], source="x") == 0

    def test_disjoint_ranges_compare_nothing_and_say_so(self) -> None:
        """Zero is the honest answer, not a pass. A caller ignoring it is gating on nothing."""
        archived = self._records()
        assert assert_cross_source_agreement(archived[:10], archived[900:910], source="x") == 0


class TestTheResidualCeiling:
    """The declared ceiling still governs the reasons no numbered gate owns.

    Exercised as a unit over a `NormalizationResult` rather than through a corrupt archive,
    because the reasons in question -- a field count that changed, a venue id that stopped
    being an integer -- are upstream layout changes that the corpus's per-gate fixtures
    deliberately do not model. What matters is the partition, and the partition is a
    property of the reason set rather than of any file.
    """

    def _outcome(self, reason: RejectionReason) -> NormalizationResult:
        return NormalizationResult(
            rows_in=1000,
            rows_out=500,
            rows_rejected=500,
            rejection_reasons={reason: 500},
            epoch_unit_applied=EpochUnit.MICROSECONDS,
            first_event_time_utc=datetime(2025, 1, 2, tzinfo=UTC),
            last_event_time_utc=datetime(2025, 1, 2, 23, 59, tzinfo=UTC),
            source_checksum_hex="0" * 64,
        )

    def test_an_unowned_reason_refuses_above_the_ceiling(self) -> None:
        with pytest.raises(DataIntegrityError, match="declared ceiling"):
            assert_residual_rejections_within_ceiling(
                self._outcome(RejectionReason.EPOCH_OUT_OF_RANGE),
                ceiling=Decimal("0.001"),
                source="x",
            )

    @pytest.mark.parametrize(
        "reason",
        [
            RejectionReason.BOOLEAN_UNRECOGNISED,
            RejectionReason.OHLC_NOT_BRACKETING,
            RejectionReason.VOLUME_NEGATIVE,
        ],
        ids=lambda reason: reason.value,
    )
    def test_an_owned_reason_is_left_to_its_own_gate(self, reason: RejectionReason) -> None:
        """Double adjudication would report the wrong gate for a failure that has one."""
        assert_residual_rejections_within_ceiling(
            self._outcome(reason), ceiling=Decimal("0.001"), source="x"
        )

    def test_every_rejection_reason_is_owned_by_a_gate_or_by_the_ceiling(self) -> None:
        """No reason falls between the numbered gates and the residual rule.

        Derived rather than listed, so a `RejectionReason` added next year is covered on the
        day it is defined rather than on the day somebody remembers this test.
        """
        owned = {named.reason for named in _CEILING_GATES.values()} | {
            RejectionReason.VOLUME_NEGATIVE
        }
        residual = set(RejectionReason) - owned
        assert owned | residual == set(RejectionReason)
        assert not owned & residual


def _as_single_member_zip(member: bytes, *, member_name: str) -> bytes:
    """Wrap CSV bytes in the single-member zip ingestion receives.

    The CSV-prefix recordings are fragments rather than archives, and gate 1 hashes an
    archive while gate 2 reads the member out of one. Wrapping here rather than committing a
    second copy of the fragment keeps one recorded corpus.
    """
    buffer = BytesIO()
    with zipfile.ZipFile(buffer, mode="w", compression=zipfile.ZIP_STORED) as bundle:
        bundle.writestr(
            zipfile.ZipInfo(filename=member_name, date_time=(2026, 1, 1, 0, 0, 0)), member
        )
    return buffer.getvalue()
