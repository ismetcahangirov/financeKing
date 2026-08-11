"""The carry baseline: harvest perpetual funding by standing on the side that gets paid.

This is a **control**, not a candidate, for the reason `donchian_breakout` states at
length: its parameters are conventional, fixed a priori, and never tuned, because a swept
baseline is not a baseline and the trials would be charged to the global counter on behalf
of a strategy nobody intends to promote.

It is the only baseline that is perpetual-specific, which is the point of including it. It
is the only one that reads a series the venue *publishes* rather than one derived from
bars, so it is the only one that exercises a positive availability lag end to end -- the
one-minute envelope `fking.data.alt.registry` measured for the mark-price stream. The
other two read features stamped at the instant their input bar closed, where a lag bug is
invisible because the lag is zero.

**Why the decision grid is eight hours and not fifteen minutes.** Binance settles
perpetual funding every eight hours, so between settlements this strategy's entire input
is unchanged and a fifteen-minute grid would re-emit the same belief thirty-two times per
settlement. The risk engine nets those, so it would not double a position -- it would
simply fill the bus and the audit log with thirty-one restatements of one decision, and
make the emitted-signal count useless as a measure of how often the thesis fired.

**The invalidation is a carry level, and this is the part the issue is right to insist
on.** The `Signal` contract requires an invalidation *price*, while the carry thesis is
falsified by a funding-regime condition. Emitting `None` and taking the hope-sized branch
would leave the fixed-fractional denominator with nothing to divide by and leave nothing
resting at the venue when the kill switch trips. The level stated here is the price at
which the carry accumulated over the holding period stops covering the adverse move: the
position earns `mean|funding| x settlements_in_horizon` as a fraction of notional over the
horizon, so an adverse move of exactly that fraction consumes the whole harvest and the
trade was pointless before it was wrong. That is computable from the funding rate and the
holding period alone, which is why no volatility term appears in it.

Realised volatility is not in the stop; it is the reason the *floor* under the stop
exists, and the floor will bind whenever the regime is paying only the base rate. At
Binance's fixed 0.01% interest component the harvest over twenty-one settlements is 21
basis points, one basis point above the 20bp floor -- so the entry threshold and the floor
very nearly meet, and a regime paying only the base rate produces a stop the position can
barely be sized against. That is not a coincidence that was arranged; it is the honest
statement that unhedged carry at base funding is not a trade, and it is exactly the kind of
finding a control group exists to produce.
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

__all__ = ["PerpetualFundingCarry"]

_ZERO: Final[Decimal] = Decimal("0")

# Every emitted signal reports the same conviction, for the reason `donchian_breakout`
# gives: the regime is either paying above the base rate or it is not, and there is no
# measured gradation between those. Scaling conviction by the size of the funding rate
# would be a hand-chosen monotone map charged to nobody, on a strategy whose whole purpose
# is to have no such numbers.
_FULL_CONVICTION: Final[Decimal] = Decimal("1")

_STRATEGY_ID: Final[str] = "perpetual-funding-carry"
_STRATEGY_VERSION: Final[int] = 1

_THESIS: Final[str] = (
    "A perpetual whose funding has been paying above the venue's base rate keeps paying it "
    "over the following week, so the side that receives funding earns the carry. The "
    "thesis is wrong, and the position retired, once price has moved against it by more "
    "than the carry the whole holding period was expected to accumulate."
)

_SETTLED_FUNDING_RATE: Final = FeatureRequirement(
    feature_name="settled_funding_rate", feature_version=1
)
_MEAN_ABSOLUTE_FUNDING_RATE: Final = FeatureRequirement(
    feature_name="trailing_mean_absolute_funding_rate", feature_version=1
)

# Binance's perpetual funding cadence. Checked against the file on every ingested row by
# `fking.data.alt.funding`, because the venue has changed it on individual perpetuals and a
# carry summed at the wrong interval is wrong by the ratio of the two.
_SETTLEMENT_INTERVAL: Final[timedelta] = timedelta(hours=8)
# Twenty-one settlements. The shortest horizon over which a funding *regime* rather than a
# single settlement is being harvested, and the same seven days the features that measure
# the rate look back over -- so the window the rate is estimated on and the window it is
# earned over are one number rather than two independent choices.
_SIGNAL_HORIZON: Final[timedelta] = _SETTLEMENT_INTERVAL * 21
# Twenty-four eight-hourly bars is eight days against a deepest declared lookback of seven.
# The registry refuses a warm-up that does not cover it; the margin is so that a later
# interval change does not land exactly on the boundary.
_WARM_UP_BARS: Final[int] = 24

_HARVEST_THRESHOLD_RATE: Final[str] = "harvest_threshold_rate"
_CARRY_SETTLEMENTS_IN_HORIZON: Final[str] = "carry_settlements_in_horizon"
_INVALIDATION_FLOOR_FRACTION: Final[str] = "invalidation_floor_fraction"
_INVALIDATION_CAP_FRACTION: Final[str] = "invalidation_cap_fraction"

PARAMETERS: Final[ParameterSpace] = ParameterSpace(
    (
        # 0.01% per settlement is the fixed interest-rate component of Binance's funding
        # formula -- the rate a perpetual pays when its premium index is flat, which is the
        # venue's own definition of "no directional imbalance". Below it there is no regime
        # to harvest, only the cost of carry the contract charges by construction. It is
        # the venue's constant rather than a level anybody chose, which is what makes it
        # admissible in a control.
        DecimalParameter(
            name=_HARVEST_THRESHOLD_RATE,
            default=Decimal("0.0001"),
            minimum=Decimal("0.00001"),
            maximum=Decimal("0.01"),
        ),
        # The horizon expressed in settlements, which is what turns a per-settlement rate
        # into the carry a position accumulates. It is derived from `_SIGNAL_HORIZON` and
        # the venue's cadence rather than chosen; it is a parameter so the evolution engine
        # can move the holding period without reaching inside `evaluate`, and the two are
        # asserted equal in the tests so a mutation of one cannot silently outrun the other.
        DecimalParameter(
            name=_CARRY_SETTLEMENTS_IN_HORIZON,
            default=Decimal("21"),
            minimum=Decimal("1"),
            maximum=Decimal("90"),
        ),
        # The same floor and cap `donchian_breakout` declares, bounding the denominator of
        # `q = (r_used * E) / |P_entry - P_invalidation|`. 20 basis points is roughly two
        # ticks of spread on the liquid USDT pairs, below which a stop sits inside the noise
        # of a single fill; without it a quiet funding regime produces an unbounded position
        # through arithmetic nobody sees.
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


class PerpetualFundingCarry:
    """Short the perpetual when funding pays shorts, long it when funding pays longs."""

    __slots__ = ("_harvest_threshold_rate", "_spec")

    def __init__(
        self,
        instruments: tuple[Instrument, ...],
        parameters: Mapping[str, ParameterValue] | None = None,
    ) -> None:
        bound = PARAMETERS.bind(parameters)
        self._harvest_threshold_rate = decimal_parameter(bound, _HARVEST_THRESHOLD_RATE)
        self._spec = StrategySpec(
            strategy_id=_STRATEGY_ID,
            strategy_version=_STRATEGY_VERSION,
            thesis=_THESIS,
            instruments=instruments,
            bar_intervals=(_SETTLEMENT_INTERVAL,),
            required_features=(_SETTLED_FUNDING_RATE, _MEAN_ABSOLUTE_FUNDING_RATE),
            warm_up_bars=_WARM_UP_BARS,
            parameters=PARAMETERS,
            invalidation=InvalidationRule(
                adverse_move_fraction=decimal_parameter(bound, _INVALIDATION_FLOOR_FRACTION),
                scaling_feature=_MEAN_ABSOLUTE_FUNDING_RATE,
                scaling_multiple=decimal_parameter(bound, _CARRY_SETTLEMENTS_IN_HORIZON),
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

        settled_rate = state.feature_at(_SETTLED_FUNDING_RATE)
        mean_absolute_rate = state.feature_at(_MEAN_ABSOLUTE_FUNDING_RATE)
        if settled_rate is None or mean_absolute_rate is None:
            return None

        # Both conditions against one threshold rather than two. The trailing mean says the
        # regime has been paying; the settled rate says it still was at the last
        # settlement. A single condition on either alone is the failure this pairing exists
        # to stop: on the mean alone the strategy keeps harvesting a regime that ended a
        # week ago, and on the settled rate alone it takes a week-long position on one
        # print.
        if mean_absolute_rate <= self._harvest_threshold_rate:
            return None
        if abs(settled_rate) <= self._harvest_threshold_rate:
            return None

        # Positive funding means longs pay shorts, so the side that *receives* is short.
        # Getting this backwards produces a strategy that pays the carry it was written to
        # collect, and it would still look like a plausible perpetual strategy in a
        # tearsheet, because the price leg dominates the funding leg over any short window.
        direction = Direction.SHORT if settled_rate > _ZERO else Direction.LONG

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
                f"funding settled at {settled_rate} against a trailing mean magnitude of "
                f"{mean_absolute_rate}, so the "
                f"{'short' if direction is Direction.SHORT else 'long'} side is paid, and "
                f"the invalidation sits where the carry accumulated over the holding "
                f"period stops covering the adverse move"
            ),
            decided_at_utc=as_of,
        )
