"""The conviction floor, the audit row it writes, and what the map refuses to be handed.

Two groups of behaviour, and the second is the reason this file is longer than the first.

**The floor discards, it does not shrink.** `RISK_PHILOSOPHY.md` section 2: near-zero
conviction is the absence of an opinion, and the correct response to the absence of an
opinion is no position rather than a tiny one whose expected edge cannot cover its own
round trip. The discard is an audited `Rejection`, because a strategy quietly emitting
sub-floor convictions all day is a discipline signal, and a silent drop is not a signal.

**The map refuses inputs whose failure would otherwise be silent.** A map fitted after
the decision it is being used for, a map belonging to a different strategy, a flat signal:
each of these produces a plausible number if it is allowed through, and a plausible wrong
number in the risk path is the failure mode this whole module exists to remove.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Final

import pytest

from fking.domain import Direction, Instrument, Signal, Venue
from fking.risk.calibration import (
    CalibrationError,
    CalibrationMap,
    ClosedTrade,
    ConvictionAssessment,
    ConvictionParameters,
    assess_conviction,
    fit_calibration,
)

pytestmark = pytest.mark.unit

_STRATEGY_ID: Final = "trailing-return-v3"
_DECIDED_AT: Final = datetime(2026, 6, 1, 12, tzinfo=UTC)
_PARAMETERS: Final = ConvictionParameters()

# A trade floor above the compiled-in one: configuration may demand more evidence than the
# hundred trades `RISK_PHILOSOPHY.md` section 2 specifies, never less.
_RAISED_TRADE_FLOOR: Final = 250

BTCUSDT: Final = Instrument(
    venue=Venue.BINANCE_SPOT_TESTNET,
    symbol="BTCUSDT",
    base_asset="BTC",
    quote_asset="USDT",
    tick_size=Decimal("0.01"),
    lot_step=Decimal("0.00001"),
    min_notional_quote=Decimal("10"),
)


def _signal(
    conviction: Decimal,
    *,
    strategy_id: str = _STRATEGY_ID,
    direction: Direction = Direction.LONG,
) -> Signal:
    return Signal(
        strategy_id=strategy_id,
        instrument=BTCUSDT,
        direction=direction,
        conviction=conviction,
        horizon=timedelta(hours=4),
        invalidation_quote_price=None if direction is Direction.FLAT else Decimal("58000"),
        rationale="trailing return crossed the entry threshold",
        decided_at_utc=_DECIDED_AT,
    )


def _unfitted(
    *, strategy_id: str = _STRATEGY_ID, as_of_utc: datetime = _DECIDED_AT
) -> CalibrationMap:
    return fit_calibration([], strategy_id=strategy_id, as_of_utc=as_of_utc, parameters=_PARAMETERS)


def _assess(signal: Signal, *, calibration: CalibrationMap | None = None) -> ConvictionAssessment:
    return assess_conviction(
        signal=signal,
        calibration=_unfitted() if calibration is None else calibration,
        parameters=_PARAMETERS,
        decided_at_utc=_DECIDED_AT,
    )


def test_a_signal_below_the_floor_is_rejected_with_a_structured_reason() -> None:
    assessment = _assess(_signal(Decimal("0.14")))

    assert not assessment.is_approved
    assert assessment.risk_fraction_used is None
    assert assessment.rejection is not None
    assert assessment.rejection.binding_limit_name == "conviction_floor"
    assert assessment.rejection.rejected_at_utc == _DECIDED_AT
    # The reason carries both numbers. A refusal that states only the threshold leaves the
    # reader unable to tell a signal that missed by a hair from one that missed by a mile,
    # and those are different conversations with the strategy that emitted them.
    assert "0.14" in assessment.rejection.reason
    assert "0.15" in assessment.rejection.reason


def test_a_signal_exactly_on_the_floor_is_admitted() -> None:
    """Equality is legal, as it is for every other bound in this package."""
    assessment = _assess(_signal(_PARAMETERS.conviction_floor))

    assert assessment.is_approved
    assert assessment.risk_fraction_used is not None


def test_the_rejected_audit_row_names_the_floor_and_the_map_it_was_read_against() -> None:
    """The row a reader reconstructs the decision from, six months later.

    Every number in it is a string: the payload lands in `jsonb`, and a JSON encoder that
    has not been told otherwise turns a `Decimal` into a float on the way into a table
    that can never be corrected.
    """
    payload = _assess(_signal(Decimal("0.02"))).audit_payload()

    assert payload["verdict"] == "rejected"
    assert payload["binding_limit_name"] == "conviction_floor"
    assert payload["reported_conviction"] == "0.02"
    assert payload["conviction_floor"] == "0.15"
    assert payload["risk_fraction_used"] is None
    assert payload["is_calibrated"] is False
    assert payload["calibration_observation_count"] == 0
    assert payload["calibration_available_at_utc"] == _DECIDED_AT.isoformat()
    assert all(not isinstance(entry, float) for entry in payload.values()), (
        "a float in an append-only audit row cannot be corrected later"
    )


def test_the_approved_audit_row_carries_the_fraction_that_will_size_the_position() -> None:
    payload = _assess(_signal(Decimal("0.8"))).audit_payload()

    assert payload["verdict"] == "approved"
    assert payload["binding_limit_name"] is None
    # An unfitted map returns 0.5, so r_used is the midpoint of the 0.25%-1.00% band.
    assert payload["calibrated_conviction"] == "0.500000000000000000"
    assert payload["risk_fraction_used"] == "0.006250000000000000"


def test_a_map_fitted_after_the_decision_is_refused() -> None:
    """The look-ahead this issue exists for, caught at the point of consumption.

    The fit itself already filters on `as_of_utc`, so this is the second of two
    independent guards: the first stops the map from seeing the future, the second stops a
    map that saw a legitimate later instant from being applied to an earlier decision --
    which is how the leak arrives when the caller caches maps.
    """
    later = _unfitted(as_of_utc=_DECIDED_AT + timedelta(seconds=1))

    with pytest.raises(CalibrationError, match="was fitted at"):
        _assess(_signal(Decimal("0.8")), calibration=later)


def test_another_strategys_map_is_refused() -> None:
    """A map is a claim about one strategy's record and means nothing applied to another."""
    with pytest.raises(CalibrationError, match="belongs to"):
        _assess(_signal(Decimal("0.8")), calibration=_unfitted(strategy_id="mean-reversion-v1"))


def test_a_flat_signal_is_not_a_conviction_to_calibrate() -> None:
    """Flat says "close what is open", which is not a request for a risk budget.

    Refused rather than sized at zero, on the same reasoning `SizingInputs` refuses it: a
    zero here would route a reduce-only instruction through the conviction floor, and the
    floor would discard it -- trapping the portfolio in the position the flat signal was
    trying to leave.
    """
    with pytest.raises(CalibrationError, match="flat signal"):
        _assess(_signal(Decimal("0"), direction=Direction.FLAT))


def test_a_full_precision_conviction_is_accepted() -> None:
    """Eighteen decimal places is the resolution the column holds, and it round-trips."""
    conviction = Decimal("0.123456789012345678")
    assessment = _assess(_signal(conviction))

    assert assessment.reported_conviction == conviction
    assert assessment.audit_payload()["reported_conviction"] == "0.123456789012345678"


def test_a_trade_carrying_more_precision_than_the_column_holds_is_refused() -> None:
    """Refused rather than quantized: quantizing moves a trade between decile buckets.

    Silently. The map would then differ from the map a restart rebuilds from the same
    rows, and the restart is the only place that difference is ever observable.
    """
    with pytest.raises(CalibrationError, match="18 decimal places"):
        ClosedTrade(
            strategy_id=_STRATEGY_ID,
            reported_conviction=Decimal("0.1234567890123456789"),
            realised_return_fraction=Decimal("0.01"),
            closed_at_utc=_DECIDED_AT,
        )


def test_a_history_holding_another_strategys_trade_is_refused() -> None:
    """A mixed history fits a map that describes nobody, and it fits it without complaint."""
    with pytest.raises(CalibrationError, match="mean-reversion-v1"):
        fit_calibration(
            [
                ClosedTrade(
                    strategy_id="mean-reversion-v1",
                    reported_conviction=Decimal("0.5"),
                    realised_return_fraction=Decimal("0.01"),
                    closed_at_utc=_DECIDED_AT,
                )
            ],
            strategy_id=_STRATEGY_ID,
            as_of_utc=_DECIDED_AT,
            parameters=_PARAMETERS,
        )


def test_a_naive_as_of_is_refused() -> None:
    """A naive instant compares against tz-aware trade timestamps by raising, or worse."""
    with pytest.raises(CalibrationError, match="timezone-aware UTC"):
        fit_calibration(
            [],
            strategy_id=_STRATEGY_ID,
            as_of_utc=datetime(2026, 6, 1, 12),  # noqa: DTZ001
            parameters=_PARAMETERS,
        )


def test_configuration_may_tighten_the_floor_but_not_undercut_the_compiled_bound() -> None:
    """`conviction_floor` is bounded below, not above: a higher floor discards more."""
    assert ConvictionParameters(conviction_floor=Decimal("0.4")).conviction_floor == Decimal("0.4")

    with pytest.raises(ValueError, match="compiled-in hard floor"):
        ConvictionParameters(conviction_floor=Decimal("0.05"))


def test_an_inverted_risk_band_is_refused() -> None:
    """`r_min > r_max` makes `r_used` *decrease* in calibrated conviction.

    Refused even though `RiskLimits` deliberately admits no cross-field rules: every
    configuration that model accepts is more conservative than the default, and an
    inverted band is not conservative, it is backwards.
    """
    with pytest.raises(ValueError, match="above risk_fraction_max_per_trade"):
        ConvictionParameters(
            risk_fraction_min_per_trade=Decimal("0.009"),
            risk_fraction_max_per_trade=Decimal("0.004"),
        )


def test_the_trade_floor_may_be_raised_but_not_lowered() -> None:
    """Below 100 closed trades the standard error on a decile mean swamps the gradient."""
    raised = ConvictionParameters(min_trades_for_calibration=_RAISED_TRADE_FLOOR)
    assert raised.min_trades_for_calibration == _RAISED_TRADE_FLOOR

    with pytest.raises(ValueError, match="compiled-in hard floor"):
        ConvictionParameters(min_trades_for_calibration=50)
