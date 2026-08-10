"""The trend baseline: a 20-period Donchian channel breakout, held for the channel period.

This is a **control**, not a candidate. Its parameters are the conventional ones, they are
fixed a priori, and they are never tuned -- including in the direction that would make the
tearsheet look better. `SURVIVAL_PROTOCOL.md` section 10 is the reason: an evolved
strategy's Sharpe has a standard error wide enough to contain almost anything, and it only
becomes interpretable against a strategy whose behaviour is fully understood, measured on
the same data through the same cost model. Tuning the control destroys that reading twice
over -- it stops being a fixed reference, and the trials get charged to the global counter
on behalf of something nobody intends to promote, so every future deflated Sharpe pays for
a search that was never meant to produce a strategy.

The thesis is the oldest one in systematic trading and is deliberately unremarkable: a
close that is the highest in the last twenty periods tends to be followed by more of the
same. Donchian's rule, and the one every trend-following program is a variation on.

**The invalidation is volatility-scaled, and that is the only non-obvious choice here.**
A stop at a fixed fraction of price is the same distance in a dead market and in a crash,
which makes it too tight to survive normal noise in one regime and too wide to size against
in the other. The distance declared here is one standard deviation of the move the symbol
normally makes over this signal's own horizon, so the question the stop asks -- "has price
moved further against me than this symbol usually moves in five hours?" -- has the same
meaning in both regimes. It is spelled as a declared multiple of a declared feature rather
than computed in `evaluate`, so `fking.strategy.step` can recompute it from the same value
the strategy was handed (`_invalidation`).

It is not Wilder's ATR, which the issue's phrasing suggests, and the difference is worth
stating rather than glossing: an ATR needs each bar's high and low, and
`fking.data.features.spec.FeatureObservation` carries only the open and the close, so no
registered feature can compute one. Widening that type is a data-pipeline change with its
own point-in-time consequences, and doing it silently inside a strategy is exactly the move
`tools/checks/feature_registry.py` exists to prevent. A close-to-close realised volatility
over the same window answers the same question with the data the corpus actually holds.
"""

from __future__ import annotations

from collections.abc import Mapping
from datetime import timedelta
from decimal import Decimal
from typing import Final

from fking.domain import Bar, Direction, Instrument, Signal
from fking.strategy._contract import Clock, StrategyState
from fking.strategy._invalidation import InvalidationRule
from fking.strategy._parameters import (
    DecimalParameter,
    ParameterSpace,
    ParameterValue,
    decimal_parameter,
)
from fking.strategy._requirements import FeatureRequirement
from fking.strategy._spec import StrategySpec

__all__ = ["DonchianChannelBreakout"]

# The three values `donchian_channel_breakout_state` is defined to take. These are the
# feature's own encoding, documented in its docstring, and not thresholds anybody chose --
# which is why they are constants here rather than entries in the parameter space. A search
# over "which state counts as a breakout" would be a search over the feature's definition.
_BREAKOUT_ABOVE: Final[Decimal] = Decimal("1")
_BREAKOUT_BELOW: Final[Decimal] = Decimal("-1")

# Every emitted signal reports the same conviction. The channel either broke or it did not;
# there is no measured gradation between those, and manufacturing one -- scaling conviction
# by how far past the channel the close sits, say -- would be a hand-chosen monotone map
# charged to nobody, on a strategy whose entire purpose is to have no such numbers.
# `fking.risk` calibrates reported conviction against realised outcome in any case.
_FULL_CONVICTION: Final[Decimal] = Decimal("1")

_STRATEGY_ID: Final[str] = "donchian-channel-breakout"
_STRATEGY_VERSION: Final[int] = 1

_THESIS: Final[str] = (
    "A close that is the highest in the trailing twenty periods continues upward over the "
    "following twenty, and the mirror image downward. The thesis is wrong, and the "
    "position retired, once price has moved against the breakout close by more than one "
    "standard deviation of the symbol's own move over that horizon."
)

_CHANNEL_STATE: Final = FeatureRequirement(
    feature_name="donchian_channel_breakout_state", feature_version=1
)
_REALISED_VOLATILITY: Final = FeatureRequirement(
    feature_name="trailing_realised_volatility", feature_version=1
)

_BAR_INTERVAL: Final[timedelta] = timedelta(minutes=15)
# Twenty-four bars is six hours, against a deepest declared lookback of five. The registry
# refuses a warm-up that does not cover it; the margin is so that a later interval change
# does not land exactly on the boundary.
_WARM_UP_BARS: Final[int] = 24
# The channel period itself. The thesis is that a breakout persists over a window at least
# as long as the one that defined it, and no shorter horizon has an argument behind it that
# is not a preference.
_SIGNAL_HORIZON: Final[timedelta] = timedelta(hours=5)

_INVALIDATION_VOLATILITY_MULTIPLE: Final[str] = "invalidation_volatility_multiple"
_INVALIDATION_FLOOR_FRACTION: Final[str] = "invalidation_floor_fraction"
_INVALIDATION_CAP_FRACTION: Final[str] = "invalidation_cap_fraction"

PARAMETERS: Final[ParameterSpace] = ParameterSpace(
    (
        # `trailing_realised_volatility` reports the standard deviation of one
        # observation's return. The corpus holds one-minute bars, so a five-hour horizon
        # spans 300 of them and the horizon's standard deviation is sqrt(300) = 17.3205
        # times the per-observation one, under the independent-increment assumption that
        # every square-root-of-time scaling makes. One horizon sigma, not two: the stop is
        # a falsification level, and a move larger than the symbol's typical move over the
        # whole holding period is no longer the noise the thesis tolerates.
        DecimalParameter(
            name=_INVALIDATION_VOLATILITY_MULTIPLE,
            default=Decimal("17.3205"),
            minimum=Decimal("1"),
            maximum=Decimal("50"),
        ),
        # The floor and the cap bound the denominator of `q = (r_used * E) / |P_entry -
        # P_invalidation|`. Without the floor a stalled market -- where the volatility
        # estimate collapses towards zero -- produces an unbounded position through
        # arithmetic nobody sees; 20 basis points is roughly two ticks of spread on the
        # liquid USDT pairs, below which a stop is inside the noise of a single fill.
        DecimalParameter(
            name=_INVALIDATION_FLOOR_FRACTION,
            default=Decimal("0.002"),
            minimum=Decimal("0.0005"),
            maximum=Decimal("0.02"),
        ),
        DecimalParameter(
            name=_INVALIDATION_CAP_FRACTION,
            default=Decimal("0.10"),
            minimum=Decimal("0.02"),
            maximum=Decimal("0.30"),
        ),
    )
)


class DonchianChannelBreakout:
    """Long a new twenty-period high close, short a new low, silent inside the channel."""

    __slots__ = ("_spec",)

    def __init__(
        self,
        instruments: tuple[Instrument, ...],
        parameters: Mapping[str, ParameterValue] | None = None,
    ) -> None:
        bound = PARAMETERS.bind(parameters)
        self._spec = StrategySpec(
            strategy_id=_STRATEGY_ID,
            strategy_version=_STRATEGY_VERSION,
            thesis=_THESIS,
            instruments=instruments,
            bar_intervals=(_BAR_INTERVAL,),
            required_features=(_CHANNEL_STATE, _REALISED_VOLATILITY),
            warm_up_bars=_WARM_UP_BARS,
            parameters=PARAMETERS,
            invalidation=InvalidationRule(
                adverse_move_fraction=decimal_parameter(bound, _INVALIDATION_FLOOR_FRACTION),
                volatility_feature=_REALISED_VOLATILITY,
                volatility_multiple=decimal_parameter(bound, _INVALIDATION_VOLATILITY_MULTIPLE),
                maximum_adverse_move_fraction=decimal_parameter(bound, _INVALIDATION_CAP_FRACTION),
            ),
            signal_horizon=_SIGNAL_HORIZON,
        )

    @property
    def spec(self) -> StrategySpec:
        return self._spec

    def evaluate(self, state: StrategyState, bar: Bar, clock: Clock) -> Signal | None:
        """The belief, or `None` for no opinion. Pure: the clock is the only time source."""
        as_of = clock()
        visible = state.visible_bars(as_of)
        if not visible:
            return None

        channel_state = state.feature_at(_CHANNEL_STATE)
        if channel_state == _BREAKOUT_ABOVE:
            direction = Direction.LONG
        elif channel_state == _BREAKOUT_BELOW:
            direction = Direction.SHORT
        else:
            return None

        # The newest bar that had already closed at `as_of`. Never `state.recent_bars[-1]`:
        # the two agree in live and differ in backtest, and the difference is the leak.
        decision_bar = visible[-1]
        return Signal(
            strategy_id=self._spec.strategy_id,
            instrument=bar.instrument,
            direction=direction,
            conviction=_FULL_CONVICTION,
            horizon=self._spec.signal_horizon,
            invalidation_quote_price=self._spec.invalidation.level_for(
                direction=direction,
                reference_quote_price=decision_bar.close_quote_price,
                instrument=bar.instrument,
                feature_values=state.feature_values,
            ),
            rationale=(
                f"the close of {decision_bar.close_quote_price} is the "
                f"{'highest' if direction is Direction.LONG else 'lowest'} of the trailing "
                f"twenty periods, and the invalidation sits one horizon standard deviation "
                f"away at a realised volatility of {state.feature_at(_REALISED_VOLATILITY)}"
            ),
            decided_at_utc=as_of,
        )
