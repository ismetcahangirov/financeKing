"""The refusal paths of `fking.risk.metrics`: the inputs whose *correct* behaviour is a
`DomainError`, not a number.

Property tests cover the arithmetic invariants; these cover specific inputs a fuzzer is
unlikely to hit by chance and that must not be allowed to produce a plausible-looking
number -- an empty sample silently reporting zero risk is far more dangerous than a loud
failure.
"""

from __future__ import annotations

from decimal import Decimal
from typing import Final

import pytest

from fking.domain import DomainError
from fking.risk.metrics import (
    beta_to_market,
    historical_tail_risk,
    volatility_against_target,
)

pytestmark = pytest.mark.unit

_ZERO: Final = Decimal("0")


def test_var_cvar_refuse_an_empty_series() -> None:
    with pytest.raises(DomainError, match="empty series"):
        historical_tail_risk((), confidence_ratio=Decimal("0.99"))


@pytest.mark.parametrize(
    "confidence_ratio", [Decimal("0"), Decimal("1"), Decimal("1.5"), Decimal("-0.1")]
)
def test_var_cvar_refuse_a_confidence_outside_the_open_unit_interval(
    confidence_ratio: Decimal,
) -> None:
    with pytest.raises(DomainError, match="confidence_ratio"):
        historical_tail_risk((Decimal("0.01"),) * 10, confidence_ratio=confidence_ratio)


def test_var_cvar_refuse_a_non_finite_observation() -> None:
    series = (Decimal("NaN"), Decimal("0.01"), Decimal("-0.01"))
    with pytest.raises(DomainError, match="non-finite"):
        historical_tail_risk(series, confidence_ratio=Decimal("0.99"))


def test_beta_refuses_asset_and_market_series_of_different_lengths() -> None:
    with pytest.raises(DomainError, match="asset series has"):
        beta_to_market(
            asset_daily_return_ratio_series=(Decimal("0.01"),) * 5,
            market_daily_return_ratio_series=(Decimal("0.01"),) * 4,
            stress_asset_daily_return_ratio_series=(Decimal("0.01"),) * 3,
            stress_market_daily_return_ratio_series=(Decimal("0.01"),) * 3,
        )


def test_beta_refuses_an_empty_window() -> None:
    with pytest.raises(DomainError, match="no observations"):
        beta_to_market(
            asset_daily_return_ratio_series=(),
            market_daily_return_ratio_series=(),
            stress_asset_daily_return_ratio_series=(Decimal("0.01"),) * 3,
            stress_market_daily_return_ratio_series=(Decimal("0.02"),) * 3,
        )


def test_beta_refuses_a_market_series_with_zero_variance() -> None:
    """A BTC series that never moved cannot anchor a beta; it must not be read as zero risk."""
    with pytest.raises(DomainError, match="zero variance"):
        beta_to_market(
            asset_daily_return_ratio_series=(Decimal("0.01"), Decimal("-0.01"), Decimal("0.02")),
            market_daily_return_ratio_series=(_ZERO, _ZERO, _ZERO),
            stress_asset_daily_return_ratio_series=(Decimal("0.01"), Decimal("-0.01")),
            stress_market_daily_return_ratio_series=(Decimal("0.03"), Decimal("-0.03")),
        )


def test_volatility_against_target_refuses_an_empty_series() -> None:
    with pytest.raises(DomainError, match="realised volatility"):
        volatility_against_target(
            (),
            volatility_floor_annualised_ratio=Decimal("0.1"),
            target_annualised_ratio=Decimal("0.12"),
        )


@pytest.mark.parametrize("target", [Decimal("0"), Decimal("-0.05")])
def test_volatility_against_target_refuses_a_non_positive_target(target: Decimal) -> None:
    with pytest.raises(DomainError, match="target_annualised_ratio"):
        volatility_against_target(
            (Decimal("0.01"),) * 30,
            volatility_floor_annualised_ratio=Decimal("0.1"),
            target_annualised_ratio=target,
        )
