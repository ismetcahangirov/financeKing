"""The cost model: six terms, kept separate, calibrated only from production data.

This is where honest backtests are separated from marketing. Most strategies that die in
this system die here, and they die correctly.

Three properties carry the design, and each one is structural rather than procedural:

**Provenance is enforced at construction.** `CostModel.calibration_source` runs through a
`field_validator` that refuses any string naming testnet, in any casing or spelling. The
measured reason is a factor of 47: Binance USDⓈ-M futures testnet showed a median BTCUSDT
spread of 7.5 bp against production's 0.16 bp, with roughly 10x inflated volume. The
instinct that a 47x overstated spread is merely conservative is what gets the rule
relaxed, and it is wrong in both directions -- testnet is pessimistic on spread and
optimistic on fill and capacity at the same time, and the failure that has actually
occurred is the inverted config (`7.5` entered as `0.075` bp) producing a model 2x
*cheaper* than production. Provenance is disqualifying regardless of which way the error
pushed the result, because direction is not a property you can read off the result.

**Spread is a distribution with an hour-of-day profile, not a scalar.** BTCUSDT's spread
roughly doubles around the 00:00/08:00/16:00 UTC funding settlements, so a strategy that
concentrates its entries there and is charged a flat median is being subsidised by the
cost model in exactly the hours it selected. Runs execute at p50 and are re-run at p99;
an edge that disappears at p99 dies during the only conditions that matter.

**Depth is assumed to be what is quoted, and nothing more.** Free full-depth L2 history
does not exist (`SOURCES.md` section 2, VF-017), so an order beyond the touch walks the
+-1% band at a linearly interpolated price and an order beyond the band is *rejected as
unfillable* rather than filled at an invented one. A backtest full of size rejections has
discovered a capacity limit, which is a genuine finding.

Everything not in `__all__` is private and may change without notice.
"""

from fking.backtest.costs._calibrate import SpreadObservation, calibrate_spread_profile
from fking.backtest.costs._charge import (
    ExecutionLeg,
    FundingExposure,
    RoundTrip,
    RoundTripCost,
    charge_leg,
    charge_round_trip,
)
from fking.backtest.costs._depth import (
    BAND_WIDTH_BPS,
    DepthProfile,
    DepthWalk,
    RejectionReason,
    walk_depth,
)
from fking.backtest.costs._errors import (
    CalibrationProvenanceError,
    CostModelConfigError,
    CostModelError,
)
from fking.backtest.costs._latency import LatencyProfile
from fking.backtest.costs._model import (
    DEFAULT_MAKER_FEE_BPS,
    DEFAULT_PASSIVE_MARKOUT_BPS,
    DEFAULT_TAKER_FEE_BPS,
    CostModel,
    FeeSchedule,
    PartialFillProfile,
)
from fking.backtest.costs._provenance import names_testnet
from fking.backtest.costs._report import (
    MIN_EDGE_TO_COST_RATIO,
    CostVerdict,
    RunCostReport,
    assess_run,
)
from fking.backtest.costs._spread import (
    HOURS_IN_DAY,
    SpreadQuantile,
    SpreadQuantiles,
    SymbolSpreadProfile,
)
from fking.backtest.costs._terms import CostBreakdown, CostTerm
from fking.backtest.costs._units import BPS_PER_UNIT

__all__: tuple[str, ...] = (
    "BAND_WIDTH_BPS",
    "BPS_PER_UNIT",
    "DEFAULT_MAKER_FEE_BPS",
    "DEFAULT_PASSIVE_MARKOUT_BPS",
    "DEFAULT_TAKER_FEE_BPS",
    "HOURS_IN_DAY",
    "MIN_EDGE_TO_COST_RATIO",
    "CalibrationProvenanceError",
    "CostBreakdown",
    "CostModel",
    "CostModelConfigError",
    "CostModelError",
    "CostTerm",
    "CostVerdict",
    "DepthProfile",
    "DepthWalk",
    "ExecutionLeg",
    "FeeSchedule",
    "FundingExposure",
    "LatencyProfile",
    "PartialFillProfile",
    "RejectionReason",
    "RoundTrip",
    "RoundTripCost",
    "RunCostReport",
    "SpreadObservation",
    "SpreadQuantile",
    "SpreadQuantiles",
    "SymbolSpreadProfile",
    "assess_run",
    "calibrate_spread_profile",
    "charge_leg",
    "charge_round_trip",
    "names_testnet",
    "walk_depth",
)
