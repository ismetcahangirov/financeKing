"""Builders shared by the cost-model suites.

No tests of its own. It carries the calibrated model every other `test_cost_*` module
charges against, so that a change to the shape of `CostModel` lands in one place rather
than in six, and so that each suite's own file contains only the behaviour it asserts.

The spread profile is deliberately *not* flat. Hours 00, 08 and 16 UTC carry double the
other hours' spread, because that is what production does around funding settlement
(`BACKTEST_ENGINE.md` section 4.2) and because a flat fixture would let an hour-of-day
regression pass every test in this directory.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Final

from fking.backtest.costs import (
    HOURS_IN_DAY,
    CostModel,
    DepthProfile,
    ExecutionLeg,
    FeeSchedule,
    FundingExposure,
    LatencyProfile,
    PartialFillProfile,
    RoundTrip,
    SpreadQuantiles,
    SymbolSpreadProfile,
)
from fking.domain import Direction

SYMBOL: Final = "BTCUSDT"

# USDⓈ-M perpetual funding settles at 00:00, 08:00 and 16:00 UTC, and the spread widens
# around each of them.
FUNDING_HOURS: Final[frozenset[int]] = frozenset({0, 8, 16})

BASE_P50_BPS: Final = Decimal("0.16")  # production BTCUSDT median, BACKTEST_ENGINE.md 4.2
BASE_P99_BPS: Final = Decimal("1.20")

TOUCH_BASE: Final = Decimal("2")
BAND_BASE: Final = Decimal("10")

CALIBRATION_SOURCE: Final = "binance_um_production_2026-03..2026-05"


def spread_profile(
    *, p50_bps: Decimal = BASE_P50_BPS, p99_bps: Decimal = BASE_P99_BPS
) -> SymbolSpreadProfile:
    """A 24-hour profile whose funding hours are twice as wide as the rest."""
    hourly = {
        hour: SpreadQuantiles(
            p50_bps=p50_bps * (Decimal("2") if hour in FUNDING_HOURS else Decimal("1")),
            p99_bps=p99_bps * (Decimal("2") if hour in FUNDING_HOURS else Decimal("1")),
        )
        for hour in range(HOURS_IN_DAY)
    }
    return SymbolSpreadProfile(hourly=hourly)


def flat_spread_profile(bps: Decimal) -> SymbolSpreadProfile:
    """A profile with the same value at every hour and both quantiles.

    Used where the assertion is about something other than spread and a varying spread
    would only add noise -- never where the p50/p99 distinction is under test.
    """
    return SymbolSpreadProfile(
        hourly={hour: SpreadQuantiles(p50_bps=bps, p99_bps=bps) for hour in range(HOURS_IN_DAY)}
    )


def depth_profile(
    *, touch_base: Decimal = TOUCH_BASE, band_base: Decimal = BAND_BASE
) -> DepthProfile:
    return DepthProfile(depth_at_touch_base=touch_base, band_1pct_base=band_base)


def latency_profile(drift_bps_per_second: Decimal = Decimal("0.5")) -> LatencyProfile:
    """The reference stage timings from BACKTEST_ENGINE.md section 4.4."""
    return LatencyProfile(
        decision_to_send=timedelta(milliseconds=250),
        send_to_ack=timedelta(milliseconds=180),
        ack_to_fill=timedelta(milliseconds=95),
        adverse_drift_bps_per_second=drift_bps_per_second,
    )


def cost_model(  # noqa: PLR0913 - one keyword per calibrated component of CostModel; a
    # params object here would be a second thing to keep in step with the model's fields.
    *,
    calibration_source: str = CALIBRATION_SOURCE,
    spreads: Mapping[str, SymbolSpreadProfile] | None = None,
    depth: Mapping[str, DepthProfile] | None = None,
    fees: FeeSchedule | None = None,
    latency: LatencyProfile | None = None,
    partial_fills: PartialFillProfile | None = None,
) -> CostModel:
    """A fully calibrated model with production provenance."""
    return CostModel(
        version="costs-2026.05.1",
        calibration_source=calibration_source,
        calibration_method="bookTicker quantiles by UTC hour, nearest rank",
        calibrated_at=datetime(2026, 6, 1, tzinfo=UTC).date(),
        fees=fees if fees is not None else FeeSchedule(),
        latency=latency if latency is not None else latency_profile(),
        partial_fills=partial_fills
        if partial_fills is not None
        else PartialFillProfile(requote_cost_bps_per_extra_fill=Decimal("1.5")),
        spreads=spreads if spreads is not None else {SYMBOL: spread_profile()},
        depth=depth if depth is not None else {SYMBOL: depth_profile()},
    )


def costless_model(*, calibration_source: str = CALIBRATION_SOURCE) -> CostModel:
    """Every calibrated parameter at zero.

    Not a plausible market. It exists so that a run can be driven to a
    `round_trip_cost_bp` of exactly zero, which is the condition that makes
    `edge_to_cost_ratio` infinite and voids the result.
    """
    return cost_model(
        calibration_source=calibration_source,
        spreads={SYMBOL: flat_spread_profile(Decimal("0"))},
        fees=FeeSchedule(maker_fee_bps=Decimal("0"), taker_fee_bps=Decimal("0")),
        latency=latency_profile(Decimal("0")),
        partial_fills=PartialFillProfile(
            passive_markout_bps=Decimal("0"),
            requote_cost_bps_per_extra_fill=Decimal("0"),
        ),
    )


def execution_leg(
    *,
    hour_utc: int = 12,
    base_quantity: Decimal = Decimal("1"),
    is_passive: bool = False,
    symbol: str = SYMBOL,
) -> ExecutionLeg:
    return ExecutionLeg(
        symbol=symbol,
        base_quantity=base_quantity,
        decided_at_utc=datetime(2026, 5, 14, hour_utc, 30, tzinfo=UTC),
        is_passive=is_passive,
    )


def round_trip(
    *,
    hour_utc: int = 12,
    base_quantity: Decimal = Decimal("1"),
    is_passive: bool = False,
    funding: FundingExposure | None = None,
    symbol: str = SYMBOL,
) -> RoundTrip:
    """An entry and an exit in the same UTC hour, holding no funding by default."""
    leg = execution_leg(
        hour_utc=hour_utc, base_quantity=base_quantity, is_passive=is_passive, symbol=symbol
    )
    return RoundTrip(
        entry_leg=leg,
        exit_leg=leg,
        funding=funding if funding is not None else FundingExposure.none_held(),
    )


def funding_credit(rate: Decimal = Decimal("0.0001"), settlements: int = 1) -> FundingExposure:
    """A short paid to hold, which makes the funding term a credit rather than a cost."""
    return FundingExposure(
        direction=Direction.SHORT, funding_rate=rate, settlement_count=settlements
    )


def default_round_trips() -> Sequence[RoundTrip]:
    """One fillable round trip per funding hour plus one outside them."""
    return tuple(round_trip(hour_utc=hour) for hour in (0, 8, 12, 16))
