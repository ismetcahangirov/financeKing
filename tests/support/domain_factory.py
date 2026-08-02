"""Builders and Hypothesis strategies for domain objects.

The instruments here carry the real filters from Binance testnet `exchangeInfo` rather
than round numbers. `lot_step` of `0.00001` and a `tick_size` of `0.01` are what
produce the dust and off-lattice quantities the position properties are hunting for;
a lot step of `1` makes every generated quantity trivially exact and the properties
pass without testing anything.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import ROUND_HALF_EVEN, Decimal
from typing import Final
from uuid import UUID

from hypothesis import strategies as st

from fking.domain import (
    Bar,
    Direction,
    Fill,
    Instrument,
    Order,
    OrderType,
    Side,
    Signal,
    Tick,
    TimeInForce,
    Venue,
)

BTCUSDT: Final = Instrument(
    venue=Venue.BINANCE_SPOT_TESTNET,
    symbol="BTCUSDT",
    base_asset="BTC",
    quote_asset="USDT",
    tick_size=Decimal("0.01"),
    lot_step=Decimal("0.00001"),
    min_notional_quote=Decimal("10.00"),
)
ETHUSDT: Final = Instrument(
    venue=Venue.BINANCE_SPOT_TESTNET,
    symbol="ETHUSDT",
    base_asset="ETH",
    quote_asset="USDT",
    tick_size=Decimal("0.01"),
    lot_step=Decimal("0.0001"),
    min_notional_quote=Decimal("10.00"),
)

EPOCH: Final = datetime(2026, 8, 1, 12, 0, tzinfo=UTC)

# Naive on purpose, and the only naive datetimes in the repository: Hypothesis's
# `st.datetimes` rejects aware bounds when `timezones` is supplied, because the bounds
# describe the naive local instant it then localises. The values it draws are aware
# UTC, which is what the domain constructors demand.
_MIN_EVENT_TIME: Final = datetime(2020, 1, 1)  # noqa: DTZ001
_MAX_EVENT_TIME: Final = datetime(2030, 1, 1)  # noqa: DTZ001


def stepped(step: Decimal, *, max_value: str) -> st.SearchStrategy[Decimal]:
    """Positive decimals that are exact multiples of an exchange step.

    `places` alone is not enough: Binance step sizes are not always powers of ten
    (`0.005` occurs), so the drawn value is snapped onto the step lattice. A value off
    the lattice is rejected by the venue with `-1013`, which would make the position
    properties assert against inputs the exchange could never have produced.
    """
    exponent = step.as_tuple().exponent
    # A finite Decimal always has an int exponent; the string forms are 'n', 'N' and
    # 'F', which are NaN and infinity -- and a step size that is one of those is a
    # bug in the caller, not an input worth generating around.
    assert isinstance(exponent, int), f"step {step} is not finite"
    places = -exponent
    return (
        st.decimals(
            min_value=Decimal("0"),
            max_value=Decimal(max_value),
            places=places,
            allow_nan=False,
            allow_infinity=False,
        )
        .map(lambda drawn: (drawn / step).quantize(Decimal("1"), rounding=ROUND_HALF_EVEN) * step)
        .filter(lambda snapped: snapped > 0)
    )


def utc_datetimes() -> st.SearchStrategy[datetime]:
    return st.datetimes(
        min_value=_MIN_EVENT_TIME, max_value=_MAX_EVENT_TIME, timezones=st.just(UTC)
    )


def fills(instrument: Instrument = BTCUSDT) -> st.SearchStrategy[Fill]:
    return st.builds(
        Fill,
        fill_id=st.uuids(),
        order_id=st.uuids(),
        venue_trade_id=st.integers(min_value=1).map(str),
        instrument=st.just(instrument),
        side=st.sampled_from(Side),
        event_time_utc=utc_datetimes(),
        quote_price=stepped(instrument.tick_size, max_value="150000"),
        base_quantity=stepped(instrument.lot_step, max_value="5"),
        fee_quote=stepped(instrument.tick_size, max_value="10") | st.just(Decimal("0")),
    )


_ID_SEQUENCE: list[int] = [0]


def _next_id() -> int:
    """A fresh integer per call, so example-based fills are distinct without ceremony."""
    _ID_SEQUENCE[0] += 1
    return _ID_SEQUENCE[0]


def make_fill(
    *,
    side: Side,
    quote_price: str,
    base_quantity: str,
    instrument: Instrument = BTCUSDT,
    fee_quote: str = "0",
    fill_id: UUID | None = None,
    event_time_utc: datetime | None = None,
) -> Fill:
    """A fill with readable literals, for the example-based tests."""
    return Fill(
        fill_id=fill_id if fill_id is not None else UUID(int=_next_id()),
        order_id=UUID(int=1),
        venue_trade_id="t-1",
        instrument=instrument,
        side=side,
        event_time_utc=event_time_utc if event_time_utc is not None else EPOCH,
        quote_price=Decimal(quote_price),
        base_quantity=Decimal(base_quantity),
        fee_quote=Decimal(fee_quote),
    )


def make_bar(
    *,
    instrument: Instrument = BTCUSDT,
    open_quote_price: str = "64000.00",
    high_quote_price: str = "64500.00",
    low_quote_price: str = "63800.00",
    close_quote_price: str = "64200.00",
    base_volume: str = "12.5",
    trade_count: int = 4210,
) -> Bar:
    return Bar(
        instrument=instrument,
        open_time_utc=EPOCH,
        close_time_utc=EPOCH + timedelta(minutes=1),
        open_quote_price=Decimal(open_quote_price),
        high_quote_price=Decimal(high_quote_price),
        low_quote_price=Decimal(low_quote_price),
        close_quote_price=Decimal(close_quote_price),
        base_volume=Decimal(base_volume),
        trade_count=trade_count,
    )


def make_tick(instrument: Instrument = BTCUSDT) -> Tick:
    return Tick(
        instrument=instrument,
        venue_trade_id="9124773",
        event_time_utc=EPOCH,
        quote_price=Decimal("64000.00"),
        base_quantity=Decimal("0.00050"),
        aggressor_side=Side.BUY,
    )


def make_signal(
    *,
    direction: Direction = Direction.LONG,
    invalidation_quote_price: str | None = "63000.00",
    instrument: Instrument = BTCUSDT,
    conviction: str = "0.6",
) -> Signal:
    return Signal(
        strategy_id="breakout-4h",
        instrument=instrument,
        direction=direction,
        conviction=Decimal(conviction),
        horizon=timedelta(hours=8),
        invalidation_quote_price=(
            None if invalidation_quote_price is None else Decimal(invalidation_quote_price)
        ),
        rationale="close above the 20-bar high",
        decided_at_utc=EPOCH,
    )


def make_order(
    *,
    order_type: OrderType = OrderType.LIMIT,
    limit_quote_price: str | None = "64000.00",
    side: Side = Side.BUY,
    base_quantity: str = "0.01",
    instrument: Instrument = BTCUSDT,
) -> Order:
    return Order(
        order_id=UUID(int=7),
        client_order_id="fk-3f1ac9",
        correlation_id=UUID(int=8),
        instrument=instrument,
        side=side,
        order_type=order_type,
        time_in_force=TimeInForce.GTC,
        base_quantity=Decimal(base_quantity),
        limit_quote_price=(None if limit_quote_price is None else Decimal(limit_quote_price)),
        created_at_utc=EPOCH,
    )
