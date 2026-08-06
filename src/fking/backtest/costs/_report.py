"""Gross, cost and net -- always three numbers -- and the gate that reads them.

`BACKTEST_ENGINE.md` section 4, reporting: a net number alone is uninterpretable. It
cannot distinguish a large edge eaten by costs from a small edge that survived, and those
two have completely different futures. So `RunCostReport` carries all three and the
decomposition behind the middle one.

Two refusals live here.

**`edge_to_cost_ratio` below 2.0 rejects, regardless of net return.** A strategy whose
gross edge is 1.5x its costs is one cost-model revision, one fee-tier change or one
volatility regime away from unprofitable, and it will spend its life oscillating across
the line. The threshold is applied literally: a ratio of 1.9999 failed.

**A ratio that computes as infinite or negative voids the result rather than reporting
it.** `BACKTEST_ENGINE.md` section 9 lists this under failure handling with the diagnosis
attached: the cost model did not run. The three ways to get there are a zero cost (the
model charged nothing, so the ratio is infinite), a negative cost (the model produced a
net credit, so the ratio's sign is inverted and its magnitude reads like a quality score),
and a negative gross edge (a loss divided by a cost, whose magnitude also reads like a
quality score). None of the three is a weak result. Reporting `edge_to_cost_ratio = -4.1`
next to `edge_to_cost_ratio = 4.1` in the same column is how a losing strategy gets
skimmed as a good one, which is why the field is `None` under `VOID` and the type system
makes every reader handle it.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from decimal import Decimal
from enum import StrEnum
from types import MappingProxyType
from typing import Final

from fking.backtest.costs._charge import RoundTrip, charge_round_trip
from fking.backtest.costs._depth import RejectionReason
from fking.backtest.costs._model import CostModel
from fking.backtest.costs._spread import SpreadQuantile
from fking.backtest.costs._terms import CostBreakdown

# BACKTEST_ENGINE.md section 4, reporting. Not a tunable: raising it is a change to what
# this project considers evidence, and lowering it is the failure it exists to prevent.
MIN_EDGE_TO_COST_RATIO: Final = Decimal("2.0")

_ZERO: Final = Decimal("0")


class CostVerdict(StrEnum):
    """What the cost gate concluded about a run.

    `VOID` is not a worse `REJECTED`. A rejected run produced numbers and failed a
    threshold; a void run produced a ratio that cannot be interpreted, so the run tells
    you nothing about the strategy and everything about the model.
    """

    ACCEPTED = "accepted"
    REJECTED_EDGE_TO_COST = "rejected_edge_to_cost"
    VOID = "void"


@dataclass(frozen=True, slots=True)
class RunCostReport:
    """One run's economics, with gross, cost and net kept apart."""

    calibration_source: str
    quantile: SpreadQuantile
    filled_trade_count: int
    rejections_by_reason: Mapping[RejectionReason, int]
    gross_edge_per_trade_bp: Decimal
    round_trip_cost_bp: Decimal
    net_edge_per_trade_bp: Decimal
    gross_return_bps: Decimal
    total_cost_bps: Decimal
    net_return_bps: Decimal
    breakdown: CostBreakdown
    edge_to_cost_ratio: Decimal | None
    verdict: CostVerdict
    void_reason: str | None

    @property
    def is_evidence(self) -> bool:
        """Whether this run may be used to argue anything about the strategy."""
        return self.verdict is CostVerdict.ACCEPTED


def _verdict_for(
    gross_edge_per_trade_bp: Decimal, round_trip_cost_bp: Decimal
) -> tuple[Decimal | None, CostVerdict, str | None]:
    """The ratio, the verdict and -- when void -- the reason, decided together."""
    if round_trip_cost_bp == _ZERO:
        return (
            None,
            CostVerdict.VOID,
            "round_trip_cost_bp is zero, so edge_to_cost_ratio is infinite: the cost "
            "model did not run",
        )
    if round_trip_cost_bp < _ZERO:
        return (
            None,
            CostVerdict.VOID,
            f"round_trip_cost_bp is {round_trip_cost_bp}, a net credit; the ratio's sign "
            f"is inverted and its magnitude would read as a quality score",
        )
    ratio = gross_edge_per_trade_bp / round_trip_cost_bp
    if ratio < _ZERO:
        return (
            None,
            CostVerdict.VOID,
            f"gross_edge_per_trade_bp is {gross_edge_per_trade_bp}, so the ratio is "
            f"negative; a loss divided by a cost is not a quality score",
        )
    if ratio < MIN_EDGE_TO_COST_RATIO:
        return ratio, CostVerdict.REJECTED_EDGE_TO_COST, None
    return ratio, CostVerdict.ACCEPTED, None


def assess_run(
    model: CostModel,
    round_trips: Sequence[RoundTrip],
    *,
    gross_edge_per_trade_bp: Decimal,
    quantile: SpreadQuantile,
) -> RunCostReport:
    """Charge every round trip and gate the run on what the charges came to.

    `gross_edge_per_trade_bp` is the strategy's edge *before* costs, supplied by whatever
    computed the fills. It is an input rather than something derived here on purpose: the
    cost model's job is to say what trading cost, and a module that also decided what
    trading earned could not be audited against the engine that produced the fills.

    Rejected round trips do not contribute a cost and do not contribute an edge. They are
    counted in `rejections_by_reason`, which is the number that turns a capacity limit
    into a visible finding instead of an absence.
    """
    accumulated = CostBreakdown.zero()
    rejections: dict[RejectionReason, int] = {}
    filled_trade_count = 0

    for round_trip in round_trips:
        charged = charge_round_trip(model, round_trip, quantile)
        for reason, occurrences in charged.rejections_by_reason.items():
            rejections[reason] = rejections.get(reason, 0) + occurrences
        if charged.breakdown is not None:
            accumulated = accumulated.plus(charged.breakdown)
            filled_trade_count += 1

    if filled_trade_count == 0:
        return RunCostReport(
            calibration_source=model.calibration_source,
            quantile=quantile,
            filled_trade_count=0,
            rejections_by_reason=MappingProxyType(dict(rejections)),
            gross_edge_per_trade_bp=gross_edge_per_trade_bp,
            round_trip_cost_bp=_ZERO,
            net_edge_per_trade_bp=_ZERO,
            gross_return_bps=_ZERO,
            total_cost_bps=_ZERO,
            net_return_bps=_ZERO,
            breakdown=CostBreakdown.zero(),
            edge_to_cost_ratio=None,
            verdict=CostVerdict.VOID,
            void_reason=(
                "no round trip was fillable, so nothing was charged; a run with no "
                "fills measures the venue's depth, not the strategy"
            ),
        )

    per_trade = accumulated.divided_by(filled_trade_count)
    round_trip_cost_bp = per_trade.round_trip_cost_bp
    net_edge_per_trade_bp = gross_edge_per_trade_bp - round_trip_cost_bp
    trades = Decimal(filled_trade_count)
    ratio, verdict, void_reason = _verdict_for(gross_edge_per_trade_bp, round_trip_cost_bp)

    return RunCostReport(
        calibration_source=model.calibration_source,
        quantile=quantile,
        filled_trade_count=filled_trade_count,
        rejections_by_reason=MappingProxyType(dict(rejections)),
        gross_edge_per_trade_bp=gross_edge_per_trade_bp,
        round_trip_cost_bp=round_trip_cost_bp,
        net_edge_per_trade_bp=net_edge_per_trade_bp,
        gross_return_bps=gross_edge_per_trade_bp * trades,
        total_cost_bps=round_trip_cost_bp * trades,
        net_return_bps=net_edge_per_trade_bp * trades,
        breakdown=per_trade,
        edge_to_cost_ratio=ratio,
        verdict=verdict,
        void_reason=void_reason,
    )
