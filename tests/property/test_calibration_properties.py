"""Properties of the conviction calibration map that no example-based test establishes.

Five, in the order they would hurt if broken:

1. **The map is monotone non-decreasing over the whole of `[0, 1]`, always.** A map with
   an inversion sizes a signal the strategy was *less* confident about larger, which
   inverts the one channel `RISK_PHILOSOPHY.md` section 2 leaves open to a strategy. The
   inversion would be invisible: every individual decision still looks defensible.
2. **The fit is point-in-time.** A map fitted `as_of` `t` is byte-identical whether or
   not trades closing after `t` exist at all. This is the look-ahead the issue names --
   inside the risk engine rather than the feature store, which is why none of the P1
   defences reach it.
3. **Below the trade floor the map is exactly `0.5` everywhere**, so a new strategy's
   conviction channel carries nothing at all rather than carrying noise.
4. **`r_used` stays inside `[r_min, r_max]` and is monotone in reported conviction**, so
   the value handed to `SizingParameters` can never be refused by its own ceiling.
5. **The persisted form round-trips exactly.** A map that changes across a restart is a
   different map, and the decisions before and after the restart stop being comparable.

`docs/rules/testing-rules.md` clause 2: property tests are mandatory for every function
in `fking.risk`.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Final

import pytest
from hypothesis import given
from hypothesis import strategies as st

from fking.domain import Direction, Instrument, Signal, Venue
from fking.risk.calibration import (
    UNCALIBRATED_FRACTION,
    CalibrationMap,
    ClosedTrade,
    ConvictionParameters,
    assess_conviction,
    fit_calibration,
    from_calibration_row,
    risk_fraction_for,
    to_calibration_row,
)
from fking.risk.sizing import SizingParameters

pytestmark = [pytest.mark.property, pytest.mark.unit]

_STRATEGY_ID: Final = "trailing-return-v3"
_EPOCH: Final = datetime(2026, 1, 1, tzinfo=UTC)
_PARAMETERS: Final = ConvictionParameters()

BTCUSDT: Final = Instrument(
    venue=Venue.BINANCE_SPOT_TESTNET,
    symbol="BTCUSDT",
    base_asset="BTC",
    quote_asset="USDT",
    tick_size=Decimal("0.01"),
    lot_step=Decimal("0.00001"),
    min_notional_quote=Decimal("10"),
)

# Conviction is stored at NUMERIC(38, 18) and the map is quantized to match, so the
# generator produces a resolution a real record can actually carry. Six places rather
# than eighteen keeps the search space navigable; the eighteen-place boundary is pinned
# by an example in `tests/risk/test_calibration_floor.py`, where a shrunk counterexample
# would not have been readable anyway.
convictions = st.decimals(
    min_value=Decimal("0"), max_value=Decimal("1"), places=6, allow_nan=False, allow_infinity=False
)

# A per-trade return of -100% is a total loss and +500% is a very good day on a crypto
# major; nothing outside that range is a return this system could have produced.
return_fractions = st.decimals(
    min_value=Decimal("-1"), max_value=Decimal("5"), places=6, allow_nan=False, allow_infinity=False
)

closing_offsets = st.integers(min_value=0, max_value=100_000)


@st.composite
def closed_trades(draw: st.DrawFn) -> ClosedTrade:
    return ClosedTrade(
        strategy_id=_STRATEGY_ID,
        reported_conviction=draw(convictions),
        realised_return_fraction=draw(return_fractions),
        closed_at_utc=_EPOCH + timedelta(minutes=draw(closing_offsets)),
    )


def histories(*, min_size: int = 0, max_size: int = 140) -> st.SearchStrategy[list[ClosedTrade]]:
    return st.lists(closed_trades(), min_size=min_size, max_size=max_size)


# The lookup grid every monotonicity assertion is swept over: both endpoints, every
# decile boundary, and the midpoints between them. A grid of only round numbers would
# miss a map whose bucket edges all sit at 0.05 increments.
_GRID: Final[tuple[Decimal, ...]] = tuple(Decimal(step) / Decimal("40") for step in range(41))


def _assert_monotone_over_the_unit_interval(calibration: CalibrationMap) -> None:
    readings = [calibration.calibrated(conviction) for conviction in _GRID]
    assert readings == sorted(readings)
    assert all(Decimal("0") <= reading <= Decimal("1") for reading in readings)


@given(history=histories())
def test_every_fitted_map_is_monotone_non_decreasing_over_the_unit_interval(
    history: list[ClosedTrade],
) -> None:
    """The core guarantee, over arbitrary histories including the degenerate ones.

    Hypothesis reaches all-wins, all-losses, a single observation and a one-bucket
    record from this generator; the named tests below pin those cases so a shrunk
    counterexample is not the only record that they were covered.
    """
    calibration = fit_calibration(
        history,
        strategy_id=_STRATEGY_ID,
        as_of_utc=_EPOCH + timedelta(days=400),
        parameters=_PARAMETERS,
    )
    _assert_monotone_over_the_unit_interval(calibration)


@given(
    conviction=convictions,
    realised_return_fraction=return_fractions,
    trade_count=st.integers(min_value=1, max_value=110),
)
def test_a_zero_variance_conviction_record_produces_a_flat_map(
    conviction: Decimal, realised_return_fraction: Decimal, trade_count: int
) -> None:
    """One reported conviction, repeated. There is no gradient to fit and none is invented.

    This is the anti-gaming property stated in `RISK_PHILOSOPHY.md` section 2: a strategy
    that reports the same conviction on everything cannot buy size with it.
    """
    history = [
        ClosedTrade(
            strategy_id=_STRATEGY_ID,
            reported_conviction=conviction,
            realised_return_fraction=realised_return_fraction,
            closed_at_utc=_EPOCH + timedelta(minutes=index),
        )
        for index in range(trade_count)
    ]
    calibration = fit_calibration(
        history,
        strategy_id=_STRATEGY_ID,
        as_of_utc=_EPOCH + timedelta(days=400),
        parameters=_PARAMETERS,
    )
    readings = {calibration.calibrated(point) for point in _GRID}
    assert readings == {UNCALIBRATED_FRACTION}


@given(history=histories(max_size=99))
def test_below_the_trade_floor_the_map_is_the_constant_half(history: list[ClosedTrade]) -> None:
    """Fewer than 100 closed trades: exactly `Decimal("0.5")` across the full range."""
    calibration = fit_calibration(
        history,
        strategy_id=_STRATEGY_ID,
        as_of_utc=_EPOCH + timedelta(days=400),
        parameters=_PARAMETERS,
    )
    assert not calibration.is_fitted
    assert all(calibration.calibrated(point) == Decimal("0.5") for point in _GRID)


@given(
    history=histories(min_size=1),
    cut_minutes=st.integers(min_value=0, max_value=100_000),
    poison=histories(min_size=1, max_size=60),
)
def test_no_trade_closing_after_the_as_of_can_reach_the_map(
    history: list[ClosedTrade], cut_minutes: int, poison: list[ClosedTrade]
) -> None:
    """Poison the future; the map fitted at the cut must not have moved.

    The poison is pushed strictly beyond the cut and carries the most extreme returns the
    generator can express, because a subtle perturbation can be absorbed by the decile
    bucketing and produce a false pass.
    """
    cut_utc = _EPOCH + timedelta(minutes=cut_minutes)
    poisoned = [
        ClosedTrade(
            strategy_id=trade.strategy_id,
            reported_conviction=trade.reported_conviction,
            realised_return_fraction=Decimal("5"),
            closed_at_utc=cut_utc + timedelta(minutes=1 + index),
        )
        for index, trade in enumerate(poison)
    ]
    baseline = fit_calibration(
        history, strategy_id=_STRATEGY_ID, as_of_utc=cut_utc, parameters=_PARAMETERS
    )
    contaminated = fit_calibration(
        [*history, *poisoned],
        strategy_id=_STRATEGY_ID,
        as_of_utc=cut_utc,
        parameters=_PARAMETERS,
    )
    assert baseline == contaminated


@given(history=histories(min_size=100), conviction=convictions)
def test_the_risk_fraction_stays_inside_its_configured_band(
    history: list[ClosedTrade], conviction: Decimal
) -> None:
    """`r_used` is always a fraction `SizingParameters` will accept.

    Asserted by construction rather than by comparison against the ceiling, because the
    ceiling is what the constructor checks and a second copy of it here could drift.
    """
    calibration = fit_calibration(
        history,
        strategy_id=_STRATEGY_ID,
        as_of_utc=_EPOCH + timedelta(days=400),
        parameters=_PARAMETERS,
    )
    risk_fraction_used = risk_fraction_for(
        calibration.calibrated(conviction), parameters=_PARAMETERS
    )
    assert _PARAMETERS.risk_fraction_min_per_trade <= risk_fraction_used
    assert risk_fraction_used <= _PARAMETERS.risk_fraction_max_per_trade
    assert SizingParameters(risk_fraction_per_trade=risk_fraction_used).risk_fraction_per_trade == (
        risk_fraction_used
    )


@given(history=histories(), lower=convictions, upper=convictions)
def test_a_higher_reported_conviction_never_buys_a_smaller_risk_fraction(
    history: list[ClosedTrade], lower: Decimal, upper: Decimal
) -> None:
    """Monotonicity carried all the way through to the number that sizes the position."""
    calibration = fit_calibration(
        history,
        strategy_id=_STRATEGY_ID,
        as_of_utc=_EPOCH + timedelta(days=400),
        parameters=_PARAMETERS,
    )
    low, high = min(lower, upper), max(lower, upper)
    assert risk_fraction_for(
        calibration.calibrated(low), parameters=_PARAMETERS
    ) <= risk_fraction_for(calibration.calibrated(high), parameters=_PARAMETERS)


@given(history=histories())
def test_the_persisted_form_round_trips_exactly(history: list[ClosedTrade]) -> None:
    """`from_calibration_row(to_calibration_row(m)) == m`, at the resolution the column holds."""
    calibration = fit_calibration(
        history,
        strategy_id=_STRATEGY_ID,
        as_of_utc=_EPOCH + timedelta(days=400),
        parameters=_PARAMETERS,
    )
    assert from_calibration_row(to_calibration_row(calibration)) == calibration


@given(history=histories(), conviction=convictions)
def test_a_signal_below_the_floor_is_refused_and_carries_no_risk_fraction(
    history: list[ClosedTrade], conviction: Decimal
) -> None:
    """Below the floor there is no number to size with, and the type says so.

    `risk_fraction_used is None` rather than zero: a zero would type-check its way into
    `SizingParameters` and be refused there instead, one layer away from the decision
    that actually rejected the signal.
    """
    decided_at_utc = _EPOCH + timedelta(days=400)
    calibration = fit_calibration(
        history, strategy_id=_STRATEGY_ID, as_of_utc=decided_at_utc, parameters=_PARAMETERS
    )
    assessment = assess_conviction(
        signal=Signal(
            strategy_id=_STRATEGY_ID,
            instrument=BTCUSDT,
            direction=Direction.LONG,
            conviction=conviction,
            horizon=timedelta(hours=4),
            invalidation_quote_price=Decimal("58000"),
            rationale="trailing return above entry threshold",
            decided_at_utc=decided_at_utc,
        ),
        calibration=calibration,
        parameters=_PARAMETERS,
        decided_at_utc=decided_at_utc,
    )
    below_floor = conviction < _PARAMETERS.conviction_floor
    assert assessment.is_approved is not below_floor
    assert (assessment.risk_fraction_used is None) is below_floor
    if below_floor:
        assert assessment.rejection is not None
        assert assessment.rejection.binding_limit_name == "conviction_floor"
