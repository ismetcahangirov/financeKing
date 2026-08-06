"""The strategy contract and its implementations. Knows about features and beliefs.

A strategy is a **declarative specification** plus a **pure function** from
`(state, bar, clock)` to `Signal | None`. Both halves are load-bearing.

The specification is data. Instruments, bar intervals, required features, warm-up,
parameter space with bounds, and the invalidation rule are all frozen typed fields rather
than sentences in a docstring, because everything downstream reads them: the engine
subscribes from the first two, the registry validates the third against the feature
catalogue *before any bar is dispatched*, the runner suppresses signals through the
fourth, the evolution engine mutates inside the fifth, and the risk engine sizes against
the level the sixth produces. Anything a strategy does that is not declared here cannot
be reproduced from the specification alone -- and a lineage whose parent cannot be
reconstructed records nothing.

The function is pure. It may not read the clock, perform I/O, touch the network, or use
randomness that is not derived from `state.seed`. Testability is the least of the reasons.
The real ones are backtest/live parity, which stops being checkable the moment a strategy
can behave differently on Tuesday; and the survival score, which is computed on replayed
history and becomes a measurement of noise -- that the evolution engine then breeds toward
-- if a replay does not reproduce.

A strategy emits a `Signal` and says nothing about size. `fking.strategy` has no import
path to `fking.execution` or `fking.risk`, enforced by `import-linter` rather than by
review. The reason is not human discipline: P5 and P6 will generate strategies via LLM
agents, and an agent asked to improve returns will size its own positions the moment the
type system permits it, because increasing size is the shortest path to the metric it was
handed. It will do so plausibly, with a comment explaining why this case is different, and
it will pass a skim. The constraint has to hold against an author who has not read
`RISK_PHILOSOPHY.md` and never will.

Everything not listed in `__all__` is private and may change without notice.
"""

from fking.strategy._catalogue import SHIPPED_STRATEGIES, StrategyBuilder
from fking.strategy._contract import Clock, Strategy, StrategyState, initial_state
from fking.strategy._errors import (
    DuplicateStrategyError,
    FeatureUnavailableError,
    ObservationRefusedError,
    SignalRefusedError,
    StrategyContractError,
    StrategyError,
)
from fking.strategy._invalidation import InvalidationRule
from fking.strategy._parameters import (
    DecimalParameter,
    IntegerParameter,
    Parameter,
    ParameterSpace,
    ParameterValue,
    decimal_parameter,
    integer_parameter,
)
from fking.strategy._registry import FeatureCatalogue, StrategyRegistry, build_registry
from fking.strategy._runner import StrategyStep, replay, step
from fking.strategy._spec import FeatureRequirement, StrategySpec

__all__: tuple[str, ...] = (
    "SHIPPED_STRATEGIES",
    "Clock",
    "DecimalParameter",
    "DuplicateStrategyError",
    "FeatureCatalogue",
    "FeatureRequirement",
    "FeatureUnavailableError",
    "IntegerParameter",
    "InvalidationRule",
    "ObservationRefusedError",
    "Parameter",
    "ParameterSpace",
    "ParameterValue",
    "SignalRefusedError",
    "Strategy",
    "StrategyBuilder",
    "StrategyContractError",
    "StrategyError",
    "StrategyRegistry",
    "StrategySpec",
    "StrategyState",
    "StrategyStep",
    "build_registry",
    "decimal_parameter",
    "initial_state",
    "integer_parameter",
    "replay",
    "step",
)
