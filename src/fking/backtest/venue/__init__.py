"""The simulated execution venues: scheduled latency, earned fills, counted rejections.

Backtest and live share one code path and only the venue swaps, so everything this
package models is a property of the *venue* and none of it is a property of the harness.
The loop, the clock and the event ordering are `fking.backtest`'s and are used here
unchanged.

Three venues live here and they answer to one Protocol, `SimulatedVenue`:

- `BacktestVenue` -- simulated fills on the event loop's clock.
- `PaperVenue` -- the same simulated fills on an injected live clock, over live data.
- `ReplayVenue` -- recorded responses from a session that already happened.

The first two share `FillSimulation` rather than each holding their own copy of the
pricing, which is what makes backtest/paper agreement structural instead of disciplinary.
`DemoVenue` is not here and does not belong here: it needs the testnet adapters and the
safety kernel's allowlist, and putting a network client behind the backtest engine is the
thing the layering exists to prevent (#11, P4).

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
from fking.backtest.venue._errors import VenueRecordingError, VenueSimulationError
from fking.backtest.venue._filters import (
    SymbolFilters,
    parse_order_rate_budget,
    parse_symbol_filters,
    screen_order,
)
from fking.backtest.venue._protocol import SimulatedVenue
from fking.backtest.venue._rejections import Rejection, RejectReason
from fking.backtest.venue._replay import (
    RecordedFill,
    RecordedRejection,
    RecordedResponse,
    ReplayVenue,
    ResponsePhase,
    VenueRecorder,
    VenueRecording,
)
from fking.backtest.venue._resting import (
    QueueProgress,
    RestingOrder,
    consume,
    join_queue,
    resting_fill_quote_price,
    volume_at_or_beyond,
)
from fking.backtest.venue._simulation import (
    DEFAULT_SEQUENTIAL_FILL_GAP,
    FILL_NAMESPACE,
    FillSimulation,
    SubmissionSchedule,
    VenueFillRecord,
    VenueReport,
)
from fking.backtest.venue._venue import BacktestVenue, PaperVenue

__all__: tuple[str, ...] = (
    "DEFAULT_SEQUENTIAL_FILL_GAP",
    "FILL_NAMESPACE",
    "BacktestVenue",
    "FillSimulation",
    "PaperVenue",
    "QueueProgress",
    "RecordedFill",
    "RecordedRejection",
    "RecordedResponse",
    "RejectReason",
    "Rejection",
    "ReplayVenue",
    "ResponsePhase",
    "RestingOrder",
    "SimulatedVenue",
    "SubmissionSchedule",
    "SymbolFilters",
    "TouchQuote",
    "VenueFillRecord",
    "VenueRecorder",
    "VenueRecording",
    "VenueRecordingError",
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
