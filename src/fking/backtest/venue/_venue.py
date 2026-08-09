"""The simulated venue: latency scheduled, fills earned, rejections reported.

Three properties carry this module, and each one is the answer to a cheaper design that
produces a better-looking equity curve.

**Latency is scheduled, never deducted.** `submit` does not return a fill. It returns the
event the venue would have sent at `decided_at + decision_to_send + send_to_ack`, and the
fill is resolved later, against whatever the book looks like then. The cheaper design --
charging a basis-point penalty and filling immediately -- models a worse price and cannot
model the case that kills latency-sensitive strategies, which is the market moving
*through* the limit during the window so the order never fills at all. A penalty produces
a slightly worse fill; reality produces no fill, and the difference is the entire trade.

**Fills are resolved against the past.** Every price this venue prints comes from the last
bar that had *closed* at the instant of the fill, and is clamped into that bar's traded
range. A fill decided on bar `[t0, t1)` cannot use anything from `t1` onward, because the
venue has not been handed it yet -- the loop dispatches a bar at its close, and the venue
has no other way to learn a price.

**Rejections are outcomes.** Filter breaches, price bands, unfillable size and the venue's
own order-rate budget are counted and dispatched, not swallowed. A run with zero
rejections against a venue that rejects in reality is a finding about the simulator.

The venue is mutable, deliberately, and it is infrastructure rather than a domain object:
it holds the working book, the resting queue and the report. `docs/rules/immutability.md`
binds domain types, and every record this class hands out -- `RestingOrder`, `Fill`,
`VenueFillRecord`, `VenueReport` -- is frozen.
"""

from __future__ import annotations

from collections import Counter
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime, timedelta
from decimal import ROUND_HALF_EVEN, Decimal
from typing import Final
from uuid import NAMESPACE_URL, UUID, uuid5

from fking.backtest._events import FillEvent, OrderAckEvent, RejectEvent
from fking.backtest.costs import (
    BPS_PER_UNIT,
    CostModel,
    SpreadQuantile,
    walk_depth,
)
from fking.backtest.costs import RejectionReason as DepthRejectionReason
from fking.backtest.venue._book import TouchQuote, quote_from_bar
from fking.backtest.venue._errors import VenueSimulationError
from fking.backtest.venue._filters import SymbolFilters, screen_order
from fking.backtest.venue._rejections import Rejection, RejectReason
from fking.backtest.venue._resting import RestingOrder, consume, join_queue
from fking.domain import Bar, Fill, Order, OrderType, Side, TimeInForce

_ZERO: Final = Decimal("0")
_QUOTE_EXPONENT: Final = Decimal("0.00000001")  # Binance quote precision is 8 decimals

# Two prints of one walked order are separated by a modelled inter-print gap so that each
# fill carries its own instant and the pair sorts deterministically. 5 ms is the order of
# a single matching-engine round trip and is stated here rather than calibrated, because
# the archive carries no per-print timing to calibrate it from.
DEFAULT_SEQUENTIAL_FILL_GAP: Final = timedelta(milliseconds=5)

_FILL_NAMESPACE: Final[UUID] = uuid5(NAMESPACE_URL, "https://fking.local/backtest/fill")


@dataclass(frozen=True, slots=True)
class VenueFillRecord:
    """One printed fill, with what made it passive and what that cost.

    `passive_markout_quote` is carried on the record rather than folded into
    `fill.fee_quote`, because it is not a fee: it is adverse selection, measured five
    minutes after the print, and a strategy that wants to know why its passive edge
    evaporated has to be able to read the two apart. Folding it into the fee would also
    make it invisible to the regression fixture that stops it being dropped.
    """

    fill: Fill
    is_passive: bool
    passive_markout_quote: Decimal


@dataclass(frozen=True, slots=True)
class VenueReport:
    """What the venue did over a run.

    `rejection_counts` carries a key for every taxonomy member, including the zeroes. A
    report that omitted the zeroes would make "this run had no size rejections" and "this
    build does not model size rejections" the same output.
    """

    rejection_counts: Mapping[RejectReason, int]
    fills: tuple[VenueFillRecord, ...]

    @property
    def fill_count(self) -> int:
        """How many prints the venue produced."""
        return len(self.fills)

    @property
    def rejection_total(self) -> int:
        """How many orders the venue refused, across every reason."""
        return sum(self.rejection_counts.values())

    @property
    def passive_fill_count(self) -> int:
        """How many prints were earned by resting rather than by crossing."""
        return sum(1 for record in self.fills if record.is_passive)

    @property
    def passive_markout_quote(self) -> Decimal:
        """Total adverse selection charged against passive prints, in the quote asset."""
        return sum((record.passive_markout_quote for record in self.fills), _ZERO)


@dataclass(frozen=True, slots=True)
class SubmissionSchedule:
    """When a submitted order becomes visible to the venue, and when it could fill.

    Returned alongside the ack so a caller can see the three stages it was charged
    without reaching into the cost model. `decision_to_send` is the stage usually omitted
    and the one this system can least afford to omit: it computes features, may consult
    an LLM agent whose latency is seconds against a free-tier quota, applies risk sizing,
    and only then sends.
    """

    decided_at_utc: datetime
    arrives_at_utc: datetime
    acknowledged_at_utc: datetime
    earliest_fill_at_utc: datetime


class BacktestVenue:
    """The `ExecutionVenue` a backtest runs against.

    The event loop drives it in three places and nowhere else: `observe` when a bar
    closes, `submit` when the risk engine has an order, and `resolve_ack` when the ack
    the venue scheduled comes back around. Everything the venue knows about the market it
    learned from `observe`, which is what makes look-ahead unavailable rather than merely
    discouraged.
    """

    __slots__ = (
        "_cost_model",
        "_fills",
        "_filters",
        "_order_rate_budget",
        "_order_rate_window",
        "_pending",
        "_quantile",
        "_quotes",
        "_recent_submissions",
        "_rejections",
        "_resting",
        "_sequential_fill_gap",
    )

    def __init__(  # noqa: PLR0913 - one keyword per calibrated input the venue needs;
        # a settings object would be a second place to keep the cost model's shape in step.
        self,
        *,
        cost_model: CostModel,
        filters: Mapping[str, SymbolFilters],
        order_rate_budget: int,
        order_rate_window: timedelta,
        quantile: SpreadQuantile = SpreadQuantile.P50,
        sequential_fill_gap: timedelta = DEFAULT_SEQUENTIAL_FILL_GAP,
    ) -> None:
        if order_rate_budget <= 0:
            raise VenueSimulationError(
                f"order_rate_budget must be positive; got {order_rate_budget}"
            )
        if order_rate_window <= timedelta(0):
            raise VenueSimulationError(
                f"order_rate_window must be positive; got {order_rate_window}"
            )
        if sequential_fill_gap < timedelta(0):
            raise VenueSimulationError(
                f"sequential_fill_gap must not be negative; got {sequential_fill_gap}"
            )
        self._cost_model = cost_model
        self._filters = dict(filters)
        self._quantile = quantile
        self._order_rate_budget = order_rate_budget
        self._order_rate_window = order_rate_window
        self._sequential_fill_gap = sequential_fill_gap
        self._quotes: dict[str, TouchQuote] = {}
        self._resting: dict[UUID, RestingOrder] = {}
        self._pending: dict[UUID, Order] = {}
        self._recent_submissions: list[datetime] = []
        self._fills: list[VenueFillRecord] = []
        self._rejections: Counter[RejectReason] = Counter()

    # -- market data ----------------------------------------------------------------

    def observe(self, bar: Bar) -> tuple[FillEvent, ...]:
        """Take a closed bar, update the modelled quote, and pay the queue what it earned.

        Fills are stamped at `bar.close_time_utc`, which is the first instant the bar's
        volume is a fact. Stamping them at the open would date a fill to before the
        information that produced it existed, which is the most productive look-ahead bug
        there is because nothing raises and the equity curve improves.
        """
        symbol = bar.instrument.symbol
        profile = self._cost_model.spread_profile_for(symbol)
        spread_bps = profile.spread_bps(bar.close_time_utc.hour, self._quantile)
        quote = quote_from_bar(
            bar, spread_bps=spread_bps, depth=self._cost_model.depth_profile_for(symbol)
        )
        self._quotes[symbol] = quote

        events: list[FillEvent] = []
        for order_id in sorted(self._resting, key=str):
            resting = self._resting[order_id]
            if resting.order.instrument.symbol != symbol:
                continue
            progress = consume(resting, bar)
            if progress.filled_base_quantity > _ZERO and progress.fill_quote_price is not None:
                events.append(
                    self._print_fill(
                        order=resting.order,
                        base_quantity=progress.filled_base_quantity,
                        quote_price=progress.fill_quote_price,
                        event_time_utc=bar.close_time_utc,
                        is_passive=True,
                        sequence=progress.resting.fill_count,
                    )
                )
            if progress.resting.is_complete:
                del self._resting[order_id]
            else:
                self._resting[order_id] = progress.resting
        return tuple(events)

    # -- order path -----------------------------------------------------------------

    def schedule_for(self, decided_at_utc: datetime) -> SubmissionSchedule:
        """The three latency stages applied to a decision taken at `decided_at_utc`."""
        latency = self._cost_model.latency
        arrives = decided_at_utc + latency.decision_to_send
        acknowledged = arrives + latency.send_to_ack
        return SubmissionSchedule(
            decided_at_utc=decided_at_utc,
            arrives_at_utc=arrives,
            acknowledged_at_utc=acknowledged,
            earliest_fill_at_utc=acknowledged + latency.ack_to_fill,
        )

    def submit(self, order: Order, *, decided_at_utc: datetime) -> OrderAckEvent | RejectEvent:
        """Screen an order and schedule the venue's answer at the instant it would arrive.

        Both answers land at `acknowledged_at_utc`. A rejection is not free and not
        instant: the venue still had to receive the order and reply, and a rejection
        reported at the decision instant would let a strategy retry inside a window that
        does not exist.
        """
        schedule = self._schedule_or_raise(order, decided_at_utc)
        rejection = self._screen(order, schedule.arrives_at_utc)
        if rejection is not None:
            return self._reject(order, schedule.acknowledged_at_utc, rejection)
        self._pending[order.order_id] = order
        return OrderAckEvent(order=order, occurs_at_utc=schedule.acknowledged_at_utc)

    def resolve_ack(self, ack: OrderAckEvent) -> tuple[FillEvent | RejectEvent, ...]:
        """Turn an acknowledged order into prints, a resting entry, or a size rejection.

        A marketable order is walked against the modelled depth here, at the ack instant,
        against the book as it stands then -- not as it stood when the order was decided.
        That is the point of scheduling the latency: the price the strategy saw and the
        price the venue offers are two different numbers, and the gap between them is the
        cost the run is supposed to measure.
        """
        order = self._pending.pop(ack.order.order_id, None)
        if order is None:
            raise VenueSimulationError(
                f"ack for {ack.order.client_order_id} names an order the venue is not "
                f"holding; either it was resolved twice or it was never submitted"
            )
        quote = self._quote_or_raise(order)
        fill_at = ack.occurs_at_utc + self._cost_model.latency.ack_to_fill

        if self._is_marketable(order, quote):
            return self._cross(order, quote, fill_at)

        limit_quote_price = order.limit_quote_price
        if limit_quote_price is None:  # pragma: no cover - Order forbids it at construction
            raise VenueSimulationError(f"{order.client_order_id} is a limit order with no price")
        if order.time_in_force in (TimeInForce.IOC, TimeInForce.FOK):
            # Neither survives to rest. An unmarketable IOC or FOK is refused rather than
            # queued, and it is a depth refusal because the reason is the book: there was
            # nothing to trade against at the price asked for.
            return (
                self._reject(
                    order,
                    fill_at,
                    Rejection(
                        RejectReason.UNFILLABLE_DEPTH,
                        f"{order.time_in_force.value} at {limit_quote_price} did not cross "
                        f"a touch of {quote.touch_for(order.side)}",
                    ),
                ),
            )
        self._resting[order.order_id] = join_queue(order, limit_quote_price, quote)
        return ()

    @property
    def report(self) -> VenueReport:
        """What this venue has done so far, including the reasons it refused."""
        counts = {reason: self._rejections.get(reason, 0) for reason in RejectReason}
        return VenueReport(rejection_counts=counts, fills=tuple(self._fills))

    @property
    def resting_orders(self) -> tuple[RestingOrder, ...]:
        """Everything still working, in a stable order."""
        return tuple(self._resting[key] for key in sorted(self._resting, key=str))

    # -- internals ------------------------------------------------------------------

    def _schedule_or_raise(self, order: Order, decided_at_utc: datetime) -> SubmissionSchedule:
        if decided_at_utc < order.created_at_utc:
            raise VenueSimulationError(
                f"{order.client_order_id} was decided at {decided_at_utc.isoformat()}, "
                f"before it was created at {order.created_at_utc.isoformat()}"
            )
        return self.schedule_for(decided_at_utc)

    def _screen(self, order: Order, arrives_at_utc: datetime) -> Rejection | None:
        if not self._within_order_rate_budget(arrives_at_utc):
            return Rejection(
                RejectReason.ORDER_RATE_BUDGET,
                f"{self._order_rate_budget} new orders already sent within "
                f"{self._order_rate_window}",
            )
        filters = self._filters.get(order.instrument.symbol)
        if filters is None:
            raise VenueSimulationError(
                f"no exchangeInfo filters loaded for {order.instrument.symbol!r}; loaded "
                f"symbols are {sorted(self._filters)}"
            )
        quote = self._quote_or_raise(order)
        # The mid of the modelled touch stands in for Binance's five-minute average
        # price. Both are derived from bars that had already closed, so neither can see
        # the bar the order is about to trade into.
        reference_quote_price = (quote.bid_quote_price + quote.ask_quote_price) / 2
        return screen_order(filters, order, reference_quote_price)

    def _within_order_rate_budget(self, arrives_at_utc: datetime) -> bool:
        """Whether the venue's new-order budget still has room at this instant.

        Refused rather than delayed. A limiter that sleeps until the budget clears turns
        a capacity problem into a latency problem, and latency in the order path is how a
        strategy gets fills at prices its decision never saw
        (`docs/rules/exchange-integration.md`).
        """
        cutoff = arrives_at_utc - self._order_rate_window
        self._recent_submissions = [sent for sent in self._recent_submissions if sent > cutoff]
        if len(self._recent_submissions) >= self._order_rate_budget:
            return False
        self._recent_submissions.append(arrives_at_utc)
        return True

    def _quote_or_raise(self, order: Order) -> TouchQuote:
        quote = self._quotes.get(order.instrument.symbol)
        if quote is None:
            raise VenueSimulationError(
                f"{order.client_order_id} arrived for {order.instrument.symbol} before any "
                f"bar had closed; the venue has no book to price it against and will not "
                f"invent one"
            )
        return quote

    def _is_marketable(self, order: Order, quote: TouchQuote) -> bool:
        if order.order_type is OrderType.MARKET:
            return True
        limit_quote_price = order.limit_quote_price
        if limit_quote_price is None:  # pragma: no cover - Order forbids it
            return True
        touch = quote.touch_for(order.side)
        return limit_quote_price >= touch if order.side is Side.BUY else limit_quote_price <= touch

    def _cross(
        self, order: Order, quote: TouchQuote, fill_at_utc: datetime
    ) -> tuple[FillEvent | RejectEvent, ...]:
        walk = walk_depth(quote.depth, order.base_quantity)
        if walk.rejection is DepthRejectionReason.UNFILLABLE_DEPTH:
            return (
                self._reject(
                    order,
                    fill_at_utc,
                    Rejection(
                        RejectReason.UNFILLABLE_DEPTH,
                        f"{order.base_quantity} beyond the modelled +-1% band of "
                        f"{quote.depth.band_1pct_base}",
                    ),
                ),
            )

        touch = quote.touch_for(order.side)
        at_touch = min(order.base_quantity, quote.depth.depth_at_touch_base)
        at_touch = order.instrument.quantize_base_quantity(at_touch)
        beyond = order.base_quantity - at_touch

        events: list[FillEvent | RejectEvent] = []
        if at_touch > _ZERO:
            events.append(
                self._print_fill(
                    order=order,
                    base_quantity=at_touch,
                    quote_price=quote.clamp_into_range(touch),
                    event_time_utc=fill_at_utc,
                    is_passive=False,
                    sequence=0,
                )
            )
        if beyond > _ZERO:
            # The walk's average slippage covers the whole order; the portion that filled
            # at the touch paid none of it, so the remainder carries all of it. Spreading
            # it evenly would report a touch print at a price the touch never showed.
            walk_bps = walk.depth_slippage_bps * order.base_quantity / beyond
            displaced = BPS_PER_UNIT + walk_bps * order.side.signed_multiplier
            adjusted = touch * displaced / BPS_PER_UNIT
            events.append(
                self._print_fill(
                    order=order,
                    base_quantity=beyond,
                    quote_price=quote.clamp_into_range(adjusted),
                    event_time_utc=fill_at_utc + self._sequential_fill_gap,
                    is_passive=False,
                    sequence=1,
                )
            )
        if not events:  # pragma: no cover - a positive quantity always produces one print
            raise VenueSimulationError(f"{order.client_order_id} crossed and produced no fill")
        return tuple(events)

    def _print_fill(  # noqa: PLR0913 - every field of the fill it prints; a record here
        # would be a second, parallel definition of a Fill inside this module.
        self,
        *,
        order: Order,
        base_quantity: Decimal,
        quote_price: Decimal,
        event_time_utc: datetime,
        is_passive: bool,
        sequence: int,
    ) -> FillEvent:
        notional_quote = base_quantity * quote_price
        fee_bps = self._cost_model.fees.fee_bps_for(is_passive=is_passive)
        fee_quote = (notional_quote * fee_bps / BPS_PER_UNIT).quantize(
            _QUOTE_EXPONENT, rounding=ROUND_HALF_EVEN
        )
        markout_quote = _ZERO
        if is_passive:
            markout_bps = self._cost_model.partial_fills.passive_markout_bps
            markout_quote = (notional_quote * markout_bps / BPS_PER_UNIT).quantize(
                _QUOTE_EXPONENT, rounding=ROUND_HALF_EVEN
            )
        venue_trade_id = f"bt-{order.client_order_id}-{sequence}"
        fill = Fill(
            fill_id=uuid5(_FILL_NAMESPACE, venue_trade_id),
            order_id=order.order_id,
            venue_trade_id=venue_trade_id,
            instrument=order.instrument,
            side=order.side,
            event_time_utc=event_time_utc,
            quote_price=quote_price,
            base_quantity=base_quantity,
            fee_quote=fee_quote,
        )
        self._fills.append(
            VenueFillRecord(fill=fill, is_passive=is_passive, passive_markout_quote=markout_quote)
        )
        return FillEvent(fill=fill)

    def _reject(self, order: Order, occurs_at_utc: datetime, rejection: Rejection) -> RejectEvent:
        self._rejections[rejection.reason] += 1
        return RejectEvent(
            order=order, occurs_at_utc=occurs_at_utc, reason=rejection.as_reject_text()
        )
