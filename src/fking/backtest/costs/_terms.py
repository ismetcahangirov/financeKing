"""The six cost terms, kept separate all the way to the report.

`BACKTEST_ENGINE.md` section 4: total round-trip cost is the sum of six terms, reported
separately, never as one number. The separation is not presentation. A strategy paying
40 bp of which 32 is funding is a carry position with an execution problem it does not
have; the same 40 bp of which 32 is depth slippage is a capacity problem, and the two
have completely different futures. Collapsing them to `round_trip_cost_bp = 40` deletes
the only field that distinguishes them.

`as_terms()` and `round_trip_cost_bp` are defined against the same mapping on purpose:
the total is *derived* from the enumerated terms rather than accumulated alongside them,
so a seventh term cannot be added to the breakdown and left out of the total. The
property test asserts the complementary half -- that the mapping's keys are exactly the
`CostTerm` members -- which is the direction a hand-written mapping can still get wrong.

Sign: every term is a cost, so positive means the strategy paid. Funding is the one term
that may be negative, because a short with a positive funding rate is *paid* to hold, and
a model that clamped that at zero would delete the entire P&L of a carry strategy.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from decimal import Decimal
from enum import StrEnum
from types import MappingProxyType
from typing import Final

from fking.backtest.costs._errors import CostModelConfigError

_ZERO: Final = Decimal("0")


class CostTerm(StrEnum):
    """The six terms. Adding a member here without a field is caught by the term test."""

    FEES = "fees"
    SPREAD = "spread"
    DEPTH_SLIPPAGE = "depth_slippage"
    LATENCY = "latency"
    PARTIAL_FILL = "partial_fill"
    FUNDING = "funding"


@dataclass(frozen=True, slots=True)
class CostBreakdown:
    """One round trip's cost, decomposed. All values in basis points of notional."""

    fees_bps: Decimal
    spread_bps: Decimal
    depth_slippage_bps: Decimal
    latency_bps: Decimal
    partial_fill_bps: Decimal
    funding_bps: Decimal

    @classmethod
    def zero(cls) -> CostBreakdown:
        """The additive identity, used as the accumulator seed over a run's trades."""
        return cls(_ZERO, _ZERO, _ZERO, _ZERO, _ZERO, _ZERO)

    def as_terms(self) -> Mapping[CostTerm, Decimal]:
        """Every term, keyed by its name. The single source the total is derived from."""
        return MappingProxyType(
            {
                CostTerm.FEES: self.fees_bps,
                CostTerm.SPREAD: self.spread_bps,
                CostTerm.DEPTH_SLIPPAGE: self.depth_slippage_bps,
                CostTerm.LATENCY: self.latency_bps,
                CostTerm.PARTIAL_FILL: self.partial_fill_bps,
                CostTerm.FUNDING: self.funding_bps,
            }
        )

    @property
    def round_trip_cost_bp(self) -> Decimal:
        """The sum of the six terms, in basis points of notional."""
        return sum(self.as_terms().values(), start=_ZERO)

    def plus(self, other: CostBreakdown) -> CostBreakdown:
        """Term-by-term addition, for accumulating a run.

        Named rather than `__add__` because a breakdown is not a number: `a + b` reads as
        producing a total, and what this produces is a second breakdown whose terms are
        still individually meaningful only because they were summed pairwise.
        """
        return CostBreakdown(
            self.fees_bps + other.fees_bps,
            self.spread_bps + other.spread_bps,
            self.depth_slippage_bps + other.depth_slippage_bps,
            self.latency_bps + other.latency_bps,
            self.partial_fill_bps + other.partial_fill_bps,
            self.funding_bps + other.funding_bps,
        )

    def divided_by(self, divisor: int) -> CostBreakdown:
        """Term-by-term division, for the per-trade mean over a run."""
        if divisor <= 0:
            raise CostModelConfigError(f"cannot average a breakdown over {divisor} trades")
        scale = Decimal(divisor)
        return CostBreakdown(
            self.fees_bps / scale,
            self.spread_bps / scale,
            self.depth_slippage_bps / scale,
            self.latency_bps / scale,
            self.partial_fill_bps / scale,
            self.funding_bps / scale,
        )
