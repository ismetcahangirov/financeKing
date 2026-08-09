"""The look-ahead probe, pointed at a risk decision rather than at a feature value.

`docs/rules/no-lookahead.md` builds its whole enforcement stack around the feature store,
and issue #49 names the gap that leaves: the risk engine is a data consumer too. A
calibration map fitted on a strategy's *full* trade record and then used to size a signal
from the middle of that record is look-ahead of exactly the same shape -- it inflates the
backtest the way a leaky feature does -- and it evades every P1 defence, because none of
them is looking at `fking.risk`.

The probe is the same experiment the feature probe runs. Replay a sequence of decisions,
poison everything that closed after the cut, replay again, and require every decision at
or before the cut to be byte-identical.

Two details are carried over deliberately, because both are what makes the feature probe
mean anything:

**The poison is gross, not subtle.** Returns are replaced with the extremes of the
representable range and convictions are inverted. A small perturbation can be absorbed by
decile bucketing and produce a false pass, which is the worst available outcome for a
probe.

**The probe is shown to fail.** `test_the_probe_detects_a_leak_it_was_built_to_catch`
poisons a trade *before* the cut and requires the digest to move. Without it, a
calibration that ignored its input entirely would pass every assertion here.
"""

from __future__ import annotations

import hashlib
from collections.abc import Sequence
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Final

import pytest

from fking.domain import Direction, Instrument, Signal, Venue
from fking.risk.calibration import (
    ClosedTrade,
    ConvictionAssessment,
    ConvictionParameters,
    assess_conviction,
    fit_calibration,
)

pytestmark = pytest.mark.unit

_STRATEGY_ID: Final = "trailing-return-v3"
_EPOCH: Final = datetime(2026, 1, 1, tzinfo=UTC)
_PARAMETERS: Final = ConvictionParameters()

# Three hundred trades at one-hour spacing, so the cut halfway through leaves 150 on each
# side -- comfortably past the hundred-trade floor, which matters: below it every map is
# the constant 0.5 and the probe would pass on a calibration that read the future freely.
_TRADE_COUNT: Final = 300
_CUT: Final = _EPOCH + timedelta(hours=_TRADE_COUNT // 2)

# One low, one middling and one high conviction per decision instant, so a leak that moves
# only one end of the map still moves the digest.
_PROBED_CONVICTIONS: Final = (Decimal("0.2"), Decimal("0.55"), Decimal("0.95"))

BTCUSDT: Final = Instrument(
    venue=Venue.BINANCE_SPOT_TESTNET,
    symbol="BTCUSDT",
    base_asset="BTC",
    quote_asset="USDT",
    tick_size=Decimal("0.01"),
    lot_step=Decimal("0.00001"),
    min_notional_quote=Decimal("10"),
)


def _record() -> tuple[ClosedTrade, ...]:
    """A strategy whose conviction carries real information, so the map has a gradient.

    An uninformative record fits flat, and a flat map is identical to the unfitted one --
    which would make every digest below equal for a reason that has nothing to do with
    point-in-time correctness.
    """
    return tuple(
        ClosedTrade(
            strategy_id=_STRATEGY_ID,
            reported_conviction=Decimal(index % 10) / Decimal("10"),
            realised_return_fraction=Decimal(index % 10) / Decimal("200") - Decimal("0.02"),
            closed_at_utc=_EPOCH + timedelta(hours=index),
        )
        for index in range(_TRADE_COUNT)
    )


def _poison_after(record: Sequence[ClosedTrade], cut_utc: datetime) -> tuple[ClosedTrade, ...]:
    """Replace every outcome after the cut with the opposite extreme, and invert conviction.

    A calibration that reads the future cannot survive this quietly: the poisoned half
    reverses the sign of the relationship the clean half establishes, so any leak moves
    the map's ordering rather than nudging its values.
    """
    return tuple(
        trade
        if trade.closed_at_utc <= cut_utc
        else ClosedTrade(
            strategy_id=trade.strategy_id,
            reported_conviction=Decimal("1") - trade.reported_conviction,
            realised_return_fraction=Decimal("5") - trade.realised_return_fraction * 3,
            closed_at_utc=trade.closed_at_utc,
        )
        for trade in record
    )


def _signal(conviction: Decimal, decided_at_utc: datetime) -> Signal:
    return Signal(
        strategy_id=_STRATEGY_ID,
        instrument=BTCUSDT,
        direction=Direction.LONG,
        conviction=conviction,
        horizon=timedelta(hours=4),
        invalidation_quote_price=Decimal("58000"),
        rationale="trailing return crossed the entry threshold",
        decided_at_utc=decided_at_utc,
    )


def _replay(record: Sequence[ClosedTrade], *, until_utc: datetime) -> list[ConvictionAssessment]:
    """One decision per day up to `until_utc`, each sized by a map fitted at that instant.

    This is the sequence a backtest produces once `RiskEngine.decide()` (#55) is wired to
    the calibrator: the engine holds no map of its own, it fits one as of the bar being
    decided. The loop is here rather than in the engine because the engine does not exist
    yet, and the property under test belongs to the calibrator either way.
    """
    assessments: list[ConvictionAssessment] = []
    decided_at_utc = _EPOCH + timedelta(days=5)
    while decided_at_utc <= until_utc:
        calibration = fit_calibration(
            record, strategy_id=_STRATEGY_ID, as_of_utc=decided_at_utc, parameters=_PARAMETERS
        )
        for conviction in _PROBED_CONVICTIONS:
            assessments.append(
                assess_conviction(
                    signal=_signal(conviction, decided_at_utc),
                    calibration=calibration,
                    parameters=_PARAMETERS,
                    decided_at_utc=decided_at_utc,
                )
            )
        decided_at_utc += timedelta(days=1)
    return assessments


def _decision_digest(assessments: Sequence[ConvictionAssessment]) -> str:
    """A digest over the exact decimal text of every decision, in sequence order.

    `format(candidate, "f")` rather than `str`: it never falls back to scientific notation
    and it preserves trailing zeros, so a rescaling from `0.5` to `0.50` is a different
    digest instead of the same one. A `1E-18` difference fails, which is the sensitivity
    `test_the_digest_sees_a_difference_in_the_last_place` asserts directly.
    """
    material = "\n".join(
        "|".join(
            (
                assessment.decided_at_utc.isoformat(),
                format(assessment.reported_conviction, "f"),
                format(assessment.calibrated_conviction, "f"),
                "none"
                if assessment.risk_fraction_used is None
                else format(assessment.risk_fraction_used, "f"),
                assessment.calibration_available_at_utc.isoformat(),
                str(assessment.calibration_observation_count),
            )
        )
        for assessment in assessments
    )
    return hashlib.blake2b(material.encode("utf-8"), digest_size=32).hexdigest()


def test_no_decision_at_or_before_the_cut_depends_on_a_trade_closed_after_it() -> None:
    baseline = _replay(_record(), until_utc=_CUT)
    poisoned = _replay(_poison_after(_record(), _CUT), until_utc=_CUT)

    # A replay that produced nothing compares two empty sequences, which are equal and
    # verify nothing. Every clause of the feature probe asserts this first, for the same
    # reason.
    assert len(baseline) > len(_PROBED_CONVICTIONS)
    assert any(
        assessment.calibration_observation_count >= _PARAMETERS.min_trades_for_calibration
        for assessment in baseline
    )
    assert _decision_digest(baseline) == _decision_digest(poisoned)


def test_the_probe_detects_a_leak_it_was_built_to_catch() -> None:
    """Poison one trade *before* the cut. If this passes, every result above is void."""
    record = _record()
    tampered = (
        ClosedTrade(
            strategy_id=record[0].strategy_id,
            reported_conviction=Decimal("1") - record[0].reported_conviction,
            realised_return_fraction=Decimal("5"),
            closed_at_utc=record[0].closed_at_utc,
        ),
        *record[1:],
    )

    assert _decision_digest(_replay(record, until_utc=_CUT)) != _decision_digest(
        _replay(tampered, until_utc=_CUT)
    )


def test_the_digest_sees_a_difference_in_the_last_place() -> None:
    """Byte-identical means literally that, down to the eighteenth decimal place."""
    baseline = _replay(_record(), until_utc=_CUT)
    nudged = [
        *baseline[:-1],
        ConvictionAssessment(
            strategy_id=baseline[-1].strategy_id,
            reported_conviction=baseline[-1].reported_conviction,
            calibrated_conviction=baseline[-1].calibrated_conviction,
            risk_fraction_used=baseline[-1].risk_fraction_used,
            conviction_floor=baseline[-1].conviction_floor,
            calibration_observation_count=baseline[-1].calibration_observation_count,
            calibration_available_at_utc=baseline[-1].calibration_available_at_utc,
            is_calibrated=baseline[-1].is_calibrated,
            decided_at_utc=baseline[-1].decided_at_utc + timedelta(microseconds=1),
            rejection=None,
        ),
    ]

    assert _decision_digest(baseline) != _decision_digest(nudged)


def test_a_map_fitted_earlier_is_a_prefix_of_the_record_and_nothing_more() -> None:
    """The claim in words: the map at `t` equals the map fitted from only the prefix.

    The digest tests above prove the future cannot reach the past. This one proves the
    stronger form -- that the fit is a pure function of the trades that had closed -- so a
    caller cannot achieve the same digest by ignoring its input.
    """
    record = _record()
    prefix = tuple(trade for trade in record if trade.closed_at_utc <= _CUT)

    assert fit_calibration(
        record, strategy_id=_STRATEGY_ID, as_of_utc=_CUT, parameters=_PARAMETERS
    ) == fit_calibration(prefix, strategy_id=_STRATEGY_ID, as_of_utc=_CUT, parameters=_PARAMETERS)
