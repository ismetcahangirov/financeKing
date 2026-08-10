"""The baselines point the right way in the regime each was written for.

A **direction** check, deliberately, and never a profitability one. Whether a twenty-period
breakout makes money on this fixture is a question about the fixture; whether it goes long
when price makes new highs is a question about the strategy, and it is the one a control
group has to answer correctly to be usable as a denominator later
(`SURVIVAL_PROTOCOL.md` section 10).

The two windows are constructed rather than sampled. A trending window is a monotone ramp,
where every close is the extreme of its own channel. A ranging window is a plateau with a
two-sided excursion that stops making new extremes -- the only shape a "fade what has
stopped extending" rule can act on, and the shape `harness.exercising_closes` documents.
Searching real history for a window on which each baseline behaved would fit the fixture to
the strategy and prove nothing about either.
"""

from __future__ import annotations

from collections.abc import Sequence
from decimal import Decimal
from typing import Final

import pytest

from fking.domain import Direction, Signal
from fking.strategy import Strategy, initial_state, step
from fking.strategy.bollinger_reversion import BollingerBandReversion
from fking.strategy.donchian_breakout import DonchianChannelBreakout
from fking.strategy.trailing_return import TrailingReturnContinuation
from tests.strategy.harness import BTCUSDT, bars_from_closes, clock_at, feature_values_for

pytestmark = pytest.mark.unit

_SEED = 20260801
_PLATEAU_BARS: Final[int] = 40
_BASE: Final[Decimal] = Decimal("64000")


def _signals_over(strategy: Strategy, closes: Sequence[Decimal]) -> tuple[Signal, ...]:
    """Every signal `strategy` emits over `closes`, driven through the runner.

    Through `step` rather than by calling `evaluate`: the warm-up suppression, the feature
    supply and the invalidation check are all the runner's, and a regime assertion made
    against a strategy the runner would have refused is an assertion about nothing.
    """
    series = bars_from_closes(closes)
    values = feature_values_for(strategy.spec, series)
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


def _ramp(bar_count: int, *, step_fraction: str) -> tuple[Decimal, ...]:
    growth = Decimal("1") + Decimal(step_fraction)
    closes = [_BASE]
    while len(closes) < bar_count:
        closes.append(closes[-1] * growth)
    return tuple(closes)


def _plateau_with_excursion(*, excursion: tuple[str, str, str]) -> tuple[Decimal, ...]:
    """A quiet alternating plateau, then a deep move, a bounce, and a shallower repeat.

    The third close is the one a mean-reversion rule can act on: it is beyond two standard
    deviations of the window and is *not* the window's extreme, because the first move went
    further.
    """
    quiet = (Decimal("0.990"), Decimal("0.991"))
    closes = [_BASE * quiet[index % len(quiet)] for index in range(_PLATEAU_BARS)]
    closes.extend(_BASE * Decimal(multiplier) for multiplier in excursion)
    return tuple(closes)


def test_the_breakout_baseline_is_long_through_a_rising_window() -> None:
    """Positive-signed, and never both signs at once: a trend strategy that emits a short
    inside a monotone advance has read something other than the channel."""
    signals = _signals_over(DonchianChannelBreakout((BTCUSDT,)), _ramp(64, step_fraction="0.006"))

    assert signals
    assert {signal.direction for signal in signals} == {Direction.LONG}


def test_the_breakout_baseline_is_short_through_a_falling_window() -> None:
    """The mirror image, which is what makes the clause above a directional claim rather
    than a statement that this strategy is long by construction."""
    signals = _signals_over(DonchianChannelBreakout((BTCUSDT,)), _ramp(64, step_fraction="-0.006"))

    assert signals
    assert {signal.direction for signal in signals} == {Direction.SHORT}


def test_the_reversion_baseline_fades_a_low_excursion_upward() -> None:
    """Long into a stretched-low close that has stopped making new lows."""
    signals = _signals_over(
        BollingerBandReversion((BTCUSDT,)),
        _plateau_with_excursion(excursion=("0.930", "0.985", "0.950")),
    )

    assert signals
    assert {signal.direction for signal in signals} == {Direction.LONG}


def test_the_reversion_baseline_fades_a_high_excursion_downward() -> None:
    """And short into the mirror of it."""
    signals = _signals_over(
        BollingerBandReversion((BTCUSDT,)),
        _plateau_with_excursion(excursion=("1.070", "1.015", "1.050")),
    )

    assert signals
    assert {signal.direction for signal in signals} == {Direction.SHORT}


def test_the_reversion_baseline_stands_aside_inside_the_excursion() -> None:
    """The regime filter, asserted as an absence.

    The deep first move is both a two-sigma excursion and the low of its own window, which
    is the one bar on which a mean-reversion rule and a trend-following rule would both
    fire, in opposite directions, on the same evidence. This baseline refuses it, and that
    refusal is why its entry count is as low as it is -- so it is asserted here rather than
    left as a property somebody later "fixes" by widening the filter.
    """
    plunge = _plateau_with_excursion(excursion=("0.930", "0.985", "0.950"))
    signals = _signals_over(BollingerBandReversion((BTCUSDT,)), plunge)

    decision_instants = {signal.decided_at_utc for signal in signals}
    series = bars_from_closes(plunge)
    first_dip = series[_PLATEAU_BARS]

    assert first_dip.close_time_utc not in decision_instants


def test_the_three_baselines_are_not_the_same_strategy_under_three_names() -> None:
    """A control group whose members agree on every bar is one control, not three.

    The plateau-with-excursion window is where they are meant to disagree: the breakout
    baseline sees a new low, the reversion baseline sees an excursion to fade, and the
    trailing-return baseline sees a large negative return to follow.
    """
    closes = _plateau_with_excursion(excursion=("0.930", "0.985", "0.950"))
    instruments = (BTCUSDT,)
    reversion = _signals_over(BollingerBandReversion(instruments), closes)
    breakout = _signals_over(DonchianChannelBreakout(instruments), closes)

    assert reversion
    assert breakout
    reversion_decisions = {(signal.decided_at_utc, signal.direction) for signal in reversion}
    breakout_decisions = {(signal.decided_at_utc, signal.direction) for signal in breakout}
    assert reversion_decisions != breakout_decisions
    assert TrailingReturnContinuation(instruments).spec.strategy_id not in {
        signal.strategy_id for signal in reversion + breakout
    }
