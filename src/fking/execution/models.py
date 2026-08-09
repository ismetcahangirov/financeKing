"""Typed views of hostile input.

Every model here is `extra="ignore"` and `frozen=True`. The asymmetry with the models
this project *authors*, which are `extra="forbid"`, is deliberate rather than an
inconsistency: Binance adds response fields without notice, and breaking on a new one is
a self-inflicted outage, whereas an unexpected field in something we wrote is a sign the
schema was not respected.

`VenueDecimal` refuses anything that is not a string. That refusal is the whole point --
it is not defensiveness about types, it is the assertion that the value reached this
model through `parse_venue_payload` and therefore carries the venue's own characters. A
float arriving here means somebody read `ccxt`'s parsed structure instead of the response
body, and the precision was already lost one frame earlier.

Timestamps arrive as integer milliseconds and are named `_ms` for exactly as long as it
takes to reach `venue_epoch_to_utc`. That unit is the *request/response* convention and
is unrelated to the archive split in ADR-0013, where spot moved to microseconds in 2025
and futures did not -- conflating the two produces a value three orders of magnitude
wrong that looks like clock drift.
"""

from __future__ import annotations

from collections.abc import Sequence
from datetime import UTC, datetime, timedelta
from decimal import Decimal, InvalidOperation
from typing import Annotated, Final, Self

from pydantic import AliasChoices, BaseModel, BeforeValidator, ConfigDict, Field, model_validator

from fking.execution._errors import PermanentExchangeError

__all__ = [
    "SymbolFilters",
    "VenueBalance",
    "VenueDecimal",
    "VenueExchangeInfo",
    "VenueOrder",
    "VenuePosition",
    "VenueSymbol",
    "VenueSymbolFilter",
    "VenueTrade",
    "venue_epoch_to_utc",
]

_EPOCH_UTC: Final[datetime] = datetime(1970, 1, 1, tzinfo=UTC)

# The same magnitude guard the archive loader uses, for the same reason: the mapping
# from a venue's epoch to a unit will eventually be wrong again, and a range check turns
# a silent 1000x error into a loud one at the boundary rather than a bar in 1970.
_PLAUSIBLE_RANGE_UTC: Final[tuple[datetime, datetime]] = (
    datetime(2015, 1, 1, tzinfo=UTC),
    datetime(2100, 1, 1, tzinfo=UTC),
)

_FROZEN_VENUE_MODEL: Final[ConfigDict] = ConfigDict(
    frozen=True, extra="ignore", populate_by_name=True
)


def _decimal_from_venue(candidate: object) -> Decimal:
    """Accept only a string-encoded decimal.

    A JSON number here has already been through the float parser, so accepting it would
    launder a rounding error into a type that looks exact.
    """
    if not isinstance(candidate, str):
        # ValueError rather than the TypeError TRY004 prefers: pydantic converts a
        # ValueError raised inside a validator into a ValidationError naming the field,
        # and lets a TypeError propagate raw -- which would surface as an unhandled
        # exception with no field in the message.
        raise ValueError(  # noqa: TRY004
            f"expected a string-encoded decimal from the venue, got "
            f"{type(candidate).__name__}; the body was not parsed with "
            f"parse_float=Decimal, or ccxt's parsed structure was read instead"
        )
    try:
        return Decimal(candidate)
    except InvalidOperation as malformed:
        raise ValueError(f"venue sent {candidate!r}, which is not a decimal") from malformed


VenueDecimal = Annotated[Decimal, BeforeValidator(_decimal_from_venue)]


def venue_epoch_to_utc(epoch_ms: int) -> datetime:
    """Convert a venue millisecond epoch to an aware UTC datetime, exactly.

    `timedelta` rather than `fromtimestamp(ms / 1000)`: the division would introduce a
    binary float on a value the venue stated exactly, and a millisecond that lands on
    the wrong side of a bar boundary is a reconciliation mismatch nobody can explain.
    """
    try:
        moment = _EPOCH_UTC + timedelta(milliseconds=epoch_ms)
    except OverflowError as beyond_datetime:
        # A microsecond epoch read as milliseconds lands past year 9999 and `timedelta`
        # refuses before the range check can. Converting it here keeps every unit error
        # arriving as one failure with one message, rather than as an OverflowError from
        # the standard library that names no venue and no field.
        raise PermanentExchangeError(
            f"venue epoch {epoch_ms} is not representable as a datetime; the timestamp "
            f"unit assumption is wrong",
            venue_id="unknown",
            http_status=None,
            venue_code=None,
        ) from beyond_datetime
    low, high = _PLAUSIBLE_RANGE_UTC
    if not low <= moment < high:
        raise PermanentExchangeError(
            f"venue epoch {epoch_ms} normalises to {moment.isoformat()}, outside the "
            f"plausible range; the timestamp unit assumption is wrong",
            venue_id="unknown",
            http_status=None,
            venue_code=None,
        )
    return moment


class VenueSymbolFilter(BaseModel):
    """One entry of a symbol's `filters` array.

    One model for every filter type rather than a discriminated union, because the
    fields this system acts on -- tick, step, notional -- appear across several types
    and every other field is dropped by `extra="ignore"`. A union would require naming
    all eleven spot filter types and would break the day Binance adds a twelfth.
    """

    model_config = _FROZEN_VENUE_MODEL

    filter_type: str = Field(alias="filterType")
    tick_size: VenueDecimal | None = Field(default=None, alias="tickSize")
    step_size: VenueDecimal | None = Field(default=None, alias="stepSize")
    min_quantity: VenueDecimal | None = Field(default=None, alias="minQty")
    max_quantity: VenueDecimal | None = Field(default=None, alias="maxQty")
    # Spot spells it NOTIONAL/minNotional, futures spells it MIN_NOTIONAL/notional.
    # Verified against both testnet exchangeInfo payloads on 2026-08-05.
    min_notional: VenueDecimal | None = Field(
        default=None, validation_alias=AliasChoices("minNotional", "notional")
    )


class SymbolFilters(BaseModel):
    """The filters an order must satisfy, extracted from a symbol's filter array.

    Missing filters are fatal rather than defaulted. A default tick size is a number
    nobody chose being used to round a price the venue will reject, and the rejection
    arrives in the order path rather than at startup where it belongs.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    tick_size: Decimal = Field(gt=0)
    step_size: Decimal = Field(gt=0)
    min_quantity: Decimal = Field(ge=0)
    max_quantity: Decimal = Field(gt=0)
    min_notional: Decimal | None = Field(default=None, ge=0)

    @classmethod
    def from_entries(cls, entries: Sequence[VenueSymbolFilter], *, symbol: str) -> Self:
        by_type = {entry.filter_type: entry for entry in entries}
        price_filter = by_type.get("PRICE_FILTER")
        lot_size = by_type.get("LOT_SIZE")
        notional = by_type.get("NOTIONAL") or by_type.get("MIN_NOTIONAL")
        if price_filter is None or price_filter.tick_size is None:
            raise PermanentExchangeError(
                f"{symbol} exchangeInfo carries no usable PRICE_FILTER",
                venue_id="unknown",
                http_status=None,
                venue_code=None,
            )
        if lot_size is None or lot_size.step_size is None or lot_size.max_quantity is None:
            raise PermanentExchangeError(
                f"{symbol} exchangeInfo carries no usable LOT_SIZE",
                venue_id="unknown",
                http_status=None,
                venue_code=None,
            )
        return cls(
            tick_size=price_filter.tick_size,
            step_size=lot_size.step_size,
            min_quantity=lot_size.min_quantity if lot_size.min_quantity is not None else Decimal(0),
            max_quantity=lot_size.max_quantity,
            min_notional=notional.min_notional if notional is not None else None,
        )


class VenueSymbol(BaseModel):
    """One entry of `exchangeInfo.symbols`, with its code points untouched."""

    model_config = _FROZEN_VENUE_MODEL

    symbol: str
    status: str
    base_asset: str = Field(alias="baseAsset")
    quote_asset: str = Field(alias="quoteAsset")
    filters: tuple[VenueSymbolFilter, ...] = ()

    @property
    def is_trading(self) -> bool:
        return self.status == "TRADING"

    def order_filters(self) -> SymbolFilters:
        return SymbolFilters.from_entries(self.filters, symbol=self.symbol)


class VenueExchangeInfo(BaseModel):
    """`GET /api/v3/exchangeInfo`, or its futures equivalent."""

    model_config = _FROZEN_VENUE_MODEL

    server_time_ms: int = Field(alias="serverTime")
    symbols: tuple[VenueSymbol, ...]

    @property
    def server_time_utc(self) -> datetime:
        return venue_epoch_to_utc(self.server_time_ms)

    def symbol(self, name: str) -> VenueSymbol:
        for entry in self.symbols:
            if entry.symbol == name:
                return entry
        raise PermanentExchangeError(
            f"exchangeInfo lists no symbol {name!r}",
            venue_id="unknown",
            http_status=None,
            venue_code=None,
        )


class VenueOrder(BaseModel):
    """An order as the venue describes it -- an ack, an open order, or a cancellation.

    One model for all three because Binance returns the same shape from `POST`,
    `DELETE` and `GET /openOrders`, differing only in which optional fields are
    populated. Splitting them would mean three models that must be kept in step with one
    upstream payload.
    """

    model_config = _FROZEN_VENUE_MODEL

    venue_order_id: int = Field(alias="orderId")
    client_order_id: str = Field(alias="clientOrderId")
    symbol: str
    status: str
    side: str
    order_type: str = Field(alias="type")
    quote_price: VenueDecimal = Field(alias="price")
    original_base_quantity: VenueDecimal = Field(alias="origQty")
    executed_base_quantity: VenueDecimal = Field(alias="executedQty")
    # Spot: cummulativeQuoteQty (the venue's own misspelling). Futures: cumQuote.
    executed_quote_amount: VenueDecimal | None = Field(
        default=None,
        validation_alias=AliasChoices("cummulativeQuoteQty", "cumQuote"),
    )
    # Spot new orders carry transactTime; open orders and futures carry updateTime.
    event_time_ms: int | None = Field(
        default=None,
        validation_alias=AliasChoices("transactTime", "updateTime", "time"),
    )

    @property
    def event_time_utc(self) -> datetime | None:
        return None if self.event_time_ms is None else venue_epoch_to_utc(self.event_time_ms)


class VenueTrade(BaseModel):
    """One fill from `GET /api/v3/myTrades`."""

    model_config = _FROZEN_VENUE_MODEL

    venue_trade_id: int = Field(validation_alias=AliasChoices("id", "tradeId"))
    venue_order_id: int = Field(alias="orderId")
    symbol: str
    fill_price: VenueDecimal = Field(alias="price")
    base_quantity: VenueDecimal = Field(alias="qty")
    fee_quote_amount: VenueDecimal = Field(validation_alias=AliasChoices("commission"))
    fee_asset: str = Field(alias="commissionAsset")
    event_time_ms: int = Field(validation_alias=AliasChoices("time"))
    is_buyer: bool = Field(alias="isBuyer")
    is_maker: bool = Field(alias="isMaker")

    @property
    def event_time_utc(self) -> datetime:
        return venue_epoch_to_utc(self.event_time_ms)


class VenueBalance(BaseModel):
    """One asset balance.

    `free` and `locked` rather than a single total: the difference between them is the
    difference between an order the venue accepts and a `-2010 insufficient balance`,
    and a model that reported only the sum would make that distinction unrepresentable.
    """

    model_config = _FROZEN_VENUE_MODEL

    asset: str
    free_amount: VenueDecimal = Field(validation_alias=AliasChoices("free", "availableBalance"))
    locked_amount: VenueDecimal | None = Field(default=None, alias="locked")


class VenuePosition(BaseModel):
    """One futures position. Spot has none, and that is a fact about the venue."""

    model_config = _FROZEN_VENUE_MODEL

    symbol: str
    # Signed: negative is short. Binance spells it positionAmt, which is one of the
    # names docs/rules/naming.md exists to translate away at the adapter boundary.
    signed_base_quantity: VenueDecimal = Field(alias="positionAmt")
    entry_quote_price: VenueDecimal = Field(alias="entryPrice")
    unrealised_pnl_quote: VenueDecimal | None = Field(default=None, alias="unRealizedProfit")

    @model_validator(mode="after")
    def _entry_price_is_present_when_a_position_is_open(self) -> Self:
        if self.signed_base_quantity != 0 and self.entry_quote_price <= 0:
            raise ValueError(
                f"{self.symbol} reports a position of {self.signed_base_quantity} with "
                f"entry price {self.entry_quote_price}; a position with no cost basis "
                f"cannot be marked and would report unbounded PnL"
            )
        return self
