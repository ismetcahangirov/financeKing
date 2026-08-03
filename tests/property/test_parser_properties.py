"""Properties of the archive parsers, over generated rows and over every recorded fixture.

Example-based tests confirm the rows somebody thought of. The properties here are the ones
whose violation is silent: a rejection that is not counted, a decimal that no longer equals
its own source text, a boolean token that two encodings both accept.

The generators build rows in Binance's own *textual* shape rather than building records and
serialising them. That direction matters. Serialising a record and parsing it back tests
that this module agrees with itself, which it always will; generating text tests the thing
the parser actually faces.

The fixture and its specs are resolved once at import. Resolving them inside a `@given`
body would read four provenance files per generated example, which turns a property test
into a filesystem benchmark and trips Hypothesis's deadline for reasons unrelated to the
property.
"""

from __future__ import annotations

import dataclasses
from datetime import UTC, date, datetime
from decimal import Decimal
from typing import Final

import pytest
from hypothesis import given
from hypothesis import strategies as st

from fking.data.format_resolver import BooleanEncoding, Dataset, Market
from fking.data.loaders import (
    KLINE_COLUMNS,
    TRADE_COLUMNS,
    ArchiveRecord,
    IngestionSpec,
    KlineRecord,
    TradeRecord,
    parse_archive,
    parse_klines,
)
from fking.data.loaders._fields import BOOLEAN_TOKENS
from fking.platform.errors import DataIntegrityError
from tests.support import archive_fixtures
from tests.support.archive_fixtures import RecordedArchive

pytestmark = [pytest.mark.property, pytest.mark.unit]

SPOT_KLINES: Final[RecordedArchive] = archive_fixtures.find(
    market=Market.SPOT, dataset=Dataset.KLINES, archive_date=date(2025, 1, 2), whole=False
)
STRICT_SPEC: Final[IngestionSpec] = SPOT_KLINES.spec()
# Tolerates every rejection, so a property can inspect the tally. The production 0.1%
# ceiling would raise before the counts could be observed, and it is asserted by example in
# tests/data/test_parsers.py, where the boundary can be named.
PERMISSIVE_SPEC: Final[IngestionSpec] = SPOT_KLINES.spec(max_rejection_fraction=Decimal("1"))

# The window every recorded fixture sits inside, in microseconds. Bounded well away from the
# plausibility limits so a generated epoch never accidentally tests the range guard -- that
# is asserted by example, where 1970 and the year 56,000 can be named.
_MIN_EPOCH_US: Final[int] = int(datetime(2020, 1, 1, tzinfo=UTC).timestamp()) * 1_000_000
_MAX_EPOCH_US: Final[int] = int(datetime(2026, 1, 1, tzinfo=UTC).timestamp()) * 1_000_000

# Eight decimal places, matching Binance's own formatting. Always positive: a price of zero
# is a separate, named rejection with its own example test.
_PRICE_TOKENS: Final[st.SearchStrategy[str]] = st.decimals(
    min_value=Decimal("0.00000001"),
    max_value=Decimal("1000000"),
    places=8,
    allow_nan=False,
    allow_infinity=False,
).map(lambda quantity: format(quantity, "f"))

_FIELD_TOKENS: Final[st.SearchStrategy[str]] = st.one_of(
    _PRICE_TOKENS,
    st.integers(min_value=0, max_value=10_000).map(str),
    st.integers(min_value=_MIN_EPOCH_US, max_value=_MAX_EPOCH_US).map(str),
    # The spellings that construct silently and mean something else.
    st.sampled_from(["", "NaN", "Infinity", "1_0", " 1", "0x10", "True", "null", "-1"]),
)

# Column indices in the kline layout, for the round-trip assertion below.
_KLINE_DECIMAL_COLUMNS: Final[dict[str, int]] = {
    "open_quote_price": 1,
    "high_quote_price": 2,
    "low_quote_price": 3,
    "close_quote_price": 4,
    "base_volume": 5,
    "quote_volume": 7,
    "taker_buy_base_volume": 9,
    "taker_buy_quote_volume": 10,
}
# Two: one spelling for True and one for False. Any other count means an encoding table
# admits a token whose meaning nobody declared.
TOKENS_PER_ENCODING: Final[int] = 2

_TRADE_DECIMAL_COLUMNS: Final[dict[str, int]] = {
    "quote_price": 1,
    "base_quantity": 2,
    "quote_quantity": 3,
}


@st.composite
def kline_rows(draw: st.DrawFn) -> str:
    """A row with the kline field count, whose fields may be plausible or not."""
    return ",".join(draw(_FIELD_TOKENS) for _ in KLINE_COLUMNS)


@st.composite
def kline_payloads(draw: st.DrawFn) -> bytes:
    """A CSV body of kline-shaped rows, some of which are deliberately a field short.

    The first row is always a valid one. The header gate reads only the first field of the
    first line, so a generated row there would decide *file-level* acceptance and every
    property below would be measuring the gate instead of the row loop. Trap 2 is asserted
    by example in `tests/data/test_parsers.py`, where both directions can be named.
    """
    rows = draw(st.lists(kline_rows(), min_size=1, max_size=12))
    drops = draw(st.lists(st.booleans(), min_size=len(rows), max_size=len(rows)))
    lines = [
        row.rsplit(",", maxsplit=1)[0] if drop else row
        for row, drop in zip(rows, drops, strict=True)
    ]
    return ("\n".join([_kline_row_with_prices("1.00000000").decode().strip(), *lines])).encode(
        "utf-8"
    ) + b"\n"


def _decimal_columns_of(record: ArchiveRecord) -> dict[str, int]:
    return _KLINE_DECIMAL_COLUMNS if isinstance(record, KlineRecord) else _TRADE_DECIMAL_COLUMNS


class TestConservation:
    """The identity that makes "a run reporting only rows_out" unrepresentable."""

    @given(payload=kline_payloads())
    def test_every_row_is_either_a_record_or_a_counted_rejection(self, payload: bytes) -> None:
        records, outcome = parse_klines(payload, PERMISSIVE_SPEC, source="generated")

        assert outcome.rows_out == len(records)
        assert outcome.rows_in == outcome.rows_out + outcome.rows_rejected
        assert sum(outcome.rejection_reasons.values()) == outcome.rows_rejected
        # No reason is recorded with a zero count: a zero on a dashboard reads as "checked
        # and clean", which is the opposite of "never observed".
        assert all(tally > 0 for tally in outcome.rejection_reasons.values())

    @given(payload=kline_payloads())
    def test_the_reported_timestamps_belong_to_accepted_records_only(self, payload: bytes) -> None:
        """A result describing rejected rows would describe data the caller does not have."""
        records, outcome = parse_klines(payload, PERMISSIVE_SPEC, source="generated")

        if not records:
            assert outcome.first_event_time_utc is None
            assert outcome.last_event_time_utc is None
        else:
            assert outcome.first_event_time_utc == records[0].open_time_utc
            assert outcome.last_event_time_utc == records[-1].open_time_utc


class TestDecimalRoundTrip:
    """`Decimal(raw) == parsed`, for every row of every recorded fixture.

    The acceptance criterion of issue #23, asserted over the real corpus rather than over
    generated text: a generated token proves the parser is self-consistent, and Binance's own
    eight-decimal formatting proves it is correct.
    """

    @pytest.mark.parametrize(
        "recorded", archive_fixtures.csv_fragments(), ids=lambda recorded: recorded.label
    )
    def test_every_parsed_quantity_equals_its_exact_source_substring(
        self, recorded: RecordedArchive
    ) -> None:
        payload = recorded.read()
        rows = (
            payload.decode().splitlines()[1:]
            if recorded.has_header_row
            else (payload.decode().splitlines())
        )
        records, _ = parse_archive(payload, recorded.spec(), source=recorded.label)

        assert len(records) == len(rows)
        for record, row in zip(records, rows, strict=True):
            fields = row.split(",")
            for name, column in _decimal_columns_of(record).items():
                parsed = getattr(record, name)
                assert isinstance(parsed, Decimal)
                assert parsed == Decimal(fields[column])
                # Not just equal -- identically spelled. A value that had passed through a
                # float would print as 94591.78 where the file says 94591.78000000, and the
                # `==` above would still hold.
                assert str(parsed) == fields[column]

    @given(token=_PRICE_TOKENS)
    def test_a_generated_price_survives_the_row_round_trip(self, token: str) -> None:
        records, outcome = parse_klines(
            _kline_row_with_prices(token), STRICT_SPEC, source="generated"
        )

        assert outcome.rows_rejected == 0
        assert records[0].open_quote_price == Decimal(token)
        assert records[0].close_quote_price == Decimal(token)

    def test_every_decimal_field_on_every_record_type_is_covered(self) -> None:
        """Guards the two index tables above against a field added without a mapping."""
        for record_type, columns in (
            (KlineRecord, _KLINE_DECIMAL_COLUMNS),
            (TradeRecord, _TRADE_DECIMAL_COLUMNS),
        ):
            declared = {
                field.name
                for field in dataclasses.fields(record_type)
                if field.type in {"Decimal", Decimal}
            }
            assert declared == set(columns), record_type.__name__


class TestBooleanEncodingsAreDisjoint:
    """No token may mean one thing under one encoding and another under a second.

    If the tables overlapped, a drifted encoding could be accepted under the old declaration
    with the opposite value -- trap 3 with a counter that never increments.
    """

    def test_no_token_is_accepted_by_more_than_one_encoding(self) -> None:
        seen: dict[str, BooleanEncoding] = {}
        for encoding, tokens in BOOLEAN_TOKENS.items():
            for token in tokens:
                assert token not in seen, (
                    f"{token!r} is accepted under both {seen.get(token)} and {encoding}"
                )
                seen[token] = encoding

    def test_every_encoding_defines_exactly_one_true_and_one_false(self) -> None:
        for encoding, tokens in BOOLEAN_TOKENS.items():
            assert len(tokens) == TOKENS_PER_ENCODING, encoding
            assert set(tokens.values()) == {True, False}, encoding

    def test_every_declared_encoding_has_a_token_table(self) -> None:
        assert set(BOOLEAN_TOKENS) == set(BooleanEncoding)


class TestPurity:
    """No clock, no state: the same bytes and spec produce the same answer every time."""

    @given(payload=kline_payloads())
    def test_parsing_is_deterministic(self, payload: bytes) -> None:
        assert parse_klines(payload, PERMISSIVE_SPEC, source="generated") == parse_klines(
            payload, PERMISSIVE_SPEC, source="generated"
        )

    @given(payload=kline_payloads())
    def test_a_refusal_never_returns_a_partial_corpus(self, payload: bytes) -> None:
        """Under the production ceiling a payload either parses within tolerance, or raises."""
        try:
            records, outcome = parse_klines(payload, STRICT_SPEC, source="generated")
        except DataIntegrityError:
            return
        assert Decimal(outcome.rows_rejected) <= Decimal("0.001") * outcome.rows_in
        assert len(records) == outcome.rows_out


def _kline_row_with_prices(token: str) -> bytes:
    """One valid kline row whose four prices are all `token`.

    Equal prices are a real shape, not a contrivance: an untraded minute on an illiquid
    symbol has open == high == low == close, and it must survive the bracket check.
    """
    fields = [
        str(_MIN_EPOCH_US),
        token,
        token,
        token,
        token,
        "0",
        str(_MIN_EPOCH_US + 59_999_999),
        "0",
        "0",
        "0",
        "0",
        "0",
    ]
    return (",".join(fields) + "\n").encode()


def test_the_generated_row_shape_matches_the_declared_layouts() -> None:
    """Guards the generators: a row of the wrong width would test only the width check."""
    assert len(_kline_row_with_prices("1").decode().strip().split(",")) == len(KLINE_COLUMNS)
    assert len(TRADE_COLUMNS) < len(KLINE_COLUMNS)
