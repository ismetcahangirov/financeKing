"""Shared construction helpers for the portfolio accounting and metric tests.

The instrument filters are BTCUSDT's on Binance spot; they are here so a quantity used
in a test is one the venue would actually have accepted, which is the difference between
exercising the accounting and exercising a number.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Final
from uuid import UUID, uuid5

from fking.backtest.portfolio import DailyMark, EquityPath, EquityPoint, PortfolioState
from fking.domain import Fill, Instrument, Side, Venue

BTCUSDT: Final = Instrument(
    venue=Venue.BINANCE_SPOT_TESTNET,
    symbol="BTCUSDT",
    base_asset="BTC",
    quote_asset="USDT",
    tick_size=Decimal("0.01"),
    lot_step=Decimal("0.00001"),
    min_notional_quote=Decimal("10.00"),
)

GRID_START: Final = datetime(2026, 1, 1, tzinfo=UTC)

# A fixed namespace so every id in the suite is derived rather than drawn. A uuid4 here
# would make a Hypothesis shrink report a different id on every run.
_ID_NAMESPACE: Final = UUID("11111111-2222-3333-4444-555555555555")


def stable_id(label: str) -> UUID:
    """A deterministic UUID for a named test object."""
    return uuid5(_ID_NAMESPACE, label)


def make_fill(
    *,
    label: str,
    side: Side,
    base_quantity: Decimal,
    quote_price: Decimal,
    event_time_utc: datetime,
    fee_quote: Decimal = Decimal("0"),
    instrument: Instrument = BTCUSDT,
) -> Fill:
    """One execution, with every identifier derived from `label`."""
    return Fill(
        fill_id=stable_id(f"fill:{label}"),
        order_id=stable_id(f"order:{label}"),
        venue_trade_id=label,
        instrument=instrument,
        side=side,
        event_time_utc=event_time_utc,
        quote_price=quote_price,
        base_quantity=base_quantity,
        fee_quote=fee_quote,
    )


def grid_day(offset_days: int) -> datetime:
    """The `offset_days`-th midnight UTC boundary of the test grid."""
    return GRID_START + timedelta(days=offset_days)


def flat_marks(quote_price: Decimal) -> Mapping[Instrument, Decimal]:
    """A mark map covering the one instrument these tests trade."""
    return {BTCUSDT: quote_price}


def daily_mark(offset_days: int, quote_price: Decimal, regime: str = "calm") -> DailyMark:
    return DailyMark(
        as_of_utc=grid_day(offset_days),
        mark_quote_prices=flat_marks(quote_price),
        regime=regime,
    )


def path_from_returns(
    return_fractions: Sequence[Decimal],
    *,
    starting_equity_usd: Decimal = Decimal("100000"),
    is_in_market: bool = True,
    regimes: Sequence[str] | None = None,
) -> EquityPath:
    """An equity path whose daily returns are exactly `return_fractions`.

    Built directly rather than through the accounting so a statistic can be exercised
    against a return series chosen by the test. The accounting path is exercised
    separately, in `test_portfolio_accounting.py`.
    """
    equity = starting_equity_usd
    points = [
        EquityPoint(
            as_of_utc=grid_day(0),
            equity_usd=equity,
            is_in_market=is_in_market,
            regime="calm" if regimes is None else regimes[0],
        )
    ]
    for index, return_fraction in enumerate(return_fractions):
        equity = equity * (Decimal("1") + return_fraction)
        points.append(
            EquityPoint(
                as_of_utc=grid_day(index + 1),
                equity_usd=equity,
                is_in_market=is_in_market,
                regime="calm" if regimes is None else regimes[index],
            )
        )
    return EquityPath(points=tuple(points))


def opening_state(starting_cash_usd: Decimal = Decimal("100000")) -> PortfolioState:
    return PortfolioState.opening(as_of_utc=grid_day(0), starting_cash_usd=starting_cash_usd)


def state_with_fill_count(fill_count: int) -> PortfolioState:
    """A state carrying `fill_count` distinct applied fills and nothing else.

    Used to show that the trade count moves through the report untouched by any
    statistic: the same path with 3 fills and with 30 must produce the same ratios.
    """
    return PortfolioState(
        as_of_utc=grid_day(0),
        quote_cash_usd=Decimal("100000"),
        positions={},
        applied_fill_ids=frozenset(stable_id(f"synthetic:{index}") for index in range(fill_count)),
        applied_funding_keys=frozenset(),
        funding_paid_usd=Decimal("0"),
        risk_limit_breach_count=0,
    )
