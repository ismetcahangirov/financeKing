"""The calibrated cost model, and the provenance check that decides whether it is real.

The `field_validator` on `calibration_source` is the whole point of this module. It is
structural enforcement, not a review checklist: a model whose provenance names testnet
cannot be constructed, so it cannot reach a run, so no result can quietly carry it.
`BACKTEST_ENGINE.md` section 4 states the rule and the measured numbers behind it.

Testnet-measured cost is not discarded -- it feeds exactly one consumer, the divergence
monitor in `fking.execution`, which compares realised testnet cost against the production
model to confirm the harness behaves as documented. That consumer does not construct a
`CostModel`, which is why the refusal here costs it nothing.

Fee defaults follow the VIP-0 reference table in `BACKTEST_ENGINE.md` section 4.1.
Assuming a better tier than the account holds is a way to manufacture edge, so the
defaults are the worst tier and a better one has to be stated on purpose.
"""

from __future__ import annotations

from collections.abc import Mapping
from datetime import date
from decimal import Decimal
from typing import Final

from pydantic import BaseModel, ConfigDict, field_validator

from fking.backtest.costs._depth import DepthProfile
from fking.backtest.costs._errors import CostModelConfigError
from fking.backtest.costs._latency import LatencyProfile
from fking.backtest.costs._provenance import require_production_provenance
from fking.backtest.costs._spread import SymbolSpreadProfile
from fking.backtest.costs._units import NonNegativeBps

# Binance USDⓈ-M futures VIP-0, BACKTEST_ENGINE.md section 4.1.
DEFAULT_MAKER_FEE_BPS: Final = Decimal("2.0")
DEFAULT_TAKER_FEE_BPS: Final = Decimal("5.0")

# Production BTCUSDT measurement quoted in BACKTEST_ENGINE.md section 4.5: passive limits
# inside 1 bp filled about 71% of the time with a five-minute post-fill markout of
# -3.2 bp against a captured spread of 0.8 bp. Charged as a positive cost, because the
# markout is a loss to the resting side.
DEFAULT_PASSIVE_MARKOUT_BPS: Final = Decimal("3.2")


class FeeSchedule(BaseModel):
    """Exchange fees, per fill, in basis points of notional at the fill price."""

    model_config = ConfigDict(frozen=True, extra="forbid", strict=True)

    maker_fee_bps: NonNegativeBps = DEFAULT_MAKER_FEE_BPS
    taker_fee_bps: NonNegativeBps = DEFAULT_TAKER_FEE_BPS

    def fee_bps_for(self, *, is_passive: bool) -> Decimal:
        """The rate a leg pays, chosen by whether it added or removed liquidity."""
        return self.maker_fee_bps if is_passive else self.taker_fee_bps


class PartialFillProfile(BaseModel):
    """What it costs when an order does not arrive as one clean print.

    Two distinct effects, deliberately not averaged into one number:

    `passive_markout_bps` is adverse selection. A passive fill is disproportionately one
    that filled *because the market came to you*, so the mid five minutes later is against
    you. Without this term, passive execution appears free and every strategy discovers
    that resting is optimal.

    `requote_cost_bps_per_extra_fill` is what the second and later prints of a walked
    order cost beyond the price walk itself -- the re-quote and queue-loss between prints.
    It is charged per *extra* fill, so an order that filled at the touch pays none of it.
    """

    model_config = ConfigDict(frozen=True, extra="forbid", strict=True)

    passive_markout_bps: NonNegativeBps = DEFAULT_PASSIVE_MARKOUT_BPS
    requote_cost_bps_per_extra_fill: NonNegativeBps


class CostModel(BaseModel):
    """A production-calibrated cost model, with its provenance attached.

    `frozen=True` because the model is content-addressed into a run's identity; a
    parameter changed mid-run would leave every result describing a model that no longer
    exists. `extra="forbid"` because a field this class does not know about is a
    calibration output nobody is charging for.
    """

    model_config = ConfigDict(frozen=True, extra="forbid", strict=True)

    version: str
    calibration_source: str
    calibration_method: str
    calibrated_at: date
    fees: FeeSchedule
    latency: LatencyProfile
    partial_fills: PartialFillProfile
    spreads: Mapping[str, SymbolSpreadProfile]
    depth: Mapping[str, DepthProfile]

    @field_validator("calibration_source")
    @classmethod
    def _provenance_is_production(cls, candidate: str) -> str:
        """Refuse any provenance naming testnet, in any casing or spelling.

        Raises `CalibrationProvenanceError`, which is not a `ValueError` and therefore
        propagates out of `CostModel(...)` rather than being collected into a
        `ValidationError` alongside unrelated field problems. That is deliberate: this is
        not one field failing a bound, it is the whole model failing to be evidence.
        """
        return require_production_provenance(candidate, "calibration_source")

    def spread_profile_for(self, symbol: str) -> SymbolSpreadProfile:
        """The calibrated spread distribution for `symbol`, or a refusal."""
        profile = self.spreads.get(symbol)
        if profile is None:
            raise CostModelConfigError(
                f"{self.version} has no calibrated spread profile for {symbol!r}; "
                f"calibrated symbols are {sorted(self.spreads)}"
            )
        return profile

    def depth_profile_for(self, symbol: str) -> DepthProfile:
        """The calibrated depth profile for `symbol`, or a refusal."""
        profile = self.depth.get(symbol)
        if profile is None:
            raise CostModelConfigError(
                f"{self.version} has no calibrated depth profile for {symbol!r}; "
                f"calibrated symbols are {sorted(self.depth)}"
            )
        return profile
