"""Charging a round trip: two execution legs plus the funding it held through.

A round trip is two legs rather than one event because both of the things a leg pays --
the fee and the spread -- are paid twice, at two different UTC hours, and the hour is what
selects the spread quantile out of the profile. Charging one leg and doubling it would
price an entry at 08:00 and an exit at 14:00 as though both happened at 08:00, which is
the funding-hour subsidy `_spread` exists to remove, reintroduced one layer up.

Funding is charged once for the whole round trip and comes from the *settlements the
position was actually held through*, not from average exposure. That discreteness is
exploitable and has to be modelled exactly: a strategy that flattens 30 seconds before
settlement pays nothing, and one that holds through pays in full
(`BACKTEST_ENGINE.md` section 4.6).

A leg the book cannot absorb makes the whole round trip uncosted. There is no partial
answer: `breakdown` is `None` and the reason is counted, because a round trip whose entry
was refused is not a cheaper trade, it is a trade that did not happen.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime, timedelta
from decimal import Decimal
from types import MappingProxyType
from typing import Final, Self

from pydantic import BaseModel, ConfigDict, field_validator

from fking.backtest.costs._depth import RejectionReason, walk_depth
from fking.backtest.costs._model import CostModel
from fking.backtest.costs._spread import SpreadQuantile
from fking.backtest.costs._terms import CostBreakdown
from fking.backtest.costs._units import BPS_PER_UNIT, PositiveBaseQuantity, SignedRate
from fking.domain import Direction

_ZERO: Final = Decimal("0")
_NO_OFFSET: Final = timedelta(0)
_NO_REJECTIONS: Final[Mapping[RejectionReason, int]] = MappingProxyType({})

# Longs pay when the funding rate is positive; shorts are paid. A flat position spans no
# settlement it could be charged for.
_FUNDING_MULTIPLIER: Final[Mapping[Direction, Decimal]] = MappingProxyType(
    {
        Direction.LONG: Decimal("1"),
        Direction.SHORT: Decimal("-1"),
        Direction.FLAT: _ZERO,
    }
)


class ExecutionLeg(BaseModel):
    """One side of a round trip: what was asked for, when, and how it was worked."""

    model_config = ConfigDict(frozen=True, extra="forbid", strict=True)

    symbol: str
    base_quantity: PositiveBaseQuantity
    decided_at_utc: datetime
    is_passive: bool

    @field_validator("decided_at_utc")
    @classmethod
    def _decision_time_is_utc(cls, candidate: datetime) -> datetime:
        """Reject rather than convert.

        The hour of this timestamp selects the spread quantile, so a leg stamped in
        `Europe/Baku` is charged the 04:00 spread for an 08:00 decision -- which lands
        exactly on the funding-settlement hours the profile exists to price, and does so
        silently. `astimezone(UTC)` here would launder a wrong offset into a confident
        number with no record that anybody guessed.
        """
        if candidate.tzinfo is None or candidate.utcoffset() is None:
            raise ValueError(f"decided_at_utc must be timezone-aware; got naive {candidate!r}")
        if candidate.utcoffset() != _NO_OFFSET:
            raise ValueError(
                f"decided_at_utc must be UTC; got offset {candidate.utcoffset()!r} in {candidate!r}"
            )
        return candidate

    @property
    def hour_utc(self) -> int:
        """The UTC hour bucket this leg is priced in."""
        return self.decided_at_utc.hour


class FundingExposure(BaseModel):
    """The perpetual funding a round trip was exposed to while it was open."""

    model_config = ConfigDict(frozen=True, extra="forbid", strict=True)

    direction: Direction
    funding_rate: SignedRate
    settlement_count: int

    @field_validator("settlement_count")
    @classmethod
    def _settlements_are_countable(cls, candidate: int) -> int:
        if candidate < 0:
            raise ValueError(f"settlement_count must not be negative; got {candidate}")
        return candidate

    @classmethod
    def none_held(cls) -> Self:
        """A round trip that spanned no settlement -- a spot trade, or an intraday flat."""
        return cls(direction=Direction.FLAT, funding_rate=_ZERO, settlement_count=0)

    def funding_bps(self) -> Decimal:
        """What the position paid in funding, in basis points of notional.

        Negative when the position was paid to hold, which is the whole P&L of a carry
        strategy and is why this term is the one that may legitimately be a credit.
        """
        multiplier = _FUNDING_MULTIPLIER[self.direction]
        return self.funding_rate * Decimal(self.settlement_count) * multiplier * BPS_PER_UNIT


class RoundTrip(BaseModel):
    """An entry, an exit, and the funding held between them."""

    model_config = ConfigDict(frozen=True, extra="forbid", strict=True)

    entry_leg: ExecutionLeg
    exit_leg: ExecutionLeg
    funding: FundingExposure


@dataclass(frozen=True, slots=True)
class RoundTripCost:
    """What one round trip cost, or why it could not be costed."""

    breakdown: CostBreakdown | None
    rejections_by_reason: Mapping[RejectionReason, int]

    @property
    def is_filled(self) -> bool:
        return self.breakdown is not None


def charge_leg(model: CostModel, leg: ExecutionLeg, quantile: SpreadQuantile) -> CostBreakdown:
    """The five per-leg terms. Funding is not among them -- it belongs to the round trip.

    Raises `CostModelConfigError` when the symbol has no calibrated profile. Callers that
    can reach an unfillable leg should walk the depth first; this function assumes the leg
    fills, and charges what filling it costs.
    """
    walk = walk_depth(model.depth_profile_for(leg.symbol), leg.base_quantity)
    spread_profile = model.spread_profile_for(leg.symbol)

    # A passive leg does not cross, so it pays no half-spread -- it pays the
    # adverse-selection markout instead, under the partial-fill term. A marketable leg
    # pays the half-spread and, beyond the touch, the walk into the band.
    spread_bps = _ZERO if leg.is_passive else spread_profile.half_spread_bps(leg.hour_utc, quantile)
    depth_slippage_bps = _ZERO if leg.is_passive else walk.depth_slippage_bps

    if leg.is_passive:
        partial_fill_bps = model.partial_fills.passive_markout_bps
    else:
        extra_fills = max(walk.fill_count - 1, 0)
        partial_fill_bps = model.partial_fills.requote_cost_bps_per_extra_fill * Decimal(
            extra_fills
        )

    return CostBreakdown(
        fees_bps=model.fees.fee_bps_for(is_passive=leg.is_passive),
        spread_bps=spread_bps,
        depth_slippage_bps=depth_slippage_bps,
        # Charged on a passive leg too: a quote that went live late is a stale quote, and
        # a stale quote is the one that gets adversely selected.
        latency_bps=model.latency.latency_bps(),
        partial_fill_bps=partial_fill_bps,
        funding_bps=_ZERO,
    )


def charge_round_trip(
    model: CostModel, round_trip: RoundTrip, quantile: SpreadQuantile
) -> RoundTripCost:
    """Both legs plus funding, or a rejection with the reason counted."""
    rejections: dict[RejectionReason, int] = {}
    for leg in (round_trip.entry_leg, round_trip.exit_leg):
        walk = walk_depth(model.depth_profile_for(leg.symbol), leg.base_quantity)
        if walk.rejection is not None:
            rejections[walk.rejection] = rejections.get(walk.rejection, 0) + 1

    if rejections:
        return RoundTripCost(None, MappingProxyType(dict(rejections)))

    legs = charge_leg(model, round_trip.entry_leg, quantile).plus(
        charge_leg(model, round_trip.exit_leg, quantile)
    )
    funded = CostBreakdown(
        fees_bps=legs.fees_bps,
        spread_bps=legs.spread_bps,
        depth_slippage_bps=legs.depth_slippage_bps,
        latency_bps=legs.latency_bps,
        partial_fill_bps=legs.partial_fill_bps,
        funding_bps=round_trip.funding.funding_bps(),
    )
    return RoundTripCost(funded, _NO_REJECTIONS)
