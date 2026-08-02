"""The construction-time validators, exercised through the types that use them.

Each of these is a failure that produces no exception anywhere downstream if it is
allowed through -- a naive timestamp joins cleanly against another naive timestamp, and
a float-derived Decimal compares, sorts and sums without complaint.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta, timezone
from decimal import Decimal

import pytest

from fking.domain import DomainError
from fking.domain._guards import (
    require_asset_code,
    require_decimal,
    require_fraction,
    require_non_negative_decimal,
    require_positive_decimal,
    require_positive_duration,
    require_text,
    require_utc,
)

pytestmark = pytest.mark.unit

BAKU = timezone(timedelta(hours=4))


class TestRequireUtc:
    def test_accepts_an_aware_utc_datetime(self) -> None:
        moment = datetime(2026, 8, 1, 12, 0, tzinfo=UTC)
        assert require_utc(moment, "event_time_utc") is moment

    def test_rejects_a_naive_datetime(self) -> None:
        with pytest.raises(DomainError, match="timezone-aware"):
            require_utc(datetime(2026, 8, 1, 12, 0), "event_time_utc")  # noqa: DTZ001

    def test_rejects_an_aware_non_utc_datetime_rather_than_converting(self) -> None:
        """Converting would launder a wrong guess made three modules upstream.

        The value below is the same instant as 12:00 UTC, so a converting
        implementation would accept it and be right -- until the day the upstream
        offset is wrong, when it is silently four hours off in every bar alignment.
        """
        with pytest.raises(DomainError, match="must be UTC"):
            require_utc(datetime(2026, 8, 1, 16, 0, tzinfo=BAKU), "event_time_utc")

    def test_rejects_a_non_datetime(self) -> None:
        with pytest.raises(DomainError, match="must be a datetime"):
            require_utc("2026-08-01T12:00:00+00:00", "event_time_utc")


class TestRequireDecimal:
    def test_accepts_an_exact_decimal(self) -> None:
        assert require_decimal(Decimal("0.1"), "quote_price") == Decimal("0.1")

    def test_names_float_separately_from_every_other_wrong_type(self) -> None:
        with pytest.raises(DomainError, match="already rounded"):
            require_decimal(0.1, "quote_price")

    def test_rejects_a_string(self) -> None:
        with pytest.raises(DomainError, match="must be a Decimal"):
            require_decimal("0.1", "quote_price")

    @pytest.mark.parametrize("candidate", ["NaN", "Infinity", "-Infinity"])
    def test_rejects_non_finite_decimals(self, candidate: str) -> None:
        """`Decimal("NaN") == Decimal("NaN")` is False.

        One of these entering a position turns every equality-based reconciliation
        downstream into a permanent mismatch that no retry clears.
        """
        with pytest.raises(DomainError, match="must be finite"):
            require_decimal(Decimal(candidate), "quote_price")

    def test_rejects_zero_where_positive_is_required(self) -> None:
        with pytest.raises(DomainError, match="must be positive"):
            require_positive_decimal(Decimal("0"), "base_quantity")

    def test_rejects_a_negative_where_non_negative_is_required(self) -> None:
        with pytest.raises(DomainError, match="must not be negative"):
            require_non_negative_decimal(Decimal("-0.01"), "fee_quote")

    def test_accepts_zero_where_non_negative_is_required(self) -> None:
        assert require_non_negative_decimal(Decimal("0"), "fee_quote") == Decimal("0")


class TestRequireFraction:
    @pytest.mark.parametrize("candidate", ["0", "0.5", "1"])
    def test_accepts_the_closed_unit_interval(self, candidate: str) -> None:
        assert require_fraction(Decimal(candidate), "conviction") == Decimal(candidate)

    @pytest.mark.parametrize("candidate", ["-0.0001", "1.0001", "100"])
    def test_rejects_anything_outside_it(self, candidate: str) -> None:
        """`100` is the interesting case: a percent written into a fraction field."""
        with pytest.raises(DomainError, match=r"\[0, 1\]"):
            require_fraction(Decimal(candidate), "conviction")


class TestRequirePositiveDuration:
    def test_accepts_a_positive_timedelta(self) -> None:
        assert require_positive_duration(timedelta(hours=8), "horizon") == timedelta(hours=8)

    @pytest.mark.parametrize("candidate", [timedelta(0), timedelta(seconds=-1)])
    def test_rejects_zero_and_negative(self, candidate: timedelta) -> None:
        with pytest.raises(DomainError, match="must be positive"):
            require_positive_duration(candidate, "horizon")

    def test_rejects_a_number_of_seconds(self) -> None:
        with pytest.raises(DomainError, match="must be a timedelta"):
            require_positive_duration(28_800, "horizon")


class TestRequireText:
    def test_accepts_a_non_blank_string(self) -> None:
        assert require_text("breakout-4h", "strategy_id") == "breakout-4h"

    @pytest.mark.parametrize("candidate", ["", "   ", "\n\t"])
    def test_rejects_blank_strings(self, candidate: str) -> None:
        with pytest.raises(DomainError, match="must not be blank"):
            require_text(candidate, "rationale")

    def test_rejects_a_non_string(self) -> None:
        with pytest.raises(DomainError, match="must be a string"):
            require_text(None, "rationale")


class TestRequireAssetCode:
    @pytest.mark.parametrize("candidate", ["BTC", "USDT", "1000SATS"])
    def test_accepts_uppercase_ascii_alphanumeric_codes(self, candidate: str) -> None:
        assert require_asset_code(candidate, "base_asset") == candidate

    # The third case is "BT" followed by U+0421, CYRILLIC CAPITAL LETTER ES. RUF001
    # flags it as an ambiguous homoglyph, which is precisely the property under test:
    # a code that renders as BTC and is not BTC must be refused, and there is no way
    # to write that test without the homoglyph in it.
    @pytest.mark.parametrize("candidate", ["btc", "BTC-PERP", "BTС", "BTC "])  # noqa: RUF001
    def test_rejects_anything_else(self, candidate: str) -> None:
        """A homoglyph is refused rather than normalised.

        Normalising would echo a different byte sequence back to the venue than the one
        it sent, so the failure has to surface at ingestion instead.
        """
        with pytest.raises(DomainError, match=r"uppercase ASCII|must not be blank"):
            require_asset_code(candidate, "base_asset")
