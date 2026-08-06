"""Engine, cost model and validation. Knows about simulated venues and historical
clocks.

Backtest and live share one code path; only the `ExecutionVenue` swaps. If a strategy
could behave differently here than on the demo account, every result this module
produces would be unfalsifiable.

Cost model parameters are calibrated from production market archives, never from
testnet -- testnet's measured spread is roughly fifty times production's.

What exists today is the deterministic event loop and the run identity built on it: the
totally-ordered queue, simulated time, the content-hashed `RunConfig`, and the trace two
runs are compared on -- plus the market-data source in `fking.backtest.feed`, which turns
the Parquet archive into that loop's event stream and refuses a window it cannot serve
without inventing bars. The venue simulator, the cost model and the validation harness
arrive in their own pull requests and hang off this loop.

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
from fking.backtest.feed import (
    CoverageRefusedError,
    CoverageReport,
    FeedRequest,
    FeedSlice,
    MarketDataFeed,
    SeriesRequest,
    WarmupGate,
)

__all__ = [
    "DEFAULT_EVENT_BUDGET",
    "BacktestError",
    "CausalityError",
    "CoverageRefusedError",
    "CoverageReport",
    "Event",
    "EventBudgetExhaustedError",
    "EventHandler",
    "EventLoop",
    "EventPriority",
    "EventQueue",
    "FeedRequest",
    "FeedSlice",
    "FillEvent",
    "FundingEvent",
    "MarketDataEvent",
    "MarketDataFeed",
    "OrderAckEvent",
    "QueuedEvent",
    "ReconciliationEvent",
    "RejectEvent",
    "RunConfig",
    "RunConfigError",
    "RunContext",
    "RunTrace",
    "SeriesRequest",
    "SimulationClock",
    "TimerEvent",
    "TraceEntry",
    "WarmupGate",
    "canonical_digest",
    "config_hash",
    "derive_seed",
]
