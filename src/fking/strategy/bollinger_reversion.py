"""The mean-reversion baseline: a 2-sigma Bollinger excursion, faded only out of a breakout.

A **control**, on the same terms as `donchian_breakout`: twenty periods and two standard
deviations are Bollinger's own numbers, they were fixed before any of this was run, and
they are not tuned afterwards. A swept baseline stops being a reference point and charges
its trials to the global counter on behalf of a strategy nobody intends to promote
(`docs/rules/overfitting-defences.md`).

**The regime filter is the part that matters, and it costs entries on purpose.** A
z-score of -2 is, by construction, usually also the lowest close in the window -- the
excursion and the breakdown are the same event seen through two features. Fading it is the
classic way a mean-reversion book dies: the distribution of "price is far below its mean"
contains both the noise this thesis feeds on and the beginning of every trend that has ever
happened, and nothing in the z-score distinguishes them. Requiring
`donchian_channel_breakout_state == 0` says: fade an excursion only once it has stopped
making new extremes. It rejects the entry at the moment of maximum apparent edge, which is
the moment the two populations are least distinguishable.

The consequence is honest and should not be optimised away: this baseline trades rarely,
and it will look inactive next to the breakout it is the complement of. That is the correct
behaviour of a conservative control, and widening the filter to make the tearsheet busier
would be tuning a control group.

**The invalidation is one further band width beyond the entry.** Entry at two sigma of the
window's own price dispersion, falsification at three: the excursion the thesis calls
temporary has instead extended by half again, which is the observation that distinguishes
"stretched" from "repricing". It scales with the band rather than with a fixed fraction of
price for the reason `_invalidation` gives -- a fixed distance means different things in a
calm window and a violent one -- and it is declared as a multiple of a declared feature so
that `fking.strategy.step` recomputes it rather than trusting the emitted number.
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

__all__ = ["BollingerBandReversion"]

# `donchian_channel_breakout_state`'s own encoding for "inside the channel", documented in
# its docstring. Not a threshold anybody chose, so not a parameter: a search over which
# state counts as quiet would be a search over the feature's definition.
_INSIDE_THE_CHANNEL: Final[Decimal] = Decimal("0")

# One conviction for every signal, for the reason `donchian_breakout` gives: there is no
# measured map from excursion depth to realised outcome, and inventing a monotone one would
# be a hand-chosen parameter charged to nobody. `fking.risk` calibrates conviction against
# outcome regardless.
_FULL_CONVICTION: Final[Decimal] = Decimal("1")

_STRATEGY_ID: Final[str] = "bollinger-band-reversion"
_STRATEGY_VERSION: Final[int] = 1

_THESIS: Final[str] = (
    "A close two or more standard deviations from the mean of the trailing twenty periods "
    "returns towards that mean, provided the market has stopped making new twenty-period "
    "extremes. The thesis is wrong, and the position retired, once the excursion has "
    "extended to three standard deviations instead."
)

_Z_SCORE: Final = FeatureRequirement(feature_name="bollinger_z_score", feature_version=1)
_BAND_WIDTH: Final = FeatureRequirement(
    feature_name="bollinger_band_width_fraction", feature_version=1
)
_CHANNEL_STATE: Final = FeatureRequirement(
    feature_name="donchian_channel_breakout_state", feature_version=1
)

_BAR_INTERVAL: Final[timedelta] = timedelta(minutes=15)
# Six hours against a deepest declared lookback of five, with the same margin and for the
# same reason as the breakout baseline.
_WARM_UP_BARS: Final[int] = 24
# The window that produced the excursion is the window over which it is expected to unwind.
# Any shorter horizon is a claim about the speed of reversion that this baseline has no
# evidence for.
_SIGNAL_HORIZON: Final[timedelta] = timedelta(hours=5)

_ENTRY_Z_SCORE: Final[str] = "entry_z_score"
_INVALIDATION_BAND_MULTIPLE: Final[str] = "invalidation_band_multiple"
_INVALIDATION_FLOOR_FRACTION: Final[str] = "invalidation_floor_fraction"
_INVALIDATION_CAP_FRACTION: Final[str] = "invalidation_cap_fraction"

PARAMETERS: Final[ParameterSpace] = ParameterSpace(
    (
        # Two standard deviations: Bollinger's own band, and the number every textbook
        # states. Fixed a priori and not searched.
        DecimalParameter(
            name=_ENTRY_Z_SCORE,
            default=Decimal("2"),
            minimum=Decimal("1"),
            maximum=Decimal("4"),
        ),
        # One further band width beyond the two the entry sits at, so the falsification
        # level is the three-sigma line. `bollinger_band_width_fraction` reports one
        # standard deviation as a fraction of the window's mean close, so a multiple of one
        # is exactly one more band.
        DecimalParameter(
            name=_INVALIDATION_BAND_MULTIPLE,
            default=Decimal("1"),
            minimum=Decimal("0.25"),
            maximum=Decimal("4"),
        ),
        # The same floor and cap the breakout baseline declares, and for the same reason:
        # this fraction is the denominator of every position sized from these signals, and
        # a window with almost no dispersion would otherwise divide it by nearly nothing.
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


class BollingerBandReversion:
    """Short a stretched-high close, long a stretched-low one, and never into a breakout."""

    __slots__ = ("_entry_z_score", "_spec")

    def __init__(
        self,
        instruments: tuple[Instrument, ...],
        parameters: Mapping[str, ParameterValue] | None = None,
    ) -> None:
        bound = PARAMETERS.bind(parameters)
        self._entry_z_score = decimal_parameter(bound, _ENTRY_Z_SCORE)
        self._spec = StrategySpec(
            strategy_id=_STRATEGY_ID,
            strategy_version=_STRATEGY_VERSION,
            thesis=_THESIS,
            instruments=instruments,
            bar_intervals=(_BAR_INTERVAL,),
            required_features=(_Z_SCORE, _BAND_WIDTH, _CHANNEL_STATE),
            warm_up_bars=_WARM_UP_BARS,
            parameters=PARAMETERS,
            invalidation=InvalidationRule(
                adverse_move_fraction=decimal_parameter(bound, _INVALIDATION_FLOOR_FRACTION),
                volatility_feature=_BAND_WIDTH,
                volatility_multiple=decimal_parameter(bound, _INVALIDATION_BAND_MULTIPLE),
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

        z_score = state.feature_at(_Z_SCORE)
        if z_score is None:
            return None
        # The regime filter, before the entry test rather than after it: an excursion that
        # is still making new extremes is the one case where this thesis and a trend are
        # indistinguishable, and the trend is the one that keeps going.
        if state.feature_at(_CHANNEL_STATE) != _INSIDE_THE_CHANNEL:
            return None

        if z_score >= self._entry_z_score:
            direction = Direction.SHORT
        elif z_score <= -self._entry_z_score:
            direction = Direction.LONG
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
                f"the close of {decision_bar.close_quote_price} sits {z_score} standard "
                f"deviations from the trailing twenty-period mean, past the declared entry "
                f"of {self._entry_z_score}, and the market is no longer making new "
                f"twenty-period extremes"
            ),
            decided_at_utc=as_of,
        )
