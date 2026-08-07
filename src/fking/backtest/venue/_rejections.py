"""The closed rejection taxonomy, and the venue envelope each member mirrors.

A backtest reporting zero rejections against a venue that rejects in reality is
optimistic in a way that no cost parameter captures, so a rejection here is a first-class
outcome: dispatched as a `RejectEvent`, counted in the venue's report, and carrying the
code and message the real venue would have returned.

The envelope table is the part worth being exact about. Binance answers every filter
breach with `-1013` and a message naming the filter that failed, so a `MIN_NOTIONAL`
rejection in a backtest and one from `POST /api/v3/order` are the same string -- which is
what lets the divergence monitor in `fking.execution` compare the two populations without
a translation table that would need updating on both sides.

`UNFILLABLE_DEPTH` has no filter to name. It is `-2010 NEW_ORDER_REJECTED`, the code
Binance uses when an order is refused for a reason that is about the book rather than
about the request, and the modelled reason is stated in the detail rather than smuggled
into the venue message -- the venue never sends the words "modelled depth" and a fixture
comparison must not have to tolerate them.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from enum import StrEnum
from typing import Final


class RejectReason(StrEnum):
    """Why the simulated venue refused an order.

    Closed on purpose. A new reason is a new modelled failure and has to be added here,
    to the envelope table, and to the report -- so a rejection cannot appear in a run
    that the report has no column for, which is how a category of refusals goes
    uncounted while the tearsheet reads clean.
    """

    PRICE_FILTER = "price_filter"
    PERCENT_PRICE_BY_SIDE = "percent_price_by_side"
    LOT_SIZE = "lot_size"
    MIN_NOTIONAL = "min_notional"
    UNFILLABLE_DEPTH = "unfillable_depth"
    ORDER_RATE_BUDGET = "order_rate_budget"


# code, message -- exactly as Binance spells them. -1013 is "Filter failure: <FILTER>",
# -1015 is the order-rate refusal, -2010 is the generic new-order rejection.
_ENVELOPE: Final[Mapping[RejectReason, tuple[int, str]]] = {
    RejectReason.PRICE_FILTER: (-1013, "Filter failure: PRICE_FILTER"),
    RejectReason.PERCENT_PRICE_BY_SIDE: (-1013, "Filter failure: PERCENT_PRICE_BY_SIDE"),
    RejectReason.LOT_SIZE: (-1013, "Filter failure: LOT_SIZE"),
    # The venue's filter is named NOTIONAL; the taxonomy member is MIN_NOTIONAL because
    # only the lower bound is modelled, and a member named NOTIONAL would read as though
    # maxNotional were enforced too.
    RejectReason.MIN_NOTIONAL: (-1013, "Filter failure: NOTIONAL"),
    RejectReason.UNFILLABLE_DEPTH: (-2010, "Order would trigger immediate liquidation"),
    RejectReason.ORDER_RATE_BUDGET: (-1015, "Too many new orders."),
}


@dataclass(frozen=True, slots=True)
class Rejection:
    """One refusal, with the venue envelope it mirrors and the reason it happened here.

    `detail` is the simulator's own explanation -- the quantity that missed the lattice,
    the notional that fell short of the floor -- and is kept separate from
    `venue_message` so that a test comparing against a recorded response compares the
    venue's words alone.
    """

    reason: RejectReason
    detail: str

    @property
    def venue_code(self) -> int:
        """The code the real venue answers this refusal with."""
        return _ENVELOPE[self.reason][0]

    @property
    def venue_message(self) -> str:
        """The message the real venue answers this refusal with, verbatim."""
        return _ENVELOPE[self.reason][1]

    def as_reject_text(self) -> str:
        """The single string a `RejectEvent` carries into the run trace.

        Deterministic by construction: every part of it is derived from the taxonomy
        member and from decimals already on the order, so two runs of one `config_hash`
        produce the same bytes. A reason that varied -- a timestamp, a uuid, a dict
        repr -- would show up as a trace-digest difference with no cause anybody can
        find.
        """
        return f"{self.venue_code} {self.venue_message} [{self.reason.value}] {self.detail}"
