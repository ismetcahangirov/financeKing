"""One strategy, present so that the contract has a concrete shape to be checked against.

The thesis is a plain continuation claim -- a large trailing one-hour return persists over
the next hour -- and it is deliberately unremarkable. Its job here is not to make money;
`CLAUDE.md` section 1 is explicit that generating strategies is the easy part. Its job is
to be the second caller that makes the contract real: something the registry validates,
the runner drives, the determinism replay digests, the warm-up property quantifies over,
and the look-ahead probe poisons.

Two things about it are worth reading rather than skimming.

**Every number it compares against comes from the declared parameter space.** There is no
`Decimal(...)` anywhere in `evaluate`, and `tests/strategy/test_no_magic_parameters.py`
asserts that for every shipped strategy. A threshold written as a literal in the body is a
parameter chosen by a human, never charged to the global trial ledger, invisible to the
evolution engine, and unreproducible from the spec -- which makes the lineage recorded for
its descendants a lie (`_parameters`).

**It reads `state.visible_bars(as_of)` and never `state.recent_bars`.** The two are
identical in live, where no future bar exists, and differ in backtest, where the whole
history is in memory. Reading the filtered view is the only spelling that behaves the same
in both, which is the parity `ARCHITECTURE.md` section 4 makes structural.

The instruments arrive from the caller rather than being written here. Tick size and lot
step are venue facts resolved at startup from the tradable universe
(`docs/rules/exchange-integration.md`), and a strategy that hardcoded them would be
declaring an instrument that may not be listed on the venue the run is pointed at.
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
    IntegerParameter,
    ParameterSpace,
    ParameterValue,
    decimal_parameter,
    integer_parameter,
)
from fking.strategy._spec import FeatureRequirement, StrategySpec

__all__ = ["TrailingReturnContinuation"]

_ZERO: Final[Decimal] = Decimal("0")
_ONE: Final[Decimal] = Decimal("1")

_STRATEGY_ID: Final[str] = "trailing-return-continuation"
_STRATEGY_VERSION: Final[int] = 1

_THESIS: Final[str] = (
    "A trailing one-hour return whose magnitude exceeds the declared entry threshold "
    "continues in the same direction over the following hour. The thesis is wrong, and "
    "the signal is retired, once price has moved against the decision close by the "
    "declared adverse-move fraction."
)

# The feature registry's key for the series this strategy consumes. Declared as a
# requirement rather than imported: the value arrives from the store, already stamped
# with an `available_at_utc` derived from the feature's declared lag, and a strategy
# calling the computation itself would bypass every point-in-time mechanism there is
# (`tools/checks/feature_registry.py`).
_TRAILING_RETURN: Final = FeatureRequirement(
    feature_name="trailing_return_fraction", feature_version=1
)

_BAR_INTERVAL: Final[timedelta] = timedelta(minutes=15)
# Two hours at the declared interval, against a feature lookback of one. The registry
# refuses a warm-up that does not cover the deepest required lookback; the margin here is
# so that a later interval change does not silently land exactly on the boundary.
_WARM_UP_BARS: Final[int] = 8
_SIGNAL_HORIZON: Final[timedelta] = timedelta(hours=1)

_ENTRY_RETURN_FRACTION: Final[str] = "entry_return_fraction"
_ADVERSE_MOVE_FRACTION: Final[str] = "adverse_move_fraction"
_CONVICTION_SPAN: Final[str] = "conviction_span"

PARAMETERS: Final[ParameterSpace] = ParameterSpace(
    (
        DecimalParameter(
            name=_ENTRY_RETURN_FRACTION,
            default=Decimal("0.004"),
            minimum=Decimal("0.0005"),
            maximum=Decimal("0.05"),
        ),
        # In the space rather than in the InvalidationRule literal, because the
        # invalidation distance is the denominator of every position sized from this
        # strategy's signals (`RISK_PHILOSOPHY.md` section 3.1). A distance fixed by hand
        # is the single most consequential unsearched number a strategy can hold.
        DecimalParameter(
            name=_ADVERSE_MOVE_FRACTION,
            default=Decimal("0.01"),
            minimum=Decimal("0.002"),
            maximum=Decimal("0.10"),
        ),
        # How many multiples of the entry threshold correspond to full conviction. One
        # means every qualifying signal reports 1.0, which is the degenerate end of the
        # dimension and is deliberately reachable: a search that finds it has learned the
        # conviction channel carries nothing, and `RISK_PHILOSOPHY.md` section 2 flattens
        # it anyway against realised outcome.
        IntegerParameter(name=_CONVICTION_SPAN, default=3, minimum=1, maximum=10),
    )
)


class TrailingReturnContinuation:
    """Long a strong positive trailing return, short a strong negative one, else silent."""

    __slots__ = ("_conviction_span", "_entry_return_fraction", "_spec")

    def __init__(
        self,
        instruments: tuple[Instrument, ...],
        parameters: Mapping[str, ParameterValue] | None = None,
    ) -> None:
        bound = PARAMETERS.bind(parameters)
        self._entry_return_fraction = decimal_parameter(bound, _ENTRY_RETURN_FRACTION)
        self._conviction_span = integer_parameter(bound, _CONVICTION_SPAN)
        self._spec = StrategySpec(
            strategy_id=_STRATEGY_ID,
            strategy_version=_STRATEGY_VERSION,
            thesis=_THESIS,
            instruments=instruments,
            bar_intervals=(_BAR_INTERVAL,),
            required_features=(_TRAILING_RETURN,),
            warm_up_bars=_WARM_UP_BARS,
            parameters=PARAMETERS,
            invalidation=InvalidationRule(
                adverse_move_fraction=decimal_parameter(bound, _ADVERSE_MOVE_FRACTION)
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

        trailing_return_fraction = state.feature_at(_TRAILING_RETURN)
        if trailing_return_fraction is None:
            return None

        magnitude = abs(trailing_return_fraction)
        if magnitude < self._entry_return_fraction:
            return None

        # The newest bar that had already closed at `as_of`. Never `state.recent_bars[-1]`:
        # the two agree in live and differ in backtest, and the difference is the leak.
        decision_bar = visible[-1]
        direction = Direction.LONG if trailing_return_fraction > _ZERO else Direction.SHORT
        full_conviction_at = self._entry_return_fraction * self._conviction_span
        conviction = _ONE if magnitude >= full_conviction_at else magnitude / full_conviction_at

        return Signal(
            strategy_id=self._spec.strategy_id,
            instrument=bar.instrument,
            direction=direction,
            conviction=conviction,
            horizon=self._spec.signal_horizon,
            invalidation_quote_price=self._spec.invalidation.level_for(
                direction=direction,
                reference_quote_price=decision_bar.close_quote_price,
                instrument=bar.instrument,
            ),
            rationale=(
                f"trailing one-hour return {trailing_return_fraction} exceeds the declared "
                f"entry threshold {self._entry_return_fraction} at a close of "
                f"{decision_bar.close_quote_price}"
            ),
            decided_at_utc=as_of,
        )
