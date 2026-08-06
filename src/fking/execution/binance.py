"""`ExecutionVenue` over Binance spot and USD-M futures testnet.

Every method is one line over `_call`, and that is the design rather than an accident:
`_call` is the only place a response body is turned into a Python object, so the
`json.loads(body, parse_float=Decimal)` boundary and the error-envelope check cannot be
skipped by adding a method. A method that parsed its own response would be one edit away
from `response["orderId"]`, which raises `KeyError` three frames from the rejection that
caused it.

The adapter never imports `ccxt`. It holds a `GuardedExchange`, whose `call` returns the
venue's raw response *text* -- which is what makes the `Decimal` path structural instead
of a rule people remember. An `import-linter` contract enforces the absence of the
import, because an adapter that could reach `ccxt` could construct an exchange with an
unguarded transport.

Spot and futures differ in more than their base URL, so the endpoint names, the shape
`fetch_balances` reads, and the cancel-replace mechanism are data on `_ENDPOINTS` rather
than branches in the methods. Where they differ *behaviourally* -- spot has no positions,
futures amends instead of cancel-replacing -- the difference is stated once, with its
reason.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from decimal import Decimal
from typing import Final

from fking.domain import Order, OrderType, Venue
from fking.execution._errors import PermanentExchangeError
from fking.execution.models import (
    VenueBalance,
    VenueExchangeInfo,
    VenueOrder,
    VenuePosition,
    VenueTrade,
)
from fking.execution.order_id import assert_client_order_id_acceptable
from fking.execution.parsing import parse_venue_payload, raise_for_venue_error
from fking.execution.venue_profile import VENUE_PROFILES, VenueProfile
from fking.platform.safety import GuardedExchange, guarded_ccxt

__all__ = ["BinanceVenue", "open_binance_venue"]


@dataclass(frozen=True, slots=True)
class _VenueEndpoints:
    """ccxt implicit-API method names for one Binance market.

    Data rather than a branch per method: the spot/futures split is the venue's, not
    this adapter's, and expressing it as a table means a third market is a new row.
    """

    exchange_info: str
    balances: str
    open_orders: str
    my_trades: str
    submit: str
    cancel: str
    # Spot cancels and places atomically; futures has no such endpoint and amends the
    # live order in place with `PUT /fapi/v1/order` instead. Two mechanisms with the
    # same intent and different response envelopes -- see `cancel_replace`.
    cancel_replace: str
    # `None` where the venue reports balances as a bare array. Spot wraps them in an
    # account object under this key.
    balances_key: str | None
    # `None` on spot, which has no positions. That is a fact about the venue, not a gap.
    positions: str | None


_ENDPOINTS: Final[Mapping[str, _VenueEndpoints]] = {
    "spot": _VenueEndpoints(
        exchange_info="publicGetExchangeInfo",
        balances="privateGetAccount",
        open_orders="privateGetOpenOrders",
        my_trades="privateGetMyTrades",
        submit="privatePostOrder",
        cancel="privateDeleteOrder",
        cancel_replace="privatePostOrderCancelReplace",
        balances_key="balances",
        positions=None,
    ),
    "futures": _VenueEndpoints(
        exchange_info="fapiPublicGetExchangeInfo",
        balances="fapiPrivateV3GetBalance",
        open_orders="fapiPrivateGetOpenOrders",
        my_trades="fapiPrivateGetUserTrades",
        submit="fapiPrivatePostOrder",
        cancel="fapiPrivateDeleteOrder",
        cancel_replace="fapiPrivatePutOrder",
        balances_key=None,
        positions="fapiPrivateV3GetPositionRisk",
    ),
}

_BINANCE_MARKETS: Final[frozenset[Venue]] = frozenset(
    {Venue.BINANCE_SPOT_TESTNET, Venue.BINANCE_FUTURES_TESTNET}
)


def _wire_decimal(quantity: Decimal) -> str:
    """Format a decimal for the wire without scientific notation.

    `str(Decimal("0.00001"))` is `'0.00001'`, but `str(Decimal("1E-8"))` is `'1E-8'`,
    which Binance rejects as a malformed quantity. `format(..., "f")` is the spelling
    that never produces an exponent.
    """
    return format(quantity, "f")


class BinanceVenue:
    """`ExecutionVenue` for one Binance testnet market."""

    def __init__(self, exchange: GuardedExchange, profile: VenueProfile) -> None:
        if profile.venue_id not in _BINANCE_MARKETS:
            raise PermanentExchangeError(
                f"{profile.venue_id} is not a Binance market; the Bybit adapter is #112 "
                f"and shares this Protocol rather than this class",
                venue_id=str(profile.venue_id),
                http_status=None,
                venue_code=None,
            )
        self._exchange = exchange
        self._profile = profile
        self._endpoints = _ENDPOINTS[profile.market]

    @property
    def profile(self) -> VenueProfile:
        return self._profile

    async def _call(self, endpoint: str, params: Mapping[str, str] | None = None) -> object:
        """Invoke `endpoint` and return the payload, with every number a `Decimal`.

        The only parsing path in this class. A rejection raises here, classified, rather
        than reaching a model that would report it as a missing field.
        """
        body = await self._exchange.call(endpoint, params or {})
        return raise_for_venue_error(
            parse_venue_payload(body), venue_id=str(self._profile.venue_id)
        )

    def _signed(self, params: Mapping[str, str]) -> dict[str, str]:
        """Add the venue-varying parameters every signed request carries.

        `recvWindow` comes from the profile so that widening it is a reviewed change to
        a named field rather than a literal somebody nudged upward to make a drifting
        clock stop failing -- which does not fix the clock and corrupts every timestamp
        the audit log then records.
        """
        return {**params, "recvWindow": str(self._profile.recv_window_ms)}

    def _expect_sequence(self, payload: object, *, endpoint: str) -> Sequence[object]:
        if not isinstance(payload, Sequence) or isinstance(payload, str | bytes):
            raise PermanentExchangeError(
                f"{endpoint} returned {type(payload).__name__}, expected an array",
                venue_id=str(self._profile.venue_id),
                http_status=None,
                venue_code=None,
            )
        return payload

    async def exchange_info(self) -> VenueExchangeInfo:
        return VenueExchangeInfo.model_validate(await self._call(self._endpoints.exchange_info))

    async def fetch_balances(self) -> tuple[VenueBalance, ...]:
        payload = await self._call(self._endpoints.balances, self._signed({}))
        key = self._endpoints.balances_key
        if key is not None:
            if not isinstance(payload, Mapping) or key not in payload:
                raise PermanentExchangeError(
                    f"{self._endpoints.balances} returned no {key!r} array",
                    venue_id=str(self._profile.venue_id),
                    http_status=None,
                    venue_code=None,
                )
            payload = payload[key]
        entries = self._expect_sequence(payload, endpoint=self._endpoints.balances)
        return tuple(VenueBalance.model_validate(entry) for entry in entries)

    async def fetch_positions(self) -> tuple[VenuePosition, ...]:
        endpoint = self._endpoints.positions
        if endpoint is None:
            # Spot. Not an unimplemented method: a spot account holds balances and has
            # no position concept at all, and returning an empty tuple is the truthful
            # answer that lets a reconciler treat both markets identically.
            return ()
        entries = self._expect_sequence(
            await self._call(endpoint, self._signed({})), endpoint=endpoint
        )
        return tuple(VenuePosition.model_validate(entry) for entry in entries)

    async def fetch_open_orders(self, *, symbol: str | None = None) -> tuple[VenueOrder, ...]:
        params = {} if symbol is None else {"symbol": symbol}
        entries = self._expect_sequence(
            await self._call(self._endpoints.open_orders, self._signed(params)),
            endpoint=self._endpoints.open_orders,
        )
        return tuple(VenueOrder.model_validate(entry) for entry in entries)

    async def fetch_my_trades(
        self, *, symbol: str, since_ms: int | None = None
    ) -> tuple[VenueTrade, ...]:
        params = {"symbol": symbol}
        if since_ms is not None:
            params["startTime"] = str(since_ms)
        entries = self._expect_sequence(
            await self._call(self._endpoints.my_trades, self._signed(params)),
            endpoint=self._endpoints.my_trades,
        )
        return tuple(VenueTrade.model_validate(entry) for entry in entries)

    def _order_params(self, order: Order) -> dict[str, str]:
        assert_client_order_id_acceptable(order.client_order_id, profile=self._profile)
        params = {
            "symbol": order.instrument.symbol,
            "side": order.side.value.upper(),
            "type": order.order_type.value.upper(),
            "quantity": _wire_decimal(order.base_quantity),
            "newClientOrderId": order.client_order_id,
        }
        if order.order_type is OrderType.LIMIT:
            # The domain already refuses a limit order with no price, so this narrowing
            # cannot fail; asserting it here would be a check on a check.
            params["price"] = _wire_decimal(order.limit_quote_price or Decimal(0))
            # timeInForce only on limit orders: Binance rejects a market order that
            # carries one, and a market order that carries a *price* has already been
            # refused by the domain.
            params["timeInForce"] = order.time_in_force.value.upper()
        return self._signed(params)

    async def submit(self, order: Order) -> VenueOrder:
        payload = await self._call(self._endpoints.submit, self._order_params(order))
        return VenueOrder.model_validate(payload)

    async def cancel(self, *, symbol: str, client_order_id: str) -> VenueOrder:
        params = self._signed({"symbol": symbol, "origClientOrderId": client_order_id})
        return VenueOrder.model_validate(await self._call(self._endpoints.cancel, params))

    async def cancel_replace(
        self, *, replacement: Order, cancel_client_order_id: str
    ) -> VenueOrder:
        """Replace a live order with `replacement`.

        Spot's `POST /api/v3/order/cancelReplace` returns an envelope carrying both
        outcomes, so the new order is extracted from `newOrderResponse`. Futures has no
        such endpoint and amends in place with `PUT /fapi/v1/order`, which returns the
        amended order directly. Both are atomic at the venue, which is the property that
        matters: a cancel followed by a place leaves a window in which the position is
        unhedged and the risk engine's view is wrong.
        """
        params = self._order_params(replacement)
        if self._profile.market == "futures":
            # An amend addresses the live order, so it carries that order's client id
            # rather than minting a replacement one.
            params["origClientOrderId"] = cancel_client_order_id
            del params["newClientOrderId"]
            amended = await self._call(self._endpoints.cancel_replace, params)
            return VenueOrder.model_validate(amended)

        params["cancelReplaceMode"] = "STOP_ON_FAILURE"
        params["cancelOrigClientOrderId"] = cancel_client_order_id
        payload = await self._call(self._endpoints.cancel_replace, params)
        if not isinstance(payload, Mapping) or "newOrderResponse" not in payload:
            raise PermanentExchangeError(
                f"{self._endpoints.cancel_replace} returned no newOrderResponse; the "
                f"replacement order's fate is unknown and must be reconciled, not assumed",
                venue_id=str(self._profile.venue_id),
                http_status=None,
                venue_code=None,
            )
        return VenueOrder.model_validate(payload["newOrderResponse"])

    async def aclose(self) -> None:
        await self._exchange.aclose()


async def open_binance_venue(
    venue_id: Venue,
    *,
    api_key: str | None = None,
    secret: str | None = None,
) -> BinanceVenue:
    """Construct a Binance venue over a guarded transport.

    The only constructor. There is no path from here to an exchange object whose
    requests are not host-validated, because `guarded_ccxt` is the only thing that
    produces one and `fking.execution` cannot import `ccxt` to build its own.
    """
    profile = VENUE_PROFILES[venue_id]
    exchange = await guarded_ccxt(
        ccxt_exchange_id=profile.ccxt_exchange_id,
        venue_id=str(profile.venue_id),
        api_key=api_key,
        secret=secret,
        options={
            "defaultType": "spot" if profile.market == "spot" else "future",
            # Corrects a drifting local clock against the venue's server time, which is
            # what keeps a -1021 out of the order path. It is not a substitute for the
            # drift check: an adjustment large enough to matter means the host's clock
            # is wrong, and that is a condition to halt on rather than to paper over.
            "adjustForTimeDifference": True,
            "recvWindow": str(profile.recv_window_ms),
        },
        timeout_seconds=profile.request_timeout_ms / 1000,
    )
    return BinanceVenue(exchange, profile)
