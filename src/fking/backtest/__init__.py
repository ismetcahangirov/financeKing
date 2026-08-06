"""Engine, cost model and validation. Knows about simulated venues and historical
clocks.

Backtest and live share one code path; only the `ExecutionVenue` swaps. If a strategy
could behave differently here than on the demo account, every result this module
produces would be unfalsifiable.

Cost model parameters are calibrated from production market archives, never from
testnet -- testnet's measured spread is roughly fifty times production's.

What exists today is the deterministic event loop and the run identity built on it: the
totally-ordered queue, simulated time, the content-hashed `RunConfig`, and the trace two
runs are compared on. The market-data source, the venue simulator, the cost model and
the validation harness arrive in their own pull requests and hang off this loop.

A backtest that is not bit-reproducible is not evidence, it is an anecdote with a number
attached -- so **a result that differs between two runs of the same `config_hash`
outranks everything else on the queue**. It is not a flake to be retried: until the
unseeded randomness or the clock read is found, nothing this engine has ever produced
can be trusted.

Everything not in `__all__` is private and may change without notice.
"""

from fking.backtest._clock import SimulationClock
from fking.backtest._config import (
    DEFAULT_EVENT_BUDGET,
    RunConfig,
    canonical_digest,
    config_hash,
    derive_seed,
)
from fking.backtest._engine import EventHandler, EventLoop, RunContext, RunTrace, TraceEntry
from fking.backtest._errors import (
    BacktestError,
    CausalityError,
    EventBudgetExhaustedError,
    RunConfigError,
)
from fking.backtest._events import (
    Event,
    EventPriority,
    FillEvent,
    FundingEvent,
    MarketDataEvent,
    OrderAckEvent,
    ReconciliationEvent,
    RejectEvent,
    TimerEvent,
)
from fking.backtest._queue import EventQueue, QueuedEvent
from fking.backtest.costs import (
    MIN_EDGE_TO_COST_RATIO,
    CalibrationProvenanceError,
    CostBreakdown,
    CostModel,
    CostModelConfigError,
    CostModelError,
    CostTerm,
    CostVerdict,
    DepthProfile,
    DepthWalk,
    ExecutionLeg,
    FeeSchedule,
    FundingExposure,
    LatencyProfile,
    PartialFillProfile,
    RejectionReason,
    RoundTrip,
    RoundTripCost,
    RunCostReport,
    SpreadObservation,
    SpreadQuantile,
    SpreadQuantiles,
    SymbolSpreadProfile,
    assess_run,
    calibrate_spread_profile,
    charge_leg,
    charge_round_trip,
    walk_depth,
)

__all__ = [
    "DEFAULT_EVENT_BUDGET",
    "MIN_EDGE_TO_COST_RATIO",
    "BacktestError",
    "CalibrationProvenanceError",
    "CausalityError",
    "CostBreakdown",
    "CostModel",
    "CostModelConfigError",
    "CostModelError",
    "CostTerm",
    "CostVerdict",
    "DepthProfile",
    "DepthWalk",
    "Event",
    "EventBudgetExhaustedError",
    "EventHandler",
    "EventLoop",
    "EventPriority",
    "EventQueue",
    "ExecutionLeg",
    "FeeSchedule",
    "FillEvent",
    "FundingEvent",
    "FundingExposure",
    "LatencyProfile",
    "MarketDataEvent",
    "OrderAckEvent",
    "PartialFillProfile",
    "QueuedEvent",
    "ReconciliationEvent",
    "RejectEvent",
    "RejectionReason",
    "RoundTrip",
    "RoundTripCost",
    "RunConfig",
    "RunConfigError",
    "RunContext",
    "RunCostReport",
    "RunTrace",
    "SimulationClock",
    "SpreadObservation",
    "SpreadQuantile",
    "SpreadQuantiles",
    "SymbolSpreadProfile",
    "TimerEvent",
    "TraceEntry",
    "assess_run",
    "calibrate_spread_profile",
    "canonical_digest",
    "charge_leg",
    "charge_round_trip",
    "config_hash",
    "derive_seed",
    "walk_depth",
]
