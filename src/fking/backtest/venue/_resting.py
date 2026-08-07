"""Queue position for a resting limit order, modelled as pessimistically as the data allows.

Queue-position data does not exist for us. What is observable is the quoted quantity at
the touch when the order arrived and, afterwards, how much traded. So the rule is:

> A resting order joins the **back** of the quantity that was resting at its price when
> it arrived, and fills only once cumulative traded volume at that price exceeds that
> quantity.

Any strategy whose backtest fills 100% of its limit orders is trading against a market
that does not exist. This model makes that outcome unreachable rather than merely
unlikely -- there is no price at which an order arrives ahead of the book, and no bar
that consumes a queue it never traded through.

**How much of a bar traded at the order's price.** A bar reports one volume over a whole
range, and attributing it to price levels needs trade prints the archive does not carry
at bar granularity. The attribution used here is the share of the bar's range that lies
at or beyond the limit -- a buy at the very low of a bar is credited nothing, a buy above
the bar's high is credited the whole bar. It is a uniform-over-range assumption and it is
stated rather than hidden, and it is conservative in the direction that matters: an order
resting deep inside the range, which is where a passive strategy wants to be, is credited
the least volume and therefore waits longest.

**Fill price.** A resting buy fills at the worst price it could have: the higher of its
limit and the bar's low is `min(limit, high)` -- the most it could have paid without
exceeding its own limit, capped by the highest price anybody traded at. A fill outside
`[low, high]` is a trade with a counterparty that did not exist, and this is the
arithmetic that makes that unreachable rather than tested for afterwards.
"""

from __future__ import annotations

from dataclasses import dataclass, replace
from decimal import Decimal
from typing import Final

from fking.backtest.venue._book import TouchQuote
from fking.domain import Bar, Order, Side

_ZERO: Final = Decimal("0")


@dataclass(frozen=True, slots=True)
class RestingOrder:
    """A limit order working at the venue, and where it stands in the queue.

    Frozen, like every other state-carrying record in this system: a transition returns a
    new one. `consume` is the only transition, and it can only move
    `queue_ahead_base` and `remaining_base` downward -- so a redelivered or replayed bar
    cannot resurrect queue depth that was already traded through.
    """

    order: Order
    limit_quote_price: Decimal
    queue_ahead_base: Decimal
    remaining_base: Decimal
    fill_count: int = 0

    @property
    def is_complete(self) -> bool:
        """Whether nothing is left working."""
        return self.remaining_base <= _ZERO


@dataclass(frozen=True, slots=True)
class QueueProgress:
    """What one bar did to one resting order."""

    resting: RestingOrder
    filled_base_quantity: Decimal
    fill_quote_price: Decimal | None


def volume_at_or_beyond(bar: Bar, side: Side, limit_quote_price: Decimal) -> Decimal:
    """The share of `bar.base_volume` attributable to prints at or beyond `limit_quote_price`.

    Beyond means "on the side that fills a resting order": below the limit for a resting
    buy, above it for a resting sell. A degenerate bar -- high equal to low, which is an
    untraded or single-print interval -- credits the whole volume only when the limit is
    reached exactly, because there is no range to apportion across.
    """
    span = bar.high_quote_price - bar.low_quote_price
    if side is Side.BUY:
        reached = bar.low_quote_price <= limit_quote_price
        if not reached:
            return _ZERO
        if span == _ZERO:
            return bar.base_volume
        share = (limit_quote_price - bar.low_quote_price) / span
    else:
        reached = bar.high_quote_price >= limit_quote_price
        if not reached:
            return _ZERO
        if span == _ZERO:
            return bar.base_volume
        share = (bar.high_quote_price - limit_quote_price) / span
    if share > 1:
        share = Decimal("1")
    return bar.base_volume * share


def resting_fill_quote_price(bar: Bar, side: Side, limit_quote_price: Decimal) -> Decimal:
    """The price a resting order fills at on this bar: its limit, capped into the range.

    Worst-case within what the limit permits. A resting buy never pays more than its
    limit and never pays more than the highest price traded, so it pays the lesser of the
    two -- which is the price a queue at the back of the book would realistically have
    seen, and never the bar's low, which is where an optimistic simulator would fill it.
    """
    if side is Side.BUY:
        return min(limit_quote_price, bar.high_quote_price)
    return max(limit_quote_price, bar.low_quote_price)


def join_queue(order: Order, limit_quote_price: Decimal, quote: TouchQuote) -> RestingOrder:
    """Place `order` at the back of the quantity quoted at its price.

    The quoted touch quantity is the only depth the archive supports, so it is what the
    order queues behind regardless of where in the book its limit sits. Queueing behind
    zero -- which is what a per-level lookup would return for a price away from the touch
    -- would let an order away from the market fill ahead of one at it.
    """
    return RestingOrder(
        order=order,
        limit_quote_price=limit_quote_price,
        queue_ahead_base=quote.depth.depth_at_touch_base,
        remaining_base=order.base_quantity,
    )


def consume(resting: RestingOrder, bar: Bar) -> QueueProgress:
    """Apply one closed bar's traded volume to a resting order.

    Volume consumes the queue ahead first and only the surplus fills the order. That
    ordering is the entire pessimism of the model: an order that arrives at a touch
    quoting 2 BTC needs more than 2 BTC to trade at its price before its own first unit
    executes, and a bar that trades exactly the queue quantity fills nothing.
    """
    available = volume_at_or_beyond(bar, resting.order.side, resting.limit_quote_price)
    if available <= _ZERO:
        return QueueProgress(resting, _ZERO, None)

    if available <= resting.queue_ahead_base:
        return QueueProgress(
            replace(resting, queue_ahead_base=resting.queue_ahead_base - available), _ZERO, None
        )

    surplus = available - resting.queue_ahead_base
    filled = min(surplus, resting.remaining_base)
    quantized = resting.order.instrument.quantize_base_quantity(filled)
    if quantized <= _ZERO:
        # The surplus was smaller than one lot step. Reporting it as a fill would print a
        # quantity the venue cannot express; the queue still moved, so the progress is
        # kept and the order waits for the next bar.
        return QueueProgress(replace(resting, queue_ahead_base=_ZERO), _ZERO, None)

    return QueueProgress(
        replace(
            resting,
            queue_ahead_base=_ZERO,
            remaining_base=resting.remaining_base - quantized,
            fill_count=resting.fill_count + 1,
        ),
        quantized,
        resting_fill_quote_price(bar, resting.order.side, resting.limit_quote_price),
    )
