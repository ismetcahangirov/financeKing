"""The depth conservatism rule, and the rejection it produces.

Stated once, and it is a limitation rather than a model:

> **Assume the quoted top-of-book quantity is all the liquidity there is, until a fill
> proves otherwise.**

`SOURCES.md` section 2 records why: free full-depth L2 order-book history does not exist
(VF-017), and `bookDepth` is aggregated bands sampled about once a minute. So
`depth_at_touch_base` is the `bookTicker` quantity and *nothing behind it is observable*.

The consequence is three regimes:

- at or inside the touch, the order takes the quoted price and pays no depth slippage --
  the half-spread has already been charged by the spread term;
- beyond the touch and within the +-1% band, the order walks into the band at a linearly
  interpolated price;
- beyond the +-1% band notional, the order is **rejected as unfillable**.

The square-root impact law `impact_coefficient * (q / depth) ** impact_exponent` that
`BACKTEST_ENGINE.md` section 4.3 quotes is a prior, not a measurement, and this module
deliberately does not use it. Fitting it needs per-level book history we do not have, so
a coefficient here would be a number with no provenance sitting in the term that decides
capacity. The linear walk uses only what the +-1% band actually reports.

A backtest full of size rejections has discovered a capacity limit, which is a genuine
finding. The alternative -- filling arbitrary size at the touch -- is how a strategy with
no capacity gets promoted.
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
from enum import StrEnum
from typing import Final, Self

from pydantic import BaseModel, ConfigDict, model_validator

from fking.backtest.costs._errors import CostModelConfigError
from fking.backtest.costs._units import PositiveBaseQuantity

# The band is +-1%, and 1% is 100 basis points. Named rather than inlined because it is
# the width the interpolation is scaled by, and a reader who sees a bare 100 next to a
# basis-point quantity has to decide whether it is a percentage or a count.
BAND_WIDTH_BPS: Final = Decimal("100")

_TWO: Final = Decimal("2")
_ZERO: Final = Decimal("0")

# A walk into the band arrives as the touch fill plus the band fill: two prints, one
# order. More would need per-level history to place, and inventing intermediate levels
# would manufacture a fill schedule the archive cannot support.
_FILLS_WITHIN_TOUCH: Final = 1
_FILLS_WALKING_THE_BAND: Final = 2


class RejectionReason(StrEnum):
    """Why the modelled venue refused an order.

    One member, because one rejection is modelled here. The other reasons
    `BACKTEST_ENGINE.md` section 4.7 lists -- notional and lot filters, price bands, rate
    limits -- belong to the venue simulator that applies `Instrument`'s filters and to the
    rate limiter, and enumerating them here before anything can raise them would produce a
    report whose zero counts mean "not implemented" while reading as "never happened".
    """

    UNFILLABLE_DEPTH = "unfillable_depth"


class DepthProfile(BaseModel):
    """The two depth quantities the production archive can actually support."""

    model_config = ConfigDict(frozen=True, extra="forbid", strict=True)

    depth_at_touch_base: PositiveBaseQuantity
    band_1pct_base: PositiveBaseQuantity

    @model_validator(mode="after")
    def _band_contains_the_touch(self) -> Self:
        if self.band_1pct_base < self.depth_at_touch_base:
            raise ValueError(
                f"the +-1% band quantity {self.band_1pct_base} is below the touch "
                f"quantity {self.depth_at_touch_base}; the band is cumulative and "
                f"therefore includes the touch"
            )
        return self


@dataclass(frozen=True, slots=True)
class DepthWalk:
    """What the book did to an order of a given size.

    `filled_base_quantity` is zero on a rejection rather than partial: the conservatism
    rule refuses to invent a price for the portion beyond the band, and filling the part
    that fits would report a smaller trade than the strategy asked for while hiding that
    it asked for more.
    """

    filled_base_quantity: Decimal
    depth_slippage_bps: Decimal
    fill_count: int
    rejection: RejectionReason | None

    @property
    def is_filled(self) -> bool:
        return self.rejection is None


def walk_depth(profile: DepthProfile, base_quantity: Decimal) -> DepthWalk:
    """Fill `base_quantity` against `profile`, or refuse it as unfillable.

    Magnitude only: a short of 3 BTC consumes the same observable depth as a long of 3.

    The interpolation is linear in the band, so the marginal price at a fraction `f` of
    the way through it is displaced by `BAND_WIDTH_BPS * f`, and the *average* over the
    walked portion is half that. The walked portion is then weighted by its share of the
    order, because the quantity that filled at the touch paid no walk at all.
    """
    requested = abs(base_quantity)
    if requested <= _ZERO:
        # Not a rejection: a zero-quantity order is a caller error, not a market outcome,
        # and returning it as `UNFILLABLE_DEPTH` would put a modelling bug into the
        # capacity report as a finding about the book.
        raise CostModelConfigError(f"base_quantity must be non-zero; got {base_quantity}")

    if requested <= profile.depth_at_touch_base:
        return DepthWalk(requested, _ZERO, _FILLS_WITHIN_TOUCH, None)

    if requested > profile.band_1pct_base:
        return DepthWalk(_ZERO, _ZERO, 0, RejectionReason.UNFILLABLE_DEPTH)

    walked = requested - profile.depth_at_touch_base
    band_depth = profile.band_1pct_base - profile.depth_at_touch_base
    fraction_into_band = walked / band_depth
    average_walk_bps = BAND_WIDTH_BPS * fraction_into_band / _TWO
    return DepthWalk(
        requested,
        average_walk_bps * walked / requested,
        _FILLS_WALKING_THE_BAND,
        None,
    )
