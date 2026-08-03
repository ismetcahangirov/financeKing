"""The archive parsers, against real recorded archives, entirely offline.

Every assertion here is about a failure that produces **no exception** if it is allowed
through, which is why so much of this file asserts that something raises. The three traps
in `DATA_PIPELINE.md` section 3 share that shape: a plausible row count, plausible prices,
plausible volumes, and one column silently wrong.

Mutated inputs are produced here, from bytes read out of a genuine recording, so the
mutation is visible in the same diff as the assertion about it. Nothing under
`tests/fixtures/archives/` is hand-authored -- see `tests/support/archive_fixtures.py`.
"""

from __future__ import annotations

import io
import zipfile
from collections.abc import Callable, Sequence
from datetime import UTC, date, datetime, timedelta, timezone
from decimal import Decimal

import pytest

from fking.data.archive import ArchiveCoordinate
from fking.data.format_resolver import (
    DECLARED_FORMATS,
    ArchiveFormat,
    BooleanEncoding,
    Dataset,
    EpochUnit,
    Market,
)
from fking.data.loaders import (
    IMPLEMENTED_DATASETS,
    KLINE_COLUMNS,
    TRADE_COLUMNS,
    IngestionSpec,
    KlineRecord,
    RejectionReason,
    TradeRecord,
    extract_single_member,
    parse_archive,
    parse_klines,
    parse_trades,
)
from fking.platform.errors import DataIntegrityError
from tests.support import archive_fixtures
from tests.support.archive_fixtures import NOW_UTC, RecordedArchive

pytestmark = pytest.mark.unit

SPOT_MICROSECOND_DAY = date(2025, 1, 2)
SPOT_MILLISECOND_DAY = date(2024, 12, 31)
FUTURES_DAY = date(2025, 1, 2)

MINUTES_IN_A_DAY = 1440
FRAGMENT_DATA_ROWS = 32
KLINE_COLUMN_COUNT = 12
TRADE_COLUMN_COUNT = 7

# Column indices used by the mutation helpers below. Named rather than inlined because a
# bare `fields[6]` in a test about close_time is a test whose intent survives only in the
# author's head.
OPEN_TIME, OPEN, HIGH, LOW, CLOSE = 0, 1, 2, 3, 4
VOLUME, CLOSE_TIME, QUOTE_VOLUME, TRADE_COUNT = 5, 6, 7, 8

RowMutation = Callable[[Sequence[str]], Sequence[str]]


def spot_klines(
    archive_date: date = SPOT_MICROSECOND_DAY, *, whole: bool = False
) -> RecordedArchive:
    return archive_fixtures.find(
        market=Market.SPOT, dataset=Dataset.KLINES, archive_date=archive_date, whole=whole
    )


def futures_klines(*, whole: bool = False) -> RecordedArchive:
    return archive_fixtures.find(
        market=Market.FUTURES_UM, dataset=Dataset.KLINES, archive_date=FUTURES_DAY, whole=whole
    )


def spot_trades() -> RecordedArchive:
    return archive_fixtures.find(
        market=Market.SPOT, dataset=Dataset.TRADES, archive_date=SPOT_MICROSECOND_DAY, whole=False
    )


def mutate_row(payload: bytes, *, row_index: int, mutation: RowMutation) -> bytes:
    """Rewrite one row of a recorded CSV, leaving every other byte alone."""
    lines = payload.splitlines()
    fields = lines[row_index].decode().split(",")
    lines[row_index] = ",".join(mutation(fields)).encode()
    return b"\n".join(lines) + b"\n"


class TestTrapOneEpochUnit:
    """VF-015. The same twelve columns, three orders of magnitude apart, in one corpus."""

    def test_post_cutover_spot_klines_are_microseconds(self) -> None:
        recorded = spot_klines(SPOT_MICROSECOND_DAY)
        bars, outcome = parse_klines(recorded.read(), recorded.spec(), source=recorded.label)

        assert outcome.epoch_unit_applied is EpochUnit.MICROSECONDS
        assert bars[0].open_time_utc == datetime(2025, 1, 2, tzinfo=UTC)
        assert bars[1].open_time_utc == datetime(2025, 1, 2, 0, 1, tzinfo=UTC)

    def test_pre_cutover_spot_klines_are_milliseconds(self) -> None:
        recorded = spot_klines(SPOT_MILLISECOND_DAY)
        bars, outcome = parse_klines(recorded.read(), recorded.spec(), source=recorded.label)

        assert outcome.epoch_unit_applied is EpochUnit.MILLISECONDS
        assert bars[0].open_time_utc == datetime(2024, 12, 31, tzinfo=UTC)

    def test_futures_klines_stayed_milliseconds_on_the_same_date(self) -> None:
        recorded = futures_klines()
        bars, outcome = parse_klines(recorded.read(), recorded.spec(), source=recorded.label)

        assert outcome.epoch_unit_applied is EpochUnit.MILLISECONDS
        assert bars[0].open_time_utc == datetime(2025, 1, 2, tzinfo=UTC)

    def test_microsecond_data_read_as_milliseconds_rejects_every_row(self) -> None:
        """One rule -- reject the row, gate the fraction -- catches a whole-file unit error.

        This is why there is no separate first-row plausibility assertion: a wrong
        declaration rejects every row identically, the fraction reaches 1, and the refusal
        message names `epoch_out_of_range` with its count.
        """
        recorded = spot_klines(SPOT_MICROSECOND_DAY)
        # The pre-cutover segment declared for a post-cutover file: milliseconds applied to
        # microsecond epochs lands near the year 56,000.
        wrong = recorded.spec(archive_date=SPOT_MILLISECOND_DAY)

        with pytest.raises(DataIntegrityError) as refusal:
            parse_klines(recorded.read(), wrong, source=recorded.label)

        message = str(refusal.value)
        assert f"{RejectionReason.EPOCH_OUT_OF_RANGE.value}={FRAGMENT_DATA_ROWS}" in message

    def test_millisecond_data_read_as_microseconds_rejects_every_row(self) -> None:
        recorded = spot_klines(SPOT_MILLISECOND_DAY)
        wrong = recorded.spec(archive_date=SPOT_MICROSECOND_DAY)

        with pytest.raises(DataIntegrityError) as refusal:
            parse_klines(recorded.read(), wrong, source=recorded.label)

        assert RejectionReason.EPOCH_OUT_OF_RANGE.value in str(refusal.value)

    def test_a_spec_cannot_declare_a_format_that_does_not_cover_the_file(self) -> None:
        """The mismatch above is only constructible because the date is overridden with it.

        Handing a resolved format to a file of another date is refused at construction,
        which is where noticing it costs nothing.
        """
        recorded = spot_klines(SPOT_MICROSECOND_DAY)
        pre_cutover_format = spot_klines(SPOT_MILLISECOND_DAY).spec().archive_format

        with pytest.raises(DataIntegrityError, match="resolve the format for the file's own date"):
            IngestionSpec(
                coordinate=recorded.coordinate(),
                archive_format=pre_cutover_format,
                source_checksum_hex=recorded.archive_sha256,
                now_utc=NOW_UTC,
            )

    def test_a_spec_cannot_pair_one_market_with_another_market_format(self) -> None:
        recorded = spot_klines(SPOT_MICROSECOND_DAY)

        with pytest.raises(DataIntegrityError, match="formats do not transfer between corpora"):
            IngestionSpec(
                coordinate=recorded.coordinate(),
                archive_format=futures_klines().spec().archive_format,
                source_checksum_hex=recorded.archive_sha256,
                now_utc=NOW_UTC,
            )


class TestTrapTwoHeaderRow:
    """VF-016. Two silent directions, and they are not equally bad."""

    def test_spot_klines_read_as_though_they_had_a_header_reject_the_file(self) -> None:
        """The dangerous direction: otherwise the first bar of the day vanishes silently."""
        recorded = spot_klines()
        as_futures = recorded.spec(market=Market.FUTURES_UM)
        assert as_futures.archive_format.has_header_row is True

        with pytest.raises(DataIntegrityError, match="declares has_header_row=True"):
            parse_klines(recorded.read(), as_futures, source=recorded.label)

    def test_futures_klines_read_as_though_they_had_none_reject_the_file(self) -> None:
        recorded = futures_klines()
        as_spot = recorded.spec(market=Market.SPOT)
        assert as_spot.archive_format.has_header_row is False

        with pytest.raises(DataIntegrityError, match="declares has_header_row=False"):
            parse_klines(recorded.read(), as_spot, source=recorded.label)

    @pytest.mark.parametrize(
        ("fixture_market", "declared_market"),
        [(Market.SPOT, Market.FUTURES_UM), (Market.FUTURES_UM, Market.SPOT)],
        ids=["spot-read-as-futures", "futures-read-as-spot"],
    )
    def test_a_header_mismatch_returns_no_partial_records_in_either_direction(
        self, fixture_market: Market, declared_market: Market
    ) -> None:
        """A file-level refusal, not a row-level one: nothing partial comes back at all.

        Asserted by binding the result to a name that stays unset. A parser that returned
        1,439 of 1,440 bars alongside a warning would pass a `pytest.raises`-free test and
        is precisely the outcome trap 2 produces.
        """
        recorded = archive_fixtures.find(
            market=fixture_market,
            dataset=Dataset.KLINES,
            archive_date=date(2025, 1, 2),
            whole=False,
        )
        returned: tuple[KlineRecord, ...] | None = None

        with pytest.raises(DataIntegrityError):
            returned, _ = parse_klines(
                recorded.read(), recorded.spec(market=declared_market), source=recorded.label
            )

        assert returned is None

    def test_the_declared_header_names_must_match_the_declared_layout(self) -> None:
        """A reordered column parses cleanly, types correctly, and means something else."""
        recorded = futures_klines()
        swapped = (KLINE_COLUMNS[1], KLINE_COLUMNS[0], *KLINE_COLUMNS[2:])
        mutated = mutate_row(recorded.read(), row_index=0, mutation=lambda _fields: swapped)

        with pytest.raises(DataIntegrityError, match="reordered or renamed column"):
            parse_klines(mutated, recorded.spec(), source=recorded.label)

    def test_the_real_futures_header_is_the_declared_layout(self) -> None:
        """If Binance renames or reorders a kline column, this is the test that says so."""
        recorded = futures_klines()
        header = recorded.read().splitlines()[0].decode()

        assert tuple(header.split(",")) == KLINE_COLUMNS


class TestTrapThreeBooleanEncoding:
    """F-005. The trap with the highest ratio of damage to visibility."""

    def test_spot_trades_serialise_booleans_python_style(self) -> None:
        recorded = spot_trades()
        assert recorded.spec().archive_format.boolean_encoding is BooleanEncoding.PYTHON

        trades, outcome = parse_trades(recorded.read(), recorded.spec(), source=recorded.label)

        assert outcome.rows_rejected == 0
        # Both values are present in the recorded fragment. The actual failure this guards --
        # `False` on every row -- would satisfy neither assertion.
        assert {trade.is_buyer_maker for trade in trades} == {True, False}
        assert trades[0].is_buyer_maker is True
        assert trades[2].is_buyer_maker is False

    @pytest.mark.parametrize(
        ("true_token", "false_token"),
        [(b"true", b"false"), (b"1", b"0"), (b"TRUE", b"FALSE")],
        ids=["json", "numeric", "uppercase"],
    )
    def test_a_drifted_boolean_encoding_is_rejected_rather_than_read_as_false(
        self, true_token: bytes, false_token: bytes
    ) -> None:
        """Every plausible alternative spelling, none of them silently accepted."""
        recorded = spot_trades()
        mutated = recorded.read().replace(b"True", true_token).replace(b"False", false_token)

        with pytest.raises(DataIntegrityError) as refusal:
            parse_trades(mutated, recorded.spec(), source=recorded.label)

        assert RejectionReason.BOOLEAN_UNRECOGNISED.value in str(refusal.value)

    def test_one_drifted_row_is_counted_under_its_own_reason_without_refusing_the_file(
        self,
    ) -> None:
        """Below the ceiling, so the per-reason counts are observable rather than raised past.

        The full-day archive is used because 0.1% of a 32-row fragment tolerates nothing --
        which is the correct threshold and makes a fragment useless for this assertion.
        """
        recorded = spot_klines(whole=True)
        member = extract_single_member(recorded.read(), source=recorded.label)
        mutated = mutate_row(
            member,
            row_index=7,
            mutation=lambda fields: [*fields[:TRADE_COUNT], "eleven", *fields[TRADE_COUNT + 1 :]],
        )

        bars, outcome = parse_klines(mutated, recorded.spec(), source=recorded.label)

        assert outcome.rows_in == MINUTES_IN_A_DAY
        assert outcome.rows_out == MINUTES_IN_A_DAY - 1
        assert len(bars) == outcome.rows_out
        assert outcome.rejection_reasons == {RejectionReason.TRADE_COUNT_NOT_INTEGER: 1}
        assert outcome.rejection_fraction < Decimal("0.001")
        assert outcome.describe_rejections() == "trade_count_not_integer=1/1440"


class TestDecimalFidelity:
    """Prices and quantities are exact, and they came from the raw source substring."""

    def test_kline_prices_and_volumes_match_their_source_substrings_exactly(self) -> None:
        recorded = spot_klines()
        source_fields = recorded.read().splitlines()[0].decode().split(",")
        bars, _ = parse_klines(recorded.read(), recorded.spec(), source=recorded.label)

        assert bars[0].open_quote_price == Decimal(source_fields[OPEN])
        assert bars[0].high_quote_price == Decimal(source_fields[HIGH])
        assert bars[0].base_volume == Decimal(source_fields[VOLUME])
        assert bars[0].quote_volume == Decimal(source_fields[QUOTE_VOLUME])
        # The exponent is the file's, not one this code chose: Binance writes eight decimal
        # places and `str()` reproduces them. A value that had passed through a float would
        # print as 94591.78 and compare unequal to its own source text.
        assert str(bars[0].open_quote_price) == source_fields[OPEN]

    def test_an_eight_decimal_trade_quantity_is_exact(self) -> None:
        recorded = spot_trades()
        source_fields = recorded.read().splitlines()[0].decode().split(",")
        trades, _ = parse_trades(recorded.read(), recorded.spec(), source=recorded.label)

        assert trades[0].base_quantity == Decimal(source_fields[2])
        assert trades[0].quote_quantity == Decimal(source_fields[3])
        assert trades[0].venue_trade_id == source_fields[0]

    @pytest.mark.parametrize(
        "token",
        ["1_0", " 1", "1 ", "NaN", "Infinity", "-Infinity", "0x10", ""],
        ids=["underscore", "leading-space", "trailing-space", "nan", "inf", "-inf", "hex", "empty"],
    )
    def test_a_dangerous_decimal_spelling_is_rejected(self, token: str) -> None:
        """`Decimal("1_0")` is 10 and `Decimal("NaN")` never equals itself.

        Both construct without complaint, and each turns a malformed field into a number
        nobody wrote -- the second one poisoning every equality-based reconciliation
        downstream rather than raising anywhere.
        """
        recorded = spot_klines()
        mutated = mutate_row(
            recorded.read(),
            row_index=0,
            mutation=lambda fields: [*fields[:OPEN], token, *fields[OPEN + 1 :]],
        )

        with pytest.raises(DataIntegrityError) as refusal:
            parse_klines(mutated, recorded.spec(), source=recorded.label)

        assert RejectionReason.DECIMAL_UNPARSEABLE.value in str(refusal.value)

    def test_an_exact_exponent_is_accepted(self) -> None:
        """`Decimal("1e-8")` is exact, so refusing it would be strictness without a reason."""
        recorded = spot_klines()
        mutated = mutate_row(
            recorded.read(),
            row_index=0,
            mutation=lambda fields: [*fields[:VOLUME], "1e-8", *fields[VOLUME + 1 :]],
        )

        bars, outcome = parse_klines(mutated, recorded.spec(), source=recorded.label)

        assert outcome.rows_rejected == 0
        assert bars[0].base_volume == Decimal("0.00000001")


class TestNormalizationResult:
    """Rejections are the interesting half of the output."""

    @pytest.mark.parametrize(
        "recorded", archive_fixtures.csv_fragments(), ids=lambda recorded: recorded.label
    )
    def test_every_recorded_fragment_reports_a_complete_result(
        self, recorded: RecordedArchive
    ) -> None:
        records, outcome = parse_archive(recorded.read(), recorded.spec(), source=recorded.label)

        assert outcome.rows_in == recorded.data_row_count
        assert outcome.rows_out == len(records)
        assert outcome.rows_in == outcome.rows_out + outcome.rows_rejected
        assert outcome.rows_rejected == 0
        assert outcome.rejection_reasons == {}
        assert outcome.source_checksum_hex == recorded.archive_sha256
        assert outcome.first_event_time_utc is not None
        assert outcome.last_event_time_utc is not None
        assert outcome.first_event_time_utc.utcoffset() == timedelta(0)
        assert outcome.last_event_time_utc.utcoffset() == timedelta(0)
        assert outcome.first_event_time_utc <= outcome.last_event_time_utc

    def test_rejection_reasons_are_per_reason_and_never_a_bare_total(self) -> None:
        """Two different faults in one file, so a single counter cannot describe it."""
        recorded = spot_klines()
        payload = mutate_row(
            recorded.read(),
            row_index=0,
            mutation=lambda fields: [*fields[:TRADE_COUNT], "-5", *fields[TRADE_COUNT + 1 :]],
        )
        payload = mutate_row(payload, row_index=1, mutation=lambda fields: fields[:-1])

        with pytest.raises(DataIntegrityError) as refusal:
            parse_klines(payload, recorded.spec(), source=recorded.label)

        message = str(refusal.value)
        assert f"{RejectionReason.FIELD_COUNT.value}=1/{FRAGMENT_DATA_ROWS}" in message
        assert f"{RejectionReason.TRADE_COUNT_NOT_INTEGER.value}=1/{FRAGMENT_DATA_ROWS}" in message
        assert f"rejected 2 of {FRAGMENT_DATA_ROWS} rows" in message

    def test_the_result_mapping_cannot_be_mutated_through_the_dict_that_built_it(self) -> None:
        recorded = spot_klines()
        _, outcome = parse_klines(recorded.read(), recorded.spec(), source=recorded.label)

        with pytest.raises(TypeError):
            outcome.rejection_reasons[RejectionReason.FIELD_COUNT] = 99  # type: ignore[index]

    def test_an_empty_archive_reports_no_rows_and_no_timestamps(self) -> None:
        """A symbol that printed nothing is an observation -- not a gap, and not an error."""
        recorded = spot_klines()

        records, outcome = parse_klines(b"\n", recorded.spec(), source=recorded.label)

        assert records == ()
        assert (outcome.rows_in, outcome.rows_out, outcome.rows_rejected) == (0, 0, 0)
        assert outcome.first_event_time_utc is None
        assert outcome.last_event_time_utc is None
        assert outcome.rejection_fraction == Decimal("0")
        assert outcome.describe_rejections() == "none"

    def test_a_ceiling_of_zero_refuses_a_single_rejection(self) -> None:
        recorded = spot_klines(whole=True)
        member = extract_single_member(recorded.read(), source=recorded.label)
        mutated = mutate_row(
            member,
            row_index=3,
            mutation=lambda fields: [*fields[:TRADE_COUNT], "x", *fields[TRADE_COUNT + 1 :]],
        )

        with pytest.raises(DataIntegrityError, match="above the declared ceiling"):
            parse_klines(
                mutated,
                recorded.spec(max_rejection_fraction=Decimal("0")),
                source=recorded.label,
            )


class TestWholeArchive:
    """The path a backfill actually takes: verified zip in, a day of bars out."""

    def test_a_full_spot_day_yields_every_minute(self) -> None:
        recorded = spot_klines(whole=True)
        member = extract_single_member(recorded.read(), source=recorded.label)

        bars, outcome = parse_klines(member, recorded.spec(), source=recorded.label)

        assert outcome.rows_in == MINUTES_IN_A_DAY
        assert outcome.rows_out == MINUTES_IN_A_DAY
        assert bars[0].open_time_utc == datetime(2025, 1, 2, tzinfo=UTC)
        assert bars[-1].open_time_utc == datetime(2025, 1, 2, 23, 59, tzinfo=UTC)
        # A silently dropped first row would remove exactly one minute -- 00:00 UTC -- and
        # leave 1,439 that look entirely normal. This is what notices.
        assert len({bar.open_time_utc for bar in bars}) == MINUTES_IN_A_DAY

    def test_a_full_futures_day_yields_every_minute_after_its_header(self) -> None:
        recorded = futures_klines(whole=True)
        member = extract_single_member(recorded.read(), source=recorded.label)

        bars, outcome = parse_klines(member, recorded.spec(), source=recorded.label)

        assert outcome.rows_in == MINUTES_IN_A_DAY
        assert outcome.rows_out == MINUTES_IN_A_DAY
        assert bars[0].open_time_utc == datetime(2025, 1, 2, tzinfo=UTC)
        assert bars[-1].open_time_utc == datetime(2025, 1, 2, 23, 59, tzinfo=UTC)

    def test_bytes_that_are_not_a_zip_are_refused(self) -> None:
        with pytest.raises(DataIntegrityError, match="not a readable zip"):
            extract_single_member(b"open_time,open,high\n1,2,3\n", source="not-a-zip")

    def test_an_archive_holding_more_than_one_member_is_refused(self) -> None:
        """Choosing the first member would hide the packaging change that produced two."""
        buffer = io.BytesIO()
        with zipfile.ZipFile(buffer, "w") as bundle:
            bundle.writestr("first.csv", "1,2\n")
            bundle.writestr("second.csv", "3,4\n")

        with pytest.raises(DataIntegrityError, match="holds 2 members"):
            extract_single_member(buffer.getvalue(), source="two-members")


class TestDispatch:
    """A dataset whose format nobody has declared has no parser, and says why."""

    @pytest.mark.parametrize(
        "dataset",
        [Dataset.AGG_TRADES, Dataset.BOOK_DEPTH, Dataset.BOOK_TICKER],
        ids=lambda dataset: dataset.value,
    )
    def test_an_undeclared_dataset_has_no_parser(self, dataset: Dataset) -> None:
        assert dataset not in IMPLEMENTED_DATASETS

    def test_the_parser_table_and_the_format_table_stay_in_step(self) -> None:
        """They answer different questions, and either drift is a defect.

        A parser for a dataset whose format cannot be resolved is unreachable code. A
        declared format with no parser is a file that cannot be read. Neither is caught by
        a type checker, because both tables type-check fine while disagreeing.
        """
        assert {dataset for _market, dataset in DECLARED_FORMATS} == IMPLEMENTED_DATASETS

    def test_parse_archive_dispatches_on_the_declared_dataset(self) -> None:
        klines = spot_klines()
        trades = spot_trades()

        kline_records, _ = parse_archive(klines.read(), klines.spec(), source=klines.label)
        trade_records, _ = parse_archive(trades.read(), trades.spec(), source=trades.label)

        assert all(isinstance(record, KlineRecord) for record in kline_records)
        assert all(isinstance(record, TradeRecord) for record in trade_records)


class TestDeclarationDrift:
    """What happens when the two declaration tables get out of step.

    Neither of these states is reachable through `resolve_archive_format` today, and both
    become reachable the moment somebody declares a format ahead of writing its parser --
    which is the ordinary way this drift happens, because declaring is the cheaper half.
    `ArchiveFormat` is a public frozen dataclass, so the tests construct the drifted state
    directly rather than waiting for the repository to reach it.
    """

    def test_a_declared_format_with_no_parser_is_refused_by_name(self) -> None:
        drifted = ArchiveFormat(
            market=Market.SPOT,
            dataset=Dataset.AGG_TRADES,
            epoch_unit=EpochUnit.MICROSECONDS,
            has_header_row=False,
            boolean_encoding=BooleanEncoding.PYTHON,
            boolean_columns=("is_buyer_maker",),
            declared_from_date=date(2017, 8, 17),
            declared_until_date=None,
        )
        recorded = spot_trades()
        spec = IngestionSpec(
            coordinate=ArchiveCoordinate(
                market=Market.SPOT,
                dataset=Dataset.AGG_TRADES,
                symbol="BTCUSDT",
                archive_date=SPOT_MICROSECOND_DAY,
            ),
            archive_format=drifted,
            source_checksum_hex=recorded.archive_sha256,
            now_utc=NOW_UTC,
        )

        with pytest.raises(DataIntegrityError, match="no parser is implemented for dataset"):
            parse_archive(recorded.read(), spec, source="drifted")

    def test_a_boolean_dataset_declared_without_an_encoding_is_refused(self) -> None:
        """`None` here means the declaration is incomplete, so every row would fail alike.

        A file-level refusal rather than a per-row rejection, because the fault is in the
        declaration and not in the data -- and because "reject every row" would report the
        problem as a data-quality event on a file that is fine.
        """
        recorded = spot_trades()
        incomplete = ArchiveFormat(
            market=Market.SPOT,
            dataset=Dataset.TRADES,
            epoch_unit=EpochUnit.MICROSECONDS,
            has_header_row=False,
            boolean_encoding=None,
            boolean_columns=(),
            declared_from_date=date(2017, 8, 17),
            declared_until_date=None,
        )
        spec = IngestionSpec(
            coordinate=recorded.coordinate(),
            archive_format=incomplete,
            source_checksum_hex=recorded.archive_sha256,
            now_utc=NOW_UTC,
        )

        with pytest.raises(DataIntegrityError, match="names no boolean_encoding"):
            parse_trades(recorded.read(), spec, source=recorded.label)


class TestSpecValidation:
    """The spec states a parse's assumptions, so it refuses ones it cannot support."""

    def test_an_unverified_checksum_cannot_be_declared(self) -> None:
        """There is no `checksum_verified: bool`, because a boolean is satisfied by True."""
        recorded = spot_klines()

        with pytest.raises(DataIntegrityError, match="lowercase 64-character SHA-256"):
            IngestionSpec(
                coordinate=recorded.coordinate(),
                archive_format=recorded.spec().archive_format,
                source_checksum_hex="not-a-digest",
                now_utc=NOW_UTC,
            )

    @pytest.mark.parametrize(
        "reference",
        [
            datetime(2026, 8, 4),  # noqa: DTZ001 -- naive on purpose; that is the defect
            datetime(2026, 8, 4, tzinfo=timezone(timedelta(hours=4))),
        ],
        ids=["naive", "aware-but-not-utc"],
    )
    def test_a_reference_instant_that_is_not_utc_is_refused(self, reference: datetime) -> None:
        """Rejected rather than converted: `astimezone(UTC)` launders a wrong guess."""
        recorded = spot_klines()

        with pytest.raises(DataIntegrityError, match="now_utc must be timezone-aware UTC"):
            IngestionSpec(
                coordinate=recorded.coordinate(),
                archive_format=recorded.spec().archive_format,
                source_checksum_hex=recorded.archive_sha256,
                now_utc=reference,
            )

    @pytest.mark.parametrize("fraction", [Decimal("-0.1"), Decimal("1.5")])
    def test_a_ceiling_outside_zero_to_one_is_refused(self, fraction: Decimal) -> None:
        recorded = spot_klines()

        with pytest.raises(DataIntegrityError, match=r"fraction in \[0, 1\], not a percent"):
            IngestionSpec(
                coordinate=recorded.coordinate(),
                archive_format=recorded.spec().archive_format,
                source_checksum_hex=recorded.archive_sha256,
                now_utc=NOW_UTC,
                max_rejection_fraction=fraction,
            )

    def test_a_float_ceiling_is_refused(self) -> None:
        recorded = spot_klines()

        with pytest.raises(DataIntegrityError, match="not a float"):
            IngestionSpec(
                coordinate=recorded.coordinate(),
                archive_format=recorded.spec().archive_format,
                source_checksum_hex=recorded.archive_sha256,
                now_utc=NOW_UTC,
                max_rejection_fraction=0.001,  # type: ignore[arg-type]
            )

    @pytest.mark.parametrize("ceiling", ["0.001", Decimal("NaN")], ids=["string", "not-a-number"])
    def test_a_ceiling_that_is_not_a_finite_decimal_is_refused(self, ceiling: object) -> None:
        """`Decimal("NaN")` compares False against everything, so every gate would pass."""
        recorded = spot_klines()

        with pytest.raises(DataIntegrityError, match="must be a finite Decimal"):
            IngestionSpec(
                coordinate=recorded.coordinate(),
                archive_format=recorded.spec().archive_format,
                source_checksum_hex=recorded.archive_sha256,
                now_utc=NOW_UTC,
                max_rejection_fraction=ceiling,  # type: ignore[arg-type]
            )


class TestRowLevelRejections:
    """Every named reason is reachable. A reason with no test reads zero forever."""

    @pytest.mark.parametrize(
        ("mutation", "expected"),
        [
            (lambda fields: fields[:-1], RejectionReason.FIELD_COUNT),
            (lambda fields: [*fields, "extra"], RejectionReason.FIELD_COUNT),
            (
                lambda fields: ["not-an-epoch", *fields[OPEN_TIME + 1 :]],
                RejectionReason.EPOCH_NOT_INTEGER,
            ),
            (lambda fields: ["1", *fields[OPEN_TIME + 1 :]], RejectionReason.EPOCH_OUT_OF_RANGE),
            (
                lambda fields: [*fields[:OPEN], "0", *fields[OPEN + 1 :]],
                RejectionReason.PRICE_NOT_POSITIVE,
            ),
            (
                lambda fields: [*fields[:VOLUME], "-1", *fields[VOLUME + 1 :]],
                RejectionReason.VOLUME_NEGATIVE,
            ),
            (
                lambda fields: [*fields[:HIGH], "1", *fields[HIGH + 1 :]],
                RejectionReason.OHLC_NOT_BRACKETING,
            ),
            (
                lambda fields: [*fields[:CLOSE_TIME], fields[OPEN_TIME], *fields[CLOSE_TIME + 1 :]],
                RejectionReason.INTERVAL_NOT_FORWARD,
            ),
            (
                lambda fields: [*fields[:TRADE_COUNT], "-5", *fields[TRADE_COUNT + 1 :]],
                RejectionReason.TRADE_COUNT_NOT_INTEGER,
            ),
        ],
        ids=[
            "field-count-short",
            "field-count-long",
            "epoch-not-integer",
            "epoch-out-of-range",
            "price-not-positive",
            "volume-negative",
            "ohlc-not-bracketing",
            "interval-not-forward",
            "trade-count-not-integer",
        ],
    )
    def test_a_malformed_kline_row_is_rejected_under_its_own_reason(
        self, mutation: RowMutation, expected: RejectionReason
    ) -> None:
        """The second row, not the first.

        The header gate reads the first field of the first line, so a non-numeric epoch
        there is a *file* refusal rather than a row rejection -- which is correct, and is
        asserted separately below.
        """
        recorded = spot_klines()
        mutated = mutate_row(recorded.read(), row_index=1, mutation=mutation)

        with pytest.raises(DataIntegrityError) as refusal:
            parse_klines(mutated, recorded.spec(), source=recorded.label)

        assert f"{expected.value}=1/{FRAGMENT_DATA_ROWS}" in str(refusal.value)

    def test_a_non_numeric_first_field_on_the_first_line_refuses_the_file(self) -> None:
        """The gate cannot distinguish "a header appeared" from "row one is corrupt".

        Both are refusals, and conflating them is the safe direction: a file that grew a
        header is a format change, and a first row that lost its epoch is a truncated
        transfer. Treating either as one skippable row is how 1,439 minutes become normal.
        """
        recorded = spot_klines()
        mutated = mutate_row(
            recorded.read(), row_index=0, mutation=lambda fields: ["not-an-epoch", *fields[1:]]
        )

        with pytest.raises(DataIntegrityError, match="declares has_header_row=False"):
            parse_klines(mutated, recorded.spec(), source=recorded.label)

    def test_a_blank_trade_id_is_rejected(self) -> None:
        """A blank id is the join key a REST-backfill seam reconciles on, matching nothing."""
        recorded = spot_trades()
        mutated = mutate_row(
            recorded.read(), row_index=0, mutation=lambda fields: ["", *fields[1:]]
        )

        with pytest.raises(DataIntegrityError, match=RejectionReason.IDENTIFIER_BLANK.value):
            parse_trades(mutated, recorded.spec(), source=recorded.label)

    def test_undecodable_bytes_refuse_the_file(self) -> None:
        recorded = spot_klines()

        with pytest.raises(DataIntegrityError, match="is not utf-8"):
            parse_klines(b"\xff\xfe,1,2\n", recorded.spec(), source=recorded.label)

    def test_a_blank_line_between_rows_is_a_row_rejection_not_a_file_refusal(self) -> None:
        """A trailing newline is not a row; a blank line in the middle of one is an anomaly."""
        recorded = spot_klines(whole=True)
        member = extract_single_member(recorded.read(), source=recorded.label)
        lines = member.splitlines()
        mutated = b"\n".join([*lines[:5], b"", *lines[5:]]) + b"\n"

        _, outcome = parse_klines(mutated, recorded.spec(), source=recorded.label)

        assert outcome.rows_in == MINUTES_IN_A_DAY + 1
        assert outcome.rejection_reasons == {RejectionReason.FIELD_COUNT: 1}


class TestPurity:
    """Same bytes, same spec, same answer -- what makes a fixture a regression test."""

    def test_parsing_twice_produces_equal_records_and_an_equal_result(self) -> None:
        recorded = spot_trades()

        first_records, first_outcome = parse_trades(
            recorded.read(), recorded.spec(), source=recorded.label
        )
        second_records, second_outcome = parse_trades(
            recorded.read(), recorded.spec(), source=recorded.label
        )

        assert first_records == second_records
        assert first_outcome == second_outcome

    def test_records_are_frozen(self) -> None:
        recorded = spot_trades()
        trades, _ = parse_trades(recorded.read(), recorded.spec(), source=recorded.label)

        with pytest.raises(AttributeError):
            trades[0].quote_price = Decimal("1")  # type: ignore[misc]

    def test_a_kline_record_is_keyed_on_its_open(self) -> None:
        """Both record shapes answer `event_time_utc`, so nothing needs an isinstance branch."""
        recorded = spot_klines()
        bars, _ = parse_klines(recorded.read(), recorded.spec(), source=recorded.label)

        assert bars[0].event_time_utc == bars[0].open_time_utc

    def test_the_two_layouts_are_distinct(self) -> None:
        """Twelve columns against seven, sharing no name -- so a swapped table cannot pass.

        `driver.DatasetParser` pairs a layout with its row parser for exactly this reason:
        a field-count check against the wrong layout would be the only thing between a
        remapped column and a corpus of plausible wrong numbers.
        """
        assert len(KLINE_COLUMNS) == KLINE_COLUMN_COUNT
        assert len(TRADE_COLUMNS) == TRADE_COLUMN_COUNT
        assert not set(KLINE_COLUMNS) & set(TRADE_COLUMNS)
