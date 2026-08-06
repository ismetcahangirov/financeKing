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
without inventing bars, the cost model in `fking.backtest.costs`, and the overfitting gate
in `fking.backtest.validation`, which discounts an observed Sharpe by the number of
configurations searched to find it and refuses a search whose in-sample ranking carries no
out-of-sample information. The venue simulator and the portfolio metric suite arrive in
their own pull requests and hang off this loop.

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
from fking.backtest.accounting import (
    MIN_OBSERVATIONS_FOR_AUTOCORRELATION,
    ONE_DAY,
    REPORT_QUANTUM,
    TRADING_DAYS_PER_YEAR,
    AccountCurrencyError,
    AccountEvent,
    AccountLedgerError,
    AccountTransition,
    EffectiveSample,
    EffectiveSampleError,
    EquityCurve,
    EquityPathRuinedError,
    EquityPoint,
    EventOrderError,
    FundingKey,
    FundingSettlement,
    GridBoundaryError,
    MarkUnavailableError,
    PortfolioAccount,
    PortfolioAccountingError,
    daily_mark_grid,
    effective_sample,
    event_instant_utc,
    mark_to_market,
    settle_funding,
)
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
from fking.backtest.feed import (
    CoverageRefusedError,
    CoverageReport,
    FeedRequest,
    FeedSlice,
    MarketDataFeed,
    SeriesRequest,
    WarmupGate,
)
from fking.backtest.validation import (
    MAX_PROBABILITY_OF_BACKTEST_OVERFITTING,
    MIN_DEFLATED_SHARPE,
    OverfittingProbability,
    PathSplit,
    PathSplitMalformedError,
    SharpeEvidence,
    SharpeEvidenceUnusableError,
    TrialCountUnavailableError,
    ValidationGateError,
    ValidationRefusal,
    ValidationReport,
    assess_validation,
    deflated_sharpe_ratio,
    expected_max_sharpe,
    probability_of_backtest_overfitting,
)

__all__ = [
    "DEFAULT_EVENT_BUDGET",
    "MAX_PROBABILITY_OF_BACKTEST_OVERFITTING",
    "MIN_DEFLATED_SHARPE",
    "MIN_EDGE_TO_COST_RATIO",
    "MIN_OBSERVATIONS_FOR_AUTOCORRELATION",
    "ONE_DAY",
    "REPORT_QUANTUM",
    "TRADING_DAYS_PER_YEAR",
    "AccountCurrencyError",
    "AccountEvent",
    "AccountLedgerError",
    "AccountTransition",
    "BacktestError",
    "CalibrationProvenanceError",
    "CausalityError",
    "CostBreakdown",
    "CostModel",
    "CostModelConfigError",
    "CostModelError",
    "CostTerm",
    "CostVerdict",
    "CoverageRefusedError",
    "CoverageReport",
    "DepthProfile",
    "DepthWalk",
    "EffectiveSample",
    "EffectiveSampleError",
    "EquityCurve",
    "EquityPathRuinedError",
    "EquityPoint",
    "Event",
    "EventBudgetExhaustedError",
    "EventHandler",
    "EventLoop",
    "EventOrderError",
    "EventPriority",
    "EventQueue",
    "ExecutionLeg",
    "FeeSchedule",
    "FeedRequest",
    "FeedSlice",
    "FillEvent",
    "FundingEvent",
    "FundingExposure",
    "FundingKey",
    "FundingSettlement",
    "GridBoundaryError",
    "LatencyProfile",
    "MarkUnavailableError",
    "MarketDataEvent",
    "MarketDataFeed",
    "OrderAckEvent",
    "OverfittingProbability",
    "PartialFillProfile",
    "PathSplit",
    "PathSplitMalformedError",
    "PortfolioAccount",
    "PortfolioAccountingError",
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
    "SeriesRequest",
    "SharpeEvidence",
    "SharpeEvidenceUnusableError",
    "SimulationClock",
    "SpreadObservation",
    "SpreadQuantile",
    "SpreadQuantiles",
    "SymbolSpreadProfile",
    "TimerEvent",
    "TraceEntry",
    "TrialCountUnavailableError",
    "ValidationGateError",
    "ValidationRefusal",
    "ValidationReport",
    "WarmupGate",
    "assess_run",
    "assess_validation",
    "calibrate_spread_profile",
    "canonical_digest",
    "charge_leg",
    "charge_round_trip",
    "config_hash",
    "daily_mark_grid",
    "deflated_sharpe_ratio",
    "derive_seed",
    "effective_sample",
    "event_instant_utc",
    "expected_max_sharpe",
    "mark_to_market",
    "probability_of_backtest_overfitting",
    "settle_funding",
    "walk_depth",
]
