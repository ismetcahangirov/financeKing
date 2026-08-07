"""Venue filters, parsed from a real `exchangeInfo` payload and applied to an order.

The filters are read from the venue rather than declared here, and that is the whole
design. A hand-written `min_notional_quote = Decimal("10")` encodes what somebody
believed Binance enforces; the recorded payload says `5.00000000`, and the gap between
the two is a band of order sizes that the backtest refuses and the venue accepts -- or
worse, the reverse, which is a backtest that fills orders the venue would have thrown
away and reports the resulting trades as edge.

Parsing is deliberately hostile. Every filter this module needs must be present and must
carry its numbers as strings; a missing `NOTIONAL` block raises rather than defaulting to
zero, because a floor of zero disables the filter silently and the run still completes.
Binance serialises these as decimal strings, so a non-string is a contract change worth
stopping for -- and constructing a `Decimal` from a JSON number that a parser has already
turned into a double is the money failure in `.claude/rules/decimal-and-money.md` arriving
through the one door left open.

Check order is fixed: price lattice, price band, lot lattice, notional floor. It matters
because an order can breach several at once and only one rejection is reported, so an
unspecified order would make the reported reason depend on dict iteration.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import timedelta
from decimal import Decimal, InvalidOperation
from typing import Final

from fking.backtest.venue._errors import VenueSimulationError
from fking.backtest.venue._rejections import Rejection, RejectReason
from fking.domain import Order, OrderType, Side

_ZERO: Final = Decimal("0")

# The rate-limit block Binance publishes for new orders. Both testnets report an ORDERS
# limit over a 10-second window; the numbers themselves are read from the payload
# because spot testnet allows 50 per 10s against production's 100 and a constant here
# would be wrong on one of them.
_ORDER_RATE_TYPE: Final = "ORDERS"
_INTERVAL_SECONDS: Final[Mapping[str, int]] = {"SECOND": 1, "MINUTE": 60, "DAY": 86_400}


def _entry_for(filters: Sequence[object], filter_type: str, symbol: str) -> Mapping[str, object]:
    for candidate in filters:
        if isinstance(candidate, Mapping) and candidate.get("filterType") == filter_type:
            return candidate
    raise VenueSimulationError(
        f"{symbol} exchangeInfo carries no {filter_type} filter; the simulator cannot "
        f"decide which orders the venue would refuse, and defaulting the bound would "
        f"disable the check without saying so"
    )


def _decimal_field(entry: Mapping[str, object], key: str, symbol: str) -> Decimal:
    candidate = entry.get(key)
    if not isinstance(candidate, str):
        raise VenueSimulationError(
            f"{symbol} {entry.get('filterType')}.{key} must be a string-encoded decimal, "
            f"got {type(candidate).__name__} {candidate!r}; a JSON number has already "
            f"been rounded by the parser before this code ran"
        )
    try:
        parsed = Decimal(candidate)
    except InvalidOperation as invalid:
        raise VenueSimulationError(
            f"{symbol} {entry.get('filterType')}.{key} is not a decimal: {candidate!r}"
        ) from invalid
    if not parsed.is_finite():
        raise VenueSimulationError(f"{symbol} {entry.get('filterType')}.{key} is not finite")
    return parsed


@dataclass(frozen=True, slots=True)
class SymbolFilters:
    """What one symbol's venue will accept, as the venue itself reported it.

    `avg_price_reference` is not stored: `PERCENT_PRICE_BY_SIDE` is evaluated against a
    five-minute average price that the venue computes, and the simulator supplies its own
    reference at screening time from the last closed bar. Storing a reference on the
    filters would freeze a price into a value object that outlives it.
    """

    symbol: str
    tick_size: Decimal
    min_quote_price: Decimal
    max_quote_price: Decimal
    lot_step: Decimal
    min_base_quantity: Decimal
    max_base_quantity: Decimal
    min_notional_quote: Decimal
    bid_multiplier_up: Decimal
    bid_multiplier_down: Decimal
    ask_multiplier_up: Decimal
    ask_multiplier_down: Decimal

    def price_band_for(self, side: Side) -> tuple[Decimal, Decimal]:
        """The `(low, high)` prices this side may quote against a reference of 1.

        Split by side because Binance splits it by side: a bid and an ask at the same
        distance from the mid are not equally suspicious to the venue, and collapsing the
        two pairs into one would let a sell through a band that rejects it.
        """
        if side is Side.BUY:
            return self.bid_multiplier_down, self.bid_multiplier_up
        return self.ask_multiplier_down, self.ask_multiplier_up


def parse_symbol_filters(payload: Mapping[str, object]) -> SymbolFilters:
    """Read one `exchangeInfo` symbol entry into the filters the simulator enforces.

    The symbol string is carried through unchanged, code point for code point. Binance
    spot testnet serves deliberately non-ASCII symbols and normalising one here would
    make the filters unfindable under the name every other module knows it by.
    """
    symbol = payload.get("symbol")
    if not isinstance(symbol, str) or not symbol:
        raise VenueSimulationError(
            f"exchangeInfo symbol entry carries no usable symbol: {symbol!r}"
        )
    filters = payload.get("filters")
    if not isinstance(filters, Sequence) or isinstance(filters, str):
        raise VenueSimulationError(f"{symbol} exchangeInfo entry carries no filters array")

    price_filter = _entry_for(filters, "PRICE_FILTER", symbol)
    lot = _entry_for(filters, "LOT_SIZE", symbol)
    notional = _entry_for(filters, "NOTIONAL", symbol)
    band = _entry_for(filters, "PERCENT_PRICE_BY_SIDE", symbol)

    return SymbolFilters(
        symbol=symbol,
        tick_size=_decimal_field(price_filter, "tickSize", symbol),
        min_quote_price=_decimal_field(price_filter, "minPrice", symbol),
        max_quote_price=_decimal_field(price_filter, "maxPrice", symbol),
        lot_step=_decimal_field(lot, "stepSize", symbol),
        min_base_quantity=_decimal_field(lot, "minQty", symbol),
        max_base_quantity=_decimal_field(lot, "maxQty", symbol),
        min_notional_quote=_decimal_field(notional, "minNotional", symbol),
        bid_multiplier_up=_decimal_field(band, "bidMultiplierUp", symbol),
        bid_multiplier_down=_decimal_field(band, "bidMultiplierDown", symbol),
        ask_multiplier_up=_decimal_field(band, "askMultiplierUp", symbol),
        ask_multiplier_down=_decimal_field(band, "askMultiplierDown", symbol),
    )


def parse_order_rate_budget(payload: Mapping[str, object]) -> tuple[int, timedelta]:
    """The venue's own new-order budget, as `(max_orders, window)`.

    The narrowest ORDERS window wins. Binance publishes several -- 50 per 10 seconds and
    160 000 per day on spot testnet -- and a simulator that enforced the daily one would
    never reject anything a backtest does, while the 10-second one is the budget a
    strategy that reprices on every bar actually runs into.
    """
    limits = payload.get("rateLimits")
    if not isinstance(limits, Sequence) or isinstance(limits, str):
        raise VenueSimulationError("exchangeInfo payload carries no rateLimits array")

    narrowest: tuple[int, timedelta] | None = None
    for candidate in limits:
        if not isinstance(candidate, Mapping) or candidate.get("rateLimitType") != _ORDER_RATE_TYPE:
            continue
        interval = candidate.get("interval")
        interval_num = candidate.get("intervalNum")
        max_orders = candidate.get("limit")
        if (
            not isinstance(interval, str)
            or interval not in _INTERVAL_SECONDS
            or not isinstance(interval_num, int)
            or not isinstance(max_orders, int)
        ):
            raise VenueSimulationError(f"unusable ORDERS rate limit entry {candidate!r}")
        window = timedelta(seconds=_INTERVAL_SECONDS[interval] * interval_num)
        if narrowest is None or window < narrowest[1]:
            narrowest = (max_orders, window)

    if narrowest is None:
        raise VenueSimulationError(
            "exchangeInfo publishes no ORDERS rate limit; the simulator would then model "
            "an unlimited order rate, which is the one assumption a rate-limited venue "
            "never justifies"
        )
    return narrowest


def _screen_quote_price(
    filters: SymbolFilters, order: Order, quoted: Decimal, reference_quote_price: Decimal
) -> Rejection | None:
    """PRICE_FILTER then PERCENT_PRICE_BY_SIDE, in that order."""
    if quoted < filters.min_quote_price or quoted > filters.max_quote_price:
        return Rejection(
            RejectReason.PRICE_FILTER,
            f"{quoted} outside [{filters.min_quote_price}, {filters.max_quote_price}]",
        )
    if quoted % filters.tick_size != _ZERO:
        return Rejection(
            RejectReason.PRICE_FILTER,
            f"{quoted} is not a multiple of tick size {filters.tick_size}",
        )
    low_multiplier, high_multiplier = filters.price_band_for(order.side)
    band_low = reference_quote_price * low_multiplier
    band_high = reference_quote_price * high_multiplier
    if quoted < band_low or quoted > band_high:
        return Rejection(
            RejectReason.PERCENT_PRICE_BY_SIDE,
            f"{order.side.value} at {quoted} outside [{band_low}, {band_high}] "
            f"against reference {reference_quote_price}",
        )
    return None


def _screen_base_quantity(filters: SymbolFilters, base_quantity: Decimal) -> Rejection | None:
    """LOT_SIZE: the bounds first, then the lattice."""
    if base_quantity < filters.min_base_quantity or base_quantity > filters.max_base_quantity:
        return Rejection(
            RejectReason.LOT_SIZE,
            f"{base_quantity} outside [{filters.min_base_quantity}, {filters.max_base_quantity}]",
        )
    if base_quantity % filters.lot_step != _ZERO:
        return Rejection(
            RejectReason.LOT_SIZE,
            f"{base_quantity} is not a multiple of step size {filters.lot_step}",
        )
    return None


def screen_order(
    filters: SymbolFilters, order: Order, reference_quote_price: Decimal
) -> Rejection | None:
    """The venue's answer to an order it has just received, or `None` if it accepts.

    `reference_quote_price` stands in for Binance's five-minute average price, and the
    caller supplies it from data that had already closed. Passing the price of the bar
    the order is about to trade into would let the band check consult the future, which
    is a look-ahead channel wearing a compliance check's clothes.
    """
    quoted = order.limit_quote_price
    if quoted is not None:
        priced = _screen_quote_price(filters, order, quoted, reference_quote_price)
        if priced is not None:
            return priced

    lotted = _screen_base_quantity(filters, order.base_quantity)
    if lotted is not None:
        return lotted

    # A market order has no price of its own, so the floor is checked against the same
    # reference the venue uses -- `applyMinToMarket` is true on every symbol in the
    # recorded payload, so a market order is not exempt.
    is_priced_limit = order.order_type is OrderType.LIMIT and quoted is not None
    notional_reference = quoted if is_priced_limit and quoted is not None else reference_quote_price
    notional_quote = order.base_quantity * notional_reference
    if notional_quote < filters.min_notional_quote:
        return Rejection(
            RejectReason.MIN_NOTIONAL,
            f"notional {notional_quote} below floor {filters.min_notional_quote}",
        )
    return None
