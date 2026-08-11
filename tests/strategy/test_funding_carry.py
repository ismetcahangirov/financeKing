"""The carry baseline points at whichever side of the perpetual gets paid, and stops there.

Direction and distance, never profitability. Whether harvesting funding makes money on a
fixture is a question about the fixture; whether the strategy stands on the receiving side
of the settlement is a question about the strategy, and it is the one a control has to
answer correctly to be usable as a denominator (`SURVIVAL_PROTOCOL.md` section 10).

The sign convention is the whole of the first half of this file, because it is the defect
that would survive every other test here. A carry baseline with the sign inverted pays the
funding it was written to collect, emits a signal on exactly the same bars, and produces a
tearsheet that looks like a plausible perpetual strategy -- because over any window short
enough to look at, the price leg dominates the funding leg.

The second half is the invalidation, which is risk math and is therefore property-based.
The distance is the denominator of `q = (r_used * E) / |P_entry - P_invalidation|`, so an
example-based test confirms the funding rates somebody thought of while the cases that
produce an unbounded or inverted quantity are the ones nobody did.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from datetime import datetime
from decimal import Decimal
from typing import Final

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st

from fking.domain import Bar, Direction, Signal
from fking.strategy import FeatureRequirement, Strategy, initial_state, step
from fking.strategy.funding_carry import PerpetualFundingCarry
from tests.strategy.harness import BTCUSDT, bars_for, clock_at, feature_values_for, rising_closes

pytestmark = pytest.mark.unit

_SEED = 20260801
# Enough eight-hour bars to clear the 24-bar warm-up and still leave a long decision window
# behind it. At the venue's cadence this is a little over three weeks.
_BAR_COUNT: Final[int] = 64

# Binance's fixed interest-rate component: 0.01% per settlement, the rate a perpetual pays
# when its premium index is flat. It is the baseline's harvest threshold, restated here so
# that a change to the strategy's constant fails a test rather than passing quietly.
_BASE_RATE: Final[Decimal] = Decimal("0.0001")
# Twenty-one settlements in the declared seven-day horizon.
_SETTLEMENTS_IN_HORIZON: Final[Decimal] = Decimal("21")
_INVALIDATION_FLOOR_FRACTION: Final[Decimal] = Decimal("0.002")
_INVALIDATION_CAP_FRACTION: Final[Decimal] = Decimal("0.10")


def _signals_at_constant_funding(rate: Decimal) -> tuple[tuple[Signal, ...], tuple[Bar, ...]]:
    """Every signal the carry baseline emits when funding settles at `rate` throughout.

    A constant rate rather than a path, because the subject here is the rule and not the
    estimator: with one rate the trailing mean magnitude is exactly `abs(rate)` on every
    bar, so the expected invalidation distance is arithmetic a reader can check rather than
    a number the fixture happened to produce.
    """
    strategy: Strategy = PerpetualFundingCarry((BTCUSDT,))
    series = bars_for(strategy.spec, rising_closes(_BAR_COUNT))
    values = feature_values_for(strategy.spec, series, funding_rate_at=lambda _index: rate)
    return _replay_through_step(strategy, series, values), series


def _replay_through_step(
    strategy: Strategy,
    series: Sequence[Bar],
    values: Mapping[datetime, Mapping[FeatureRequirement, Decimal]],
) -> tuple[Signal, ...]:
    """Driven through `step`, never by calling `evaluate`.

    The warm-up suppression, the feature supply and the recomputed invalidation check are
    all the runner's, and an assertion made against a strategy the runner would have
    refused is an assertion about nothing.
    """
    state = initial_state(seed=_SEED)
    emitted: list[Signal] = []
    for observed in series:
        outcome = step(
            strategy,
            state,
            observed,
            clock_at(observed.close_time_utc),
            feature_values=values[observed.close_time_utc],
        )
        state = outcome.state
        if outcome.signal is not None:
            emitted.append(outcome.signal)
    return tuple(emitted)


# ---------------------------------------------------------------------------
# The sign convention
# ---------------------------------------------------------------------------


def test_positive_funding_puts_the_baseline_on_the_short_side() -> None:
    """Longs pay shorts when funding is positive, so the harvester is short.

    Inverted, this strategy pays the carry it exists to collect and nothing else in the
    suite notices: the same bars produce the same number of signals with the same
    invalidation distances, and the price leg dominates the funding leg over any window
    short enough to eyeball.
    """
    signals, _ = _signals_at_constant_funding(Decimal("0.0006"))

    assert signals
    assert {signal.direction for signal in signals} == {Direction.SHORT}


def test_negative_funding_puts_the_baseline_on_the_long_side() -> None:
    """The mirror image, which is what makes the clause above a directional claim rather
    than a statement that this baseline is short by construction. Inverted funding is a
    real and recurring regime, not an edge case."""
    signals, _ = _signals_at_constant_funding(Decimal("-0.0006"))

    assert signals
    assert {signal.direction for signal in signals} == {Direction.LONG}


def test_funding_at_the_venue_base_rate_is_not_a_regime_to_harvest() -> None:
    """The refusal branch, asserted as an absence.

    0.01% per settlement is what a perpetual pays when its premium index is flat -- the
    venue's own definition of no directional imbalance. There is no regime there to
    harvest, only the cost of carry the contract charges by construction, and a baseline
    that took a week-long directional position for it would be measuring its own fee
    schedule.
    """
    signals, _ = _signals_at_constant_funding(_BASE_RATE)

    assert signals == ()


def test_a_flat_funding_rate_produces_no_opinion_rather_than_a_flat_signal() -> None:
    """Zero funding is "no thesis", which is not the same as "hold no position".

    A flat `Signal` is an instruction the risk engine nets against; `None` is silence. A
    carry baseline that emitted flat on every settlement would be continuously instructing
    the engine to close positions other strategies opened.
    """
    signals, _ = _signals_at_constant_funding(Decimal("0"))

    assert signals == ()


# ---------------------------------------------------------------------------
# The invalidation
# ---------------------------------------------------------------------------


def test_every_signal_carries_an_invalidation_level() -> None:
    """The acceptance criterion the carry baseline was most at risk of failing.

    The `Signal` contract wants an invalidation *price* and the carry thesis is falsified
    by a funding condition, so the tempting spelling is `None` and the hope-sized branch.
    Without a falsification price the fixed-fractional denominator has nothing to divide by
    and nothing rests at the venue when the kill switch trips.
    """
    signals, _ = _signals_at_constant_funding(Decimal("0.0006"))

    assert signals
    assert all(signal.invalidation_quote_price is not None for signal in signals)


def test_the_invalidation_sits_where_the_accumulated_carry_stops_covering_the_move() -> None:
    """The distance is the carry, and that is checkable arithmetic rather than a claim.

    At 6bp a settlement over twenty-one settlements the position expects 126bp of carry, so
    an adverse move of 1.26% consumes the whole harvest. The level is that fraction from
    the decision bar's close, snapped away from the entry.
    """
    rate = Decimal("0.0006")
    signals, series = _signals_at_constant_funding(rate)
    closes_by_instant = {observed.close_time_utc: observed.close_quote_price for observed in series}
    distance = rate * _SETTLEMENTS_IN_HORIZON

    assert _INVALIDATION_FLOOR_FRACTION < distance < _INVALIDATION_CAP_FRACTION
    assert signals
    for signal in signals:
        entry = closes_by_instant[signal.decided_at_utc]
        level = signal.invalidation_quote_price
        assert level is not None
        # A short is wrong when price rises; the tick snap moves the level away from the
        # entry, so the emitted level is at or beyond the arithmetic one.
        assert level >= entry * (Decimal("1") + distance)
        assert level < entry * (Decimal("1") + distance) + BTCUSDT.tick_size


def test_an_extreme_funding_regime_is_capped_rather_than_widening_without_bound() -> None:
    """0.6% a settlement is inside what Binance has actually printed on a squeezed
    perpetual, and twenty-one of them is 12.6% -- past the point where the position is
    interpretable. The cap binds instead, which is the whole reason it is not optional
    when a feature scales the distance."""
    rate = Decimal("0.006")
    signals, series = _signals_at_constant_funding(rate)
    closes_by_instant = {observed.close_time_utc: observed.close_quote_price for observed in series}

    assert rate * _SETTLEMENTS_IN_HORIZON > _INVALIDATION_CAP_FRACTION
    assert signals
    for signal in signals:
        entry = closes_by_instant[signal.decided_at_utc]
        level = signal.invalidation_quote_price
        assert level is not None
        assert level >= entry * (Decimal("1") + _INVALIDATION_CAP_FRACTION)
        assert level < entry * (Decimal("1") + _INVALIDATION_CAP_FRACTION) + BTCUSDT.tick_size


def test_the_harvest_threshold_keeps_every_emitted_stop_above_the_floor() -> None:
    """A relationship between two constants that nothing else would notice breaking.

    The floor exists so a collapsing scaling feature cannot produce an unbounded position.
    Here it can never bind, because the smallest rate that clears the harvest threshold
    still accumulates 21bp of carry against a 20bp floor -- so every emitted stop is a real
    carry level rather than a clamp. If somebody lowers the threshold or raises the floor,
    the baseline silently starts sizing off a constant, and this is the assertion that
    says so.
    """
    assert _BASE_RATE * _SETTLEMENTS_IN_HORIZON > _INVALIDATION_FLOOR_FRACTION


@settings(deadline=None, max_examples=50)
@given(
    rate=st.decimals(
        min_value=Decimal("0.00011"),
        max_value=Decimal("0.05"),
        places=5,
        allow_nan=False,
        allow_infinity=False,
    ),
    paying_longs=st.booleans(),
)
def test_the_level_is_always_on_the_losing_side_of_the_entry(
    rate: Decimal, paying_longs: bool
) -> None:
    """Over any harvestable rate and either sign, the stop is where the position loses.

    The failure this quantifies over is a distance that inverts the level through the entry
    price -- a long whose stop sits above where it was opened. It does not raise, it
    produces a negative denominator, and the sized quantity comes out with the wrong sign.
    """
    signed_rate = -rate if paying_longs else rate
    signals, series = _signals_at_constant_funding(signed_rate)
    closes_by_instant = {observed.close_time_utc: observed.close_quote_price for observed in series}

    assert signals
    for signal in signals:
        entry = closes_by_instant[signal.decided_at_utc]
        level = signal.invalidation_quote_price
        assert level is not None
        assert level > Decimal("0")
        if signal.direction is Direction.LONG:
            assert level < entry
        else:
            assert level > entry
