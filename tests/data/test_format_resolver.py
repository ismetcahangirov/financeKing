"""The format resolver, and the four verified traps it exists to make impossible.

Every assertion here is about a failure that produces no exception if it is allowed
through. Milliseconds parsed as microseconds land in 1970 and microseconds parsed as
milliseconds land near the year 56,000 -- both absurd, both catchable -- but the
genuinely dangerous case is *mixing* the two inside one series while resampling, which
gives correct-looking endpoints and corrupted spacing in the middle. The only defence
that scales is refusing to guess, so most of this file asserts that the resolver
raises.

The fourth trap (VF-029) is quieter than the first three and is why `TimestampEncoding`
exists: `metrics` stamps `2024-01-02 00:00:00`, which is not an epoch, and reading it
with `fromisoformat` yields a value that is correct to the second and merely has no
timezone. Nothing about it looks wrong until a join returns nothing months later.
"""

from __future__ import annotations

import ast
import itertools
from datetime import UTC, date, datetime
from pathlib import Path

import pytest

from fking.data import (
    ArchiveFormat,
    BooleanEncoding,
    Dataset,
    EpochUnit,
    Market,
    TimestampEncoding,
    epoch_to_utc,
    parse_naive_utc_datetime,
    resolve_archive_format,
)
from fking.data import format_resolver as module_under_test
from fking.platform.errors import DataIntegrityError

pytestmark = pytest.mark.unit

# 2026-08-03T00:00:00Z. Fixed rather than read from the clock: the plausibility window
# is a function of `now`, so a test that read the real clock would change its own
# boundary conditions every day it ran.
NOW_UTC = datetime(2026, 8, 3, tzinfo=UTC)

# 2025-01-01T00:00:00Z, the cutover instant, as both units.
CUTOVER_MS = 1_735_689_600_000
CUTOVER_US = 1_735_689_600_000_000

DECLARED_KEYS = frozenset(
    {
        (Market.SPOT, Dataset.KLINES),
        (Market.SPOT, Dataset.TRADES),
        (Market.FUTURES_UM, Dataset.KLINES),
        # The two alternative datasets (#155). Declared here like every other format and
        # read by `fking.data.alt` rather than by the corpus loaders; `ALT_DATASETS` is
        # what states that distinction.
        (Market.FUTURES_UM, Dataset.FUNDING_RATE),
        (Market.FUTURES_UM, Dataset.METRICS),
    }
)
UNDECLARED_KEYS = sorted(
    key for key in itertools.product(Market, Dataset) if key not in DECLARED_KEYS
)


class TestEpochUnitCutover:
    """VF-015. The acceptance criterion of issue #21, stated four ways."""

    @pytest.mark.parametrize(
        ("archive_date", "expected"),
        [
            (date(2024, 12, 31), EpochUnit.MILLISECONDS),
            (date(2025, 1, 1), EpochUnit.MICROSECONDS),
        ],
    )
    def test_spot_klines_switch_on_2025_01_01(
        self, archive_date: date, expected: EpochUnit
    ) -> None:
        resolved = resolve_archive_format(
            market=Market.SPOT, dataset=Dataset.KLINES, archive_date=archive_date
        )
        assert resolved.require_epoch_unit() is expected

    @pytest.mark.parametrize("archive_date", [date(2024, 12, 31), date(2025, 1, 1)])
    def test_futures_klines_stayed_milliseconds_across_the_same_boundary(
        self, archive_date: date
    ) -> None:
        resolved = resolve_archive_format(
            market=Market.FUTURES_UM, dataset=Dataset.KLINES, archive_date=archive_date
        )
        assert resolved.require_epoch_unit() is EpochUnit.MILLISECONDS

    def test_spot_trades_switch_on_the_same_date_as_spot_klines(self) -> None:
        """The cutover is a property of the market, and the table must not drift.

        A table that got klines right and trades wrong would produce a corpus whose
        bars and tape disagree by three orders of magnitude on one side of a date.
        """
        before = resolve_archive_format(
            market=Market.SPOT, dataset=Dataset.TRADES, archive_date=date(2024, 12, 31)
        )
        after = resolve_archive_format(
            market=Market.SPOT, dataset=Dataset.TRADES, archive_date=date(2025, 1, 1)
        )
        assert (before.require_epoch_unit(), after.require_epoch_unit()) == (
            EpochUnit.MILLISECONDS,
            EpochUnit.MICROSECONDS,
        )


class TestHeaderAndBooleanDeclarations:
    def test_futures_klines_declare_a_header_row_and_spot_klines_do_not(self) -> None:
        """VF-016. Reading a spot file as though it had a header discards the first
        real bar of the day -- always 00:00 UTC, always one row, invisible."""
        futures = resolve_archive_format(
            market=Market.FUTURES_UM, dataset=Dataset.KLINES, archive_date=date(2026, 1, 1)
        )
        spot = resolve_archive_format(
            market=Market.SPOT, dataset=Dataset.KLINES, archive_date=date(2026, 1, 1)
        )
        assert (futures.has_header_row, spot.has_header_row) == (True, False)

    def test_spot_trades_declare_python_booleans_on_both_affected_columns(self) -> None:
        """F-005. A lowercase comparison returns False for every row in the file, so
        the trade side is uniformly sign-inverted while every other column is right."""
        resolved = resolve_archive_format(
            market=Market.SPOT, dataset=Dataset.TRADES, archive_date=date(2026, 1, 1)
        )
        assert resolved.boolean_encoding is BooleanEncoding.PYTHON
        assert resolved.boolean_columns == ("is_buyer_maker", "is_best_match")

    @pytest.mark.parametrize("market", [Market.SPOT, Market.FUTURES_UM])
    def test_klines_declare_no_boolean_columns_at_all(self, market: Market) -> None:
        resolved = resolve_archive_format(
            market=market, dataset=Dataset.KLINES, archive_date=date(2026, 1, 1)
        )
        assert resolved.boolean_encoding is None
        assert resolved.boolean_columns == ()


class TestUndeclaredCombinationsRaise:
    @pytest.mark.parametrize(("market", "dataset"), UNDECLARED_KEYS, ids=str)
    def test_every_undeclared_pair_raises_and_names_itself(
        self, market: Market, dataset: Dataset
    ) -> None:
        """Generated from the enums, not hand-listed.

        A new `Dataset` member therefore cannot be added without either being declared
        in the table or arriving here as a new failing case -- which is the only way a
        list like this stays true.
        """
        with pytest.raises(DataIntegrityError) as raised:
            resolve_archive_format(market=market, dataset=dataset, archive_date=date(2026, 1, 1))
        message = str(raised.value)
        assert market.value in message
        assert dataset.value in message

    def test_a_date_before_the_declaration_raises_rather_than_extrapolating(self) -> None:
        """Spot archives do not exist before 2017-08-17, so a request for one is not a
        format question with an obvious backwards answer -- it is a bad request."""
        with pytest.raises(DataIntegrityError, match="2016-01-01"):
            resolve_archive_format(
                market=Market.SPOT, dataset=Dataset.KLINES, archive_date=date(2016, 1, 1)
            )

    def test_futures_before_the_usdm_launch_raises(self) -> None:
        with pytest.raises(DataIntegrityError, match="2019-01-01"):
            resolve_archive_format(
                market=Market.FUTURES_UM,
                dataset=Dataset.KLINES,
                archive_date=date(2019, 1, 1),
            )


class TestTheModuleHasNoDefault:
    """Defaulting is trap 1's exact mechanism, so the absence of a default is asserted
    structurally rather than trusted to review."""

    @staticmethod
    def _module_tree() -> ast.Module:
        source = Path(module_under_test.__file__).read_text(encoding="utf-8")
        return ast.parse(source)

    def test_no_archive_format_is_constructed_inside_a_function(self) -> None:
        """Every `ArchiveFormat` in the module belongs to the declaration table.

        One constructed inside `resolve_archive_format` would be a fallback whatever
        its author called it, and a fallback here is a silently wrong corpus.
        """
        constructed_in_functions: list[int] = []
        for node in ast.walk(self._module_tree()):
            if not isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef):
                continue
            constructed_in_functions.extend(
                inner.lineno
                for inner in ast.walk(node)
                if isinstance(inner, ast.Call)
                and isinstance(inner.func, ast.Name)
                and inner.func.id == "ArchiveFormat"
            )
        assert constructed_in_functions == []

    def test_resolve_never_returns_a_literal(self) -> None:
        resolver = next(
            node
            for node in ast.walk(self._module_tree())
            if isinstance(node, ast.FunctionDef) and node.name == "resolve_archive_format"
        )
        returned_constants = [
            node.lineno
            for node in ast.walk(resolver)
            if isinstance(node, ast.Return) and isinstance(node.value, ast.Constant)
        ]
        assert returned_constants == []


class TestEpochToUtc:
    def test_milliseconds_render_as_the_expected_instant(self) -> None:
        assert epoch_to_utc(CUTOVER_MS, unit=EpochUnit.MILLISECONDS, now_utc=NOW_UTC) == datetime(
            2025, 1, 1, tzinfo=UTC
        )

    def test_microseconds_render_as_the_same_instant(self) -> None:
        assert epoch_to_utc(CUTOVER_US, unit=EpochUnit.MICROSECONDS, now_utc=NOW_UTC) == datetime(
            2025, 1, 1, tzinfo=UTC
        )

    def test_microsecond_resolution_survives(self) -> None:
        """A double holds microsecond epochs exactly until 2255, which is the whole
        argument for `raw / divisor` being acceptable here and nowhere near money."""
        assert epoch_to_utc(
            CUTOVER_US + 123_456, unit=EpochUnit.MICROSECONDS, now_utc=NOW_UTC
        ) == datetime(2025, 1, 1, 0, 0, 0, 123_456, tzinfo=UTC)

    def test_milliseconds_read_as_microseconds_land_in_1970_and_raise(self) -> None:
        with pytest.raises(DataIntegrityError) as raised:
            epoch_to_utc(CUTOVER_MS, unit=EpochUnit.MICROSECONDS, now_utc=NOW_UTC)
        message = str(raised.value)
        assert str(CUTOVER_MS) in message, "the raw integer must be in the message"
        assert "us" in message, "the applied unit must be in the message"
        assert "1970" in message

    def test_microseconds_read_as_milliseconds_land_beyond_the_window_and_raise(
        self,
    ) -> None:
        with pytest.raises(DataIntegrityError) as raised:
            epoch_to_utc(CUTOVER_US, unit=EpochUnit.MILLISECONDS, now_utc=NOW_UTC)
        message = str(raised.value)
        assert str(CUTOVER_US) in message
        assert "ms" in message

    def test_the_upper_bound_is_one_day_past_the_supplied_instant(self) -> None:
        """Not `now`: an archive written seconds ago carries a timestamp fractionally
        ahead of a slightly skewed local clock, and rejecting that would be a false
        positive on every live run."""
        one_day_ahead = int((NOW_UTC.timestamp() + 86_400) * 1000)
        with pytest.raises(DataIntegrityError):
            epoch_to_utc(one_day_ahead, unit=EpochUnit.MILLISECONDS, now_utc=NOW_UTC)
        assert epoch_to_utc(
            one_day_ahead - 1000, unit=EpochUnit.MILLISECONDS, now_utc=NOW_UTC
        ) < NOW_UTC.replace(day=4)

    def test_a_naive_reference_instant_is_refused(self) -> None:
        with pytest.raises(DataIntegrityError, match="timezone-aware"):
            epoch_to_utc(
                CUTOVER_MS,
                unit=EpochUnit.MILLISECONDS,
                now_utc=datetime(2026, 8, 3),  # noqa: DTZ001
            )

    def test_a_bool_is_not_an_acceptable_epoch(self) -> None:
        """`bool` is a subclass of `int`, so an `isinstance(raw, int)` check accepts
        `True` and normalises it to 1970-01-01T00:00:00.001Z without complaint.

        There is no `type: ignore` on the call below, and that is the point: `mypy
        --strict` also accepts `True` where an `int` is annotated, so the type system
        is not the thing standing between a stray boolean column and a row at the Unix
        epoch. Only the runtime guard is.
        """
        with pytest.raises(DataIntegrityError, match="must be an int"):
            epoch_to_utc(True, unit=EpochUnit.MILLISECONDS, now_utc=NOW_UTC)

    def test_a_value_far_outside_a_double_still_raises_rather_than_overflowing(
        self,
    ) -> None:
        """`10**30 / 1000` overflows a float, and `datetime.fromtimestamp` raises
        `OverflowError`/`OSError` rather than `ValueError`. The guard must be the
        magnitude check, not the conversion."""
        with pytest.raises(DataIntegrityError):
            epoch_to_utc(10**30, unit=EpochUnit.MILLISECONDS, now_utc=NOW_UTC)

    def test_a_negative_epoch_raises(self) -> None:
        with pytest.raises(DataIntegrityError):
            epoch_to_utc(-CUTOVER_MS, unit=EpochUnit.MILLISECONDS, now_utc=NOW_UTC)


class TestTheNaiveDatetimeEncoding:
    """VF-029, the fourth trap: a declared timestamp that is not an epoch at all."""

    def test_the_metrics_declaration_says_it_is_not_an_epoch(self) -> None:
        resolved = resolve_archive_format(
            market=Market.FUTURES_UM, dataset=Dataset.METRICS, archive_date=date(2024, 1, 2)
        )
        assert resolved.timestamp_encoding is TimestampEncoding.NAIVE_UTC_DATETIME
        assert resolved.has_header_row is True

    def test_asking_the_metrics_declaration_for_an_epoch_unit_refuses(self) -> None:
        """The refusal is the point. A `None` here would be a value somebody picks a unit
        for; a raise names the dataset and the function to use instead."""
        resolved = resolve_archive_format(
            market=Market.FUTURES_UM, dataset=Dataset.METRICS, archive_date=date(2024, 1, 2)
        )
        with pytest.raises(DataIntegrityError) as raised:
            resolved.require_epoch_unit()
        message = str(raised.value)
        assert "metrics" in message
        assert "parse_naive_utc_datetime" in message

    def test_the_funding_declaration_still_answers_with_milliseconds(self) -> None:
        """The other alternative dataset is an epoch, which is why the two are declared
        rather than assumed from the fact that both live in `fking.data.alt`."""
        resolved = resolve_archive_format(
            market=Market.FUTURES_UM,
            dataset=Dataset.FUNDING_RATE,
            archive_date=date(2020, 1, 1),
        )
        assert resolved.timestamp_encoding is TimestampEncoding.EPOCH_MILLISECONDS
        assert resolved.require_epoch_unit() is EpochUnit.MILLISECONDS

    def test_a_declared_timestamp_reads_as_aware_utc(self) -> None:
        """The whole defect this encoding exists for: `fromisoformat` would return a naive
        datetime that compares equal to nothing and joins against nothing."""
        parsed = parse_naive_utc_datetime("2024-01-02 00:05:00", now_utc=NOW_UTC)

        assert parsed == datetime(2024, 1, 2, 0, 5, tzinfo=UTC)
        assert parsed.tzinfo is not None
        assert parsed.utcoffset() == datetime(2024, 1, 2, tzinfo=UTC).utcoffset()

    @pytest.mark.parametrize(
        "raw",
        [
            "2024-01-02T00:00:00",  # ISO 'T' separator
            "2024-01-02 00:00:00Z",  # a Z suffix
            "2024-01-02 00:00:00+04:00",  # an offset, the case that must never be absorbed
            "2024-01-02 00:00:00.500",  # fractional seconds
            "2024-01-02",  # a date with no time
            "1704153600000",  # a millisecond epoch, i.e. the wrong declaration
            "",
        ],
    )
    def test_anything_but_the_declared_spelling_is_refused(self, raw: str) -> None:
        with pytest.raises(DataIntegrityError, match="is not"):
            parse_naive_utc_datetime(raw, now_utc=NOW_UTC)

    def test_an_offset_is_refused_rather_than_converted(self) -> None:
        """Stated separately from the parametrised case because it is the one that would
        otherwise *work*: `fromisoformat` reads it, converts, and silently moves every row
        in the file by the offset with nothing to show for it."""
        with pytest.raises(DataIntegrityError) as raised:
            parse_naive_utc_datetime("2024-01-02 00:00:00+04:00", now_utc=NOW_UTC)
        assert "publisher changed how it spells this column" in str(raised.value)

    def test_a_shape_that_matches_but_is_not_a_real_instant_is_refused(self) -> None:
        """The pattern admits month 13 and hour 32; the calendar does not."""
        with pytest.raises(DataIntegrityError, match="not a real instant"):
            parse_naive_utc_datetime("2024-13-45 32:99:99", now_utc=NOW_UTC)

    @pytest.mark.parametrize("raw", ["2009-12-31 23:59:59", "2026-08-05 00:00:00"])
    def test_the_same_plausibility_window_applies(self, raw: str) -> None:
        """One definition of "an instant this corpus could contain", shared with
        `epoch_to_utc`: below 2010 there is no archive, and above `now + 1 day` there is
        no archive yet. `NOW_UTC` is 2026-08-03, so the second case is two days ahead."""
        with pytest.raises(DataIntegrityError, match="plausible window"):
            parse_naive_utc_datetime(raw, now_utc=NOW_UTC)

    def test_a_naive_reference_instant_is_refused(self) -> None:
        with pytest.raises(DataIntegrityError, match="timezone-aware"):
            parse_naive_utc_datetime(
                "2024-01-02 00:00:00",
                now_utc=datetime(2026, 8, 3),  # noqa: DTZ001 - the input under test
            )


class TestTheDeclarationTableIsWellFormed:
    def test_every_declared_segment_is_immutable(self) -> None:
        resolved = resolve_archive_format(
            market=Market.SPOT, dataset=Dataset.KLINES, archive_date=date(2026, 1, 1)
        )
        assert isinstance(resolved, ArchiveFormat)
        with pytest.raises(AttributeError):
            resolved.timestamp_encoding = (  # type: ignore[misc]
                TimestampEncoding.EPOCH_MILLISECONDS
            )

    @pytest.mark.parametrize(("market", "dataset"), sorted(DECLARED_KEYS), ids=str)
    def test_segments_are_contiguous_and_ordered(self, market: Market, dataset: Dataset) -> None:
        """A hole between segments is a date that resolves to nothing for no stated
        reason; an overlap is two answers to one question."""
        segments = module_under_test.DECLARED_FORMATS[market, dataset]
        for earlier, later in itertools.pairwise(segments):
            assert earlier.declared_until_date == later.declared_from_date
        assert segments[-1].declared_until_date is None

    @pytest.mark.parametrize(("market", "dataset"), sorted(DECLARED_KEYS), ids=str)
    def test_a_declared_segment_agrees_with_the_key_it_is_filed_under(
        self, market: Market, dataset: Dataset
    ) -> None:
        for segment in module_under_test.DECLARED_FORMATS[market, dataset]:
            assert (segment.market, segment.dataset) == (market, dataset)

    def test_a_dataset_with_a_boolean_encoding_names_the_columns_it_applies_to(
        self,
    ) -> None:
        for segments in module_under_test.DECLARED_FORMATS.values():
            for segment in segments:
                assert (segment.boolean_encoding is None) == (segment.boolean_columns == ())
