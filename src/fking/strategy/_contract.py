"""The contract itself: a pure function from `(state, bar, clock)` to `Signal | None`.

Three properties are structural rather than conventional, and each one exists because
the alternative fails silently.

**The clock is a parameter.** A strategy that reads the wall clock produces a different
decision on Tuesday than it produced on Monday against the same bar. That breaks
backtest/live parity at the only point where parity is checkable, and it makes the
survival score a measurement of noise -- so the evolution engine then breeds toward the
noise. `tools/checks/clock_isolation.py` fails the build on a wall-clock read inside this
package; the `Clock` parameter is what makes the correct spelling available.

**The state is immutable and the transition returns a new one.** `advanced_with` never
mutates. A `StrategyState` shared between the runner, a telemetry consumer and a replay
fold would otherwise let fold *n+1* observe state left over from fold *n*, which is
look-ahead by aliasing: it does not raise, and it inflates the fold Sharpe.

**`visible_bars` takes `as_of` and filters on `close_time_utc`.** The upper bound is the
half that gets omitted, because live has no future bars and the omission is invisible
there, while backtest holds the whole history in memory and leaks. `close_time_utc` is
the stronger of the two available bounds: a bar whose close has not happened yet has not
happened, even though its open is in the past, and a decision taken on its partial
values is the single most common look-ahead defect there is
(`.claude/rules/no-lookahead.md`).
"""

from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import dataclass, replace
from datetime import datetime
from decimal import Decimal
from types import MappingProxyType
from typing import Protocol, runtime_checkable

from fking.domain import Bar, Signal
from fking.strategy._errors import StrategyContractError
from fking.strategy._spec import FeatureRequirement, StrategySpec

__all__ = ["Clock", "Strategy", "StrategyState", "initial_state"]

# The shape every injected clock in this repository has (`fking.backtest.SimulationClock`,
# `fking.platform.scheduler`, `fking.data.live.supervisor`). A one-method `Clock` protocol
# would be a callable with extra ceremony that every test then has to implement.
Clock = Callable[[], datetime]


@dataclass(frozen=True, slots=True, kw_only=True)
class StrategyState:
    """Everything `evaluate` may read besides the bar it is being handed.

    There is deliberately no position, no equity and no open-order field. A strategy that
    can see its own position is a strategy that can size, scale in and average down, and
    `RISK_PHILOSOPHY.md` gives that authority to the risk engine alone -- which nets
    across the whole population and therefore knows things a single strategy cannot.
    """

    bars_consumed: int
    recent_bars: tuple[Bar, ...]
    feature_values: Mapping[FeatureRequirement, Decimal]
    seed: int

    def __post_init__(self) -> None:
        if self.bars_consumed < 0:
            raise StrategyContractError(
                f"bars_consumed must not be negative; got {self.bars_consumed}"
            )
        previous: Bar | None = None
        for observed in self.recent_bars:
            if previous is not None and observed.open_time_utc <= previous.open_time_utc:
                raise StrategyContractError(
                    f"recent_bars must ascend strictly by open_time_utc; "
                    f"{observed.open_time_utc.isoformat()} does not follow "
                    f"{previous.open_time_utc.isoformat()}"
                )
            previous = observed
        # `frozen=True` protects the binding, not the mapping bound to it. Copying and
        # proxying is what makes the immutability real rather than nominal, and a
        # strategy holding a live reference to the engine's feature dict would observe
        # values appearing mid-evaluation.
        object.__setattr__(self, "feature_values", MappingProxyType(dict(self.feature_values)))

    def visible_bars(self, as_of: datetime) -> tuple[Bar, ...]:
        """The bars that had already closed at `as_of`, oldest first.

        The upper bound is the point. A strategy filtering only on a lower bound is
        correct in live -- where no future bar exists -- and leaks in backtest, where the
        whole history is in memory.
        """
        return tuple(observed for observed in self.recent_bars if observed.close_time_utc <= as_of)

    def feature_at(self, requirement: FeatureRequirement) -> Decimal | None:
        """The point-in-time value of one required feature, or `None` if it is absent."""
        return self.feature_values.get(requirement)

    def advanced_with(
        self,
        observed: Bar,
        *,
        feature_values: Mapping[FeatureRequirement, Decimal],
        window_bars: int,
    ) -> StrategyState:
        """The state after consuming `observed`. Returns a new object; mutates nothing.

        `window_bars` bounds the retained history. Unbounded retention would make memory
        a function of run length, and -- worse -- would let a strategy reach further back
        than the lookback its specification declared, which is history the walk-forward
        embargo was never sized for.
        """
        kept = (*self.recent_bars, observed)[-window_bars:] if window_bars > 0 else ()
        return replace(
            self,
            bars_consumed=self.bars_consumed + 1,
            recent_bars=kept,
            feature_values=feature_values,
        )


@runtime_checkable
class Strategy(Protocol):
    """A declarative specification plus one pure function.

    `evaluate` may not read the clock, perform I/O, touch the network, or use randomness
    that is not derived from `state.seed`. It returns a `Signal` or `None`, and there is
    no method on `Signal` -- and no import path out of this package -- that yields an
    `Order`. That absence is enforced by `import-linter` rather than by review, because
    the author of a future strategy will be an LLM agent that has not read
    `RISK_PHILOSOPHY.md` and never will.
    """

    @property
    def spec(self) -> StrategySpec:
        """The declaration everything downstream reads."""

    def evaluate(self, state: StrategyState, bar: Bar, clock: Clock) -> Signal | None:
        """The belief this strategy holds about `bar.instrument` at `clock()`.

        `None` means "no opinion", which is not the same as a flat `Signal` -- flat means
        "no position" and is an instruction the risk engine nets against.
        """


def initial_state(*, seed: int) -> StrategyState:
    """The state a run starts from.

    `seed` is the only sanctioned source of randomness inside `evaluate`. A strategy
    needing a draw seeds a generator from it, so two replays of one run agree; a strategy
    reaching for an unseeded generator makes the survival score unreproducible, and an
    unreproducible score is one the evolution engine optimises anyway.
    """
    empty: Mapping[FeatureRequirement, Decimal] = {}
    return StrategyState(bars_consumed=0, recent_bars=(), feature_values=empty, seed=seed)
