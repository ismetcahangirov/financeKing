"""A cost model calibrated from testnet cannot be constructed.

This is the acceptance test for the rule that `CLAUDE.md` section 2 states as a
non-negotiable, and the reason it is a test rather than a review item is that a review
item is applied by the person who wants the run to proceed.

The measurement behind the rule: Binance USDⓈ-M futures testnet showed a median BTCUSDT
spread of **7.5 bp against production's 0.16 bp** -- a factor of 47 -- with roughly 10x
inflated volume. The direction of the resulting error is *not* a defence. Testnet is
pessimistic on spread and optimistic on fill and capacity simultaneously, and the failure
this project's design notes actually record is the inverted config, `7.5` entered as
`0.075` bp, producing a model 2x cheaper than production and making every strategy look
brilliant.
"""

from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal

import pytest

from fking.backtest.costs import (
    CalibrationProvenanceError,
    CostModel,
    SpreadObservation,
    calibrate_spread_profile,
)
from tests.backtest.test_cost_fixtures import cost_model

pytestmark = pytest.mark.unit

TESTNET_SOURCES = [
    "testnet",
    "TESTNET",
    "TestNet",
    "binance_um_testnet_2026-03..2026-05",
    "binance-um-TESTNET-2026",
    # Separators are stripped before the containment test, so a spelling that would slip
    # past a plain `"testnet" in source.lower()` is caught too.
    "binance um test net 2026",
    "binance_um_test-net",
    "BINANCE.UM.TEST_NET.2026",
]


@pytest.mark.parametrize("calibration_source", TESTNET_SOURCES)
def test_a_testnet_calibration_source_is_refused_at_construction(calibration_source: str) -> None:
    with pytest.raises(CalibrationProvenanceError, match="names testnet"):
        cost_model(calibration_source=calibration_source)


def test_the_refusal_is_not_collected_into_a_validation_error() -> None:
    """It must escape pydantic's error collection with its own type intact.

    A `ValidationError` reads as one field among several failing a bound, and a caller
    that summarises it into a log line loses the only thing that matters here. Keeping
    `CalibrationProvenanceError` outside the `ValueError`/`AssertionError` pair pydantic
    collects is what makes the failure name the thing that is wrong.
    """
    with pytest.raises(CalibrationProvenanceError) as refused:
        cost_model(calibration_source="binance_um_testnet_2026")
    assert not isinstance(refused.value, ValueError)


def test_a_blank_calibration_source_is_refused() -> None:
    """An empty provenance records nothing an investigator could check months later."""
    with pytest.raises(CalibrationProvenanceError, match="must name where"):
        cost_model(calibration_source="   ")


@pytest.mark.parametrize(
    "calibration_source",
    [
        "binance_um_production_2026-03..2026-05",
        "binance_spot_production_2026-01",
        "production archive, data.binance.vision, 2026-05",
    ],
)
def test_a_production_calibration_source_is_accepted(calibration_source: str) -> None:
    assert cost_model(calibration_source=calibration_source).calibration_source == (
        calibration_source
    )


def test_the_recorded_source_survives_onto_the_model() -> None:
    """`BACKTEST_ENGINE.md` section 7 reports it as a credibility metric on every run."""
    model = cost_model()
    assert isinstance(model, CostModel)
    assert "production" in model.calibration_source


def test_calibration_from_testnet_observations_is_refused_before_a_model_exists(
    hourly_observations: list[SpreadObservation],
) -> None:
    """The same refusal one layer earlier, where the samples are still in hand."""
    with pytest.raises(CalibrationProvenanceError, match="names testnet"):
        calibrate_spread_profile(
            hourly_observations, calibration_source="binance_um_testnet_2026-05"
        )


@pytest.fixture
def hourly_observations() -> list[SpreadObservation]:
    return [
        SpreadObservation(
            observed_at_utc=datetime(2026, 5, 14, hour, 0, tzinfo=UTC),
            spread_bps=Decimal("0.16"),
        )
        for hour in range(24)
    ]
