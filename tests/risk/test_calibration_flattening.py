"""A strategy that asserts confidence indiscriminately flattens its own map.

This is the acceptance test for the anti-gaming claim in `RISK_PHILOSOPHY.md` section 2.
If it fails, `conviction` is a number a strategy assigns to itself that multiplies
notional -- which is strategy-side sizing wearing a calibration map, and the whole
argument for `Signal` carrying a conviction rather than a quantity collapses.

The comparison that matters is between the two synthetic strategies below. One reports
`1.0` on everything and one reports a conviction that tracks its realised outcome. They
have the same trade count, the same realised return distribution and the same instrument;
the only difference is whether the reported number carries information. The first gets
one `r_used` for every signal, the second gets a spread.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Final

import pytest

from fking.risk.calibration import (
    UNCALIBRATED_FRACTION,
    ClosedTrade,
    ConvictionParameters,
    fit_calibration,
    risk_fraction_for,
)

pytestmark = pytest.mark.unit

_STRATEGY_ID: Final = "always-certain"
_EPOCH: Final = datetime(2026, 1, 1, tzinfo=UTC)
_AS_OF: Final = datetime(2027, 1, 1, tzinfo=UTC)
_PARAMETERS: Final = ConvictionParameters()

# Two hundred trades: comfortably past the hundred-trade floor, so the map is fitted and
# the flatness below is a property of the record rather than of the floor.
_TRADE_COUNT: Final = 200

# Alternating win and loss of the same magnitude. A strategy with no edge at all would be
# the easy case; this one has a real spread of outcomes and still earns no gradient,
# because none of that spread is predicted by the number it reported.
_RETURNS: Final = (Decimal("0.03"), Decimal("-0.02"))


def _history(convictions: tuple[Decimal, ...]) -> list[ClosedTrade]:
    return [
        ClosedTrade(
            strategy_id=_STRATEGY_ID,
            reported_conviction=convictions[index % len(convictions)],
            realised_return_fraction=_RETURNS[index % len(_RETURNS)],
            closed_at_utc=_EPOCH + timedelta(hours=index),
        )
        for index in range(_TRADE_COUNT)
    ]


def _risk_fractions(history: list[ClosedTrade]) -> set[Decimal]:
    calibration = fit_calibration(
        history, strategy_id=_STRATEGY_ID, as_of_utc=_AS_OF, parameters=_PARAMETERS
    )
    return {
        risk_fraction_for(calibration.calibrated(trade.reported_conviction), parameters=_PARAMETERS)
        for trade in history
    }


def test_a_strategy_reporting_full_conviction_on_every_signal_is_sized_identically() -> None:
    """One reported conviction, one bucket, one `r_used`. The channel carries nothing."""
    history = _history((Decimal("1"),))
    calibration = fit_calibration(
        history, strategy_id=_STRATEGY_ID, as_of_utc=_AS_OF, parameters=_PARAMETERS
    )

    assert calibration.is_fitted
    assert len(calibration.buckets) == 1
    assert _risk_fractions(history) == {
        risk_fraction_for(UNCALIBRATED_FRACTION, parameters=_PARAMETERS)
    }


def test_a_flat_map_sizes_exactly_as_an_unfitted_one_does() -> None:
    """The two ways of having no gradient produce the same number, deliberately.

    A strategy with no record and a strategy whose record says its conviction predicts
    nothing are in the same epistemic position, and giving them different risk budgets
    would mean the budget encodes trade count rather than evidence.
    """
    flattened = fit_calibration(
        _history((Decimal("1"),)),
        strategy_id=_STRATEGY_ID,
        as_of_utc=_AS_OF,
        parameters=_PARAMETERS,
    )
    unfitted = fit_calibration(
        [], strategy_id=_STRATEGY_ID, as_of_utc=_AS_OF, parameters=_PARAMETERS
    )
    for conviction in (Decimal("0"), Decimal("0.5"), Decimal("0.87"), Decimal("1")):
        assert flattened.calibrated(conviction) == unfitted.calibrated(conviction)


def test_spreading_reported_conviction_without_earning_it_still_flattens() -> None:
    """Ten distinct convictions, ten buckets, and outcomes uncorrelated with any of them.

    The gaming attempt this catches is subtler than reporting `1.0` on everything: report
    a full spread of convictions so the map has ten buckets to fit, and hope the fit hands
    the top bucket `r_max`. It does not, because the isotonic step pools buckets whose
    realised means are out of order, and a record with no relationship between reported
    and realised collapses to a single pooled level.
    """
    # Conviction cycles with period 10 and the outcome cycles with period 2, so every
    # bucket holds the same 50/50 mix of the two returns and every bucket mean is equal.
    convictions = tuple(Decimal(step) / Decimal("10") for step in range(1, 11))
    assert len(_risk_fractions(_history(convictions))) == 1


def test_conviction_that_predicts_outcome_earns_a_gradient() -> None:
    """The control. Without this the three tests above pass on a map that is always flat.

    A calibration that flattens everything is not a calibration, it is a constant, and
    a constant would satisfy every anti-gaming assertion in this file.
    """
    history = [
        ClosedTrade(
            strategy_id=_STRATEGY_ID,
            reported_conviction=Decimal(index % 10) / Decimal("10"),
            # Realised return rises with reported conviction: this strategy's number
            # means something, and the map is what lets it be paid for that.
            realised_return_fraction=Decimal(index % 10) / Decimal("100") - Decimal("0.04"),
            closed_at_utc=_EPOCH + timedelta(hours=index),
        )
        for index in range(_TRADE_COUNT)
    ]
    calibration = fit_calibration(
        history, strategy_id=_STRATEGY_ID, as_of_utc=_AS_OF, parameters=_PARAMETERS
    )

    assert calibration.calibrated(Decimal("0")) == Decimal("0")
    assert calibration.calibrated(Decimal("1")) == Decimal("1")
    assert (
        risk_fraction_for(calibration.calibrated(Decimal("0.9")), parameters=_PARAMETERS)
        == _PARAMETERS.risk_fraction_max_per_trade
    )
    assert (
        risk_fraction_for(calibration.calibrated(Decimal("0")), parameters=_PARAMETERS)
        == _PARAMETERS.risk_fraction_min_per_trade
    )
