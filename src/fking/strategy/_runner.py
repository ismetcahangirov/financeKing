"""Driving a strategy one bar at a time, and refusing everything the spec did not declare.

The runner is where the specification stops being documentation. Four things happen here
that a strategy is deliberately not trusted to do for itself:

**The engine suppresses during warm-up, not the strategy.** A strategy self-policing its
warm-up is a strategy whose first meaningful bar is a property of its body rather than of
its declaration, so the walk-forward embargo -- which is sized from declared numbers --
is sized wrong, and adjacent folds share information that nothing reports.

**An undeclared bar is refused rather than evaluated.** An instrument the spec never
named or an interval it never subscribed to is data the backtest never gated. It reaches
`evaluate` as a perfectly ordinary `Bar`, produces a perfectly ordinary signal, and the
run is then a measurement of a strategy that could not have been deployed.

**A bar whose close has not happened is refused.** Not ignored: a current bar arriving
from the engine's future is a defect in the engine, and swallowing it would hide the
defect while producing decisions. The symmetric case -- *history* containing bars from
the future -- is the strategy's problem and is handled by `StrategyState.visible_bars`,
because that is the asymmetry between backtest and live that the shared code path exists
to expose.

**The emitted signal is checked against the spec that produced it.** Most sharply, the
invalidation level: `RISK_PHILOSOPHY.md` section 3.1 makes that level the denominator of
every position size, so a strategy free to name any level is a strategy free to size
itself. Checking it against the declared rule is what turns `InvalidationRule` from a
description into a constraint.

Every function here is pure. The state is threaded through explicitly rather than held on
an object, so a replay of a fold cannot observe state left over from the fold before it.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal

from fking.domain import Bar, Direction, Signal
from fking.strategy._contract import Clock, Strategy, StrategyState, initial_state
from fking.strategy._errors import ObservationRefusedError, SignalRefusedError
from fking.strategy._guards import require_utc
from fking.strategy._spec import FeatureRequirement, StrategySpec

__all__ = ["StrategyStep", "replay", "step"]

_NO_FEATURES: Mapping[FeatureRequirement, Decimal] = {}


@dataclass(frozen=True, slots=True)
class StrategyStep:
    """One bar's outcome: the state after it, and the belief it produced if any.

    A pair rather than a mutated runner, so the caller decides what to keep. The backtest
    keeps both; a look-ahead probe keeps the signal stream alone and throws the state
    away, which is only safe because the state was never shared.
    """

    state: StrategyState
    signal: Signal | None


def step(
    strategy: Strategy,
    state: StrategyState,
    bar: Bar,
    clock: Clock,
    *,
    feature_values: Mapping[FeatureRequirement, Decimal] | None = None,
) -> StrategyStep:
    """Consume `bar` and return the new state plus whatever belief it produced."""
    spec = strategy.spec
    as_of = require_utc(clock(), "clock()")
    supplied = _NO_FEATURES if feature_values is None else feature_values

    _require_declared_observation(spec, bar, as_of=as_of, state=state)
    _require_known_requirements(spec, supplied)

    advanced = state.advanced_with(bar, feature_values=supplied, window_bars=spec.warm_up_bars + 1)
    if advanced.bars_consumed < spec.warm_up_bars:
        # Suppressed by the engine, so the strategy body never has to know it is warming
        # up -- and so the number of suppressed bars is exactly the declared one.
        return StrategyStep(state=advanced, signal=None)

    _require_every_declared_feature(spec, supplied)
    signal = strategy.evaluate(advanced, bar, clock)
    if signal is not None:
        _require_signal_matches_spec(spec, signal, bar, as_of=as_of)
    return StrategyStep(state=advanced, signal=signal)


def replay(
    strategy: Strategy,
    bars: Sequence[Bar],
    *,
    seed: int,
    feature_values_at: Mapping[datetime, Mapping[FeatureRequirement, Decimal]] | None = None,
) -> tuple[Signal, ...]:
    """Every signal `strategy` emits over `bars`, in order.

    The clock advances to each bar's close time, which is what a backtest loop does and
    what live does: a decision is taken at the instant the bar it was computed from
    became knowable, and never earlier. Feature values are keyed by that same instant, so
    a caller cannot accidentally supply a value stamped at a time the decision could not
    have seen.
    """
    values_by_instant = feature_values_at or {}
    state = initial_state(seed=seed)
    emitted: list[Signal] = []
    for observed in bars:
        outcome = step(
            strategy,
            state,
            observed,
            _fixed_clock(observed.close_time_utc),
            feature_values=values_by_instant.get(observed.close_time_utc, _NO_FEATURES),
        )
        state = outcome.state
        if outcome.signal is not None:
            emitted.append(outcome.signal)
    return tuple(emitted)


def _fixed_clock(as_of: datetime) -> Clock:
    """A clock frozen at `as_of`.

    A closure rather than `SimulationClock`: that type lives in `fking.backtest`, which
    sits *above* `fking.strategy` in the layering, so importing it here would invert the
    dependency the whole module map is arranged around.
    """
    return lambda: as_of


def _require_declared_observation(
    spec: StrategySpec, bar: Bar, *, as_of: datetime, state: StrategyState
) -> None:
    if not spec.declares(bar.instrument):
        raise ObservationRefusedError(
            f"{spec.describe()} was handed a {bar.instrument.symbol} bar and declares "
            f"{sorted(instrument.symbol for instrument in spec.instruments)}; a bar the "
            f"spec did not name is data the backtest never gated"
        )
    interval = bar.close_time_utc - bar.open_time_utc
    if not spec.declares_interval(interval):
        raise ObservationRefusedError(
            f"{spec.describe()} was handed a {interval} bar and declares "
            f"{sorted(spec.bar_intervals)}; warm_up_bars is only a duration against a "
            f"declared interval, so an undeclared one makes the embargo unsizable"
        )
    if bar.close_time_utc > as_of:
        raise ObservationRefusedError(
            f"{spec.describe()} was handed a bar closing at "
            f"{bar.close_time_utc.isoformat()} while the clock reads "
            f"{as_of.isoformat()}; the bar has not finished, and a value read from it is "
            f"look-ahead that produces a plausible equity curve rather than an error"
        )
    if state.recent_bars and bar.open_time_utc <= state.recent_bars[-1].open_time_utc:
        raise ObservationRefusedError(
            f"{spec.describe()} was handed a bar opening at "
            f"{bar.open_time_utc.isoformat()} after one opening at "
            f"{state.recent_bars[-1].open_time_utc.isoformat()}; the series went backwards"
        )


def _require_known_requirements(
    spec: StrategySpec, feature_values: Mapping[FeatureRequirement, Decimal]
) -> None:
    """Refuse a feature value the specification never asked for.

    An undeclared value reaching `evaluate` is an input the registry never checked against
    the availability contract and the look-ahead probe never covered -- the exact route
    `fking.data.features.registry` exists to close, reopened one layer up.
    """
    undeclared = sorted(
        requirement.describe()
        for requirement in feature_values
        if requirement not in spec.required_features
    )
    if undeclared:
        raise ObservationRefusedError(
            f"{spec.describe()} was handed values for {undeclared}, which it does not "
            f"declare; an undeclared input is one no availability check gated"
        )


def _require_every_declared_feature(
    spec: StrategySpec, feature_values: Mapping[FeatureRequirement, Decimal]
) -> None:
    missing = sorted(
        requirement.describe()
        for requirement in spec.required_features
        if requirement not in feature_values
    )
    if missing:
        raise ObservationRefusedError(
            f"{spec.describe()} declares {missing} and was handed no value for them after "
            f"its {spec.warm_up_bars}-bar warm-up. Registration validates that the warm-up "
            f"covers every required feature's lookback, so an absent value here is the "
            f"engine failing to subscribe rather than the series being short"
        )


def _require_signal_matches_spec(
    spec: StrategySpec, signal: Signal, bar: Bar, *, as_of: datetime
) -> None:
    if signal.strategy_id != spec.strategy_id:
        raise SignalRefusedError(
            f"{spec.describe()} emitted a signal attributed to {signal.strategy_id!r}; "
            f"the survival score, the lineage edge and the audit row all key on that id"
        )
    if signal.instrument != bar.instrument:
        raise SignalRefusedError(
            f"{spec.describe()} decided on a {bar.instrument.symbol} bar and emitted a "
            f"signal on {signal.instrument.symbol}"
        )
    if signal.decided_at_utc != as_of:
        raise SignalRefusedError(
            f"{spec.describe()} stamped a signal at {signal.decided_at_utc.isoformat()} "
            f"while the injected clock read {as_of.isoformat()}; a decision time that is "
            f"not the injected instant means a second clock exists somewhere"
        )
    if signal.horizon != spec.signal_horizon:
        raise SignalRefusedError(
            f"{spec.describe()} declares a horizon of {spec.signal_horizon} and emitted "
            f"{signal.horizon}; the horizon sizes the walk-forward embargo and the label"
        )
    _require_declared_invalidation(spec, signal, bar)


def _require_declared_invalidation(spec: StrategySpec, signal: Signal, bar: Bar) -> None:
    """The emitted level must be the one the declared rule produces.

    This is the check that makes the invalidation declaration load-bearing. The level is
    the denominator of `q = (r_used * E) / |P_entry - P_invalidation|`, so a strategy free
    to widen it at will is a strategy free to enlarge its own position -- which is the
    authority the risk engine holds alone.
    """
    if signal.direction is Direction.FLAT:
        return
    expected = spec.invalidation.level_for(
        direction=signal.direction,
        reference_quote_price=bar.close_quote_price,
        instrument=bar.instrument,
    )
    if signal.invalidation_quote_price != expected:
        raise SignalRefusedError(
            f"{spec.describe()} emitted an invalidation level of "
            f"{signal.invalidation_quote_price} on a {signal.direction} signal; its "
            f"declared rule applied to the bar close of {bar.close_quote_price} yields "
            f"{expected}. The level is the denominator of every position sized from this "
            f"signal, so an undeclared one is strategy-side sizing with extra steps"
        )
