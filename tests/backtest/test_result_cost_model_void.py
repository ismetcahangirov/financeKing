"""A testnet-named cost model marks the result `not_credible`, and voids every result
carrying that cost model version -- regardless of net return, Sharpe or sample size.
"""

from __future__ import annotations

from decimal import Decimal

import pytest

from fking.backtest.results import (
    AuditCheck,
    AuditStatus,
    CredibilityInvariantError,
    ResultCredibility,
    check_cost_model,
)
from tests.backtest.results_support import result_for

pytestmark = pytest.mark.unit

_TESTNET_SOURCES = (
    "binance_futures_testnet_2026-05",
    "TESTNET",
    "Test-Net",
    "binance um test net 2026-05",
)


@pytest.mark.parametrize("source", _TESTNET_SOURCES)
def test_check_cost_model_fires_on_any_spelling_of_testnet(source: str) -> None:
    result = check_cost_model(calibration_source=source, round_trip_cost_bp=Decimal("4"))
    assert result.status is AuditStatus.FAIL
    assert result.check is AuditCheck.COST_MODEL


def test_check_cost_model_fires_on_a_zero_round_trip_cost() -> None:
    result = check_cost_model(
        calibration_source="binance_um_production_2026-03..2026-05",
        round_trip_cost_bp=Decimal("0"),
    )
    assert result.status is AuditStatus.FAIL
    assert "not positive" in result.evidence


def test_check_cost_model_passes_a_production_source_with_a_real_charge() -> None:
    result = check_cost_model(
        calibration_source="binance_um_production_2026-03..2026-05",
        round_trip_cost_bp=Decimal("4"),
    )
    assert result.status is AuditStatus.PASS


def test_a_testnet_cost_model_cannot_be_reported_credible_at_any_net_return() -> None:
    """A large, clean-looking net return does not rescue a testnet-calibrated model."""
    with pytest.raises(CredibilityInvariantError, match="not_credible"):
        result_for(
            cost_model_calibration_source="binance_futures_testnet_2026-05",
            credibility=ResultCredibility.CREDIBLE,
        )


def test_a_testnet_cost_model_must_be_reported_not_credible_not_left_unaudited() -> None:
    """The disqualification is mandatory, not merely permitted."""
    with pytest.raises(CredibilityInvariantError, match="unaudited"):
        result_for(
            cost_model_calibration_source="binance_futures_testnet_2026-05",
            credibility=ResultCredibility.UNAUDITED,
        )

    voided = result_for(
        cost_model_calibration_source="binance_futures_testnet_2026-05",
        credibility=ResultCredibility.NOT_CREDIBLE,
    )
    assert voided.credibility is ResultCredibility.NOT_CREDIBLE
    assert voided.cost_model_calibration_source == "binance_futures_testnet_2026-05"
