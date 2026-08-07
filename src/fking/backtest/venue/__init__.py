"""The simulated execution venue: scheduled latency, earned fills, counted rejections.

Backtest and live share one code path and only the venue swaps, so everything this
package models is a property of the *venue* and none of it is a property of the harness.
The loop, the clock and the event ordering are `fking.backtest`'s and are used here
unchanged.

Three things are worth knowing before reading further:

- **The latency is scheduled through the loop, not deducted from a price.** An order
  decided at `t` is acknowledged at `t + decision_to_send + send_to_ack` and can fill no
  earlier than one `ack_to_fill` after that, and the market moves across those intervals
  exactly as it would live.
- **A resting order joins the back of the queue and has to earn its fill.** It fills only
  once volume traded at its price exceeds the quantity that was quoted there when it
  arrived, so a backtest cannot fill 100% of its limit orders.
- **A rejection is a reported outcome carrying the venue's own code and message.** The
  filters come from a recorded `exchangeInfo` payload, not from constants somebody
  believed.

Everything not in `__all__` is private and may change without notice.
"""

from fking.backtest.venue._book import TouchQuote, quote_from_bar
from fking.backtest.venue._errors import VenueSimulationError
from fking.backtest.venue._filters import (
    SymbolFilters,
    parse_order_rate_budget,
    parse_symbol_filters,
    screen_order,
)
from fking.backtest.venue._rejections import Rejection, RejectReason
from fking.backtest.venue._resting import (
    QueueProgress,
    RestingOrder,
    consume,
    join_queue,
    resting_fill_quote_price,
    volume_at_or_beyond,
)
from fking.backtest.venue._venue import (
    DEFAULT_SEQUENTIAL_FILL_GAP,
    BacktestVenue,
    SubmissionSchedule,
    VenueFillRecord,
    VenueReport,
)

__all__: tuple[str, ...] = (
    "DEFAULT_SEQUENTIAL_FILL_GAP",
    "BacktestVenue",
    "QueueProgress",
    "RejectReason",
    "Rejection",
    "RestingOrder",
    "SubmissionSchedule",
    "SymbolFilters",
    "TouchQuote",
    "VenueFillRecord",
    "VenueReport",
    "VenueSimulationError",
    "consume",
    "join_queue",
    "parse_order_rate_budget",
    "parse_symbol_filters",
    "quote_from_bar",
    "resting_fill_quote_price",
    "screen_order",
    "volume_at_or_beyond",
)
