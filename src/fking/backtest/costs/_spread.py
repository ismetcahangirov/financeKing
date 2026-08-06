"""Spread as a distribution with an hour-of-day profile, never as a scalar.

`BACKTEST_ENGINE.md` section 4.2 states the requirement and the reason, and the reason is
not a refinement. BTCUSDT's spread roughly doubles in the hour around the 00:00, 08:00 and
16:00 UTC funding settlements. A strategy that concentrates its entries there and is
charged a flat median is being *subsidised by the cost model* -- the subsidy is largest
in exactly the hours the strategy chose, which is the shape of a selection effect rather
than a rounding error.

So the profile is per symbol, per hour, at two quantiles. Runs execute against p50 and are
re-run against p99 as a robustness check: an edge that disappears at p99 dies during the
only conditions that matter.

Every one of the 24 hours must be present. A missing hour cannot be filled from the daily
median without reintroducing exactly the subsidy this type exists to remove, and a
`KeyError` at charge time is a better outcome than a plausible number.
"""

from __future__ import annotations

from collections.abc import Mapping
from decimal import Decimal
from enum import StrEnum
from typing import Final, Self

from pydantic import BaseModel, ConfigDict, model_validator

from fking.backtest.costs._units import NonNegativeBps

HOURS_IN_DAY: Final = 24

_TWO: Final = Decimal("2")


class SpreadQuantile(StrEnum):
    """Which point of the calibrated spread distribution a run is charged at.

    A `StrEnum` rather than a `Decimal` quantile argument: the profile carries two
    measured order statistics, not a continuous curve, and accepting `Decimal("0.75")`
    would require interpolating between them -- which is inventing a spread that was never
    observed and doing it in the field that decides whether a strategy is profitable.
    """

    P50 = "p50"
    P99 = "p99"


class SpreadQuantiles(BaseModel):
    """One hour's calibrated spread, at both quantiles, in basis points of notional."""

    model_config = ConfigDict(frozen=True, extra="forbid", strict=True)

    p50_bps: NonNegativeBps
    p99_bps: NonNegativeBps

    @model_validator(mode="after")
    def _quantiles_are_ordered(self) -> Self:
        if self.p99_bps < self.p50_bps:
            raise ValueError(
                f"p99 spread {self.p99_bps} is below p50 {self.p50_bps}; the two quantiles "
                f"have been transposed, which makes the p99 robustness run the cheaper one"
            )
        return self

    def at(self, quantile: SpreadQuantile) -> Decimal:
        """The full spread at `quantile`, in basis points."""
        return self.p50_bps if quantile is SpreadQuantile.P50 else self.p99_bps


class SymbolSpreadProfile(BaseModel):
    """One symbol's spread distribution across the 24 UTC hours."""

    model_config = ConfigDict(frozen=True, extra="forbid", strict=True)

    hourly: Mapping[int, SpreadQuantiles]

    @model_validator(mode="after")
    def _covers_every_hour(self) -> Self:
        expected = set(range(HOURS_IN_DAY))
        if set(self.hourly) != expected:
            missing = sorted(expected - set(self.hourly))
            unexpected = sorted(set(self.hourly) - expected)
            raise ValueError(
                f"an hour-of-day spread profile must carry all {HOURS_IN_DAY} UTC hours; "
                f"missing {missing}, unexpected {unexpected}"
            )
        return self

    def spread_bps(self, hour_utc: int, quantile: SpreadQuantile) -> Decimal:
        """The full quoted spread for that UTC hour, in basis points of notional."""
        return self.hourly[hour_utc].at(quantile)

    def half_spread_bps(self, hour_utc: int, quantile: SpreadQuantile) -> Decimal:
        """What a marketable order pays to cross: half the quoted spread.

        A passive fill that is not adversely selected pays none of this, which is why the
        adverse-selection markout is charged separately as a partial-fill effect rather
        than folded in here. Folding it in would make passive execution look free at the
        spread term and then free again overall, and every strategy would discover that
        resting is optimal.
        """
        return self.spread_bps(hour_utc, quantile) / _TWO
