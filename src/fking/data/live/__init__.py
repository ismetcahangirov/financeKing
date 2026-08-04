"""Live WebSocket ingestion: closed bars, the trade tape, and every interruption.

`DATA_PIPELINE.md` section 5 is the specification. Live and historical land in the same
canonical records -- `KlineRecord` and `TradeRecord` -- and the only differences are
latency and the failure modes this package exists to make visible:

- **Only closed klines are persisted.** An open kline is a partial aggregate that will
  change, and storing one to update it later is a mutation of a time series, which turns
  replay into a lie: the backtest reads a value the live system never saw in that form.
- **A reconnect is a scheduled event.** Binance closes a healthy connection every 24
  hours, so treating it as an error path logs a daily incident forever.
- **Every interruption becomes a gap row, even a 400 ms one.** Reconnects that recover
  invisibly are how a missing minute becomes unexplainable nine months later.
- **Two independent detectors**, because they fail differently: the sequence detector
  sizes a loss exactly within one message and is blind to silence; the cadence detector
  catches a stream that is connected and sending nothing and cannot size a partial loss.
- **The read loop never defends itself.** `session.read_frames` holds no `except` at
  all; `supervisor` owns the reconnect decision.

The layering is deliberate and is what makes all of the above testable without a socket:
`frames` parses, `detectors` and `router` decide, `writer` persists, `supervisor`
connects. Everything except `writer` and `supervisor` is pure over injected time.

Everything not in `__all__` is private and may change without notice.
"""

from fking.data.live.backoff import (
    RECONNECT_BASE_SECONDS,
    RECONNECT_CAP_SECONDS,
    reconnect_delay_seconds,
)
from fking.data.live.detectors import CADENCE_GRACE, CadenceGapDetector, SequenceGapDetector
from fking.data.live.frames import (
    AggTradeFrame,
    BookTickerFrame,
    KlineFrame,
    LiveFrame,
    MarkPriceFrame,
    parse_frame,
)
from fking.data.live.router import LiveBar, LiveGap, LiveRouter, LiveTrade, RoutedFrame
from fking.data.live.session import WebSocketConnection, read_frames
from fking.data.live.streams import (
    LIVE_BAR_INTERVAL,
    LIVE_STREAM_PROFILES,
    LiveStreamProfile,
    combined_stream_url,
    stream_names_for,
)
from fking.data.live.supervisor import LiveIngestSupervisor, SessionOutcome
from fking.data.live.writer import BAR_SOURCE_STREAM, LiveMarketDataWriter

__all__ = [
    "BAR_SOURCE_STREAM",
    "CADENCE_GRACE",
    "LIVE_BAR_INTERVAL",
    "LIVE_STREAM_PROFILES",
    "RECONNECT_BASE_SECONDS",
    "RECONNECT_CAP_SECONDS",
    "AggTradeFrame",
    "BookTickerFrame",
    "CadenceGapDetector",
    "KlineFrame",
    "LiveBar",
    "LiveFrame",
    "LiveGap",
    "LiveIngestSupervisor",
    "LiveMarketDataWriter",
    "LiveRouter",
    "LiveStreamProfile",
    "LiveTrade",
    "MarkPriceFrame",
    "RoutedFrame",
    "SequenceGapDetector",
    "SessionOutcome",
    "WebSocketConnection",
    "combined_stream_url",
    "parse_frame",
    "read_frames",
    "reconnect_delay_seconds",
    "stream_names_for",
]
