"""Fixtures for `RiskEngine.decide()`.

The universe is five symbols and the book already holds four of them at equal notional.
That is not padding. Concentration binds on `CTR_i / sigma_p`, so a book holding one name
carries 100% of its own risk by definition and breaches every concentration limit there is
-- including on the first order the system ever sends. Building the fixtures around a book
that is already diversified keeps the other tests testing what they are named for; the
single-name case has its own test, which asserts the refusal rather than working around it.

`ConcentrationLimits` here are set at the compiled-in ceilings (40% cluster, 50% asset)
rather than at the shipped defaults (25% / 35%). With four equally risky uncorrelated names
the shipped cluster default is hit *exactly* at 25%, and a fixture sitting on a threshold
turns a rounding change into a test failure that says nothing about the code.
"""

from __future__ import annotations

from dataclasses import replace
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Final
from uuid import UUID

from fking.domain import (
    Direction,
    Instrument,
    Portfolio,
    Position,
    Signal,
    Venue,
)
from fking.risk import (
    CLUSTER_CUT_CORRELATION,
    CalibrationMap,
    ConcentrationLimits,
    CorrelationMatrix,
    DrawdownState,
    InstrumentMarketState,
    KillSwitchGate,
    KillSwitchState,
    KillSwitchStatus,
    MarketState,
    PortfolioState,
    RiskEngine,
    RiskModel,
    RiskPolicy,
    StrategyState,
    cluster_by_correlation,
    open_first_time,
)
from fking.risk.drawdown import Scope

DECIDED_AT: Final = datetime(2026, 8, 1, 12, 0, tzinfo=UTC)
SIGNALLED_AT: Final = DECIDED_AT - timedelta(minutes=1)
CORRELATION_ID: Final = UUID("11111111-2222-3333-4444-555555555555")
SEED: Final = 20260801

EQUITY_USD: Final = Decimal("100000")
# Each held name is opened at this notional so the four of them carry equal risk share.
HELD_NOTIONAL_USD: Final = Decimal("2000")

_ONE: Final = Decimal("1")
_ZERO: Final = Decimal("0")

# Filters copied from Binance spot testnet `exchangeInfo`. A lot step of 1 would make every
# generated quantity trivially on-lattice and the quantization properties would pass without
# testing anything.
_MARK_USD_BY_SYMBOL: Final[dict[str, str]] = {
    "ADAUSDT": "0.80",
    "BNBUSDT": "600.00",
    "BTCUSDT": "64000.00",
    "ETHUSDT": "3200.00",
    "SOLUSDT": "160.00",
}
UNIVERSE: Final[tuple[str, ...]] = tuple(sorted(_MARK_USD_BY_SYMBOL))
TRADED_SYMBOL: Final = "BTCUSDT"
HELD_SYMBOLS: Final[tuple[str, ...]] = tuple(
    symbol for symbol in UNIVERSE if symbol != TRADED_SYMBOL
)


def instrument_for(symbol: str) -> Instrument:
    return Instrument(
        venue=Venue.BINANCE_SPOT_TESTNET,
        symbol=symbol,
        base_asset=symbol.removesuffix("USDT"),
        quote_asset="USDT",
        tick_size=Decimal("0.01"),
        lot_step=Decimal("0.00001"),
        min_notional_quote=Decimal("10.00"),
    )


INSTRUMENTS: Final[dict[str, Instrument]] = {symbol: instrument_for(symbol) for symbol in UNIVERSE}
BTCUSDT: Final = INSTRUMENTS[TRADED_SYMBOL]


def mark_usd(symbol: str) -> Decimal:
    return Decimal(_MARK_USD_BY_SYMBOL[symbol])


def make_risk_model(symbols: tuple[str, ...] = UNIVERSE, *, correlation: str = "0.30") -> RiskModel:
    """An equicorrelated model. At 0.30 every name is its own cluster (cut is 0.70)."""
    entries = {
        symbol_a: {
            symbol_b: (_ONE if symbol_a == symbol_b else Decimal(correlation))
            for symbol_b in symbols
        }
        for symbol_a in symbols
    }
    matrix = CorrelationMatrix(symbols=symbols, entries=entries)
    return RiskModel(
        symbols=symbols,
        daily_volatility_ratio=dict.fromkeys(symbols, Decimal("0.03")),
        correlations=matrix,
        shrinkage_intensity_ratio=_ZERO,
        psd_repair_ratio=_ZERO,
        clusters=cluster_by_correlation(matrix, cut_correlation=CLUSTER_CUT_CORRELATION),
    )


def make_market_state(
    *,
    symbols: tuple[str, ...] = UNIVERSE,
    model_symbols: tuple[str, ...] | None = None,
    mid_offset_quote: str = "0",
) -> MarketState:
    """Market state for the whole universe.

    `mid_offset_quote` moves the mid away from the mark, which is what makes it visible in a
    test whether an internal cross was booked at the mid or at the mark. `model_symbols`
    narrows the risk model below the priced set, which is the shape a stale covariance
    estimate arrives in: marks for a symbol the model has never seen.
    """
    return MarketState(
        instruments={
            INSTRUMENTS[symbol]: InstrumentMarketState(
                instrument=INSTRUMENTS[symbol],
                mark_usd=mark_usd(symbol),
                mid_quote_price=mark_usd(symbol) + Decimal(mid_offset_quote),
                atr_14_quote=mark_usd(symbol) * Decimal("0.02"),
                # Flat returns: the volatility floor is what ends up used, so the
                # volatility term is a constant across tests rather than a moving part.
                return_series=(_ZERO,) * 60,
                volatility_floor_annualised=Decimal("0.50"),
            )
            for symbol in symbols
        },
        risk_model=make_risk_model(model_symbols or symbols),
    )


def make_position(symbol: str, *, notional_usd: Decimal = HELD_NOTIONAL_USD) -> Position:
    instrument = INSTRUMENTS[symbol]
    price = mark_usd(symbol)
    return Position(
        instrument=instrument,
        signed_base_quantity=instrument.quantize_base_quantity(notional_usd / price),
        average_entry_quote_price=price,
        realised_pnl_quote=_ZERO,
        fee_quote_paid=_ZERO,
        opened_at_utc=SIGNALLED_AT,
        applied_fill_ids=frozenset(),
    )


def make_calibration(strategy_id: str) -> CalibrationMap:
    """An unfitted map: `calibrated()` returns the constant 0.5 and says so."""
    return CalibrationMap(
        strategy_id=strategy_id,
        available_at_utc=DECIDED_AT - timedelta(days=1),
        observation_count=0,
        buckets=(),
    )


def make_strategy_state(
    strategy_id: str, *, drawdown_state: DrawdownState | None = None
) -> StrategyState:
    return StrategyState(
        strategy_id=strategy_id,
        calibration=make_calibration(strategy_id),
        closed_trade_count=0,
        realised_mean_return_fraction=_ZERO,
        realised_return_stdev_fraction=_ZERO,
        drawdown_state=drawdown_state,
    )


def drawdown_state(
    scope: Scope, subject_id: str, *, current_equity_usd: Decimal = EQUITY_USD
) -> DrawdownState:
    """State for a subject opened at `EQUITY_USD` and since marked to `current_equity_usd`.

    Marking it down is how a breach is produced: at half the peak the drawdown ratio is 0.50
    against a budget of 0.10, so `evaluate` latches. Constructed through `open_first_time`
    and then `replace`d rather than assembled by hand, so the rolling window and the day
    anchor are the ones the module itself builds.
    """
    opened = open_first_time(
        scope=scope,
        subject_id=subject_id,
        opening_equity_usd=EQUITY_USD,
        as_of_utc=DECIDED_AT,
    )
    if current_equity_usd == EQUITY_USD:
        return opened
    return replace(opened, current_equity_usd=current_equity_usd)


def make_portfolio_state(
    *,
    strategy_ids: tuple[str, ...] = ("alpha", "beta"),
    held_symbols: tuple[str, ...] = HELD_SYMBOLS,
    positions: tuple[Position, ...] | None = None,
    equity_usd: Decimal = EQUITY_USD,
    strategies: dict[str, StrategyState] | None = None,
    portfolio_drawdown: DrawdownState | None = None,
) -> PortfolioState:
    held = (
        positions
        if positions is not None
        else tuple(make_position(symbol) for symbol in held_symbols)
    )
    return PortfolioState(
        portfolio=Portfolio(as_of_utc=DECIDED_AT, positions=held, cash_balances={}),
        equity_usd=equity_usd,
        strategies=strategies
        or {strategy_id: make_strategy_state(strategy_id) for strategy_id in strategy_ids},
        drawdown_state=portfolio_drawdown
        or drawdown_state("portfolio", "portfolio", current_equity_usd=equity_usd),
    )


def make_policy(**overrides: object) -> RiskPolicy:
    defaults: dict[str, object] = {
        "concentration_limits": ConcentrationLimits(
            max_cluster_risk_share_ratio=Decimal("0.40"),
            max_asset_risk_share_ratio=Decimal("0.50"),
        ),
        "tradable_symbols": frozenset(UNIVERSE),
    }
    defaults.update(overrides)
    return RiskPolicy(**defaults)  # type: ignore[arg-type]  # kwargs are typed by RiskPolicy


def make_engine(*, policy: RiskPolicy | None = None, halted: bool = False) -> RiskEngine:
    state = (
        KillSwitchState(
            status=KillSwitchStatus.HALTED,
            incident_id=None,
            tripped_at_utc=None,
            trigger=None,
            halted_reason="tripped by the test",
        )
        if halted
        else KillSwitchState(
            status=KillSwitchStatus.TRADING,
            incident_id=None,
            tripped_at_utc=None,
            trigger=None,
            halted_reason=None,
        )
    )
    return RiskEngine(policy=policy or make_policy(), kill_switch=KillSwitchGate(state))


def make_signal(
    strategy_id: str,
    *,
    direction: Direction = Direction.LONG,
    symbol: str = TRADED_SYMBOL,
    conviction: str = "0.60",
    invalidation_quote_price: str | None = "63000.00",
) -> Signal:
    return Signal(
        strategy_id=strategy_id,
        instrument=INSTRUMENTS[symbol],
        direction=direction,
        conviction=Decimal(conviction),
        horizon=timedelta(hours=8),
        invalidation_quote_price=(
            None
            if direction is Direction.FLAT or invalidation_quote_price is None
            else Decimal(invalidation_quote_price)
        ),
        rationale="close above the 20-bar high",
        decided_at_utc=SIGNALLED_AT,
    )


def frozen_clock() -> datetime:
    """The decision instant, injected. `risk` never reads a clock of its own."""
    return DECIDED_AT
