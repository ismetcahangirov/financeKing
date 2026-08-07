"""Builders for the venue-simulator suites, and the recorded filters they load.

No tests of its own. The filters come from `tests/fixtures/recorded/` -- the same
`exchangeInfo` body a real testnet returned -- because a hand-written
`min_notional_quote` encodes what its author believes the venue enforces, and the number
in the recording is `5.00000000` (`.claude/rules/testing-rules.md`).

The body is read with `json.loads(..., parse_float=Decimal)` so that nothing in the
payload can reach a `Decimal` through a binary double, even though every filter Binance
publishes is already a string.
"""

from __future__ import annotations

import json
from collections.abc import Mapping
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from typing import Final
from uuid import UUID

import yaml

from fking.backtest.costs import CostModel, SpreadQuantile
from fking.backtest.venue import (
    BacktestVenue,
    SymbolFilters,
    parse_order_rate_budget,
    parse_symbol_filters,
)
from fking.domain import Bar, Instrument, Order, OrderType, Side, TimeInForce, Venue
from tests.backtest.test_cost_fixtures import cost_model, depth_profile, flat_spread_profile

RECORDED_ROOT: Final[Path] = Path(__file__).resolve().parents[1] / "fixtures" / "recorded"

SYMBOL: Final = "BTCUSDT"
EPOCH: Final = datetime(2026, 8, 1, 12, 0, tzinfo=UTC)

# The venue's own lattice, taken from the recorded payload rather than restated here:
# tick 0.01, step 0.00001, notional floor 5.00.
BTCUSDT: Final = Instrument(
    venue=Venue.BINANCE_SPOT_TESTNET,
    symbol=SYMBOL,
    base_asset="BTC",
    quote_asset="USDT",
    tick_size=Decimal("0.01"),
    lot_step=Decimal("0.00001"),
    min_notional_quote=Decimal("5.00"),
)


def _recorded_exchange_info() -> Mapping[str, object]:
    directory = RECORDED_ROOT / "binance-spot-testnet" / "exchangeInfo"
    path = max(directory.glob("*.yaml"))
    document = yaml.safe_load(path.read_text(encoding="utf-8"))
    payload = json.loads(document["body"], parse_float=Decimal)
    if not isinstance(payload, dict):  # pragma: no cover - the recording is an object
        raise TypeError(f"{path} does not hold a JSON object")
    return payload


def recorded_filters(symbol: str = SYMBOL) -> SymbolFilters:
    """The filters the venue published for `symbol`, parsed from the recording."""
    symbols = _recorded_exchange_info()["symbols"]
    if not isinstance(symbols, list):  # pragma: no cover - the recording is an array
        raise TypeError("the recorded exchangeInfo body has no symbols array")
    for entry in symbols:
        if isinstance(entry, dict) and entry.get("symbol") == symbol:
            return parse_symbol_filters(entry)
    raise KeyError(f"{symbol} is not in the recorded exchangeInfo")


def recorded_order_rate_budget() -> tuple[int, timedelta]:
    """The narrowest ORDERS budget the venue published: 50 per 10 seconds on spot testnet."""
    return parse_order_rate_budget(_recorded_exchange_info())


def make_venue(  # noqa: PLR0913 - one keyword per varying field of the fixture;
    # a builder object would be a second thing to keep in step with the venue.
    *,
    model: CostModel | None = None,
    spread_bps: Decimal = Decimal("2.00"),
    touch_base: Decimal = Decimal("2"),
    band_base: Decimal = Decimal("10"),
    quantile: SpreadQuantile = SpreadQuantile.P50,
    order_rate_budget: int | None = None,
) -> BacktestVenue:
    """A venue carrying the recorded filters and a production-provenance cost model."""
    budget, window = recorded_order_rate_budget()
    resolved = (
        model
        if model is not None
        else cost_model(
            spreads={SYMBOL: flat_spread_profile(spread_bps)},
            depth={SYMBOL: depth_profile(touch_base=touch_base, band_base=band_base)},
        )
    )
    return BacktestVenue(
        cost_model=resolved,
        filters={SYMBOL: recorded_filters()},
        order_rate_budget=order_rate_budget if order_rate_budget is not None else budget,
        order_rate_window=window,
        quantile=quantile,
    )


def make_bar(  # noqa: PLR0913 - one keyword per varying field of the fixture;
    # a builder object would be a second thing to keep in step with the venue.
    *,
    open_time_utc: datetime = EPOCH,
    open_quote_price: str = "64000.00",
    high_quote_price: str = "64500.00",
    low_quote_price: str = "63800.00",
    close_quote_price: str = "64200.00",
    base_volume: str = "12.5",
    instrument: Instrument = BTCUSDT,
) -> Bar:
    return Bar(
        instrument=instrument,
        open_time_utc=open_time_utc,
        close_time_utc=open_time_utc + timedelta(minutes=1),
        open_quote_price=Decimal(open_quote_price),
        high_quote_price=Decimal(high_quote_price),
        low_quote_price=Decimal(low_quote_price),
        close_quote_price=Decimal(close_quote_price),
        base_volume=Decimal(base_volume),
        trade_count=4210,
    )


def make_order(  # noqa: PLR0913 - one keyword per varying field of the fixture;
    # a builder object would be a second thing to keep in step with the venue.
    *,
    ordinal: int = 1,
    side: Side = Side.BUY,
    order_type: OrderType = OrderType.LIMIT,
    limit_quote_price: str | None = "64000.00",
    base_quantity: str = "0.01",
    time_in_force: TimeInForce = TimeInForce.GTC,
    created_at_utc: datetime = EPOCH,
    instrument: Instrument = BTCUSDT,
) -> Order:
    """An order with an id derived from `ordinal`, so a suite can hold several at once."""
    return Order(
        order_id=UUID(int=ordinal),
        client_order_id=f"fk-{ordinal:08x}",
        correlation_id=UUID(int=1000 + ordinal),
        instrument=instrument,
        side=side,
        order_type=order_type,
        time_in_force=time_in_force,
        base_quantity=Decimal(base_quantity),
        limit_quote_price=None if limit_quote_price is None else Decimal(limit_quote_price),
        created_at_utc=created_at_utc,
    )
